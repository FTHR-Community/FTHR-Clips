from __future__ import annotations

import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QFrame, QPushButton, QWidget

from core import windows_dpi
from core.field_diagnostics import qt_display_snapshot
from core.window_geometry import (
    clamp_rect_to_available,
    is_rect_visible,
    popup_rect_for_anchor,
    restore_window_geometry,
)
from ui import frameless_window_windows as frameless


ROOT = Path(__file__).resolve().parent.parent


class _Screen:
    def __init__(self, name: str, geometry: QRect, *, dpr: float = 1.0,
                 logical_dpi: float = 96.0) -> None:
        self._name = name
        self._geometry = QRect(geometry)
        self._dpr = dpr
        self._logical_dpi = logical_dpi

    def name(self):
        return self._name

    def geometry(self):
        return QRect(self._geometry)

    def availableGeometry(self):
        return QRect(self._geometry)

    def devicePixelRatio(self):
        return self._dpr

    def logicalDotsPerInch(self):
        return self._logical_dpi

    def refreshRate(self):
        return 60.0


class _App:
    def __init__(self, screens, primary=None) -> None:
        self._screens = list(screens)
        self._primary = primary or self._screens[0]

    def screens(self):
        return list(self._screens)

    def primaryScreen(self):
        return self._primary


@pytest.mark.parametrize(
    ('dpi', 'physical', 'logical'),
    [(96, 100, 100), (120, 125, 100), (144, 150, 100), (192, 200, 100)],
)
def test_physical_client_coordinates_convert_to_logical_once(
        dpi, physical, logical):
    point = frameless.physical_client_to_logical(physical, physical, dpi)
    assert point == QPoint(logical, logical)


def test_nchittest_lparam_preserves_negative_virtual_desktop_coordinates():
    x, y = -2560, -240
    lparam = (x & 0xFFFF) | ((y & 0xFFFF) << 16)
    assert frameless.decode_nchittest_point(lparam) == (x, y)


@pytest.mark.parametrize(
    ('point', 'expected'),
    [
        ((-2000, 100), frameless.HT_TOPLEFT),
        ((-1001, 100), frameless.HT_TOPRIGHT),
        ((-2000, 799), frameless.HT_BOTTOMLEFT),
        ((-1001, 799), frameless.HT_BOTTOMRIGHT),
        ((-1500, 100), frameless.HT_TOP),
        ((-1500, 799), frameless.HT_BOTTOM),
        ((-2000, 400), frameless.HT_LEFT),
        ((-1001, 400), frameless.HT_RIGHT),
    ],
)
def test_native_resize_edges_use_physical_window_rectangle(point, expected):
    hit = frameless.classify_native_hit(
        QRect(-2000, 100, 1000, 700), *point,
        client_y=100, titlebar_height_physical=50,
        resize_border_physical=8, maximized=False, interactive=False)
    assert hit == expected


def test_caption_hit_test_excludes_interactive_controls_and_maximized_edges():
    rect = QRect(100, 100, 800, 600)
    assert frameless.classify_native_hit(
        rect, 300, 120, client_y=20, titlebar_height_physical=50,
        resize_border_physical=8, maximized=False, interactive=False,
    ) == frameless.HT_CAPTION
    assert frameless.classify_native_hit(
        rect, 300, 120, client_y=20, titlebar_height_physical=50,
        resize_border_physical=8, maximized=False, interactive=True,
    ) == frameless.HT_CLIENT
    assert frameless.classify_native_hit(
        rect, 101, 101, client_y=1, titlebar_height_physical=50,
        resize_border_physical=8, maximized=True, interactive=False,
    ) == frameless.HT_CAPTION


def test_native_hit_test_converts_client_point_before_child_lookup(
        qtbot, monkeypatch):
    window = QWidget()
    qtbot.addWidget(window)
    window.resize(400, 300)
    button = QPushButton('button', window)
    button.setGeometry(40, 0, 120, 40)
    window.show()

    monkeypatch.setattr(frameless, 'get_window_rect',
                        lambda _window: QRect(100, 100, 500, 375))
    monkeypatch.setattr(frameless, 'dpi_for_window', lambda _window: 120)
    monkeypatch.setattr(window, 'devicePixelRatioF', lambda: 1.25)
    monkeypatch.setattr(
        frameless, 'screen_to_client_physical',
        lambda _window, _x, _y: QPoint(100, 25))
    # 100x25 physical at 125% becomes 80x20 logical, inside the button.
    assert frameless.native_hit_test(
        window, 200, 125, titlebar_height=40) == frameless.HT_CLIENT


def test_start_system_move_reports_refusal_and_success():
    class _Window:
        def __init__(self, result):
            self.result = result

        def startSystemMove(self):
            return self.result

    assert frameless.start_system_move(None) is False
    assert frameless.start_system_move(_Window(False)) is False
    assert frameless.start_system_move(_Window(True)) is True


def test_frameless_dialog_header_uses_native_move_on_windows(
        qtbot, monkeypatch):
    from ui import dialogs

    calls = []
    monkeypatch.setattr(dialogs.sys, 'platform', 'win32')
    monkeypatch.setattr(
        dialogs, 'start_system_move',
        lambda window: calls.append(window) or True)
    dialog = dialogs.FthrDialog('DPI test')
    qtbot.addWidget(dialog)
    dialog.show()
    header = dialog.findChild(QFrame, 'fthrDialogHeader')
    assert header is not None

    qtbot.mousePress(
        header, Qt.MouseButton.LeftButton, pos=QPoint(4, 4))
    assert len(calls) == 1
    assert calls[0] is dialog.windowHandle()


