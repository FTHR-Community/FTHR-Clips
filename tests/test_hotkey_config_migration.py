from __future__ import annotations

import json
import inspect

from FTHR_UI.core.hotkey_manager import HotkeyManager
from FTHR_UI.main import MainWindow


def test_save_clip_keyboard_hotkey_still_registers_and_dispatches():
    setup_source = inspect.getsource(MainWindow._setup_hotkeys)
    dispatch_source = inspect.getsource(MainWindow._on_hotkey_save_clip)

    assert 'save_clip_triggered.connect(self._on_hotkey_save_clip)' in setup_source
    assert 'self.hotkey_manager.register_all()' in setup_source
    assert 'self._save_clip(' in dispatch_source


def test_recording_has_separate_start_and_stop_hotkey_dispatch():
    setup_source = inspect.getsource(MainWindow._setup_hotkeys)
    start_source = inspect.getsource(MainWindow._on_hotkey_start_recording)
    stop_source = inspect.getsource(MainWindow._on_hotkey_stop_recording)

    assert 'start_recording_triggered.connect(' in setup_source
    assert 'stop_recording_triggered.connect(' in setup_source
    assert 'self._start_manual_recording()' in start_source
    assert 'self._stop_manual_recording()' in stop_source


def test_unset_recording_hotkey_is_not_registered(monkeypatch):
    registrations = []
    monkeypatch.setattr(
        'FTHR_UI.core.hotkey_manager.keyboard.add_hotkey',
        lambda *args: registrations.append(args))
    manager = HotkeyManager.__new__(HotkeyManager)

    manager._register_keyboard_hotkey('', object())

    assert registrations == []


def test_lua_hyprland_bindings_are_written_to_custom_keybinds(tmp_path):
    manager = HotkeyManager.__new__(HotkeyManager)
    manager.hotkeys = {'save_clip': 'Ctrl+F9', 'save_screenshot': 'F12'}
    manager.socket_command = lambda action: (
        f'echo -n {action} | /usr/bin/nc -U /run/user/1000/fthr/hotkey.sock')
    manager._write_hyprland_lua_config(tmp_path / 'keybinds.lua')

    content = (tmp_path / 'keybinds.lua').read_text()
    assert 'hl.bind("CTRL + F9"' in content
    assert 'hl.bind("F12"' in content
    assert 'save_clip' in content
    assert 'save_screenshot' in content


def test_lua_hyprland_bindings_are_idempotent(tmp_path):
    manager = HotkeyManager.__new__(HotkeyManager)
    manager.hotkeys = {'save_clip': 'F9', 'save_screenshot': ''}
    manager.socket_command = lambda action: f'echo -n {action} | nc -U /tmp/{action}'
    path = tmp_path / 'keybinds.lua'
    path.write_text('local existing = true\n')

    manager._write_hyprland_lua_config(path)
    path.write_text(path.read_text() + 'hl.bind("USER", user_action)\n')
    manager._write_hyprland_lua_config(path)

    content = path.read_text()
    assert content.count('FTHR Clips hotkeys begin') == 1
    assert content.count('FTHR Clips hotkeys end') == 1
    assert content.count('hl.bind("F9"') == 1
    assert 'hl.bind("USER", user_action)' in content


def test_lua_hyprland_incomplete_block_is_left_untouched(tmp_path):
    manager = HotkeyManager.__new__(HotkeyManager)
    manager.hotkeys = {'save_clip': 'F9', 'save_screenshot': ''}
    manager.socket_command = lambda action: f'echo -n {action} | nc -U /tmp/{action}'
    path = tmp_path / 'keybinds.lua'
    original = '-- FTHR Clips hotkeys begin (managed)\\nhl.bind("USER", user_action)\\n'
    path.write_text(original)

    manager._write_hyprland_lua_config(path)

    assert path.read_text() == original


def test_load_hotkeys_migrates_legacy_nested_schema_without_losing_controller_bindings(tmp_path):
    manager = HotkeyManager.__new__(HotkeyManager)
    manager.config_file = tmp_path / 'hotkeys.json'
    manager.hotkeys = {
        'save_clip': 'F9',
        'save_screenshot': 'F11',
        'confirm_game_detection': 'F8',
        'dismiss_game_detection': 'F7',
    }
    manager.config_file.write_text(json.dumps({
        'save_clip': {'keyboard': 'Ctrl+F9', 'controller': 'Back+X'},
        'save_screenshot': {'keyboard': 'F10', 'controller': ''},
        'game_capture_accept': {'keyboard': 'Ctrl+F8', 'controller': ''},
        'game_capture_dismiss': {'keyboard': 'Ctrl+F7', 'controller': ''},
    }), encoding='utf-8')

    manager._load_hotkeys()

    assert manager.hotkeys == {
        'save_clip': 'Ctrl+F9',
        'save_screenshot': 'F10',
        'confirm_game_detection': 'Ctrl+F8',
        'dismiss_game_detection': 'Ctrl+F7',
    }
    assert manager.controller_hotkeys == {
        'save_clip': 'Back+X',
        'save_screenshot': '',
        'confirm_game_detection': '',
        'dismiss_game_detection': '',
    }
    assert json.loads(manager.config_file.read_text(encoding='utf-8')) == {
        action: {
            'keyboard': manager.hotkeys[action],
            'controller': manager.controller_hotkeys[action],
        }
        for action in manager.hotkeys
    }
