"""Optional DXGI policy owns its source, native launch, and plugin lifetime."""
import ast
import copy
import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from PySide6.QtWidgets import QWidget

import main
from core.capture_admission import AdmissionPolicy
from core.capture_compatibility import (
    apply_border_free_capture, border_free_capture_enabled,
    capture_policy_is_current,
)
from core.capture_session import (
    CaptureSnapshotError, ResolvedCaptureRequest, SourceSnapshot,
    bind_capture_session,
)
from core.capture_settings import CaptureConfig, CaptureConfigTracker, ApplyStatus
from core.plugin_runtime import PluginContext, PluginRuntime
from core.settings_manager import SettingsManager


def config(**values):
    return replace(CaptureConfig(fps=60, buffer_seconds=32, width=0, height=0,
        bitrate_kbps=25000, codec='auto', preset=4, monitor='DISPLAY1',
        scaling='stretch', audio_enabled=False), **values)


class Packages:
    def __init__(self, code):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as archive:
            archive.writestr('__init__.py', code)
        self.packages = [({'id': 'test.capture'}, output.getvalue())]

    def feature_packages(self):
        return self.packages


@pytest.mark.parametrize('failure', ['none', 'constructor', 'start'])
def test_runtime_cleans_capture_owner_on_disable_and_failure(qtbot, monkeypatch, failure):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    code = '''
class Plugin:
    def __init__(self, context):
        context.register_border_free_capture()
        if FAILURE == 'constructor': raise RuntimeError('constructor failed')
    def start(self):
        if FAILURE == 'start': raise RuntimeError('start failed')
    def stop(self):
        raise RuntimeError('cleanup failed')
'''.replace('FAILURE', repr(failure))
    window = QWidget()
    qtbot.addWidget(window)
    window.settings_manager = {}
    changes = []
    window._on_border_free_capture_changed = lambda: changes.append(
        border_free_capture_enabled(window))
    manager = Packages(code)
    runtime = PluginRuntime(window, manager)
    try:
        runtime.refresh()
        if failure == 'none':
            assert border_free_capture_enabled(window)
            qtbot.waitUntil(lambda: changes == [True])
            manager.packages = []
            runtime.refresh()
            qtbot.waitUntil(lambda: changes == [True, False])
        else:
            assert 'test.capture' in runtime.errors
            qtbot.wait(1)
            assert changes == []  # never launch a failed plugin's policy
        assert not border_free_capture_enabled(window)
    finally:
        runtime.close()


