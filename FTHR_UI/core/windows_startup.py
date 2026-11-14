"""Small Windows startup policy for the frozen application.

The policy is deliberately independent of Qt, settings, profiles, and the
Windows registry.  It only asks the current process for the extension-point
mitigation state and enables the documented bit when the platform exposes the
API.  A failure is diagnostic information; it must never prevent FTHR from
starting.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass


_PROCESS_EXTENSION_POINT_DISABLE_POLICY = 6
_EXTENSION_POINT_DISABLE = 0x00000001
_LEGACY_HOOKS_ENV = "FTHR_ALLOW_LEGACY_HOOKS"


@dataclass(frozen=True, slots=True)
class StartupPolicyResult:
    """Immutable, non-blocking result of the process startup policy."""

    status: str
    error_code: str | None = None


def _error_code(operation: str) -> str:
    """Return a stable diagnostic without raising from startup handling."""
    code = ctypes.get_last_error()
    return f"{operation}:winerror={code}"


def _load_kernel32():
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_policy = kernel32.GetProcessMitigationPolicy
    set_policy = kernel32.SetProcessMitigationPolicy
    get_current_process = kernel32.GetCurrentProcess

    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    get_policy.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_size_t,
    ]
    get_policy.restype = wintypes.BOOL
    set_policy.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_size_t,
    ]
    set_policy.restype = wintypes.BOOL
    return get_current_process, get_policy, set_policy


def _read_policy(get_current_process, get_policy):
    flags = wintypes.DWORD(0)
    try:
        succeeded = get_policy(
            get_current_process(),
            _PROCESS_EXTENSION_POINT_DISABLE_POLICY,
            ctypes.byref(flags),
            ctypes.sizeof(flags),
        )
    except (AttributeError, OSError) as error:
        return None, f"{type(error).__name__}: {error}"
    if not succeeded:
        return None, _error_code("GetProcessMitigationPolicy")
    return flags, None


def apply_startup_policy() -> StartupPolicyResult:
    """Apply the process-only extension-point policy when supported.

    The call is idempotent.  Existing mitigation bits are read before a Set
    call and are always preserved.  ``FTHR_ALLOW_LEGACY_HOOKS=1`` suppresses
    only the new Set call; it cannot clear an already active OS policy.
    """
    if sys.platform != "win32":
        return StartupPolicyResult("not_windows")

    try:
        get_current_process, get_policy, set_policy = _load_kernel32()
    except (AttributeError, OSError) as error:
        return StartupPolicyResult("unavailable", f"{type(error).__name__}: {error}")

    current, read_error = _read_policy(get_current_process, get_policy)
    if current is None:
        return StartupPolicyResult("read_failed", read_error)
    if current.value & _EXTENSION_POINT_DISABLE:
        return StartupPolicyResult("already_enabled")
    if os.environ.get(_LEGACY_HOOKS_ENV) == "1":
        return StartupPolicyResult("skipped_legacy_hooks")

    requested = wintypes.DWORD(current.value | _EXTENSION_POINT_DISABLE)
    try:
        if not set_policy(
            _PROCESS_EXTENSION_POINT_DISABLE_POLICY,
            ctypes.byref(requested),
            ctypes.sizeof(requested),
        ):
            return StartupPolicyResult("set_failed", _error_code("SetProcessMitigationPolicy"))
    except (AttributeError, OSError) as error:
        return StartupPolicyResult("set_failed", f"{type(error).__name__}: {error}")

    readback, readback_error = _read_policy(get_current_process, get_policy)
    if readback is None:
        return StartupPolicyResult("readback_failed", readback_error)
    if not readback.value & _EXTENSION_POINT_DISABLE:
        return StartupPolicyResult("readback_disabled", "ExtensionPointDisablePolicy bit not set")
    return StartupPolicyResult("enabled")
