"""Reuse source evidence and carry reviewed decisions across project versions."""
import copy
import os
import shutil
import uuid
from pathlib import Path

from core import digest, file_hash, read, require, write


def source_identity(project):
    """Content, not location/mtime. Compute once per prepare, never upload media."""
    if 'source_content_sha256' not in project:
        root = project['path'] / 'recording'
        files = {p for p in root.rglob('*') if p.is_file() and p.suffix.lower() in
                 {'.m3u8', '.m4a', '.m4s', '.mp4', '.mov', '.wav', '.aac', '.ts'}}
        for recorder in project['meta'].get('recorders', []):
            if recorder['type'] == 'input':
                for session in recorder.get('sessions', []):
                    for field in ['mouseClicksFilename', 'keyStrokesFilename']:
                        if session.get(field):
                            files.add(root / session[field])
        require(all(p.resolve().is_relative_to(root.resolve()) for p in files), '录制文件越界。')
        project['source_content_sha256'] = digest({'metadata': project['meta'],
            'files': [[str(p.relative_to(root)), file_hash(p)] for p in sorted(files)]})
    return project['source_content_sha256']


def cache_matches(cache, key):
    try:
        return read(cache / 'key.json') == key
    except (OSError, ValueError):
        return False


def evidence_location(args, project):
    root = Path(os.environ.get('SCREENSTUDIO_EDIT_HOME') or '~/.screenstudio-edit').expanduser()
    key = {'schema': 2, 'source_content_sha256': source_identity(project), 'language': args.language,
           'asr_model': args.asr_model, 'vad_model_sha256': file_hash(Path(__file__).resolve().parents[1] / 'assets/silero_vad.onnx')}
    work = Path(args.work_dir).expanduser().resolve() if args.work_dir else None
    donor = None
    if work and not (work / 'media/key.json').exists():
        require(not any((work / 'media' / name).exists() for name in
                        ['microphone.wav', 'transcript.json', 'vad-probabilities.npy', 'screen-activity.json']),
                '工作目录含未绑定来源的缓存；请用新目录，并显式导入历史转录。')
    if work and (work / 'media/key.json').exists():
        require(cache_matches(work / 'media', key), '工作目录的媒体／语言／模型不匹配；请使用新工作目录。')
    else:
        explicit = Path(args.cache_from).expanduser().resolve() if args.cache_from else None
        candidates = [explicit] if explicit else sorted({p.parent for p in root.glob('**/context.json')
                                                       if (p.parent / 'media/key.json').is_file()})
        donor = next((p for p in candidates if (p / 'media/transcript.json').is_file()
                      and cache_matches(p / 'media', key)), None)
        require(not explicit or donor, '--cache-from 没有匹配的有效缓存。')
    if work is None:
        work = donor or root / f'{args.today}-{digest(key)[:12]}'
    require(not work.is_relative_to(project['path']), '工作目录不能写进原工程。')
    cache = work / 'media'
    cache.mkdir(parents=True, exist_ok=True)
    if donor and donor != work:
        for name in ['microphone.wav', 'transcript.json', 'vad-probabilities.npy', 'screen-activity.json']:
            if (donor / 'media' / name).exists() and not (cache / name).exists():
                shutil.copy2(donor / 'media' / name, cache / name)
    write(cache / 'key.json', key)
    return work, str(donor) if donor else None


def pause_signature(candidate):
    return digest([[round(p['start_ms'], 6), round(p['end_ms'], 6)] for p in candidate['pieces']])


def footprint(context, ranges):
    result = []
    for a, b in ranges:
        part = []
        for m in context['mapping']:
            c, d = max(0, a-500, m['source_start_ms']), min(b+500, m['source_end_ms'])
            if c < d:
                item = [c, d, m['time_scale'], m.get('attributes_sha256')]
                if part and part[-1][1] == c and part[-1][2:] == item[2:]:
                    part[-1][1] = d
                else:
                    part.append(item)
        result.append(part)
    return result


