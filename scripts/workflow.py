#!/usr/bin/env python3
"""One entry: prepare → caller-reviewed plan → dry-run → apply; optional proxy."""
import argparse
import copy
import datetime
import json
import shutil
import sys
import uuid
from pathlib import Path

import numpy as np

from core import (EditError, activities, audio_stats, decode_audio, digest,
                  energy_threshold, file_hash, intervals, load_project, mapped_pieces,
                  nonspeech, normalize_transcript, read, read_pcm, require,
                  retained_words, run, subtract, timeline, union, vad, write)
from continuity import evidence_location, pause_signature, update_plan


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def today():
    return datetime.date.today().isoformat()


def state(work):
    work = Path(work).expanduser().resolve()
    context = read(work / 'context.json')
    project = load_project(context['project'])
    require(project['edit_sha256'] == context['edit_sha256'] and project['media_sha256'] == context['media_sha256'],
            '工程有效剪辑状态或媒体已变化；请重新 prepare，转录缓存仍可复用。')
    return work, context, project


def prepare(args):
    project = load_project(args.project)
    args.today = today()
    work, donor = evidence_location(args, project)
    old_context = read(work / 'context.json') if (work / 'context.json').exists() else None
    old_context_hash = file_hash(work / 'context.json') if old_context else None
    cache = work / 'media'
    audio = cache / 'microphone.wav'
    if not audio.exists():
        decode_audio(project['media']['microphone'], audio)
    pcm = read_pcm(audio)
    require(abs(len(pcm)/16-project['duration_ms']) <= 300,
            '麦克风与录制会话长度不符；停止以免把缺失音频当成空白。')
    transcript_path = cache / 'transcript.json'
    new_asr = 0
    if args.transcript_json:
        imported = normalize_transcript(read(args.transcript_json), project['duration_ms'])
        if transcript_path.exists():
            require(read(transcript_path)['sentences'] == imported, '已有转录与导入不同；保留旧缓存，请使用新工作目录。')
        else:
            write(transcript_path, {'audio_sha256': file_hash(audio), 'sentences': imported})
    elif not transcript_path.exists():
        require(args.asr != 'cache-only', '没有转录缓存；提供 --transcript-json 或明确使用 --asr bailian。')
        raw = cache / 'asr-response.json'
        if not raw.exists():
            run(['bl', 'speech', 'recognize', '--url', audio, '--model', args.asr_model,
                 '--language', args.language, '--out', raw, '--quiet', '--timeout', '300'] + (['--config', args.asr_profile] if args.asr_profile else []))
            new_asr = 1
        write(transcript_path, {'audio_sha256': file_hash(audio),
              'sentences': normalize_transcript(read(raw), project['duration_ms'])})
    transcript = read(transcript_path)
    require(transcript['audio_sha256'] == file_hash(audio), '分析音轨已变化，停止使用旧转录。')
    sentences = transcript['sentences']
    vad_path = cache / 'vad-probabilities.npy'
    new_vad = not vad_path.exists()
    if new_vad:
        np.save(vad_path, vad(pcm))
    probabilities = np.load(vad_path)
    require(len(probabilities) == (len(pcm)+511)//512 and np.isfinite(probabilities).all()
            and np.min(probabilities) >= 0 and np.max(probabilities) <= 1, 'VAD 缓存不完整，请选新的工作目录重新检测。')
    activity_path = cache / 'screen-activity.json'
    new_screen = not activity_path.exists()
    if new_screen:
        from core import screen_activity
        write(activity_path, screen_activity(project))
    activity = read(activity_path)
    calibration = energy_threshold(pcm)
    current, mapping = timeline(pcm, project['slices'])
    for m, s in zip(mapping, project['slices']):
        m['attributes_sha256'] = digest({k: v for k, v in s.items() if k not in {'id', 'sourceStartMs', 'sourceEndMs'}})
    energy = intervals(np.abs(current.astype(np.int32)) <= 32768 * 10 ** (calibration['threshold_dbfs'] / 20))
    voice_gaps = nonspeech(probabilities, len(pcm) / 16)
    mapped_voice_gaps = []
    for m in mapping:
        for a, b in voice_gaps:
            c, d = max(a, m['source_start_ms']), min(b, m['source_end_ms'])
            if c < d:
                mapped_voice_gaps.append([m['edited_start_ms'] + (c - m['source_start_ms']) * m['time_scale'],
                                         m['edited_start_ms'] + (d - m['source_start_ms']) * m['time_scale']])
    candidates = []
    # Keep independent evidence independent: a broad uncertain VAD-only region
    # must not swallow a confidently measured energy-silence interval.
    regions = sorted(set(tuple(r) for r in energy + union(mapped_voice_gaps)))
    for a, b in regions:
        if b - a <= args.pause_ms:
            continue
        after = min(80, args.keep_ms / 3)
        ca, cb = a + args.keep_ms - after, b - after
        if cb - ca < 120:
            continue
        pieces = mapped_pieces(mapping, ca, cb)
        probability_pieces = [probabilities[int(p['start_ms'] / 32):int(p['end_ms'] / 32) + 1] for p in pieces]
        probs = np.concatenate(probability_pieces)
        stats = audio_stats(current, ca, cb)
        reasons = []
        if float(np.median(probs)) > .9 or any(intervals(p > .9, 32, 96) for p in probability_pieces):
            reasons.append('high_vad_probability_keep_soft_syllable')
        if stats['rms_dbfs'] > -35:
            reasons.append('audible_non_speech_needs_review')
        overlaps = [s['id'] for s in sentences if any(s['begin_time'] < p['end_ms'] and s['end_time'] > p['start_ms'] for p in pieces)]
        candidates.append({'id': 'P'+pause_signature({'pieces': pieces})[:10], 'edited_gap_ms': [a, b],
            'cut_edited_ms': [ca, cb], 'pieces': pieces, 'audio': stats,
            'vad_probability_median': float(np.median(probs)), 'status': 'review' if reasons else 'auto',
            'keep_reasons': reasons, 'asr_overlap_utterances': overlaps,
            'activity': activities(activity['events'], pieces)})
    words = retained_words(sentences, project['slices'])
    utterances = []
    for s in sentences:
        kept = [w for uid, w in words if uid == s['id']]
        if kept:
            utterances.append({'id': s['id'], 'source_ms': [s['begin_time'], s['end_time']],
                'text': ''.join(w['text'] + w.get('punctuation', '') for w in kept)})
    context = {'schema_version': 1, 'project': str(project['path']), 'work_dir': str(work),
        'edit_sha256': project['edit_sha256'], 'media_sha256': project['media_sha256'],
        'source_content_sha256': project['source_content_sha256'],
        'transcript_sha256': file_hash(transcript_path), 'vad_sha256': file_hash(vad_path),
        'activity_sha256': file_hash(activity_path), 'profile': {'pause_ms': args.pause_ms,
        'keep_ms': args.keep_ms, 'min_fragment_ms': args.min_fragment_ms}, 'calibration': calibration,
        'mapping': mapping, 'utterances': utterances, 'pause_candidates': candidates,
        'current_duration_ms': mapping[-1]['edited_end_ms'], 'source_duration_ms': project['duration_ms']}
    write(work / 'context.json', context)
    (work / 'transcript.txt').write_text('\n'.join(
        f"{s['id']} [原片 {s['source_ms'][0]/1000:.2f}–{s['source_ms'][1]/1000:.2f}] {s['text']}" for s in utterances) + '\n')
    plan = work / 'plan.json'
    plan_reuse = update_plan(work, old_context, old_context_hash, context)
    emit({'status': 'evidence_ready', 'work_dir': str(work), 'transcript': str(work / 'transcript.txt'),
          'plan': str(plan), 'context': str(work / 'context.json'), 'profile': context['profile'],
          'seconds': round(context['current_duration_ms'] / 1000, 3), 'utterances': len(utterances),
          'pause_candidates': len(candidates), 'risk_pauses_kept': sum(p['status'] == 'review' for p in candidates),
          'new_asr_calls': new_asr, 'cache_reused_from': donor, 'vad_computed': new_vad, 'screen_scanned': new_screen,
          'pause_review_command': 'pauses --work-dir "'+str(work)+'" --status auto --activity-only', 'plan_reuse': plan_reuse})


