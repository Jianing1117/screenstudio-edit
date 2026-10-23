"""Sparse, local pointer/text zooms. Never changes slices or existing zooms."""
import copy
import json
import shutil
import sys
import unicodedata
import uuid
from pathlib import Path

import numpy as np

from core import digest, file_hash, load_project, read, require, run, write
from continuity import source_identity

VERSION = 2


def cursor_track(project):
    recorders = project['meta']['recorders']
    inputs = [r for r in recorders if r['type'] == 'input' and r.get('sessions')]
    displays = [r for r in recorders if r['type'] == 'display' and r.get('sessions')]
    require(len(inputs) == len(displays) == 1, '缩放需要单会话鼠标轨迹和屏幕坐标。')
    session = inputs[0]['sessions'][0]
    bounds = displays[0]['sessions'][0].get('bounds')
    require(bounds and bounds['width'] > 0 and bounds['height'] > 0 and
            bounds.get('yAxis', 'topBasedIncreasingDownwards') == 'topBasedIncreasingDownwards',
            '屏幕坐标格式未覆盖，未生成缩放。')
    root = project['path'] / 'recording'
    path = (root / session['mouseMovesFilename']).resolve()
    require(path.is_relative_to(root.resolve()) and path.is_file(), '鼠标轨迹缺失或越界。')
    rows = [[e['processTimeMs'] - session['processTimeStartMs'],
             (e['x']-bounds['x'])/bounds['width'], (e['y']-bounds['y'])/bounds['height']]
            for e in read(path) if e.get('type') == 'mouseMoved']
    track = np.array(sorted(rows), dtype=float).reshape(-1, 3)
    require(np.isfinite(track).all(), '鼠标坐标无效。')
    return track, file_hash(path)


def gestures(track, slices):
    """Arrival+dwell or horizontal pointing; no stationary/transit-only zooms."""
    result, edited = [], 0.
    if not len(track):
        return result
    times = track[:, 0]
    for index, sl in enumerate(slices):
        a, b, scale = sl['sourceStartMs'], sl['sourceEndMs'], sl['timeScale']
        last = -1e9
        for t in np.arange(a+350, b-2000, 500):
            if t-last < 2500:
                continue
            part = track[(times >= t) & (times <= t+2000)]
            kind = None
            if len(part) >= 8:
                dx = np.diff(part[:, 1])
                signs = np.sign(dx[np.abs(dx) >= .0016])
                spans = np.percentile(part[:, 1:], 90, axis=0)-np.percentile(part[:, 1:], 10, axis=0)
                if .02 <= spans[0] <= .34 and spans[1] < .055 and np.abs(dx).sum() >= .073 and (signs[1:] != signs[:-1]).sum() >= 2:
                    kind = 'pointing'
            # Sample-and-hold catches a real arrival even when the pointer then
            # emits no events. Arrival must be recent and within this slice.
            before = track[(times >= max(a, t-600)) & (times < t)]
            arrivals = track[(times >= t) & (times <= t+500)]
            if kind is None and len(before) and len(arrivals):
                anchor = arrivals[-1, 1:]
                later = track[(times > t+500) & (times <= t+2000), 1:]
                held = not len(later) or np.linalg.norm(later-anchor, axis=1).max() <= .022
                if held and np.linalg.norm(before[0, 1:]-anchor) >= .06:
                    kind, part = 'arrival-dwell', arrivals[-1:]
            if kind:
                point = np.median(part[:, 1:], axis=0).tolist()
                if .06 < point[0] < .94 and .09 < point[1] < .90:
                    start, end = max(a, t-250), min(b, t+3500)
                    if (end-start)*scale < 1800:
                        continue
                    result.append({'start_ms': float(start), 'end_ms': float(end), 'sample_ms': float(t+1000),
                        'edited_start_ms': edited+(start-a)*scale, 'point01': point, 'kind': kind,
                        'slice_index': index, 'time_scale': scale})
                    last = t
        edited += (b-a)*scale
    return result


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).lower() if c.isalnum())


def speech_match(text, speech):
    a, b = normalized(text), normalized(speech)
    grams = {a[i:i+2] for i in range(len(a)-1)}
    hits = sum(g in b for g in grams)
    return hits >= 3 and hits/max(1, len(grams)) >= .22


