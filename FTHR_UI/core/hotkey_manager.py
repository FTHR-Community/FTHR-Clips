"""Global shortcuts through RegisterHotKey on Windows and compositor binds
or the keyboard library on Linux.

Wayland binds send commands to the private socket resolved by
core.linux_runtime.hotkey_socket_path(). Hyprland configuration is written
to ~/.config/hypr/fthr-hotkeys.conf when a shortcut changes.
"""
import ctypes
import keyboard
import socket
import os
import re
import shlex
import sys
import subprocess
import threading
import time
from PySide6.QtCore import QObject, Signal, QTimer, Qt
import json
from pathlib import Path

from core import linux_runtime, linux_tools

_FTHR_HYPR_CONF    = Path.home() / '.config' / 'hypr' / 'fthr-hotkeys.conf'
_HYPR_CONF         = Path.home() / '.config' / 'hypr' / 'hyprland.conf'
_HYPR_LUA_CONF     = Path.home() / '.config' / 'hypr' / 'hyprland.lua'
_HYPR_CUSTOM_LUA   = Path.home() / '.config' / 'hypr' / 'custom' / 'keybinds.lua'
_FTHR_LUA_MARKER   = '-- FTHR Clips hotkeys (managed)'

# Older builds stored each binding as ``{'keyboard': 'F9', 'controller': ...}``
# and used the longer game-detection action names.  Keep the user's keyboard
# choices when upgrading instead of letting the nested record reach Qt.
_LEGACY_ACTION_NAMES = {
    'confirm_game_detection': 'game_capture_accept',
    'dismiss_game_detection': 'game_capture_dismiss',
}

_HOTKEY_ACTIONS = (
    'save_clip',
    'save_screenshot',
    'start_recording',
    'stop_recording',
    'confirm_game_detection',
    'dismiss_game_detection',
)

# The legacy Windows selector accepted controller chords as well as keyboard
# combinations.  Keep the same names and ordering so existing controller
# bindings remain readable after an upgrade.
CONTROLLER_BUTTON_ORDER = (
    'LT', 'RT', 'LB', 'RB',
    'Back', 'Start', 'Share', 'LS', 'RS',
    'DPad Up', 'DPad Down', 'DPad Left', 'DPad Right',
    'Y', 'B', 'A', 'X',
)
_CONTROLLER_BUTTON_ALIASES = {
    'a': 'A', 'b': 'B', 'x': 'X', 'y': 'Y',
    'lb': 'LB', 'left bumper': 'LB', 'l1': 'LB',
    'rb': 'RB', 'right bumper': 'RB', 'r1': 'RB',
    'lt': 'LT', 'left trigger': 'LT', 'l2': 'LT',
    'rt': 'RT', 'right trigger': 'RT', 'r2': 'RT',
    'back': 'Back', 'select': 'Back', 'view': 'Back',
    'start': 'Start', 'menu': 'Start', 'share': 'Share',
    'capture': 'Share', 'screenshot': 'Share', 'misc': 'Share',
    'misc 1': 'Share',
    'ls': 'LS', 'left stick': 'LS', 'l3': 'LS',
    'rs': 'RS', 'right stick': 'RS', 'r3': 'RS',
    'dpad up': 'DPad Up', 'd-pad up': 'DPad Up', 'up': 'DPad Up',
    'dpad down': 'DPad Down', 'd-pad down': 'DPad Down', 'down': 'DPad Down',
    'dpad left': 'DPad Left', 'd-pad left': 'DPad Left', 'left': 'DPad Left',
    'dpad right': 'DPad Right', 'd-pad right': 'DPad Right', 'right': 'DPad Right',
}
_XINPUT_BUTTON_FLAGS = {
    'DPad Up': 0x0001, 'DPad Down': 0x0002,
    'DPad Left': 0x0004, 'DPad Right': 0x0008,
    'Start': 0x0010, 'Back': 0x0020, 'LS': 0x0040, 'RS': 0x0080,
    'LB': 0x0100, 'RB': 0x0200,
    'A': 0x1000, 'B': 0x2000, 'X': 0x4000, 'Y': 0x8000,
}
_XINPUT_TRIGGER_THRESHOLD = 30
WM_INPUT = 0x00FF
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
WM_HOTKEY = 0x0312
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIDEV_INPUTSINK = 0x00000100
RIDEV_DEVNOTIFY = 0x00002000
RIM_TYPEKEYBOARD = 1
RIM_TYPEHID = 2
RI_KEY_BREAK = 0x0001
RAW_KEYBOARD_USAGE = (0x01, 0x06)
RAW_GAME_CONTROLLER_USAGES = (
    (0x01, 0x04),  # Joystick
    (0x01, 0x05),  # Game Pad
    (0x01, 0x08),  # Multi-axis controller
)
_POINTER_MASK = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
_KEYBOARD_MODIFIERS = ('Ctrl', 'Alt', 'Shift', 'Win')
_KEYBOARD_MODIFIER_ALIASES = {
    'ctrl': 'Ctrl', 'control': 'Ctrl', 'alt': 'Alt', 'shift': 'Shift',
    'win': 'Win', 'windows': 'Win', 'meta': 'Win', 'cmd': 'Win', 'command': 'Win',
}

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_SESSION_UNLOCK = 0x0008
NOTIFY_FOR_THIS_SESSION = 0
_WINDOWS_HOTKEY_ID_BASE = 0x4600
_WINDOWS_HOTKEY_IDS = {
    action: _WINDOWS_HOTKEY_ID_BASE + index
    for index, action in enumerate(_HOTKEY_ACTIONS)
}
_WINDOWS_MODIFIER_FLAGS = {
    'Alt': MOD_ALT,
    'Ctrl': MOD_CONTROL,
    'Shift': MOD_SHIFT,
    'Win': MOD_WIN,
}
_WINDOWS_NAMED_VIRTUAL_KEYS = {
    'Backspace': 0x08,
    'Tab': 0x09,
    'Enter': 0x0D,
    'Esc': 0x1B,
    'Escape': 0x1B,
    'Space': 0x20,
    'Page Up': 0x21,
    'Page Down': 0x22,
    'End': 0x23,
    'Home': 0x24,
    'Left': 0x25,
    'Up': 0x26,
    'Right': 0x27,
    'Down': 0x28,
    'Insert': 0x2D,
    'Delete': 0x2E,
}
_WINDOWS_MODIFIER_VIRTUAL_KEYS = {
    0x10: MOD_SHIFT,   # VK_SHIFT
    0x11: MOD_CONTROL, # VK_CONTROL
    0x12: MOD_ALT,     # VK_MENU
    0x5B: MOD_WIN,     # VK_LWIN
    0x5C: MOD_WIN,     # VK_RWIN
    0xA0: MOD_SHIFT,   # VK_LSHIFT
    0xA1: MOD_SHIFT,   # VK_RSHIFT
    0xA2: MOD_CONTROL, # VK_LCONTROL
    0xA3: MOD_CONTROL, # VK_RCONTROL
    0xA4: MOD_ALT,     # VK_LMENU
    0xA5: MOD_ALT,     # VK_RMENU
}


class _XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [
        ('wButtons', ctypes.c_ushort),
        ('bLeftTrigger', ctypes.c_ubyte),
        ('bRightTrigger', ctypes.c_ubyte),
        ('sThumbLX', ctypes.c_short),
        ('sThumbLY', ctypes.c_short),
        ('sThumbRX', ctypes.c_short),
        ('sThumbRY', ctypes.c_short),
    ]


class _XINPUT_STATE(ctypes.Structure):
    _fields_ = [
        ('dwPacketNumber', ctypes.c_uint),
        ('Gamepad', _XINPUT_GAMEPAD),
    ]


class _MSG(ctypes.Structure):
    _fields_ = [
        ('hwnd', ctypes.c_void_p),
        ('message', ctypes.c_uint),
        ('wParam', ctypes.c_size_t),
        ('lParam', ctypes.c_ssize_t),
        ('time', ctypes.c_uint32),
        ('pt_x', ctypes.c_long),
        ('pt_y', ctypes.c_long),
    ]