def inspect(args):
    work, context, project = state(args.work_dir)
    require(0 <= args.start_ms < args.end_ms <= context['source_duration_ms'] + 300, '查询原片范围无效。')
    transcript = read(work / 'media/transcript.json')['sentences']
    words = [{'id': s['id'], **w} for s in transcript for w in s['words']
             if w['begin_time'] < args.end_ms and w['end_time'] > args.start_ms]
    emit({'source_range_ms': [args.start_ms, args.end_ms], 'words': words[:200],
          'words_truncated': len(words) > 200, 'mapped_slices': [m for m in context['mapping']
          if m['source_start_ms'] < args.end_ms and m['source_end_ms'] > args.start_ms],
          'activity': activities(read(work / 'media/screen-activity.json')['events'],
                                  [{'start_ms': args.start_ms, 'end_ms': args.end_ms}])[:40]})


def pause_groups(candidates):
    groups = []
    for p in candidates:
        touching, separate = [], []
        for group in groups:
            overlap = any(a['start_ms'] < b['end_ms'] and a['end_ms'] > b['start_ms']
                          for a in p['pieces'] for q in group for b in q['pieces'])
            (touching if overlap else separate).append(group)
        groups = separate + [[q for group in touching for q in group] + [p]]
    return sorted(groups, key=lambda g: min(v['start_ms'] for p in g for v in p['pieces']))