def text_target(lines, point):
    def distance(line):
        x, y, w, h = line['box01']
        return max(x-point[0], 0, point[0]-x-w)**2 + 3*max(y-point[1], 0, point[1]-y-h)**2
    useful = [l for l in lines if l['confidence'] >= .35 and len(normalized(l['text'])) >= 6 and
              .012 <= l['box01'][3] <= .055]
    if not useful:
        return None
    line = min(useful, key=distance)
    x, y, w, h = line['box01']
    if not (x-.025 <= point[0] <= x+w+.025 and y-.055 <= point[1] <= y+h+.055):
        return None
    center = [min(.75, max(.25, x+w/2)), min(.75, max(.25, y+h/2))]
    # The complete OCR line, with a margin, must fit in a 2x viewport.
    fits = (x >= center[0]-.23 and x+w <= center[0]+.23 and
            y >= center[1]-.23 and y+h <= center[1]+.23)
    return {'text': line['text'], 'box01': line['box01'], 'center01': center, 'fits_2x': fits}


def frame(project, cache, ms):
    path = cache / f'{round(ms)}.jpg'
    if not path.exists():
        run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-ss', f'{ms/1000:.6f}',
             '-i', project['media']['display'], '-frames:v', '1', '-vf', 'scale=1280:-2', path])
    return path


def stable_frames(paths):
    frames = [np.frombuffer(run(['ffmpeg', '-nostdin', '-v', 'error', '-i', p, '-vf',
               'scale=160:90,format=gray', '-frames:v', '1', '-f', 'rawvideo', 'pipe:1']), np.uint8).astype(float)
              for p in paths]
    return all(np.abs(f-frames[0]).mean() < 5 and (np.abs(f-frames[0]) > 32).mean() < .04 for f in frames[1:])


def local_ocr(cache, paths):
    require(sys.platform == 'darwin', '本地文字缩放需要 macOS Vision；原剪辑未改变。')
    source = Path(__file__).with_name('local_ocr.m')
    helper = cache.parent / f'local-ocr-{file_hash(source)[:12]}'
    if not helper.exists():
        run(['xcrun', 'clang', '-fobjc-arc', '-framework', 'Foundation', '-framework', 'Vision', source, '-o', helper])
    return json.loads(run([helper, *paths]))


def cached_stable(cache, paths):
    result = cache / ('stable-'+digest([p.name for p in paths])[:16]+'.json')
    if not result.exists():
        write(result, stable_frames(paths))
    return read(result)


