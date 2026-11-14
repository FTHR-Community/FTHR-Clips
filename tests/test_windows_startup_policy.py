"""Isolated contract checks for the non-blocking Windows startup mitigation."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import windows_startup


POLICY_ID = 6
EXTENSION_POINT_DISABLE = 0x1
DWORD_SIZE = ctypes.sizeof(windows_startup.wintypes.DWORD)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _MitigationApi:
    """In-memory WinAPI stand-in; never changes the host process policy."""

    def __init__(self, flags=0):
        self.flags = flags
        self.get_count = 0
        self.set_count = 0
        self.get_calls = []
        self.set_calls = []
        self.fail_get_calls = set()
        self.accept_set = True
        self.apply_set = True

    @staticmethod
    def _read_dword(pointer):
        return ctypes.cast(pointer, ctypes.POINTER(windows_startup.wintypes.DWORD)).contents.value

    @staticmethod
    def _write_dword(pointer, value):
        ctypes.cast(pointer, ctypes.POINTER(windows_startup.wintypes.DWORD)).contents.value = value

    def get_current_process(self):
        return 123

    def get_process_mitigation_policy(self, process, policy, output, size):
        self.get_count += 1
        self.get_calls.append((process, policy, size))
        if self.get_count in self.fail_get_calls:
            return False
        self._write_dword(output, self.flags)
        return True

    def set_process_mitigation_policy(self, policy, value, size):
        self.set_count += 1
        requested = self._read_dword(value)
        self.set_calls.append((policy, requested, size))
        if self.accept_set and self.apply_set:
            self.flags = requested
        return self.accept_set

    def install(self, monkeypatch):
        monkeypatch.setattr(windows_startup.sys, "platform", "win32")
        monkeypatch.setattr(
            windows_startup, "_load_kernel32",
            lambda: (self.get_current_process,
                     self.get_process_mitigation_policy,
                     self.set_process_mitigation_policy))


def _isolated_child_env(tmp_path: Path, *, qt_platform="offscreen") -> dict[str, str]:
    profile = tmp_path / "profile"
    appdata = profile / "AppData" / "Roaming"
    local = profile / "AppData" / "Local"
    temporary = tmp_path / "temp"
    cache = tmp_path / "pycache"
    for directory in (profile, appdata, local, temporary, cache):
        directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key, value in {
        "HOME": str(profile), "USERPROFILE": str(profile),
        "APPDATA": str(appdata), "LOCALAPPDATA": str(local),
        "TEMP": str(temporary), "TMP": str(temporary), "TMPDIR": str(temporary),
        "PYTHONPYCACHEPREFIX": str(cache), "PYTHONDONTWRITEBYTECODE": "1",
        "QT_QPA_PLATFORM": qt_platform,
    }.items():
        env[key] = value
    if os.name == "nt":
        env["HOMEDRIVE"], env["HOMEPATH"] = os.path.splitdrive(str(profile))
    pythonpath = [str(PROJECT_ROOT / "FTHR_UI"), str(PROJECT_ROOT / "tools")]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env.pop("FTHR_ALLOW_LEGACY_HOOKS", None)
    return env


def test_importing_policy_module_and_ordinary_main_module_has_no_policy_effect(
        tmp_path):
    script = r"""
import core.windows_startup as startup
calls = []
startup._load_kernel32 = lambda: calls.append('loaded')
import main
assert calls == [], calls
assert main._STARTUP_POLICY_RESULT is None
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=PROJECT_ROOT / "FTHR_UI",
        env=_isolated_child_env(tmp_path), text=True, capture_output=True,
        timeout=45, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_non_windows_is_a_side_effect_free_noop(monkeypatch):
    monkeypatch.setattr(windows_startup.sys, "platform", "linux")
    monkeypatch.setattr(
        windows_startup, "_load_kernel32",
        lambda: pytest.fail("non-Windows startup must not load kernel32"))

    result = windows_startup.apply_startup_policy()

    assert result.status == "not_windows"
    assert result.error_code is None


