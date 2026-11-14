"""Wheel input scrolls visible scrollbars without changing unrelated controls."""
import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateTimeEdit, QDial, QDoubleSpinBox, QListWidget,
    QScrollArea, QSlider, QSpinBox, QTabBar, QTextEdit, QWidget,
)
from ui.app_style import apply_app_style
from ui.number_spinbox import NumberSpinBox, NumberDoubleSpinBox


@pytest.fixture(autouse=True)
def wheel_policy(qapp):
    apply_app_style(qapp)


def wheel(widget, *, x=0, y=-120, pixel=None, modifiers=Qt.KeyboardModifier.NoModifier):
    point = widget.rect().center()
    event = QWheelEvent(
        QPointF(point), QPointF(widget.mapToGlobal(point)), pixel or QPoint(), QPoint(x, y),
        Qt.MouseButton.NoButton, modifiers, Qt.ScrollPhase.ScrollUpdate, False)
    QApplication.sendEvent(widget, event)
    return event


def scroll_area(qtbot, *, horizontal=False):
    area = QScrollArea()
    qtbot.addWidget(area)
    area.resize(360, 200)
    content = QWidget()
    content.setMinimumSize(1600 if horizontal else 280, 120 if horizontal else 1600)
    area.setWidget(content)
    area.setWidgetResizable(True)
    area.show()
    qtbot.waitUntil(lambda: (area.horizontalScrollBar() if horizontal
                            else area.verticalScrollBar()).isVisible())
    return area, content


@pytest.mark.parametrize('focused', [False, True])
@pytest.mark.parametrize('kind', [QTabBar, QComboBox, QSpinBox, QDoubleSpinBox,
                                  NumberSpinBox, NumberDoubleSpinBox, QDateTimeEdit,
                                  QSlider, QDial])
def test_native_and_shared_controls_never_change_from_wheel(qtbot, kind, focused):
    control = kind()
    qtbot.addWidget(control)
    if isinstance(control, (QTabBar, QComboBox)):
        for text in ('Clips', 'Recording', 'Audio', 'Camera'):
            control.addTab(text) if isinstance(control, QTabBar) else control.addItem(text)
        control.setCurrentIndex(1)
        value = control.currentIndex
    elif isinstance(control, QDateTimeEdit):
        value = control.dateTime
    else:
        control.setRange(0, 100)
        control.setValue(50)
        value = control.value
    control.resize(400, 50)
    control.show()
    if focused:
        control.setFocus()
    before = value()
    for modifiers in (Qt.KeyboardModifier.NoModifier, Qt.KeyboardModifier.ControlModifier,
                      Qt.KeyboardModifier.ShiftModifier):
        wheel(control, modifiers=modifiers)
        wheel(control, x=-120, y=0, modifiers=modifiers)
    assert value() == before


def test_wheel_over_profile_tabs_scrolls_page_without_switching_category(qtbot):
    area, content = scroll_area(qtbot)
    tabs = QTabBar(content)
    tabs.addTab('Clips')
    tabs.addTab('Recording')
    tabs.addTab('Audio')
    tabs.addTab('Camera')
    tabs.setGeometry(10, 10, 300, 40)
    tabs.show()
    position = tabs.mapTo(area, tabs.rect().center())
    QTest.wheelEvent(area.windowHandle(), position, QPoint(0, -120))
    assert tabs.currentIndex() == 0
    assert area.verticalScrollBar().value() > 0
    qtbot.keyClick(tabs, Qt.Key.Key_Right)
    assert tabs.currentIndex() == 1
    qtbot.mouseClick(tabs, Qt.MouseButton.LeftButton, pos=tabs.tabRect(2).center())
    assert tabs.currentIndex() == 2


def test_number_field_child_routes_to_scrollbar_without_changing_value(qtbot):
    area, content = scroll_area(qtbot)
    spin = NumberSpinBox(content)
    spin.setValue(50)
    spin.show()
    spin.lineEdit().setFocus()
    wheel(spin.lineEdit())
    assert spin.value() == 50
    assert area.verticalScrollBar().value() > 0
    qtbot.mouseClick(spin._plus, Qt.MouseButton.LeftButton)
    assert spin.value() == 51


def test_hidden_scrollbar_disables_scrolling(qtbot):
    area, _content = scroll_area(qtbot)
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    assert not area.verticalScrollBar().isVisible()
    wheel(area.viewport())
    assert area.verticalScrollBar().value() == 0


