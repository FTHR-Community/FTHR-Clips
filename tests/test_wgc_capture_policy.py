"""Windows Graphics Capture plugins change the backend without changing source."""
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QWidget

import main
from core.capture_compatibility import (
    apply_capture_policy, border_free_capture_enabled, capture_policy_is_current,
    wgc_capture_enabled,
)
from core.capture_session import ResolvedCaptureRequest, SourceSnapshot
from core.plugin_runtime import PluginContext, PluginRuntime
from core.settings_manager import SettingsManager
from test_border_free_capture import Packages, config


def test_wgc_registrations_are_owner_scoped_and_coalesced(qtbot, monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    window = QWidget()
    qtbot.addWidget(window)
    first = PluginContext(window, {}, 'first')
    second = PluginContext(window, {}, 'second')
    changes = []
    window._on_wgc_capture_changed = lambda: changes.append(wgc_capture_enabled(window))
    first.register_wgc_capture()
    first.register_wgc_capture()
    second.unregister_wgc_capture()
    assert wgc_capture_enabled(window)
    qtbot.waitUntil(lambda: changes == [True])
    second.register_wgc_capture()
    first.unregister_wgc_capture()
    qtbot.wait(1)
    assert changes == [True]
    second.unregister_wgc_capture()
    first.register_wgc_capture()
    qtbot.wait(1)
    assert changes == [True]
    first.unregister_wgc_capture()
    qtbot.waitUntil(lambda: changes == [True, False])


def test_wgc_requires_windows_owner_and_rejects_incompatible_policy(qtbot, monkeypatch):
    window = QWidget()
    qtbot.addWidget(window)
    wgc = PluginContext(window, {}, 'wgc')
    border_free = PluginContext(window, {}, 'dxgi')
    monkeypatch.setattr(main.sys, 'platform', 'linux')
    with pytest.raises(RuntimeError, match='only on Windows'):
        wgc.register_wgc_capture()
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    with pytest.raises(ValueError, match='requires a plugin owner'):
        PluginContext(window, {}).register_wgc_capture()
    border_free.register_border_free_capture()
    with pytest.raises(RuntimeError, match='Border-free Capture is active'):
        wgc.register_wgc_capture()
    assert border_free_capture_enabled(window)
    assert not wgc_capture_enabled(window)
    border_free.unregister_border_free_capture()
    wgc.register_wgc_capture()
    with pytest.raises(RuntimeError, match='forcing Windows Graphics Capture'):
        border_free.register_border_free_capture()
    assert wgc_capture_enabled(window)
    assert not border_free_capture_enabled(window)


@pytest.mark.parametrize('failure', ['', 'constructor', 'start'])
def test_runtime_releases_wgc_even_if_plugin_does_not(qtbot, monkeypatch, failure):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    manager = Packages(f'''
class Plugin:
    def __init__(self, context):
        context.register_wgc_capture()
        if {failure!r} == 'constructor': raise RuntimeError('constructor failed')
    def start(self):
        if {failure!r} == 'start': raise RuntimeError('start failed')
    def stop(self):
        raise RuntimeError('cleanup failed')
''')
    window = QWidget()
    qtbot.addWidget(window)
    window.settings_manager = {}
    changes = []
    window._on_wgc_capture_changed = lambda: changes.append(wgc_capture_enabled(window))
    runtime = PluginRuntime(window, manager)
    runtime.refresh()
    if failure:
        assert 'test.capture' in runtime.errors
    else:
        assert wgc_capture_enabled(window)
        qtbot.waitUntil(lambda: changes == [True])
    runtime.close()
    assert not wgc_capture_enabled(window)
    if failure:
        qtbot.wait(1)
        assert changes == []
    else:
        qtbot.waitUntil(lambda: changes == [True, False])


@pytest.mark.parametrize('mode', ['desktop', 'window'])
def test_policy_preserves_capture_config_and_rejects_stale_requests(qtbot, monkeypatch, mode):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    window = QWidget()
    qtbot.addWidget(window)
    owner = PluginContext(window, {}, 'plugin')
    source = SourceSnapshot(mode, authority='automatic', monitor_identity='DISPLAY1')
    baseline = config(crop_enabled=True, crop_x=0.1, crop_w=0.5, capture_cursor=False)
    request = ResolvedCaptureRequest(1, 'clips', source, baseline)
    owner.register_wgc_capture()
    override = apply_capture_policy(window, baseline)
    assert override == replace(baseline, capture_backend='wgc')
    forced = replace(request, capture_config=override)
    assert capture_policy_is_current(window, forced)
    with pytest.raises(ValueError, match='compatibility changed'):
        main.MainWindow._resolve_capture_request(window, prepared_request=request)
    owner.unregister_wgc_capture()
    assert main.MainWindow._resolve_capture_request(window, prepared_request=request) is request
    with pytest.raises(ValueError, match='compatibility changed'):
        main.MainWindow._resolve_capture_request(window, prepared_request=forced)


def test_resolver_uses_wgc_without_rewriting_source_or_settings(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    settings = SettingsManager()
    settings.set('capture_mode', 'window')
    settings.set('capture_monitor', 'DISPLAY1')
    settings.set('capture_cursor', False)
    settings.set('framerate', 120)
    source = SourceSnapshot('window', r'C:\Games\osu!.exe',
        {'hwnd': 4242, 'pid': 32, 'exe_path': r'C:\Games\osu!.exe',
         'class_name': 'Game', 'process_created': 11}, authority='manual')
    saved = copy.deepcopy(settings.settings)
    host = QWidget()
    qtbot.addWidget(host)
    host.settings_manager = settings
    host._capture_request_token = 0
    for name in ('_capture_global_defaults_snapshot', '_capture_source_snapshot',
                 '_begin_capture_intent'):
        setattr(host, name, getattr(main.MainWindow, name).__get__(host))
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])
    monkeypatch.setattr(main, 'resolve_monitor_selection', lambda *a: SimpleNamespace(
        path='DISPLAY1', persist_required=False))
    baseline = main.MainWindow._resolve_capture_request(host, source_snapshot=source)
    owner = PluginContext(host, settings, 'plugin')
    owner.register_wgc_capture()
    forced = main.MainWindow._resolve_capture_request(host, source_snapshot=source)
    assert forced.source == baseline.source == source
    assert forced.capture_config.fps == 120
    assert forced.capture_config.capture_cursor is False
    assert forced.capture_config == replace(baseline.capture_config, capture_backend='wgc')
    assert settings.settings == saved


