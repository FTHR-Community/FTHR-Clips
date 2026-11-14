"""Signal, timing and recovery contracts for the lossless audio pipeline.

Run tools/build_audio_validation.py first for native disk/recording checks.
"""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import json
import os
import subprocess

import numpy as np
import pytest

from core.audio_processing import SourceProcessing, mixed_output_metadata, parse_source_handler
from core.ffmpeg_playback import InProcessFFmpegMixer, probe_audio_streams
from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
from core.playback_mix_model import (
    PlaybackSource, SourceMixState, build_playback_sources, ffmpeg_mix_filter, source_gain,
)
from ui.clip_viewer import EditorSnapshot, _editor_snapshot_from_draft, _editor_snapshot_to_draft
from ui.clip_viewer import _speed_audio_chain


ROOT = Path(__file__).resolve().parents[1]


def _run(arguments, *, stdin=None):
    environment = dict(os.environ)
    environment['PATH'] = str(Path(get_ffmpeg_exe()).parent) + os.pathsep + environment.get('PATH', '')
    result = subprocess.run([str(value) for value in arguments], input=stdin, env=environment,
                            capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors='replace')[-3000:]
    return result.stdout


def _native_tool(name):
    executable = ROOT / '.audio-validation' / f'{name}.exe'
    if not executable.is_file():
        pytest.skip('Build native checks with python tools/build_audio_validation.py')
    return executable


def _float_wave(path, left=.2, right=.6):
    pcm = np.tile(np.array([left, right], '<f4'), (12000, 1))
    _run([get_ffmpeg_exe(), '-v', 'error', '-f', 'f32le', '-ar', '48000', '-ac', '2',
          '-i', '-', '-c:a', 'pcm_f32le', '-y', path], stdin=pcm.tobytes())


def _preview(path, source, *, limiter=True, rate=1.0, preserve_pitch=True):
    chunks = []
    peaks = []
    with_mixer = InProcessFFmpegMixer(str(path), (source,), limiter_enabled=limiter)
    try:
        with_mixer.set_playback_rate(rate, preserve_pitch)
        for _ in range(100):
            block = with_mixer.pull(1024, [1.0], 1.0)
            if not block:
                break
            peaks.append(with_mixer.output_peak())
            chunks.append(block)
        else:
            pytest.fail('Decoder failed to reach EOF')
        # A second read after EOF and seek must reset both source and master graphs.
        with_mixer.seek(0)
        assert with_mixer.pull(1024, [1.0], 1.0)
    finally:
        with_mixer.close()
    return np.frombuffer(b''.join(chunks), '<f4'), max(peaks)


@pytest.mark.parametrize('limiter', [True, False])
def test_overload_matches_export_and_does_not_change_original(tmp_path, limiter):
    original = tmp_path / 'overload.wav'
    _float_wave(original, 1.2, -1.2)
    checksum = sha256(original.read_bytes()).digest()
    source = PlaybackSource('dry', 'Dry', 'track', 0, 0)
    actual, peak = _preview(original, source, limiter=limiter)
    graph, label = ffmpeg_mix_filter((source,), {}, 100, limiter_enabled=limiter)
    expected = np.frombuffer(_run([
        get_ffmpeg_exe(), '-v', 'error', '-i', original, '-filter_complex', ';'.join(graph),
        '-map', label, '-t', '0.25', '-f', 'f32le', '-']), '<f4')
    assert len(actual) == len(expected)
    assert np.max(np.abs(actual[2048:-2048] - expected[2048:-2048])) < 2e-5
    assert np.max(np.abs(actual)) <= (0.98001 if limiter else 1.0)
    assert peak > 1.19
    assert sha256(original.read_bytes()).digest() == checksum


