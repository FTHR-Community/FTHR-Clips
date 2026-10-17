from __future__ import annotations

import builtins
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from FTHR_UI.core import hotkey_manager as hotkeys
from FTHR_UI import main
from FTHR_UI.ui.error_bar import ErrorBar


def _manager(tmp_path, qtbot):
    manager = hotkeys.HotkeyManager()
    manager.hotkeys = {
        'save_clip': 'F9',
        'save_screenshot': 'F12',
        'start_recording': '',
        'stop_recording': '',
        'confirm_game_detection': 'F8',
        'dismiss_game_detection': 'F7',
    }
    manager.controller_hotkeys = {action: '' for action in manager.hotkeys}
    manager.config_file = tmp_path / 'hotkeys.json'
    return manager


def _write_current_config(manager):
    manager.config_file.parent.mkdir(parents=True, exist_ok=True)
    if any(manager.controller_hotkeys.values()):
        payload = {
            action: {
                'keyboard': manager.hotkeys[action],
                'controller': manager.controller_hotkeys[action],
            }
            for action in manager.hotkeys
        }
    else:
        payload = manager.hotkeys
    manager.config_file.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    return manager.config_file.read_bytes()


def _fail_save_at(monkeypatch, manager, failure_point):
    if failure_point == 'mkdir':
        original = Path.mkdir

        def fail_config_directory(path, *args, **kwargs):
            if path == manager.config_file.parent:
                raise PermissionError('config directory denied')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'mkdir', fail_config_directory)
    elif failure_point == 'write':
        original = builtins.open

        def fail_temp_write(path, *args, **kwargs):
            if Path(path) == manager.config_file.with_suffix('.json.tmp'):
                raise OSError('config write denied')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(builtins, 'open', fail_temp_write)
    elif failure_point == 'replace':
        def fail_replace(*_args, **_kwargs):
            raise PermissionError('config replace denied')

        monkeypatch.setattr(hotkeys.os, 'replace', fail_replace)
    else:
        raise AssertionError(f'unknown failure point: {failure_point}')


@pytest.mark.parametrize('failure_point', ['mkdir', 'write', 'replace'])
def test_keyboard_binding_save_failure_restores_binding_and_old_file(
        qtbot, tmp_path, monkeypatch, failure_point):
    manager = _manager(tmp_path, qtbot)
    original_config = _write_current_config(manager)
    registrations = set()
    registration_attempts = []
    errors = []
    manager.error_occurred.connect(
        lambda title, detail, level: errors.append((title, detail, level)))

    def register(action):
        binding = manager.hotkeys[action]
        registration_attempts.append(binding)
        registrations.add(binding)
        return True

    manager._register_action = register
    manager._unregister_keyboard_action = (
        lambda _action, binding: registrations.discard(binding))
    manager._apply_compositor_config = lambda: None
    _fail_save_at(monkeypatch, manager, failure_point)

    assert manager.set_hotkey('save_clip', 'F1') is False

    assert manager.hotkeys['save_clip'] == 'F9'
    assert registrations == {'F9'}
    assert registration_attempts == ['F1', 'F9']
    assert manager.config_file.read_bytes() == original_config
    assert errors and errors[0][0] == 'HOTKEY SAVE FAILED'
    assert errors[0][2] == 'warning'
    assert len(errors[0][1]) < 300


def test_keyboard_binding_save_failure_reports_if_old_registration_cannot_be_restored(
        qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, qtbot)
    original_config = _write_current_config(manager)
    registration_attempts = []
    registrations = set()
    errors = []
    manager.error_occurred.connect(
        lambda title, detail, level: errors.append((title, detail, level)))

    def register(action):
        binding = manager.hotkeys[action]
        registration_attempts.append(binding)
        if binding == 'F1':
            registrations.add(binding)
            return True
        return False

    manager._register_action = register
    manager._unregister_keyboard_action = (
        lambda _action, binding: registrations.discard(binding))
    manager._apply_compositor_config = lambda: None
    _fail_save_at(monkeypatch, manager, 'replace')

    assert manager.set_hotkey('save_clip', 'F1') is False

    assert manager.hotkeys['save_clip'] == 'F9'
    assert registration_attempts == ['F1', 'F9']
    assert registrations == set()
    assert manager.config_file.read_bytes() == original_config
    assert errors
    assert any('F9' in detail and 'restore' in detail.lower()
               for _title, detail, _level in errors)


