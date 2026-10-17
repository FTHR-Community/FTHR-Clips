from __future__ import annotations

import pytest

from FTHR_UI import main
from FTHR_UI.core import hotkey_manager as hotkeys


class _PopupHotkeyManager:
    hotkeys = {
        'save_clip': 'F9',
        'save_screenshot': 'F12',
        'start_recording': '',
        'stop_recording': '',
        'confirm_game_detection': 'F8',
        'dismiss_game_detection': 'F7',
    }
    controller_hotkeys = {action: '' for action in hotkeys}

    def get_hotkey(self, action, device='keyboard'):
        values = self.controller_hotkeys if device == 'controller' else self.hotkeys
        return values.get(action, '')

    def get_controller_hotkey(self, action):
        return self.controller_hotkeys.get(action, '')

    def setup_instructions(self, _compositor):
        return ''

    def begin_input_capture(self):
        return None

    def end_input_capture(self):
        return None

    def set_hotkey(self, action, key, device='keyboard'):
        values = self.controller_hotkeys if device == 'controller' else self.hotkeys
        values[action] = key
        return True


def test_hotkey_popup_exposes_all_visible_configurable_actions(qtbot):
    popup = main.HotkeyPopup(_PopupHotkeyManager())
    qtbot.addWidget(popup)

    expected = {
        'save_clip',
        'save_screenshot',
        'confirm_game_detection',
        'dismiss_game_detection',
    }
    if main.sys.platform == 'win32':
        expected.update({'start_recording', 'stop_recording'})

    assert set(popup._controller_buttons) == expected
    labels = {label.text() for label in popup.findChildren(main.QLabel)}
    assert {'CONFIRM GAME', 'DISMISS GAME'} <= labels


def test_keyboard_clear_only_unbinds_selected_action(qtbot):
    manager = _PopupHotkeyManager()
    manager.hotkeys = dict(manager.hotkeys)
    manager.controller_hotkeys = dict(manager.controller_hotkeys)
    manager.controller_hotkeys['save_screenshot'] = 'Back+A'
    popup = main.HotkeyPopup(manager)
    qtbot.addWidget(popup)
    popup._keyboard_clear_buttons['save_screenshot'].click()
    assert manager.hotkeys['save_screenshot'] == ''
    assert manager.hotkeys['save_clip'] == 'F9'
    assert manager.controller_hotkeys['save_screenshot'] == 'Back+A'
    assert not popup._keyboard_clear_buttons['save_screenshot'].isEnabled()
    assert popup._keyboard_buttons['save_screenshot'].text() == hotkeys.format_keyboard_combo('')


@pytest.mark.parametrize(
    ('binding', 'expected_modifiers', 'expected_vk'),
    [
        *[
            (f'F{number}', hotkeys.MOD_NOREPEAT, 0x6F + number)
            for number in range(1, 13)
        ],
        *[
            (f'Alt+F{number}', hotkeys.MOD_NOREPEAT | hotkeys.MOD_ALT,
             0x6F + number)
            for number in range(1, 13)
        ],
    ],
)
def test_function_key_bindings_normalize_and_convert(
        binding, expected_modifiers, expected_vk):
    assert hotkeys.normalize_keyboard_combo(binding) == binding
    assert hotkeys.windows_hotkey_parts(binding) == (
        expected_modifiers, expected_vk)


def test_function_and_game_detection_bindings_survive_save_reload(
        qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = hotkeys.HotkeyManager()
    manager._register_action = lambda _action: True

    assert manager.set_hotkey('save_clip', 'F1')
    assert manager.set_hotkey('confirm_game_detection', 'F8')
    assert manager.set_hotkey('dismiss_game_detection', 'F7')
    assert manager.set_hotkey('save_screenshot', 'Alt+F12')

    reloaded = hotkeys.HotkeyManager()
    assert reloaded.hotkeys['save_clip'] == 'F1'
    assert reloaded.hotkeys['confirm_game_detection'] == 'F8'
    assert reloaded.hotkeys['dismiss_game_detection'] == 'F7'
    assert reloaded.hotkeys['save_screenshot'] == 'Alt+F12'


@pytest.mark.parametrize(
    ('binding', 'existing_action'),
    [('F7', 'dismiss_game_detection'), ('F8', 'confirm_game_detection')],
)
def test_duplicate_game_detection_binding_is_rejected_with_visible_error(
        qtbot, tmp_path, monkeypatch, binding, existing_action):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = hotkeys.HotkeyManager()
    errors = []
    manager.error_occurred.connect(
        lambda title, detail, level: errors.append((title, detail, level)))

    assert not manager.set_hotkey('save_clip', binding)
    assert manager.hotkeys['save_clip'] == 'F9'
    assert errors
    title, detail, level = errors[-1]
    assert title == 'HOTKEY ALREADY IN USE'
    assert binding in detail
    assert existing_action.replace('_', ' ') in detail
    assert level == 'warning'