@pytest.mark.parametrize('busy_fields', [
    {'_manual_record_state': 'preparing'},
    {'_manual_record_state': 'recording'},
    {'_save_state': SimpleNamespace(is_busy=lambda: True)},
    {'_restart_pending': True},
    {'_engine_handshake_generation': 5},
    {'_pending_launch_config': object()},
])
def test_wgc_switch_waits_for_capture_owners_to_release(busy_fields):
    calls = []
    host = SimpleNamespace(engine_process=object(), **busy_fields)
    host._on_capture_settings_restart_requested = lambda: calls.append('apply')
    for name in ('_capture_resources_busy', '_apply_latest_desired_request_if_idle'):
        setattr(host, name, getattr(main.MainWindow, name).__get__(host))
    main.MainWindow._on_wgc_capture_changed(host)
    assert calls == []
    assert host._wgc_capture_change_pending
    for field in busy_fields:
        delattr(host, field)
    host._apply_latest_desired_request_if_idle()
    assert calls == ['apply']
    assert not host._wgc_capture_change_pending


def test_wgc_policy_already_applied_or_removed_requires_no_restart(monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    auto_request = ResolvedCaptureRequest(1, 'clips', SourceSnapshot('desktop'), config())
    host = SimpleNamespace(
        _accepted_capture_session=SimpleNamespace(request=auto_request),
        _capture_resources_busy=lambda: False,
        _desired_capture_request=None,
        _wgc_capture_change_pending=True,
        _on_capture_settings_restart_requested=lambda: pytest.fail('policy is already current'),
    )
    assert main.MainWindow._apply_latest_desired_request_if_idle(host) is False
    assert not host._wgc_capture_change_pending
