"""Trusted optional microphone DSP has one preview/export graph per open clip."""
import io
import zipfile

import pytest
from PySide6.QtWidgets import QWidget

from core.audio_effects import (
    microphone_effect_filter, register_microphone_effect, unregister_microphone_effects,
)
from core.playback_mix_model import PlaybackSource, ffmpeg_mix_filter
from core.plugin_runtime import PluginContext, PluginRuntime


@pytest.fixture
def effect():
    owner = object()
    register_microphone_effect('test.dsp', lambda: 'highpass=f=80', owner=owner)
    yield owner
    unregister_microphone_effects(owner)


def source(kind='microphone', **kwargs):
    return PlaybackSource(kind, kind, kind, 1, 0, **kwargs)


def test_absent_effect_is_unchanged():
    assert source().processing_filter == 'anull'
    assert source(input_gain_percent=150).processing_filter == 'volume=1.50000000'


def test_only_microphone_gets_effect_before_gain(effect):
    assert source(input_gain_percent=150).processing_filter == 'highpass=f=80,volume=1.50000000'
    for kind in ('application', 'system', 'track'):
        assert source(kind).processing_filter == 'anull'


def test_graph_snapshot_survives_disable_and_new_clip_is_dry(effect):
    opened = source()
    unregister_microphone_effects(effect)
    assert opened.processing_filter == 'highpass=f=80,anull'
    graph, _ = ffmpeg_mix_filter((opened,), {}, 100)
    assert 'highpass=f=80,anull,volume=1.000000' in graph[0]
    assert source().processing_filter == 'anull'


def test_preview_uses_export_snapshot_and_utf8(monkeypatch):
    from core import ffmpeg_playback as playback
    expected = "arnndn=m='C\\:/模型/model.rnnn',anull"
    opened = source(effect_filter_snapshot="arnndn=m='C\\:/模型/model.rnnn'")
    received = []

    class Function:
        def __init__(self, callback):
            self.callback = callback

        def __call__(self, *args):
            return self.callback(*args)

    class Bridge:
        fthr_playback_open = Function(lambda *args: 1)
        fthr_playback_configure_dsp = Function(
            lambda handle, filters, count, *rest: received.extend(
                filters[i].decode('utf-8') for i in range(count)) or 1)
        fthr_playback_close = Function(lambda *args: None)

    monkeypatch.setattr(playback, '_load_bridge', lambda: Bridge())
    monkeypatch.setattr(playback.InProcessFFmpegMixer, '_configure_signatures', lambda self: None)
    mixer = playback.InProcessFFmpegMixer('clip.mp4', (opened,))
    mixer.close()
    assert received == [expected]
    graph, _ = ffmpeg_mix_filter((opened,), {}, 100)
    assert expected in graph[0]


class Packages:
    def __init__(self, code):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('__init__.py', code)
        self.packages = [({'id': 'test.plugin'}, output.getvalue())]

    def feature_packages(self):
        return self.packages


@pytest.mark.parametrize('failure', ['none', 'start', 'constructor'])
def test_runtime_removes_effect_even_when_plugin_cleanup_fails(qtbot, failure):
    code = '''
class Plugin:
    def __init__(self, context):
        context.register_microphone_effect('test.lifecycle', lambda: 'lowpass=f=8000')
        if FAILURE == 'constructor': raise RuntimeError('constructor failed')
    def start(self):
        if FAILURE == 'start': raise RuntimeError('start failed')
    def stop(self):
        raise RuntimeError('cleanup failed')
'''.replace('FAILURE', repr(failure))
    manager = Packages(code)
    window = QWidget()
    window.settings_manager = {}
    qtbot.addWidget(window)
    runtime = PluginRuntime(window, manager)
    try:
        runtime.refresh()
        if failure == 'none':
            assert microphone_effect_filter() == 'lowpass=f=8000'
            manager.packages = []  # disabled/uninstalled packages leave the active set
            runtime.refresh()
        else:
            assert 'test.plugin' in runtime.errors
        assert microphone_effect_filter() == ''
    finally:
        runtime.close()


