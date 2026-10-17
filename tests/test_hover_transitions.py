"""Hover easing, reversal and interaction cleanup."""
import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QListWidget, QMenu, QPushButton, QSlider

from ui.hover_transition import HoverTransitions


@pytest.fixture
def hover_filter(qapp):
    # App-style tests may already have installed the global filter. Test one
    # filter at a time so two overlays do not capture each other.
    installed = getattr(qapp, '_hover_transitions', None)
    if installed is not None:
        qapp.removeEventFilter(installed)
    previous_sheet = qapp.styleSheet()
    qapp.setStyleSheet('')
    previous_motion = qapp.property('fthrReduceMotion')
    qapp.setProperty('fthrReduceMotion', False)
    transitions = HoverTransitions(qapp)
    yield transitions
    qapp.removeEventFilter(transitions)
    transitions.deleteLater()
    qapp.setProperty('fthrReduceMotion', previous_motion)
    qapp.setStyleSheet(previous_sheet)
    if installed is not None:
        qapp.installEventFilter(installed)


def enter(control):
    # Drive Qt hover state directly; the offscreen platform's cursor position
    # can be left over from another window earlier in the full suite.
    fade = getattr(control, '_hover_fade', None)
    if fade is not None:
        fade.dispose()
    control.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    QApplication.sendEvent(control, QEvent(QEvent.Type.Enter))
    move(control, QPoint(20, 20))


def leave(control):
    control.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    QApplication.sendEvent(control, QEvent(QEvent.Type.Leave))
    move(control, QPoint(-10, -10))


def move(control, position):
    event = QMouseEvent(QEvent.Type.MouseMove, QPointF(position), QPointF(position),
                        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                        Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(control, event)


def test_hover_fades_between_colors_and_releases_overlay(qtbot, hover_filter):
    button = QPushButton('')
    qtbot.addWidget(button)
    button.setStyleSheet('QPushButton { background: #0000ff; border: none; } '
                        'QPushButton:hover { background: #ff0000; }')
    button.resize(100, 40)
    button.show()
    enter(button)
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
    button = QPushButton('')
    qtbot.addWidget(button)
    button.setStyleSheet('QPushButton { background: #0000ff; border: none; } '
                        'QPushButton:hover { background: #ff0000; }')
    button.resize(100, 40)
    button.show()
    enter(button)
    first = button._hover_fade
    first.animation.setCurrentTime(60)
    before = button.grab().toImage().pixelColor(20, 20)
    leave(button)
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
    QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
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
    qtbot.mouseMove(menu, menu.actionGeometry(first).center())
    qtbot.mouseMove(menu, menu.actionGeometry(second).center())
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
    items.viewport().setMouseTracking(True)
    move(items.viewport(), items.visualItemRect(items.item(0)).center())
    move(items.viewport(), items.visualItemRect(items.item(1)).center())
    assert items.viewport()._hover_fade is not None
    slider = QSlider(Qt.Orientation.Horizontal)
    qtbot.addWidget(slider)
    slider.setMouseTracking(True)
    slider.resize(200, 30)
    slider.show()
    move(slider, QPoint(100, 15))
    assert slider._hover_fade is not None
    assert slider._hover_fade.animation.duration() == 120