def pauses(args):
    _, context, _ = state(args.work_dir)
    require(args.offset >= 0 and 1 <= args.limit <= 100, 'offset 须非负，limit 须在 1–100。')
    groups = pause_groups([p for p in context['pause_candidates'] if args.status == 'all' or p['status'] == args.status])
    selected = [g for g in groups if (not args.activity_only or any(p['activity'] for p in g)) and
                (not args.candidate_id or any(p['id'] == args.candidate_id for p in g))]
    items = []
    for group in selected[args.offset:args.offset+args.limit]:
        ranges = union([[v['start_ms'], v['end_ms']] for p in group for v in p['pieces']])
        a, b = ranges[0][0], ranges[-1][1]
        speech = context['utterances']
        before = [s for s in speech if s['source_ms'][0] < a][-1:]
        after = [s for s in speech if s['source_ms'][1] > b][:1]
        events = {(e['kind'], e['source_ms']) for p in group for e in p['activity']}
        statuses = {p['status'] for p in group}
        levels = [p['audio']['rms_dbfs'] for p in group]
        items.append({'ids': [p['id'] for p in group], 'status': next(iter(statuses)) if len(statuses) == 1 else 'mixed',
            'source_cuts_ms': ranges,
            'edited_gap_ms': [min(p['edited_gap_ms'][0] for p in group), max(p['edited_gap_ms'][1] for p in group)],
            'cut_edited_ms': [min(p['cut_edited_ms'][0] for p in group), max(p['cut_edited_ms'][1] for p in group)],
            'keep_reasons': sorted({r for p in group for r in p['keep_reasons']}),
            'audio': {'candidate_rms_dbfs_range': [min(levels), max(levels)],
                      'peak_dbfs': max(p['audio']['peak_dbfs'] for p in group)},
            'activity': {k: sum(kind == k for kind, _ in events) for k in sorted({kind for kind, _ in events})},
            'nearby_speech': [{'id': s['id'], 'text': s['text'][:120]} for s in {s['id']: s for s in before+after}.values()]})
    next_offset = args.offset+len(items)
    emit({'total': len(selected), 'candidate_total': sum(len(g) for g in selected), 'offset': args.offset, 'items': items,
          'next_offset': next_offset if next_offset < len(selected) else None})


def build_slices(project, cuts, pcm, probabilities, sentences, minimum_ms):
    active = list(cuts)
    withdrawn = []
    for _ in range(len(cuts) + 1):
        result, islands, retry = [], [], None
        for i, parent in enumerate(project['slices']):
            selected = [c for c in active if any(p['parent_index'] == i for p in c['pieces'])]
            exclusions = [[p['start_ms'], p['end_ms']] for c in selected for p in c['pieces'] if p['parent_index'] == i]
            for a, b in subtract(parent['sourceStartMs'], parent['sourceEndMs'], exclusions):
                short = (b - a) * parent['timeScale'] < minimum_ms
                unchanged = a == parent['sourceStartMs'] and b == parent['sourceEndMs']
                if short and not unchanged:
                    stats = audio_stats(pcm, a, b)
                    ps = probabilities[int(a / 32):int(b / 32) + 1]
                    word_overlap = any(w['begin_time'] < b and w['end_time'] > a for s in sentences for w in s['words'])
                    if stats['rms_dbfs'] < -45 and len(ps) and float(np.max(ps)) < .2 and not word_overlap:
                        islands.append({'parent_index': i, 'source_ms': [a, b], 'audio': stats})
                        continue
                    adjacent = [c for c in selected if c['kind'] == 'pause' and any(
                        abs(p['end_ms'] - a) < .001 or abs(p['start_ms'] - b) < .001 for p in c['pieces'] if p['parent_index'] == i)]
                    require(adjacent, f'语义计划产生有声短片段 {a:.2f}–{b:.2f}ms，修改计划后重跑 dry-run。')
                    retry = adjacent[0]
                    break
                sl = {**parent, 'sourceStartMs': a, 'sourceEndMs': b}
                if not unchanged:
                    sl['id'] = digest([parent['id'], a, b])[:12]
                result.append(sl)
            if retry:
                break
        if retry:
            active.remove(retry)
            withdrawn.append({'id': retry['id'], 'reason': 'protect_voiced_short_fragment'})
            continue
        require(result, '计划会删空工程，停止。')
        return result, islands, withdrawn, active
    raise EditError('无法消除有声碎片。')


