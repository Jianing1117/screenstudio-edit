"""Local Screen Studio mechanics. No semantic model and no credential access."""
import hashlib
import json
import math
import re
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

SKILL = Path(__file__).resolve().parents[1]
SR = 16000


class EditError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise EditError(message)


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def canonical(value):
    if isinstance(value, dict):
        return {k: canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run(args, data=None):
    require(shutil.which(str(args[0])), f'缺少程序：{args[0]}')
    p = subprocess.run(list(map(str, args)), input=data, capture_output=True)
    if p.returncode:
        name = Path(str(args[0])).name
        # Local media tools have actionable diagnostics. Provider CLI errors may
        # contain authorization details, so never echo them or the full command.
        detail = p.stderr.decode(errors='replace')[-2000:] if name in {'ffmpeg', 'ffprobe'} else ''
        raise EditError(f'{name} 执行失败（退出码 {p.returncode}）；未自动重试。' + ('\n'+detail if detail else ''))
    return p.stdout


def union(ranges):
    result = []
    for a, b in sorted(ranges):
        if b <= a:
            continue
        if result and a <= result[-1][1] + 1e-6:
            result[-1][1] = max(result[-1][1], b)
        else:
            result.append([a, b])
    return result


def subtract(a, b, cuts):
    result, cursor = [], a
    for c, d in union(cuts):
        if d <= cursor or c >= b:
            continue
        if c > cursor + 1e-6:
            result.append([cursor, min(c, b)])
        cursor = max(cursor, d)
    if cursor < b - 1e-6:
        result.append([cursor, b])
    return result


def safe_recording_file(root, name, seen=None):
    p = (root / name).resolve()
    require(p.is_relative_to(root.resolve()) and p.is_file(), '录制文件缺失或指向工程外。')
    require(p.suffix.lower() in {'.m3u8', '.m4a', '.m4s', '.mp4', '.mov', '.wav', '.aac', '.ts'},
            '录制文件类型不支持。')
    seen = set() if seen is None else seen
    if p.suffix.lower() == '.m3u8' and p not in seen:
        seen.add(p)
        text = p.read_text()
        require('#EXT-X-KEY' not in text, '不处理含加密密钥的播放列表。')
        refs = [v.strip() for v in text.splitlines() if v.strip() and not v.startswith('#')]
        refs += re.findall(r'URI="([^"]+)"', text)
        for ref in refs:
            q = (p.parent / ref).resolve()
            require('://' not in ref and q.is_relative_to(root.resolve()) and q.is_file(),
                    '播放列表引用了外部路径或网络资源。')
            safe_recording_file(root, str(q.relative_to(root.resolve())), seen)
    return p


def load_project(path):
    path = Path(path).expanduser().resolve()
    require(path.suffix == '.screenstudio' and (path / 'project.json').is_file(), '需要有效的 .screenstudio 工程目录。')
    doc = read(path / 'project.json')
    project = doc.get('json', {})
    require(str(project.get('version', '')).startswith('4.'), '当前仅支持 Screen Studio 4 工程。')
    require(len(project.get('scenes', [])) == 1, '当前入口只处理单场景工程；未修改原件。')
    slices = project['scenes'][0].get('slices', [])
    require(slices, '工程没有保留片段。')
    require(not project['scenes'][0].get('voiceOvers'), '检测到独立配音轨，当前入口不能仅凭麦克风判断空白。')
    audio = project.get('config', {}).get('audio', {})
    require(not audio.get('muteMicrophone', False), '麦克风轨已静音，当前入口不能据此判断实际口播。')
    meta = read(path / 'recording/metadata.json')
    require(len(meta.get('sessions', [])) == 1, '当前只处理单录制会话。')
    media = {}
    for rec in meta.get('recorders', []):
        kind, sessions = rec['type'], rec.get('sessions', [])
        if not sessions:
            continue
        require(len(sessions) == 1, '一个录制通道包含多会话，停止以免套错时间轴。')
        if kind in {'microphone', 'display', 'webcam'}:
            media[kind] = safe_recording_file(path / 'recording', sessions[0]['outputFilename'])
        elif kind not in {'input', 'cursor'}:
            audio = project.get('config', {}).get('audio', {})
            muted = audio.get('muteSystemAudio') if 'system' in kind.lower() else audio.get('muteExternalDeviceAudio')
            require(muted is True, '检测到未静音的系统／外部音轨，需另行确认音画证据。')
    require('microphone' in media and 'display' in media, '需要麦克风和屏幕录制通道。')
    duration = float(meta['sessions'][0]['durationMs'])
    for i, s in enumerate(slices):
        a, b, scale = s['sourceStartMs'], s['sourceEndMs'], s['timeScale']
        require(all(isinstance(v, (float, int)) and math.isfinite(v) for v in [a, b, scale])
                and 0 <= a < b <= duration + 1 and scale > 0, '片段坐标或速度无效。')
        if i:
            require(slices[i - 1]['sourceEndMs'] <= a, '当前入口保留原片顺序；重排或重复使用源片段的工程需单独处理。')
    inventory = [[str(p.relative_to(path / 'recording')), p.stat().st_size, p.stat().st_mtime_ns]
                 for p in sorted((path / 'recording').rglob('*'))
                 if p.is_file() and p.suffix.lower() in {'.m3u8', '.m4a', '.m4s', '.mp4', '.mov', '.wav', '.aac', '.ts'}]
    media_sha = digest({'files': inventory, 'metadata': meta})
    edit_sha = digest({'version': project['version'], 'config': project.get('config', {}), 'scenes': project['scenes']})
    return {'path': path, 'doc': doc, 'slices': slices, 'meta': meta, 'media': media,
            'duration_ms': duration, 'media_sha256': media_sha, 'edit_sha256': edit_sha}


def decode_audio(media, path):
    run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n', '-i', media,
         '-ac', '1', '-ar', SR, '-c:a', 'pcm_s16le', path])


