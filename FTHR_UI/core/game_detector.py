"""Detect foreground game windows using platform-specific APIs.

Windows filters launchers, browsers, and tools, consults Game Bar, and
requires stable candidates. Linux uses compositor-specific enumeration.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
import ntpath
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal
from core.compositor import detect_compositor
from core import linux_tools


def _enumerate_via_hyprctl() -> list:
    """List windows on Hyprland via hyprctl clients -j."""
    try:
        hyprctl = linux_tools.require('hyprctl')
        r = subprocess.run([hyprctl, 'clients', '-j'],
                           capture_output=True, timeout=2)
        clients = json.loads(r.stdout.decode(errors='replace'))
        windows = []
        for c in clients:
            title = c.get('title') or c.get('class') or ''
            if not title:
                continue
            hwnd    = int(c.get('address', '0x0'), 16) & 0xFFFFFFFF
            is_game = bool(c.get('fullscreen')) or c.get('fullscreenMode', 0) > 0
            windows.append({'hwnd': hwnd, 'display_name': title,
                            'title': title, 'is_game': is_game})
        return windows
    except Exception:
        return []


def _enumerate_via_xdotool() -> list:
    """List visible windows via xdotool + xprop. Works for XWayland and X11.
    Covers Steam/Proton games and most Linux native games."""
    if not linux_tools.available('xdotool'):
        return []
    try:
        r = subprocess.run(
            [linux_tools.require('xdotool'), 'search', '--all',
             '--onlyvisible', '--maxdepth', '2', ''],
            capture_output=True, timeout=3)
        if r.returncode != 0:
            return []
        wids = [w.strip() for w in r.stdout.decode().splitlines() if w.strip()]
    except Exception:
        return []

    windows = []
    for wid in wids[:50]:  # cap to avoid slow scans
        try:
            r_name = subprocess.run([linux_tools.require('xdotool'),
                                     'getwindowname', wid],
                                    capture_output=True, timeout=1)
            title = r_name.stdout.decode().strip()
            if not title:
                continue

            is_game = False
            xprop = linux_tools.path('xprop')
            r_prop = subprocess.run([xprop, '-id', wid, '_NET_WM_STATE'],
                                    capture_output=True, timeout=1)
            if r_prop.returncode == 0:
                is_game = '_NET_WM_STATE_FULLSCREEN' in r_prop.stdout.decode()

            windows.append({
                'hwnd':         int(wid) & 0xFFFFFFFF,
                'display_name': title,
                'title':        title,
                'is_game':      is_game,
            })
        # Window enumeration is an optional Linux integration; fail closed if
        # a compositor returns malformed data or is unavailable mid-poll.
        except Exception:
            continue
    return windows


def _enumerate_linux_windows() -> list:
    """Pick the right backend for the current compositor."""
    comp = detect_compositor()
    if comp == 'hyprland':
        return _enumerate_via_hyprctl()
    return _enumerate_via_xdotool()


class GameDetector(QObject):
    game_appeared = Signal(dict)   # new is_game=True window
    game_closed   = Signal(int)    # hwnd of a game that disappeared
    _windows_enumerated = Signal(list)  # worker thread → main thread

    def __init__(self, enumerate_fn=None, parent=None):
        super().__init__(parent)
        if enumerate_fn is None:
            if sys.platform == 'win32':
                from ui.capture_settings_widget import _enumerate_capturable_windows
                enumerate_fn = _enumerate_capturable_windows
            else:
                enumerate_fn = _enumerate_linux_windows
        self._enumerate = enumerate_fn
        self._known: dict[int, dict] = {}   # hwnd → window dict
        self._poll_running = False          # skip ticks while worker is busy
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        # Enumeration shells out to xdotool/xprop on X11 (dozens of blocking
        # subprocess calls) — never run that on the Qt main thread. The worker
        # thread enumerates; results come back via queued signal.
        self._timer.timeout.connect(self._start_poll)
        self._windows_enumerated.connect(self._apply_windows)

    def start(self):
        self._known.clear()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _start_poll(self):
        if self._poll_running:
            return  # previous enumeration still in flight — don't pile up
        self._poll_running = True

        def _work():
            try:
                windows = self._enumerate()
            except Exception as e:
                print(f'[GameDetector] enumeration failed: {e}')
                windows = []
            # Cross-thread emit — Qt queues this to the main thread.
            self._windows_enumerated.emit(windows)

        threading.Thread(target=_work, daemon=True,
                         name='fthr-game-detect').start()

    def _apply_windows(self, windows: list):
        self._poll_running = False
        self._diff_and_emit({w['hwnd']: w for w in windows if w.get('is_game')})

    def _poll(self):
        """Synchronous poll — used by tests and as a manual refresh."""
        current = {w['hwnd']: w for w in self._enumerate() if w.get('is_game')}
        self._diff_and_emit(current)

    def _diff_and_emit(self, current: dict):
        for hwnd, window in current.items():
            if hwnd not in self._known:
                self._known[hwnd] = window
                self.game_appeared.emit(window)
        for hwnd in list(self._known):
            if hwnd not in current:
                del self._known[hwnd]
                self.game_closed.emit(hwnd)


# Windows foreground game detector (ported from the legacy Windows build)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_CAPTION = 0x00C00000
WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080
MONITOR_DEFAULTTONEAREST = 2


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('rcMonitor', wintypes.RECT),
        ('rcWork', wintypes.RECT),
        ('dwFlags', wintypes.DWORD),
    ]


_USER32 = None
_KERNEL32 = None
if sys.platform == 'win32':
    try:
        _USER32 = ctypes.WinDLL('user32', use_last_error=True)
        _KERNEL32 = ctypes.WinDLL('kernel32', use_last_error=True)

        _USER32.GetForegroundWindow.restype = wintypes.HWND
        _USER32.IsWindow.argtypes = (wintypes.HWND,)
        _USER32.IsWindow.restype = wintypes.BOOL
        _USER32.IsWindowVisible.argtypes = (wintypes.HWND,)
        _USER32.IsWindowVisible.restype = wintypes.BOOL
        _USER32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
        _USER32.GetWindowTextLengthW.restype = ctypes.c_int
        _USER32.GetWindowTextW.argtypes = (
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        _USER32.GetWindowTextW.restype = ctypes.c_int
        _USER32.GetClassNameW.argtypes = (
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        _USER32.GetClassNameW.restype = ctypes.c_int
        _USER32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        _USER32.GetWindowThreadProcessId.restype = wintypes.DWORD
        _USER32.GetWindowRect.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        _USER32.GetWindowRect.restype = wintypes.BOOL
        _USER32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        _USER32.GetWindowLongW.restype = ctypes.c_long
        _USER32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        _USER32.MonitorFromWindow.restype = wintypes.HMONITOR
        _USER32.GetMonitorInfoW.argtypes = (
            wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO))
        _USER32.GetMonitorInfoW.restype = wintypes.BOOL

        _KERNEL32.OpenProcess.argtypes = (
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        _KERNEL32.OpenProcess.restype = wintypes.HANDLE
        _KERNEL32.QueryFullProcessImageNameW.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD))
        _KERNEL32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        _KERNEL32.CloseHandle.argtypes = (wintypes.HANDLE,)
        _KERNEL32.CloseHandle.restype = wintypes.BOOL
    # The Win32 DLLs are optional outside Windows; leave them disabled when
    # their symbols cannot be configured so the cross-platform detector loads.
    except Exception:
        _USER32 = None
        _KERNEL32 = None


@dataclass(frozen=True)
class GameWindow:
    """A foreground window with enough metadata for capture handoff."""

    hwnd: int
    pid: int
    title: str
    exe_path: str = ''
    exe_name: str = ''
    class_name: str = ''
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0
    monitor_width: int = 0
    monitor_height: int = 0
    is_borderless: bool = False
    is_fullscreen: bool = False
    is_known_game: bool = False

    @property
    def identity(self) -> tuple[int, int]:
        return self.pid, self.hwnd

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def display_name(self) -> str:
        return self.title.strip() or Path(self.exe_name).stem or 'Game'

    @property
    def capture_signature(self) -> tuple[int, int, int, int, bool]:
        """Include window and monitor dimensions in the capture signature.

        A fullscreen mode change can alter geometry without changing HWND.
        """
        return (
            self.width,
            self.height,
            max(0, self.monitor_width),
            max(0, self.monitor_height),
            self.is_fullscreen,
        )

    def as_capture_window(self) -> dict:
        """Return the shape consumed by the current source selector."""
        return {
            'hwnd': self.hwnd,
            'pid': self.pid,
            'title': self.title,
            'display_name': self.display_name,
            'exe_path': self.exe_path,
            'exe_name': self.exe_name,
            'is_game': True,
            'icon': None,
        }


_EXCLUDED_EXECUTABLES = {
    'applicationframehost.exe', 'chatgpt.exe', 'cmd.exe', 'code.exe',
    'conhost.exe', 'devenv.exe', 'discord.exe', 'dwm.exe', 'excel.exe',
    'explorer.exe', 'firefox.exe', 'chrome.exe', 'msedge.exe', 'brave.exe',
    'opera.exe', 'vivaldi.exe', 'lockapp.exe', 'logonui.exe', 'mmc.exe',
    'notepad.exe', 'obs32.exe', 'obs64.exe', 'photoshop.exe',
    'powerpnt.exe', 'powershell.exe', 'pwsh.exe', 'python.exe',
    'pythonw.exe', 'searchhost.exe', 'shellexperiencehost.exe',
    'slack.exe', 'spotify.exe', 'startmenuexperiencehost.exe',
    'streamlabs obs.exe', 'systemsettings.exe', 'taskmgr.exe', 'teams.exe',
    'textinputhost.exe', 'vlc.exe', 'windowsterminal.exe', 'winword.exe',
    # Launcher shells are not game targets.
    'battle.net.exe', 'eadesktop.exe', 'epicgameslauncher.exe',
    'galaxyclient.exe', 'riotclientservices.exe', 'steam.exe',
    'ubisoftconnect.exe',
    # Steam helpers can look like borderless/fullscreen windows.
    'steamwebhelper.exe', 'steamservice.exe', 'steamerrorreporter.exe',
    'steam_monitor.exe', 'gameoverlayui.exe',
    # Never allow the capture application to target itself.
    'fthrclips.exe',
}

_EXCLUDED_WINDOW_CLASSES = {
    'applicationframewindow', 'cabinetwclass', 'progman', 'workerw',
    'shell_traywnd', 'windows.ui.core.corewindow', 'chrome_widgetwin_0',
    'chrome_widgetwin_1',
}

# Windows' text-input surface can briefly report incomplete process metadata
# while the shell is starting. Keep its user-facing titles blocked as well as
# its executable/class names so the generic borderless-window heuristic cannot
# promote it to a capture target during that startup race.
_EXCLUDED_WINDOW_TITLE_MARKERS = (
    'text input host',
    'windows input experience',
    'microsoft text input application',
)

_GAME_WINDOW_CLASS_MARKERS = (
    'unitywndclass', 'unrealwindow', 'sdl_app', 'glfw', 'godot',
    'cryengine', 'grcwindow', 'frostbite',
)

_GAME_PATH_MARKERS = (
    '\\steamapps\\common\\', '\\epic games\\', '\\gog games\\',
    '\\gog galaxy\\games\\', '\\xboxgames\\', '\\riot games\\',
    '\\ea games\\', '\\origin games\\',
    '\\ubisoft game launcher\\games\\',
)


def _normalise_executable_path(path: str) -> str:
    value = str(path or '').strip()
    if not value:
        return ''
    return os.path.normcase(os.path.normpath(value)).casefold()


def normalise_crop_profile(profile: object) -> dict[str, object] | None:
    """Validate a normalized crop rectangle stored with a game rule.

    Normalized coordinates make profiles independent of capture resolution and
    keep the result deterministic: the same rule always maps to the same area.
    """
    if not isinstance(profile, dict):
        return None
    try:
        x = float(profile.get('x', 0.0))
        y = float(profile.get('y', 0.0))
        width = float(profile.get('w', 1.0))
        height = float(profile.get('h', 1.0))
    except (TypeError, ValueError):
        # Malformed user-edited crop profiles are disabled, never guessed.
        return None
    values = (x, y, width, height)
    if not all(math.isfinite(value) for value in values):
        return None
    x = min(1.0, max(0.0, x))
    y = min(1.0, max(0.0, y))
    width = min(1.0 - x, max(0.01, width))
    height = min(1.0 - y, max(0.01, height))
    if width <= 0.0 or height <= 0.0:
        return None
    return {
        'enabled': bool(profile.get('enabled', True)),
        'x': round(x, 6),
        'y': round(y, 6),
        'w': round(width, 6),
        'h': round(height, 6),
    }


def normalise_custom_game_rules(rules: object) -> tuple[dict[str, object], ...]:
    """Return safe, de-duplicated manual game and crop rules."""
    if not isinstance(rules, (list, tuple)):
        return ()

    normalised: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw_rule in rules:
        if not isinstance(raw_rule, dict):
            continue
        title = str(raw_rule.get('title_contains', '') or '').strip()
        exe_path = str(raw_rule.get('exe_path', '') or '').strip()
        folder_path = str(raw_rule.get('folder_path', '') or '').strip()
        exe_name = str(raw_rule.get('exe_name', '') or '').strip()
        if not exe_name and exe_path:
            exe_name = ntpath.basename(exe_path) or Path(exe_path).name
        if not title and not exe_path and not folder_path and not exe_name:
            continue

        key = (title.casefold(), _normalise_executable_path(exe_path),
               _normalise_executable_path(folder_path), exe_name.casefold())
        if key in seen:
            continue
        seen.add(key)
        rule: dict[str, object] = {
            'title_contains': title,
            'exe_path': exe_path,
            'folder_path': folder_path,
            'exe_name': exe_name,
        }
        crop_profile = normalise_crop_profile(raw_rule.get('crop_profile'))
        if crop_profile is not None:
            rule['crop_profile'] = crop_profile
        normalised.append(rule)
    return tuple(normalised)


def matching_custom_game_rule(
    window: GameWindow, rules: object,
) -> dict[str, object] | None:
    """Return the first persisted rule matching *window*, if one exists."""
    title = window.title.casefold()
    path = _normalise_executable_path(window.exe_path)
    exe_name = (window.exe_name or Path(window.exe_path).name).casefold()

    for rule in normalise_custom_game_rules(rules):
        rule_title = str(rule['title_contains']).casefold()
        rule_path = _normalise_executable_path(str(rule['exe_path']))
        rule_folder = _normalise_executable_path(str(rule['folder_path']))
        rule_exe_name = str(rule['exe_name']).casefold()
        if rule_title and rule_title not in title:
            continue
        if rule_path and rule_path != path:
            # A Java runtime may move while the title remains the stable identity.
            if not (rule_title and rule_title in title and rule_exe_name and rule_exe_name == exe_name):
                continue
        elif rule_folder:
            folder_prefix = rule_folder.rstrip('/\\') + os.sep
            if path != rule_folder and not path.startswith(folder_prefix):
                continue
        elif not rule_path and rule_exe_name and rule_exe_name != exe_name:
            continue
        return rule
    return None


def is_user_configured_game(window: GameWindow, rules: object) -> bool:
    """Return whether *window* matches a user-created game rule."""
    return matching_custom_game_rule(window, rules) is not None


def crop_profile_for_window(
    window: GameWindow, rules: object,
) -> dict[str, object] | None:
    """Return an enabled, normalized crop profile for the matching game."""
    rule = matching_custom_game_rule(window, rules)
    if rule is None:
        return None
    profile = normalise_crop_profile(rule.get('crop_profile'))
    if profile is None or not profile['enabled']:
        return None
    return profile


def is_probable_game(window: Optional[GameWindow],
                     custom_game_rules: object = ()) -> bool:
    """Apply conservative, deterministic game heuristics to a window."""
    if window is None or not window.hwnd or not window.pid:
        return False
    if window.width < 640 or window.height < 360:
        return False
    if is_user_configured_game(window, custom_game_rules):
        return True

    title = window.title.casefold()
    if any(marker in title for marker in _EXCLUDED_WINDOW_TITLE_MARKERS):
        return False
    if window.exe_name.casefold() in _EXCLUDED_EXECUTABLES:
        return False
    if window.class_name.casefold() in _EXCLUDED_WINDOW_CLASSES:
        return False
    if window.is_known_game:
        return True

    path = window.exe_path.casefold().replace('/', '\\')
    class_name = window.class_name.casefold()
    if any(marker in path for marker in _GAME_PATH_MARKERS):
        return True
    if any(marker in class_name for marker in _GAME_WINDOW_CLASS_MARKERS):
        return True

    # The final borderless/fullscreen fallback is intentionally fail-closed
    # when process metadata is unavailable. A shell/input surface with a
    # transiently missing path must not look like an unknown game just because
    # it happens to cover a monitor.
    if not window.exe_path and not window.exe_name:
        return False

    return bool(
        window.is_borderless
        and (window.is_fullscreen or (window.width >= 960 and window.height >= 540))
    )


def is_capture_window_valid(hwnd: int) -> bool:
    """Return whether *hwnd* still names a real Windows window."""
    if _USER32 is None or not hwnd:
        return False
    try:
        return bool(_USER32.IsWindow(wintypes.HWND(int(hwnd))))
    # A window can disappear between validation and the Win32 API call; an
    # invalid handle simply means capture handoff is no longer possible.
    except Exception:
        return False


def _window_text(hwnd: int) -> str:
    length = int(_USER32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ''
    buffer = ctypes.create_unicode_buffer(length + 1)
    _USER32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def _window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    if _USER32.GetClassNameW(hwnd, buffer, len(buffer)):
        return buffer.value.strip()
    return ''


def _process_path(pid: int) -> str:
    if _KERNEL32 is None or not pid:
        return ''
    handle = _KERNEL32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid))
    if not handle:
        return ''
    try:
        capacity = 32768
        buffer = ctypes.create_unicode_buffer(capacity)
        size = wintypes.DWORD(capacity)
        if _KERNEL32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    # Process metadata is advisory. Access-denied or transient Win32 errors
    # must not prevent the detector from continuing with the window title.
    except Exception:
        pass
    finally:
        _KERNEL32.CloseHandle(handle)
    return ''


_GAME_DATABASE_LOCK = threading.Lock()
_GAME_DATABASE_LOADED_AT = 0.0
_GAME_DATABASE_PATHS: set[str] = set()
_GAME_DATABASE_PARENTS: set[str] = set()
_GAME_DATABASE_TTL_SECONDS = 300.0
_GENERIC_GAME_PARENT_NAMES = {
    'app', 'apps', 'bin', 'binaries', 'common', 'game', 'games',
    'program files', 'program files (x86)', 'shipping', 'win32', 'win64',
    'windowsapps',
}


def _refresh_windows_game_database_if_needed():
    """Cache executables already classified as games by Windows Game Bar."""
    global _GAME_DATABASE_LOADED_AT, _GAME_DATABASE_PATHS, _GAME_DATABASE_PARENTS
    if sys.platform != 'win32':
        return
    now = time.monotonic()
    if now - _GAME_DATABASE_LOADED_AT < _GAME_DATABASE_TTL_SECONDS:
        return

    with _GAME_DATABASE_LOCK:
        now = time.monotonic()
        if now - _GAME_DATABASE_LOADED_AT < _GAME_DATABASE_TTL_SECONDS:
            return
        paths: set[str] = set()
        parents: set[str] = set()
        try:
            import winreg
            root_path = r'System\GameConfigStore\Children'
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, root_path) as root:
                index = 0
                while True:
                    try:
                        child_name = winreg.EnumKey(root, index)
                    # End-of-enumeration is the normal registry API signal.
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(root, child_name) as child:
                            try:
                                matched_path = winreg.QueryValueEx(
                                    child, 'MatchedExeFullPath')[0]
                            # Game Bar entries may omit this optional value.
                            except OSError:
                                matched_path = ''
                            try:
                                parent_name = winreg.QueryValueEx(
                                    child, 'ExeParentDirectory')[0]
                            # Parent metadata is optional and may be absent.
                            except OSError:
                                parent_name = ''
                    # Ignore a single deleted or inaccessible Game Bar entry.
                    except OSError:
                        continue

                    normalised = _normalise_executable_path(matched_path)
                    if normalised:
                        paths.add(normalised)
                    parent_name = str(parent_name or '').strip().casefold()
                    if (len(parent_name) >= 4
                            and parent_name not in _GENERIC_GAME_PARENT_NAMES):
                        parents.add(parent_name)
        # Game Bar's registry cache is optional; missing keys or non-Windows
        # registry support should leave heuristic detection available.
        except (ImportError, OSError):
            pass

        _GAME_DATABASE_PATHS = paths
        _GAME_DATABASE_PARENTS = parents
        _GAME_DATABASE_LOADED_AT = now


def _is_windows_known_game(exe_path: str) -> bool:
    if not exe_path:
        return False
    _refresh_windows_game_database_if_needed()
    if _normalise_executable_path(exe_path) in _GAME_DATABASE_PATHS:
        return True
    try:
        parent_name = Path(exe_path).parent.name.strip().casefold()
    # A malformed executable path should only make this optional cache lookup
    # miss; it is not a reason to stop foreground detection.
    except Exception:
        return False
    return bool(parent_name and parent_name in _GAME_DATABASE_PARENTS)


def _monitor_rect(hwnd: int) -> wintypes.RECT:
    monitor = _USER32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if monitor and _USER32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return info.rcMonitor
    return wintypes.RECT(0, 0, 0, 0)


def get_foreground_window(own_pid: Optional[int] = None) -> Optional[GameWindow]:
    """Read the current foreground window using fast Win32 calls only."""
    if _USER32 is None:
        return None
    try:
        hwnd = int(_USER32.GetForegroundWindow() or 0)
        if not hwnd or not _USER32.IsWindowVisible(hwnd):
            return None
        title = _window_text(hwnd)
        if not title or title.casefold() == 'program manager':
            return None

        pid_value = wintypes.DWORD(0)
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_value))
        pid = int(pid_value.value)
        if not pid or pid == int(own_pid or os.getpid()):
            return None

        ex_style = int(_USER32.GetWindowLongW(hwnd, GWL_EXSTYLE))
        if ex_style & WS_EX_TOOLWINDOW:
            return None
        style = int(_USER32.GetWindowLongW(hwnd, GWL_STYLE)) & 0xFFFFFFFF

        rect = wintypes.RECT()
        if not _USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        monitor = _monitor_rect(hwnd)
        monitor_w = max(0, monitor.right - monitor.left)
        monitor_h = max(0, monitor.bottom - monitor.top)
        window_w = max(0, rect.right - rect.left)
        window_h = max(0, rect.bottom - rect.top)
        covers_monitor = bool(
            monitor_w and monitor_h
            and window_w >= monitor_w * 0.90
            and window_h >= monitor_h * 0.90
        )
        has_caption = bool(style & WS_CAPTION)
        path = _process_path(pid)
        return GameWindow(
            hwnd=hwnd,
            pid=pid,
            title=title,
            exe_path=path,
            exe_name=Path(path).name if path else '',
            class_name=_window_class(hwnd),
            left=int(rect.left), top=int(rect.top),
            right=int(rect.right), bottom=int(rect.bottom),
            monitor_width=monitor_w, monitor_height=monitor_h,
            is_borderless=bool((style & WS_POPUP) or not has_caption),
            is_fullscreen=covers_monitor,
            is_known_game=_is_windows_known_game(path),
        )
    except Exception:
        # Detection is optional convenience and must never destabilise capture.
        return None


WindowProvider = Callable[[Optional[int]], Optional[GameWindow]]


class ForegroundGameDetector(QObject):
    """Poll foreground windows off the UI thread and emit stable games."""

    game_detected = Signal(object)  # GameWindow
    game_lost = Signal()

    def __init__(
        self,
        parent: Optional[QObject] = None,
        *,
        interval_seconds: float = 1.0,
        stable_polls: int = 2,
        window_provider: WindowProvider = get_foreground_window,
        custom_game_rules: object = (),
    ):
        super().__init__(parent)
        self.interval_seconds = max(0.2, float(interval_seconds))
        self.stable_polls = max(1, int(stable_polls))
        self._window_provider = window_provider
        self.available = _USER32 is not None or window_provider is not get_foreground_window
        self._own_pid = os.getpid()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._custom_game_rules = normalise_custom_game_rules(custom_game_rules)
        self._observed_identity: Optional[tuple[int, int]] = None
        self._observed_capture_signature: tuple | None = None
        self._observed_count = 0
        self._emitted_identity: Optional[tuple[int, int]] = None
        self._emitted_capture_signature: tuple | None = None
        self._suppressed_identity: Optional[tuple[int, int]] = None
        self._active_identity: Optional[tuple[int, int]] = None
        self._no_game_count = 0

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive())

    def start(self) -> bool:
        if not self.available:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop_event.clear()
            self._reset_observation_locked()
            self._thread = threading.Thread(
                target=self._run, name='FTHRGameDetection', daemon=True)
            self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.35)
        with self._lock:
            self._thread = None
            self._reset_observation_locked()

    def set_enabled(self, enabled: bool) -> bool:
        if enabled:
            return self.start()
        self.stop()
        return True

    def suppress(self, window: Optional[GameWindow]):
        """Ignore a prompted candidate until it leaves the foreground."""
        with self._lock:
            self._suppressed_identity = window.identity if window else None

    def set_custom_game_rules(self, rules: object):
        with self._lock:
            self._custom_game_rules = normalise_custom_game_rules(rules)

    def _reset_observation_locked(self):
        self._observed_identity = None
        self._observed_capture_signature = None
        self._observed_count = 0
        self._emitted_identity = None
        self._emitted_capture_signature = None
        self._suppressed_identity = None
        self._active_identity = None
        self._no_game_count = 0

    def _run(self):
        while not self._stop_event.is_set():
            try:
                window = self._window_provider(self._own_pid)
                with self._lock:
                    rules = self._custom_game_rules
                if not is_probable_game(window, rules):
                    window = None
                self._observe(window)
            # Detection is a best-effort background service; one bad poll
            # should be treated as game loss and not stop future polls.
            except Exception:
                self._observe(None)
            self._stop_event.wait(self.interval_seconds)

    def _observe(self, window: Optional[GameWindow]):
        emit_window = None
        emit_lost = False
        identity = window.identity if window else None
        capture_signature = window.capture_signature if window else None
        with self._lock:
            if identity is None:
                self._observed_identity = None
                self._observed_capture_signature = None
                self._observed_count = 0
                self._emitted_identity = None
                self._emitted_capture_signature = None
                self._suppressed_identity = None
                if self._active_identity is not None:
                    self._no_game_count += 1
                    if self._no_game_count >= self.stable_polls:
                        self._active_identity = None
                        self._no_game_count = 0
                        emit_lost = True
            else:
                self._no_game_count = 0
                observation_changed = (
                    identity != self._observed_identity
                    or capture_signature != self._observed_capture_signature)
                if observation_changed:
                    previous_identity = self._observed_identity
                    self._observed_identity = identity
                    self._observed_capture_signature = capture_signature
                    self._observed_count = 1
                    if identity != previous_identity:
                        self._emitted_identity = None
                        self._emitted_capture_signature = None
                    if (identity != previous_identity
                            and identity != self._suppressed_identity):
                        self._suppressed_identity = None
                else:
                    self._observed_count += 1

                if (
                    self._observed_count >= self.stable_polls
                    and (
                        identity != self._emitted_identity
                        or capture_signature != self._emitted_capture_signature)
                    and identity != self._suppressed_identity
                ):
                    self._emitted_identity = identity
                    self._emitted_capture_signature = capture_signature
                    self._active_identity = identity
                    emit_window = window

        if emit_window is not None and not self._stop_event.is_set():
            self.game_detected.emit(emit_window)
        if emit_lost and not self._stop_event.is_set():
            self.game_lost.emit()


__all__ = [
    'ForegroundGameDetector',
    'GameDetector',
    'GameWindow',
    'get_foreground_window',
    'is_capture_window_valid',
    'is_probable_game',
    'matching_custom_game_rule',
    'crop_profile_for_window',
    'normalise_crop_profile',
    'normalise_custom_game_rules',
]
