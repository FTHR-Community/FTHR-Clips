"""Compositor-driven move and resize support for the Linux frameless window.

The Windows main window uses native frame styles and hit testing.  On Linux a
plain ``QWidget.move()`` bypasses the compositor, so tiling and interactive
resize do not work for the frameless window.  Qt exposes the compositor's
interactive move/resize APIs through ``QWindow``; this module keeps that
platform-specific behavior out of the main application class.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QWindow


RESIZE_MARGIN = 8

_CURSORS = {
    Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.TopEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.BottomEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeFDiagCursor,
    Qt.Edge.TopEdge | Qt.Edge.RightEdge: Qt.CursorShape.SizeBDiagCursor,
    Qt.Edge.BottomEdge | Qt.Edge.LeftEdge: Qt.CursorShape.SizeBDiagCursor,
}


def edges_at(x: float, y: float, width: int, height: int,
             margin: int = RESIZE_MARGIN) -> Qt.Edge:
    """Return the window edges within ``margin`` of an interior point."""
    edges = Qt.Edge(0)
    if width <= 0 or height <= 0:
        return edges
    if x < margin:
        edges |= Qt.Edge.LeftEdge
    elif x >= width - margin:
        edges |= Qt.Edge.RightEdge
    if y < margin:
        edges |= Qt.Edge.TopEdge
    elif y >= height - margin:
        edges |= Qt.Edge.BottomEdge
    return edges


def cursor_for_edges(edges: Qt.Edge) -> Qt.CursorShape | None:
    return _CURSORS.get(Qt.Edge(edges))


def start_system_move(window: QWindow | None) -> bool:
    """Ask the compositor to begin a move, returning False if unsupported."""
    if window is None:
        return False
    try:
        return bool(window.startSystemMove())
    except Exception:
        return False


def install_resize_filter(
        window: QWindow | None,
        margin: int = RESIZE_MARGIN,
) -> FramelessResizeFilter | None:
    """Install one border filter per window, reusing it across repeated shows."""
    if window is None:
        return None
    existing = window.findChild(FramelessResizeFilter)
    if existing is not None:
        return existing
    return FramelessResizeFilter(window, margin)


class FramelessResizeFilter(QObject):
    """Start compositor resize operations from the top-level window border."""

    def __init__(self, window: QWindow, margin: int = RESIZE_MARGIN) -> None:
        super().__init__(window)
        self._window = window
        self._margin = margin
        self._override_active = False
        window.installEventFilter(self)

    def _edges(self, event: QMouseEvent) -> Qt.Edge:
        state = self._window.windowStates()
        if state & (Qt.WindowState.WindowMaximized | Qt.WindowState.WindowFullScreen):
            return Qt.Edge(0)
        pos = event.position()
        return edges_at(
            pos.x(), pos.y(), self._window.width(), self._window.height(),
            self._margin)

    def _set_cursor(self, edges: Qt.Edge) -> None:
        shape = cursor_for_edges(edges)
        if shape is None:
            if self._override_active:
                QGuiApplication.restoreOverrideCursor()
                self._override_active = False
            return
        if self._override_active:
            QGuiApplication.changeOverrideCursor(QCursor(shape))
        else:
            QGuiApplication.setOverrideCursor(QCursor(shape))
            self._override_active = True

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is not self._window:
            return False
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            if not event.buttons():
                self._set_cursor(self._edges(event))
        elif kind == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.LeftButton:
                edges = self._edges(event)
                if edges:
                    self._set_cursor(Qt.Edge(0))
                    try:
                        started = bool(self._window.startSystemResize(edges))
                    except Exception:
                        started = False
                    if started:
                        return True
        elif kind == QEvent.Type.Leave:
            self._set_cursor(Qt.Edge(0))
        return False