def test_microphone_processing_is_shared_and_the_dry_stereo_file_survives(tmp_path):
    original = tmp_path / 'dry-mic.wav'
    _float_wave(original)
    checksum = sha256(original.read_bytes()).digest()
    source = PlaybackSource('mic', 'Microphone', 'microphone', 0, 0,
                            input_gain_percent=150, mono=True)
    actual, _ = _preview(original, source)
    assert np.allclose(actual[2048:-2048], .6, atol=2e-6)
    assert sha256(original.read_bytes()).digest() == checksum
    dry = np.frombuffer(_run([get_ffmpeg_exe(), '-v', 'error', '-i', original,
                             '-f', 'f32le', '-']), '<f4').reshape(-1, 2)
    assert np.allclose(dry[:, 0], .2) and np.allclose(dry[:, 1], .6)


def test_volume_change_ramps_without_seeking(tmp_path, monkeypatch):
    original = tmp_path / 'steady.wav'
    _float_wave(original, .2, .2)
    mixer = InProcessFFmpegMixer(str(original), (PlaybackSource('a', 'A', 'track', 0, 0),))
    try:
        mixer.pull(2048, [1.0], 1.0)

        def forbidden_seek(*_args):
            pytest.fail('A gain edit must not seek the decoder')

        monkeypatch.setattr(mixer, 'seek', forbidden_seek)
        block = np.frombuffer(mixer.pull(2048, [.5], 1.0), '<f4').reshape(-1, 2)[:, 0]
        assert np.max(np.abs(np.diff(block))) < .001
        assert block[-1] == pytest.approx(.1, abs=1e-6)
    finally:
        mixer.close()


def test_zero_capture_gain_is_an_initial_mute_that_can_be_recovered(tmp_path):
    original = tmp_path / 'muted-dry-mic.wav'
    _float_wave(original)
    source = PlaybackSource('mic', 'Microphone', 'microphone', 0, 0, input_gain_percent=0)
    assert source.default_muted and source_gain(source, {}) == 0
    assert source_gain(source, {'mic': SourceMixState(muted=False)}) == 1
    actual, _ = _preview(original, source)
    assert np.allclose(actual.reshape(-1, 2)[1024:-1024], [.2, .6], atol=2e-6)
    for states, level in (({}, [0, 0]), ({'mic': SourceMixState(muted=False)}, [.2, .6])):
        graph, label = ffmpeg_mix_filter((source,), states, 100)
        rendered = np.frombuffer(_run([
            get_ffmpeg_exe(), '-v', 'error', '-i', original, '-filter_complex', ';'.join(graph),
            '-map', label, '-t', '0.25', '-f', 'f32le', '-']), '<f4').reshape(-1, 2)
        assert np.allclose(rendered[1024:-1024], level, atol=2e-6)


@pytest.mark.parametrize('rate', [.25, .5, 1.5, 2.0])
@pytest.mark.parametrize('preserve_pitch', [True, False])
def test_shared_preview_and_export_match_at_different_playback_rates(tmp_path, rate, preserve_pitch):
    original = tmp_path / 'speed.wav'
    samples = np.arange(12000) / 48000
    pcm = np.column_stack((np.sin(2 * np.pi * 440 * samples),
                           np.sin(2 * np.pi * 660 * samples))).astype('<f4') * .12
    _run([get_ffmpeg_exe(), '-v', 'error', '-f', 'f32le', '-ar', '48000', '-ac', '2',
          '-i', '-', '-c:a', 'pcm_f32le', '-y', original], stdin=pcm.tobytes())
    source = PlaybackSource('mic', 'Microphone', 'microphone', 0, 0,
                            input_gain_percent=150, mono=True)
    actual, _ = _preview(original, source, rate=rate, preserve_pitch=preserve_pitch)
    graph, label = ffmpeg_mix_filter((source,), {}, 100)
    graph.append(f'{label}atrim=duration=0.25,{_speed_audio_chain(rate, preserve_pitch)}[speed]')
    expected = np.frombuffer(_run([
        get_ffmpeg_exe(), '-v', 'error', '-i', original, '-filter_complex', ';'.join(graph),
        '-map', '[speed]', '-f', 'f32le', '-']), '<f4')
    assert len(actual) == len(expected)
    assert np.max(np.abs(actual - expected)) < .002


