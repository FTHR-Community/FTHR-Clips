"""Hover easing, reversal and interaction cleanup."""
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent, QMouseEvent
from PySide6.QtWidgets import (
    QApplication, QListWidget, QMenu, QPushButton, QSlider, QStyle,
)

from ui.hover_transition import HoverTransitions


def _send_enter(widget):
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    point = QPointF(widget.rect().center())
    QApplication.sendEvent(widget, QEnterEvent(point, point, point))


def _send_leave(widget):
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))


def _send_mouse_move(widget, pos):
    local = QPointF(pos)
    global_pos = QPointF(widget.mapToGlobal(pos))
    widget.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    QApplication.sendEvent(
        widget,
        QMouseEvent(
            QEvent.Type.MouseMove, local, local, global_pos,
            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier),
    )
    QApplication.processEvents()


class _DeterministicHoverButton(QPushButton):
    """Make the requested WA_UnderMouse state visible to the offscreen style."""

    def initStyleOption(self, option):
        super().initStyleOption(option)
        if self.testAttribute(Qt.WidgetAttribute.WA_UnderMouse):
            option.state |= QStyle.StateFlag.State_MouseOver
        else:
            option.state &= ~QStyle.StateFlag.State_MouseOver


def _hover_button(qtbot):
    button = _DeterministicHoverButton('')
    qtbot.addWidget(button)
    button.resize(100, 40)
    button.setStyleSheet(
        'QPushButton { background: #0000ff; border: none; } '
        'QPushButton:hover { background: #ff0000; }')
    button.show()
    button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    _send_enter(button)
    return button


@pytest.fixture
def hover_filter(qapp):
    existing = getattr(qapp, '_hover_transitions', None)
    if existing is not None:
        qapp.removeEventFilter(existing)
    transitions = HoverTransitions(qapp)
    yield transitions
    qapp.removeEventFilter(transitions)
    transitions.deleteLater()
    if existing is not None:
        qapp.installEventFilter(existing)


def test_hover_fades_between_colors_and_releases_overlay(qtbot, hover_filter):
    button = _hover_button(qtbot)
    fade = button._hover_fade
    assert fade is not None
    assert fade.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert fade._frame.toImage().pixelColor(20, 20).name() == '#0000ff'
    fade.animation.setCurrentTime(60)
    color = button.grab().toImage().pixelColor(20, 20)
    assert 120 <= color.red() <= 135 and 120 <= color.blue() <= 135
    qtbot.waitUntil(lambda: button._hover_fade is None)
    assert button.grab().toImage().pixelColor(20, 20).name() == '#ff0000'


def test_hover_reversal_starts_from_visible_frame_and_clicks_stay_immediate(qtbot, hover_filter):
    button = _hover_button(qtbot)
    first = button._hover_fade
    first.animation.setCurrentTime(60)
    before = button.grab().toImage().pixelColor(20, 20)
    _send_leave(button)
    second = button._hover_fade
    assert second is not first
    assert not first.isVisible()
    assert second._frame.toImage().pixelColor(20, 20) == before
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert button._hover_fade is None


@pytest.mark.parametrize('action', ['hide', 'resize', 'disable'])
def test_hover_cleanup_on_widget_changes(qtbot, hover_filter, action):
    button = QPushButton('Test')
    qtbot.addWidget(button)
    button.show()
    _send_enter(button)
    assert button._hover_fade is not None
    if action == 'hide':
        button.hide()
    elif action == 'resize':
        button.resize(160, 60)
    else:
        button.setEnabled(False)
    assert button._hover_fade is None


def test_menu_item_changes_fade_only_the_affected_rows(qtbot, hover_filter):
    menu = QMenu()
    qtbot.addWidget(menu)
    first = menu.addAction('First')
    second = menu.addAction('Second')
    menu.addAction('Third')
    menu.show()
    _send_mouse_move(menu, menu.actionGeometry(first).center())
    _send_mouse_move(menu, menu.actionGeometry(second).center())
    fade = menu._hover_fade
    assert fade is not None
    assert fade.geometry() == menu.actionGeometry(first).united(menu.actionGeometry(second))
    menu.hide()
    assert menu._hover_fade is None


def test_dropdown_rows_and_slider_subcontrols_get_short_fades(qtbot, hover_filter):
    items = QListWidget()
    qtbot.addWidget(items)
    items.setMouseTracking(True)
    items.addItems(['First', 'Second', 'Third'])
    items.show()
    _send_mouse_move(items.viewport(), items.visualItemRect(items.item(0)).center())
    _send_mouse_move(items.viewport(), items.visualItemRect(items.item(1)).center())
    assert items.viewport()._hover_fade is not None
    slider = QSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMouseTracking(True)
    slider.resize(200, 30)
    slider.show()
    _send_mouse_move(slider, QPoint(100, 15))
    assert slider._hover_fade is not None
    assert slider._hover_fade.animation.duration() == 120
