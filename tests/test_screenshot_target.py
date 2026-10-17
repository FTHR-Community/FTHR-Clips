from __future__ import annotations

from dataclasses import dataclass

from core.screenshot_target import select_qt_screen
from core.windows_monitor import MonitorChoice


@dataclass
class _Screen:
    screen_name: str
    bounds: tuple[int, int, int, int] | None = None

    def name(self):
        return self.screen_name

    def geometry(self):
        if self.bounds is None:
            raise AttributeError('no geometry')
        return _Geometry(*self.bounds)


@dataclass
class _Geometry:
    left: int
    top: int
    pixel_width: int
    pixel_height: int

    def x(self):
        return self.left

    def y(self):
        return self.top

    def width(self):
        return self.pixel_width

    def height(self):
        return self.pixel_height


def test_windows_stable_monitor_path_maps_to_matching_qt_screen():
    screens = [_Screen(r'\\.\DISPLAY1'), _Screen(r'\\.\DISPLAY2')]
    choices = [
        MonitorChoice(r'\\.\DISPLAY1', 'One', 'path-one', True),
        MonitorChoice(r'\\.\DISPLAY2', 'Two', 'path-two', False),
    ]

    selected = select_qt_screen(
        'path-two', screens, platform='win32', windows_monitors=choices)

    assert selected is screens[1]


def test_selected_monitor_missing_does_not_silently_fall_back_to_primary():
    screens = [_Screen('DP-1'), _Screen('DP-2')]

    assert select_qt_screen('DP-9', screens, platform='win32') is None


def test_windows_qt_friendly_names_map_by_current_geometry():
    screens = [
        _Screen('27G2G8', (0, 0, 1920, 1080)),
        _Screen('LG ULTRAWIDE', (-2560, 0, 2560, 1080)),
    ]
    choices = [
        MonitorChoice(r'\\.\DISPLAY1', 'Generic', 'path-one', True,
                      0, 0, 1920, 1080),
        MonitorChoice(r'\\.\DISPLAY2', 'Generic', 'path-two', False,
                      -2560, 0, 2560, 1080),
    ]

    selected = select_qt_screen(
        'path-two', screens, platform='win32', windows_monitors=choices,
        primary=screens[0])

    assert selected is screens[1]


def test_windows_topology_change_is_resolved_fresh_from_stable_identity():
    screens = [_Screen(r'\\.\DISPLAY1'), _Screen(r'\\.\DISPLAY2')]
    first_topology = [
        MonitorChoice(r'\\.\DISPLAY1', 'One', 'path-one', True),
        MonitorChoice(r'\\.\DISPLAY2', 'Two', 'path-two', False),
    ]
    second_topology = [
        MonitorChoice(r'\\.\DISPLAY2', 'Two', 'path-one', True),
        MonitorChoice(r'\\.\DISPLAY1', 'One', 'path-two', False),
    ]

    first = select_qt_screen(
        'path-two', screens, platform='win32', windows_monitors=first_topology)
    second = select_qt_screen(
        'path-two', screens, platform='win32', windows_monitors=second_topology)

    assert first is screens[1]
    assert second is screens[0]


def test_no_selected_monitor_uses_primary_fallback():
    screens = [_Screen('DP-1'), _Screen('DP-2')]

    assert select_qt_screen('', screens, platform='win32', primary=screens[0]) is screens[0]
