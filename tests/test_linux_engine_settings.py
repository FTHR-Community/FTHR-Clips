"""The 2.0 settings and status paths that talk to the Linux capture engine."""
from __future__ import annotations

import json
from types import SimpleNamespace
import sys

import pytest

from core.capture_health import CaptureHealthFlag, CaptureHealthState
from core.encoder_capabilities import probe_encoder_capabilities

linux_only_runtime = pytest.mark.skipif(
    sys.platform == 'win32', reason='needs the Linux runtime helpers')

_SINKS = json.dumps({'schema_version': 1, 'microphones': [
    {'endpoint_id': 'alsa_output.pci-0000_13_00.6.analog-stereo',
     'display_name': 'Ryzen HD Audio Controller Analog Stereo',
     'device_state': 1, 'is_default': True},
    {'endpoint_id': 'easyeffects_sink', 'display_name': 'Easy Effects Sink',
     'device_state': 1, 'is_default': False},
]})


def _make_page(qtbot, monkeypatch, tmp_path):
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    _ = page.separate_audio_check  # materialize the audio page
    return page, settings


@linux_only_runtime
def test_linux_playback_picker_lists_sinks_from_the_engine(qtbot, monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'linux')
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=_SINKS)

    monkeypatch.setattr(main.subprocess, 'run', run)
    monkeypatch.setattr(
        main.threading, 'Thread',
        lambda *, target, **_kwargs: SimpleNamespace(start=target))
    page, settings = _make_page(qtbot, monkeypatch, tmp_path)
    monkeypatch.setattr(page, 'window', lambda: SimpleNamespace(engine_path='/opt/FTHRclips'))
    page._playback_scan_running = False
    page._populate_playback_devices()
    qtbot.waitUntil(lambda: page.playback_combo.count() == 3)

    argv, kwargs = calls[-1]
    assert argv == ['/opt/FTHRclips', '--list-playback-devices']
    # CREATE_NO_WINDOW does not exist on Linux; the child gets the host env.
    assert 'creationflags' not in kwargs
    assert 'env' in kwargs
    assert page.playback_combo.itemText(0) == 'System default'
    assert page.playback_combo.itemData(2) == 'easyeffects_sink'

    changes = []
    page.audio_capture_changed.connect(changes.append)
    page.playback_combo.setCurrentIndex(2)
    assert settings.get('playback_device_id') == 'easyeffects_sink'
    assert changes == [True]

    page._apply_playback_devices([])
    assert page.playback_combo.currentData() == 'easyeffects_sink'
    assert 'unavailable; using the default output' in page.playback_combo.currentText()
    assert changes == [True]


def test_linux_microphone_is_always_mono(qtbot, monkeypatch, tmp_path):
    import main

    monkeypatch.setattr(main.sys, 'platform', 'linux')
    page, settings = _make_page(qtbot, monkeypatch, tmp_path)
    _ = page.mic_mono_check  # materialize
    assert page.mic_mono_check.isChecked()
    assert not page.mic_mono_check.isEnabled()
    assert 'always recorded in mono' in page.mic_mono_check.toolTip()
    assert not settings.get('mic_downmix_mono', False)


def _vaapi_runner(working):
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        encoder = command[command.index('-c:v') + 1]
        return SimpleNamespace(returncode=0 if encoder in working else 1)
    return run, commands


def test_linux_probe_offers_vaapi_under_the_engine_amd_key():
    run, commands = _vaapi_runner({'h264_vaapi', 'hevc_vaapi', 'libopenh264'})
    capabilities = probe_encoder_capabilities('ffmpeg', platform='linux', runner=run)

    assert [(item.key, item.label, item.codecs) for item in capabilities] == [
        ('amf', 'VA-API (AMD / Intel)', ('h264', 'hevc')),
        ('software', 'Software', ('h264',)),
    ]
    assert capabilities[0].encoder_name_for('h264') == 'h264_vaapi'
    vaapi = next(command for command, _ in commands if 'h264_vaapi' in command)
    assert '-vaapi_device' in vaapi
    assert vaapi[vaapi.index('-vf') + 1] == 'format=nv12,hwupload'


def test_windows_probe_is_unchanged_by_the_vaapi_backend():
    run, commands = _vaapi_runner({'h264_amf', 'h264_vaapi'})
    capabilities = probe_encoder_capabilities('ffmpeg', platform='win32', runner=run)

    assert [(item.key, item.label, item.codecs) for item in capabilities] == [
        ('amf', 'AMD AMF', ('h264',))]
    assert not any('_vaapi' in ' '.join(command) for command, _ in commands)


def test_focus_pause_is_shown_as_paused_on_linux_only():
    import main

    paused = int(CaptureHealthFlag.ACTIVE | CaptureHealthFlag.PAUSED)
    assert main._is_focus_pause('linux', CaptureHealthState.DEGRADED, paused)
    assert not main._is_focus_pause(
        'linux', CaptureHealthState.DEGRADED, int(CaptureHealthFlag.ACTIVE))
    assert not main._is_focus_pause('linux', CaptureHealthState.HEALTHY, paused)
    assert not main._is_focus_pause('win32', CaptureHealthState.DEGRADED, paused)
    assert not main._is_focus_pause('linux', CaptureHealthState.DEGRADED, 'junk')


def test_engine_exit_after_a_published_capture_failure_is_explained():
    import main

    failed = CaptureHealthState.FAILED
    assert 'after the capture failure above' in main._engine_stopped_detail(
        'linux', 0, failed)
    # A crash, a non-zero exit or an exit without a published failure stays
    # "unexpected"; Windows text is unchanged.
    assert 'unexpectedly (code -9)' in main._engine_stopped_detail('linux', -9, failed)
    assert 'unexpectedly (code 0)' in main._engine_stopped_detail(
        'linux', 0, CaptureHealthState.HEALTHY)
    assert 'unexpectedly (code 0)' in main._engine_stopped_detail('win32', 0, failed)


@linux_only_runtime
def test_linux_ffmpeg_lookup_uses_linux_names_and_layouts(monkeypatch):
    from core import ffmpeg_tools

    assert ffmpeg_tools._EXE_NAME == 'ffmpeg'
    assert ffmpeg_tools._PROBE_NAME == 'ffprobe'
    candidates = [str(path) for path in ffmpeg_tools._candidate_paths()]
    assert any(path.endswith('FTHRcapture_linux/third_party/ffmpeg/bin/ffmpeg')
               for path in candidates)
    assert str(ffmpeg_tools.Path(sys.executable).parent / 'ffmpeg') in candidates
