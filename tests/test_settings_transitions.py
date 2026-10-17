"""Settings motion must not move/repaint the live controls every frame."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QLineEdit, QWidget

from main import SlidingStackedWidget
from ui.shell_transitions import ShellPageStack


class CountedPage(QWidget):
    def __init__(self, color):
        super().__init__()
        self.color = QColor(color)
        self.paints = 0
        self.field = QLineEdit(self)
        self.field.setGeometry(10, 10, 120, 30)

    def paintEvent(self, event):
        self.paints += 1
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.color)


@pytest.mark.parametrize('stack_type', [ShellPageStack, SlidingStackedWidget])
@pytest.mark.parametrize('direction', [1, -1])
def test_static_transition_keeps_controls_still_and_pixels_full_resolution(
        qtbot, stack_type, direction):
    stack = stack_type()
    qtbot.addWidget(stack)
    pages = [CountedPage('red'), CountedPage('blue')]
    for page in pages:
        stack.addWidget(page)
    stack.resize(400, 300)
    stack.setCurrentIndex(0 if direction > 0 else 1)
    stack.show()
    qtbot.wait(20)
    target = 1 if direction > 0 else 0
    page = pages[target]
    page.field.setText('Unsaved setting')
    stack.setCurrentIndex(target)
    slide = stack._slide
    assert slide._surface is None  # QWidget-only pages need no Quick compositor.
    assert slide._live_incoming is None
    assert slide.testAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
    assert slide._incoming.devicePixelRatio() == stack.devicePixelRatioF()
    assert slide._incoming.deviceIndependentSize().toSize() == stack.size()
    slide.animation.stop()
    qtbot.wait(20)
    paints = page.paints
    for progress in (.1, .3, .5):
        slide._advance(progress)
        qtbot.wait(20)
        assert page.geometry() == stack.rect()
        assert page.paints == paints
    image = stack.grab().toImage()
    if stack_type is SlidingStackedWidget:
        before, after = (50, 150), (350, 150)
    else:
        before, after = (200, 60), (200, 260)
    dpr = image.devicePixelRatio()
    assert image.pixelColor(int(before[0] * dpr), int(before[1] * dpr)) == QColor('red')
    assert image.pixelColor(int(after[0] * dpr), int(after[1] * dpr)) == QColor('blue')
    stack._finish_slide()
    assert slide._incoming.isNull() and slide._outgoing.isNull()
    page.field.setFocus()
    qtbot.keyClicks(page.field, '!')
    assert page.field.text() == 'Unsaved setting!'


@pytest.mark.parametrize('stack_type', [ShellPageStack, SlidingStackedWidget])
def test_interruption_resize_and_hide_release_snapshots_without_losing_page_state(qtbot, stack_type):
    stack = stack_type()
    qtbot.addWidget(stack)
    pages = [CountedPage(color) for color in ('red', 'green', 'blue')]
    for page in pages:
        stack.addWidget(page)
        page.field.setText('Keep me')
    stack.resize(400, 300)
    stack.show()
    qtbot.wait(10)
    stack.setCurrentIndex(1)
    first = stack._slide
    first._advance(.4)
    stack.setCurrentIndex(2)
    assert first._incoming.isNull() and first._outgoing.isNull()
    second = stack._slide
    stack.resize(500, 350)
    assert stack._slide is None and second._incoming.isNull()
    assert pages[2].geometry() == stack.rect()
    stack.setCurrentIndex(0)
    third = stack._slide
    stack.hide()
    assert stack._slide is None and third._incoming.isNull()
    stack.show()
    assert stack.currentIndex() == 0
    assert pages[0].geometry() == stack.rect()
    assert all(page.field.text() == 'Keep me' for page in pages)