def prepare(args):
    project = load_project(args.project)
    crop = project['doc']['json'].get('config', {}).get('crop', {}).get('rect01', {})
    require(not crop or crop == {'x': 0, 'y': 0, 'width': 1, 'height': 1},
            '工程另有画面裁剪，缩放坐标未覆盖；保留初剪，请人工缩放。')
    work = Path(args.work_dir).expanduser().resolve()
    require(not work.is_relative_to(project['path']), '工作目录不能位于工程内。')
    track, cursor_sha = cursor_track(project)
    source_sha = source_identity(project)
    cache_from = Path(args.cache_from).expanduser().resolve() if args.cache_from else work
    transcript_path = cache_from / 'media/transcript.json'
    key_path = cache_from / 'media/key.json'
    valid = (key_path.exists() and transcript_path.exists() and
             read(key_path).get('source_content_sha256') == source_sha)
    require(not args.cache_from or valid, '指定转录缓存不同源或不完整。')
    transcript = read(transcript_path)['sentences'] if valid else []
    key = {'version': VERSION, 'source_sha256': source_sha, 'cursor_sha256': cursor_sha,
           'transcript_sha256': file_hash(transcript_path) if valid else None,
           'ocr_helper_sha256': file_hash(Path(__file__).with_name('local_ocr.m'))}
    cache = work / 'zoom-cache' / digest(key)[:16]
    cache.mkdir(parents=True, exist_ok=True)
    original = project['doc']['json']['scenes'][0].get('zooms', [])
    candidates = []
    for item in gestures(track, project['slices']):
        a, b = item['start_ms'], item['end_ms']
        if any(z['sourceStartMs'] < b+500 and z['sourceEndMs'] > a-500 for z in original):
            continue
        item = copy.deepcopy(item)
        paths = [frame(project, cache, t) for t in [a, item['sample_ms'], b-100]]
        ocr_path = cache / f'{round(item["sample_ms"])}-ocr.json'
        if not ocr_path.exists():
            write(ocr_path, {'lines': local_ocr(cache, [paths[1]])[0]['lines'], 'stable': cached_stable(cache, paths)})
        evidence = read(ocr_path)
        target = text_target(evidence['lines'], item['point01'])
        if target is None:
            continue
        speech = ''.join(s['text'] for s in transcript if s['begin_time'] < b+3000 and s['end_time'] > a-3000)
        reasons = []
        if not target['fits_2x']:
            reasons.append('line_would_be_cropped')
        if not evidence['stable']:
            reasons.append('screen_changed')
        if not speech_match(target['text'], speech):
            reasons.append('speech_match_uncertain' if valid else 'no_bound_transcript')
        status = 'blocked' if not target['fits_2x'] or not evidence['stable'] else 'review' if reasons else 'auto'
        item.update(target=target, speech=speech[:350], reasons=reasons, status=status,
                    frame=str(paths[1]), stable=evidence['stable'])
        # Merge repeated gestures at the same text inside one retained slice.
        previous = candidates[-1] if candidates else None
        if previous and previous['slice_index'] == item['slice_index'] and previous['target']['text'] == target['text'] and a <= previous['end_ms']+1000:
            # Never bridge a gap without checking it. Each candidate interval is
            # stable; same target/center still needs the endpoint to match.
            same = cached_stable(cache, [Path(previous['frame']), paths[1]])
            if same and b-previous['start_ms'] <= 10000:
                previous['end_ms'] = b
                previous['reasons'] = sorted(set(previous['reasons']+reasons))
                previous['status'] = ('blocked' if 'blocked' in {previous['status'], status}
                                      else 'review' if previous['reasons'] else 'auto')
                continue
        item['id'] = 'Z'+digest([a, b, target['box01']])[:10]
        if previous and (a-previous['end_ms'])*item['time_scale'] < 1600:
            item['reasons'].append('nearby_zoom')
            if item['status'] != 'blocked':
                item['status'] = 'review'
        candidates.append(item)
    evidence = {'schema_version': VERSION, 'project': str(project['path']), 'edit_sha256': project['edit_sha256'],
        'media_sha256': project['media_sha256'], **key, 'existing_zooms': len(original), 'candidates': candidates}
    evidence_path = work / 'zoom-evidence.json'
    # Reuse reviewed decisions only when every piece of evidence is identical.
    plan_path = work / 'zoom-plan.json'
    old = read(plan_path) if plan_path.exists() else {}
    write(evidence_path, evidence)
    evidence_sha = file_hash(evidence_path)
    if old.get('evidence_sha256') != evidence_sha:
        if old:
            shutil.copy2(plan_path, work / f'zoom-plan-previous-{uuid.uuid4().hex[:8]}.json')
        write(plan_path, {'evidence_sha256': evidence_sha, 'decisions': []})
    return {'status': 'zoom_candidates_ready', 'auto': sum(c['status'] == 'auto' for c in candidates),
            'review': sum(c['status'] == 'review' for c in candidates),
            'blocked': sum(c['status'] == 'blocked' for c in candidates), 'plan': str(plan_path),
            'review_command': f'zooms --work-dir "{work}"', 'new_asr_calls': 0}


def query(args):
    require(args.offset >= 0 and 1 <= args.limit <= 100, '分页参数无效。')
    evidence = read(Path(args.work_dir).expanduser() / 'zoom-evidence.json')
    items = [c for c in evidence['candidates'] if args.status == 'all' or c['status'] == args.status]
    return {'total': len(items), 'blocked_kept': sum(c['status'] == 'blocked' for c in evidence['candidates']),
            'candidates': items[args.offset:args.offset+args.limit]}


def additions(evidence, plan, existing):
    require(len({d['id'] for d in plan['decisions']}) == len(plan['decisions']), '缩放决策编号重复。')
    ids = {c['id'] for c in evidence['candidates']}
    require(all(d.get('id') in ids and d.get('decision') in {'add', 'keep'} and d.get('reason') for d in plan['decisions']),
            '缩放决策需要有效编号、add/keep 和理由。')
    decisions = {d['id']: d['decision'] for d in plan['decisions']}
    result = []
    for c in evidence['candidates']:
        chosen = decisions.get(c['id'], 'add' if c['status'] == 'auto' else 'keep')
        if chosen != 'add':
            continue
        require(c['status'] != 'blocked' and c['target']['fits_2x'] and c['stable'],
                '整句放不下或画面变化的候选，请在 Screen Studio 人工处理。')
        require(all(z['sourceStartMs'] >= c['end_ms']+500 or z['sourceEndMs'] <= c['start_ms']-500
                    for z in existing+result), '新增缩放区间冲突，请保留其中一项。')
        result.append({'id': 'ssedit-'+c['id'], 'sourceStartMs': c['start_ms'], 'sourceEndMs': c['end_ms'],
            'zoom': 2, 'type': 'manual', 'presentation': 'screen', 'manualTargetPoint': dict(zip(['x', 'y'], c['target']['center01'])),
            'loupeOptions': {'radius01': .35, 'bevelRatio': .2, 'chromaticAberration': .5, 'hasGlassOptics': True},
            'isDisabled': False, 'hasInstantAnimation': False})
    return result


