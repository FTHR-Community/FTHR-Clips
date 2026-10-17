"""Shared native Windows behavior for FTHR's frameless Qt windows."""
from __future__ import annotations

import ctypes
from typing import Any

from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QSlider,
    QWidget,
)

from core.windows_dpi import BASE_DPI, dpi_for_window


HT_LEFT, HT_RIGHT, HT_TOP, HT_TOPLEFT, HT_TOPRIGHT = 10, 11, 12, 13, 14
HT_BOTTOM, HT_BOTTOMLEFT, HT_BOTTOMRIGHT, HT_CAPTION = 15, 16, 17, 2
HT_CLIENT = 1


def start_system_move(window: Any) -> bool:
    """Start a compositor/native move, returning False for manual fallback."""
    try:
        return bool(window.startSystemMove()) if window is not None else False
    except (AttributeError, RuntimeError, TypeError):
        # Refusal is expected for unrealized/destroyed windows; the caller
        # retains its Qt-logical manual-drag fallback.
        return False


def decode_nchittest_point(lparam: int) -> tuple[int, int]:
    """Decode signed 16-bit screen coordinates from WM_NCHITTEST lParam."""
    value = int(lparam) & 0xFFFFFFFFFFFFFFFF
    x = (value & 0xFFFF)
    y = ((value >> 16) & 0xFFFF)
    if x & 0x8000:
        x -= 0x10000
    if y & 0x8000:
        y -= 0x10000
    return x, y


def get_window_rect(window: Any) -> QRect | None:
    """Read the physical Win32 window rectangle, including signed origins."""
    try:
        hwnd = int(window.winId())
        class RECT(ctypes.Structure):
            _fields_ = [('left', ctypes.c_long), ('top', ctypes.c_long),
                        ('right', ctypes.c_long), ('bottom', ctypes.c_long)]
        native = RECT()
        user32 = ctypes.windll.user32
        ok = user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(native))
        if not ok:
            return None
        return QRect(int(native.left), int(native.top),
                     int(native.right - native.left),
                     int(native.bottom - native.top))
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        # Native geometry can disappear during monitor or window teardown;
        # returning None delegates hit testing to Qt's default WndProc.
        return None


def screen_to_client_physical(
        window: Any, screen_x: int, screen_y: int) -> QPoint | None:
    """Convert Win32 physical screen pixels to physical client pixels."""
    try:
        hwnd = int(window.winId())

        class POINT(ctypes.Structure):
            _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]

        point = POINT(int(screen_x), int(screen_y))
        user32 = ctypes.windll.user32
        user32.ScreenToClient.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(POINT)]
        user32.ScreenToClient.restype = ctypes.c_int
        if not user32.ScreenToClient(
                ctypes.c_void_p(hwnd), ctypes.byref(point)):
            return None
        return QPoint(int(point.x), int(point.y))
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        # A failed native coordinate conversion must not guess a hit region.
        return None


def physical_client_to_logical(client_x: int, client_y: int, dpi: int,
                               *, device_pixel_ratio: float | None = None) -> QPoint:
    """Convert physical client pixels once, using Qt's effective scale if given."""
    ratio = (device_pixel_ratio if device_pixel_ratio is not None
             else max(1, int(dpi)) / BASE_DPI)
    scale = 1.0 / max(0.01, float(ratio))
    return QPoint(round(int(client_x) * scale),
                  round(int(client_y) * scale))


