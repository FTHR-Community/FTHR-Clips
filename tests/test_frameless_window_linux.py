"""Edge hit-testing for the Linux frameless window resize border."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from PySide6.QtCore import Qt

from ui.frameless_window_linux import RESIZE_MARGIN, cursor_for_edges, edges_at


def test_interior_points_grab_nothing():
    assert edges_at(400, 300, 800, 600) == Qt.Edge(0)
    assert edges_at(RESIZE_MARGIN, RESIZE_MARGIN, 800, 600) == Qt.Edge(0)


def test_borders_and_corners_map_to_edges():
    assert edges_at(0, 300, 800, 600) == Qt.Edges(Qt.Edge.LeftEdge)
    assert edges_at(799, 300, 800, 600) == Qt.Edges(Qt.Edge.RightEdge)
    assert edges_at(400, 2, 800, 600) == Qt.Edges(Qt.Edge.TopEdge)
    assert edges_at(400, 599, 800, 600) == Qt.Edges(Qt.Edge.BottomEdge)
    assert edges_at(1, 1, 800, 600) == Qt.Edge.TopEdge | Qt.Edge.LeftEdge
    assert edges_at(799, 599, 800, 600) == Qt.Edge.BottomEdge | Qt.Edge.RightEdge


def test_degenerate_window_grabs_nothing():
    assert edges_at(0, 0, 0, 0) == Qt.Edge(0)


def test_cursor_shapes_follow_edges():
    assert cursor_for_edges(Qt.Edge(0)) is None
    assert cursor_for_edges(Qt.Edges(Qt.Edge.LeftEdge)) is Qt.CursorShape.SizeHorCursor
    assert cursor_for_edges(Qt.Edge.TopEdge | Qt.Edge.RightEdge) is Qt.CursorShape.SizeBDiagCursor
    assert cursor_for_edges(Qt.Edge.BottomEdge | Qt.Edge.RightEdge) is Qt.CursorShape.SizeFDiagCursor


# Behaviour of the filter and the move helper against a real offscreen QWindow.
# startSystemMove/startSystemResize are called through Python attribute
# lookup, so a subclass can record the calls and dictate the return value.

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF
from PySide6.QtGui import QGuiApplication, QMouseEvent, QWindow

from ui.frameless_window_linux import (
    FramelessResizeFilter, install_resize_filter, start_system_move,
)


class _RecordingWindow(QWindow):
    def __init__(self, *, accept: bool = True) -> None:
        super().__init__()
        self.accept = accept
        self.resize_calls: list = []
        self.move_calls = 0

    def startSystemResize(self, edges):  # noqa: N802 - Qt naming
        self.resize_calls.append(Qt.Edge(edges))
        return self.accept

    def startSystemMove(self):  # noqa: N802 - Qt naming
        self.move_calls += 1
        return self.accept


class _RaisingWindow(QWindow):
    def startSystemMove(self):  # noqa: N802 - Qt naming
        raise RuntimeError('platform refused')


@pytest.fixture
def app():
    return QGuiApplication.instance() or QGuiApplication([])


def _press(window, x, y, button=Qt.MouseButton.LeftButton):
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y), QPointF(x, y),
        button, button, Qt.KeyboardModifier.NoModifier)
    return QCoreApplication.sendEvent(window, event), event


def _hover(window, x, y):
    event = QMouseEvent(
        QEvent.Type.MouseMove, QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier)
    QCoreApplication.sendEvent(window, event)


def test_start_system_move_reports_success_failure_and_exceptions(app):
    assert start_system_move(None) is False
    assert start_system_move(_RecordingWindow(accept=True)) is True
    assert start_system_move(_RecordingWindow(accept=False)) is False
    assert start_system_move(_RaisingWindow()) is False


def test_install_is_idempotent_per_window(app):
    window = _RecordingWindow()
    assert install_resize_filter(None) is None
    first = install_resize_filter(window)
    second = install_resize_filter(window)
    assert first is second
    assert len(window.findChildren(FramelessResizeFilter)) == 1
    # A different window gets its own filter.
    assert install_resize_filter(_RecordingWindow()) is not first


def test_border_press_starts_resize_and_is_consumed_only_on_success(app):
    window = _RecordingWindow(accept=True)
    window.resize(800, 600)
    install_resize_filter(window)

    _, event = _press(window, 2, 300)
    assert window.resize_calls == [Qt.Edge(Qt.Edge.LeftEdge)]
    assert event.isAccepted()

    window.resize_calls.clear()
    _press(window, 400, 300)
    assert window.resize_calls == []

    # Right button on the border is not a resize.
    _press(window, 2, 300, button=Qt.MouseButton.RightButton)
    assert window.resize_calls == []

    refused = _RecordingWindow(accept=False)
    refused.resize(800, 600)
    install_resize_filter(refused)
    _press(refused, 799, 599)
    assert refused.resize_calls == [Qt.Edge.BottomEdge | Qt.Edge.RightEdge]


def test_maximized_and_fullscreen_windows_have_no_resize_border(app):
    for state in (Qt.WindowState.WindowMaximized, Qt.WindowState.WindowFullScreen):
        window = _RecordingWindow()
        window.resize(800, 600)
        window.setWindowStates(state)
        install_resize_filter(window)
        _press(window, 1, 1)
        assert window.resize_calls == [], state
        _hover(window, 1, 1)
        assert QGuiApplication.overrideCursor() is None


def test_hover_sets_and_clears_the_override_cursor(app):
    window = _RecordingWindow()
    window.resize(800, 600)
    install_resize_filter(window)
    try:
        _hover(window, 799, 300)
        cursor = QGuiApplication.overrideCursor()
        assert cursor is not None and cursor.shape() is Qt.CursorShape.SizeHorCursor
        _hover(window, 2, 2)
        assert QGuiApplication.overrideCursor().shape() is Qt.CursorShape.SizeFDiagCursor
        _hover(window, 400, 300)
        assert QGuiApplication.overrideCursor() is None
        _hover(window, 799, 300)
        QCoreApplication.sendEvent(window, QEvent(QEvent.Type.Leave))
        assert QGuiApplication.overrideCursor() is None
    finally:
        while QGuiApplication.overrideCursor() is not None:
            QGuiApplication.restoreOverrideCursor()