def test_policy_sets_only_the_requested_bit_with_native_dword_size(monkeypatch):
    api = _MitigationApi(flags=0x20)
    api.install(monkeypatch)

    result = windows_startup.apply_startup_policy()

    assert result.status == "enabled"
    assert result.error_code is None
    assert api.flags == 0x20 | EXTENSION_POINT_DISABLE
    assert api.get_calls == [(123, POLICY_ID, 4), (123, POLICY_ID, 4)]
    assert api.set_calls == [(POLICY_ID, 0x20 | EXTENSION_POINT_DISABLE, 4)]
    assert DWORD_SIZE == 4


def test_existing_policy_is_never_weakened_and_application_is_idempotent(monkeypatch):
    api = _MitigationApi(flags=0x41)
    api.install(monkeypatch)

    first = windows_startup.apply_startup_policy()
    second = windows_startup.apply_startup_policy()

    assert first.status == second.status == "already_enabled"
    assert api.flags == 0x41
    assert api.set_count == 0


def test_legacy_hook_optout_skips_only_set_and_never_clears_existing_bit(
        monkeypatch):
    api = _MitigationApi(flags=0x4)
    api.install(monkeypatch)
    monkeypatch.setenv("FTHR_ALLOW_LEGACY_HOOKS", "1")

    skipped = windows_startup.apply_startup_policy()
    assert skipped.status == "skipped_legacy_hooks"
    assert skipped.error_code is None
    assert api.flags == 0x4
    assert api.set_count == 0

    api.flags = 0x5
    existing = windows_startup.apply_startup_policy()
    assert existing.status == "already_enabled"
    assert api.flags == 0x5
    assert api.set_count == 0


def test_missing_native_api_and_initial_read_failure_are_diagnostic_nonfatal_results(
        monkeypatch):
    monkeypatch.setattr(windows_startup.sys, "platform", "win32")
    monkeypatch.setattr(windows_startup, "_load_kernel32",
                        lambda: (_ for _ in ()).throw(OSError("API unavailable")))
    unavailable = windows_startup.apply_startup_policy()
    assert unavailable.status == "unavailable"
    assert unavailable.error_code

    api = _MitigationApi()
    api.fail_get_calls.add(1)
    api.install(monkeypatch)
    read_failed = windows_startup.apply_startup_policy()
    assert read_failed.status == "read_failed"
    assert read_failed.error_code
    assert api.set_count == 0


def test_set_and_readback_failures_remain_nonfatal_and_never_claim_enabled(
        monkeypatch):
    failed_set = _MitigationApi()
    failed_set.accept_set = False
    failed_set.install(monkeypatch)
    result = windows_startup.apply_startup_policy()
    assert result.status == "set_failed"
    assert result.error_code
    assert failed_set.set_count == 1

    failed_readback = _MitigationApi()
    failed_readback.fail_get_calls.add(2)
    failed_readback.install(monkeypatch)
    result = windows_startup.apply_startup_policy()
    assert result.status == "readback_failed"
    assert result.error_code

    false_readback = _MitigationApi()
    false_readback.apply_set = False
    false_readback.install(monkeypatch)
    result = windows_startup.apply_startup_policy()
    assert result.status == "readback_disabled"
    assert result.error_code


def test_result_is_immutable():
    result = windows_startup.StartupPolicyResult("not_windows")
    with pytest.raises((AttributeError, TypeError)):
        result.status = "enabled"


