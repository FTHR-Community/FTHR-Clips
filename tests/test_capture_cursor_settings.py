"""System cursor preference migration, UI persistence, and capture request ownership."""
import ast
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.capture_profile_policy import build_capture_config
from core.capture_session import ResolvedCaptureRequest, SourceSnapshot, bind_capture_session
from core.capture_settings import ApplyStatus, CaptureConfigTracker
from core.settings_manager import SettingsManager
from main import BITRATE_PRESETS, MainWindow, _resolution_to_dims, _SettingsPage
from ui.settings_search import search_settings


def _config(global_values=None):
    return build_capture_config(
        values=dict(clip_length=30, framerate=60, resolution='source', bitrate_level='medium'),
        global_values=global_values or {}, purpose='clips', bitrate_presets=BITRATE_PRESETS,
        resolution_to_dims=_resolution_to_dims, monitor='', codec='auto',
        encoder='auto', preset=4, scaling='stretch')


def test_legacy_settings_default_to_capturing_system_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    path = tmp_path / '.fthr' / 'settings.json'
    path.parent.mkdir()
    for raw, expected in (({}, True), ({'capture_cursor': False}, False),
                          ({'capture_cursor': 'false'}, True)):
        path.write_text(json.dumps(raw), encoding='utf-8')
        assert SettingsManager().get('capture_cursor') is expected
    assert _config().capture_cursor is True


@pytest.mark.parametrize('enabled', [False, True])
def test_cursor_choice_survives_snapshot_diagnostics_and_apply(enabled, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    settings.set('capture_cursor', enabled)
    values = MainWindow._capture_global_defaults_snapshot(SimpleNamespace(settings_manager=settings))
    config = _config(values)
    assert config.capture_cursor is enabled
    request = ResolvedCaptureRequest(request_token=1, purpose='clips',
        source=SourceSnapshot(source_mode='desktop'), capture_config=config)
    session = bind_capture_session(request, process_generation=1,
        native_capture_generation=2, accepted_config_token=3)
    assert session.to_dict()['request']['capture_config']['capture_cursor'] is enabled
    assert MainWindow._diagnostic_capture_config(config)['capture_cursor'] is enabled
    previous = replace(config, capture_cursor=not enabled)
    tracker = CaptureConfigTracker(active=previous)
    tracker.request(config)
    assert tracker.status is ApplyStatus.REQUESTED
    token = tracker.begin_apply()
    assert tracker.active.capture_cursor is not enabled
    tracker.succeed(token)
    assert tracker.active.capture_cursor is enabled


def test_cursor_toggle_commits_then_requests_restart_and_rolls_back_on_failure(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for method in ('_start_encoder_probe', '_populate_camera_devices',
                   '_populate_mic_devices', '_populate_playback_devices',
                   '_populate_profile_mic_devices', '_start_audio_preview_if_current'):
        monkeypatch.setattr(_SettingsPage, method, lambda *a, **k: None)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    assert page.capture_cursor_check.isChecked()
    persisted = []
    page.capture_cursor_changed.connect(lambda: persisted.append(SettingsManager().get('capture_cursor')))
    for enabled in (False, True):
        with qtbot.waitSignal(page.capture_cursor_changed):
            page.capture_cursor_check.click()
        assert persisted[-1] is enabled
    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.capture_cursor_changed):
        page.capture_cursor_check.click()
    assert settings.get('capture_cursor') is True
    assert page.capture_cursor_check.isChecked()
    assert not page.capture_cursor_save_error.isHidden()
    assert any(result.target == 'capture_cursor_check' for result in search_settings('cursor'))


def test_native_cursor_flag_uses_frozen_config_and_follows_existing_arguments():
    # Exercise the actual launch expression without launching devices or an engine.
    tree = ast.parse((Path(__file__).parents[1] / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8'))
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == 'extend'
                and node.args and isinstance(node.args[0], ast.Tuple)
                and isinstance(node.args[0].elts[0], ast.Constant)
                and node.args[0].elts[0].value == '--capture-cursor')
    expr = ast.Expression(call.args[0])
    for enabled in (False, True):
        args = eval(compile(expr, '<launch cursor args>', 'eval'),
                    {'launch_config': SimpleNamespace(capture_cursor=enabled)})
        assert args == ('--capture-cursor', '1' if enabled else '0')
    replay = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == 'launch_args')
    assert call.lineno > replay.lineno