def read_pcm(path):
    with wave.open(str(path)) as f:
        require((f.getframerate(), f.getnchannels(), f.getsampwidth()) == (SR, 1, 2), '分析音轨格式错误。')
        return np.frombuffer(f.readframes(f.getnframes()), '<i2')


def tempo_filters(scale):
    tempo, filters = 1/scale, []
    require(math.isfinite(tempo) and tempo > 0, '变速超出可处理范围。')
    while tempo < .5 or tempo > 2:
        factor = .5 if tempo < .5 else 2
        filters.append(f'atempo={factor}')
        tempo /= factor
    if tempo != 1:
        filters.append(f'atempo={tempo:.12f}')
    return ','.join(filters)


def timeline(pcm, slices):
    pieces, mapping, cursor, sample_cursor = [], [], 0., 0
    for i, s in enumerate(slices):
        a, b, scale = s['sourceStartMs'], s['sourceEndMs'], s['timeScale']
        part = pcm[round(a * 16):round(b * 16)]
        end = cursor + (b - a) * scale
        n = round(end * 16) - sample_cursor
        if scale != 1:
            part = np.frombuffer(run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 's16le', '-ar', SR,
                '-ac', '1', '-i', 'pipe:0', '-af', tempo_filters(scale), '-f', 's16le', 'pipe:1'], part.tobytes()), '<i2')
        pieces.append(np.pad(part[:n], (0, max(0, n - len(part)))))
        mapping.append({'parent_index': i, 'slice_id': s['id'], 'source_start_ms': a, 'source_end_ms': b,
                        'time_scale': scale, 'edited_start_ms': cursor, 'edited_end_ms': end})
        cursor, sample_cursor = end, sample_cursor + n
    return np.concatenate(pieces), mapping


def mapped_pieces(mapping, a, b):
    pieces = []
    for m in mapping:
        c, d = max(a, m['edited_start_ms']), min(b, m['edited_end_ms'])
        if d > c:
            pieces.append({'parent_index': m['parent_index'],
                'start_ms': max(m['source_start_ms'], m['source_start_ms'] + (c - m['edited_start_ms']) / m['time_scale']),
                'end_ms': min(m['source_end_ms'], m['source_start_ms'] + (d - m['edited_start_ms']) / m['time_scale'])})
    return pieces


def intervals(mask, step_ms=1/16, minimum_ms=250):
    edges = np.diff(np.pad(mask.astype(np.int8), (1, 1)))
    return [[float(a * step_ms), float(b * step_ms)] for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
            if (b - a) * step_ms >= minimum_ms]


def vad(pcm):
    import onnxruntime as ort
    provenance = read(SKILL / 'assets/vad-model-provenance.json')
    model = SKILL / 'assets/silero_vad.onnx'
    require(file_hash(model) == provenance['sha256'], 'VAD 模型指纹不符。')
    opts = ort.SessionOptions()
    opts.inter_op_num_threads = opts.intra_op_num_threads = 1
    session = ort.InferenceSession(str(model), opts, providers=['CPUExecutionProvider'])
    state, context, result = np.zeros((2, 1, 128), np.float32), np.zeros((1, 64), np.float32), []
    for pos in range(0, len(pcm), 512):
        frame = pcm[pos:pos + 512].astype(np.float32).reshape(1, -1) / 32768
        frame = np.pad(frame, ((0, 0), (0, 512 - frame.shape[1])))
        inp = np.concatenate([context, frame], axis=1)
        pred, state = session.run(None, {'input': inp, 'state': state, 'sr': np.array(SR, np.int64)})
        result.append(float(pred.flat[0]))
        context = inp[:, -64:]
    return np.array(result, np.float32)