def dry_run(args):
    work, context, project = state(args.work_dir)
    require(file_hash(work / 'media/transcript.json') == context['transcript_sha256'] and
            file_hash(work / 'media/vad-probabilities.npy') == context['vad_sha256'] and
            file_hash(work / 'media/screen-activity.json') == context['activity_sha256'], '缓存证据已变化，重新 prepare。')
    plan = read(work / 'plan.json')
    require(plan.get('edit_sha256') == context['edit_sha256'] and plan.get('context_sha256') == file_hash(work / 'context.json'), '计划指纹过期。')
    require(not plan.get('pending_pause_overrides'), '旧停顿保留决定尚待关联，先用 pauses 复核 pending_pause_overrides。')
    require(args.silence_only or not any(d.get('needs_review') for d in plan.get('decisions', [])), '仅复核标记 needs_review 的判断，再更新计划。')
    require(args.silence_only or plan.get('semantic_review_complete') is True, '先完成语义审查；只有明确只清空白才用 --silence-only。')
    sentences = read(work / 'media/transcript.json')['sentences']
    events = read(work / 'media/screen-activity.json')['events']
    overrides = plan.get('pause_overrides', [])
    known = {p['id'] for p in context['pause_candidates']}
    require(all(o.get('candidate_id') in known and o.get('action') == 'keep' and o.get('reason') for o in overrides), '停顿覆写只接受已有候选的 keep 及理由。')
    kept = {o['candidate_id'] for o in overrides}
    protected = [piece for p in context['pause_candidates'] if p['id'] in kept for piece in p['pieces']]
    # Independent detectors can propose overlapping pauses. An explicit keep
    # must protect the interval from every detector, not just one candidate ID.
    kept |= {p['id'] for p in context['pause_candidates'] if any(
        a['start_ms'] < b['end_ms'] and a['end_ms'] > b['start_ms'] for a in p['pieces'] for b in protected)}
    cuts = [{'id': p['id'], 'kind': 'pause', 'pieces': p['pieces']} for p in context['pause_candidates']
            if p['status'] == 'auto' and p['id'] not in kept]
    for cut in cuts:
        cut['pieces'] = [piece for piece in cut['pieces'] if
            (project['slices'][piece['parent_index']]['sourceEndMs']-project['slices'][piece['parent_index']]['sourceStartMs'])*
            project['slices'][piece['parent_index']]['timeScale'] >= context['profile']['min_fragment_ms']]
    cuts = [c for c in cuts if c['pieces']]
    semantic = []
    for i, d in enumerate([] if args.silence_only else plan.get('decisions', [])):
        if d.get('decision') != 'cut' or d.get('confidence') not in {'high', 'medium'}:
            continue
        a, b = d.get('start_ms'), d.get('end_ms')
        require(all(isinstance(v, (int, float)) and not isinstance(v, bool) and np.isfinite(v) for v in [a, b])
                and 0 <= a < b <= context['source_duration_ms'], '语义删减坐标无效。')
        require(d.get('reason') and d.get('replacement_utterance_ids'), '有声删减需要理由及保留的替代发言编号。')
        require(set(d['replacement_utterance_ids']) <= {s['id'] for s in sentences}, '替代发言编号不存在。')
        pieces = [{'parent_index': j, 'start_ms': max(a, s['sourceStartMs']), 'end_ms': min(b, s['sourceEndMs'])}
                  for j, s in enumerate(project['slices']) if s['sourceStartMs'] < b and s['sourceEndMs'] > a]
        require(pieces, '语义删减不在当前保留时间线内。')
        activity = activities(events, pieces)
        if activity:
            require(d.get('screen_action') == 'redundant' and d.get('visual_reason'), '有画面／输入活动的语义删减需要明确的演示覆盖判断。')
        boundary = [w for s in sentences for w in s['words'] if any(w['begin_time'] + 30 < v < w['end_time'] - 30 for v in [a, b])]
        require(not boundary or d.get('boundary_reviewed') is True, '语义切口落在 ASR 词内，请 inspect 后明确 boundary_reviewed。')
        cut = {'id': f'S{i+1:04d}', 'kind': 'semantic', 'pieces': pieces, 'decision': d,
               'activity': activity, 'longer_than_5s': b-a > 5000, 'boundary_words': boundary}
        cuts.append(cut)
        semantic.append(cut)
    slices, islands, withdrawn, active = build_slices(project, cuts, read_pcm(work / 'media/microphone.wav'),
        np.load(work / 'media/vad-probabilities.npy'), sentences, context['profile']['min_fragment_ms'])
    before_words = retained_words(sentences, project['slices'])
    after_words = retained_words(sentences, slices)
    for c in semantic:
        for uid in c['decision']['replacement_utterance_ids']:
            expected = [w for word_id, w in before_words if word_id == uid]
            retained = [w for word_id, w in after_words if word_id == uid]
            require(expected and retained == expected, '替代发言未完整保留，请修改计划。')
    frozen = {'schema_version': 1, 'edit_sha256': context['edit_sha256'], 'media_sha256': context['media_sha256'],
              'context_sha256': file_hash(work / 'context.json'), 'plan_sha256': file_hash(work / 'plan.json'),
              'new_slices': slices, 'cuts': active, 'silent_islands_removed': islands, 'pause_cuts_withdrawn': withdrawn}
    write(work / 'cuts.json', frozen)
    after = sum((s['sourceEndMs']-s['sourceStartMs'])*s['timeScale'] for s in slices)
    after_pcm, after_mapping = timeline(read_pcm(work / 'media/microphone.wav'), slices)
    threshold = 32768*10**(context['calibration']['threshold_dbfs']/20)
    remaining_gaps = intervals(np.abs(after_pcm.astype(np.int32)) <= threshold, minimum_ms=context['profile']['pause_ms'])
    report = {'status': 'dry_run_ready', 'cuts_sha256': file_hash(work / 'cuts.json'),
        'before_seconds': context['current_duration_ms']/1000, 'after_seconds': after/1000,
        'pause_cuts': sum(c['kind'] == 'pause' for c in active), 'semantic_cuts': len(semantic),
        'silent_islands_removed': len(islands), 'pause_cuts_withdrawn': withdrawn,
        'slices_before': len(project['slices']), 'slices_after': len(slices),
        'risk_pauses_kept': [p['id'] for p in context['pause_candidates'] if p['status'] == 'review' or p['id'] in kept],
        'long_semantic_cuts': [c['id'] for c in semantic if c['longer_than_5s']],
        'semantic_activity_overlaps': [c['id'] for c in semantic if c['activity']],
        'automatic_pauses_with_activity': [p['id'] for p in context['pause_candidates'] if p['activity'] and any(c['id'] == p['id'] for c in active)],
        'post_cut_energy_rescan_executed': True, 'post_cut_long_gaps_ms': remaining_gaps,
        'actual_listening_verified': False}
    write(work / 'report.json', report)
    emit({k: v for k, v in report.items() if k not in {'risk_pauses_kept', 'automatic_pauses_with_activity', 'post_cut_long_gaps_ms'}} |
         {'risks_kept': len(report['risk_pauses_kept']), 'remaining_long_gaps': len(remaining_gaps), 'full_report': str(work / 'report.json')})


