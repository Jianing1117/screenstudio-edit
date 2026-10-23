"""Offline behavior and native-project preservation checks for pointer zooms."""
import argparse
import copy
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from core import EditError, file_hash, load_project, read, run, write
from continuity import source_identity
import zooms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', required=True)
    root = Path(parser.parse_args().work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, value):
        assert value, name
        checks.append(name)

    def refuses(name, fn):
        try:
            fn()
        except EditError:
            check(name, True)
            return
        raise AssertionError(name)

    slices = [{'sourceStartMs': 0, 'sourceEndMs': 8000, 'timeScale': 1}]
    t = np.arange(0, 6000, 100.)
    def track(x, y=.45):
        return np.column_stack([t, np.broadcast_to(x, t.shape), np.broadcast_to(y, t.shape)])
    check('stationary pointer is ignored', not zooms.gestures(track(.4), slices))
    check('small jitter is ignored', not zooms.gestures(track(.4+.002*np.sin(t/80)), slices))
    transit_t = np.arange(0, 8000, 100.)
    transit = np.column_stack([transit_t, .1+transit_t/11000, np.full(len(transit_t), .45)])
    check('simple transit is ignored', not zooms.gestures(transit, slices))
    pointing = track(.4+.06*np.sin(t/130))
    check('horizontal pointing is detected', any(c['kind'] == 'pointing' for c in zooms.gestures(pointing, slices)))
    arrival = track(np.where(t < 1100, .1, .4))
    check('arrival then dwell is detected', any(c['kind'] == 'arrival-dwell' for c in zooms.gestures(arrival, slices)))
    check('removed source material is ignored', not zooms.gestures(arrival, [{**slices[0], 'sourceStartMs': 3500}]))
    check('fast fragments do not get flashing zooms', not zooms.gestures(pointing, [{**slices[0], 'timeScale': .2}]))
    mapped = zooms.gestures(pointing, [{**slices[0], 'sourceEndMs': 500, 'timeScale': .5},
                                    {**slices[0], 'sourceStartMs': 1000, 'timeScale': 2}])
    check('edited coordinates respect speed and removed gaps', all(abs(c['edited_start_ms']-(250+(c['start_ms']-1000)*2)) < .001 for c in mapped))
    line = {'text': 'Small text example', 'confidence': .7, 'box01': [.3, .4, .25, .03]}
    target = zooms.text_target([line], [.42, .45])
    check('underline pointer selects nearby small text', target and target['fits_2x'])
    check('large headings are ignored', zooms.text_target([{**line, 'box01': [.3, .4, .3, .09]}], [.4, .4]) is None)
    check('long lines are protected from crop', not zooms.text_target([{**line, 'box01': [.3, .4, .55, .03]}], [.4, .43])['fits_2x'])
    check('unrelated distant text is ignored', zooms.text_target([line], [.8, .8]) is None)
    check('speech match supports normalized English', zooms.speech_match('Small text example', 'A small text example is here.'))
    check('speech match supports Chinese', zooms.speech_match('把模糊想法用日常语言告诉AI', '你可以把模糊想法用日常语言告诉 AI 就可以了'))
    check('unrelated speech is not a match', not zooms.speech_match('Small text example', 'look at another slide'))
    timed = [{'begin_time': 0, 'end_time': 20000, 'text': 'old phrase current words', 'words': [
        {'begin_time': 0, 'end_time': 1000, 'text': 'old phrase'},
        {'begin_time': 15000, 'end_time': 17000, 'text': 'current words'}]}]
    check('long sentences cannot leak distant phrases into a zoom', zooms.nearby_speech(timed, 15000, 16000) == 'current words')
    check('sentence fallback supports imported evidence without words', zooms.nearby_speech([{k: v for k, v in timed[0].items() if k != 'words'}], 15000, 16000) == timed[0]['text'])

    project_path = root / 'demo.screenstudio'
    recording = project_path / 'recording'
    recording.mkdir(parents=True)
    with wave.open(str(recording / 'mic.wav'), 'wb') as f:
        f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        f.writeframes(np.zeros(160000, dtype='<i2').tobytes())
    # Native text fixture avoids Pillow and ffmpeg's optional drawtext filter.
    drawing = root / 'draw.m'
    drawing.write_text('''#import <AppKit/AppKit.h>
int main(int argc,const char *argv[]){@autoreleasepool{
 NSImage *image=[[NSImage alloc] initWithSize:NSMakeSize(1000,562)];
 [image lockFocus]; [[NSColor whiteColor] set]; NSRectFill(NSMakeRect(0,0,1000,562));
 [@"Small text example" drawAtPoint:NSMakePoint(300,300) withAttributes:@{
 NSFontAttributeName:[NSFont systemFontOfSize:22],NSForegroundColorAttributeName:[NSColor blackColor]}];
 [image unlockFocus]; NSBitmapImageRep *rep=[[NSBitmapImageRep alloc] initWithData:[image TIFFRepresentation]];
 [[rep representationUsingType:NSBitmapImageFileTypePNG properties:@{}]
 writeToFile:[NSString stringWithUTF8String:argv[1]] atomically:YES];
}return 0;}''')
    run(['xcrun', 'clang', '-fobjc-arc', '-framework', 'AppKit', drawing, '-o', root / 'draw'])
    run([root / 'draw', root / 'text.png'])
    run(['ffmpeg', '-nostdin', '-v', 'error', '-n', '-loop', '1', '-i', root / 'text.png',
         '-t', '10', '-r', '10', '-pix_fmt', 'yuv420p', '-c:v', 'libx264', recording / 'screen.mp4'])
    metadata = {'sessions': [{'durationMs': 10000}], 'recorders': [
        {'type': 'microphone', 'sessions': [{'outputFilename': 'mic.wav'}]},
        {'type': 'display', 'sessions': [{'outputFilename': 'screen.mp4',
          'bounds': {'x': 11, 'y': 27, 'width': 1000, 'height': 562}}]},
        {'type': 'input', 'sessions': [{'processTimeStartMs': 1000, 'mouseMovesFilename': 'moves.json'}]}]}
    events = [{'type': 'mouseMoved', 'processTimeMs': float(ms+1000), 'x': float(11+x*1000), 'y': float(27+y*562)}
              for ms, x, y in pointing]
    write(recording / 'moves.json', events)
    write(recording / 'metadata.json', metadata)
    manual = {'id': 'user-zoom', 'sourceStartMs': 8000, 'sourceEndMs': 10000, 'type': 'follow-click-groups', 'zoom': 2}
    doc = {'json': {'version': '4.0.0', 'id': 'demo', 'name': 'demo', 'config': {'padding': 0},
        'scenes': [{'id': 'scene', 'zooms': [manual], 'slices': [dict(slices[0], id='first', volume=.8),
            {'id': 'second', 'sourceStartMs': 8000, 'sourceEndMs': 10000, 'timeScale': 5/6, 'volume': 1}]}]}}
    write(project_path / 'project.json', doc)
    project = load_project(project_path)
    actual, _ = zooms.cursor_track(project)
    check('crop origin and recording clock normalize correctly', np.allclose(actual, pointing))
    work = root / 'work'
    write(work / 'media/key.json', {'schema': 2, 'source_content_sha256': source_identity(project)})
    write(work / 'media/transcript.json', {'sentences': [{'begin_time': 0, 'end_time': 7000, 'text': line['text']}]})
    args = SimpleNamespace(project=project_path, work_dir=work, cache_from=None)
    before = file_hash(project_path / 'project.json')
    result = zooms.prepare(args)
    check('real local OCR produces automatic candidates', result['auto'] >= 1)
    snapshot = read(work / 'zoom-evidence.json')
    plan = read(work / 'zoom-plan.json')
    auto = next(c for c in snapshot['candidates'] if c['status'] == 'auto')
    plan['decisions'] = [{'id': auto['id'], 'decision': 'keep', 'reason': 'viewer needs context'}]
    write(work / 'zoom-plan.json', plan)
    with patch.object(zooms, 'local_ocr', side_effect=AssertionError('OCR repeated')), patch.object(zooms, 'stable_frames', side_effect=AssertionError('scan repeated')):
        zooms.prepare(args)
    check('cached OCR and identical reviewed plan are reused', read(work / 'zoom-plan.json') == plan)
    check('prepare leaves source unchanged', file_hash(project_path / 'project.json') == before)
    plan['decisions'] = []
    write(work / 'zoom-plan.json', plan)
    output = root / 'editable.screenstudio'
    applied = zooms.apply(SimpleNamespace(work_dir=work, output=output))
    final = load_project(output)
    check('native manual screen zooms are written', applied['added'] >= 1 and all(z['zoom'] == 2 and z['type'] == 'manual' and z['presentation'] == 'screen'
          for z in final['doc']['json']['scenes'][0]['zooms'] if z['id'] != manual['id']))
    check('all original manual zooms survive unchanged', manual in final['doc']['json']['scenes'][0]['zooms'])
    check('slices speed gain and config remain identical', final['slices'] == project['slices'] and final['doc']['json']['config'] == doc['json']['config'])
    check('apply leaves original byte-identical', file_hash(project_path / 'project.json') == before)
    refuses('existing result is never overwritten', lambda: zooms.apply(SimpleNamespace(work_dir=work, output=output)))
    rerun = zooms.prepare(SimpleNamespace(project=output, work_dir=root / 'again', cache_from=work))
    check('repeated zoom stage does not duplicate existing zooms', rerun['auto'] == 0)
    changed = copy.deepcopy(doc)
    changed['json']['config']['padding'] = 4
    write(project_path / 'project.json', changed)
    refuses('stale edit plan is rejected', lambda: zooms.apply(SimpleNamespace(work_dir=work, output=root / 'stale.screenstudio')))
    write(project_path / 'project.json', doc)
    events[0]['x'] += 20
    write(recording / 'moves.json', events)
    refuses('changed cursor track invalidates execution', lambda: zooms.apply(SimpleNamespace(work_dir=work, output=root / 'stale.screenstudio')))
    c = copy.deepcopy(auto)
    c.update(status='blocked', stable=False)
    refuses('unsafe review cannot be forced into an automatic zoom', lambda: zooms.additions({'candidates': [c]},
        {'decisions': [{'id': c['id'], 'decision': 'add', 'reason': 'force'}]}, []))
    check('uncertain candidates are kept by default', not zooms.additions({'candidates': [{**auto, 'status': 'review'}]}, {'decisions': []}, []))
    write(root / 'test-results.json', {'passed': len(checks), 'checks': checks, 'new_asr_calls': 0})
    print(json.dumps({'passed': len(checks), 'new_asr_calls': 0}))


if __name__ == '__main__':
    main()
