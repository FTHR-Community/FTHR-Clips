import json
import subprocess
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PySide6.QtCore import Qt
from PySide6.QtMultimedia import QAudioFormat
from PySide6.QtWidgets import QWidget

from core import ffmpeg_playback, hdr_color, mp4_timing, playback_proxy
from core.audio_manifest import media_sha256
from core.ffmpeg_tools import get_ffmpeg_exe


@pytest.fixture
def compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr(playback_proxy, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(playback_proxy, 'CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(playback_proxy, 'get_ffprobe_exe', lambda: 'ffprobe')
    return playback_proxy.prepare_compatibility_path


def _probe_document(transfer='bt709', stderr=b''):
    return 0, json.dumps({'streams': [
        {'codec_type': 'video', 'color_transfer': transfer, 'start_time': '0'},
        {'codec_type': 'audio', 'codec_tag_string': 'mp4a'},
    ]}).encode(), stderr


def test_validated_original_compatibility_is_reused_until_media_changes(
        tmp_path, monkeypatch, compatibility):
    clip = tmp_path / 'clip.mp4'
    subprocess.run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=s=16x16:d=0.1', '-c:v', 'mpeg4', '-use_editlist', '0', str(clip)],
                   check=True, capture_output=True, timeout=10)
    probes = []
    hdr_probes = []

    def probe(command, *_args, **_kwargs):
        probes.append(command[-1])
        return _probe_document(transfer='unknown')

    def probe_hdr(command, *_args, **_kwargs):
        hdr_probes.append(command[-1])
        return 0, b'{"streams": [{"color_transfer": "unknown"}]}', b''

    monkeypatch.setattr(playback_proxy, 'run_media_process', probe)
    monkeypatch.setattr(hdr_color, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(hdr_color, 'run_media_process', probe_hdr)
    for _ in range(2):
        assert compatibility(str(clip), threading.Event()) == str(clip)
    assert probes == [str(clip)]
    assert hdr_probes == [str(clip)]
    with clip.open('ab') as media:
        media.write(b'\0\0\0\x08free')
    assert compatibility(str(clip), threading.Event()) == str(clip)
    assert probes == [str(clip)] * 2
    assert hdr_probes == [str(clip)] * 2


@pytest.mark.parametrize('first_outcome', [
    'failed probe', 'incomplete probe', 'failed HDR inspection', 'unsupported normalization',
    'cancelled normalization',
])
def test_uncertain_compatibility_retries_until_validated(
        tmp_path, monkeypatch, compatibility, first_outcome):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    probes = []
    normalization = mp4_timing.Mp4NormalizationResult

    def probe(*_args, **_kwargs):
        probes.append(True)
        if len(probes) == 1:
            if first_outcome == 'failed probe':
                return None
            if first_outcome == 'incomplete probe':
                return _probe_document(stderr=b'read failed')
            if first_outcome == 'failed HDR inspection':
                return _probe_document(transfer='unknown')
        return _probe_document()

    def normalize(_source, _destination, cancelled):
        if len(probes) == 1:
            if first_outcome == 'unsupported normalization':
                return normalization.UNAVAILABLE
            if first_outcome == 'cancelled normalization':
                cancelled.set()
        return normalization.UNCHANGED

    monkeypatch.setattr(playback_proxy, 'run_media_process', probe)
    monkeypatch.setattr(hdr_color, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(hdr_color, 'run_media_process', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_result', normalize)
    for _ in range(3):
        assert compatibility(str(clip), threading.Event()) == str(clip)
    assert len(probes) == 2


def test_compatibility_cache_evicts_least_recently_used_media(
        tmp_path, monkeypatch, compatibility):
    monkeypatch.setattr(playback_proxy, '_ORIGINAL_COMPATIBILITY',
                        playback_proxy._ORIGINAL_COMPATIBILITY.copy())
    playback_proxy._ORIGINAL_COMPATIBILITY.clear()
    monkeypatch.setattr(playback_proxy, '_MAX_ORIGINAL_COMPATIBILITY_ENTRIES', 2)
    monkeypatch.setattr(mp4_timing, 'normalize_mp4_presentation_result',
                        lambda *_: mp4_timing.Mp4NormalizationResult.UNCHANGED)
    probes = []

    def probe(command, *_args, **_kwargs):
        probes.append(command[-1])
        return _probe_document()

    monkeypatch.setattr(playback_proxy, 'run_media_process', probe)
    paths = [tmp_path / f'{i}.mp4' for i in range(3)]
    for path in paths:
        path.write_bytes(b'clip')
    first, second, third = map(str, paths)
    for clip in (first, second, first, third, first, second):
        assert compatibility(clip, threading.Event()) == clip
    assert probes == [first, second, third, second]
    assert len(playback_proxy._ORIGINAL_COMPATIBILITY) == 2


def test_audio_discovery_retries_after_existing_sidecar_read_falls_back(
        tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    ffmpeg_playback.manifest_path_for(clip).write_text(json.dumps({
        'manifest_version': 1,
        'transaction_id': str(uuid4()),
        'media_file': clip.name,
        'media_sha256': media_sha256(clip),
        'sources': [],
        'capture_warnings': ['microphone_unavailable'],
    }), encoding='utf-8')
    read_manifest = ffmpeg_playback.read_manifest_for_media
    reads = []

    def read(*args):
        reads.append(True)
        return read_manifest(*args) if len(reads) > 1 else None

    monkeypatch.setattr(ffmpeg_playback, 'read_manifest_for_media', read)
    monkeypatch.setattr(ffmpeg_playback, 'probe_audio_streams', lambda *_: ())
    assert ffmpeg_playback.discover_playback_sources(str(clip)) == ()
    sources = ffmpeg_playback.discover_playback_sources(str(clip))
    assert len(sources) == 1 and sources[0].capture_warning
    assert ffmpeg_playback.discover_playback_sources(str(clip)) == sources
    assert len(reads) == 2


@pytest.mark.parametrize('outcome', [(True, ''), (False, 'Native decoder could not open')])
def test_native_audio_publishes_preparation_result_before_signal(qtbot, monkeypatch, outcome):
    class Mixer:
        def __init__(self, *_args, **_kwargs):
            if not outcome[0]:
                raise ffmpeg_playback.PlaybackError(outcome[1])

        def close(self):
            pass

    audio_format = QAudioFormat()
    audio_format.setSampleRate(48_000)
    audio_format.setChannelCount(2)
    audio_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
    controller_type = ffmpeg_playback.FFmpegPlaybackController
    monkeypatch.setattr(controller_type, '_select_output_format',
                        lambda *_: (None, audio_format, lambda pcm: pcm))
    monkeypatch.setattr(ffmpeg_playback, 'InProcessFFmpegMixer', Mixer)
    parent = QWidget()
    qtbot.addWidget(parent)
    controller = controller_type('clip.mp4', (), parent)
    observed = []
    controller.ready_changed.connect(
        lambda *_: observed.append(controller.preparation_result),
        Qt.ConnectionType.DirectConnection)
    assert controller.preparation_result is None
    controller.start()
    try:
        qtbot.waitUntil(lambda: bool(observed))
        assert observed == [outcome]
        assert controller.preparation_result == outcome
    finally:
        controller.stop()
