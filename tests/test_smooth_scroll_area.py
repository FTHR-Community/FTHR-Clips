from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QWidget

from ui.smooth_scroll_area import SmoothScrollArea


def wheel(angle=-120, pixel=0):
    return QWheelEvent(QPointF(40, 40), QPointF(40, 40), QPoint(0, pixel),
                       QPoint(0, angle), Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def make_area(qtbot):
    area = SmoothScrollArea()
    content = QWidget()
    content.setFixedSize(400, 6000)
    area.setWidget(content)
    area.resize(450, 600)
    qtbot.addWidget(area)
    area.show()
    return area


def test_wheel_input_accumulates_and_moves_through_intermediate_positions(qapp, qtbot):
    area = make_area(qtbot)
    positions = []
    area.verticalScrollBar().valueChanged.connect(positions.append)
    area.wheelEvent(wheel())
    first_target = area._scroll_target
    area.wheelEvent(wheel())
    assert area._scroll_target == first_target * 2
    qtbot.waitUntil(lambda: not area._scroll_timer.isActive(), timeout=2000)
    assert area.verticalScrollBar().value() == round(first_target * 2)
    assert len(positions) > 3
    assert positions == sorted(positions)


def test_manual_scroll_and_trackpad_interrupt_pending_wheel_motion(qapp, qtbot):
    area = make_area(qtbot)
    area.wheelEvent(wheel())
    area.verticalScrollBar().setValue(800)
    assert not area._scroll_timer.isActive()
    assert area._scroll_target == 800
    area.wheelEvent(wheel())
    area.wheelEvent(wheel(angle=0, pixel=-20))
    assert not area._scroll_timer.isActive()
    qtbot.wait(200)
    assert not area._scroll_timer.isActive()


def test_reverse_input_and_bounds_do_not_leave_an_animation_running(qapp, qtbot):
    area = make_area(qtbot)
    area.wheelEvent(wheel(angle=-12000))
    assert area._scroll_target == area.verticalScrollBar().maximum()
    area.wheelEvent(wheel(angle=12000))
    qtbot.waitUntil(lambda: not area._scroll_timer.isActive(), timeout=2000)
    assert area.verticalScrollBar().value() == 0
