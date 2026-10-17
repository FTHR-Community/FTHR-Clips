"""Frameless input and message dialogs matching the application theme."""

from __future__ import annotations

from ui.style import set_theme_style

import sys
from ui.icons import IconButton

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.style import (
    Colors,
    Fonts,
    Sizes,
    button_outline_qss,
    button_primary_qss,
    label_body,
    label_uppercase,
)
from ui.frameless_window_windows import start_system_move


class _DialogHeader(QFrame):
    """Square title bar that also provides drag behaviour."""

    def __init__(self, dialog: QDialog, title: str):
        super().__init__(dialog)
        self._dialog = dialog
        self._drag_offset: QPoint | None = None
        self.setObjectName('fthrDialogHeader')
        self.setFixedHeight(38)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 6, 0)
        layout.setSpacing(0)

        title_label = QLabel(title)
        title_label.setObjectName('fthrDialogTitle')
        set_theme_style(title_label, lambda: (label_uppercase(
            Colors.TEXT, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        layout.addWidget(title_label)
        layout.addStretch(1)

        close = IconButton('close.png', label='Close')
        close.setObjectName('fthrDialogClose')
        close.setFixedSize(30, 30)
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip('Close')
        close.clicked.connect(dialog.reject)
        layout.addWidget(close)

    def mousePressEvent(self, event):  # noqa: N802 - Qt API name
        if event.button() == Qt.MouseButton.LeftButton:
            if (sys.platform == 'win32'
                    and start_system_move(self._dialog.windowHandle())):
                self._drag_offset = None
                event.accept()
                return
            self._drag_offset = (
                event.globalPosition().toPoint()
                - self._dialog.frameGeometry().topLeft())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt API name
        if (self._drag_offset is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._dialog.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt API name
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class FthrDialog(QDialog):
    """Base modal dialog with an FTHR title bar and square black shell."""

    def __init__(self, title: str, parent: QWidget | None = None,
                 *, width: int = 360):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName('fthrDialog')
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        set_theme_style(self, lambda: (f'''
            QDialog#fthrDialog {{
                background-color: {Colors.BG};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QFrame#fthrDialogHeader {{
                background-color: {Colors.SURFACE_2};
                border: none;
                border-bottom: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QLabel#fthrDialogTitle {{
                color: {Colors.TEXT};
                background: transparent;
                font-family: {Fonts.DISPLAY};
            }}
            QPushButton#fthrDialogClose {{
                background: transparent;
                border: none;
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.BODY};
                font-size: 20px;
                padding: 0px;
            }}
            QPushButton#fthrDialogClose:hover {{
                background: {Colors.ERROR};
                color: {Colors.TEXT};
            }}
            QLabel {{
                color: {Colors.TEXT};
                background: transparent;
                font-family: {Fonts.BODY};
            }}
            QLineEdit#fthrDialogInput {{
                background: {Colors.SURFACE_1};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                padding: 7px 9px;
                selection-background-color: {Colors.ACCENT};
                selection-color: {Colors.BG};
            }}
            QLineEdit#fthrDialogInput:focus {{ border-color: {Colors.ACCENT}; }}
        '''))

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(_DialogHeader(self, title))

        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(16, 16, 16, 8)
        self.body_layout.setSpacing(10)
        root.addLayout(self.body_layout)

        self.action_layout = QHBoxLayout()
        self.action_layout.setContentsMargins(16, 8, 16, 14)
        self.action_layout.setSpacing(8)
        root.addLayout(self.action_layout)


def install_fthr_titlebar(dialog: QDialog, title: str) -> None:
    """Replace a legacy QDialog title bar while preserving its body layout."""
    layout = dialog.layout()
    if layout is None:
        return
    dialog.setWindowFlags(
        dialog.windowFlags()
        | Qt.WindowType.Dialog
        | Qt.WindowType.FramelessWindowHint)
    dialog.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    set_theme_style(dialog, lambda dialog=dialog: (dialog.styleSheet() + f'''
        QFrame#fthrDialogHeader {{
            background: {Colors.SURFACE_2};
            border: none;
            border-bottom: 1px solid {Colors.BORDER};
            border-radius: {Sizes.RADIUS_MD}px;
        }}
        QPushButton#fthrDialogClose {{
            background: transparent;
            border: none;
            border-radius: {Sizes.RADIUS_MD}px;
            color: {Colors.TEXT_DIM};
            font-family: {Fonts.BODY};
            font-size: 20px;
            padding: 0px;
        }}
        QPushButton#fthrDialogClose:hover {{
            background: {Colors.ERROR};
            color: {Colors.TEXT};
        }}
    '''))
    layout.insertWidget(0, _DialogHeader(dialog, title))


class FthrInputDialog(FthrDialog):
    """Single-line text input with a custom title bar."""

    def __init__(self, title: str, label: str, text: str = '', parent=None):
        super().__init__(title, parent, width=360)
        prompt = QLabel(label)
        set_theme_style(prompt, lambda: (label_uppercase(
            Colors.TEXT, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        self.body_layout.addWidget(prompt)

        self.text_edit = QLineEdit(text)
        self.text_edit.setObjectName('fthrDialogInput')
        self.text_edit.setMinimumHeight(32)
        self.body_layout.addWidget(self.text_edit)
        self.text_edit.returnPressed.connect(self.accept)

        self.action_layout.addStretch(1)
        cancel = QPushButton('Cancel')
        set_theme_style(cancel, lambda: (button_outline_qss()))
        cancel.clicked.connect(self.reject)
        self.action_layout.addWidget(cancel)
        ok = QPushButton('OK')
        set_theme_style(ok, lambda: (button_primary_qss()))
        ok.clicked.connect(self.accept)
        self.action_layout.addWidget(ok)
        self._ok_button = ok

        self.text_edit.setFocus()
        self.text_edit.selectAll()

    @classmethod
    def get_text(cls, parent, title: str, label: str, *, text: str = '') -> tuple[str, bool]:
        dialog = cls(title, label, text, parent)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.text_edit.text(), accepted


class FthrMessageDialog(FthrDialog):
    """Simple information, warning, and confirmation dialog."""

    _live_non_modal: list['FthrMessageDialog'] = []

    def __init__(self, title: str, message: str, parent=None, *, question=False):
        super().__init__(title, parent, width=360 if question else 340)
        copy = QLabel(message)
        copy.setWordWrap(True)
        set_theme_style(copy, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY_L)))
        self.body_layout.addWidget(copy)

        if question:
            self.action_layout.addStretch(1)
            no = QPushButton('No')
            set_theme_style(no, lambda: (button_outline_qss()))
            no.clicked.connect(self.reject)
            self.action_layout.addWidget(no)
            yes = QPushButton('Yes')
            set_theme_style(yes, lambda: (button_primary_qss()))
            yes.clicked.connect(self.accept)
            self.action_layout.addWidget(yes)
            no.setFocus()
        else:
            self.action_layout.addStretch(1)
            ok = QPushButton('OK')
            set_theme_style(ok, lambda: (button_primary_qss()))
            ok.clicked.connect(self.accept)
            self.action_layout.addWidget(ok)

    @classmethod
    def information(cls, parent, title: str, message: str) -> None:
        dialog = cls(title, message, parent)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        cls._live_non_modal.append(dialog)
        dialog.finished.connect(
            lambda _code, d=dialog: cls._live_non_modal.remove(d)
            if d in cls._live_non_modal else None)
        dialog.show()

    @classmethod
    def warning(cls, parent, title: str, message: str) -> None:
        dialog = cls(title, message, parent)
        dialog.setModal(False)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        cls._live_non_modal.append(dialog)
        dialog.finished.connect(
            lambda _code, d=dialog: cls._live_non_modal.remove(d)
            if d in cls._live_non_modal else None)
        dialog.show()

    @classmethod
    def question(cls, parent, title: str, message: str) -> bool:
        return cls(title, message, parent, question=True).exec() == QDialog.DialogCode.Accepted


__all__ = [
    'FthrDialog',
    'FthrInputDialog',
    'FthrMessageDialog',
    'install_fthr_titlebar',
]
