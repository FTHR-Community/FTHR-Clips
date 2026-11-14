"""Shared dropdown feedback and slider input regressions."""
import pytest

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QScrollArea, QSlider, QStyle, QStyleOptionSlider, QVBoxLayout, QWidget

from ui.app_style import apply_app_style
from ui.capture_settings_widget import _combo_style
from ui.style import Colors, Sizes, WheelSafeComboBox, combo_qss, slider_qss


class WheelParent(QWidget):
    def __init__(self):
        super().__init__()
        self.wheels = 0

    def wheelEvent(self, event):
        self.wheels += 1
        event.accept()


class CustomSlider(QSlider):
    def wheelEvent(self, event):
        self.setValue(0)
        event.accept()


@pytest.mark.parametrize('slider_type', [QSlider, CustomSlider])
@pytest.mark.parametrize('focused', [False, True])
def test_slider_wheel_preserves_value_without_triggering_hidden_parent_action(
        qtbot, qapp, slider_type, focused):
    apply_app_style(qapp)
    parent = WheelParent()
    qtbot.addWidget(parent)
    parent.resize(300, 80)
    slider = slider_type(Qt.Orientation.Horizontal, parent)
    slider.setGeometry(20, 20, 240, Sizes.SLIDER_H)
    slider.setRange(0, 100)
    slider.setValue(50)
    parent.show()
    if focused:
        slider.setFocus()
    event = QWheelEvent(
        QPointF(100, 12), QPointF(slider.mapToGlobal(QPoint(100, 12))),
        QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(slider, event)
    assert slider.value() == 50
    assert parent.wheels == 0


def test_slider_keyboard_and_drag_still_change_value(qtbot, qapp):
    apply_app_style(qapp)
    slider = QSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.resize(260, Sizes.SLIDER_H)
    slider.setRange(0, 100)
    slider.setValue(30)
    slider.show()
    slider.setFocus()
    qtbot.keyClick(slider, Qt.Key.Key_Right)
    assert slider.value() == 31
    option = QStyleOptionSlider()
    slider.initStyleOption(option)
    handle = slider.style().subControlRect(
        QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider)
    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=handle.center())
    qtbot.mouseMove(slider, handle.center() + QPoint(70, 0))
    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=handle.center() + QPoint(70, 0))
    assert slider.value() > 31


def test_shared_slider_metrics_apply_to_native_and_recipe_sliders(qtbot, qapp):
    apply_app_style(qapp)
    sliders = [QSlider(Qt.Orientation.Horizontal) for _ in range(2)]
    sliders[1].setStyleSheet(slider_qss())
    geometries = []
    for slider in sliders:
        qtbot.addWidget(slider)
        slider.resize(240, Sizes.SLIDER_H)
        slider.show()
        option = QStyleOptionSlider()
        slider.initStyleOption(option)
        geometries.append(slider.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option, QStyle.SubControl.SC_SliderHandle, slider))
    assert geometries[0].size() == geometries[1].size()
    assert geometries[0].width() == Sizes.SLIDER_HANDLE_SIZE
    assert geometries[0].height() == Sizes.SLIDER_HANDLE_SIZE


def test_combo_feedback_is_neutral_and_capture_uses_compact_recipe():
    for sheet in (combo_qss(), _combo_style()):
        assert f'selection-background-color: {Colors.CARD_BG_HI};' in sheet
        assert f'selection-color: {Colors.TEXT};' in sheet
        assert f'border-color: {Colors.BORDER_HI};' in sheet
        assert Colors.ACCENT not in sheet
    assert _combo_style() == combo_qss(background=Colors.SURFACE_2, compact=True)


def test_slider_wheel_scrolls_enclosing_area(qtbot, qapp):
    apply_app_style(qapp)
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(320, 160)
    content = QWidget()
    content.setMinimumHeight(800)
    layout = QVBoxLayout(content)
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setValue(50)
    layout.addWidget(slider)
    layout.addStretch()
    area.setWidget(content)
    area.setWidgetResizable(True)
    area.show()
    qtbot.waitUntil(lambda: area.verticalScrollBar().maximum() > 0)
    event = QWheelEvent(
        QPointF(20, 12), QPointF(slider.mapToGlobal(QPoint(20, 12))),
        QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False,
    )
    QApplication.sendEvent(slider, event)
    assert slider.value() == 50
    assert area.verticalScrollBar().value() > 0


@pytest.mark.parametrize('compact', [False, True])
def test_combo_preferred_width_stays_stable_with_long_items(qtbot, compact):
    combo = WheelSafeComboBox()
    qtbot.addWidget(combo)
    combo.setStyleSheet(combo_qss(compact=compact))
    combo.addItems(['Short', 'Medium'])
    combo.show()
    initial_width = combo.sizeHint().width()
    combo.addItem('A very long device name or source title that must not widen the whole settings page')
    combo.setCurrentIndex(2)
    assert combo.sizeHint().width() == initial_width
    combo.setCurrentIndex(0)
    assert combo.sizeHint().width() == initial_width
    assert combo.minimumContentsLength() == 14
    assert f'min-width: {Sizes.COMBO_COMPACT_MIN_W if compact else Sizes.COMBO_MIN_W}px;' in combo.styleSheet()
