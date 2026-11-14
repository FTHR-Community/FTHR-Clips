"""Local process inventory and reusable application icons.

Paths and PIDs are used only locally to resolve icons. Capture exclusions and
clip manifests use the same path-free executable identity as the native engine.
"""
from __future__ import annotations

from dataclasses import dataclass
import ntpath
import hashlib
from pathlib import Path
import re
import sys
import time

MAX_APPLICATION_AUDIO_EXCLUSIONS = 64
_SAFE_IDENTITY = re.compile(r"^[a-z0-9 ._+()'\-]{1,128}$")
_UNSAFE_IDENTITY = re.compile(r"[^a-z0-9 ._+()'\-]")
_paths: dict[str, str] = {}
_icons: dict[str, object] = {}
_last_scan = 0.0
_NAMES = {'discord': 'Discord', 'discordcanary': 'Discord Canary',
          'chrome': 'Google Chrome', 'msedge': 'Microsoft Edge',
          'firefox': 'Firefox', 'spotify': 'Spotify', 'steam': 'Steam',
          'steamwebhelper': 'Steam WebHelper', 'obs64': 'OBS Studio',
          'brave': 'Brave', 'vlc': 'VLC'}


def executable_identity(basename: str) -> str:
    """Match NormalizeWindowsExecutableIdentity (including its safe alphabet)."""
    name = ntpath.basename(str(basename)).lower()
    if name.endswith('.exe'):
        name = name[:-4]
    return ' '.join(_UNSAFE_IDENTITY.sub('', name).split())[:128].strip()


def normalize_application_audio_exclusions(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in value:
        if (not isinstance(item, str) or any(c in item for c in '/\\:|;')
                or any(ord(c) < 32 or ord(c) == 127 for c in item)):
            continue
        identity = item.strip().lower()
        if identity.endswith('.exe'):
            identity = identity[:-4]
        if not _SAFE_IDENTITY.fullmatch(identity) or identity in result:
            continue
        result.append(identity)
        if len(result) == MAX_APPLICATION_AUDIO_EXCLUSIONS:
            break
    return result


def encode_application_audio_exclusions(value: object) -> str:
    return '|'.join(normalize_application_audio_exclusions(value))


@dataclass(frozen=True)
class RunningApp:
    identity: str
    name: str
    executable_path: str
    pid: int


def list_running_apps() -> list[RunningApp]:
    """One row per readable executable, including apps not currently audible."""
    global _last_scan
    apps: dict[str, RunningApp] = {}
    for pid, basename, path in _process_inventory():
        try:
            identity = executable_identity(basename)
            if not identity or not path:
                continue
            name = _NAMES.get(identity, ntpath.splitext(basename)[0])
            apps.setdefault(identity, RunningApp(identity, name, path, pid))
        except OSError:
            continue
    for identity, app in apps.items():
        if _paths.get(identity) != app.executable_path:
            _icons.pop(identity, None)
        _paths[identity] = app.executable_path
    _last_scan = time.monotonic()
    return sorted(apps.values(), key=lambda app: (app.name.casefold(), app.identity))


def _process_inventory():
    """Read Win32 process metadata without optional Python dependencies."""
    if sys.platform != 'win32':
        return
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for name in ('Process32FirstW', 'Process32NextW'):
        function = getattr(kernel, name)
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        function.restype = wintypes.BOOL
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, ctypes.c_void_p(-1).value):
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while present:
            process = kernel.OpenProcess(0x1000, False, entry.th32ProcessID)
            if process:
                try:
                    path = ctypes.create_unicode_buffer(32768)
                    length = wintypes.DWORD(len(path))
                    if kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)):
                        yield int(entry.th32ProcessID), entry.szExeFile, path.value
                finally:
                    kernel.CloseHandle(process)
            present = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)


def _icon_cache_path(identity: str) -> Path:
    # Hash even normalized identities: Windows reserves names such as CON.
    key = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    return Path.home() / '.fthr' / 'app-icons' / (key + '.png')


def application_icon(identity: str, size: int = 20, *, fallback: bool = True):
    """Return a local app icon, optionally falling back to a generic file icon.

    Call on the Qt GUI thread. A path never comes from clip metadata; unknown
    identities cannot make this function load an arbitrary file.
    """
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QStyle

    del size  # QIcon is resolution independent; consumers select their size.
    identity = executable_identity(identity)
    if not identity:
        return QIcon()
    if time.monotonic() - _last_scan > 5:
        list_running_apps()
    if identity in _icons:
        return QIcon(_icons[identity])
    app = QApplication.instance()
    if app is None:
        return QIcon()
    path = _paths.get(identity)
    icon = _windows_executable_icon(path) if path and sys.platform == 'win32' else QIcon()
    cache_path = _icon_cache_path(identity)
    if not icon.isNull():
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            icon.pixmap(64, 64).save(str(cache_path), 'PNG')
        except OSError:
            pass  # Icon persistence must never prevent opening the editor.
    elif cache_path.is_file():
        icon = QIcon(str(cache_path))
    # Shell file providers can return generic EXE/PC icons as successful
    # results. Only cache actual extracted resources or our saved bitmaps.
    # Do not cache misses: a later scan may discover the app after launch.
    if not icon.isNull() and not icon.pixmap(24, 24).isNull():
        _icons[identity] = icon
        return QIcon(icon)
    return (app.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
            if fallback else QIcon())


def _windows_executable_icon(path):
    import ctypes
    from ctypes import wintypes
    from PySide6.QtGui import QIcon, QImage, QPixmap
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    user = ctypes.WinDLL('user32', use_last_error=True)
    shell.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON), wintypes.UINT]
    shell.ExtractIconExW.restype = wintypes.UINT
    user.DestroyIcon.argtypes = [wintypes.HICON]
    user.DestroyIcon.restype = wintypes.BOOL
    large, small = wintypes.HICON(), wintypes.HICON()
    try:
        shell.ExtractIconExW(path, 0, ctypes.byref(large), ctypes.byref(small), 1)
        handle = large.value or small.value
        return QIcon(QPixmap.fromImage(QImage.fromHICON(handle))) if handle else QIcon()
    except (OSError, AttributeError, TypeError):
        return QIcon()
    finally:
        if large.value:
            user.DestroyIcon(large)
        if small.value:
            user.DestroyIcon(small)
