import hashlib
import json
from fractions import Fraction
import os
import subprocess
import threading
from uuid import uuid4

import pytest

from core import clip_overlap as overlap
from core import clip_overlap_cache
from core.export_lifecycle import ExportState


GENERATION = '35b611f2-a164-4978-93e6-835968bfa884'


def timeline(start=0, end=30, generation=GENERATION, num=1, den=1):
    return overlap.CaptureTimeline(generation, start, end, num, den)


def clip(path, start=0, end=30, generation=GENERATION):
    return overlap.ClipInfo(str(path), (), timeline(start, end, generation), 160, 120, Fraction(10))


def test_timeline_strict_validation():
    valid = timeline().to_json()
    assert overlap.parse_timeline(valid) == timeline()
    for key, value in [('version', True), ('version', 2), ('generation_id', 'no'),
                       ('start_tick', -1), ('end_tick', 0), ('end_tick', 2**63),
                       ('time_base_num', 0), ('time_base_den', 0), ('start_tick', 0.5)]:
        data = json.loads(valid)
        data[key] = value
        assert overlap.parse_timeline(json.dumps(data)) is None
    assert overlap.parse_timeline('{}') is None
    assert overlap.parse_timeline('x' * 3000) is None
    assert overlap.parse_timeline(json.dumps({**json.loads(valid), 'extra': 1})) is None


def test_union_deduplicates_overlap_and_containment():
    a, b = clip('a', 0, 30), clip('b', 20, 50)
    plan = overlap.plan_merge([b, a, clip('contained', 5, 10)])
    assert plan.duration == 50
    assert [(s.clip.path, s.offset, s.duration) for s in plan.segments] == [('a', 0, 30), ('b', 10, 20)]
    assert overlap.plan_merge([a, clip('copy', 0, 30)]).duration == 30


@pytest.mark.parametrize('other', [clip('b', 30, 60), clip('b', 40, 60), clip('b', 10, 50, str(uuid4()))])
def test_no_adjacent_gap_or_cross_generation_merge(other):
    assert overlap.find_overlaps([clip('a'), other]) == {}
    with pytest.raises(ValueError):
        overlap.plan_merge([clip('a'), other])


def test_rational_ticks_and_exclusive_overlap():
    a = overlap.ClipInfo('a', (), timeline(100, 400, num=1001, den=30000), 160, 120, Fraction(30000, 1001))
    b = overlap.ClipInfo('b', (), timeline(300, 500, num=1001, den=30000), 160, 120, a.fps)
    plan = overlap.plan_merge([a, b])
    assert plan.timeline.end_tick == 500
    assert plan.duration == Fraction(400 * 1001, 30000)


def test_discovery_is_bounded_even_for_dense_overlaps():
    infos = [clip(f'clip{i}', i, 10001) for i in range(10000)]
    matches = overlap.find_overlaps(infos)
    assert len(matches) == len(infos)
    assert all(0 < len(paths) <= 16 for paths in matches.values())


def test_replaced_source_refused(tmp_path):
    path = tmp_path / 'source.mp4'
    path.write_bytes(b'old')
    info = overlap.ClipInfo(str(path), overlap.file_identity(path), timeline(), 160, 120, Fraction(10))
    path.write_bytes(b'changed')
    with pytest.raises(OSError, match='changed'):
        overlap._check_sources(overlap.MergePlan((info,), (), timeline()))


def test_probe_rejects_stale_duration_and_legacy():
    document = {'streams': [{'codec_type': 'video', 'width': 160, 'height': 120,
                            'avg_frame_rate': '10/1', 'duration': '30'}],
                'format': {'tags': {overlap.TIMELINE_TAG: timeline().to_json()}}}
    assert overlap._clip_from_probe('a', (), document) is not None
    document['streams'][0]['start_time'] = '2'
    assert overlap._clip_from_probe('a', (), document) is None
    document['streams'][0]['start_time'] = '0'
    document['streams'][0]['duration'] = '15'
    assert overlap._clip_from_probe('a', (), document) is None
    document['format']['tags'] = {}
    assert overlap._clip_from_probe('a', (), document) is None


