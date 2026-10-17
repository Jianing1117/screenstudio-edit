#!/usr/bin/env python3
"""Regression checks for cache discovery, query access and incremental plans."""
import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import EditError, file_hash, load_project, read, run, tempo_filters, timeline, write
from continuity import source_identity, update_plan
from workflow import pause_groups


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
        p = subprocess.run([sys.executable, str(Path(__file__).with_name('workflow.py')), *map(str, args)], capture_output=True)
        assert p.returncode == (2 if fail else 0), p.stderr.decode()[:2000]
        return json.loads(p.stderr if fail else p.stdout)

    run([sys.executable, Path(__file__).with_name('test_workflow.py'), '--work-dir', root / 'baseline'])
    baseline = read(root / 'baseline/test-results.json')
    source = root / 'baseline/arbitrary-name.screenstudio'
    work = root / 'baseline/evidence'
    # A conservative keep and two independently reviewable source regions.
    doc = read(source / 'project.json')
    first = doc['json']['scenes'][0]['slices'][0]
    doc['json']['scenes'][0]['slices'][:1] = [{**first, 'sourceEndMs': 1000}, {**first, 'id': 'second', 'sourceStartMs': 1000}]
    write(source / 'project.json', doc)
    prepared = cli('prepare', '--project', source, '--work-dir', work)
    check('prepare review command selects automatic pauses', '--status auto' in prepared['pause_review_command'])
    check('prepare does not save unused current audio', not (work / 'current-audio.npy').exists())
    plan = read(work / 'plan.json')
    candidate = next(p for p in read(work / 'context.json')['pause_candidates']
                     if p['edited_gap_ms'][1]-p['edited_gap_ms'][0] < 1000 and any(
                         x['start_ms'] < 2300 < x['end_ms'] for x in p['pieces']))
    plan.update(semantic_review_complete=True, decisions=[
        {'decision': 'cut', 'confidence': 'high', 'start_ms': 0, 'end_ms': 2000,
         'replacement_utterance_ids': ['U0002'], 'reason': '完整后文覆盖试讲',
         'screen_action': 'redundant', 'visual_reason': '夹具中的相同操作已保留'},
        {'decision': 'keep', 'start_ms': 3900, 'end_ms': 5000, 'replacement_utterance_ids': ['U0003'], 'reason': '保护独有说明'}],
        pause_overrides=[{'candidate_id': candidate['id'], 'action': 'keep', 'reason': '读屏'}], pending_pause_overrides=[])
    write(work / 'plan.json', plan)
    # Style-only changes do not throw away completed semantic work.
    styled = copy.deepcopy(doc)
    styled['json']['config']['padding'] = 5
    write(source / 'project.json', styled)
    cli('prepare', '--project', source, '--work-dir', work)
    migrated = read(work / 'plan.json')
    check('style-only edit preserves decisions and completed review', migrated['decisions'] == plan['decisions'] and migrated['semantic_review_complete'])
    check('unchanged pause keep survives', migrated['pause_overrides'] == plan['pause_overrides'])
    # A speed change near only one decision invalidates that judgment alone.
    changed = copy.deepcopy(styled)
    changed['json']['scenes'][0]['slices'][0]['timeScale'] = 1.1
    write(source / 'project.json', changed)
    cli('prepare', '--project', source, '--work-dir', work)
    changed_plan = read(work / 'plan.json')
    check('only affected decision marked for review', changed_plan['decisions'][0].get('needs_review') is True and not changed_plan['decisions'][1].get('needs_review'))
    check('decision reasons retained', [d['reason'] for d in changed_plan['decisions']] == [d['reason'] for d in plan['decisions']])
    changed_plan['semantic_review_complete'] = True
    write(work / 'plan.json', changed_plan)
    check('pending judgment cannot silently execute', 'needs_review' in cli('dry-run', '--work-dir', work, fail=True)['reason'])
    # Legacy numbered pause IDs migrate by source coordinates rather than number.
    context = read(work / 'context.json')
    old_pause = next(p for p in context['pause_candidates'] if p['id'] == changed_plan['pause_overrides'][0]['candidate_id'])
    expected_id = old_pause['id']
    old_pause['id'] = 'P0001'
    write(work / 'context.json', context)
    changed_plan['context_sha256'] = file_hash(work / 'context.json')
    changed_plan['pause_overrides'] = [{'candidate_id': 'P0001', 'action': 'keep', 'reason': '读屏'}]
    write(work / 'plan.json', changed_plan)
    cli('prepare', '--project', source, '--work-dir', work)
    check('legacy pause ID remapped by source', read(work / 'plan.json')['pause_overrides'][0]['candidate_id'] == expected_id)
    check('original plans recoverable', len(list(work.glob('plan-previous-*.json'))) >= 3)
    # Query-only fixtures: overlapping intervals form one item, then pagination.
    saved_context = read(work / 'context.json')
    display = copy.deepcopy(saved_context)
    sample = next(p for p in display['pause_candidates'] if p['id'] == expected_id)
    a = {**copy.deepcopy(sample), 'id': 'display-A', 'status': 'auto',
         'activity': [{'kind': 'screen', 'source_ms': 2300}]}
    b = {**copy.deepcopy(a), 'id': 'display-B'}
    b['pieces'][0]['start_ms'] += 10
    c = {**copy.deepcopy(a), 'id': 'display-C',
         'pieces': [{'parent_index': 2, 'start_ms': 5100, 'end_ms': 5500}],
         'activity': [{'kind': 'screen', 'source_ms': 5300}]}
    display['pause_candidates'] = [a, b, c]
    write(work / 'context.json', display)
    context_hash = file_hash(work / 'context.json')
    listing = cli('pauses', '--work-dir', work, '--status', 'auto', '--limit', 1)
    item = listing['items'][0]
    check('pause list includes actionable context', all(k in item for k in ['ids', 'status', 'source_cuts_ms', 'edited_gap_ms', 'keep_reasons', 'activity', 'nearby_speech']))
    check('overlapping display lists every ID once', set(item['ids']) == {'display-A', 'display-B'} and listing['candidate_total'] == 3)
    check('shared activity is not double counted', item['activity'] == {'screen': 1})
    second = cli('pauses', '--work-dir', work, '--status', 'auto', '--offset', 1, '--limit', 1)
    check('pause paging works on merged items', listing['total'] == 2 and listing['next_offset'] == 1 and second['items'][0]['ids'] == ['display-C'])
    filtered = cli('pauses', '--work-dir', work, '--status', 'auto', '--activity-only')
    check('activity filter returns only relevant pauses', filtered['total'] >= 1 and all(p['activity'] for p in filtered['items']))
    detail = cli('pauses', '--work-dir', work, '--candidate-id', 'display-B')
    check('any member ID locates its display group', detail['total'] == 1 and set(detail['items'][0]['ids']) == {'display-A', 'display-B'})
    check('query does not alter candidate evidence', file_hash(work / 'context.json') == context_hash)
    write(work / 'context.json', saved_context)
    def interval_candidate(name, *ranges):
        return {'id': name, 'pieces': [{'start_ms': x, 'end_ms': y} for x, y in ranges]}
    groups = pause_groups([interval_candidate('A', (0, 100)), interval_candidate('C', (140, 200)),
        interval_candidate('B', (50, 150)), interval_candidate('D', (200, 250)),
        interval_candidate('E', (300, 320), (500, 520)), interval_candidate('F', (400, 420))])
    check('display merges transitive overlap', {p['id'] for p in groups[0]} == {'A', 'B', 'C'})
    check('touching and disjoint source pieces stay separate', len(groups) == 4)
    # An explicit keep also protects overlapping candidates from another detector.
    context = read(work / 'context.json')
    protected = next(p for p in context['pause_candidates'] if p['id'] == expected_id)
    context['pause_candidates'] += [{**copy.deepcopy(protected), 'id': 'overlap-A', 'status': 'auto'},
                                   {**copy.deepcopy(protected), 'id': 'overlap-B', 'status': 'auto'}]
    write(work / 'context.json', context)
    overlap_plan = read(work / 'plan.json')
    overlap_plan['context_sha256'] = file_hash(work / 'context.json')
    overlap_plan['pause_overrides'] = [{'candidate_id': 'overlap-A', 'action': 'keep', 'reason': '读屏'}]
    write(work / 'plan.json', overlap_plan)
    overlap_result = cli('dry-run', '--work-dir', work, '--silence-only')
    check('explicit keep protects overlapping detector candidate', overlap_result['pause_cuts'] == 0)
    # Restore the reviewed keep so the next prepare tests normal reassociation.
    overlap_plan['pause_overrides'][0]['candidate_id'] = expected_id
    write(work / 'plan.json', overlap_plan)
    cli('prepare', '--project', source, '--work-dir', work, '--keep-ms', 200)
    pending = read(work / 'plan.json')
    check('changed pause keeps are preserved as pending', not pending['pause_overrides'] and pending['pending_pause_overrides'][0]['reason'] == '读屏')
    check('unresolved keep cannot become automatic deletion', 'pending_pause_overrides' in cli('dry-run', '--work-dir', work, '--silence-only', fail=True)['reason'])
    # Copying/touching media must not trigger transcription or detector reruns.
    clone = root / 'moved-and-touched.screenstudio'
    shutil.copytree(source, clone)
    before = load_project(source)
    for f in (clone / 'recording').rglob('*'):
        if f.is_file():
            os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns+1000000000))
    after = load_project(clone)
    check('copied media identity ignores filesystem time', before['media_sha256'] != after['media_sha256'] and source_identity(before) == source_identity(after))
    reused = cli('prepare', '--project', clone, '--work-dir', root / 'discovered')
    check('nonstandard scratch cache automatically discovered', reused['cache_reused_from'] == str(work) and reused['new_asr_calls'] == 0 and not reused['vad_computed'] and not reused['screen_scanned'])
    explicit = cli('prepare', '--project', clone, '--work-dir', root / 'explicit', '--cache-from', work)
    check('explicit external cache source works', explicit['new_asr_calls'] == 0 and explicit['cache_reused_from'] == str(work))
    cli('prepare', '--project', clone, '--work-dir', root / 'wrong-language', '--cache-from', work, '--language', 'en', fail=True)
    orphan = root / 'orphan/media'
    orphan.mkdir(parents=True)
    shutil.copy2(work / 'media/microphone.wav', orphan / 'microphone.wav')
    check('unbound cache cannot be trusted as this source', '未绑定来源' in cli('prepare', '--project', clone, '--work-dir', orphan.parent, fail=True)['reason'])
    # Same-size source edits, even with a restored mtime, must not reuse v2 cache.
    microphone = clone / 'recording/different-microphone.wav'
    stat = microphone.stat()
    data = bytearray(microphone.read_bytes())
    data[100] ^= 1
    microphone.write_bytes(data)
    os.utime(microphone, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    check('changed media rejects existing cache', '不匹配' in cli('prepare', '--project', clone, '--work-dir', root / 'discovered', fail=True)['reason'])
    check('cache miss does not silently call ASR', '没有转录缓存' in cli('prepare', '--project', clone, '--work-dir', root / 'uncached', fail=True)['reason'])
    # Both analysis and proxy support native 0.25x playback via chained atempo.
    slow = copy.deepcopy(doc)
    slow['json']['scenes'][0]['slices'] = [{**first, 'sourceEndMs': 1000, 'timeScale': 4}]
    write(source / 'project.json', slow)
    slow_result = cli('prepare', '--project', source, '--work-dir', work)
    check('quarter-speed prepare succeeds with correct duration', slow_result['seconds'] == 4)
    cli('preview', '--project', source, '--output', root / 'quarter-speed.mp4')
    check('quarter-speed proxy decodes and syncs', True)
    check('ordinary playback avoids atempo', tempo_filters(1) == '')
    try:
        run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'no_such_filter', '-f', 'null', '-'])
    except EditError as error:
        check('media errors contain diagnostic detail', 'no_such_filter' in str(error))
    with patch('core.shutil.which', return_value='/fake/bl'), patch('core.subprocess.run', return_value=SimpleNamespace(returncode=1, stderr=b'SENSITIVE_TEST_MARKER', stdout=b'')):
        try:
            run(['bl'])
        except EditError as error:
            check('provider diagnostics stay hidden', 'SENSITIVE_TEST_MARKER' not in str(error))
    # No sidecar report next to editable output, and plan changes bind silence mode.
    clean = read(work / 'plan.json')
    clean.update(decisions=[], pause_overrides=[], pending_pause_overrides=[])
    write(work / 'plan.json', clean)
    cli('dry-run', '--work-dir', work, '--silence-only')
    output = root / 'report-stays-in-work.screenstudio'
    cli('apply', '--work-dir', work, '--output', output)
    check('report only written in work directory', (work / 'applied.json').exists() and not output.with_suffix('.edit-report.json').exists())
    clean['pause_overrides'] = [{'candidate_id': read(work / 'context.json')['pause_candidates'][0]['id'], 'action': 'keep', 'reason': 'new keep'}]
    write(work / 'plan.json', clean)
    check('silence-only frozen plan also rejects later changes', '计划已修改' in cli('apply', '--work-dir', work, '--output', root / 'stale-output.screenstudio', fail=True)['reason'])
    # Reread work persists independently of one prepare response.
    queue = root / 'reread-queue'
    context = copy.deepcopy(saved_context)
    write(queue / 'context.json', context)
    update_plan(queue, None, None, context)
    ids = [u['id'] for u in context['utterances']]
    check('first plan stores utterances to read', read(queue / 'plan.json')['pending_utterance_ids'] == ids)
    queue_plan = read(queue / 'plan.json')
    queue_plan.update(semantic_review_complete=True, pending_utterance_ids=[])
    write(queue / 'plan.json', queue_plan)
    def change_utterance(index):
        nonlocal context
        previous, previous_hash = context, file_hash(queue / 'context.json')
        context = copy.deepcopy(context)
        context['utterances'][index]['text'] += '（本轮变化）'
        write(queue / 'context.json', context)
        return update_plan(queue, previous, previous_hash, context)
    change_utterance(0)
    check('changed utterance is stored for reread', read(queue / 'plan.json')['pending_utterance_ids'] == ids[:1])
    summary = change_utterance(1)
    check('unfinished reread IDs accumulate across rounds', read(queue / 'plan.json')['pending_utterance_ids'] == sorted(ids[:2]) and summary['pending_utterance_ids'] == sorted(ids[:2]))
    update_plan(queue, context, file_hash(queue / 'context.json'), context)
    check('identical prepare retains unfinished reread IDs', read(queue / 'plan.json')['pending_utterance_ids'] == sorted(ids[:2]))
    queue_plan = read(queue / 'plan.json')
    queue_plan['semantic_review_complete'] = True
    write(queue / 'plan.json', queue_plan)
    change_utterance(2)
    check('completed review is not carried into next reread queue', read(queue / 'plan.json')['pending_utterance_ids'] == ids[2:3])
    result = {'passed': baseline['passed']+len(checks), 'original_checks': baseline['passed'], 'new_checks': len(checks), 'checks': checks, 'new_asr_calls': 0}
    write(root / 'test-results.json', result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
