from __future__ import annotations

import ctypes
import json

import pytest

from FTHR_UI.core import hotkey_manager as hotkeys


pytestmark = pytest.mark.skipif(
    __import__('sys').platform != 'win32', reason='Windows native hotkey contract')


class _NativeTarget:
    def winId(self):
        return 1234


class _User32:
    def __init__(self, *, registration_result=True):
        self.registration_result = registration_result
        self.registrations = []
        self.unregistrations = []
        self.raw_registrations = 0

    def RegisterHotKey(self, hwnd, hotkey_id, modifiers, virtual_key):
        self.registrations.append(
            (hwnd.value, hotkey_id, modifiers, virtual_key))
        return self.registration_result

    def UnregisterHotKey(self, hwnd, hotkey_id):
        self.unregistrations.append((hwnd.value, hotkey_id))
        return True

    def RegisterRawInputDevices(self, _devices, count, _size):
        self.raw_registrations += int(count)
        return True


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    manager = hotkeys.HotkeyManager()
    monkeypatch.setattr(
        manager, '_ensure_windows_message_target', lambda: _NativeTarget())
    return manager


def test_windows_hotkey_parts_support_function_and_modifier_chords():
    modifiers, virtual_key = hotkeys.windows_hotkey_parts('F9')
    assert modifiers == hotkeys.MOD_NOREPEAT
    assert virtual_key == 0x78

    modifiers, virtual_key = hotkeys.windows_hotkey_parts('ctrl+shift+s')
    assert modifiers == (
        hotkeys.MOD_NOREPEAT | hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT)
    assert virtual_key == ord('S')


@pytest.mark.parametrize('binding', ['', 'Ctrl', 'F9+F10'])
def test_windows_hotkey_parts_rejects_incomplete_or_multi_key_chords(binding):
    with pytest.raises(ValueError):
        hotkeys.windows_hotkey_parts(binding)


def test_raw_registration_tracks_action_and_unregisters_cleanly(
        qtbot, tmp_path, monkeypatch):
    user32 = _User32()
    monkeypatch.setattr(hotkeys, '_USER32', user32)
    manager = _manager(tmp_path, monkeypatch)

    assert manager._register_windows_hotkey('save_clip', 'F9')
    hotkeys._WINDOWS_HOTKEY_IDS['save_clip']
    assert user32.registrations == []
    assert user32.raw_registrations == 1
    assert manager._windows_raw_hotkey_actions == {'save_clip': (0, 0x78)}
    assert manager._windows_hotkey_actions == {}
    assert manager._windows_hotkey_combos == {'save_clip': 'F9'}

    manager._raw_input_widget = _NativeTarget()
    manager._unregister_windows_hotkey('save_clip')

    assert user32.unregistrations == []
    assert manager._windows_raw_hotkey_actions == {}
    assert manager._windows_hotkey_actions == {}
    assert manager._windows_hotkey_combos == {}


