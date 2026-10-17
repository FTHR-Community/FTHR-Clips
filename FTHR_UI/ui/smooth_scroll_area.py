"""Accumulate mouse-wheel input without interrupting native trackpad scrolling."""
import math

from PySide6.QtCore import QElapsedTimer, QTimer, Qt
from PySide6.QtWidgets import QApplication, QScrollArea


class SmoothScrollArea(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scroll_target = 0.0
        self._scroll_position = 0.0
        self._applying_scroll = False
        self._scroll_clock = QElapsedTimer()
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._scroll_timer.setInterval(8)
        self._scroll_timer.timeout.connect(self._advance_scroll)
        self.verticalScrollBar().valueChanged.connect(self._external_scroll)
        self.verticalScrollBar().sliderPressed.connect(self._stop_scroll)
        self.verticalScrollBar().rangeChanged.connect(self._clamp_target)

    def _stop_scroll(self):
        self._scroll_timer.stop()
        self._scroll_target = self._scroll_position = float(self.verticalScrollBar().value())

    def _external_scroll(self, _value):
        if not self._applying_scroll:
            self._stop_scroll()

    def _clamp_target(self, _minimum, _maximum):
        bar = self.verticalScrollBar()
        self._scroll_target = max(bar.minimum(), min(bar.maximum(), self._scroll_target))

    def wheelEvent(self, event):
        # Pixel deltas already contain the device's motion/inertia. Horizontal
        # and modified wheel events retain Qt's native behavior as well.
        if (not event.pixelDelta().isNull() or event.angleDelta().y() == 0
                or event.modifiers() != Qt.KeyboardModifier.NoModifier):
            self._stop_scroll()
            super().wheelEvent(event)
            return
        bar = self.verticalScrollBar()
        delta = -event.angleDelta().y() / 120 * bar.singleStep() * QApplication.wheelScrollLines()
        if not self._scroll_timer.isActive():
            self._scroll_target = self._scroll_position = float(bar.value())
        target = max(bar.minimum(), min(bar.maximum(), self._scroll_target + delta))
        if target == self._scroll_target:
            event.ignore()
            return
        self._scroll_target = target
        if not self._scroll_timer.isActive():
            self._scroll_clock.start()
            self._scroll_timer.start()
        event.accept()

    def _advance_scroll(self):
        elapsed = max(1, self._scroll_clock.restart())
        # Time-based motion stays consistent when a frame is delayed. New
        # wheel input extends the target instead of restarting an animation.
        self._scroll_position += (self._scroll_target - self._scroll_position) * (1 - math.exp(-elapsed / 35))
        finished = abs(self._scroll_target - self._scroll_position) < 0.5
        if finished:
            self._scroll_position = self._scroll_target
            self._scroll_timer.stop()
        self._applying_scroll = True
        try:
            self.verticalScrollBar().setValue(round(self._scroll_position))
        finally:
            self._applying_scroll = False

    def hideEvent(self, event):
        self._stop_scroll()
        super().hideEvent(event)