@pytest.mark.skipif(sys.platform != "win32", reason="requires real Windows mitigation readback")
@pytest.mark.parametrize("entry", ["main", "card", "runtime_hook"])
def test_entry_policy_is_active_at_first_qt_import_boundary(entry, tmp_path):
    script = r"""
import ctypes
import runpy
import sys
from ctypes import wintypes
from pathlib import Path

root = Path(sys.argv[1])
entry = sys.argv[2]
sys.path.insert(0, str(root / 'FTHR_UI'))
sys.path.insert(0, str(root / 'tools'))

def assert_policy_active():
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    get_current = kernel32.GetCurrentProcess
    get_current.argtypes = []
    get_current.restype = wintypes.HANDLE
    get_policy = kernel32.GetProcessMitigationPolicy
    get_policy.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_size_t]
    get_policy.restype = wintypes.BOOL
    flags = wintypes.DWORD(0)
    assert get_policy(get_current(), 6, ctypes.byref(flags), ctypes.sizeof(flags))
    assert ctypes.sizeof(flags) == 4 and flags.value & 1, hex(flags.value)

class FirstQtImport(BaseException):
    pass

class QtBoundary:
    checked = False
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6' or fullname.startswith('PySide6.'):
            assert_policy_active()
            self.checked = True
            raise FirstQtImport(fullname)

sentinel = QtBoundary()
sys.meta_path.insert(0, sentinel)
try:
    if entry == 'main':
        runpy.run_path(str(root / 'FTHR_UI' / 'main.py'), run_name='__main__')
    elif entry == 'card':
        runpy.run_path(str(root / 'FTHR_UI' / 'ui' / 'capture_card_process.py'),
                       run_name='__main__')
    elif entry == 'runtime_hook':
        runpy.run_path(str(root / 'tools' / 'pyi_rth_windows_startup.py'),
                       run_name='__main__')
        import PySide6.QtCore
    else:
        raise AssertionError(entry)
except FirstQtImport:
    pass
else:
    raise AssertionError('expected controlled abort at first Qt import')
assert sentinel.checked, 'no Qt import boundary was reached'
assert not any(name == 'PySide6' or name.startswith('PySide6.') for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(PROJECT_ROOT), entry],
        cwd=PROJECT_ROOT, env=_isolated_child_env(tmp_path),
        text=True, capture_output=True, timeout=60, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows Raw Input registration")
def test_real_raw_input_registration_works_with_policy_and_hidden_target(tmp_path):
    script = r"""
from core.windows_startup import apply_startup_policy
result = apply_startup_policy()
assert result.status in {'enabled', 'already_enabled'}, (result.status, result.error_code)
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
from core.hotkey_manager import HotkeyManager
manager = HotkeyManager()
try:
    assert manager._register_windows_hotkey('save_clip', 'Ctrl+Alt+Shift+F24')
    target = manager._raw_input_widget
    assert target is not None and target.isHidden()
    assert int(target.winId()) > 0
    assert manager._raw_keyboard_registered
    assert 'save_clip' in manager._windows_raw_hotkey_actions
    manager.unregister_all()
finally:
    manager.cleanup()
    app.processEvents()
assert not manager._raw_keyboard_registered
assert manager._raw_input_widget is None
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=PROJECT_ROOT / "FTHR_UI",
        env=_isolated_child_env(tmp_path, qt_platform="windows"),
        text=True, capture_output=True, timeout=45, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="requires the real per-process Windows API")
def test_real_child_applies_policy_before_qtcore_and_widget_creation(tmp_path):
    script = r"""
import ctypes
from ctypes import wintypes
from core.windows_startup import apply_startup_policy
result = apply_startup_policy()
assert result.status in {'enabled', 'already_enabled'}, (result.status, result.error_code)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
get_current = kernel32.GetCurrentProcess
get_current.argtypes = []
get_current.restype = wintypes.HANDLE
get_policy = kernel32.GetProcessMitigationPolicy
get_policy.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_size_t]
get_policy.restype = wintypes.BOOL
flags = wintypes.DWORD(0)
assert get_policy(get_current(), 6, ctypes.byref(flags), ctypes.sizeof(flags))
assert ctypes.sizeof(flags) == 4 and flags.value & 1
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication, QWidget
app = QApplication.instance() or QApplication([])
widget = QWidget()
assert widget is not None and QCoreApplication.instance() is app
widget.close()
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=PROJECT_ROOT / "FTHR_UI",
        env=_isolated_child_env(tmp_path), text=True, capture_output=True,
        timeout=60, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