def test_valid_secondary_geometry_survives_stale_screen_name():
    primary = _Screen('primary', QRect(0, 0, 1920, 1040))
    left = _Screen('left', QRect(-2560, 0, 2560, 1040), dpr=1.25)
    saved = {
        'screen_name': 'old-name',
        'normal_geometry': {
            'x': -2200, 'y': 100, 'width': 1200, 'height': 800,
        },
        'maximized': False,
        'saved_dpr': 1.0,
    }
    rect, maximized, owner = restore_window_geometry(
        saved, [primary, left], primary)
    assert owner is left
    assert rect == QRect(-2200, 100, 1200, 800)
    assert maximized is False


def test_removed_monitor_geometry_recovers_into_primary_work_area():
    primary = _Screen('primary', QRect(0, 0, 1920, 1040))
    saved = {
        'screen_name': 'removed',
        'normal_geometry': {
            'x': -2500, 'y': -900, 'width': 1600, 'height': 1000,
        },
        'maximized': True,
    }
    rect, maximized, owner = restore_window_geometry(saved, [primary], primary)
    assert owner is primary
    assert primary.availableGeometry().contains(rect)
    assert maximized is True


def test_first_launch_restore_geometry_is_centered_but_still_maximized():
    primary = _Screen('primary', QRect(-100, 20, 1920, 1040))
    rect, maximized, owner = restore_window_geometry({}, [primary], primary)
    assert owner is primary
    assert rect == QRect(310, 180, 1100, 720)
    assert maximized is True


def test_small_work_area_never_expands_window_beyond_available_geometry():
    available = QRect(0, 0, 800, 600)
    rect = clamp_rect_to_available(
        QRect(50, 50, 400, 300), available,
        min_width=1100, min_height=720)
    assert rect == available


def test_visibility_and_popup_placement_support_negative_coordinates():
    left = _Screen('left', QRect(-2560, -200, 2560, 1040))
    assert is_rect_visible(QRect(-2500, -100, 1200, 800), [left])
    assert not is_rect_visible(QRect(500, 500, 1200, 800), [left])

    placed = popup_rect_for_anchor(
        QRect(-120, 760, 100, 40), 456, 320,
        left.availableGeometry(), margin=8)
    assert left.availableGeometry().adjusted(8, 8, -8, -8).contains(placed)
    assert placed.y() < 760  # insufficient space below: placed above anchor


def test_display_snapshot_labels_logical_and_estimated_physical_geometry():
    screen = _Screen('scaled', QRect(-100, 20, 2048, 1152),
                     dpr=1.25, logical_dpi=120.0)
    snapshot = qt_display_snapshot(_App([screen]))[0]
    assert snapshot['logical_geometry'] == {
        'x': -100, 'y': 20, 'width': 2048, 'height': 1152,
    }
    assert snapshot['physical_size_estimate']['width'] == 2560
    assert snapshot['physical_size_estimate']['height'] == 1440
    assert snapshot['device_pixel_ratio'] == 1.25


class _Function:
    def __init__(self, result):
        self.result = result
        self.argtypes = None
        self.restype = None

    def __call__(self, *_args):
        return self.result


def test_dpi_snapshot_distinguishes_per_monitor_v2(monkeypatch):
    pointer_bits = ctypes.sizeof(ctypes.c_void_p) * 8
    pmv2_pointer = (-4) & ((1 << pointer_bits) - 1)

    class _Equal(_Function):
        def __call__(self, left, right):
            return int(left.value == right.value == pmv2_pointer)

    user32 = SimpleNamespace(
        GetThreadDpiAwarenessContext=_Function(pmv2_pointer),
        GetAwarenessFromDpiAwarenessContext=_Function(2),
        AreDpiAwarenessContextsEqual=_Equal(0),
    )
    monkeypatch.setattr(windows_dpi.sys, 'platform', 'win32')
    monkeypatch.setattr(
        windows_dpi.ctypes, 'windll', SimpleNamespace(user32=user32),
        raising=False)
    snapshot = windows_dpi.dpi_awareness_snapshot()
    assert snapshot['thread_awareness'] == 'PerMonitor'
    assert snapshot['process_awareness'] == 'unavailable:api_missing'
    assert snapshot['per_monitor'] is True
    assert snapshot['per_monitor_v2'] is True


def test_packaged_manifest_declares_one_compatible_per_monitor_contract():
    manifest = (ROOT / 'FTHR_UI' / 'assets' /
                'fthr_windows.manifest').read_text(encoding='utf-8')
    spec = (ROOT / 'FTHR.spec').read_text(encoding='utf-8')
    assert '<dpiAware ' in manifest
    assert '>true/pm</dpiAware>' in manifest
    assert '>PerMonitorV2, PerMonitor</dpiAwareness>' in manifest
    assert 'SetProcessDpiAwareness' not in manifest
    assert "manifest=str(ASSETS_DIR / 'fthr_windows.manifest')" in spec
