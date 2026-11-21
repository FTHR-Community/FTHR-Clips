"""Shared pointer behavior for desktop sliders."""

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider

from ui.style import Sizes


class ClickToSeekSlider(QSlider):
    """Jump to a left click and continue dragging through the same gesture."""

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(orientation, parent)
        self._pointer_axis = None
        self._handle_offset = 0
        if orientation == Qt.Orientation.Horizontal:
            self.setMinimumHeight(Sizes.SLIDER_H)
        else:
            self.setMinimumWidth(Sizes.SLIDER_H)

    def _slider_geometry(self):
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        style = self.style()
        groove = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderGroove, self)
        handle = style.subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, self)
        return option, groove, handle

    def _axis(self, point: QPoint) -> int:
        return point.x() if self.orientation() == Qt.Orientation.Horizontal else point.y()

    def _value_from_position(self, position: QPoint) -> int:
        option, groove, handle = self._slider_geometry()
        if option.orientation == Qt.Orientation.Horizontal:
            start, span = groove.x(), groove.width() - handle.width()
        else:
            start, span = groove.y(), groove.height() - handle.height()
        # QRect centers round toward the leading edge for an even-sized handle.
        handle_center = self._axis(handle.center()) - self._axis(handle.topLeft())
        if span <= 0:
            return self.sliderPosition()
        offset = self._axis(position) - start - handle_center - self._handle_offset
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), max(0, min(span, offset)), span, option.upsideDown)

    def mousePressEvent(self, event):  # noqa: N802 - Qt API name
        if (event.button() != Qt.MouseButton.LeftButton
                or event.buttons() != Qt.MouseButton.LeftButton
                or not self.isEnabled() or self.minimum() == self.maximum()):
            super().mousePressEvent(event)
            return
        position = event.position().toPoint()
        _, _, handle = self._slider_geometry()
        self._pointer_axis = self._axis(position)
        on_handle = handle.contains(position)
        self._handle_offset = self._pointer_axis - self._axis(handle.center()) if on_handle else 0
        self.setSliderDown(True)
        if self._pointer_axis is not None and not on_handle:
            self.setSliderPosition(self._value_from_position(position))
        event.accept()

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API name
        if self._pointer_axis is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._move_pointer(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API name
        if self._pointer_axis is not None and event.button() == Qt.MouseButton.LeftButton:
            self._move_pointer(event.position().toPoint())
            self._end_pointer_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def event(self, event):
        if self._pointer_axis is not None and (
                event.type() in (QEvent.Type.UngrabMouse, QEvent.Type.Hide)
                or (event.type() == QEvent.Type.EnabledChange and not self.isEnabled())):
            self._end_pointer_drag()
        return super().event(event)

    def _move_pointer(self, position: QPoint):
        axis = self._axis(position)
        if axis != self._pointer_axis:
            self._pointer_axis = axis
            self.setSliderPosition(self._value_from_position(position))

    def _end_pointer_drag(self):
        self._pointer_axis = None
        self._handle_offset = 0
        self.setSliderDown(False)