def apply(args):
    work, context, project = state(args.work_dir)
    frozen, report = read(work / 'cuts.json'), read(work / 'report.json')
    require(file_hash(work / 'cuts.json') == report['cuts_sha256'] and
            file_hash(work / 'context.json') == frozen['context_sha256'] and
            context['edit_sha256'] == frozen['edit_sha256'] and context['media_sha256'] == frozen['media_sha256'], '冻结计划或证据已变化。')
    require(not frozen['plan_sha256'] or file_hash(work / 'plan.json') == frozen['plan_sha256'], '计划已修改，重新 dry-run。')
    if args.output:
        target = Path(args.output).expanduser().resolve()
    else:
        folder = project['path'].parent
        version = 1
        target = folder / f'{project["path"].stem}-初剪-v{version}.screenstudio'
        while target.exists():
            version += 1
            target = folder / f'{project["path"].stem}-初剪-v{version}.screenstudio'
    require(target.suffix == '.screenstudio' and not target.exists() and not target.is_relative_to(project['path']), '目标必须是原工程之外、尚不存在的 .screenstudio 路径。')
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f'.{target.stem}-{uuid.uuid4().hex[:8]}.partial.screenstudio')
    if sys.platform == 'darwin':
        run(['cp', '-cRp', project['path'], partial])
    else:
        shutil.copytree(project['path'], partial, symlinks=True)
    latest = load_project(project['path'])
    require(latest['edit_sha256'] == context['edit_sha256'] and latest['media_sha256'] == context['media_sha256'],
            '复制过程中工程发生变化；保留 partial，未交付。')
    doc = copy.deepcopy(latest['doc'])
    doc['json']['id'], doc['json']['name'] = uuid.uuid4().hex[:12], target.stem
    doc['json']['scenes'][0]['slices'] = frozen['new_slices']
    write(partial / 'project.json', doc)
    require(not target.exists(), '目标被其他操作占用；保留 partial，不覆盖。')
    partial.rename(target)
    verified = load_project(target)
    require(verified['slices'] == frozen['new_slices'] and verified['doc']['json']['config'] == project['doc']['json']['config'], '副本验证失败。')
    result = {**report, 'status': 'applied', 'project': str(target), 'project_edit_sha256': verified['edit_sha256'],
              'source_edit_content_unchanged': True, 'native_styles_preserved': True, 'new_asr_calls': 0}
    write(work / 'applied.json', result)
    emit({'status': 'applied', 'project': str(target), 'seconds': report['after_seconds'],
          'slices': len(frozen['new_slices']), 'report': str(work / 'applied.json')})


