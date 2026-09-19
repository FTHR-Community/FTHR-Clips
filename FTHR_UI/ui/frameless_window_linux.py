"""Window-manager driven move and resize for the frameless main window on Linux.

Windows gets resize and Aero snap through WS_THICKFRAME/WM_NCHITTEST in
main.py. Linux has no equivalent frame style, so a frameless window there
could only be dragged through ``QWidget.move()`` — invisible to the
compositor, hence no quick tiling — and not resized at all. Qt exposes the
compositor's own interactive move/resize (``_NET_WM_MOVERESIZE`` on X11,
``xdg_toplevel`` move/resize on Wayland) as ``QWindow.startSystemMove()`` and
``startSystemResize()``; this module wires those to the title bar and to a
thin border around the window.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent, QWindow

#: Width of the invisible resize border, in device-independent pixels.
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
    """Which window edges a point inside the window is close enough to grab."""
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
    """Hand the drag to the compositor. False when unsupported, so the caller
    can fall back to moving the widget itself."""
    if window is None:
        return False
    try:
        return bool(window.startSystemMove())
    except Exception:
        return False


def install_resize_filter(window: QWindow | None,
                          margin: int = RESIZE_MARGIN) -> FramelessResizeFilter | None:
    """Attach one FramelessResizeFilter to a window, reusing an existing one.

    showEvent() runs on every show, and windowHandle() can still be None on
    the very first one, so callers retry from each showEvent and rely on
    this to never stack a second filter on the same window.
    """
    if window is None:
        return None
    existing = window.findChild(FramelessResizeFilter)
    if existing is not None:
        return existing
    return FramelessResizeFilter(window, margin)


class FramelessResizeFilter(QObject):
    """Event filter for the top-level QWindow that starts a compositor resize
    from the window border and shows the matching cursor while hovering it.

    It filters the QWindow rather than the widgets because the platform
    delivers every mouse move to the window, whereas child widgets only see
    moves when they enable mouse tracking.
    """

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
        return edges_at(pos.x(), pos.y(), self._window.width(),
                        self._window.height(), self._margin)

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
                    if self._window.startSystemResize(edges):
                        return True
        elif kind == QEvent.Type.Leave:
            self._set_cursor(Qt.Edge(0))
        return False
