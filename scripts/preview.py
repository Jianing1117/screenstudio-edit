"""Optional basic proxy. Native animations stay in the editable project."""
import json
from pathlib import Path

from core import load_project, require, run, tempo_filters


def preview(args):
    p = load_project(args.project)
    target = Path(args.output).expanduser().resolve()
    require(target.suffix.lower() == '.mp4' and not target.exists() and not target.is_relative_to(p['path']),
            '预览目标须为工程外、尚不存在的 MP4。')
    total = sum((s['sourceEndMs']-s['sourceStartMs'])*s['timeScale'] for s in p['slices'])
    end = total if args.end_ms is None else args.end_ms
    require(0 <= args.start_ms < end <= total + .001, '预览范围使用有效的成片毫秒。')
    selected, cursor = [], 0
    for sl in p['slices']:
        d = (sl['sourceEndMs']-sl['sourceStartMs'])*sl['timeScale']
        a, b = max(cursor, args.start_ms), min(cursor+d, end)
        if a < b:
            selected.append({**sl, 'sourceStartMs': sl['sourceStartMs']+(a-cursor)/sl['timeScale'],
                             'sourceEndMs': sl['sourceStartMs']+(b-cursor)/sl['timeScale']})
        cursor += d
    duration = (end-args.start_ms)/1000
    select = '+'.join(f'gte(t,{s["sourceStartMs"]/1000:.9f})*lt(t,{s["sourceEndMs"]/1000:.9f})' for s in selected)
    first = selected[0]
    pts = f'(T-{first["sourceStartMs"]/1000:.9f})*{first["timeScale"]:.12f}'
    for prev, cur in zip(selected, selected[1:]):
        start = cur['sourceStartMs']/1000
        pts += f'-{(cur["sourceStartMs"]-prev["sourceEndMs"])/1000*prev["timeScale"]:.12f}*gte(T,{start:.9f})'
        change = cur['timeScale']-prev['timeScale']
        if change:
            pts += f'+({change:.12f})*max(0,T-{start:.9f})'
    timing = f"setpts=PTS-STARTPTS,fps=30,select='{select}',setpts='({pts})/TB',fps=30:round=near:eof_action=pass,trim=duration={duration:.9f}"
    config = p['doc']['json'].get('config', {})
    camera = 'webcam' in p['media'] and not config.get('camera', {}).get('hide', False)
    inputs = [p['media']['display']] + ([p['media']['webcam']] if camera else []) + [p['media']['microphone']]
    audio_index = len(inputs)-1
    probe = json.loads(run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                            'stream=width,height', '-of', 'json', inputs[0]]))['streams'][0]
    width, height = 1280, max(2, round(1280*probe['height']/probe['width']/2)*2)
    filters = [f'[0:v]{timing},scale={width}:{height},setsar=1[screen]']
    if camera:
        size = max(32, round(height*config.get('defaultLayout', {}).get('cameraOverlay', {}).get('overlayCameraSizeRatio01', .3)/2)*2)
        filters += [f'[1:v]{timing},crop=min(iw\\,ih):min(iw\\,ih),scale={size}:{size},setsar=1[cam]',
                    '[screen][cam]overlay=main_w-overlay_w-24:main_h-overlay_h-24:shortest=1[video]']
    else:
        filters.append('[screen]null[video]')
    filters.append(f'[{audio_index}:a]aresample=48000,asetpts=PTS-STARTPTS,asplit={len(selected)}' + ''.join(f'[a{i}]' for i in range(len(selected))))
    elapsed, sample_cursor = 0., 0
    for i, s in enumerate(selected):
        d = (s['sourceEndMs']-s['sourceStartMs'])*s['timeScale']/1000
        elapsed += d
        samples = round(elapsed*48000)-sample_cursor
        sample_cursor += samples
        tempo = tempo_filters(s['timeScale']) + ',' if s['timeScale'] != 1 else ''
        gain = float(s.get('volume', 1))*float(config.get('audio', {}).get('volume', 1))
        volume = f'volume={gain},' if gain != 1 else ''
        filters.append(f'[a{i}]atrim=start_sample={round(s["sourceStartMs"]*48)}:end_sample={round(s["sourceEndMs"]*48)},asetpts=PTS-STARTPTS,'
                       f'{tempo}{volume}apad,atrim=end_sample={samples}[c{i}]')
    filters.append(''.join(f'[c{i}]' for i in range(len(selected)))+f'concat=n={len(selected)}:v=0:a=1[audio]')
    target.parent.mkdir(parents=True, exist_ok=True)
    cmd = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-n']
    for source in inputs:
        cmd += ['-i', source]
    cmd += ['-filter_complex_threads', '2', '-filter_complex', ';'.join(filters), '-map', '[video]', '-map', '[audio]',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-threads', '2', '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '128k', '-fps_mode', 'passthrough', '-t', f'{duration:.9f}', '-movflags', '+faststart', target]
    run(cmd)
    probe = json.loads(run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type,duration', '-of', 'json', target]))
    audio = next(s for s in probe['streams'] if s['codec_type'] == 'audio')
    video = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    require(abs(float(audio['duration'])-duration) < .003 and abs(float(video['duration'])-float(audio['duration'])) < .04,
            '预览音视频时长未通过检查；保留输出供诊断。')
    run(['ffmpeg', '-nostdin', '-v', 'error', '-i', target, '-f', 'null', '-'])
    print(json.dumps({'status': 'proxy_verified', 'preview': str(target), 'seconds': duration,
          'full_decode_passed': True, 'source_edited_range_ms': [args.start_ms, end],
          'limitation': '基础合成，使用原始麦克风；原生缩放、光标、音频预设及精确样式以工程为准。',
          'actual_listening_verified': False}, ensure_ascii=False, indent=2))
