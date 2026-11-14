"""Native Qt controls should use immediate hover and popup effects."""

from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QPushButton, QStyle, QWidget

from ui.app_style import apply_app_style


def test_app_style_disables_native_animation_effects(qapp, qtbot):
    apply_app_style(qapp)
    assert not qapp.isEffectEnabled(Qt.UIEffect.UI_General)
    assert not qapp.isEffectEnabled(Qt.UIEffect.UI_AnimateMenu)
    assert not qapp.isEffectEnabled(Qt.UIEffect.UI_FadeTooltip)
    assert qapp.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) == 0

    window = QWidget()
    qtbot.addWidget(window)
    button = QPushButton('Apply', window)
    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    window.show()
    qtbot.mouseMove(button)
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    assert clicked == [True]
    assert not button.findChildren(QAbstractAnimation)