def main():
    parser = argparse.ArgumentParser(description='Screen Studio 可复用初剪：prepare → dry-run → apply；语义判断由当前 Agent 完成。')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare', help='准备或复用证据，不写原工程')
    p.add_argument('--project', required=True)
    p.add_argument('--work-dir')
    p.add_argument('--cache-from', help='从指定工作目录复用同源缓存，适用于默认缓存目录之外的缓存')
    p.add_argument('--transcript-json')
    p.add_argument('--asr', choices=['bailian', 'cache-only'], default='cache-only')
    p.add_argument('--asr-profile', help='百炼 CLI 配置档；省略时用当前激活的配置')
    p.add_argument('--asr-model', default='fun-asr')
    p.add_argument('--language', default='zh')
    p.add_argument('--pause-ms', type=float, default=300)
    p.add_argument('--keep-ms', type=float, default=180)
    p.add_argument('--min-fragment-ms', type=float, default=300)
    p = sub.add_parser('inspect', help='仅查看局部原片毫秒范围的词级证据')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--start-ms', type=float, required=True)
    p.add_argument('--end-ms', type=float, required=True)
    p = sub.add_parser('pauses', help='分页查询停顿、画面活动和前后文')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--status', choices=['all', 'auto', 'review'], default='all')
    p.add_argument('--activity-only', action='store_true')
    p.add_argument('--candidate-id')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=20)
    p = sub.add_parser('dry-run', help='验证语义计划并冻结切点，不写原工程')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--silence-only', action='store_true')
    p = sub.add_parser('apply', help='只执行已冻结且未过期的结果，另存工程')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--output', help='省略时另存到源工程旁边，名称自动递增版本')
    p = sub.add_parser('preview', help='按当前副本生成基础审片 MP4')
    p.add_argument('--project', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--start-ms', type=float, default=0)
    p.add_argument('--end-ms', type=float)
    import zooms
    zooms.register(sub)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            require(0 <= args.keep_ms < args.pause_ms and args.pause_ms >= 120 and args.min_fragment_ms > 0, '停顿参数无效。')
        if args.command in {'zoom-prepare', 'zooms', 'zoom-apply'}:
            emit({'zoom-prepare': zooms.prepare, 'zooms': zooms.query, 'zoom-apply': zooms.apply}[args.command](args))
        elif args.command == 'preview':
            from preview import preview
            preview(args)
        else:
            {'prepare': prepare, 'inspect': inspect, 'pauses': pauses, 'dry-run': dry_run, 'apply': apply}[args.command](args)
    except (EditError, OSError, ValueError, KeyError) as error:
        print(json.dumps({'status': 'stopped', 'reason': str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
