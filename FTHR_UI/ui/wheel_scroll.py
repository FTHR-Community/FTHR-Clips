"""Route wheel input only to visibly scrollable content, never value controls."""
from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QAbstractScrollArea, QScrollBar, QWidget

CTRL_WHEEL_ZOOM_VIEWPORT = 'fthrCtrlWheelZoomViewport'


class VisibleScrollWheelFilter(QObject):
    """One app-wide rule, including native controls and plugin widgets.

    Deliver to the scrollbar itself instead of a viewport or arbitrary parent:
    those may interpret a wheel as zoom, a new selection, or a changed value.
    Explicit click, drag, and keyboard input are unaffected.
    """

    @staticmethod
    def _can_scroll(bar):
        return bar.isVisible() and bar.isEnabled() and bar.maximum() > bar.minimum()

    @staticmethod
    def _forward(bar, event):
        angle = event.angleDelta()
        if angle.isNull() and not event.pixelDelta().isNull():
            # QWidget scrollbars consume angular deltas on Windows. Convert
            # pixel-only touchpad input to their native units; item views use
            # rows/columns while ordinary scroll areas use pixels.
            units_per_pixel = 1.0
            owner = bar.parentWidget()
            while owner is not None and not isinstance(owner, QAbstractScrollArea):
                owner = owner.parentWidget()
            if owner is not None:
                extent = (owner.viewport().height() if bar.orientation() == Qt.Orientation.Vertical
                          else owner.viewport().width())
                units_per_pixel = bar.pageStep() / max(1, extent)
            scale = 120 * units_per_pixel / max(1, bar.singleStep() * QApplication.wheelScrollLines())
            pixels = event.pixelDelta()
            angle = QPoint(round(pixels.x() * scale), round(pixels.y() * scale))
            if angle.isNull():
                return True
        forwarded = QWheelEvent(
            bar.mapFromGlobal(event.globalPosition().toPoint()),
            event.globalPosition(), event.pixelDelta(), angle,
            event.buttons(), event.modifiers(), event.phase(),
            event.inverted(), event.source(), event.pointingDevice())
        forwarded.ignore()
        QApplication.sendEvent(bar, forwarded)
        return forwarded.isAccepted()

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        if (event.modifiers() & Qt.KeyboardModifier.ControlModifier
                and watched.property(CTRL_WHEEL_ZOOM_VIEWPORT) is True):
            area = watched.parentWidget()
            if isinstance(area, QAbstractScrollArea) and area.viewport() is watched:
                return False
        # This also terminates routing for the copied event below.
        if isinstance(watched, QScrollBar) and self._can_scroll(watched):
            if event.angleDelta().isNull() and not event.pixelDelta().isNull():
                # The converted copy has angular deltas and takes the normal
                # native path on the next event-filter invocation.
                event.setAccepted(self._forward(watched, event))
                return True
            return False

        event.ignore()
        delta = event.pixelDelta() if not event.pixelDelta().isNull() else event.angleDelta()
        horizontal = (abs(delta.x()) > abs(delta.y())
                      or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        ancestor = watched
        while ancestor is not None:
            if isinstance(ancestor, QAbstractScrollArea):
                vertical = ancestor.verticalScrollBar()
                sideways = ancestor.horizontalScrollBar()
                bar = sideways if horizontal else vertical
                # A horizontal-only strip can use a conventional vertical wheel
                # because its visible scrollbar makes the destination explicit.
                if not horizontal and not self._can_scroll(vertical):
                    bar = sideways
                if self._can_scroll(bar) and self._forward(bar, event):
                    event.accept()
                    return True
            # Never scroll a page behind a popup, dialog, or separate window.
            if ancestor.isWindow():
                break
            ancestor = ancestor.parentWidget()
        return True
