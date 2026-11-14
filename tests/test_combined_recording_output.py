from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pytest

from core import combined_audio_policy
from core.audio_manifest import AudioSourceManifestEntry, build_manifest, write_manifest_atomic
from core.capture_highlights import read_capture_highlights, write_capture_highlights
from core.combined_audio_policy import combine_recording_audio
from core.ffmpeg_playback import discover_playback_sources
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.highlight_markers import HighlightMarker


def _run(command):
    return subprocess.run(
        command, capture_output=True, check=True, timeout=30,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout


@pytest.fixture
def tools():
    try:
        return get_ffmpeg_exe(), get_ffprobe_exe()
    except FFmpegUnavailable as error:
        pytest.skip(str(error))


@pytest.mark.parametrize('route', ['manual', 'replay'])
def test_combined_recording_embeds_both_sources(tmp_path, tools, route):
    ffmpeg, ffprobe = tools
    recording = tmp_path / 'recording.mp4'
    system_id, microphone_id = str(uuid4()), str(uuid4())
    system_handler = f'FTHR3|{system_id}|system|100|0|0:96000|Default Mix'
    mic_handler = f'FTHR3|{microphone_id}|microphone|50|0|0:96000|Microphone'
    _run([
        ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=160x90:r=30:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:d=2',
        '-f', 'lavfi', '-i', 'sine=frequency=880:sample_rate=48000:d=2',
        '-map', '0:v', '-map', '1:a', '-map', '2:a', '-c:v', 'mpeg4',
        '-c:a', 'pcm_f32le', '-ac', '2',
        '-movflags', '+frag_keyframe+empty_moov+use_metadata_tags',
        '-metadata', 'fthr_capture_audio=independent-float-v1',
        '-metadata:s:a:0', f'handler_name={system_handler}',
        '-metadata:s:a:1', f'handler_name={mic_handler}', str(recording),
    ])
    if route == 'manual':
        marker = HighlightMarker(str(uuid4()), 5_000_000, 'Highlight')
        write_capture_highlights(recording, (marker,))
        combine_recording_audio(recording)
        assert read_capture_highlights(recording) == (marker,)
    else:
        from main import MainWindow

        manifest = build_manifest(media_path=recording, transaction_id=str(uuid4()), sources=[
            AudioSourceManifestEntry(system_id, 1, 'system', 'Default Mix', 'default-mix',
                                     'system-audio', 0, 20_000_000, 48000, 2, 'flt'),
            AudioSourceManifestEntry(microphone_id, 2, 'microphone', 'Microphone', 'microphone',
                                     'microphone', 0, 20_000_000, 48000, 2, 'flt'),
        ])
        manifest['mixing_mode'] = 'independent'
        write_manifest_atomic(manifest, recording)
        host = SimpleNamespace(
            _record_finalization_warning=lambda _path, detail: pytest.fail(detail),
            _finalize_clip_worker=lambda *_args: pytest.fail('Replay bypassed combined audio'),
            _replace_finalization_output=os.replace,
            _apply_crop=lambda *_args: None,
            _apply_image_overlay=lambda *_args: None,
            _apply_keyboard_overlay=lambda *_args: None,
            _apply_camera_overlay=lambda *_args: None,
        )
        MainWindow._combine_native_audio_worker(host, str(recording), 2, 0.0)

    probe = json.loads(_run([
        ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json',
        str(recording),
    ]))
    assert [stream['codec_name'] for stream in probe['streams']] == ['mpeg4', 'aac']
    assert probe['format']['tags']['comment'] == 'fthr-audio-mode=combined'
    assert 'fthr_capture_audio' not in probe['format']['tags']
    assert float(probe['streams'][0]['duration']) == pytest.approx(2.0, abs=0.04)
    assert list(tmp_path.iterdir()) == [recording]
    sources = discover_playback_sources(str(recording))
    assert len(sources) == 1
    assert sources[0].mix_role == 'direct'
    assert not sources[0].editable

    samples = np.frombuffer(_run([
        ffmpeg, '-v', 'error', '-i', str(recording), '-vn', '-ac', '1',
        '-ar', '48000', '-f', 'f32le', 'pipe:1',
    ]), dtype='<f4')[24_000:72_000]
    spectrum = np.abs(np.fft.rfft(samples))
    assert spectrum[440] > 1000
    assert spectrum[880] / spectrum[440] == pytest.approx(0.5, abs=0.05)


def test_silent_recording_is_left_byte_for_byte(tmp_path, tools):
    ffmpeg, _ = tools
    recording = tmp_path / 'silent.mp4'
    _run([
        ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=160x90:r=30:d=0.1',
        '-c:v', 'mpeg4', str(recording),
    ])
    original = recording.read_bytes()

    combine_recording_audio(recording)

    assert recording.read_bytes() == original
    assert list(tmp_path.iterdir()) == [recording]


def test_combined_recording_retires_manifest_only_when_media_commits(
        tmp_path, tools, monkeypatch):
    ffmpeg, _ = tools
    recording = tmp_path / 'recording.mp4'
    _run([
        ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=s=160x90:r=30:d=1',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:d=1',
        '-c:v', 'mpeg4', '-c:a', 'aac', str(recording),
    ])
    source = AudioSourceManifestEntry(
        str(uuid4()), 1, 'system', 'Default Mix', 'default-mix',
        'system-audio', 0, 10_000_000, 48000, 1, 'fltp',
    )
    manifest = build_manifest(
        media_path=recording, transaction_id=str(uuid4()), sources=[source])
    sidecar = write_manifest_atomic(manifest, recording)
    media_bytes = recording.read_bytes()
    sidecar_bytes = sidecar.read_bytes()

    def locked_destination(*_args):
        raise PermissionError('Recording destination is locked')

    with monkeypatch.context() as patch:
        patch.setattr(combined_audio_policy, 'commit_staged_output', locked_destination)
        with pytest.raises(RuntimeError, match='Recording destination is locked'):
            combine_recording_audio(recording)
    assert recording.read_bytes() == media_bytes
    assert sidecar.read_bytes() == sidecar_bytes
    assert set(tmp_path.iterdir()) == {recording, sidecar}

    combine_recording_audio(recording)

    assert list(tmp_path.iterdir()) == [recording]
