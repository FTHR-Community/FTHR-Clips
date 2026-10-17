"""Bounded page transitions; static widget pages avoid repainting their trees."""

from PySide6.QtCore import (
    QEasingCurve, QPoint, QRect, QSize, Qt, QVariantAnimation,
)
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QStackedWidget, QWidget
from ui.motion import reduce_motion
from ui.quick_snapshot import create_snapshot_surface


def compact_snapshot(widget, bounds=QSize(560, 350)):
    """Render a shrinking transition directly into its small backing store."""
    size = widget.size().scaled(bounds, Qt.AspectRatioMode.KeepAspectRatio).boundedTo(widget.size())
    ratio = widget.devicePixelRatioF()
    pixmap = QPixmap(size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.scale(size.width() / max(1, widget.width()),
                      size.height() / max(1, widget.height()))
        widget.render(painter, QPoint())
    finally:
        painter.end()
    return pixmap


class SnapshotFlight(QWidget):
    """Scale one captured editor frame between two screen rectangles."""

    def __init__(self, owner, pixmap, start, end, *, compact=False):
        super().__init__(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint)
        self._surface = None
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._compact = compact
        if compact:
            # Animate a thumbnail on a fixed, small native surface. A desktop-
            # sized layered window is costly to upload even as its image shrinks.
            size = start.size().scaled(QSize(560, 350), Qt.AspectRatioMode.KeepAspectRatio)
            size = size.boundedTo(start.size()).expandedTo(end.size())
            center = start.center()
            start = QRect(QPoint(), size)
            start.moveCenter(center)
            dpr = pixmap.devicePixelRatio()
            if pixmap.size() != size * dpr:
                pixmap = pixmap.scaled(
                    size * dpr, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            pixmap.setDevicePixelRatio(dpr)
        self._pixmap = pixmap
        self._expanding = end.width() * end.height() > start.width() * start.height()
        self._opacity = 0.0 if self._expanding else 1.0
        # Keep the native surface size fixed. Large flights stay stationary;
        # compact flights move their small surface along the thumbnail's path.
        self.setGeometry(start if compact else start.united(end))
        self._origin = self.geometry().topLeft()
        self._frame_rect = start.translated(-self.geometry().topLeft())
        self._animation = QVariantAnimation(self)
        self._animation.valueChanged.connect(self._advance)
        self._animation.setDuration(220)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.setStartValue(self._frame_rect)
        self._animation.setEndValue(end.translated(-self.geometry().topLeft()))
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._surface = None if compact else create_snapshot_surface(self)
        if self._surface is not None:
            self._surface.failed.connect(self._fallback)
            self._surface.setGeometry(self.rect())
            self._surface.set_images(pixmap)
            self._surface.set_flight(self._frame_rect, self._opacity)

    def _fallback(self):
        if self._surface is not None:
            self._surface.hide()
            self._surface.release_images()
            self._surface.deleteLater()
            self._surface = None
            self.update()

    def _advance(self, rect):
        if self._compact:
            # Move, but never resize, the native surface. Only the thumbnail
            # inside it scales; the compositor upload stays bounded throughout.
            origin = self._animation.startValue().topLeft()
            global_rect = rect.translated(self._origin)
            self.move(global_rect.topLeft() - origin)
            self._frame_rect = QRect(origin, rect.size())
        else:
            self._frame_rect = rect
        progress = self._animation.easingCurve().valueForProgress(
            self._animation.currentTime() / self._animation.duration())
        self._opacity = progress if self._expanding else 1.0 - progress
        if self._surface is not None:
            self._surface.set_flight(self._frame_rect, self._opacity)
        else:
            self.update()

    def start(self):
        self.show()
        self._animation.finished.connect(self.deleteLater)
        self._animation.start()

    def paintEvent(self, event):
        if self._surface is not None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(self._frame_rect, self._pixmap)

    def hideEvent(self, event):
        self._pixmap = QPixmap()
        if self._surface is not None:
            self._surface.release_images()
        super().hideEvent(event)


class SnapshotSlide(QWidget):
    def __init__(self, stack, outgoing, incoming, direction, *, horizontal=False,
                 use_gpu=True):
        super().__init__(stack)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(stack.rect())
        self._outgoing = outgoing
        self._live_incoming = incoming if isinstance(incoming, QWidget) else None
        self._incoming = QPixmap() if self._live_incoming is not None else incoming
        self._incoming_geometry = (QRect(incoming.geometry())
                                   if self._live_incoming is not None else None)
        self._background = (stack.palette().window()
                            if not use_gpu and self._live_incoming is None else None)
        if self._live_incoming is not None:
            self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self._direction = direction
        self._horizontal = horizontal
        self._progress = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(220)
        self.animation.setStartValue(0.0)
        self.animation.setEndValue(1.0)
        self.animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.animation.valueChanged.connect(self._advance)
        self._surface = getattr(stack, '_snapshot_slide_surface', None) if use_gpu else None
        if (use_gpu and self._surface is None
                and not getattr(stack, '_snapshot_gpu_failed', False)):
            self._surface = create_snapshot_surface(stack)
            stack._snapshot_slide_surface = self._surface
        if self._surface is not None:
            self._surface.failed.connect(self._fallback)
            self._surface.setGeometry(self.geometry())
            self._surface.set_images(outgoing, self._incoming)
            self._surface.set_slide(0.0, horizontal, direction)
            self._surface.show()
            self._surface.raise_()
        # Static page snapshots cover the whole viewport. Marking their
        # raster overlay opaque prevents Qt repainting the controls underneath
        # on every frame. Live video and transparent snapshots keep compositing.
        if self._background is not None and self._background.isOpaque():
            self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._advance(0.0)

    def _fallback(self):
        if self._surface is not None:
            parent = self.parentWidget()
            parent._snapshot_gpu_failed = True
            parent._snapshot_slide_surface = None
            surface = self._surface
            surface.failed.disconnect(self._fallback)
            surface.hide()
            surface.release_images()
            surface.deleteLater()
            self._surface = None
            self.update()

    def release_snapshots(self):
        if self._live_incoming is not None:
            self._live_incoming.setGeometry(self._incoming_geometry)
            self._live_incoming = None
        self._outgoing = QPixmap()
        self._incoming = QPixmap()
        if self._surface is not None:
            self._surface.failed.disconnect(self._fallback)
            self._surface.hide()
            self._surface.release_images()
            self._surface = None

    def _advance(self, value):
        self._progress = value
        if self._live_incoming is not None:
            distance = self.width() if self._horizontal else self.height()
            offset = round(distance * (1.0 - value)) * self._direction
            origin = self._incoming_geometry.topLeft()
            self._live_incoming.move(origin + (
                QPoint(offset, 0) if self._horizontal else QPoint(0, offset)))
        if self._surface is not None:
            self._surface.set_slide(value, self._horizontal, self._direction)
        else:
            self.update()

    def paintEvent(self, event):
        if self._surface is not None:
            return
        painter = QPainter(self)
        if self._background is not None:
            painter.fillRect(self.rect(), self._background)
        distance = self.width() if self._horizontal else self.height()
        offset = round(distance * self._progress) * self._direction
        outgoing = QPoint(-offset, 0) if self._horizontal else QPoint(0, -offset)
        incoming = (QPoint(distance * self._direction - offset, 0) if self._horizontal
                    else QPoint(0, distance * self._direction - offset))
        painter.drawPixmap(QRect(outgoing, self.size()), self._outgoing)
        if not self._incoming.isNull():
            painter.drawPixmap(QRect(incoming, self.size()), self._incoming)

    def hideEvent(self, event):
        self.release_snapshots()
        super().hideEvent(event)


class ShellPageStack(QStackedWidget):
    """Keep normal stack semantics while painting a bounded vertical slide."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._slide = None

    def _finish_slide(self):
        if self._slide is not None:
            self._slide.animation.stop()
            self._slide.hide()
            self._slide.release_snapshots()
            self._slide.deleteLater()
            self._slide = None

    def setCurrentIndex(self, index):
        previous = self.currentIndex()
        if index == previous or not 0 <= index < self.count():
            return
        self._finish_slide()
        animate = self.isVisible() and previous >= 0 and not reduce_motion(self)
        outgoing = self.currentWidget().grab() if animate else None
        super().setCurrentIndex(index)
        if animate:
            incoming = self.currentWidget()
            # These pages contain large QWidget trees. Moving the live page
            # underneath a transparent Quick surface repaints it every frame.
            # Capture it once; the real page stays laid out at its final origin.
            self._slide = SnapshotSlide(
                self, outgoing, incoming.grab(), 1 if index > previous else -1,
                use_gpu=False)
            self._slide.show()
            self._slide.raise_()
            self._slide.animation.finished.connect(self._finish_slide)
            self._slide.animation.start()

    def resizeEvent(self, event):
        self._finish_slide()
        super().resizeEvent(event)
        # Qt can resize the stacked page before delivering this event. Ending
        # a slide restores its pre-animation geometry; negotiate layout again.
        self.layout().invalidate()
        self.layout().activate()

    def hideEvent(self, event):
        self._finish_slide()
        super().hideEvent(event)


def sidebar_rect(button):
    return QRect(button.mapToGlobal(QPoint(0, 0)), button.size())