@pytest.fixture
def recorded_clip(tmp_path):
    executable = _native_tool('audio_recording_smoke')
    video = tmp_path / 'video.mp4'
    _run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=30:d=1',
          '-c:v', 'libopenh264', '-g', '15', '-y', video])
    target = tmp_path / 'recording.mp4'
    _run([executable, video, target])
    return video, target


def test_native_lossless_retention_silence_budget_and_process_groups():
    assert b'passed' in _run([_native_tool('audio_quality_smoke')])


def test_native_legacy_recordings_and_transactional_manifest_regression():
    assert b'checks passed' in _run([_native_tool('audio_regression')])


def test_manual_recording_keeps_late_timing_and_self_contained_source_metadata(recorded_clip):
    _, target = recorded_clip
    probe = json.loads(_run([get_ffprobe_exe(), '-v', 'error', '-show_streams', '-of', 'json', target]))
    audio = [stream for stream in probe['streams'] if stream['codec_type'] == 'audio']
    assert len(audio) == 3
    assert [float(stream['start_time']) for stream in audio] == pytest.approx([0, .3, .5], abs=1/48000)
    assert {stream['codec_name'] for stream in audio} == {'pcm_f32le'}
    sources = build_playback_sources(None, probe_audio_streams(str(target)))
    mic = next(source for source in sources if source.source_type == 'microphone')
    assert mic.input_gain_percent == 175 and mic.mono
    assert [source_gain(source, {}, sources) for source in sources if source.mix_role == 'backup'] == [0]
    assert not list(target.parent.glob('*.fthr-audio.json'))
    # The saved microphone retains its stereo, unprocessed samples. Packet
    # timestamps start late; decoding that stream alone does not pad the gap.
    dry = np.frombuffer(_run([get_ffmpeg_exe(), '-v', 'error', '-i', target,
                             '-map', '0:a:1', '-f', 'f32le', '-']), '<f4').reshape(-1, 2)
    expected = (np.sin(np.arange(14400, 48000) * .05) * .125).astype('<f4')
    assert dry.shape == (33600, 2)
    assert np.array_equal(dry[:, 1], -dry[:, 0])
    assert np.max(np.abs(dry[:, 0] - expected)) < 2e-8


def test_crashed_recording_exposes_only_committed_sources(recorded_clip, tmp_path):
    video, _ = recorded_clip
    target = tmp_path / 'crashed.mp4'
    _run([_native_tool('audio_recording_smoke'), video, target, 'crash'])
    streams = probe_audio_streams(str(target))
    assert len(streams) == 2
    assert [stream.audio_index for stream in streams] == [0, 1]
    assert [stream.source_type for stream in streams] == ['system', 'microphone']
    sources = build_playback_sources(None, streams)
    system = next(source for source in sources if source.source_type == 'system')
    assert source_gain(system, {}, sources) == 1
    assert system.mix_role != 'backup'


def test_reserved_slot_filter_preserves_actual_audio_ordinals(monkeypatch):
    import core.ffmpeg_playback as playback
    handler = 'FTHR2|33333333-3333-4333-8333-333333333333|application|100|0|Game'
    raw = {'format': {'tags': {'fthr_capture_audio': 'independent-float-v1'}}, 'streams': [
        {'codec_type': 'audio', 'index': 1, 'tags': {'handler_name': 'FTHR reserved audio ' + ' ' * 320}},
        {'codec_type': 'audio', 'index': 7, 'codec_name': 'pcm_f32le', 'tags': {'handler_name': handler}},
    ]}
    monkeypatch.setattr(playback, 'get_ffprobe_exe', lambda: 'ffprobe')
    from types import SimpleNamespace
    monkeypatch.setattr(playback.subprocess, 'run', lambda *_args, **_kwargs:
                        SimpleNamespace(returncode=0, stdout=json.dumps(raw)))
    streams = probe_audio_streams('crashed.mp4')
    assert len(streams) == 1
    assert streams[0].container_index == 7 and streams[0].audio_index == 1