def test_registration_is_owner_scoped_and_replacement_is_coalesced(qtbot, monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    window = QWidget()
    qtbot.addWidget(window)
    first = PluginContext(window, {}, 'first')
    second = PluginContext(window, {}, 'second')
    changes = []
    window._on_border_free_capture_changed = lambda: changes.append(True)
    first.register_border_free_capture()
    second.unregister_border_free_capture()
    assert border_free_capture_enabled(window)
    qtbot.waitUntil(lambda: len(changes) == 1)
    # Runtime upgrades stop old then start new synchronously.
    first.unregister_border_free_capture()
    second.register_border_free_capture()
    qtbot.wait(1)
    assert len(changes) == 1
    first.unregister_border_free_capture()
    assert border_free_capture_enabled(window)
    second.unregister_border_free_capture()
    qtbot.waitUntil(lambda: len(changes) == 2)


def test_registration_rejects_unsupported_platform_and_desktop_admission(qtbot, monkeypatch):
    window = QWidget()
    qtbot.addWidget(window)
    context = PluginContext(window, {}, 'plugin')
    monkeypatch.setattr(main.sys, 'platform', 'linux')
    with pytest.raises(RuntimeError, match='Windows'):
        context.register_border_free_capture()
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    seen = []
    window._admission_decision_for_settings = lambda **kw: (
        seen.append(kw) or SimpleNamespace(allowed=False, reason='Desktop blocked'))
    with pytest.raises(RuntimeError, match='Desktop blocked'):
        context.register_border_free_capture()
    assert seen == [{'source_mode_override': 'desktop'}]
    assert not border_free_capture_enabled(window)


def test_resolver_freezes_desktop_before_profiles_and_preserves_preferences(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    settings = SettingsManager()
    settings.set('capture_mode', 'window')
    settings.set('target_hwnd', 4242)
    settings.set('capture_cursor', False)
    settings.set('capture_source_authority', 'automatic')
    settings.set('capture_monitor', 'DISPLAY1')
    saved = copy.deepcopy(settings.settings)
    host = QWidget()
    qtbot.addWidget(host)
    host.settings_manager = settings
    host._capture_request_token = 0
    for name in ('_capture_global_defaults_snapshot', '_capture_source_snapshot',
                 '_begin_capture_intent'):
        setattr(host, name, getattr(main.MainWindow, name).__get__(host))
    host._border_free_capture_owners = {'test'}
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])
    monkeypatch.setattr(main, 'resolve_monitor_selection', lambda *a: SimpleNamespace(
        path='DISPLAY1', persist_required=False))
    monkeypatch.setattr(main, 'get_window_info', lambda *a, **kw: pytest.fail(
        'Desktop request must not inspect or authorize the saved HWND'))
    profiles = []
    resolver = main.resolve_profile_values
    monkeypatch.setattr(main, 'resolve_profile_values', lambda cat, exe, values: (
        profiles.append(exe) or resolver(cat, exe, values)))
    original = SourceSnapshot('window', r'C:\Games\Game.exe',
        {'hwnd': 4242, 'pid': 32, 'exe_path': r'C:\Games\Game.exe',
         'class_name': 'Game', 'process_created': 11}, authority='automatic')
    request = main.MainWindow._resolve_capture_request(host, source_snapshot=original)
    assert request.source.source_mode == 'desktop'
    assert request.source.authority == 'manual'
    assert request.source.live_identity is None and request.source.exe_path is None
    assert profiles == [None]
    assert request.capture_config.capture_backend == 'dxgi'
    assert request.capture_config.capture_cursor is True
    assert request.capture_config.crop_enabled is False
    assert request.capture_config.monitor == 'DISPLAY1'
    assert settings.settings == saved
    host._border_free_capture_owners.clear()
    restored = main.MainWindow._resolve_capture_request(host, source_snapshot=original)
    assert restored.source == original
    assert restored.capture_config.capture_backend == 'auto'
    assert restored.capture_config.capture_cursor is False
    assert settings.settings == saved


def test_frozen_backend_survives_session_diagnostics_and_forces_restart():
    original = config(capture_cursor=True, crop_enabled=True,
                      crop_x=0.1, crop_w=0.8)
    forced = apply_border_free_capture(original)
    request = ResolvedCaptureRequest(1, 'clips', SourceSnapshot('desktop'), forced)
    session = bind_capture_session(request, process_generation=1,
        native_capture_generation=2, accepted_config_token=3)
    assert session.request.capture_config == forced
    assert session.to_dict()['request']['capture_config']['capture_backend'] == 'dxgi'
    assert main.MainWindow._diagnostic_capture_config(forced)['capture_backend'] == 'dxgi'
    assert (forced.crop_x, forced.crop_y, forced.crop_w, forced.crop_h) == (0, 0, 1, 1)
    tracker = CaptureConfigTracker(active=config(capture_cursor=True))
    tracker.request(forced)
    assert tracker.status is ApplyStatus.REQUESTED
    with pytest.raises(CaptureSnapshotError, match='capture_backend'):
        replace(request, capture_config=config(capture_backend='invalid'))


