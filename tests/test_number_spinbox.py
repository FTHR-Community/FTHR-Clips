import pytest
from PySide6.QtCore import Qt, QSignalBlocker
from ui.number_spinbox import NumberSpinBox, NumberDoubleSpinBox


@pytest.mark.parametrize('kind', [NumberSpinBox, NumberDoubleSpinBox])
def test_inset_stepper_keeps_keyboard_limits_and_enabled_state(qtbot, kind):
    spin = kind()
    qtbot.addWidget(spin)
    spin.setRange(0, 2)
    spin.setSingleStep(1)
    spin.resize(220, 34)
    spin.show()
    qtbot.wait(20)
    assert not spin._minus.isEnabled()
    assert spin._plus.isEnabled()
    for button in (spin._minus, spin._plus):
        assert not button.icon().isNull()
        assert button.height() < spin.height()
        assert button.geometry().top() >= 5
        assert button.geometry().right() < spin.width() - 4
    qtbot.mouseClick(spin._plus, Qt.MouseButton.LeftButton)
    assert spin.value() == 1
    with QSignalBlocker(spin):
        spin.setValue(0)
    assert not spin._minus.isEnabled()
    with QSignalBlocker(spin):
        spin.setValue(1)
    assert spin._minus.isEnabled()
    spin.setFocus()
    qtbot.keyClick(spin, Qt.Key.Key_Up)
    assert spin.value() == 2
    assert not spin._plus.isEnabled()
    spin.setEnabled(False)
    spin.setEnabled(True)
    assert not spin._plus.isEnabled()
    assert spin._minus.isEnabled()
    spin.setMaximum(3)
    assert spin._plus.isEnabled()
    qtbot.mouseClick(spin._minus, Qt.MouseButton.LeftButton)
    assert spin.value() == 1
