"""Pure selected-monitor mapping for screenshots."""

from __future__ import annotations

import os

from .windows_monitor import desktop_monitor_identity
from typing import Iterable, Sequence


def _screen_name(screen: object) -> str:
    name = getattr(screen, 'name', None)
    return str(name() if callable(name) else name or '')


def qt_screen_name(screen: object) -> str:
    """Return the stable Qt output name used by the platform backend."""

    return _screen_name(screen)


def _screen_geometry(screen: object) -> tuple[int, int, int, int] | None:
    geometry_getter = getattr(screen, 'geometry', None)
    if not callable(geometry_getter):
        return None
    geometry = geometry_getter()
    try:
        return tuple(
            int(getattr(geometry, part)())
            for part in ('x', 'y', 'width', 'height')
        )
    except (AttributeError, TypeError, ValueError):
        return None


def select_qt_screen(
    selected_monitor: str,
    screens: Sequence[object],
    *,
    platform: str,
    windows_monitors: Iterable[object] = (),
    primary: object | None = None,
) -> object | None:
    if not selected_monitor:
        return primary if primary is not None else (screens[0] if screens else None)

    target = selected_monitor
    if platform.startswith('win'):
        selected_key = os.path.normcase(selected_monitor)
        matched_monitor = None
        for monitor in windows_monitors:
            device_path = os.path.normcase(str(getattr(monitor, 'device_path', '')))
            fallback = desktop_monitor_identity(int(getattr(monitor, 'hmonitor', 0)),
                                                str(getattr(monitor, 'gdi_name', '')))
            if device_path == selected_key or fallback == selected_key:
                matched_monitor = monitor
                target = str(getattr(monitor, 'gdi_name', ''))
                break
        else:
            return None

    for screen in screens:
        actual = _screen_name(screen)
        if platform.startswith('win'):
            if actual.casefold() == target.casefold():
                return screen
        elif actual == target:
            return screen
    if platform.startswith('win') and matched_monitor is not None:
        expected_geometry = tuple(
            int(getattr(matched_monitor, part, 0))
            for part in ('x', 'y', 'width', 'height')
        )
        if expected_geometry[2] > 0 and expected_geometry[3] > 0:
            for screen in screens:
                if _screen_geometry(screen) == expected_geometry:
                    return screen
        if bool(getattr(matched_monitor, 'primary', False)) and primary is not None:
            return primary
    return None


def build_grim_command(
    grim_path: str, output_path: str, selected_output: str = ''
) -> list[str]:
    output = selected_output.strip()
    if not output:
        raise ValueError('grim requires an explicit selected output')
    return [grim_path, '-o', output, output_path]