def update_plan(work, old_context, old_context_hash, context):
    path = work / 'plan.json'
    old = old_context or {}
    old_speech = {s['id']: s for s in old.get('utterances', [])}
    new_speech = {s['id']: s for s in context['utterances']}
    changed = [u for u in new_speech if old_speech.get(u) != new_speech[u]]
    previous = read(path) if path.exists() else None
    unread = (previous.get('pending_utterance_ids', list(old_speech))
              if previous and not previous.get('semantic_review_complete') else [])
    pending = sorted(set(unread) | set(changed)) if previous else list(new_speech)
    template = {'schema_version': 1, 'edit_sha256': context['edit_sha256'],
        'context_sha256': file_hash(work / 'context.json'), 'semantic_review_complete': False,
        'decisions': [], 'pause_overrides': [], 'pending_pause_overrides': [], 'pending_utterance_ids': pending}
    summary = {'decisions_carried': 0, 'decisions_need_review': 0, 'keeps_carried': 0, 'keeps_need_review': 0,
               'new_or_changed_utterance_ids': changed, 'pending_utterance_ids': pending}
    if previous is None:
        write(path, template)
        return summary
    if previous.get('context_sha256') == template['context_sha256']:
        if previous.get('pending_utterance_ids') != pending:
            previous['pending_utterance_ids'] = pending
            write(path, previous)
        return {**summary, 'decisions_carried': sum(not d.get('needs_review') for d in previous.get('decisions', [])),
            'decisions_need_review': sum(bool(d.get('needs_review')) for d in previous.get('decisions', [])),
            'keeps_carried': len(previous.get('pause_overrides', [])),
            'keeps_need_review': len(previous.get('pending_pause_overrides', []))}
    archive = work / f'plan-previous-{uuid.uuid4().hex[:8]}.json'
    shutil.copy2(path, archive)
    same_source = (old.get('source_content_sha256') == context['source_content_sha256'] if old.get('source_content_sha256')
                   else old.get('media_sha256') == context['media_sha256'])
    valid_base = same_source and previous.get('context_sha256') == old_context_hash and all(
        old.get(k) == context[k] for k in ['transcript_sha256', 'vad_sha256', 'activity_sha256'])
    for original in previous.get('decisions', []):
        d = copy.deepcopy(original)
        refs = d.get('replacement_utterance_ids', [])
        ranges = [[d.get('start_ms', -1), d.get('end_ms', -1)]] + [old_speech[u]['source_ms'] for u in refs if u in old_speech]
        local_same = old.get('edit_sha256') == context['edit_sha256'] or (
            all(m.get('attributes_sha256') for m in old.get('mapping', [])) and
            footprint(old, ranges) == footprint(context, ranges))
        reusable = valid_base and local_same and all(u in old_speech and old_speech[u] == new_speech.get(u) for u in refs)
        if not reusable:
            d.update(needs_review=True, review_reason='关联媒体、证据或附近片段已变化，请复核这条判断。')
        template['decisions'].append(d)
        summary['decisions_need_review' if d.get('needs_review') else 'decisions_carried'] += 1
    old_pauses = {p['id']: p for p in old.get('pause_candidates', [])}
    new_pauses = {pause_signature(p): p for p in context['pause_candidates']}
    template['pending_pause_overrides'] = copy.deepcopy(previous.get('pending_pause_overrides', []))
    for original in previous.get('pause_overrides', []):
        candidate = old_pauses.get(original.get('candidate_id'))
        match = new_pauses.get(pause_signature(candidate)) if candidate and valid_base else None
        if match:
            template['pause_overrides'].append({**original, 'candidate_id': match['id']})
            summary['keeps_carried'] += 1
        else:
            template['pending_pause_overrides'].append({**original, 'source_cuts_ms':
                [[p['start_ms'], p['end_ms']] for p in candidate['pieces']] if candidate else [],
                'review_reason': '原候选已变化；用 pauses 查询后重新关联保留决定。'})
    summary['keeps_need_review'] = len(template['pending_pause_overrides'])
    template['semantic_review_complete'] = bool(previous.get('semantic_review_complete') and valid_base
        and not summary['decisions_need_review'] and not summary['keeps_need_review'] and old_speech == new_speech)
    write(path, template)
    return {**summary, 'previous_plan': str(archive)}