def nonspeech(probs, duration_ms):
    speech, start, end = [], None, None
    for i, probability in enumerate(probs):
        pos = i * 32
        if start is None:
            if probability >= .5:
                start = pos
        elif probability >= .5:
            end = None
        elif probability < .35:
            end = pos if end is None else end
            if pos - end >= 120:
                if end - start >= 120:
                    speech.append([max(0, start - 70), min(duration_ms, end + 70)])
                start = end = None
    if start is not None:
        speech.append([max(0, start - 70), duration_ms])
    return subtract(0, duration_ms, union(speech))


def energy_threshold(pcm):
    levels = []
    for i in range(0, len(pcm) - 400, 1920):
        rms = float(np.sqrt(np.mean(pcm[i:i + 400].astype(float) ** 2)))
        levels.append(20 * np.log10(max(1, rms) / 32768))
    require(levels, '录音过短。')
    p20, p80 = np.percentile(levels, [20, 80])
    return {'p20_dbfs': float(p20), 'p80_dbfs': float(p80),
            'threshold_dbfs': max(-30.5, min(-18., round(float(p20 + .45 * (p80 - p20)), 1)))}


def audio_stats(pcm, a, b):
    values = pcm[max(0, round(a * 16)):min(len(pcm), round(b * 16))].astype(float) / 32768
    require(len(values), '音频检查区间为空。')
    return {'rms_dbfs': float(20 * np.log10(max(1e-8, np.sqrt(np.mean(values ** 2))))),
            'peak_dbfs': float(20 * np.log10(max(1e-8, np.max(np.abs(values)))))}


def screen_activity(project):
    raw = run(['ffmpeg', '-nostdin', '-v', 'error', '-i', project['media']['display'],
               '-vf', 'fps=2,scale=160:90,format=gray', '-f', 'rawvideo', 'pipe:1'])
    frames = np.frombuffer(raw, np.uint8).reshape(-1, 90, 160).astype(np.int16)
    changes = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) / 255
    events = [{'kind': 'screen', 'source_ms': (i + 1) * 500} for i, v in enumerate(changes) if v > .015]
    inputs = next((r for r in project['meta']['recorders'] if r['type'] == 'input'), None)
    if inputs and inputs.get('sessions'):
        session = inputs['sessions'][0]
        origin = session['processTimeStartMs']
        for field, kind in [('mouseClicksFilename', 'click'), ('keyStrokesFilename', 'key_event')]:
            if session.get(field):
                p = (project['path'] / 'recording' / session[field]).resolve()
                require(p.is_relative_to((project['path'] / 'recording').resolve()), '输入事件文件越界。')
                # Extract timestamps only; never inspect recorded key values.
                events.extend({'kind': kind, 'source_ms': float(t) - origin}
                              for t in re.findall(r'"processTimeMs"\s*:\s*([0-9.]+)', p.read_text()))
    return {'frames_scanned': len(frames), 'fps': 2, 'events': sorted(events, key=lambda e: e['source_ms'])}


def activities(events, pieces):
    return [e for e in events if any(p['start_ms'] - 250 <= e['source_ms'] <= p['end_ms'] + 250 for p in pieces)]


def normalize_transcript(payload, duration):
    sentences = payload.get('sentences')
    if sentences is None:
        sentences = [s for track in payload.get('transcripts', []) for s in track.get('sentences', [])]
    require(sentences, '没有可用转录；不能将失败结果当成无声。')
    result = []
    for i, s in enumerate(sorted(sentences, key=lambda s: s['begin_time'])):
        a, b = float(s['begin_time']), float(s['end_time'])
        require(0 <= a < b <= duration + 300, '转录不在当前原片坐标范围内。')
        words = s.get('words', [])
        require(words and all(isinstance(w.get('text'), str) and 0 <= w['begin_time'] < w['end_time'] <= duration + 300
                              for w in words), '需要词级原片毫秒时间戳。')
        result.append({'id': f'U{i+1:04d}', 'begin_time': a, 'end_time': b, 'text': s.get('text', ''), 'words': words})
    return result


def retained_words(sentences, slices):
    return [(s['id'], w) for s in sentences for w in s['words']
            if any(v['sourceStartMs'] <= (w['begin_time'] + w['end_time']) / 2 < v['sourceEndMs'] for v in slices)]