def classify_native_hit(
        window_rect: QRect,
        screen_x: int,
        screen_y: int,
        *,
        client_y: int,
        titlebar_height_physical: int,
        resize_border_physical: int,
        maximized: bool,
        interactive: bool,
) -> int | None:
    """Classify a physical WM_NCHITTEST point without consulting Qt state."""
    x, y = int(screen_x), int(screen_y)
    if not window_rect.contains(x, y):
        return None
    if interactive:
        return HT_CLIENT
    left_edge = int(window_rect.left())
    top_edge = int(window_rect.top())
    right_edge = left_edge + int(window_rect.width())
    bottom_edge = top_edge + int(window_rect.height())
    border = max(1, int(resize_border_physical))
    left = x < left_edge + border
    right = x >= right_edge - border
    top = y < top_edge + border
    bottom = y >= bottom_edge - border
    if not maximized:
        if top and left:
            return HT_TOPLEFT
        if top and right:
            return HT_TOPRIGHT
        if bottom and left:
            return HT_BOTTOMLEFT
        if bottom and right:
            return HT_BOTTOMRIGHT
        if top:
            return HT_TOP
        if bottom:
            return HT_BOTTOM
        if left:
            return HT_LEFT
        if right:
            return HT_RIGHT
    if (0 <= int(client_y) < int(titlebar_height_physical)
            and not interactive):
        return HT_CAPTION
    # Own the entire frameless hit region. DefWindowProc's thick-frame
    # fallback can classify custom controls as non-client window chrome.
    return HT_CLIENT


def _is_interactive(widget: QWidget | None) -> bool:
    while widget is not None:
        if isinstance(widget, (QAbstractButton, QComboBox, QLineEdit,
                               QAbstractSpinBox, QSlider)):
            return True
        widget = widget.parentWidget()
    return False


def native_hit_test(
        window: QWidget,
        screen_x: int,
        screen_y: int,
        *,
        titlebar_height: int,
        resize_border: int = 6,
) -> int | None:
    """Own frameless hit regions; defer only if native geometry is unavailable."""
    rect = get_window_rect(window)
    if rect is None:
        return None
    dpi = dpi_for_window(window)
    physical_client = screen_to_client_physical(window, screen_x, screen_y)
    if physical_client is None:
        return None
    # Qt's effective scale includes rounding and QT_SCALE_FACTOR overrides.
    # GetDpiForWindow alone does not describe QWidget coordinates in those
    # configurations, especially after moving between unlike monitors.
    scale = float(window.devicePixelRatioF())
    logical_client = physical_client_to_logical(
        physical_client.x(), physical_client.y(), dpi,
        device_pixel_ratio=scale)
    interactive = _is_interactive(window.childAt(logical_client))
    return classify_native_hit(
        rect, screen_x, screen_y,
        client_y=physical_client.y(),
        titlebar_height_physical=max(1, round(int(titlebar_height) * scale)),
        resize_border_physical=max(1, round(int(resize_border) * scale)),
        maximized=bool(window.isMaximized()),
        interactive=interactive,
    )


def apply_native_resize_style(window: QWidget) -> bool:
    """Add a thick frame while retaining Qt's frameless visual surface."""
    try:
        hwnd = int(window.winId())
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongW
        set_style = user32.SetWindowLongW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_long
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        style_index = -16
        style = int(get_style(ctypes.c_void_p(hwnd), style_index))
        style |= 0x00040000 | 0x00010000  # WS_THICKFRAME | WS_MAXIMIZEBOX
        set_style(ctypes.c_void_p(hwnd), style_index, style)
        user32.SetWindowPos(ctypes.c_void_p(hwnd), None, 0, 0, 0, 0,
                            0x0020 | 0x0002 | 0x0001)  # FRAMECHANGED|NOMOVE|NOSIZE
        return True
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        return False


__all__ = [
    'start_system_move', 'decode_nchittest_point', 'get_window_rect',
    'screen_to_client_physical', 'physical_client_to_logical',
    'classify_native_hit', 'native_hit_test', 'apply_native_resize_style',
    'HT_LEFT', 'HT_RIGHT', 'HT_TOP', 'HT_TOPLEFT', 'HT_TOPRIGHT',
    'HT_BOTTOM', 'HT_BOTTOMLEFT', 'HT_BOTTOMRIGHT', 'HT_CAPTION', 'HT_CLIENT',
]