class _RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ('usUsagePage', ctypes.c_ushort),
        ('usUsage', ctypes.c_ushort),
        ('dwFlags', ctypes.c_uint),
        ('hwndTarget', ctypes.c_void_p),
    ]


class _RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ('dwType', ctypes.c_uint),
        ('dwSize', ctypes.c_uint),
        ('hDevice', ctypes.c_void_p),
        ('wParam', ctypes.c_size_t),
    ]


class _RAWKEYBOARD(ctypes.Structure):
    _fields_ = [
        ('MakeCode', ctypes.c_ushort),
        ('Flags', ctypes.c_ushort),
        ('Reserved', ctypes.c_ushort),
        ('VKey', ctypes.c_ushort),
        ('Message', ctypes.c_uint),
        ('ExtraInformation', ctypes.c_uint),
    ]


_USER32 = ctypes.WinDLL('user32', use_last_error=True) if sys.platform == 'win32' else None
if _USER32 is not None:
    _USER32.RegisterHotKey.argtypes = (
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint)
    _USER32.RegisterHotKey.restype = ctypes.c_bool
    _USER32.UnregisterHotKey.argtypes = (ctypes.c_void_p, ctypes.c_int)
    _USER32.UnregisterHotKey.restype = ctypes.c_bool
    _USER32.VkKeyScanW.argtypes = (ctypes.c_wchar,)
    _USER32.VkKeyScanW.restype = ctypes.c_short
    _USER32.RegisterRawInputDevices.argtypes = (
        ctypes.POINTER(_RAWINPUTDEVICE), ctypes.c_uint, ctypes.c_uint)
    _USER32.RegisterRawInputDevices.restype = ctypes.c_bool
    _USER32.GetRawInputData.argtypes = (
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint), ctypes.c_uint)
    _USER32.GetRawInputData.restype = ctypes.c_uint
    _USER32.GetRawInputDeviceInfoW.argtypes = (
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint))
    _USER32.GetRawInputDeviceInfoW.restype = ctypes.c_uint

try:
    _WTSAPI32 = (ctypes.WinDLL('wtsapi32', use_last_error=True)
                 if sys.platform == 'win32' else None)
except OSError:
    _WTSAPI32 = None
if _WTSAPI32 is not None:
    _WTSAPI32.WTSRegisterSessionNotification.argtypes = (
        ctypes.c_void_p, ctypes.c_uint)
    _WTSAPI32.WTSRegisterSessionNotification.restype = ctypes.c_bool
    _WTSAPI32.WTSUnRegisterSessionNotification.argtypes = (ctypes.c_void_p,)
    _WTSAPI32.WTSUnRegisterSessionNotification.restype = ctypes.c_bool


def _load_xinput_get_state():
    """Return the first usable XInput reader, or ``None`` off Windows."""
    if sys.platform != 'win32':
        return None
    for dll_name in ('xinput1_4', 'xinput9_1_0', 'xinput1_3'):
        try:
            dll = ctypes.WinDLL(dll_name)
            get_state = dll.XInputGetState
            get_state.argtypes = (ctypes.c_uint, ctypes.POINTER(_XINPUT_STATE))
            get_state.restype = ctypes.c_uint
            return get_state
        except Exception:
            continue
    return None


def _qt_key_value(key) -> int:
    return int(key.value) if hasattr(key, 'value') else int(key)


def _build_qt_key_names() -> dict[int, str]:
    names = {
        _qt_key_value(Qt.Key.Key_Control): 'Ctrl',
        _qt_key_value(Qt.Key.Key_Alt): 'Alt',
        _qt_key_value(Qt.Key.Key_Shift): 'Shift',
        _qt_key_value(Qt.Key.Key_Meta): 'Win',
        _qt_key_value(Qt.Key.Key_Escape): 'Esc',
        _qt_key_value(Qt.Key.Key_Return): 'Enter',
        _qt_key_value(Qt.Key.Key_Enter): 'Enter',
        _qt_key_value(Qt.Key.Key_Space): 'Space',
        _qt_key_value(Qt.Key.Key_Tab): 'Tab',
        _qt_key_value(Qt.Key.Key_Backspace): 'Backspace',
        _qt_key_value(Qt.Key.Key_Delete): 'Delete',
        _qt_key_value(Qt.Key.Key_Insert): 'Insert',
        _qt_key_value(Qt.Key.Key_Home): 'Home',
        _qt_key_value(Qt.Key.Key_End): 'End',
        _qt_key_value(Qt.Key.Key_PageUp): 'Page Up',
        _qt_key_value(Qt.Key.Key_PageDown): 'Page Down',
        _qt_key_value(Qt.Key.Key_Up): 'Up',
        _qt_key_value(Qt.Key.Key_Down): 'Down',
        _qt_key_value(Qt.Key.Key_Left): 'Left',
        _qt_key_value(Qt.Key.Key_Right): 'Right',
    }
    for number in range(10):
        names[_qt_key_value(getattr(Qt.Key, f'Key_{number}'))] = str(number)
    for code in range(ord('A'), ord('Z') + 1):
        letter = chr(code)
        names[_qt_key_value(getattr(Qt.Key, f'Key_{letter}'))] = letter
    for number in range(1, 25):
        names[_qt_key_value(getattr(Qt.Key, f'Key_F{number}'))] = f'F{number}'
    return names


_QT_KEY_NAMES = _build_qt_key_names()


def normalize_keyboard_combo(combo) -> str:
    """Canonicalise a keyboard chord for display, storage and registration."""
    if combo is None:
        return ''
    if isinstance(combo, (list, tuple, set)):
        raw_parts = [str(part).strip() for part in combo if str(part).strip()]
    else:
        raw_parts = [part.strip() for part in str(combo).split('+') if part.strip()]
    seen, modifiers, keys = set(), [], []
    for raw_part in raw_parts:
        key = raw_part.lower().replace('_', ' ')
        canonical = _KEYBOARD_MODIFIER_ALIASES.get(key)
        if canonical is None:
            if len(raw_part) == 1 and raw_part.isalpha():
                canonical = raw_part.upper()
            elif len(raw_part) == 1 and raw_part.isdigit():
                canonical = raw_part
            elif key.startswith('f') and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
                canonical = f'F{int(key[1:])}'
            else:
                canonical = raw_part.strip().title()
        if canonical in seen:
            continue
        seen.add(canonical)
        (modifiers if canonical in _KEYBOARD_MODIFIERS else keys).append(canonical)
    return '+'.join([mod for mod in _KEYBOARD_MODIFIERS if mod in modifiers] + keys)


def format_keyboard_combo(combo) -> str:
    return normalize_keyboard_combo(combo) or 'Unset'


def windows_hotkey_parts(combo: str) -> tuple[int, int]:
    """Translate a saved keyboard chord into RegisterHotKey flags and a VK.

    Keeping the conversion independent from registration makes unsupported
    bindings fail before they can leave a half-registered shortcut behind.
    """
    normalized = normalize_keyboard_combo(combo)
    parts = normalized.split('+') if normalized else []
    key_parts = [part for part in parts if part not in _KEYBOARD_MODIFIERS]
    if len(key_parts) != 1:
        raise ValueError('a hotkey must contain exactly one non-modifier key')

    modifiers = MOD_NOREPEAT
    for part in parts:
        modifiers |= _WINDOWS_MODIFIER_FLAGS.get(part, 0)

    key_name = key_parts[0]
    if len(key_name) == 1 and key_name.isalnum():
        virtual_key = ord(key_name.upper())
    elif re.fullmatch(r'F(?:[1-9]|1\d|2[0-4])', key_name):
        virtual_key = 0x70 + int(key_name[1:]) - 1
    else:
        virtual_key = _WINDOWS_NAMED_VIRTUAL_KEYS.get(key_name)

    # Qt's binding selector can produce a printable punctuation key. Ask the
    # active Windows keyboard layout for that key instead of hard-coding a US
    # layout. VkKeyScanW's high byte contributes Shift/Ctrl/Alt when required.
    if virtual_key is None and len(key_name) == 1 and _USER32 is not None:
        translated = int(_USER32.VkKeyScanW(key_name))
        if translated != -1:
            virtual_key = translated & 0xFF
            layout_modifiers = (translated >> 8) & 0xFF
            if layout_modifiers & 1:
                modifiers |= MOD_SHIFT
            if layout_modifiers & 2:
                modifiers |= MOD_CONTROL
            if layout_modifiers & 4:
                modifiers |= MOD_ALT

    if virtual_key is None:
        raise ValueError(f'unsupported Windows hotkey key: {key_name!r}')
    return modifiers, virtual_key