def test_policy_toggle_rejects_prepared_request_before_launch(monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    host = SimpleNamespace(_border_free_capture_owners={'plugin'})
    old = ResolvedCaptureRequest(1, 'clips', SourceSnapshot('desktop'), config())
    new = replace(old, capture_config=apply_border_free_capture(config()))
    assert not capture_policy_is_current(host, old)
    assert capture_policy_is_current(host, new)
    with pytest.raises(ValueError, match='compatibility changed'):
        main.MainWindow._resolve_capture_request(host, prepared_request=old)
    host._border_free_capture_owners.clear()
    assert not capture_policy_is_current(host, new)
    assert main.MainWindow._resolve_capture_request(host, prepared_request=old) is old


def test_transition_uses_existing_restart_and_does_not_duplicate_initial_start():
    calls = []
    host = SimpleNamespace(_on_capture_settings_restart_requested=lambda: calls.append('apply'))
    main.MainWindow._on_border_free_capture_changed(host)
    assert calls == []
    host.engine_process = object()
    main.MainWindow._on_border_free_capture_changed(host)
    assert calls == ['apply']
    host._shutdown_requested = True
    main.MainWindow._on_border_free_capture_changed(host)
    assert calls == ['apply']


def test_production_launch_uses_frozen_backend():
    tree = ast.parse((Path(__file__).parents[1] / 'FTHR_UI/main.py').read_text(encoding='utf-8'))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == 'extend'
        and n.args and isinstance(n.args[0], ast.Tuple)
        and isinstance(n.args[0].elts[0], ast.Constant)
        and n.args[0].elts[0].value == '--capture-backend')
    args = eval(compile(ast.Expression(call.args[0]), '<backend launch>', 'eval'),
        {'launch_config': config(capture_backend='dxgi')})
    assert args == ('--capture-backend', 'dxgi')


def test_enabled_window_allowlist_cannot_authorize_border_free_desktop(monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    identity = {'hwnd': 42, 'pid': 3, 'process_created': 4,
                'exe_path': r'C:\Games\Game.exe', 'class_name': 'Game'}
    host = SimpleNamespace(_border_free_capture_owners={'plugin'},
        settings_manager={'capture_mode': 'window', 'target_window_identity': identity})
    sources = []
    host._admission_decision_for_source = lambda source, policy: (
        sources.append(source) or main.MainWindow._admission_decision_for_source(
            host, source, policy))
    host._admission_live_identity = lambda *_a: pytest.fail('Desktop must not use HWND grant')
    policy = AdmissionPolicy(enabled=True, executable_paths=(identity['exe_path'],))
    decision = main.MainWindow._admission_decision_for_settings_once(
        host, capture_window=identity, policy=policy)
    assert decision.allowed is False
    assert sources[0].source_mode == 'desktop'


def test_hidden_start_runtime_is_reused_when_settings_ui_attaches(qtbot, monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    manager = Packages('''
class Plugin:
    def __init__(self, context): self.context = context
    def start(self):
        self.context.window.starts += 1
        self.context.register_border_free_capture()
    def stop(self): self.context.unregister_border_free_capture()
''')
    window = QWidget()
    qtbot.addWidget(window)
    window.settings_manager = {}
    window._ultra_performance = False
    window.starts = 0
    monkeypatch.setattr(main, 'PluginRuntime', lambda host: PluginRuntime(host, manager))
    runtime = main.MainWindow._ensure_plugin_runtime(window)
    try:
        assert border_free_capture_enabled(window)
        assert runtime.surfaces is None
        attached = []
        surfaces = SimpleNamespace(register=lambda *args: attached.append(args),
                                   unregister=lambda *_a: None)
        window._settings_page_widget = SimpleNamespace(get_plugin_surfaces=lambda: surfaces)
        assert main.MainWindow._ensure_plugin_runtime(window) is runtime
        assert main.MainWindow._ensure_plugin_runtime(window) is runtime
        assert window.starts == 1
        assert len(attached) == 1 and attached[0][0] == 'test.capture'
    finally:
        runtime.close()


def test_tray_source_label_tracks_accepted_capture_during_queued_policy_change(monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    host = SimpleNamespace(_border_free_capture_owners={'plugin'},
        settings_manager={'capture_mode': 'window', 'target_window_name': 'Game'})
    assert main.MainWindow._current_capture_source_label(host) == 'Desktop (Border-free capture)'
    # Until idle applies the queued policy, the tray must describe accepted A.
    host._accepted_capture_session = SimpleNamespace(request=SimpleNamespace(
        source=SourceSnapshot('window', display_name='Game')))
    assert main.MainWindow._current_capture_source_label(host) == 'Game'