def test_trackpad_pixel_scrolling_reaches_visible_scrollbar(qtbot):
    area, _content = scroll_area(qtbot)
    wheel(area.viewport(), y=0, pixel=QPoint(0, -24))
    assert area.verticalScrollBar().value() > 0
    before = area.verticalScrollBar().value()
    wheel(area.verticalScrollBar(), y=0, pixel=QPoint(0, -24))
    assert area.verticalScrollBar().value() > before
    # Sub-step pixel input must be consumed without recursively forwarding
    # another pixel-only event to the same scrollbar.
    area.verticalScrollBar().setSingleStep(100_000)
    wheel(area.verticalScrollBar(), y=0, pixel=QPoint(0, -1))


def test_popup_without_scrollbar_does_not_scroll_underlying_page(qtbot):
    area, content = scroll_area(qtbot)
    popup = QWidget(content, Qt.WindowType.Popup)
    qtbot.addWidget(popup)
    popup.show()
    wheel(popup)
    assert area.verticalScrollBar().value() == 0
    popup.close()


@pytest.mark.parametrize('horizontal_delta', [False, True])
def test_visible_horizontal_scrollbar_still_scrolls(qtbot, horizontal_delta):
    area, _content = scroll_area(qtbot, horizontal=True)
    wheel(area.viewport(), x=-120 if horizontal_delta else 0,
          y=0 if horizontal_delta else -120)
    assert area.horizontalScrollBar().value() > 0


def test_list_scrolls_without_changing_selection(qtbot):
    items = QListWidget()
    qtbot.addWidget(items)
    items.addItems([f'Profile {i}' for i in range(100)])
    items.resize(300, 200)
    items.setCurrentRow(0)
    items.show()
    qtbot.waitUntil(items.verticalScrollBar().isVisible)
    wheel(items.viewport())
    assert items.verticalScrollBar().value() > 0
    assert items.currentRow() == 0


def test_open_dropdown_scrolls_list_without_committing_a_selection(qtbot):
    combo = QComboBox()
    qtbot.addWidget(combo)
    combo.addItems([f'App {i}' for i in range(100)])
    combo.setMaxVisibleItems(5)
    combo.show()
    combo.showPopup()
    qtbot.waitUntil(combo.view().verticalScrollBar().isVisible)
    wheel(combo.view().viewport())
    assert combo.view().verticalScrollBar().value() > 0
    assert combo.currentIndex() == 0
    combo.hidePopup()


def test_ctrl_wheel_does_not_zoom_text(qtbot):
    text = QTextEdit()
    qtbot.addWidget(text)
    text.setPlainText('\n'.join(f'Line {i}' for i in range(100)))
    text.resize(300, 200)
    text.show()
    size = text.fontPointSize()
    wheel(text.viewport(), modifiers=Qt.KeyboardModifier.ControlModifier)
    assert text.fontPointSize() == size
    assert text.verticalScrollBar().value() > 0


def test_nested_area_at_boundary_scrolls_visible_parent(qtbot):
    outer, content = scroll_area(qtbot)
    inner = QListWidget(content)
    inner.addItems([str(i) for i in range(100)])
    inner.resize(200, 100)
    inner.show()
    qtbot.waitUntil(inner.verticalScrollBar().isVisible)
    inner.verticalScrollBar().setValue(inner.verticalScrollBar().maximum())
    wheel(inner.viewport())
    assert outer.verticalScrollBar().value() > 0


def test_custom_timeline_and_crop_have_no_hidden_wheel_actions(qtbot):
    from ui.clip_viewer import TrimSlider
    from ui.customize_page import _IconCropWidget
    timeline = TrimSlider(30_000)
    qtbot.addWidget(timeline)
    timeline.set_zoom(2, 0.5)
    timeline.show()
    before = (timeline.zoom_factor, timeline.view_start_pct)
    wheel(timeline)
    wheel(timeline, modifiers=Qt.KeyboardModifier.ShiftModifier)
    assert (timeline.zoom_factor, timeline.view_start_pct) == before
    crop = _IconCropWidget(QPixmap(400, 400), 64)
    qtbot.addWidget(crop)
    crop.show()
    before = crop._crop_rect.size()
    wheel(crop)
    assert crop._crop_rect.size() == before
