"""A wheel event applies its complete scroll delta in the same turn."""

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QScrollArea, QWidget


def wheel(angle=-120, pixel=0):
    return QWheelEvent(QPointF(40, 40), QPointF(40, 40), QPoint(0, pixel),
                       QPoint(0, angle), Qt.MouseButton.NoButton,
                       Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)


def test_native_wheel_has_no_intermediate_positions(qtbot):
    area = QScrollArea()
    content = QWidget()
    content.setFixedSize(400, 6000)
    area.setWidget(content)
    area.resize(450, 600)
    qtbot.addWidget(area)
    area.show()
    bar = area.verticalScrollBar()
    values = []
    bar.valueChanged.connect(values.append)

    area.wheelEvent(wheel())
    first = bar.value()
    assert first > 0
    assert values == [first]
    area.wheelEvent(wheel())
    assert bar.value() == first * 2
    area.wheelEvent(wheel(120))
    assert bar.value() == first
    qtbot.wait(100)
    assert values == [first, first * 2, first]
