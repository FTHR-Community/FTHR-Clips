"""Shared editable numeric fields with compact, inset minus/plus buttons."""
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QAbstractSpinBox, QSpinBox, QDoubleSpinBox
from ui.icons import IconButton
from ui.style import Colors, Fonts, Sizes, set_theme_style


class _NumberStepper:
    """Keyboard-editable value with inset minus/plus controls at the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('fthrNumberSpin')
        self.setProperty('fthrStepper', True)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setFixedHeight(34)
        self.lineEdit().setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._minus = IconButton('decrease.png', self, size=12, label='Decrease')
        self._minus.setObjectName('fthrStepDown')
        self._minus.setToolTip('Decrease')
        self._minus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._minus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._minus.clicked.connect(self.stepDown)

        self._plus = IconButton('increase.png', self, size=12, label='Increase')
        self._plus.setObjectName('fthrStepUp')
        self._plus.setToolTip('Increase')
        self._plus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._plus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._plus.clicked.connect(self.stepUp)

        self.valueChanged.connect(self._sync_step_buttons)
        self._sync_step_buttons()
        self._apply_style()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        button_w = 22
        button_h = 22
        inset = 5
        gap = 3
        y = (self.height() - button_h) // 2
        self._minus.setGeometry(
            self.width() - inset - 2 * button_w - gap, y, button_w, button_h)
        self._plus.setGeometry(
            self.width() - inset - button_w, y, button_w, button_h)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and hasattr(self, '_minus'):
            self._sync_step_buttons()

    def setRange(self, minimum, maximum):
        super().setRange(minimum, maximum)
        self._sync_step_buttons()

    def setValue(self, value):
        super().setValue(value)
        self._sync_step_buttons()

    def setMinimum(self, minimum):
        super().setMinimum(minimum)
        self._sync_step_buttons()

    def setMaximum(self, maximum):
        super().setMaximum(maximum)
        self._sync_step_buttons()

    def _sync_step_buttons(self, *_args):
        enabled = self.isEnabled()
        self._minus.setEnabled(enabled and self.value() > self.minimum())
        self._plus.setEnabled(enabled and self.value() < self.maximum())

    def _apply_style(self):
        set_theme_style(self, lambda: (f'''
            QAbstractSpinBox[fthrStepper="true"] {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_BODY}px;
                font-weight: 600;
                letter-spacing: 0.8px;
                padding: 4px 58px 4px 10px;
                selection-background-color: {Colors.ACCENT};
                selection-color: {Colors.BG};
            }}
            QAbstractSpinBox[fthrStepper="true"]:hover,
            QAbstractSpinBox[fthrStepper="true"]:focus {{
                border-color: {Colors.ACCENT};
            }}
            QAbstractSpinBox[fthrStepper="true"]:disabled {{
                color: {Colors.TEXT_MUTED};
                background-color: {Colors.SURFACE_1};
                border-color: {Colors.BORDER};
            }}
            QPushButton#fthrStepDown,
            QPushButton#fthrStepUp {{
                background-color: {Colors.SURFACE_1};
                border: none;
                border-radius: {Sizes.RADIUS_SM}px;
                color: {Colors.ACCENT};
                font-family: {Fonts.DISPLAY};
                font-size: 14px;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton#fthrStepDown:hover,
            QPushButton#fthrStepUp:hover {{
                background-color: {Colors.CARD_BG_HI};
                color: {Colors.TEXT};
            }}
            QPushButton#fthrStepDown:pressed,
            QPushButton#fthrStepUp:pressed {{
                background-color: {Colors.TEXT};
                color: {Colors.BG};
            }}
            QPushButton#fthrStepDown:disabled,
            QPushButton#fthrStepUp:disabled {{
                color: {Colors.TEXT_MUTED};
                background-color: {Colors.SURFACE_1};
            }}
        '''))


class NumberSpinBox(_NumberStepper, QSpinBox):
    """Integer values such as clip length and bitrate."""


class NumberDoubleSpinBox(_NumberStepper, QDoubleSpinBox):
    """Decimal values such as camera position and size."""
