"""Static search destination emphasis without touching a control's theme or layout."""
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ui.style import Colors


class SettingsSearchHighlight(QWidget):
    def __init__(self, target):
        super().__init__(target)
        self.setObjectName('settingsSearchHighlight')
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        target.installEventFilter(self)
        self.setGeometry(target.rect())
        self.show()
        self.raise_()

    def eventFilter(self, target, event):
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(target.rect())
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = QColor(Colors.ACCENT)
        painter.setPen(QPen(accent, 2))
        accent.setAlpha(24)
        painter.setBrush(accent)
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 5, 5)

    def clear(self):
        self.parentWidget().removeEventFilter(self)
        self.hide()
        self.deleteLater()