def apply(args):
    work = Path(args.work_dir).expanduser().resolve()
    evidence_path = work / 'zoom-evidence.json'
    evidence, plan = read(evidence_path), read(work / 'zoom-plan.json')
    require(plan['evidence_sha256'] == file_hash(evidence_path), '缩放证据已变化，请重新 zoom-prepare。')
    project = load_project(evidence['project'])
    _, cursor_sha = cursor_track(project)
    require(project['edit_sha256'] == evidence['edit_sha256'] and project['media_sha256'] == evidence['media_sha256'] and
            source_identity(project) == evidence['source_sha256'] and cursor_sha == evidence['cursor_sha256'],
            '工程或鼠标轨迹已改变，请重新 zoom-prepare。')
    original = project['doc']['json']['scenes'][0].get('zooms', [])
    new = additions(evidence, plan, original)
    if not new:
        result = {'status': 'no_zooms_added', 'project': str(project['path']), 'added': 0}
    else:
        target = Path(args.output).expanduser().resolve() if args.output else project['path'].with_name(project['path'].stem+'-指示缩放.screenstudio')
        require(target.suffix == '.screenstudio' and not target.exists() and not target.is_relative_to(project['path']),
                '缩放目标须为工程外、尚不存在的 .screenstudio 路径。')
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f'.{target.stem}-{uuid.uuid4().hex[:8]}.partial.screenstudio')
        if sys.platform == 'darwin':
            run(['cp', '-cRp', project['path'], partial])
        else:
            shutil.copytree(project['path'], partial, symlinks=True)
        latest = load_project(project['path'])
        require(latest['edit_sha256'] == project['edit_sha256'] and latest['media_sha256'] == project['media_sha256'],
                '复制中工程发生变化，保留 partial，未交付。')
        doc = copy.deepcopy(latest['doc'])
        doc['json']['id'], doc['json']['name'] = uuid.uuid4().hex[:12], target.stem
        doc['json']['scenes'][0]['zooms'] = sorted(original+new, key=lambda z: z['sourceStartMs'])
        write(partial / 'project.json', doc)
        verified = load_project(partial)
        expected = copy.deepcopy(project['doc'])
        expected['json'].update(id=doc['json']['id'], name=doc['json']['name'])
        expected['json']['scenes'][0]['zooms'] = doc['json']['scenes'][0]['zooms']
        require(verified['doc'] == expected and verified['slices'] == project['slices'], '缩放副本验证失败。')
        require(not target.exists(), '目标已被占用；保留 partial，未覆盖。')
        partial.rename(target)
        result = {'status': 'zooms_applied', 'project': str(target), 'added': len(new)}
    result.update(existing_preserved=len(original), slices_unchanged=True, new_asr_calls=0,
                  pending_review=[c['id'] for c in evidence['candidates'] if c['status'] == 'review' and
                                  c['id'] not in {d['id'] for d in plan['decisions']}])
    write(work / 'zoom-applied.json', result)
    return result


def register(sub):
    p = sub.add_parser('zoom-prepare', help='初剪后，本地筛选鼠标指示文字的 2 倍缩放')
    p.add_argument('--project', required=True)
    p.add_argument('--work-dir', required=True)
    p.add_argument('--cache-from', help='同源 prepare 工作目录；缺省使用当前工作目录的转录')
    p = sub.add_parser('zooms', help='只返回待复核缩放摘要，按需看单帧')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--status', choices=['auto', 'review', 'blocked', 'all'], default='review')
    p.add_argument('--offset', type=int, default=0)
    p.add_argument('--limit', type=int, default=10)
    p = sub.add_parser('zoom-apply', help='另存带原生可编辑缩放的副本，不改变切点')
    p.add_argument('--work-dir', required=True)
    p.add_argument('--output')
