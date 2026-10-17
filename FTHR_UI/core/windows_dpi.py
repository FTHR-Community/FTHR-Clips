"""Small, read-only Windows DPI queries used at native UI boundaries.

Qt owns process DPI configuration.  This module deliberately does not call
any of the process-awareness setters; it only reports the effective context
and per-window DPI after a QApplication/window exists.
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any


BASE_DPI = 96
PER_MONITOR_AWARE = -3
PER_MONITOR_AWARE_V2 = -4


def _hwnd(value: Any) -> int:
    if isinstance(value, int):
        return int(value)
    try:
        return int(value.winId())
    except (AttributeError, TypeError, ValueError, RuntimeError):
        # An unrealized or already-destroyed Qt window has no usable HWND.
        return 0


def dpi_for_window(window_or_hwnd: Any) -> int:
    """Return effective per-window DPI, or the 96-DPI baseline off Windows."""
    hwnd = _hwnd(window_or_hwnd)
    if not hwnd:
        return BASE_DPI
    try:
        user32 = ctypes.windll.user32
        get_dpi = user32.GetDpiForWindow
        get_dpi.argtypes = [ctypes.c_void_p]
        get_dpi.restype = ctypes.c_uint
        value = int(get_dpi(ctypes.c_void_p(hwnd)))
        return value if value > 0 else BASE_DPI
    except (AttributeError, OSError, TypeError, ValueError):
        return BASE_DPI


def _context_value(context: Any) -> int:
    try:
        return ctypes.c_ssize_t(context).value
    except (TypeError, ValueError):
        try:
            return int(context)
        except (TypeError, ValueError):
            # An unavailable native context is represented explicitly as 0.
            return 0


def _awareness_name(value: int) -> str:
    return {
        0: 'Unaware',
        1: 'System',
        2: 'PerMonitor',
    }.get(int(value), 'Unknown')


def dpi_awareness_snapshot(window: Any = None) -> dict[str, Any]:
    """Return a bounded effective DPI-awareness snapshot for diagnostics."""
    result: dict[str, Any] = {
        'platform': 'windows' if sys.platform == 'win32' else sys.platform,
        'thread_context': 'unavailable:not_windows',
        'thread_awareness': 'unavailable:not_windows',
        'process_awareness': 'unavailable:not_windows',
        'per_monitor_v2': False,
        'per_monitor': False,
        'window_dpi': BASE_DPI,
        'awareness_context_comparison': 'unavailable:not_windows',
    }
    try:
        user32 = ctypes.windll.user32
        get_thread = user32.GetThreadDpiAwarenessContext
        get_thread.restype = ctypes.c_void_p
        context = get_thread()
        raw_context = _context_value(context)
        result['thread_context'] = raw_context

        get_awareness = getattr(user32, 'GetAwarenessFromDpiAwarenessContext', None)
        awareness = None
        if get_awareness is not None:
            get_awareness.argtypes = [ctypes.c_void_p]
            get_awareness.restype = ctypes.c_int
            awareness = int(get_awareness(ctypes.c_void_p(raw_context)))
            result['thread_awareness'] = _awareness_name(awareness)

        try:
            shcore = ctypes.windll.shcore
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            process_awareness = ctypes.c_int(-1)
            shcore.GetProcessDpiAwareness.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
            shcore.GetProcessDpiAwareness.restype = ctypes.c_long
            status = int(shcore.GetProcessDpiAwareness(
                kernel32.GetCurrentProcess(),
                ctypes.byref(process_awareness)))
            result['process_awareness'] = (
                _awareness_name(process_awareness.value)
                if status == 0 else f'unavailable:hresult_0x{status & 0xFFFFFFFF:08X}')
        except (AttributeError, OSError, TypeError, ValueError):
            # Thread context remains the authoritative PMv2 discriminator on
            # systems where the older SHCore process query is unavailable.
            result['process_awareness'] = 'unavailable:api_missing'

        # Context equality is preferable to inferring PMv2 from a numeric
        # value, and is available on supported Windows 10/11 builds.
        equal = getattr(user32, 'AreDpiAwarenessContextsEqual', None)
        if equal is not None:
            equal.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            equal.restype = ctypes.c_int
            pm = bool(equal(ctypes.c_void_p(raw_context),
                            ctypes.c_void_p(PER_MONITOR_AWARE)))
            pmv2 = bool(equal(ctypes.c_void_p(raw_context),
                              ctypes.c_void_p(PER_MONITOR_AWARE_V2)))
            result['per_monitor'] = pm or pmv2
            result['per_monitor_v2'] = pmv2
            result['awareness_context_comparison'] = 'available'
        elif awareness is not None:
            result['per_monitor'] = awareness == 2
            result['awareness_context_comparison'] = 'unavailable:api_missing'
        if window is not None:
            result['window_dpi'] = dpi_for_window(window)
    except (AttributeError, OSError, TypeError, ValueError):
        result['thread_context'] = 'unavailable:query_failed'
        result['thread_awareness'] = 'unavailable:query_failed'
    return result


__all__ = [
    'BASE_DPI', 'PER_MONITOR_AWARE', 'PER_MONITOR_AWARE_V2',
    'dpi_for_window', 'dpi_awareness_snapshot',
]
