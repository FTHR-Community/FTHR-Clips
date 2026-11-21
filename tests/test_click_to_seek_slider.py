"""Pointer gestures use the same signal and tracking rules as native sliders."""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionSlider

from ui.sliders import ClickToSeekSlider
from ui.style import slider_qss


@pytest.fixture
def slider(qtbot):
    widget = ClickToSeekSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(widget)
    widget.setStyleSheet(slider_qss())
    widget.resize(240, widget.minimumHeight())
    widget.setRange(0, 100)
    widget.setValue(20)
    widget.show()
    return widget


@pytest.mark.parametrize('tracking', [True, False])
def test_groove_press_drag_and_release(qtbot, slider, tracking):
    slider.setTracking(tracking)
    events = []
    slider.sliderPressed.connect(lambda: events.append(('pressed', slider.sliderPosition())))
    slider.sliderMoved.connect(lambda value: events.append(('moved', value)))
    slider.sliderReleased.connect(lambda: events.append(('released', slider.sliderPosition())))
    slider.valueChanged.connect(lambda value: events.append(('changed', value)))

    # Start above the painted groove and continue in the same gesture.
    start, drag, end = QPoint(140, 1), QPoint(180, 12), QPoint(215, 12)
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=start)
    clicked_position = slider.sliderPosition()
    assert 55 <= clicked_position <= 65
    assert slider.isSliderDown()
    assert slider.value() == (clicked_position if tracking else 20)

    move = QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(drag), QPointF(slider.mapToGlobal(drag)),
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(slider, move)
    dragged_position = slider.sliderPosition()
    assert dragged_position > clicked_position
    assert slider.value() == (dragged_position if tracking else 20)

    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=end)
    final_value = slider.value()
    assert final_value > dragged_position
    assert not slider.isSliderDown()
    assert [value for event, value in events if event == 'moved'] == [
        clicked_position, dragged_position, final_value]
    assert [value for event, value in events if event == 'changed'] == (
        [clicked_position, dragged_position, final_value] if tracking else [final_value])
    assert events[0] == ('pressed', 20)
    assert [value for event, value in events if event == 'released'] == [final_value]
    assert events[-2:] == (
        [('changed', final_value), ('released', final_value)] if tracking
        else [('released', final_value), ('changed', final_value)])


def test_thumb_click_preserves_value_and_keyboard_still_steps(qtbot, slider):
    slider.setRange(0, 10_000)
    slider.setValue(3123)
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider)
    with qtbot.assertNotEmitted(slider.valueChanged):
        qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=handle.bottomRight())
    assert slider.value() == 3123

    qtbot.keyClick(slider, Qt.Key.Key_Right)
    assert slider.value() == 3124
    qtbot.keyClick(slider, Qt.Key.Key_End)
    assert slider.value() == 10_000


def test_inverted_vertical_slider_maps_both_ends(qtbot):
    slider = ClickToSeekSlider(Qt.Orientation.Vertical)
    qtbot.addWidget(slider)
    slider.setInvertedAppearance(True)
    slider.setRange(0, 100)
    slider.resize(slider.minimumWidth(), 240)
    slider.show()
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(10, 238))
    assert slider.value() == 100
    qtbot.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(10, 1))
    assert slider.value() == 0