def _probe_document(provenance):
    return {
        'streams': [{'codec_type': 'video', 'width': 160, 'height': 120,
                     'avg_frame_rate': '10/1', 'duration': '30'},
                    {'codec_type': 'audio', 'index': 1, 'tags': {'title': 'System'}}],
        'format': {'tags': {overlap.TIMELINE_TAG: timeline().to_json()} if provenance else {}},
    }


@pytest.mark.parametrize('provenance', [True, False])
def test_inspection_persists_conclusive_result_and_refresh_reprobes(tmp_path, monkeypatch, provenance):
    path = tmp_path / 'source.mp4'
    path.touch()
    calls = []

    def probe(path, _cancel):
        calls.append(path)
        return _probe_document(provenance)

    monkeypatch.setattr(overlap, '_probe', probe)
    cache_path = tmp_path / 'overlap.sqlite3'
    with clip_overlap_cache.open_overlap_probe_cache(cache_path) as cache:
        cold = overlap.inspect_clip(path, threading.Event(), cache=cache)
    assert cold.complete and cold.probed and (cold.info is not None) == provenance
    with clip_overlap_cache.open_overlap_probe_cache(cache_path) as cache:
        warm = overlap.inspect_clip(path, threading.Event(), cache=cache)
        assert warm.complete and not warm.probed and warm.info == cold.info
    assert calls == [str(path)]
    assert overlap.probe_clip(path, refresh=True) == cold.info
    assert calls == [str(path), str(path)]


def test_failed_and_cancelled_inspections_do_not_store_negative(tmp_path, monkeypatch):
    path = tmp_path / 'source.mp4'
    path.touch()
    calls = []

    def probe(path, cancel):
        calls.append(path)
        if len(calls) == 1:
            raise ValueError('timeout')
        if len(calls) == 2:
            cancel.set()
        return _probe_document(True)

    monkeypatch.setattr(overlap, '_probe', probe)
    with clip_overlap_cache.open_overlap_probe_cache(tmp_path / 'overlap.sqlite3') as cache:
        for _ in range(2):
            assert not overlap.inspect_clip(path, threading.Event(), cache=cache).complete
            assert cache.get(overlap.file_identity(path)) is clip_overlap_cache.CACHE_MISS
        assert overlap.inspect_clip(path, threading.Event(), cache=cache).info is not None
    assert len(calls) == 3


def test_cached_result_excludes_replaced_same_size_same_mtime_file(tmp_path, monkeypatch):
    path = tmp_path / 'source.mp4'
    path.write_bytes(b'old')
    calls = []

    def probe(path, _cancel):
        calls.append(path)
        return _probe_document(len(calls) > 1)

    monkeypatch.setattr(overlap, '_probe', probe)
    old = path.stat()
    with clip_overlap_cache.open_overlap_probe_cache(tmp_path / 'overlap.sqlite3') as cache:
        assert overlap.inspect_clip(path, threading.Event(), cache=cache).complete
        replacement = tmp_path / 'replacement.mp4'
        replacement.write_bytes(b'new')
        os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
        replacement.replace(path)
        new = path.stat()
        assert (old.st_size, old.st_mtime_ns) == (new.st_size, new.st_mtime_ns)
        outdated = (old.st_size, old.st_mtime_ns, old.st_ino, old.st_dev)
        assert not overlap.inspect_clip(path, threading.Event(), cache=cache, expected=outdated).complete
        assert overlap.inspect_clip(path, threading.Event(), cache=cache).info is not None
    assert len(calls) == 2


def test_probe_cache_entry_cap_and_unreadable_database(tmp_path, monkeypatch):
    monkeypatch.setattr(clip_overlap_cache, '_MAX_ENTRIES', 2)
    cache_path = tmp_path / 'overlap.sqlite3'
    with clip_overlap_cache.open_overlap_probe_cache(cache_path) as cache:
        cache.put(('first',), None)
        cache.put(('second',), b'positive')
        cache.put(('third',), None)
    with clip_overlap_cache.open_overlap_probe_cache(cache_path) as cache:
        assert cache.get(('first',)) is clip_overlap_cache.CACHE_MISS
        assert cache.get(('second',)) == b'positive'
        assert cache.get(('third',)) is None
    cache_path.write_bytes(b'invalid sqlite')
    with clip_overlap_cache.open_overlap_probe_cache(cache_path) as cache:
        cache.put(('fourth',), None)
        assert cache.get(('fourth',)) is clip_overlap_cache.CACHE_MISS


