"""Deterministic logical Qt window geometry persistence.

Saved positions are Qt logical coordinates.  DPI values are recorded only as
diagnostic context; restoration never rescales a valid logical geometry.
"""
from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QRect


DEFAULT_MIN_WIDTH = 1100
DEFAULT_MIN_HEIGHT = 720
MIN_VISIBLE_WIDTH = 64
MIN_VISIBLE_HEIGHT = 40


def _rect_values(rect: Any) -> tuple[int, int, int, int]:
    return (int(rect.x()), int(rect.y()), int(rect.width()), int(rect.height()))


def rect_to_dict(rect: Any) -> dict[str, int]:
    x, y, width, height = _rect_values(rect)
    return {'x': x, 'y': y, 'width': width, 'height': height}


def rect_from_dict(value: Any) -> QRect | None:
    if not isinstance(value, dict):
        return None
    try:
        width = int(value['width'])
        height = int(value['height'])
        if width <= 0 or height <= 0:
            return None
        return QRect(int(value['x']), int(value['y']), width, height)
    except (KeyError, TypeError, ValueError):
        # User-editable persisted geometry is optional; invalid data means the
        # deterministic current-screen default is used by the caller.
        return None


def clamp_rect_to_available(
        rect: QRect,
        available: QRect,
        *,
        min_width: int = DEFAULT_MIN_WIDTH,
        min_height: int = DEFAULT_MIN_HEIGHT,
) -> QRect:
    """Keep a logical window visible while preserving its usable size."""
    width = min(int(available.width()),
                max(int(min_width), int(rect.width())))
    height = min(int(available.height()),
                 max(int(min_height), int(rect.height())))
    x_min = int(available.left())
    y_min = int(available.top())
    x_max = int(available.right()) - width + 1
    y_max = int(available.bottom()) - height + 1
    x = min(max(int(rect.x()), x_min), max(x_min, x_max))
    y = min(max(int(rect.y()), y_min), max(y_min, y_max))
    return QRect(x, y, width, height)


def _screen_name(screen: Any) -> str:
    try:
        return str(screen.name() or '')
    except (AttributeError, RuntimeError):
        return ''


def _screen_geometry(screen: Any, *, available: bool = True) -> QRect | None:
    try:
        geometry = screen.availableGeometry() if available else screen.geometry()
        return QRect(geometry)
    except (AttributeError, RuntimeError, TypeError):
        # Screens may disappear while Qt dispatches a topology change. The
        # caller can then select another live screen deterministically.
        return None


def choose_restore_screen(
        saved_screen_name: str,
        screens: Iterable[Any],
        primary_screen: Any = None,
        candidate: QRect | None = None,
) -> Any:
    """Choose saved screen, geometric owner, primary, then first screen."""
    screens = list(screens)
    wanted = str(saved_screen_name or '').casefold()
    if wanted:
        for screen in screens:
            if _screen_name(screen).casefold() == wanted:
                return screen
    if candidate is not None:
        best_screen = None
        best_area = 0
        for screen in screens:
            available = _screen_geometry(screen)
            if available is None:
                continue
            intersection = candidate.intersected(available)
            area = max(0, intersection.width()) * max(0, intersection.height())
            if area > best_area:
                best_area = area
                best_screen = screen
        if best_screen is not None:
            return best_screen
    if primary_screen in screens:
        return primary_screen
    return screens[0] if screens else None


def is_rect_visible(
        rect: QRect,
        screens: Iterable[Any],
        *,
        min_visible_width: int = MIN_VISIBLE_WIDTH,
        min_visible_height: int = MIN_VISIBLE_HEIGHT,
) -> bool:
    """Return whether a useful logical portion is visible on any work area."""
    for screen in screens:
        available = _screen_geometry(screen)
        if available is None:
            continue
        intersection = rect.intersected(available)
        if (intersection.width() >= int(min_visible_width)
                and intersection.height() >= int(min_visible_height)):
            return True
    return False


def popup_rect_for_anchor(
        anchor: QRect,
        popup_width: int,
        popup_height: int,
        available: QRect,
        *,
        margin: int = 8,
) -> QRect:
    """Place a logical Qt popup below/above its anchor inside one work area."""
    safe = available.adjusted(int(margin), int(margin),
                              -int(margin), -int(margin))
    width = min(max(1, int(popup_width)), max(1, safe.width()))
    height = min(max(1, int(popup_height)), max(1, safe.height()))
    x = int(anchor.left())
    y = int(anchor.bottom()) + 1
    if x + width > safe.right() + 1:
        x = int(anchor.right()) - width + 1
    if y + height > safe.bottom() + 1:
        y = int(anchor.top()) - height
    x = min(max(x, safe.left()), safe.right() - width + 1)
    y = min(max(y, safe.top()), safe.bottom() - height + 1)
    return QRect(x, y, width, height)


def save_window_geometry(window: Any) -> dict[str, Any]:
    """Serialize normal logical geometry and diagnostic DPI context."""
    normal = window.normalGeometry()
    screen = window.screen()
    payload: dict[str, Any] = {
        'normal_geometry': rect_to_dict(normal),
        'screen_name': _screen_name(screen),
        'maximized': bool(window.isMaximized()),
    }
    try:
        payload['saved_dpr'] = round(float(screen.devicePixelRatio()), 3)
        payload['saved_logical_dpi'] = round(float(screen.logicalDotsPerInch()), 3)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        payload['saved_dpr'] = None
        payload['saved_logical_dpi'] = None
    return payload


def restore_window_geometry(
        saved: Any,
        screens: Iterable[Any],
        primary_screen: Any = None,
        *,
        default_size: tuple[int, int] = (DEFAULT_MIN_WIDTH, DEFAULT_MIN_HEIGHT),
        default_maximized: bool = True,
) -> tuple[QRect, bool, Any]:
    """Resolve persisted geometry without applying any DPI rescaling."""
    screens = list(screens)
    candidate = rect_from_dict(
        saved.get('normal_geometry') if isinstance(saved, dict) else None)
    screen = choose_restore_screen(
        saved.get('screen_name', '') if isinstance(saved, dict) else '',
        screens, primary_screen, candidate)
    available = _screen_geometry(screen) if screen is not None else None
    if available is None:
        width, height = default_size
        return QRect(0, 0, int(width), int(height)), default_maximized, screen
    if candidate is None:
        width = min(int(default_size[0]), int(available.width()))
        height = min(int(default_size[1]), int(available.height()))
        candidate = QRect(
            int(available.x()) + (int(available.width()) - width) // 2,
            int(available.y()) + (int(available.height()) - height) // 2,
            width,
            height,
        )
    restored = clamp_rect_to_available(candidate, available,
                                       min_width=int(default_size[0]),
                                       min_height=int(default_size[1]))
    maximized = (bool(saved.get('maximized')) if isinstance(saved, dict)
                 and 'maximized' in saved else bool(default_maximized))
    return restored, maximized, screen


__all__ = [
    'DEFAULT_MIN_WIDTH', 'DEFAULT_MIN_HEIGHT', 'MIN_VISIBLE_WIDTH',
    'MIN_VISIBLE_HEIGHT', 'rect_to_dict', 'rect_from_dict',
    'clamp_rect_to_available', 'choose_restore_screen', 'save_window_geometry',
    'restore_window_geometry', 'is_rect_visible', 'popup_rect_for_anchor',
]