def normalize_controller_combo(combo) -> str:
    """Canonicalise a controller chord using the legacy selector's order."""
    if combo is None:
        return ''
    if isinstance(combo, (list, tuple, set)):
        raw_parts = [str(part).strip() for part in combo if str(part).strip()]
    else:
        raw_parts = [part.strip() for part in str(combo).split('+') if part.strip()]
    seen = set()
    raw_buttons = []
    for raw_part in raw_parts:
        canonical = _CONTROLLER_BUTTON_ALIASES.get(
            raw_part.lower().replace('_', ' '), raw_part.strip())
        if canonical in CONTROLLER_BUTTON_ORDER:
            seen.add(canonical)
        elif _is_raw_controller_token(canonical) and canonical not in raw_buttons:
            raw_buttons.append(canonical)
    ordered = [button for button in CONTROLLER_BUTTON_ORDER if button in seen]
    return '+'.join(ordered + raw_buttons)


def _is_raw_controller_token(value: str) -> bool:
    return bool(re.fullmatch(r'HID:[^:]+:B\d+\.\d+', str(value).strip(), re.IGNORECASE))


def _format_controller_button(value: str) -> str:
    value = str(value)
    return f'Raw {value.rsplit(":", 1)[-1]}' if _is_raw_controller_token(value) else value


def format_controller_combo(combo) -> str:
    normalized = normalize_controller_combo(combo)
    if not normalized:
        return 'Add'
    return ' + '.join(_format_controller_button(button)
                      for button in normalized.split('+'))