def test_context_cannot_unregister_another_plugin_effect():
    first = PluginContext(None, {}, object())
    second = PluginContext(None, {}, object())
    try:
        first.register_microphone_effect('test.owned', lambda: 'anull')
        second.unregister_microphone_effect('test.owned')
        assert microphone_effect_filter() == 'anull'
        with pytest.raises(ValueError):
            second.register_microphone_effect('test.owned', lambda: '')
    finally:
        first.unregister_microphone_effect('test.owned')


def test_cached_discovery_resnapshots_effect_when_clip_reopens(monkeypatch, effect):
    from core import ffmpeg_playback as playback
    from core.playback_mix_model import ProbedAudioStream
    identity = object()
    probes = []
    monkeypatch.setattr(playback, '_source_identity', lambda path: identity)
    monkeypatch.setattr(playback, 'probe_audio_streams', lambda path: probes.append(path) or [
        ProbedAudioStream(0, 0, source_type='microphone')])
    monkeypatch.setattr(playback, 'read_manifest_for_media', lambda path: None)
    first = playback.discover_playback_sources('fake.wav')
    unregister_microphone_effects(effect)
    second = playback.discover_playback_sources('fake.wav')
    assert len(probes) == 1
    assert first[0].processing_filter == 'highpass=f=80,anull'
    assert second[0].processing_filter == 'anull'
    playback._SOURCE_CACHE.pop(identity, None)


def test_native_rnnoise_preview_matches_render_and_preserves_dry_file(tmp_path):
    from hashlib import sha256
    from pathlib import Path
    import os
    import shutil
    import subprocess
    import numpy as np
    from core.ffmpeg_playback import InProcessFFmpegMixer
    from core.ffmpeg_tools import get_ffmpeg_exe

    ffmpeg = get_ffmpeg_exe()
    environment = dict(os.environ)
    environment['PATH'] = str(Path(ffmpeg).parent) + os.pathsep + environment.get('PATH', '')

    def run(*args, data=None):
        result = subprocess.run([ffmpeg, '-v', 'error', *map(str, args)], input=data,
                                env=environment, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr.decode(errors='replace')
        return result.stdout

    model = tmp_path / '模型' / 'std.rnnn'
    model.parent.mkdir()
    shutil.copyfile(Path(__file__).resolve().parents[1] / 'plugins/rnnoise/assets/std.rnnn', model)
    escaped = model.as_posix().replace(':', '\\:')
    owner = object()
    register_microphone_effect('test.native', lambda: f"arnndn=m='{escaped}':mix=1", owner=owner)
    try:
        opened = PlaybackSource('mic', 'Microphone', 'microphone', 0, 0)
    finally:
        unregister_microphone_effects(owner)
    original = tmp_path / 'dry.wav'
    time = np.arange(48000) / 48000
    rng = np.random.default_rng(42)
    pcm = (.08 * np.sin(2 * np.pi * 440 * time) + .01 * rng.normal(size=time.shape))
    stereo = np.column_stack((pcm, pcm)).astype('<f4')
    run('-f', 'f32le', '-ar', '48000', '-ac', '2', '-i', '-', '-c:a', 'pcm_f32le',
        '-y', original, data=stereo.tobytes())
    checksum = sha256(original.read_bytes()).digest()
    chunks = []
    mixer = InProcessFFmpegMixer(str(original), (opened,))
    try:
        for _ in range(100):
            block = mixer.pull(1024, [1.0], 1.0)
            if not block:
                break
            chunks.append(block)
        else:
            pytest.fail('Native RNNoise graph did not reach EOF')
    finally:
        mixer.close()
    actual = np.frombuffer(b''.join(chunks), '<f4')
    graph, label = ffmpeg_mix_filter((opened,), {}, 100)
    expected = np.frombuffer(run('-i', original, '-filter_complex', ';'.join(graph),
                                '-map', label, '-t', '1', '-f', 'f32le', '-'), '<f4')
    assert len(actual) == len(expected)
    assert np.max(np.abs(actual[2048:-2048] - expected[2048:-2048])) < 2e-5
    assert np.max(np.abs(actual[2048:-2048] - stereo.reshape(-1)[2048:-2048])) > .001
    assert sha256(original.read_bytes()).digest() == checksum