def test_audio_handler_identity_ignores_old_sample_bounds():
    document = {'streams': [{'codec_type': 'video', 'width': 160, 'height': 120,
                            'avg_frame_rate': '10/1', 'duration': '30'},
                           {'codec_type': 'audio', 'index': 1, 'tags': {
                               'handler_name': f'FTHR3|{GENERATION}|microphone|75|0|123:456|Microphone'}}],
                'format': {'tags': {overlap.TIMELINE_TAG: timeline().to_json()}}}
    info = overlap._clip_from_probe('a', (), document)
    assert info.audio[0].handler == f'FTHR2|{GENERATION}|microphone|75|0|Microphone'
    assert '123:456' not in info.audio[0].key


def test_edited_export_drops_original_timeline():
    from core.export_encoding import encode_command
    command = encode_command(['ffmpeg', '-i', 'a.mp4', '-map_metadata', '0', 'out.mp4'], [], 'quality')
    assert command[-3:-1] == ['-metadata', 'fthr_capture_timeline=']


def test_queue_owns_all_sources_and_releases_on_cancel(tmp_path):
    from core import export_queue as queue
    from core.export_lifecycle import ExportResult
    started = threading.Event()
    def work(task):
        started.set()
        assert task.cancel.wait(5)
        return ExportResult(ExportState.CANCELLED, 'cancelled')
    a, b, out = (tmp_path / name for name in ('a', 'b', 'out'))
    task = queue.submit_work([a, b], out, 'merge', work)
    try:
        assert started.wait(5)
        assert queue.acquire_local_mutations([a, b]) is None
    finally:
        task.cancel.set()
        task.thread.join(5)
    assert task.done and task.result.state == ExportState.CANCELLED
    reservation = queue.acquire_local_mutations([a, b])
    assert reservation is not None
    queue.release_local_mutations(reservation)


def _run(command):
    result = subprocess.run(command, capture_output=True, timeout=40)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    return result.stdout


@pytest.fixture
def overlapping_media(tmp_path):
    ffmpeg = overlap.get_ffmpeg_exe()
    source = tmp_path / 'source.mp4'
    # Three distinct two-second scenes and a microphone starting two seconds
    # late expose both duplicate-footage and audio-shifting merge errors.
    _run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=red:s=160x120:r=10:d=2',
          '-f', 'lavfi', '-i', 'color=blue:s=160x120:r=10:d=2',
          '-f', 'lavfi', '-i', 'color=green:s=160x120:r=10:d=2',
          '-f', 'lavfi', '-i', 'sine=frequency=440:duration=6:sample_rate=48000',
          '-itsoffset', '2', '-f', 'lavfi', '-i', 'sine=frequency=880:duration=4:sample_rate=48000',
          '-filter_complex', '[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]',
          '-map', '[v]', '-map', '3:a', '-map', '4:a', '-c:v', 'mpeg4', '-q:v', '2',
          '-c:a', 'aac', '-metadata:s:a:0', 'title=System', '-metadata:s:a:1', 'title=Microphone',
          '-movflags', 'use_metadata_tags', str(source)])
    paths = []
    for name, start in [('a', 0), ('b', 2)]:
        path = tmp_path / f'{name}.mp4'
        _run([ffmpeg, '-v', 'error', '-y', '-ss', str(start), '-i', str(source), '-t', '4',
              '-map', '0', '-c', 'copy', '-avoid_negative_ts', 'disabled',
              '-metadata', f'{overlap.TIMELINE_TAG}={timeline(start * 10, (start + 4) * 10, den=10).to_json()}',
              '-movflags', 'use_metadata_tags', str(path)])
        paths.append(path)
    return paths