def test_late_and_ended_recording_sources_do_not_force_unbounded_read_ahead(tmp_path):
    video, target = tmp_path / 'long-video.mp4', tmp_path / 'long-recording.mp4'
    _run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i', 'color=s=64x48:r=30:d=12',
          '-c:v', 'libopenh264', '-g', '15', '-y', video])
    _run([_native_tool('audio_recording_smoke'), video, target, 'long'])
    sources = build_playback_sources(None, probe_audio_streams(str(target)))
    mixer = InProcessFFmpegMixer(str(target), sources)
    try:
        gains = [source_gain(source, {}, sources) for source in sources]
        assert mixer.pull(1024, gains, 1.0)
        assert 0 < mixer.buffered_frames() < 48000
        mixer.seek(5000)  # The microphone has ended; the application has not started.
        assert mixer.pull(1024, gains, 1.0)
        assert 0 < mixer.buffered_frames() < 48000
        mixer.seek(10000)
        assert mixer.pull(1024, gains, 1.0)
        assert 0 < mixer.buffered_frames() < 48000
    finally:
        mixer.close()


def test_mixed_output_clears_processing_identity(tmp_path):
    original = tmp_path / 'mic.mp4'
    pcm = np.tile(np.array([.2, .6], '<f4'), (4800, 1))
    _run([get_ffmpeg_exe(), '-v', 'error', '-f', 'f32le', '-ar', '48000', '-ac', '2', '-i', '-',
          '-c:a', 'pcm_f32le', '-metadata:s:a:0',
          'handler_name=FTHR2|22222222-2222-4222-8222-222222222222|microphone|175|1|Microphone',
          '-y', original], stdin=pcm.tobytes())
    target = tmp_path / 'mixed.m4a'
    _run([get_ffmpeg_exe(), '-v', 'error', '-i', original, '-c:a', 'aac',
          *mixed_output_metadata(), '-y', target])
    streams = probe_audio_streams(str(target))
    assert streams[0].audio_mode == 'combined'
    assert streams[0].source_id is None
    assert streams[0].gain_percent == 100 and not streams[0].mono


def test_limiter_and_import_processing_roundtrip_in_editor_history(tmp_path):
    source = PlaybackSource('import:mic', 'Dry mic', 'track', 0, 0,
                            media_path=str(tmp_path / 'dry-mic.wav'), input_gain_percent=175, mono=True)
    initial = replace(EditorSnapshot(0, 1, ((0, 1, False),), 0, None, 1, {}),
                      imported_sources=(source,), limiter_enabled=False)
    restored = _editor_snapshot_from_draft(_editor_snapshot_to_draft(initial))
    assert restored is not None and not restored.limiter_enabled
    assert restored.imported_sources[0].input_gain_percent == 175
    assert restored.imported_sources[0].mono
    invalid = _editor_snapshot_to_draft(initial)
    invalid['limiter_enabled'] = 'false'
    assert _editor_snapshot_from_draft(invalid) is None


@pytest.mark.parametrize('handler', [
    'FTHR2|bad|microphone|100|0|Microphone',
    'FTHR2|22222222-2222-4222-8222-222222222222|microphone|999|0|Microphone',
    'FTHR2|22222222-2222-4222-8222-222222222222|microphone|100|1|Mic\nname',
])
def test_invalid_capture_processing_metadata_is_rejected(handler):
    assert parse_source_handler(handler) is None


@pytest.mark.parametrize('gain,mono', [(201, False), (100, 'true'), (-1, False)])
def test_invalid_processing_cannot_reach_ffmpeg(gain, mono):
    with pytest.raises(ValueError):
        SourceProcessing(gain, mono)