class HotkeyManager(QObject):
    """Manages global keyboard and Windows controller hotkeys."""
    
    # Signals
    save_clip_triggered = Signal()
    save_screenshot_triggered = Signal()
    start_recording_triggered = Signal()
    stop_recording_triggered = Signal()
    confirm_game_detection_triggered  = Signal()
    dismiss_game_detection_triggered  = Signal()
    error_occurred = Signal(str, str, str)   # title, detail, level
    controller_buttons_changed = Signal(object)
    
    def __init__(self):
        super().__init__()
        # Lives next to all the other user state in ~/.fthr. Survives reinstalls.
        self.config_file = Path.home() / '.fthr' / 'hotkeys.json'

        # Default shortcuts for clips and screenshots; recording is opt-in.
        self.hotkeys = {
            'save_clip': 'F9',
            'save_screenshot': 'F12',
            # Recording controls are opt-in so an upgrade never claims a key
            # the user already relies on in a game or another recorder.
            'start_recording': '',
            'stop_recording': '',
            'confirm_game_detection':  'F8',
            'dismiss_game_detection':  'F7',
        }
        self.controller_hotkeys = {action: '' for action in self.hotkeys}

        # Non-Windows fallback registrations. Native Windows registrations are
        # tracked by action/id below so they survive a hidden main window.
        self._registered_hotkeys = []
        self._windows_hotkey_actions: dict[int, str] = {}
        self._windows_hotkey_combos: dict[str, str] = {}
        self._windows_failed_actions: set[str] = set()
        self._session_notifications_registered = False
        self._windows_refresh_pending = False
        self._windows_hotkey_watchdog_timer = QTimer(self)
        self._windows_hotkey_watchdog_timer.setInterval(30_000)
        self._windows_hotkey_watchdog_timer.timeout.connect(
            self._windows_hotkey_watchdog)

        self._socket_running = False
        self._socket_thread: threading.Thread | None = None
        self._input_capture_depth = 0
        self._xinput_get_state = _load_xinput_get_state()
        self._xinput_controller_buttons: set[str] = set()
        self._raw_input_widget = None
        self._raw_input_registered = False
        self._raw_keyboard_registered = False
        self._windows_raw_hotkey_actions: dict[str, tuple[int, int]] = {}
        self._raw_keyboard_modifiers = 0
        self._raw_keyboard_down: set[int] = set()
        self._raw_device_names = {}
        self._raw_hid_reports = {}
        self._raw_controller_buttons: set[str] = set()
        self._raw_controller_buttons_by_device = {}
        self._controller_buttons: set[str] = set()
        self._controller_active_actions: set[str] = set()
        self._controller_timer = QTimer(self)
        self._controller_timer.setInterval(40)
        self._controller_timer.timeout.connect(self._poll_controller_buttons)

        self._load_hotkeys()
    
    def _load_hotkeys(self):
        """Load hotkeys from config file"""
        # This also keeps the migration helper usable in isolated tests and
        # maintenance scripts that construct a lightweight manager instance.
        if not hasattr(self, 'controller_hotkeys'):
            self.controller_hotkeys = {action: '' for action in self.hotkeys}
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    saved_hotkeys = json.load(f)
                if not isinstance(saved_hotkeys, dict):
                    raise ValueError('hotkeys config must contain an object')

                # Retire the old action instead of registering or re-saving it.
                migrated = any(
                    isinstance(mapping, dict) and 'save_extended_clip' in mapping
                    for mapping in (saved_hotkeys, saved_hotkeys.get('keyboard'),
                                    saved_hotkeys.get('controller')))
                keyboard_map = saved_hotkeys.get('keyboard')
                controller_map = saved_hotkeys.get('controller')
                for action in self.hotkeys:
                    source_action = action
                    if source_action not in saved_hotkeys:
                        source_action = _LEGACY_ACTION_NAMES.get(action, action)
                        migrated |= source_action != action and source_action in saved_hotkeys

                    value = saved_hotkeys.get(source_action)
                    if isinstance(keyboard_map, dict):
                        value = keyboard_map.get(source_action, value)
                        migrated = True
                    if isinstance(value, dict):
                        keyboard_value = value.get('keyboard')
                        controller_value = value.get('controller')
                    else:
                        keyboard_value = value
                        controller_value = (
                            controller_map.get(source_action)
                            if isinstance(controller_map, dict) else None)
                    if isinstance(keyboard_value, str) and keyboard_value.strip():
                        self.hotkeys[action] = normalize_keyboard_combo(keyboard_value)
                    elif value is not None:
                        migrated = True
                    if isinstance(controller_value, str):
                        self.controller_hotkeys[action] = normalize_controller_combo(
                            controller_value)
                        migrated = True

                if migrated:
                    self._save_hotkeys()
            except Exception as e:
                print(f"Failed to load hotkeys: {e}")
    
    def _save_hotkeys(self):
        """Save hotkeys to config file"""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.config_file.with_suffix('.json.tmp')
        try:
            has_controller_bindings = any(self.controller_hotkeys.values())
            if has_controller_bindings:
                payload = {
                    action: {
                        'keyboard': self.hotkeys[action],
                        'controller': self.controller_hotkeys[action],
                    }
                    for action in self.hotkeys
                }
            else:
                payload = self.hotkeys
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f, indent=2)
            os.replace(str(tmp), str(self.config_file))
        except Exception as e:
            print(f"Failed to save hotkeys: {e}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
    
    def set_hotkey(self, action: str, key: str, device: str = 'keyboard'):
        """Bind an action from ``_HOTKEY_ACTIONS`` to a key such as ctrl+shift+s."""
        if action not in self.hotkeys:
            print(f"Unknown action: {action}")
            return False

        if device == 'controller':
            normalized = normalize_controller_combo(key)
            for other_action, other_key in self.controller_hotkeys.items():
                if (normalized and other_action != action
                        and other_key == normalized):
                    self.error_occurred.emit(
                        'CONTROLLER HOTKEY ALREADY IN USE',
                        f'"{format_controller_combo(normalized)}" is already bound to '
                        f'{other_action.replace("_", " ")}. Pick a different binding.',
                        'warning',
                    )
                    return False
            self.controller_hotkeys[action] = normalized
            self._controller_active_actions.discard(action)
            self._save_hotkeys()
            self._ensure_controller_polling()
            return True
        if device != 'keyboard':
            print(f"Unknown hotkey device: {device}")
            return False

        key = normalize_keyboard_combo(key)

        # Refuse duplicate bindings — the same key on two actions makes one
        # keypress fire both (two saves, the second eaten by the spam guard,
        # which reads as random behavior to the user).
        for other_action, other_key in self.hotkeys.items():
            if other_action != action and other_key == key:
                self.error_occurred.emit(
                    'HOTKEY ALREADY IN USE',
                    f'"{key}" is already bound to {other_action.replace("_", " ")}.'
                    ' Pick a different key.',
                    'warning',
                )
                return False

        # Tear down the old binding first so changing a shortcut cannot leave
        # both keys registered and trigger the action twice.
        old_key = self.hotkeys.get(action)
        self._unregister_keyboard_action(action, old_key)
        
        # Validate the new Windows chord before persisting it. RegisterHotKey
        # gives us an authoritative collision result; if another app owns the
        # chord, restore the old working binding instead of saving a dead one.
        self.hotkeys[action] = key
        registered = self._register_action(action)
        if sys.platform == 'win32' and not registered:
            self.hotkeys[action] = old_key
            self._register_action(action)
            return False

        self._save_hotkeys()

        self._apply_compositor_config()
        return True
    
    def get_hotkey(self, action: str, device: str = 'keyboard') -> str:
        """Get the current keyboard or controller hotkey for an action."""
        if device == 'controller':
            return self.controller_hotkeys.get(action, '')
        return self.hotkeys.get(action, '')

    def get_controller_hotkey(self, action: str) -> str:
        return self.get_hotkey(action, device='controller')

    def set_controller_hotkey(self, action: str, key: str) -> bool:
        return self.set_hotkey(action, key, device='controller')
    
    def _register_keyboard_hotkey(self, key: str, signal, *, action: str = '') -> bool:
        """Register one keyboard shortcut on the platform's reliable path."""
        if not key:
            return True
        if sys.platform == 'win32':
            return self._register_windows_hotkey(action, key)
        try:
            keyboard.add_hotkey(key, lambda: signal.emit())
            if key not in self._registered_hotkeys:
                self._registered_hotkeys.append(key)
            return True
        except Exception as e:
            self._keyboard_failed = True
            if sys.platform != 'linux':
                print(f"Failed to register hotkey {key}: {e}")
                self.error_occurred.emit(
                    'HOTKEY REGISTRATION FAILED',
                    f'Could not register "{key}" — it may be in use by another '
                    'app. Pick a different key in Hotkey settings.',
                    'warning',
                )
            return False

    def _register_windows_hotkey(self, action: str, key: str) -> bool:
        """Register one chord against an always-alive hidden native window."""
        if _USER32 is None or action not in _WINDOWS_HOTKEY_IDS:
            self._keyboard_failed = True
            self._windows_failed_actions.add(action)
            return False
        target = self._ensure_windows_message_target()
        if target is None:
            self._keyboard_failed = True
            self._windows_failed_actions.add(action)
            self.error_occurred.emit(
                'HOTKEY REGISTRATION FAILED',
                'Windows could not create the background hotkey target.',
                'warning',
            )
            return False
        try:
            modifiers, virtual_key = windows_hotkey_parts(key)
        except ValueError as exc:
            self._keyboard_failed = True
            self._windows_failed_actions.add(action)
            print(f'[Hotkey] Unsupported Windows binding {key!r}: {exc}')
            self.error_occurred.emit(
                'HOTKEY REGISTRATION FAILED',
                f'"{key}" is not supported as a Windows global shortcut.',
                'warning',
            )
            return False

        hotkey_id = _WINDOWS_HOTKEY_IDS[action]
        self._unregister_windows_hotkey(action)
        ctypes.set_last_error(0)
        registered = bool(_USER32.RegisterHotKey(
            ctypes.c_void_p(int(target.winId())), hotkey_id,
            modifiers, virtual_key))
        if not registered:
            error_code = ctypes.get_last_error()
            # Microsoft reserves F12 from RegisterHotKey for debuggers. Keep a
            # saved F12 binding working through the same hidden HWND using Raw
            # Input, never by reviving the fragile low-level keyboard hook.
            if (virtual_key == 0x7B
                    and self._register_windows_raw_hotkey(
                        action, key, modifiers, virtual_key)):
                return True
            self._keyboard_failed = True
            self._windows_failed_actions.add(action)
            print('[Hotkey] Native registration failed '
                  f'action={action} key={key!r} WinError={error_code}')
            detail = (
                f'Could not register "{key}" — another app may already own it. '
                'Pick a different key in Hotkey settings.'
            )
            self.error_occurred.emit(
                'HOTKEY REGISTRATION FAILED', detail, 'warning')
            return False

        self._windows_hotkey_actions[hotkey_id] = action
        self._windows_hotkey_combos[action] = key
        self._windows_failed_actions.discard(action)
        print(f'[Hotkey] Registered native Windows hotkey {key} -> {action}')
        return True

    def _register_windows_raw_hotkey(
            self, action: str, key: str, modifiers: int,
            virtual_key: int) -> bool:
        if not self._ensure_raw_keyboard_input():
            return False
        self._windows_raw_hotkey_actions[action] = (
            modifiers & ~MOD_NOREPEAT, virtual_key)
        self._windows_hotkey_combos[action] = key
        self._windows_failed_actions.discard(action)
        print(f'[Hotkey] Registered Windows Raw Input fallback {key} -> {action}')
        return True

    def _register_action(self, action: str) -> bool:
        registrations = {
            'save_clip': self._register_save_clip,
            'save_screenshot': self._register_save_screenshot,
            'start_recording': self._register_start_recording,
            'stop_recording': self._register_stop_recording,
            'confirm_game_detection': self._register_confirm_game_detection,
            'dismiss_game_detection': self._register_dismiss_game_detection,
        }
        register = registrations.get(action)
        if register is None:
            return False
        return bool(register())

    def _register_save_clip(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['save_clip'], self.save_clip_triggered,
            action='save_clip')


    def _register_save_screenshot(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['save_screenshot'], self.save_screenshot_triggered,
            action='save_screenshot')

    def _register_start_recording(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['start_recording'], self.start_recording_triggered,
            action='start_recording')

    def _register_stop_recording(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['stop_recording'], self.stop_recording_triggered,
            action='stop_recording')

    def _register_confirm_game_detection(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['confirm_game_detection'],
            self.confirm_game_detection_triggered,
            action='confirm_game_detection')

    def _register_dismiss_game_detection(self):
        return self._register_keyboard_hotkey(
            self.hotkeys['dismiss_game_detection'],
            self.dismiss_game_detection_triggered,
            action='dismiss_game_detection')

    def register_all(self):
        """Register all hotkeys"""
        self._unregister_keyboard_hotkeys()
        self._keyboard_failed = False
        if sys.platform == 'win32':
            self._windows_failed_actions.clear()
        for action in _HOTKEY_ACTIONS:
            self._register_action(action)
        if sys.platform == 'win32' and self._input_capture_depth == 0:
            self._windows_hotkey_watchdog_timer.start()
            expected = sum(bool(self.hotkeys.get(action))
                           for action in _HOTKEY_ACTIONS)
            print('[Hotkey] Native Windows registrations '
                  f'{len(self._windows_hotkey_combos)}/{expected}')
        self._start_socket_server()
        self._apply_compositor_config()
        self._ensure_controller_polling()
        self._warn_if_linux_hotkeys_dead()

    def begin_input_capture(self):
        """Pause dispatch while a selector records a new keyboard/controller chord."""
        if self._input_capture_depth == 0:
            self._windows_hotkey_watchdog_timer.stop()
            self._unregister_keyboard_hotkeys()
            self._controller_active_actions.clear()
            self._ensure_controller_polling(force=True)
        self._input_capture_depth += 1

    def end_input_capture(self):
        if self._input_capture_depth == 0:
            return
        self._input_capture_depth -= 1
        if self._input_capture_depth == 0:
            self._controller_active_actions.clear()
            self.register_all()

    def qt_key_name(self, event) -> str:
        key_name = _QT_KEY_NAMES.get(_qt_key_value(event.key()))
        if key_name:
            return key_name
        text = event.text()
        if text and text.strip():
            char = text.strip()
            if len(char) == 1:
                return char.upper() if char.isalpha() else char
        return ''

    def qt_modifier_names(self, event) -> list[str]:
        modifiers = event.modifiers()
        names = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            names.append('Ctrl')
        if modifiers & Qt.KeyboardModifier.AltModifier:
            names.append('Alt')
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            names.append('Shift')
        if modifiers & Qt.KeyboardModifier.MetaModifier:
            names.append('Win')
        return names

    def _ensure_windows_message_target(self):
        """Create an invisible HWND that remains alive while the app is in tray."""
        if sys.platform != 'win32' or _USER32 is None:
            return None
        try:
            from PySide6.QtWidgets import QApplication, QWidget
            app = QApplication.instance()
            if app is None:
                return None
            if self._raw_input_widget is None:
                class _WindowsMessageTarget(QWidget):
                    def __init__(self, dispatcher):
                        super().__init__()
                        self._dispatcher = dispatcher

                    def nativeEvent(self, event_type, message):
                        return self._dispatcher.nativeEventFilter(
                            event_type, message)

                widget = _WindowsMessageTarget(self)
                widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
                widget.setWindowTitle('FTHR Background Input')
                widget.resize(1, 1)
                widget.hide()
                self._raw_input_widget = widget
            # winId() forces native handle creation without showing the window.
            native_window = int(self._raw_input_widget.winId())
            if (_WTSAPI32 is not None
                    and not self._session_notifications_registered):
                self._session_notifications_registered = bool(
                    _WTSAPI32.WTSRegisterSessionNotification(
                        ctypes.c_void_p(native_window),
                        NOTIFY_FOR_THIS_SESSION))
                if not self._session_notifications_registered:
                    print('[Hotkey] Session-unlock notification registration '
                          f'failed WinError={ctypes.get_last_error()}')
            return self._raw_input_widget
        except Exception as exc:
            print(f'[Hotkey] Native message target unavailable: {exc}')
            return None

    def _schedule_windows_hotkey_refresh(
            self, reason: str, *, delay_ms: int = 750) -> None:
        """Debounce recovery after resume/unlock while Windows settles devices."""
        if sys.platform != 'win32' or self._input_capture_depth:
            return
        if self._windows_refresh_pending:
            return
        self._windows_refresh_pending = True

        def _refresh():
            self._windows_refresh_pending = False
            if self._input_capture_depth:
                return
            print(f'[Hotkey] Refreshing native registrations after {reason}')
            self._unregister_keyboard_hotkeys()
            self._keyboard_failed = False
            self._windows_failed_actions.clear()
            for action in _HOTKEY_ACTIONS:
                self._register_action(action)

        QTimer.singleShot(max(0, int(delay_ms)), _refresh)

    def _windows_hotkey_watchdog(self) -> None:
        """Repair lost bookkeeping or a recreated hidden HWND.

        RegisterHotKey is owned by Windows rather than a fragile callback hook,
        so it has no separate hook thread to probe. The invariant we can verify
        is that every configured action is attached to the current native HWND.
        """
        if sys.platform != 'win32' or self._input_capture_depth:
            return
        expected = {
            action: self.hotkeys[action]
            for action in _HOTKEY_ACTIONS
            if (self.hotkeys.get(action)
                and action not in self._windows_failed_actions)
        }
        if expected != self._windows_hotkey_combos:
            self._schedule_windows_hotkey_refresh(
                'hotkey watchdog mismatch', delay_ms=0)

    def nativeEventFilter(self, _event_type, message):
        """Dispatch Windows hotkeys and receive optional raw controller input."""
        if sys.platform != 'win32':
            return False, 0
        try:
            native_message = _MSG.from_address(int(message))
        except Exception:
            # Qt may pass a non-address message on a platform plugin we do not own.
            return False, 0
        if native_message.message == WM_HOTKEY:
            hotkey_id = int(native_message.wParam)
            action = self._windows_hotkey_actions.get(hotkey_id)
            if action:
                key = self._windows_hotkey_combos.get(action, '?')
                print(f'[Hotkey] Received native Windows hotkey {key} -> {action}')
                self._emit_action(action, source='keyboard')
        elif native_message.message == WM_POWERBROADCAST:
            if int(native_message.wParam) in (
                    PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self._schedule_windows_hotkey_refresh('system resume')
        elif native_message.message == WM_WTSSESSION_CHANGE:
            if int(native_message.wParam) == WTS_SESSION_UNLOCK:
                self._schedule_windows_hotkey_refresh('session unlock')
        elif (native_message.message == WM_INPUT
              and (self._raw_input_registered
                   or self._raw_keyboard_registered)):
            self._handle_raw_input(native_message.lParam)
        return False, 0

    def _emit_action(self, action: str, *, source: str = 'controller'):
        """Emit an action once when a registered chord is completed."""
        now = time.monotonic()
        if now - getattr(self, '_last_emit_at', {}).get(action, 0.0) < 0.35:
            return
        if not hasattr(self, '_last_emit_at'):
            self._last_emit_at = {}
        self._last_emit_at[action] = now
        signals = {
            'save_clip': self.save_clip_triggered,
            'save_screenshot': self.save_screenshot_triggered,
            'start_recording': self.start_recording_triggered,
            'stop_recording': self.stop_recording_triggered,
            'confirm_game_detection': self.confirm_game_detection_triggered,
            'dismiss_game_detection': self.dismiss_game_detection_triggered,
        }
        signal = signals.get(action)
        if signal:
            print(f'[Hotkey] Dispatching {source} action={action}')
            signal.emit()

    def _has_controller_bindings(self) -> bool:
        return any(self.controller_hotkeys.values())

    def _ensure_controller_polling(self, *, force: bool = False) -> bool:
        if not force and not self._has_controller_bindings():
            self._stop_controller_polling()
            return False
        registered = False
        if self._xinput_get_state is not None and not self._controller_timer.isActive():
            self._controller_timer.start()
            registered = True
        elif self._xinput_get_state is not None:
            registered = True
        return self._ensure_raw_input() or registered

    def _stop_controller_polling(self):
        self._controller_timer.stop()
        self._xinput_controller_buttons.clear()
        self._raw_controller_buttons.clear()
        self._raw_controller_buttons_by_device.clear()
        self._raw_hid_reports.clear()
        if self._controller_buttons:
            self._controller_buttons.clear()
            self.controller_buttons_changed.emit(set())
        self._controller_active_actions.clear()

    def _ensure_raw_input(self) -> bool:
        """Register a hidden native target for generic Windows HID pads."""
        if sys.platform != 'win32' or _USER32 is None:
            return False
        if self._raw_input_registered:
            return True
        try:
            target = self._ensure_windows_message_target()
            if target is None:
                return False
            native_window = int(target.winId())
            devices = (_RAWINPUTDEVICE * len(RAW_GAME_CONTROLLER_USAGES))()
            for index, (usage_page, usage) in enumerate(RAW_GAME_CONTROLLER_USAGES):
                devices[index].usUsagePage = usage_page
                devices[index].usUsage = usage
                devices[index].dwFlags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY
                devices[index].hwndTarget = native_window
            if not _USER32.RegisterRawInputDevices(
                    devices, len(RAW_GAME_CONTROLLER_USAGES),
                    ctypes.sizeof(_RAWINPUTDEVICE)):
                print(f'Raw controller input registration failed, WinError={ctypes.get_last_error()}')
                return False
            self._raw_input_registered = True
            print('Registered raw controller input fallback')
            return True
        except Exception as exc:
            print(f'Raw controller input unavailable: {exc}')
            return False

    def _ensure_raw_keyboard_input(self) -> bool:
        """Register Raw Input only for keys Windows reserves from WM_HOTKEY."""
        if sys.platform != 'win32' or _USER32 is None:
            return False
        if self._raw_keyboard_registered:
            return True
        try:
            target = self._ensure_windows_message_target()
            if target is None:
                return False
            usage_page, usage = RAW_KEYBOARD_USAGE
            device = _RAWINPUTDEVICE()
            device.usUsagePage = usage_page
            device.usUsage = usage
            device.dwFlags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY
            device.hwndTarget = int(target.winId())
            if not _USER32.RegisterRawInputDevices(
                    ctypes.byref(device), 1,
                    ctypes.sizeof(_RAWINPUTDEVICE)):
                print('[Hotkey] Raw keyboard input registration failed '
                      f'WinError={ctypes.get_last_error()}')
                return False
            self._raw_keyboard_registered = True
            print('[Hotkey] Registered Raw Input for reserved Windows keys')
            return True
        except Exception as exc:
            print(f'[Hotkey] Raw keyboard input unavailable: {exc}')
            return False

    def _handle_raw_input(self, raw_handle):
        raw_ptr = ctypes.c_void_p(int(raw_handle) & _POINTER_MASK)
        size = ctypes.c_uint(0)
        header_size = ctypes.sizeof(_RAWINPUTHEADER)
        result = _USER32.GetRawInputData(
            raw_ptr, RID_INPUT, None, ctypes.byref(size), header_size)
        if result == 0xFFFFFFFF or size.value <= header_size:
            return
        buffer = ctypes.create_string_buffer(size.value)
        result = _USER32.GetRawInputData(
            raw_ptr, RID_INPUT, buffer, ctypes.byref(size), header_size)
        if result == 0xFFFFFFFF:
            return
        raw = buffer.raw[:size.value]
        header = _RAWINPUTHEADER.from_buffer_copy(raw[:header_size])
        if header.dwType == RIM_TYPEKEYBOARD:
            keyboard_size = ctypes.sizeof(_RAWKEYBOARD)
            if len(raw) >= header_size + keyboard_size:
                keyboard_data = _RAWKEYBOARD.from_buffer_copy(
                    raw[header_size:header_size + keyboard_size])
                self._process_raw_keyboard_input(keyboard_data)
            return
        if header.dwType != RIM_TYPEHID or len(raw) < header_size + 8:
            return
        report_size = ctypes.c_uint.from_buffer_copy(raw, header_size).value
        report_count = ctypes.c_uint.from_buffer_copy(raw, header_size + 4).value
        if report_size <= 0 or report_count <= 0:
            return
        device_key = self._raw_device_key(header.hDevice)
        data_offset = header_size + 8
        for index in range(report_count):
            start = data_offset + index * report_size
            end = start + report_size
            if end <= len(raw):
                self._process_raw_hid_report(device_key, raw[start:end])

    def _process_raw_keyboard_input(self, keyboard_data: _RAWKEYBOARD) -> None:
        virtual_key = int(keyboard_data.VKey)
        is_break = bool(int(keyboard_data.Flags) & RI_KEY_BREAK)
        modifier = _WINDOWS_MODIFIER_VIRTUAL_KEYS.get(virtual_key)
        if modifier:
            if is_break:
                self._raw_keyboard_modifiers &= ~modifier
            else:
                self._raw_keyboard_modifiers |= modifier
            return
        if is_break:
            self._raw_keyboard_down.discard(virtual_key)
            return
        if virtual_key in self._raw_keyboard_down:
            return
        self._raw_keyboard_down.add(virtual_key)
        for action, (required_modifiers, required_key) in tuple(
                self._windows_raw_hotkey_actions.items()):
            if (required_key == virtual_key
                    and required_modifiers == self._raw_keyboard_modifiers):
                key = self._windows_hotkey_combos.get(action, '?')
                print(f'[Hotkey] Received Windows Raw Input hotkey '
                      f'{key} -> {action}')
                self._emit_action(action, source='keyboard')

    def _raw_device_key(self, device_handle) -> str:
        handle = int(device_handle or 0) & _POINTER_MASK
        if handle in self._raw_device_names:
            return self._raw_device_names[handle]
        device_key = f'DEV_{handle:x}'
        try:
            name_length = ctypes.c_uint(0)
            _USER32.GetRawInputDeviceInfoW(
                ctypes.c_void_p(handle), RIDI_DEVICENAME, None,
                ctypes.byref(name_length))
            if name_length.value:
                name_buffer = ctypes.create_unicode_buffer(name_length.value + 1)
                result = _USER32.GetRawInputDeviceInfoW(
                    ctypes.c_void_p(handle), RIDI_DEVICENAME, name_buffer,
                    ctypes.byref(name_length))
                if result != 0xFFFFFFFF:
                    match = re.search(
                        r'VID_([0-9A-F]{4}).*PID_([0-9A-F]{4})',
                        name_buffer.value.upper())
                    if match:
                        device_key = f'VID_{match.group(1)}&PID_{match.group(2)}'
        except Exception:
            # Device identity is optional; the stable handle key remains usable.
            pass
        self._raw_device_names[handle] = device_key
        return device_key

    @staticmethod
    def _raw_controller_token(device_key: str, byte_index: int, bit_index: int) -> str:
        return f'HID:{device_key}:B{byte_index}.{bit_index}'

    def _process_raw_hid_report(self, device_key: str, report: bytes):
        previous = self._raw_hid_reports.get(device_key)
        self._raw_hid_reports[device_key] = report
        if previous is None:
            return
        active = set(self._raw_controller_buttons_by_device.get(device_key, set()))
        changed = False
        for byte_index in range(max(len(previous), len(report))):
            old_value = previous[byte_index] if byte_index < len(previous) else 0
            new_value = report[byte_index] if byte_index < len(report) else 0
            if old_value == new_value:
                continue
            for bit_index in range(8):
                mask = 1 << bit_index
                token = self._raw_controller_token(device_key, byte_index, bit_index)
                was_down, is_down = bool(old_value & mask), bool(new_value & mask)
                if is_down and not was_down:
                    active.add(token)
                    changed = True
                elif was_down and not is_down and token in active:
                    active.remove(token)
                    changed = True
        if changed:
            self._raw_controller_buttons_by_device[device_key] = active
            self._raw_controller_buttons = set().union(
                *self._raw_controller_buttons_by_device.values())
            self._publish_controller_buttons()

    def _poll_controller_buttons(self):
        if self._xinput_get_state is None:
            return
        pressed = set()
        for user_index in range(4):
            state = _XINPUT_STATE()
            try:
                result = self._xinput_get_state(user_index, ctypes.byref(state))
            except Exception:
                # A controller can disconnect between polling slots; try the rest.
                continue
            if result != 0:
                continue
            buttons = int(state.Gamepad.wButtons)
            for name, flag in _XINPUT_BUTTON_FLAGS.items():
                if buttons & flag:
                    pressed.add(name)
            if state.Gamepad.bLeftTrigger >= _XINPUT_TRIGGER_THRESHOLD:
                pressed.add('LT')
            if state.Gamepad.bRightTrigger >= _XINPUT_TRIGGER_THRESHOLD:
                pressed.add('RT')

        self._xinput_controller_buttons = pressed
        self._publish_controller_buttons()

    def _publish_controller_buttons(self):
        pressed = set(self._xinput_controller_buttons) | set(self._raw_controller_buttons)
        if pressed != self._controller_buttons:
            self._controller_buttons = pressed
            self.controller_buttons_changed.emit(set(pressed))
        matching = {
            action for action, binding in self.controller_hotkeys.items()
            if binding and set(binding.split('+')).issubset(pressed)
        }
        if self._input_capture_depth:
            self._controller_active_actions = matching
            return
        for action in matching - self._controller_active_actions:
            self._emit_action(action)
        self._controller_active_actions = matching

    def _warn_if_linux_hotkeys_dead(self):
        """The keyboard fallback may lack Linux input permissions. Compositor
        socket binds need separate setup, so report when neither path is usable.
        """
        if sys.platform == 'win32' or not getattr(self, '_keyboard_failed', False):
            return
        comp = None
        try:
            from core.compositor import detect_compositor
            comp = detect_compositor()
        except Exception:
            pass
        if comp == 'hyprland':
            return   # binds are auto-written; hotkeys work via the socket
        # Be specific about which desktop this is and what to actually do.
        # A vague warning here is why "hotkeys don't work" was the most common
        # Linux report with no actionable follow-up.
        where = {
            'kwin':  'KDE: System Settings -> Shortcuts -> Add Command',
            'gnome': 'GNOME: Settings -> Keyboard -> Custom Shortcuts',
            'x11':   "your window manager's keybinding config",
        }.get(comp or '', "your desktop's custom-shortcut settings")

        if not linux_tools.available('nc'):
            self.error_occurred.emit(
                'GLOBAL HOTKEYS UNAVAILABLE',
                linux_tools.missing_message('nc') +
                ' Without it, compositor binds cannot reach FTHR Clips.',
                'error',
            )
            return

        self.error_occurred.emit(
            'GLOBAL HOTKEYS NEED MANUAL SETUP',
            'Direct key capture needs root on Linux and is disabled by design. '
            f'Bind a key in {where} to this command:    '
            f'{self.socket_command("save_clip")}',
            'warning',
        )

    # Compositor auto-config (Hyprland)

    @staticmethod
    def _to_hyprland_bind(key: str) -> tuple[str, str]:
        """Convert key syntax to a Hyprland modifier/key pair.

        For example, ctrl+shift+s becomes ('CTRL SHIFT', 'S').
        """
        parts = key.split('+')
        if len(parts) == 1:
            return '', parts[0]
        mods = ' '.join(p.upper() for p in parts[:-1])
        k    = parts[-1].upper() if len(parts[-1]) == 1 else parts[-1]
        return mods, k

    def socket_command(self, action: str) -> str:
        """Build the shell command shared by compositor binds and setup instructions.

        Resolve both the private socket path and the absolute nc executable path.
        """
        nc = linux_tools.path('nc') or 'nc'
        sock = getattr(self, '_socket_path', None)
        if not sock:
            try:
                sock = linux_runtime.hotkey_socket_path(create_dir=False)
            except Exception as exc:
                print(f'[Hotkey] Cannot determine socket path: {exc}')
                sock = '<socket unavailable>'
        return f'echo -n "{action}" | {nc} -U {sock}'

    def setup_instructions(self, compositor: str) -> str:
        """Return compositor instructions built from the real private socket."""
        # Keep this helper usable with side-effect-free stand-ins in diagnostics
        # and documentation tests; socket_command itself remains the canonical
        # command builder for real HotkeyManager instances.
        command_builder = getattr(self, 'socket_command', None)
        if not callable(command_builder):
            command_builder = lambda action: HotkeyManager.socket_command(self, action)
        commands = {
            action: command_builder(action)
            for action in ('save_clip', 'save_screenshot')
        }
        if compositor == 'kwin':
            heading = 'KDE: System Settings → Shortcuts → Custom Shortcuts'
            rendered = commands.values()
        elif compositor == 'gnome':
            heading = 'GNOME: Settings → Keyboard → Custom Shortcuts'
            rendered = (f'bash -c {shlex.quote(command)}'
                        for command in commands.values())
        else:
            heading = 'Bind your preferred keys to these commands:'
            rendered = commands.values()
        return heading + '\n\n' + '\n'.join(rendered)

    def _build_bind_line(self, action: str) -> str:
        key       = self.hotkeys.get(action, '')
        mod, k    = self._to_hyprland_bind(key)
        return f'bind = {mod}, {k}, exec, {self.socket_command(action)}'

    def _write_hyprland_config(self) -> None:
        """Write ~/.config/hypr/fthr-hotkeys.conf with current hotkeys."""
        lines = [
            '# FTHR Clips hotkeys — auto-generated, do not edit manually.',
            '# Change hotkeys inside FTHR Clips → Hotkeys menu.',
            self._build_bind_line('save_clip'),
            self._build_bind_line('save_screenshot'),
            '',
        ]
        tmp = _FTHR_HYPR_CONF.with_suffix('.conf.tmp')
        try:
            _FTHR_HYPR_CONF.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text('\n'.join(lines))
            os.replace(str(tmp), str(_FTHR_HYPR_CONF))
            print(f'[Hotkey] Written {_FTHR_HYPR_CONF}')
        except Exception as e:
            print(f'[Hotkey] Failed to write Hyprland config: {e}')
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_hyprland_lua_config(self, path: Path = _HYPR_CUSTOM_LUA) -> None:
        """Add/update bindings in a Lua-based Hyprland custom keybind file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text() if path.exists() else ''
            if _FTHR_LUA_MARKER in existing:
                existing = existing[:existing.index(_FTHR_LUA_MARKER)].rstrip() + '\n'
            lines = [existing.rstrip(), '', _FTHR_LUA_MARKER]
            for action in ('save_clip', 'save_screenshot'):
                key = self.hotkeys.get(action, '')
                if not key:
                    continue
                combo = ' + '.join(part.upper() for part in key.split('+'))
                command = self.socket_command(action).replace('\\', '\\\\').replace('"', '\\"')
                lines.append(
                    f'hl.bind("{combo}", hl.dsp.exec_cmd("{command}"), '
                    f'{{ description = "FTHR Clips: {action}" }})')
            path.write_text('\n'.join(lines).rstrip() + '\n')
            print(f'[Hotkey] Written Lua bindings to {path}')
        except OSError as e:
            print(f'[Hotkey] Failed to write Lua bindings: {e}')

    def _ensure_hyprland_source(self) -> None:
        """Add a source line to hyprland.conf if it doesn't already exist.

        Skipped if the user already has fthr_hotkey.sock bind lines directly
        in hyprland.conf — we don't want to add a second set of binds.
        """
        if not _HYPR_CONF.exists():
            return
        try:
            content = _HYPR_CONF.read_text()
        except Exception:
            return
        # Already sourced, or user has manual FTHR binds — either way, skip.
        if 'fthr-hotkeys.conf' in content or 'fthr_hotkey.sock' in content:
            return
        source_line = f'\n# FTHR Clips hotkeys (auto-added)\nsource = {_FTHR_HYPR_CONF}\n'
        try:
            with open(_HYPR_CONF, 'a') as f:
                f.write(source_line)
            print(f'[Hotkey] Added source line to {_HYPR_CONF}')
        except Exception as e:
            print(f'[Hotkey] Could not add source line: {e}')

    def _apply_compositor_config(self) -> None:
        """Write compositor config and apply live. Hyprland only for now."""
        if sys.platform == 'win32':
            return
        try:
            from core.compositor import detect_compositor
            comp = detect_compositor()
        except Exception:
            return
        if comp != 'hyprland':
            return
        if _HYPR_CUSTOM_LUA.exists() or _HYPR_LUA_CONF.exists():
            self._write_hyprland_lua_config()
        else:
            self._write_hyprland_config()
            self._ensure_hyprland_source()
        # Apply live without a full reload — one keyword per binding.
        # hyprctl keyword bind accepts the same format as the config file.
        if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE'):
            def _reload():
                try:
                    hyprctl = linux_tools.path('hyprctl')
                    if not hyprctl:
                        raise FileNotFoundError(
                            linux_tools.missing_message('hyprctl'))
                    subprocess.run(
                        [hyprctl, 'reload'],
                        capture_output=True, timeout=5,
                    )
                    print('[Hotkey] Hyprland config reloaded.')
                except Exception as e:
                    print(f'[Hotkey] hyprctl reload failed: {e}')
                    self.error_occurred.emit(
                        'HOTKEY CONFIG ERROR',
                        'Hyprland config reload failed. Re-apply manually in Hotkey settings.',
                        'warning',
                    )
            threading.Thread(target=_reload, daemon=True,
                             name='fthr-hyprctl-apply').start()

    def _start_socket_server(self):
        """Listen on the private hotkey socket for compositor bind commands."""
        # Unix-socket path is Linux-only. Windows uses its native message path;
        # starting this there would raise
        # AttributeError (no AF_UNIX) and flash a bogus error banner.
        if sys.platform == 'win32' or not hasattr(socket, 'AF_UNIX'):
            return

        # One-time migration: a build before AUDIT-003b bound /tmp/fthr_hotkey.sock.
        # Remove it only if it is ours and dead; never touch a squatted path.
        legacy = linux_runtime.cleanup_legacy_socket()
        if legacy:
            print(f'[Hotkey] {legacy}')

        try:
            sock_path = linux_runtime.hotkey_socket_path()
            # Refuses to delete anything that is not a stale socket owned by
            # this user, and refuses to steal a socket a live instance holds.
            linux_runtime.prepare_socket_path(sock_path)
        except linux_runtime.RuntimeDirError as e:
            print(f'[Hotkey] Cannot prepare socket: {e}')
            self.error_occurred.emit(
                'HOTKEY SERVER FAILED',
                f'{e} Hotkeys will not work until this is resolved.',
                'error',
            )
            return
        except OSError as e:
            print(f'[Hotkey] Cannot prepare socket: {e}')
            self.error_occurred.emit(
                'HOTKEY SERVER FAILED',
                'Could not create the private runtime directory. '
                'Hotkeys will not work.',
                'error',
            )
            return

        self._socket_path = sock_path

        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            # Owner-only from the moment the node appears. The directory is
            # already 0700 and user-owned, so this is defence in depth rather
            # than the only barrier — but a chmod *after* bind() would still
            # leave a window, so the umask stays.
            _old_umask = os.umask(0o177)
            try:
                srv.bind(sock_path)
            finally:
                os.umask(_old_umask)
            # Some kernels/filesystems ignore umask for AF_UNIX nodes, so
            # assert the mode explicitly as well.
            try:
                os.chmod(sock_path, 0o600)
            except OSError as e:
                print(f'[Hotkey] Could not tighten socket permissions: {e}')
            srv.listen(8)
            srv.settimeout(1.0)
        except Exception as e:
            print(f"[Hotkey] Socket server failed to start: {e}")
            self.error_occurred.emit(
                'HOTKEY SERVER FAILED',
                'Port in use or permission denied. Hotkeys will not work.',
                'error',
            )
            return

        self._socket_running = True
        print(f"[Hotkey] Socket server listening on {sock_path}")

        _dispatch = {
            'save_clip':               self.save_clip_triggered,
            'save_screenshot':         self.save_screenshot_triggered,
            'start_recording':         self.start_recording_triggered,
            'stop_recording':          self.stop_recording_triggered,
            'confirm_game_detection':  self.confirm_game_detection_triggered,
            'dismiss_game_detection':  self.dismiss_game_detection_triggered,
        }

        def _serve():
            while self._socket_running:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except Exception:
                    break
                with conn:
                    try:
                        # Single recv — all commands fit in 256 bytes.
                        # Do NOT loop until EOF: nc without -N holds the
                        # connection open waiting for a server response,
                        # causing both sides to hang indefinitely.
                        conn.settimeout(0.5)
                        data = conn.recv(256).decode().strip()
                        print(f"[Hotkey] Received: {data!r}")
                        if data in _dispatch:
                            # Direct emit is safe: PySide6 AutoConnection detects
                            # the cross-thread call and queues it to the main
                            # thread automatically. QTimer.singleShot does NOT
                            # work from a plain threading.Thread (no event loop).
                            _dispatch[data].emit()
                        else:
                            print(f"[Hotkey] Unknown command: {data!r}")
                    except Exception as e:
                        print(f"[Hotkey] Socket recv error: {e}")
            srv.close()
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass

        self._socket_thread = threading.Thread(target=_serve, daemon=True, name='fthr-hotkey-socket')
        self._socket_thread.start()

    def _unregister_windows_hotkey(self, action: str) -> None:
        hotkey_id = _WINDOWS_HOTKEY_IDS.get(action)
        actions = getattr(self, '_windows_hotkey_actions', {})
        target = getattr(self, '_raw_input_widget', None)
        if (hotkey_id is not None and hotkey_id in actions
                and _USER32 is not None and target is not None):
            try:
                _USER32.UnregisterHotKey(
                    ctypes.c_void_p(int(target.winId())), hotkey_id)
            except Exception as exc:
                print(f'[Hotkey] Native unregister failed action={action}: {exc}')
        if hotkey_id is not None:
            actions.pop(hotkey_id, None)
        getattr(self, '_windows_raw_hotkey_actions', {}).pop(action, None)
        getattr(self, '_windows_hotkey_combos', {}).pop(action, None)

    def _unregister_keyboard_action(self, action: str, key: str | None) -> None:
        if sys.platform == 'win32':
            self._unregister_windows_hotkey(action)
            return
        registered = getattr(self, '_registered_hotkeys', [])
        if key and key in registered:
            try:
                keyboard.remove_hotkey(key)
                registered.remove(key)
            except Exception as exc:
                print(f'[Hotkey] Fallback unregister failed key={key}: {exc}')

    def _unregister_keyboard_hotkeys(self):
        """Remove only process-wide keyboard registrations, retaining sockets."""
        if sys.platform == 'win32':
            for action in tuple(getattr(
                    self, '_windows_hotkey_combos', {}).keys()):
                self._unregister_windows_hotkey(action)
            self._raw_keyboard_modifiers = 0
            self._raw_keyboard_down.clear()
            return
        for key in getattr(self, '_registered_hotkeys', []):
            try:
                keyboard.remove_hotkey(key)
            except Exception as exc:
                print(f'[Hotkey] Fallback unregister failed key={key}: {exc}')
        self._registered_hotkeys.clear()

    def unregister_all(self):
        """Unregister keyboard and controller hotkeys."""
        self._windows_hotkey_watchdog_timer.stop()
        self._unregister_keyboard_hotkeys()
        self._stop_controller_polling()

    def cleanup(self):
        """Clean up hotkeys on exit"""
        self.unregister_all()
        self._socket_running = False
        if self._socket_thread:
            self._socket_thread.join(timeout=2.0)
        target = getattr(self, '_raw_input_widget', None)
        if (target is not None and _WTSAPI32 is not None
                and self._session_notifications_registered):
            try:
                _WTSAPI32.WTSUnRegisterSessionNotification(
                    ctypes.c_void_p(int(target.winId())))
            except Exception as exc:
                print(f'[Hotkey] Session notification cleanup failed: {exc}')
            self._session_notifications_registered = False
        if target is not None:
            target.close()
            target.deleteLater()
            self._raw_input_widget = None


# The dropdown options in settings. Not exhaustive on purpose — these are the
# combos that (a) don't collide with common game binds and (b) map cleanly to
# platform-global shortcuts. Add more at your own peril.
AVAILABLE_KEYS = [
    'F1', 'F2', 'F3', 'F4', 'F5', 'F6', 'F7', 'F8', 'F9', 'F10', 'F11', 'F12',
    'ctrl+shift+s', 'ctrl+shift+c', 'ctrl+shift+x',
    'alt+s', 'alt+c', 'alt+x',
    'ctrl+alt+s', 'ctrl+alt+c', 'ctrl+alt+x'
]