def test_wm_hotkey_dispatches_while_main_window_is_not_involved(
        qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    hotkey_id = hotkeys._WINDOWS_HOTKEY_IDS['save_clip']
    manager._windows_hotkey_actions[hotkey_id] = 'save_clip'
    manager._windows_hotkey_combos['save_clip'] = 'F9'
    received = []
    manager.save_clip_triggered.connect(lambda: received.append('save_clip'))
    message = hotkeys._MSG()
    message.message = hotkeys.WM_HOTKEY
    message.wParam = hotkey_id

    handled, result = manager.nativeEventFilter(
        b'windows_dispatcher_MSG', ctypes.addressof(message))

    assert (handled, result) == (False, 0)
    assert received == ['save_clip']


@pytest.mark.parametrize(
    ('message_type', 'parameter', 'reason'),
    [
        (hotkeys.WM_POWERBROADCAST,
         hotkeys.PBT_APMRESUMEAUTOMATIC, 'system resume'),
        (hotkeys.WM_WTSSESSION_CHANGE,
         hotkeys.WTS_SESSION_UNLOCK, 'session unlock'),
    ],
)
def test_resume_and_unlock_schedule_native_registration_recovery(
        qtbot, tmp_path, monkeypatch, message_type, parameter, reason):
    manager = _manager(tmp_path, monkeypatch)
    refreshes = []
    monkeypatch.setattr(
        manager, '_schedule_windows_hotkey_refresh',
        lambda value: refreshes.append(value))
    message = hotkeys._MSG()
    message.message = message_type
    message.wParam = parameter

    manager.nativeEventFilter(
        b'windows_generic_MSG', ctypes.addressof(message))

    assert refreshes == [reason]


def test_f12_uses_raw_input_without_reviving_low_level_hook(
        qtbot, tmp_path, monkeypatch):
    user32 = _User32(registration_result=False)
    monkeypatch.setattr(hotkeys, '_USER32', user32)
    manager = _manager(tmp_path, monkeypatch)
    received = []
    manager.save_screenshot_triggered.connect(
        lambda: received.append('screenshot'))

    assert manager._register_windows_hotkey('save_screenshot', 'F12')
    assert user32.raw_registrations == 1
    assert manager._windows_raw_hotkey_actions == {
        'save_screenshot': (0, 0x7B)}

    key_down = hotkeys._RAWKEYBOARD()
    key_down.VKey = 0x7B
    manager._process_raw_keyboard_input(key_down)
    manager._process_raw_keyboard_input(key_down)  # auto-repeat is suppressed
    key_up = hotkeys._RAWKEYBOARD()
    key_up.VKey = 0x7B
    key_up.Flags = hotkeys.RI_KEY_BREAK
    manager._process_raw_keyboard_input(key_up)

    assert received == ['screenshot']


def test_another_app_can_own_the_same_chord(qtbot, tmp_path, monkeypatch):
    user32 = _User32(registration_result=False)
    monkeypatch.setattr(hotkeys, '_USER32', user32)
    manager = _manager(tmp_path, monkeypatch)
    received, errors = [], []
    manager.save_clip_triggered.connect(lambda: received.append('clip'))
    manager.error_occurred.connect(lambda *args: errors.append(args))
    assert manager.set_hotkey('save_clip', 'F6')
    assert json.loads(manager.config_file.read_text())['save_clip'] == 'F6'
    event = hotkeys._RAWKEYBOARD()
    event.VKey = 0x75
    manager._process_raw_keyboard_input(event)
    assert received == ['clip']
    assert not errors
    assert not user32.registrations


def test_raw_registration_failure_restores_old_binding(qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    results = iter((False, True))
    monkeypatch.setattr(manager, '_ensure_raw_keyboard_input', lambda: next(results))
    manager._save_hotkeys()
    assert not manager.set_hotkey('save_clip', 'F6')
    assert manager.hotkeys['save_clip'] == 'F9'
    assert json.loads(manager.config_file.read_text())['save_clip'] == 'F9'
    assert manager._windows_raw_hotkey_actions == {'save_clip': (0, 0x78)}


def _key(manager, vk, *, up=False, scan=0, device=0, extended=False):
    event = hotkeys._RAWKEYBOARD()
    event.VKey, event.MakeCode = vk, scan
    event.Flags = (hotkeys.RI_KEY_BREAK if up else 0) | (hotkeys.RI_KEY_E0 if extended else 0)
    manager._process_raw_keyboard_input(event, device)


def test_modifiers_repeat_release_and_two_keyboards(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(hotkeys, '_USER32', _User32())
    manager = _manager(tmp_path, monkeypatch)
    assert manager._register_windows_hotkey('save_clip', 'Shift+F9')
    received = []
    manager.save_clip_triggered.connect(lambda: received.append('clip'))
    _key(manager, 0x10, scan=0x2A, device=1)
    _key(manager, 0x10, scan=0x36, device=2)
    _key(manager, 0x10, scan=0x2A, device=1, up=True)
    _key(manager, 0x78, device=2)
    _key(manager, 0x78, device=2)
    assert received == ['clip']
    _key(manager, 0x78, device=2, up=True)
    manager._last_emit_at.clear()
    _key(manager, 0x78, device=2)
    assert received == ['clip', 'clip']
    _key(manager, 0x78, device=2, up=True)
    _key(manager, 0x10, scan=0x36, device=2, up=True)
    manager._last_emit_at.clear()
    _key(manager, 0x78, device=2)
    assert received == ['clip', 'clip']


def test_device_removal_clears_stuck_keys(qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _key(manager, 0x11, device=42)
    _key(manager, 0x78, device=42)
    message = hotkeys._MSG()
    message.message = hotkeys.WM_INPUT_DEVICE_CHANGE
    message.wParam = hotkeys.GIDC_REMOVAL
    message.lParam = 42
    manager.nativeEventFilter(b'windows_generic_MSG', ctypes.addressof(message))
    assert not manager._raw_keyboard_down
    assert manager._raw_keyboard_modifiers == 0


def test_watchdog_retries_failed_listener(qtbot, tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manager._windows_failed_actions.add('save_clip')
    calls = []
    monkeypatch.setattr(manager, '_schedule_windows_hotkey_refresh', lambda *a, **kw: calls.append(a))
    manager._windows_hotkey_watchdog()
    assert calls


def test_distinct_fast_presses_are_not_lost(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(hotkeys, '_USER32', _User32())
    manager = _manager(tmp_path, monkeypatch)
    assert manager._register_windows_hotkey('save_clip', 'F9')
    received = []
    manager.save_clip_triggered.connect(lambda: received.append('clip'))
    _key(manager, 0x78)
    _key(manager, 0x78, up=True)
    _key(manager, 0x78)
    assert received == ['clip', 'clip']


def test_wm_input_reads_device_and_dispatches(qtbot, tmp_path, monkeypatch):
    user32 = _User32()
    header = hotkeys._RAWINPUTHEADER()
    header.dwType, header.hDevice = hotkeys.RIM_TYPEKEYBOARD, 321
    event = hotkeys._RAWKEYBOARD()
    event.VKey = 0x78
    payload = bytes(header) + bytes(event)
    def get_input(handle, command, buffer, size, header_size):
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint)).contents.value = len(payload)
        if buffer is None:
            return 0
        ctypes.memmove(buffer, payload, len(payload))
        return len(payload)
    user32.GetRawInputData = get_input
    monkeypatch.setattr(hotkeys, '_USER32', user32)
    manager = _manager(tmp_path, monkeypatch)
    assert manager._register_windows_hotkey('save_clip', 'F9')
    received = []
    manager.save_clip_triggered.connect(lambda: received.append('clip'))
    message = hotkeys._MSG()
    message.message = hotkeys.WM_INPUT
    message.lParam = 987
    assert manager.nativeEventFilter(b'windows_generic_MSG', ctypes.addressof(message)) == (False, 0)
    assert received == ['clip']
    assert manager._raw_keyboard_devices[321] == {0x78}