def test_real_merge_duration_frames_multitrack_and_originals(overlapping_media, tmp_path, monkeypatch):
    import numpy as np
    from core import export_encoding
    monkeypatch.setattr(export_encoding, 'choose_video_args', lambda *a, **kw: ['-c:v', 'mpeg4', '-q:v', '2'])
    paths = overlapping_media
    hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    infos = [overlap.probe_clip(p) for p in paths]
    assert all(infos)
    assert len(overlap.find_overlaps(infos)) == 2
    output = tmp_path / 'merged.mp4'
    result = overlap.execute_merge(paths, output, threading.Event())
    assert result.succeeded, result.detail + result.stderr_tail
    merged = overlap.probe_clip(output)
    assert merged is not None and merged.timeline.duration == 6 and len(merged.audio) == 2
    ffmpeg = overlap.get_ffmpeg_exe()
    for time, channel in [(1, 0), (3, 2), (5, 1)]:
        raw = _run([ffmpeg, '-v', 'error', '-ss', str(time), '-i', str(output),
                    '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'])
        color = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).mean(axis=0)
        assert color.argmax() == channel
    rms = []
    for time in (0.5, 2.5):
        raw = _run([ffmpeg, '-v', 'error', '-ss', str(time), '-i', str(output),
                    '-t', '0.3', '-map', '0:a:1', '-f', 'f32le', '-'])
        rms.append(float(np.sqrt(np.mean(np.frombuffer(raw, dtype='<f4') ** 2))))
    assert rms[0] < .001 and rms[1] > .02, rms
    assert [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths] == hashes
    assert not list(tmp_path.glob('.*partial*'))


def test_cancelled_merge_does_not_create_output(overlapping_media, tmp_path):
    cancel = threading.Event()
    cancel.set()
    output = tmp_path / 'cancelled.mp4'
    assert overlap.execute_merge(overlapping_media, output, cancel).state == ExportState.CANCELLED
    assert not output.exists()


def test_real_merge_never_replaces_existing_output(overlapping_media, tmp_path, monkeypatch):
    from core import export_encoding
    monkeypatch.setattr(export_encoding, 'choose_video_args', lambda *a, **kw: ['-c:v', 'mpeg4', '-q:v', '2'])
    output = tmp_path / 'existing.mp4'
    output.write_bytes(b'existing user file')
    result = overlap.execute_merge(overlapping_media, output, threading.Event())
    assert not result.succeeded
    assert output.read_bytes() == b'existing user file'
    assert not list(tmp_path.glob('.*partial*'))


def test_real_video_only_and_contained_merge(overlapping_media, tmp_path, monkeypatch):
    from core import export_encoding
    monkeypatch.setattr(export_encoding, 'choose_video_args', lambda *a, **kw: ['-c:v', 'mpeg4', '-q:v', '2'])
    paths = []
    for i in range(2):
        path = tmp_path / f'video_only{i}.mp4'
        _run([overlap.get_ffmpeg_exe(), '-v', 'error', '-y', '-i', str(overlapping_media[0]),
              '-map', '0:v', '-c', 'copy', '-map_metadata', '0', '-movflags', 'use_metadata_tags', str(path)])
        paths.append(path)
    output = tmp_path / 'video_only_merged.mp4'
    result = overlap.execute_merge(paths, output, threading.Event())
    assert result.succeeded, result.detail + result.stderr_tail
    info = overlap.probe_clip(output)
    assert info.timeline.duration == 4 and not info.audio


def test_source_change_during_merge_prevents_publication(overlapping_media, tmp_path, monkeypatch):
    from core import export_encoding
    monkeypatch.setattr(export_encoding, 'choose_video_args', lambda *a, **kw: ['-c:v', 'mpeg4', '-q:v', '2'])
    check = overlap._check_sources
    calls = 0
    def change_source(plan):
        nonlocal calls
        calls += 1
        if calls == 2:
            with open(overlapping_media[0], 'ab') as source:
                source.write(b'changed')
        check(plan)
    monkeypatch.setattr(overlap, '_check_sources', change_source)
    output = tmp_path / 'stale.mp4'
    result = overlap.execute_merge(overlapping_media, output, threading.Event())
    assert not result.succeeded and 'changed' in result.detail
    assert not output.exists()
    assert not list(tmp_path.glob('.*partial*'))
