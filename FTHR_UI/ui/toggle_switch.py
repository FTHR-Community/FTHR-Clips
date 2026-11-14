"""An immediate, keyboard-accessible settings switch."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QCheckBox
from ui.style import Colors


class ToggleSwitch(QCheckBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 28)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.toggled.connect(self.update)

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.isChecked() and self.isEnabled()
        painter.setPen(QPen(QColor(Colors.ACCENT if active or self.hasFocus()
                                   else Colors.BORDER_HI), 1))
        painter.setBrush(QColor(Colors.ACCENT_SOFT if active else Colors.SURFACE_1))
        painter.drawRoundedRect(1, 4, 42, 20, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(Colors.ACCENT if active else Colors.TEXT_MUTED))
        painter.drawEllipse(26 if self.isChecked() else 5, 8, 12, 12)