def test_controller_binding_save_failure_restores_binding_and_held_state(
        qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, qtbot)
    manager.controller_hotkeys['save_clip'] = 'Back+A'
    manager._controller_active_actions = {'save_clip', 'save_screenshot'}
    original_config = _write_current_config(manager)
    polling_calls = []
    manager._ensure_controller_polling = lambda **kwargs: polling_calls.append(kwargs)
    _fail_save_at(monkeypatch, manager, 'replace')

    assert manager.set_hotkey('save_clip', 'Back+B', device='controller') is False

    assert manager.controller_hotkeys['save_clip'] == 'Back+A'
    assert manager._controller_active_actions == {'save_clip', 'save_screenshot'}
    assert polling_calls == []
    assert manager.config_file.read_bytes() == original_config


def test_successful_keyboard_save_reloads_and_empty_binding_unbinds(
        qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, qtbot)
    manager._register_action = lambda _action: True
    manager._unregister_keyboard_action = lambda *_args: None
    manager._apply_compositor_config = lambda: None

    assert manager.set_hotkey('save_clip', 'F1') is True
    reloaded = hotkeys.HotkeyManager()
    reloaded.config_file = manager.config_file
    reloaded._load_hotkeys()
    assert reloaded.hotkeys['save_clip'] == 'F1'

    assert manager.set_hotkey('save_clip', '') is True
    reloaded._load_hotkeys()
    assert reloaded.hotkeys['save_clip'] == ''


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in self.slots:
            slot(*args)


class _HotkeySignals:
    def __init__(self):
        self.save_clip_triggered = _Signal()
        self.custom_clip_triggered = _Signal()
        self.save_screenshot_triggered = _Signal()
        self.start_recording_triggered = _Signal()
        self.stop_recording_triggered = _Signal()
        self.confirm_game_detection_triggered = _Signal()
        self.dismiss_game_detection_triggered = _Signal()
        self.error_occurred = _Signal()

    def register_all(self):
        pass


def _hotkey_warning_host(error_bar):
    settings = SimpleNamespace(
        get=lambda key, default=None: default
        if key != 'error_notifications_enabled' else True)
    host = SimpleNamespace(
        hotkey_manager=_HotkeySignals(),
        error_bar=error_bar,
        settings_manager=settings,
        _toggle_hotkeys=lambda: None,
        _save_clip=lambda _seconds: None,
        _on_hotkey_save_clip=lambda: None,
        _on_hotkey_save_screenshot=lambda: None,
        _on_hotkey_start_recording=lambda: None,
        _on_hotkey_stop_recording=lambda: None,
        _on_confirm_game_detection=lambda: None,
        _on_dismiss_game_detection=lambda: None,
        _show_compositor_warning=lambda: None,
    )
    host.push_error = lambda title, detail, level, actions=None: (
        main.MainWindow.push_error(
            host, title, detail, level, actions=actions))
    host._show_hotkey_warning = lambda title, detail, level: (
        main.MainWindow._show_hotkey_warning(host, title, detail, level))
    return host


@pytest.mark.parametrize(
    'title', ['HOTKEY SAVE FAILED', 'HOTKEY RESTORE FAILED'])
def test_windows_hotkey_persistence_warnings_reach_error_bar(
        qtbot, monkeypatch, title):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    error_bar = ErrorBar()
    host = _hotkey_warning_host(error_bar)

    main.MainWindow._setup_hotkeys(host)
    host.hotkey_manager.error_occurred.emit(
        title, 'persistence failure detail ' * 20, 'warning')

    assert error_bar.isVisible()
    assert error_bar._current['title'] == title
    assert error_bar._current['level'] == 'warning'
    assert error_bar._current['actions'][0][0] == 'OPEN HOTKEYS'
    assert len(error_bar._detail.text()) == 240


def test_hotkey_warning_route_honors_muting_and_preserves_coded_errors(
        qtbot, monkeypatch):
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    error_bar = ErrorBar()
    settings = {'error_notifications_enabled': False}
    host = _hotkey_warning_host(error_bar)
    host.settings_manager.get = lambda key, default=None: settings.get(
        key, default)

    main.MainWindow._setup_hotkeys(host)
    host.hotkey_manager.error_occurred.emit(
        'HOTKEY SAVE FAILED', 'settings file is read-only', 'warning')
    assert not error_bar.isVisible()

    settings['error_notifications_enabled'] = True
    host.hotkey_manager.error_occurred.emit(
        'CLIP SAVE FAILED', 'disk is full', 'error')
    assert error_bar.isVisible()
    assert error_bar._current['title'] == '[Error 014] CLIP SAVE FAILED'
    assert error_bar._current['detail'] == 'disk is full'
    assert error_bar._current['actions'][0][0] == 'OPEN HOTKEYS'


