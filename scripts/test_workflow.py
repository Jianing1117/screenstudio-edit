#!/usr/bin/env python3
"""Offline behavioral checks. Keep generated fixtures in the requested scratch folder."""
import argparse
import copy
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

from core import EditError, digest, file_hash, load_project, read, run, timeline, write
from workflow import build_slices


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    root = Path(parser.parse_args().work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.environ['SCREENSTUDIO_EDIT_HOME'] = str(root)
    checks = []

    def check(name, condition):
        assert condition, name
        checks.append(name)

    def cli(*args, fail=False):
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('workflow.py')), *map(str, args)], capture_output=True)
        if result.returncode != (2 if fail else 0):
            raise AssertionError(f'{args[0]}: {result.stderr.decode()[:1000]}')
        checks.append('CLI ' + str(args[0]) + (' rejects' if fail else ' succeeds'))
        return json.loads(result.stderr if fail else result.stdout)

    project = root / 'arbitrary-name.screenstudio'
    recording = project / 'recording'
    require_new = not project.exists()
    check('fresh fixture directory', require_new)
    recording.mkdir(parents=True)
    pcm = (np.sin(np.arange(96000)*2*np.pi*220/16000)*9000).astype('<i2')
    pcm[32000:44800] = 0  # 800 ms exact silence, between two sentences.
    with wave.open(str(recording / 'different-microphone.wav'), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(pcm.tobytes())
    run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-f', 'lavfi', '-i',
         'color=c=blue:size=320x180:rate=30:duration=6', '-c:v', 'libx264', recording / 'different-screen.mp4'])
    metadata = {'test_fixture': str(root), 'sessions': [{'durationMs': 6000}], 'recorders': [
        {'type': 'microphone', 'sessions': [{'outputFilename': 'different-microphone.wav'}]},
        {'type': 'display', 'sessions': [{'outputFilename': 'different-screen.mp4'}]},
        {'type': 'input', 'sessions': []}]}
    write(recording / 'metadata.json', metadata)
    doc = {'json': {'version': '4.0.0', 'id': 'fixture', 'name': 'fixture', 'playheadMs': 1,
        'config': {'padding': 0, 'audio': {'volume': 1, 'muteSystemAudio': True}},
        'scenes': [{'id': 'scene', 'zooms': [{'id': 'manual-zoom', 'startMs': 3000, 'endMs': 4500, 'scale': 2}],
                    'slices': [{'id': 'ordinary', 'sourceStartMs': 0, 'sourceEndMs': 5000, 'timeScale': 1, 'volume': .8},
                               {'id': 'speed', 'sourceStartMs': 5000, 'sourceEndMs': 6000, 'timeScale': 5/6, 'volume': 1}]}]}}
    write(project / 'project.json', doc)
    transcript = {'sentences': [
        {'begin_time': 100, 'end_time': 1900, 'text': '第一次讲解。', 'words': [
            {'begin_time': 100, 'end_time': 900, 'text': '第一次'}, {'begin_time': 1000, 'end_time': 1900, 'text': '讲解。'}]},
        {'begin_time': 2900, 'end_time': 4800, 'text': '完整的替代讲解。', 'words': [
            {'begin_time': 2900, 'end_time': 3800, 'text': '完整的'}, {'begin_time': 3900, 'end_time': 4800, 'text': '替代讲解。'}]},
        {'begin_time': 5100, 'end_time': 5800, 'text': '另一段。', 'words': [
            {'begin_time': 5100, 'end_time': 5800, 'text': '另一段。'}]}]}
    write(root / 'transcript-import.json', transcript)
    original_hash = file_hash(project / 'project.json')
    work = root / 'evidence'
    prepared = cli('prepare', '--project', project, '--work-dir', work,
                   '--transcript-json', root / 'transcript-import.json', '--asr', 'cache-only')
    check('default pause profile', prepared['profile'] == {'pause_ms': 300, 'keep_ms': 180, 'min_fragment_ms': 300})
    check('import requires no ASR', prepared['new_asr_calls'] == 0)
    cached = cli('prepare', '--project', project, '--work-dir', work, '--asr', 'cache-only')
    check('reuse media evidence', cached['new_asr_calls'] == 0 and not cached['vad_computed'] and not cached['screen_scanned'])
    check('speed duration uses multiplication', abs(prepared['seconds']-5.833) < .001)
    cli('dry-run', '--work-dir', work, fail=True)
    silence = cli('dry-run', '--work-dir', work, '--silence-only')
    check('actual silence removed', silence['pause_cuts'] >= 1 and silence['after_seconds'] < silence['before_seconds']-.5)
    check('silence retains 180 ms', any(abs((p['edited_gap_ms'][1]-p['edited_gap_ms'][0]) -
         (p['cut_edited_ms'][1]-p['cut_edited_ms'][0])-180) < 1e-6 for p in read(work / 'context.json')['pause_candidates']))
    plan = read(work / 'plan.json')
    plan['semantic_review_complete'] = True
    decision = {'decision': 'cut', 'confidence': 'high', 'start_ms': 0, 'end_ms': 2000,
                'reason': '先前试讲由完整后文覆盖', 'replacement_utterance_ids': ['U0002']}
    plan['decisions'] = [decision]
    write(work / 'plan.json', plan)
    semantic = cli('dry-run', '--work-dir', work)
    check('whole retake decision applied', semantic['semantic_cuts'] == 1)
    # Editing save metadata and JSON number formatting must not invalidate a plan.
    latest = copy.deepcopy(doc)
    latest['json']['playheadMs'] = 3200
    latest['json']['savedAt'] = 'changed-only-for-test'
    latest['json']['scenes'][0]['slices'][0]['sourceStartMs'] = 0.0
    write(project / 'project.json', latest)
    output = root / 'editable-result.screenstudio'
    cli('apply', '--work-dir', work, '--output', output)
    result = read(output / 'project.json')['json']
    check('latest playhead metadata preserved', result['playheadMs'] == 3200)
    check('styles and manual zooms preserved', result['config'] == doc['json']['config'] and result['scenes'][0]['zooms'] == doc['json']['scenes'][0]['zooms'])
    check('parent speed and volume preserved', all(s['timeScale'] == 5/6 and s['volume'] == 1 for s in result['scenes'][0]['slices'] if s['sourceStartMs'] >= 5000))
    check('apply does not modify source', read(project / 'project.json') == latest)
    cli('apply', '--work-dir', work, '--output', output, fail=True)
    plan['decisions'][0]['reason'] += '（已修改）'
    write(work / 'plan.json', plan)
    cli('apply', '--work-dir', work, '--output', root / 'must-not-exist.screenstudio', fail=True)
    # A replacement must remain complete, not merely have one surviving word.
    plan['decisions'].append({**decision, 'start_ms': 3900, 'end_ms': 5000, 'replacement_utterance_ids': ['U0003']})
    write(work / 'plan.json', plan)
    rejected = cli('dry-run', '--work-dir', work, fail=True)
    check('partial replacement is rejected', '替代发言未完整保留' in rejected['reason'])
    plan['decisions'] = [{**decision, 'start_ms': 500}]
    write(work / 'plan.json', plan)
    cli('dry-run', '--work-dir', work, fail=True)
    # Evidence of a unique screen operation blocks deletion without a visual reason.
    activity = read(work / 'media/screen-activity.json')
    activity['events'] = [{'kind': 'click', 'source_ms': 800}]
    write(work / 'media/screen-activity.json', activity)
    context = read(work / 'context.json')
    context['activity_sha256'] = file_hash(work / 'media/screen-activity.json')
    write(work / 'context.json', context)
    plan['context_sha256'] = file_hash(work / 'context.json')
    plan['decisions'] = [decision]
    write(work / 'plan.json', plan)
    cli('dry-run', '--work-dir', work, fail=True)
    plan['decisions'][0].update(screen_action='redundant', visual_reason='测试夹具中的同一操作已在后文保留')
    write(work / 'plan.json', plan)
    cli('dry-run', '--work-dir', work)
    changed = copy.deepcopy(latest)
    changed['json']['config']['padding'] = 5
    write(project / 'project.json', changed)
    cli('apply', '--work-dir', work, '--output', root / 'stale-must-not-exist.screenstudio', fail=True)
    write(project / 'project.json', latest)
    # Direct boundary checks isolate short-island decisions from VAD uncertainty.
    parent = {'id': 'p', 'sourceStartMs': 0, 'sourceEndMs': 2000, 'timeScale': 1}
    cuts = [{'id': 'P1', 'kind': 'pause', 'pieces': [{'parent_index': 0, 'start_ms': 100, 'end_ms': 600}]}]
    silent = np.zeros(32000, '<i2')
    sl, islands, _, _ = build_slices({'slices': [parent]}, cuts, silent, np.zeros(63), [], 300)
    check('new tiny quiet island removed', len(islands) == 1 and sl[0]['sourceStartMs'] == 600)
    sl, _, withdrawn, _ = build_slices({'slices': [parent]}, cuts, pcm[:32000], np.ones(63), transcript['sentences'], 300)
    check('voiced tiny fragment restores pause cut', len(withdrawn) == 1 and sl == [parent])
    manual = {**parent, 'sourceEndMs': 100}
    sl, _, _, _ = build_slices({'slices': [manual]}, [], silent, np.zeros(63), [], 300)
    check('existing manual short slice preserved', sl == [manual])
    plain, _ = timeline(pcm[:16000], [{**parent, 'sourceEndMs': 1000}])
    check('ordinary speed PCM is bit identical', np.array_equal(plain, pcm[:16000]))
    cli('preview', '--project', output, '--output', root / 'speed-and-cuts.mp4')
    # New output is independently usable as the next input, with the same media key.
    check('output retains media identity', load_project(output)['media_sha256'] == load_project(project)['media_sha256'])
    write(project / 'project.json', doc)
    check('fixture source restored', file_hash(project / 'project.json') == original_hash)
    # A brief high speech-probability region must not be hidden by a long quiet median.
    probabilities = np.load(work / 'media/vad-probabilities.npy')
    probabilities[69:73] = .99
    np.save(work / 'media/vad-probabilities.npy', probabilities)
    cli('prepare', '--project', project, '--work-dir', work, '--asr', 'cache-only')
    pauses = read(work / 'context.json')['pause_candidates']
    protected = [p for p in pauses if any(x['start_ms'] < 2208 < x['end_ms'] for x in p['pieces'])]
    check('brief strong speech evidence protects soft syllable', protected and all(p['status'] == 'review' for p in protected))
    summary = {'passed': len(checks), 'checks': checks, 'new_asr_calls': 0, 'work_dir': str(root)}
    write(root / 'test-results.json', summary)
    print(json.dumps({'passed': len(checks), 'results': str(root / 'test-results.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
