"""Accordion settings for colors, icons, fonts, and sounds.

Use a static preview, lazy icon caches, and preloaded sound cues. Apply
updates the main theme in one QSS pass.
"""
from __future__ import annotations

from ui.style import set_theme_style

from ui.icons import IconButton

from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt, Signal, QRect, QPoint,
)
from PySide6.QtGui import (
    QPixmap, QColor, QPainter, QPen, QBrush, QFont, QCursor,
    QMouseEvent, QPaintEvent, QFontDatabase,
)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame, QColorDialog, QFileDialog, QSizePolicy, QGridLayout,
    QSlider,
)
from ui.style import (
    Colors, Fonts, Sizes, label_uppercase, label_body,
    button_primary_qss, button_outline_qss, button_secondary_qss,
    combo_qss, slider_qss, ThemedDropdownArrow, WheelSafeComboBox,
)
from core.theme_manager import (
    ThemeManager, DEFAULT_COLORS, CUSTOMIZABLE_ICONS,
    CUSTOMIZABLE_SOUNDS, SUPPORTED_IMAGE_FORMATS, SUPPORTED_SOUND_FORMATS,
    SUPPORTED_FONT_FORMATS, DEFAULT_CAPTURE_CARD_COLORS, DEFAULT_FONTS,
    CUSTOMIZABLE_COLOR_GROUPS, BUILTIN_FONT_FAMILIES,
)
from ui.sound_playback import SoundPlayback


def _default_icon_path(filename: str) -> Path:
    """Return the bundled source used by the live app for an icon key."""
    from ui.icons import bundled_icon_path
    return bundled_icon_path(filename)


def _default_icon_pixmap(filename: str, size: int) -> QPixmap:
    """Preview the same supplied artwork used by live controls."""
    from ui.icons import icon_pixmap
    return icon_pixmap(filename, size, bundled=True)


# Dark-themed stylesheet for the non-native QColorDialog
def _color_dialog_qss() -> str:
    return f"""
    QColorDialog {{
        background-color: {Colors.SURFACE_1};
    }}
    QColorDialog QWidget {{
        background-color: {Colors.SURFACE_1};
        color: {Colors.TEXT};
    }}
    QColorDialog QLabel {{
        color: {Colors.TEXT_DIM};
        font-family: {Fonts.BODY};
        font-size: {Fonts.SIZE_BODY}px;
        background: transparent;
    }}
    QColorDialog QGroupBox {{
        color: {Colors.ACCENT};
        font-family: {Fonts.DISPLAY};
        font-size: {Fonts.SIZE_LABEL}px;
        font-weight: 600;
        letter-spacing: {Fonts.TRACK_LABEL}px;
        border: 1px solid {Colors.BORDER};
        margin-top: 10px;
        padding-top: 14px;
    }}
    QColorDialog QGroupBox::title {{
        subcontrol-origin: margin;
        padding: 0 6px;
    }}
    QColorDialog QSpinBox {{
        background-color: {Colors.SURFACE_2};
        color: {Colors.TEXT};
        border: 1px solid {Colors.BORDER};
        padding: 2px 4px;
        font-family: {Fonts.BODY};
        font-size: {Fonts.SIZE_BODY}px;
        min-height: 20px;
    }}
    QColorDialog QSpinBox:focus {{
        border-color: {Colors.ACCENT};
    }}
    QColorDialog QSpinBox::up-button, QColorDialog QSpinBox::down-button {{
        background-color: {Colors.SURFACE_3};
        border: 1px solid {Colors.BORDER};
        width: 16px;
    }}
    QColorDialog QSpinBox::up-button:hover, QColorDialog QSpinBox::down-button:hover {{
        background-color: {Colors.BORDER_HI};
    }}
    QColorDialog QSpinBox::up-arrow {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 4px solid {Colors.TEXT_DIM};
    }}
    QColorDialog QSpinBox::down-arrow {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 4px solid {Colors.TEXT_DIM};
    }}
    QColorDialog QLineEdit {{
        background-color: {Colors.SURFACE_2};
        color: {Colors.TEXT};
        border: 1px solid {Colors.BORDER};
        padding: 4px 6px;
        font-family: {Fonts.BODY};
        font-size: {Fonts.SIZE_BODY}px;
        selection-background-color: {Colors.ACCENT_DIM};
        selection-color: {Colors.TEXT};
    }}
    QColorDialog QLineEdit:focus {{
        border-color: {Colors.ACCENT};
    }}
    QColorDialog QPushButton {{
        background-color: {Colors.SURFACE_3};
        color: {Colors.TEXT};
        border: 1px solid {Colors.BORDER};
        padding: 6px 14px;
        font-family: {Fonts.DISPLAY};
        font-weight: 600;
        font-size: {Fonts.SIZE_BODY}px;
        letter-spacing: 0px;
        min-width: 70px;
    }}
    QColorDialog QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
    QColorDialog QPushButton:pressed {{
        background-color: {Colors.SURFACE_2};
    }}
    QColorDialog QPushButton:default {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG};
        border-color: {Colors.ACCENT};
    }}
    QColorDialog QPushButton:default:hover {{
        background-color: {Colors.ACCENT_DIM};
        border-color: {Colors.ACCENT_DIM};
        color: {Colors.BG};
    }}
    QColorDialog QDialogButtonBox {{
        background: transparent;
    }}
    QColorDialog QFrame {{
        background-color: {Colors.SURFACE_1};
    }}
    """



def _inject_custom_titlebar(dlg, title: str):
    """Replace the native window title bar with an FTHR-styled one."""
    old_layout = dlg.layout()
    if not old_layout:
        return

    title_bar = QFrame()
    title_bar.setFixedHeight(36)
    set_theme_style(title_bar, lambda: (f'QFrame {{ background-color: {Colors.SURFACE_1};'
        f' border-bottom: 1px solid {Colors.BORDER}; }}'))
    tb_layout = QHBoxLayout(title_bar)
    tb_layout.setContentsMargins(12, 0, 6, 0)
    tb_layout.setSpacing(0)

    title_lbl = QLabel(title)
    set_theme_style(title_lbl, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;'
        f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
        f' letter-spacing: {Fonts.TRACK_LABEL}px;'
        f' background: transparent;'))
    tb_layout.addWidget(title_lbl)
    tb_layout.addStretch()

    close_btn = IconButton('close.png', label='Close')
    close_btn.setFixedSize(28, 28)
    close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    set_theme_style(close_btn, lambda: (f'QPushButton {{ background: transparent; border: none;'
        f' color: {Colors.TEXT_DIM}; font-size: 16px; font-weight: 600; }}'
        f' QPushButton:hover {{ background-color: {Colors.ERROR};'
        f' color: {Colors.TEXT}; }}'))
    close_btn.clicked.connect(dlg.reject)
    tb_layout.addWidget(close_btn)

    old_layout.insertWidget(0, title_bar)

    # Allow dragging the dialog by the title bar
    title_bar._drag_pos = None

    def _press(e):
        if e.button() == Qt.MouseButton.LeftButton:
            title_bar._drag_pos = e.globalPosition().toPoint() - dlg.frameGeometry().topLeft()

    def _move(e):
        if title_bar._drag_pos is not None:
            dlg.move(e.globalPosition().toPoint() - title_bar._drag_pos)

    def _release(e):
        title_bar._drag_pos = None

    title_bar.mousePressEvent = _press
    title_bar.mouseMoveEvent = _move
    title_bar.mouseReleaseEvent = _release


def _fthr_message_box(parent, title: str, message: str,
                       ok_cancel: bool = True) -> bool:
    """Custom styled message dialog matching the FTHR design language."""
    from PySide6.QtWidgets import QDialog

    dlg = QDialog(parent)
    dlg.setWindowFlags(
        Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
    )
    dlg.setFixedWidth(420)
    set_theme_style(dlg, lambda: (f'QDialog {{ background: {Colors.SURFACE_1};'
        f' border: 1px solid {Colors.BORDER_HI}; }}'))

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    # Title bar
    title_bar = QFrame()
    title_bar.setFixedHeight(36)
    set_theme_style(title_bar, lambda: (f'QFrame {{ background: {Colors.SURFACE_2};'
        f' border-bottom: 1px solid {Colors.BORDER}; }}'))
    tb_layout = QHBoxLayout(title_bar)
    tb_layout.setContentsMargins(12, 0, 6, 0)
    tb_layout.setSpacing(0)
    title_lbl = QLabel(title)
    set_theme_style(title_lbl, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;'
        f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
        f' letter-spacing: {Fonts.TRACK_LABEL}px;'
        f' background: transparent;'))
    tb_layout.addWidget(title_lbl)
    tb_layout.addStretch()
    close_btn = IconButton('close.png', label='Close')
    close_btn.setFixedSize(28, 28)
    close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    set_theme_style(close_btn, lambda: (f'QPushButton {{ background: transparent; border: none;'
        f' color: {Colors.TEXT_DIM}; font-size: 16px; font-weight: 600; }}'
        f' QPushButton:hover {{ background-color: {Colors.ERROR};'
        f' color: {Colors.TEXT}; }}'))
    close_btn.clicked.connect(dlg.reject)
    tb_layout.addWidget(close_btn)
    layout.addWidget(title_bar)

    # Body
    body = QFrame()
    set_theme_style(body, lambda: (f'QFrame {{ background: {Colors.SURFACE_1}; }}'))
    bl = QVBoxLayout(body)
    bl.setContentsMargins(20, 16, 20, 16)
    bl.setSpacing(12)
    msg_lbl = QLabel(message)
    msg_lbl.setWordWrap(True)
    set_theme_style(msg_lbl, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY}px;'
        f' font-family: {Fonts.BODY}; background: transparent;'))
    bl.addWidget(msg_lbl)

    # Buttons
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton('OK')
    ok_btn.setFixedWidth(80)
    ok_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    set_theme_style(ok_btn, lambda: (f'QPushButton {{ background: {Colors.ACCENT}; color: {Colors.BG};'
        f' border: none; padding: 6px 14px; font-family: {Fonts.DISPLAY};'
        f' font-weight: 600; font-size: {Fonts.SIZE_LABEL}px;'
        f' letter-spacing: 0px; }}'
        f' QPushButton:hover {{ background: {Colors.TEXT}; }}'))
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)

    if ok_cancel:
        cancel_btn = QPushButton('Cancel')
        cancel_btn.setFixedWidth(80)
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(cancel_btn, lambda: (f'QPushButton {{ background: transparent; color: {Colors.TEXT_DIM};'
            f' border: 1px solid {Colors.BORDER}; padding: 6px 14px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' font-size: {Fonts.SIZE_LABEL}px; letter-spacing: 0px; }}'
            f' QPushButton:hover {{ border-color: {Colors.TEXT};'
            f' color: {Colors.TEXT}; }}'))
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)

    bl.addLayout(btn_row)
    layout.addWidget(body)

    # Draggable title bar
    title_bar._drag_pos = None

    def _press(e):
        if e.button() == Qt.MouseButton.LeftButton:
            title_bar._drag_pos = e.globalPosition().toPoint() - dlg.frameGeometry().topLeft()

    def _move(e):
        if title_bar._drag_pos is not None:
            dlg.move(e.globalPosition().toPoint() - title_bar._drag_pos)

    def _release(e):
        title_bar._drag_pos = None

    title_bar.mousePressEvent = _press
    title_bar.mouseMoveEvent = _move
    title_bar.mouseReleaseEvent = _release

    from PySide6.QtWidgets import QDialog as _D
    return dlg.exec() == _D.DialogCode.Accepted


# Accordion Section — collapsible container matching fthrclips.com style

class _AccordionSection(QFrame):
    """Collapsible section with an immediate, stable expand/collapse."""

    def __init__(self, title: str, number: str = '', parent=None):
        super().__init__(parent)
        self.setObjectName('accordionSection')
        self._title = title
        self._expanded = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header (clickable trigger)
        self._header = QPushButton()
        self._header.setObjectName('accordionHeader')
        self._header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._header.setFixedHeight(Sizes.CAPTURE_CONTROL_H)
        self._header.clicked.connect(self.toggle)

        h_layout = QHBoxLayout(self._header)
        h_layout.setContentsMargins(16, 0, 16, 0)
        h_layout.setSpacing(12)

        if number:
            num_lbl = QLabel(number)
            set_theme_style(num_lbl, lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px;'
                f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                f' background: transparent;'))
            h_layout.addWidget(num_lbl)

        title_lbl = QLabel(title)
        set_theme_style(title_lbl, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'))
        h_layout.addWidget(title_lbl)
        h_layout.addStretch()

        self._indicator = ThemedDropdownArrow()
        h_layout.addWidget(self._indicator)

        layout.addWidget(self._header)

        # Body
        self._body = QWidget()
        self._body.setObjectName('accordionBody')
        self._body.setVisible(False)
        self._body_layout = QVBoxLayout(self._body)
        # Give controls a full spacing step around every edge.  The wrappers
        # inside the body used to paint the global pure-black canvas and made
        # right-aligned actions appear to touch the section border.
        self._body_layout.setContentsMargins(Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5, Sizes.SPACE_5)
        self._body_layout.setSpacing(8)
        layout.addWidget(self._body)
        self.refresh_theme()

    def add_content(self, widget: QWidget):
        # The application-wide QWidget rule paints the page canvas black.
        # Name each direct content wrapper so the accordion can explicitly
        # keep its expanded content on the raised gray surface instead.
        widget.setObjectName('accordionContent')
        self._body_layout.addWidget(widget)

    def add_layout(self, layout):
        self._body_layout.addLayout(layout)

    def clear_content(self):
        """Remove the current body widgets so theme styles can be rebuilt."""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                child_layout = item.layout()
                while child_layout.count():
                    child_item = child_layout.takeAt(0)
                    child_widget = child_item.widget()
                    if child_widget is not None:
                        child_widget.deleteLater()

    def refresh_theme(self):
        """Refresh the section shell and header after a theme change."""
        set_theme_style(self, lambda: (f'''
            QFrame#accordionSection {{
                background-color: {Colors.SURFACE_3};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
            QPushButton#accordionHeader {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-radius: {Sizes.RADIUS_CARD}px;
                text-align: left;
            }}
            QPushButton#accordionHeader:hover {{
                background-color: {Colors.BORDER_HI};
            }}
            QWidget#accordionBody,
            QWidget#accordionBody > QWidget#accordionContent {{
                background-color: transparent;
            }}
            QWidget#accordionBody QFrame#iconRow,
            QWidget#accordionBody QFrame#iconTintRow,
            QWidget#accordionBody QFrame#soundRow {{
                background-color: transparent;
                border: none;
            }}
        '''))
        for label in self._header.findChildren(QLabel):
            if label.text() == self._title:
                set_theme_style(label, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px;'
                    f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                    f' letter-spacing: {Fonts.TRACK_LABEL}px;'
                    f' background: transparent;'))
            else:
                set_theme_style(label, lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px;'
                    f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                    f' background: transparent;'))
        self._indicator.refresh_theme()

    def toggle(self):
        self._expanded = not self._expanded
        self._indicator.setExpanded(self._expanded)
        self._body.setVisible(self._expanded)

    def expand(self):
        if not self._expanded:
            self.toggle()

    def collapse(self):
        if self._expanded:
            self.toggle()

    @property
    def expanded(self) -> bool:
        return self._expanded


# Color Swatch — clickable color picker tile

class _ColorSwatch(QWidget):
    """Small clickable color tile that opens a QColorDialog on click."""

    color_changed = Signal(str, str)  # (token, new_hex)

    def __init__(self, token: str, label: str, hex_color: str, parent=None):
        super().__init__(parent)
        self.token = token
        self._label = label
        self._color = hex_color
        self.setFixedSize(96, 52)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setToolTip(f'{label}\nClick to change')

    def set_color(self, hex_color: str):
        self._color = hex_color
        self.update()

    def get_color(self) -> str:
        return self._color

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Swatch rectangle
        swatch_rect = QRect(0, 0, self.width(), 30)
        p.setBrush(QBrush(QColor(self._color)))
        p.setPen(QPen(QColor(Colors.BORDER_HI), 1))
        p.drawRoundedRect(swatch_rect.adjusted(1, 1, -1, -1), Sizes.RADIUS_MD, Sizes.RADIUS_MD)

        # Label text below
        p.setPen(QPen(QColor(Colors.TEXT_DIM)))
        p.setFont(QFont(Fonts.BODY_FAMILY, 7))
        text_rect = QRect(0, 33, self.width(), 16)
        p.drawText(text_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self._label)
        p.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            initial = QColor(self._color)
            dlg = QColorDialog(initial, self)
            dlg.setWindowFlags(
                Qt.WindowType.Dialog
                | Qt.WindowType.FramelessWindowHint
            )
            dlg.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel)
            dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
            set_theme_style(dlg, lambda: (_color_dialog_qss()
                + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'))
            _inject_custom_titlebar(dlg, f'Pick color: {self._label}')
            if dlg.exec():
                color = dlg.currentColor()
                self._color = color.name()
                self.update()
                self.color_changed.emit(self.token, self._color)


# Color Preview Mockup — miniature app UI that reflects current theme colors

class _ColorPreviewMockup(QFrame):
    """Miniature mockup of the app UI to preview color changes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(220)
        self.setObjectName('colorPreview')
        tm = ThemeManager()
        self._colors = tm.get_all_colors()
        self._rebuild()

    def update_colors(self, colors: dict[str, str]):
        from ui.style import refresh_theme_styles
        self._colors.clear()
        self._colors.update(colors)
        refresh_theme_styles(self)

    def _rebuild(self):
        # Clear existing children
        old = self.layout()
        if old:
            while old.count():
                item = old.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            QWidget().setLayout(old)

        c = self._colors
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Mini top bar
        top_bar = QFrame()
        top_bar.setFixedHeight(26)
        set_theme_style(top_bar, lambda: (f'background-color: {c["SHELL_BG"]};'
            f' border-top-left-radius: {Sizes.RADIUS_CARD}px;'
            f' border-top-right-radius: {Sizes.RADIUS_CARD}px;'
            f' border-bottom: 1px solid {c["SHELL_DIVIDER"]};'))
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(10, 0, 10, 0)
        logo_lbl = QLabel('FTHR')
        set_theme_style(logo_lbl, lambda: (f'color: {c["TEXT"]}; font-size: 10px; font-weight: 600;'
            f' font-family: {Fonts.DISPLAY}; letter-spacing: 0px;'
            f' background: transparent;'))
        tb_layout.addWidget(logo_lbl)
        tb_layout.addStretch()
        status = QLabel('Capturing')
        set_theme_style(status, lambda: (f'color: {c["ACCENT"]}; background: transparent; border: none;'
            f' font-size: 8px; font-weight: 600; padding: 0 2px;'
            f' font-family: {Fonts.DISPLAY}; letter-spacing: 0px;'))
        tb_layout.addWidget(status)
        layout.addWidget(top_bar)

        # The secondary shell surface is used by the app's status strips and
        # settings chrome. Keep it visible in the preview so it is obvious
        # that changing the token affects more than the main canvas.
        status_bar = QFrame()
        status_bar.setFixedHeight(20)
        set_theme_style(status_bar, lambda: (f'background-color: {c["SHELL_BG_2"]};'
            f' border-bottom: 1px solid {c["SHELL_DIVIDER"]};'))
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(10, 0, 10, 0)
        ready = QLabel('Ready')
        set_theme_style(ready, lambda: (f'color: {c["SUCCESS"]}; font-size: 8px; font-weight: 600;'
            f' font-family: {Fonts.DISPLAY}; background: transparent;'))
        sb_layout.addWidget(ready)
        sb_layout.addStretch()
        sb_hint = QLabel('Status strip')
        set_theme_style(sb_hint, lambda: (f'color: {c["TEXT_MUTED"]}; font-size: 7px;'
            f' font-family: {Fonts.BODY}; background: transparent;'))
        sb_layout.addWidget(sb_hint)
        layout.addWidget(status_bar)

        # Content area with cards
        content = QFrame()
        set_theme_style(content, lambda: (f'background-color: {c["BG"]};'
                             f' border-bottom-left-radius: {Sizes.RADIUS_CARD}px;'
                             f' border-bottom-right-radius: {Sizes.RADIUS_CARD}px;'))
        cl = QVBoxLayout(content)
        cl.setContentsMargins(10, 8, 10, 8)
        cl.setSpacing(Sizes.SPACE_3)

        # Mini clip cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(Sizes.SPACE_3)
        for i in range(3):
            card = QFrame()
            card.setFixedSize(80, 48)
            set_theme_style(card, lambda: (f'background-color: {c["CARD_BG"]};'
                f' border: 1px solid {c["CARD_BORDER"]};'
                f' border-radius: {Sizes.RADIUS_SM}px;'))
            card_l = QVBoxLayout(card)
            card_l.setContentsMargins(6, 4, 6, 4)
            thumb = QFrame()
            thumb.setFixedHeight(22)
            set_theme_style(thumb, lambda: (f'background-color: {c["SURFACE_2"]};'))
            card_l.addWidget(thumb)
            title = QLabel(f'clip_{i+1}.mp4')
            set_theme_style(title, lambda: (f'color: {c["TEXT_DIM"]}; font-size: 7px; background: transparent;'))
            card_l.addWidget(title)
            cards_row.addWidget(card)
        cards_row.addStretch()
        cl.addLayout(cards_row)

        # Mini button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(Sizes.SPACE_3)
        primary_btn = QLabel('Save clip')
        set_theme_style(primary_btn, lambda: (f'background-color: {c["ACCENT"]}; color: {c["BG"]};'
            f' font-size: 8px; font-weight: 600; padding: 4px 10px;'
            f' font-family: {Fonts.DISPLAY};'))
        btn_row.addWidget(primary_btn)
        outline_btn = QLabel('Export')
        set_theme_style(outline_btn, lambda: (f'background-color: transparent; color: {c["TEXT"]};'
            f' border: 1px solid {c["BORDER_HI"]};'
            f' font-size: 8px; font-weight: 600; padding: 4px 10px;'
            f' font-family: {Fonts.DISPLAY};'))
        btn_row.addWidget(outline_btn)
        btn_row.addStretch()
        cl.addLayout(btn_row)

        # Mini text samples
        text_row = QVBoxLayout()
        text_row.setSpacing(2)
        t1 = QLabel('Primary text sample')
        set_theme_style(t1, lambda: (f'color: {c["TEXT"]}; font-size: 9px; background: transparent;'))
        t2 = QLabel('Secondary text sample')
        set_theme_style(t2, lambda: (f'color: {c["TEXT_DIM"]}; font-size: 9px; background: transparent;'))
        t3 = QLabel('Muted text sample')
        set_theme_style(t3, lambda: (f'color: {c["TEXT_MUTED"]}; font-size: 9px; background: transparent;'))
        t4 = QLabel('Ghost text sample')
        set_theme_style(t4, lambda: (f'color: {c["TEXT_GHOST"]}; font-size: 9px; background: transparent;'))
        text_row.addWidget(t1)
        text_row.addWidget(t2)
        text_row.addWidget(t3)
        text_row.addWidget(t4)
        cl.addLayout(text_row)

        # Error / delete samples
        state_row = QHBoxLayout()
        state_row.setSpacing(8)
        err = QLabel('Error')
        set_theme_style(err, lambda: (f'color: {c["ERROR"]}; font-size: 8px; font-weight: 600;'
            f' border: 1px solid {c["ERROR"]}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'))
        state_row.addWidget(err)
        warning = QLabel('Warning')
        set_theme_style(warning, lambda: (f'color: {c["WARNING"]}; font-size: 8px; font-weight: 600;'
            f' border: 1px solid {c["WARNING"]}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'))
        state_row.addWidget(warning)
        success = QLabel('Success')
        set_theme_style(success, lambda: (f'color: {c["SUCCESS"]}; font-size: 8px; font-weight: 600;'
            f' border: 1px solid {c["SUCCESS"]}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'))
        state_row.addWidget(success)
        delbtn = QLabel('Delete')
        set_theme_style(delbtn, lambda: (f'color: {c["DELETE"]}; font-size: 8px; font-weight: 600;'
            f' border: 1px solid {c["DELETE"]}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'))
        state_row.addWidget(delbtn)
        state_row.addStretch()
        cl.addLayout(state_row)

        layout.addWidget(content, stretch=1)

    def update_single_color(self, token: str, hex_value: str):
        self.update_colors({**self._colors, token: hex_value})


# Icon Crop Dialog — crop imported image to match original icon dimensions

class _IconCropWidget(QWidget):
    """Crop area selector with free resize via corner handles and scroll wheel."""

    _HANDLE_R = 6  # corner handle radius in display px

    def __init__(self, pixmap: QPixmap, target_size: int, parent=None):
        super().__init__(parent)
        self._source = pixmap
        self._target_size = target_size

        # Start crop at 60 % of the smaller dimension, centered
        src_w, src_h = pixmap.width(), pixmap.height()
        init_size = max(target_size, int(min(src_w, src_h) * 0.6))
        init_size = min(init_size, min(src_w, src_h))
        cx, cy = src_w // 2 - init_size // 2, src_h // 2 - init_size // 2
        self._crop_rect = QRect(cx, cy, init_size, init_size)

        self._min_crop = max(target_size, 16)
        self._max_crop = min(src_w, src_h)

        self._dragging = False
        self._resizing = False
        self._resize_corner = -1  # 0=TL 1=TR 2=BL 3=BR
        self._drag_start = QPoint()
        self._rect_start = QRect()

        self._display_scale = 1.0
        self._offset_x = 0
        self._offset_y = 0
        self.setMinimumSize(200, 200)
        self.setMaximumSize(400, 400)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def get_cropped(self) -> QPixmap:
        cropped = self._source.copy(self._crop_rect)
        return cropped.scaled(
            self._target_size, self._target_size,
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    # painting

    def paintEvent(self, event: QPaintEvent):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        dw, dh = self.width(), self.height()
        src_w, src_h = self._source.width(), self._source.height()
        self._display_scale = min(dw / max(src_w, 1), dh / max(src_h, 1))

        scaled = self._source.scaled(
            int(src_w * self._display_scale),
            int(src_h * self._display_scale),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._offset_x = (dw - scaled.width()) // 2
        self._offset_y = (dh - scaled.height()) // 2
        p.drawPixmap(self._offset_x, self._offset_y, scaled)

        # Dim overlay outside crop
        overlay = QColor(Colors.BG)
        overlay.setAlpha(140)
        p.setBrush(QBrush(overlay))
        p.setPen(Qt.PenStyle.NoPen)
        cr = self._crop_to_display()
        p.drawRect(0, 0, dw, cr.top())
        p.drawRect(0, cr.bottom(), dw, dh - cr.bottom())
        p.drawRect(0, cr.top(), cr.left(), cr.height())
        p.drawRect(cr.right(), cr.top(), dw - cr.right(), cr.height())

        # Crop border
        accent = QColor(Colors.ACCENT)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, 2))
        p.drawRect(cr)

        # Corner handles
        r = self._HANDLE_R
        p.setBrush(QBrush(accent))
        p.setPen(Qt.PenStyle.NoPen)
        for cx, cy in self._corner_centers(cr):
            p.drawEllipse(QPoint(cx, cy), r, r)

        p.end()

    def _crop_to_display(self) -> QRect:
        s = self._display_scale
        return QRect(
            int(self._crop_rect.x() * s) + self._offset_x,
            int(self._crop_rect.y() * s) + self._offset_y,
            int(self._crop_rect.width() * s),
            int(self._crop_rect.height() * s),
        )

    @staticmethod
    def _corner_centers(cr: QRect):
        return [
            (cr.left(),  cr.top()),
            (cr.right(), cr.top()),
            (cr.left(),  cr.bottom()),
            (cr.right(), cr.bottom()),
        ]

    def _hit_corner(self, pos: QPoint) -> int:
        cr = self._crop_to_display()
        r = self._HANDLE_R + 4
        for i, (cx, cy) in enumerate(self._corner_centers(cr)):
            if abs(pos.x() - cx) <= r and abs(pos.y() - cy) <= r:
                return i
        return -1

    # interaction

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        corner = self._hit_corner(event.pos())
        if corner >= 0:
            self._resizing = True
            self._resize_corner = corner
            self._drag_start = event.pos()
            self._rect_start = QRect(self._crop_rect)
        else:
            self._dragging = True
            self._drag_start = event.pos()
            self._rect_start = QRect(self._crop_rect)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._resizing:
            self._handle_resize(event.pos())
        elif self._dragging:
            dx = int((event.pos().x() - self._drag_start.x()) / self._display_scale)
            dy = int((event.pos().y() - self._drag_start.y()) / self._display_scale)
            new_x = max(0, min(self._rect_start.x() + dx,
                               self._source.width() - self._crop_rect.width()))
            new_y = max(0, min(self._rect_start.y() + dy,
                               self._source.height() - self._crop_rect.height()))
            self._crop_rect.moveTopLeft(QPoint(new_x, new_y))
            self.update()
        else:
            if self._hit_corner(event.pos()) >= 0:
                self.setCursor(QCursor(Qt.CursorShape.SizeFDiagCursor))
            else:
                self.setCursor(QCursor(Qt.CursorShape.CrossCursor))

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._dragging = False
        self._resizing = False
        self._resize_corner = -1

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        step = max(4, int(self._crop_rect.width() * 0.05))
        new_size = self._crop_rect.width() + (step if delta > 0 else -step)
        new_size = max(self._min_crop, min(new_size, self._max_crop))
        self._resize_crop_centered(new_size)
        self.update()

    # resize helpers

    def _handle_resize(self, pos: QPoint):
        dx = int((pos.x() - self._drag_start.x()) / self._display_scale)
        dy = int((pos.y() - self._drag_start.y()) / self._display_scale)
        c = self._resize_corner
        old = self._rect_start

        if c == 3:    # BR — grow right/down
            delta = (dx + dy) // 2
        elif c == 0:  # TL — grow left/up
            delta = -(dx + dy) // 2
        elif c == 1:  # TR — grow right/up
            delta = (dx - dy) // 2
        else:         # BL — grow left/down
            delta = (-dx + dy) // 2
        new_size = old.width() + delta

        new_size = max(self._min_crop, min(new_size, self._max_crop))
        diff = new_size - old.width()

        if c == 0:  # TL anchor BR
            nx = old.x() - diff
            ny = old.y() - diff
        elif c == 1:  # TR anchor BL
            nx = old.x()
            ny = old.y() - diff
        elif c == 2:  # BL anchor TR
            nx = old.x() - diff
            ny = old.y()
        else:  # BR anchor TL
            nx = old.x()
            ny = old.y()

        # Clamp to image bounds
        nx = max(0, min(nx, self._source.width() - new_size))
        ny = max(0, min(ny, self._source.height() - new_size))
        self._crop_rect = QRect(nx, ny, new_size, new_size)
        self.update()

    def _resize_crop_centered(self, new_size: int):
        cx = self._crop_rect.x() + self._crop_rect.width() // 2
        cy = self._crop_rect.y() + self._crop_rect.height() // 2
        nx = cx - new_size // 2
        ny = cy - new_size // 2
        nx = max(0, min(nx, self._source.width() - new_size))
        ny = max(0, min(ny, self._source.height() - new_size))
        self._crop_rect = QRect(nx, ny, new_size, new_size)


# Icon Row — single icon customization entry

class _IconRow(QFrame):
    """Row for customizing one icon: shows reference + current + import button."""

    icon_changed = Signal(str)  # filename

    def __init__(self, filename: str, label: str, theme_mgr: ThemeManager, parent=None):
        super().__init__(parent)
        self._filename = filename
        self._label = label
        self._theme = theme_mgr
        self.setFixedHeight(52)
        self.setObjectName('iconRow')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        # Label
        name_lbl = QLabel(label)
        name_lbl.setFixedWidth(120)
        set_theme_style(name_lbl, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY)))
        layout.addWidget(name_lbl)

        # Original icon reference
        ref_lbl = QLabel('Default:')
        set_theme_style(ref_lbl, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL)))
        layout.addWidget(ref_lbl)
        self._ref_icon = QLabel()
        self._ref_icon.setFixedSize(32, 32)
        set_theme_style(self._ref_icon, lambda: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'))
        self._load_reference_icon()
        layout.addWidget(self._ref_icon)

        # Current custom icon
        cur_lbl = QLabel('Current:')
        set_theme_style(cur_lbl, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL)))
        layout.addWidget(cur_lbl)
        self._cur_icon = QLabel()
        self._cur_icon.setFixedSize(32, 32)
        set_theme_style(self._cur_icon, lambda: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'))
        self._load_current_icon()
        layout.addWidget(self._cur_icon)

        layout.addStretch()

        # Import button
        import_btn = QPushButton('Import')
        set_theme_style(import_btn, lambda: (f'QPushButton {{ background: {Colors.SURFACE_3};'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 12px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT};'
            f' color: {Colors.ACCENT}; }}'))
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        # Reset button
        reset_btn = QPushButton('Reset')
        set_theme_style(reset_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

    def _load_reference_icon(self):
        pix = _default_icon_pixmap(self._filename, 28)
        if not pix.isNull():
            self._ref_icon.setPixmap(pix)

    def _load_current_icon(self):
        custom = self._theme.get_custom_icon_path(self._filename)
        if custom and custom.exists():
            pix = QPixmap(str(custom)).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._cur_icon.setPixmap(pix)
        else:
            self._load_reference_as_current()

    def _load_reference_as_current(self):
        pix = _default_icon_pixmap(self._filename, 28)
        if not pix.isNull():
            self._cur_icon.setPixmap(pix)

    def _on_import(self):
        formats = ' '.join(f'*{ext}' for ext in SUPPORTED_IMAGE_FORMATS)
        file_path, _ = QFileDialog.getOpenFileName(
            self, f'Select icon for {self._label}',
            '', f'Images ({formats})',
        )
        if not file_path:
            return

        source = Path(file_path)
        file_size_kb = source.stat().st_size / 1024

        # Get original icon dimensions for crop target
        ref_path = _default_icon_path(self._filename)
        ref_pix = QPixmap(str(ref_path)) if ref_path.exists() else QPixmap(32, 32)
        target_size = max(ref_pix.width(), ref_pix.height(), 32)

        source_pix = QPixmap(str(source))
        if source_pix.isNull():
            return

        # Performance warning for large files
        if file_size_kb > 100:
            if not _fthr_message_box(
                self, 'Large Icon File',
                f'This image is {file_size_kb:.0f} KB.\n\n'
                f'Icons larger than 100 KB may slightly impact startup time '
                f'and memory usage. The icon will be resized to '
                f'{target_size}x{target_size}px which reduces the stored size.',
            ):
                return

        # Always show crop dialog so user can position the icon
        final = self._show_crop_dialog(source_pix, target_size)
        if final is None:
            return

        # Save to temp file then copy via theme manager
        temp_path = self._theme.icons_dir / f'_temp_{self._filename}'
        if not final.save(str(temp_path), 'PNG'):
            print(f'[Theme] Failed to write temp icon for {self._filename}')
            return
        self._theme.set_custom_icon(self._filename, temp_path)
        temp_path.unlink(missing_ok=True)
        self._theme.save()

        self._load_current_icon()
        self.icon_changed.emit(self._filename)

    def _show_crop_dialog(self, source_pix: QPixmap, target_size: int) -> Optional[QPixmap]:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        dlg.setMinimumSize(500, 400)
        set_theme_style(dlg, lambda: (f'QDialog {{ background: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER_HI}; }}'
            f' QLabel {{ color: {Colors.TEXT}; background: transparent; }}'))
        _inject_custom_titlebar(dlg, f'Crop Icon — {self._label}')

        dl = QVBoxLayout(dlg)
        dl.setSpacing(12)

        # Side by side: crop widget + reference
        row = QHBoxLayout()
        crop_widget = _IconCropWidget(source_pix, target_size)
        row.addWidget(crop_widget, stretch=3)

        # Reference column
        ref_col = QVBoxLayout()
        ref_col.setSpacing(8)
        ref_title = QLabel('Reference')
        set_theme_style(ref_title, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL)))
        ref_col.addWidget(ref_title)
        ref_display = QLabel()
        ref_display.setFixedSize(64, 64)
        set_theme_style(ref_display, lambda: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'))
        ref_pix = _default_icon_pixmap(self._filename, 60)
        if not ref_pix.isNull():
            ref_display.setPixmap(ref_pix)
        ref_col.addWidget(ref_display)
        ref_col.addStretch()
        row.addLayout(ref_col, stretch=1)

        dl.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        set_theme_style(buttons, lambda: (f'QPushButton {{ background: {Colors.SURFACE_3}; color: {Colors.TEXT};'
            f' border: 1px solid {Colors.BORDER}; padding: 6px 16px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}'))
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dl.addWidget(buttons)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            return crop_widget.get_cropped()
        return None

    def _on_reset(self):
        self._theme.remove_custom_icon(self._filename)
        self._theme.save()
        self._load_current_icon()
        self.icon_changed.emit(self._filename)


# Icon Tint Row — per-icon color control for recolorable default icons


def _tint_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
    """Recolor all opaque pixels to *color*, preserving alpha."""
    tinted = QPixmap(pixmap.size())
    tinted.setDevicePixelRatio(pixmap.devicePixelRatio())
    tinted.fill(Qt.GlobalColor.transparent)
    p = QPainter(tinted)
    p.drawPixmap(0, 0, pixmap)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(tinted.rect(), color)
    p.end()
    return tinted


class _IconTintRow(QFrame):
    """Row for setting the tint color of one default icon."""

    tint_changed = Signal(str)  # filename

    def __init__(self, filename: str, label: str, theme_mgr: ThemeManager,
                 parent=None):
        super().__init__(parent)
        self._filename = filename
        self._label = label
        self._theme = theme_mgr
        self.setFixedHeight(48)
        self.setObjectName('iconTintRow')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        # Tinted icon preview
        self._preview = QLabel()
        self._preview.setFixedSize(32, 32)
        set_theme_style(self._preview, lambda: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'))
        self._refresh_preview()
        layout.addWidget(self._preview)

        # Label
        name_lbl = QLabel(label)
        name_lbl.setFixedWidth(120)
        set_theme_style(name_lbl, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY)))
        layout.addWidget(name_lbl)

        layout.addStretch()

        # Color swatch (clickable)
        self._swatch = QLabel()
        self._swatch.setFixedSize(32, 20)
        self._swatch.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._swatch.setToolTip('Click to change tint color')
        self._update_swatch()
        self._swatch.mousePressEvent = self._on_swatch_click
        layout.addWidget(self._swatch)

        # Reset button
        reset_btn = QPushButton('Reset')
        set_theme_style(reset_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

    def _current_tint(self) -> str:
        return self._theme.get_icon_tint(self._filename)

    def _update_swatch(self):
        set_theme_style(self._swatch, lambda: (f'background: {self._current_tint()};'
            f' border: 1px solid {Colors.BORDER_HI};'))

    def _refresh_preview(self):
        pix = _default_icon_pixmap(self._filename, 28)
        if pix.isNull():
            return
        pix = _tint_pixmap(pix, QColor(self._current_tint()))
        self._preview.setPixmap(pix)

    def _on_swatch_click(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        initial = QColor(self._current_tint())
        dlg = QColorDialog(initial, self)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        set_theme_style(dlg, lambda: (_color_dialog_qss()
            + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'))
        _inject_custom_titlebar(dlg, f'Icon color: {self._label}')
        if dlg.exec():
            color = dlg.currentColor().name()
            self._theme.set_icon_tint(self._filename, color)
            self._theme.save()
            self._update_swatch()
            self._refresh_preview()
            self.tint_changed.emit(self._filename)

    def _on_reset(self):
        self._theme.remove_icon_tint(self._filename)
        self._theme.save()
        self._update_swatch()
        self._refresh_preview()
        self.tint_changed.emit(self._filename)

    def refresh(self):
        self._update_swatch()
        self._refresh_preview()


# Sound Row — single sound customization entry with preview

class _SoundRow(QFrame):
    """Row for customizing one sound: shows current + import + preview buttons."""

    sound_changed = Signal(str)  # key

    _VOLUME_KEYS = {
        'clip_captured': 'clip',
        'screenshot_captured': 'screenshot',
        'error': 'error',
        'startup': 'startup',
        'upload_successful': 'upload_successful',
        'upload_failed': 'upload_failed',
    }

    def __init__(self, key: str, label: str, theme_mgr: ThemeManager,
                 settings_manager=None, parent=None):
        super().__init__(parent)
        self._key = key
        self._label = label
        self._theme = theme_mgr
        self._settings = settings_manager
        self._sound_playback = SoundPlayback(parent=self)
        self.setFixedHeight(52)
        self.setObjectName('soundRow')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        # Label
        name_lbl = QLabel(label)
        name_lbl.setFixedWidth(150)
        set_theme_style(name_lbl, lambda: (label_body(Colors.TEXT, Fonts.SIZE_BODY)))
        layout.addWidget(name_lbl)

        # Current file indicator
        self._file_lbl = QLabel()
        self._file_lbl.setFixedWidth(150)
        set_theme_style(self._file_lbl, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL)))
        self._update_file_label()
        layout.addWidget(self._file_lbl)

        volume_key = self._VOLUME_KEYS.get(key, key)
        self._volume_setting = f'sound_volume_{volume_key}'
        volume_label = QLabel('Volume')
        set_theme_style(volume_label, lambda: (label_uppercase(Colors.TEXT_MUTED, Fonts.SIZE_MICRO, 1)))
        layout.addWidget(volume_label)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setFixedWidth(120)
        set_theme_style(self._volume_slider, lambda: (slider_qss()))
        current_volume = int(
            self._settings.get(self._volume_setting, 100)
            if self._settings is not None else 100)
        self._volume_slider.setValue(max(0, min(100, current_volume)))
        self._volume_slider.valueChanged.connect(self._on_volume_changed)
        layout.addWidget(self._volume_slider)
        self._volume_value = QLabel(f'{self._volume_slider.value()}%')
        self._volume_value.setFixedWidth(34)
        self._volume_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_theme_style(self._volume_value, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL)))
        layout.addWidget(self._volume_value)

        layout.addStretch()

        # Preview button
        preview_btn = IconButton('play.png', label='Preview sound', text='preview')
        set_theme_style(preview_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.ACCENT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT}; }}'))
        preview_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        preview_btn.clicked.connect(self._on_preview)
        layout.addWidget(preview_btn)

        # Import button
        import_btn = QPushButton('Import')
        set_theme_style(import_btn, lambda: (f'QPushButton {{ background: {Colors.SURFACE_3};'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 12px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT};'
            f' color: {Colors.ACCENT}; }}'))
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        # Reset button
        reset_btn = QPushButton('Reset')
        set_theme_style(reset_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

        current_sound = self._theme.get_custom_sound_path(self._key)
        if current_sound is None:
            current_sound = self._find_default_sound()
        if current_sound is not None:
            self._sound_playback.prepare(current_sound)

    def _update_file_label(self):
        custom = self._theme.get_custom_sound_path(self._key)
        if custom and custom.exists():
            size_kb = custom.stat().st_size / 1024
            self._file_lbl.setText(f'{custom.name} ({size_kb:.0f} KB)')
        else:
            self._file_lbl.setText('(default)')

    def _on_preview(self):
        # Find the sound file to play
        custom = self._theme.get_custom_sound_path(self._key)
        if custom and custom.exists():
            sound_path = custom
        else:
            sound_path = self._find_default_sound()
            if not sound_path:
                return

        self._sound_playback.play(sound_path, self._volume_slider.value())

    def _on_volume_changed(self, value: int):
        self._volume_value.setText(f'{int(value)}%')
        self._sound_playback.set_volume(int(value))
        if self._settings is None:
            return
        self._settings.set(self._volume_setting, int(value))
        self._settings.save_settings()

    def _find_default_sound(self) -> Optional[Path]:
        sounds_dir = Path(__file__).parent.parent / 'assets' / 'sounds'
        mapping = {
            'clip_captured': 'clip_captured.wav',
            'error': 'error.wav',
            'screenshot_captured': 'screenshot_saved.wav',
            'screenshot_saved': 'screenshot_saved.wav',
            'startup': 'startup.wav',
            'upload_successful': 'upload_successful.wav',
            'upload_failed': 'upload_failed.wav',
        }
        filename = mapping.get(self._key)
        if filename:
            p = sounds_dir / filename
            if p.exists():
                return p
        return None

    def _on_import(self):
        formats = ' '.join(f'*{ext}' for ext in SUPPORTED_SOUND_FORMATS)
        file_path, _ = QFileDialog.getOpenFileName(
            self, f'Select sound for {self._label}',
            '', f'Audio files ({formats})',
        )
        if not file_path:
            return

        source = Path(file_path)
        self._sound_playback.stop()
        stored = self._theme.set_custom_sound(self._key, source)
        self._theme.save()
        self._update_file_label()
        self._sound_playback.prepare(stored)
        self.sound_changed.emit(self._key)

    def _on_reset(self):
        self._theme.remove_custom_sound(self._key)
        self._theme.save()
        self._update_file_label()
        self._sound_playback.stop()
        self.sound_changed.emit(self._key)


# CustomizePage — full settings tab assembling all sections

class CustomizePage(QWidget):
    """Complete Customize settings tab with accordion sections."""

    theme_applied = Signal()  # Emitted when user clicks Apply

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent)
        self.setObjectName('customizePage')
        set_theme_style(self, lambda: (f'QWidget#customizePage {{ background: {Colors.BG}; }}'))
        self._theme = ThemeManager()
        self._settings = settings_manager
        self._swatches: list[_ColorSwatch] = []
        self._setup_ui()

    def _setup_ui(self):
        # Theme actions stay outside the scrolling options.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setObjectName('customizeScroll')
        set_theme_style(scroll, lambda: (f'QScrollArea#customizeScroll {{ border: none;'
            f' background: {Colors.BG}; }}'
            f' QScrollArea#customizeScroll QWidget#qt_scrollarea_viewport {{'
            f' background: {Colors.BG}; }}'))
        self._scroll = scroll

        container = QWidget()
        container.setObjectName('customizeContainer')
        set_theme_style(container, lambda: (f'QWidget#customizeContainer {{ background: {Colors.BG}; }}'))
        self._container = container
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        self._layout.setSpacing(12)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # Format info bar
        info_bar = QLabel(
            'Themes include typography, colors, icons, capture-card styling, and sounds  —  '
            f'Fonts: {", ".join(SUPPORTED_FONT_FORMATS)}  |  '
            f'Icons: {", ".join(SUPPORTED_IMAGE_FORMATS)}  |  '
            f'Sounds: {", ".join(SUPPORTED_SOUND_FORMATS)}'
        )
        set_theme_style(info_bar, lambda: (f'color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_LABEL}px;'
            f' font-family: {Fonts.BODY}; background: {Colors.SURFACE_2};'
            f' padding: 12px 16px; border: 1px solid {Colors.BORDER}; border-radius: {Sizes.RADIUS_CARD}px;'))
        info_bar.setWordWrap(True)
        self._info_bar = info_bar
        self._layout.addWidget(info_bar)

        # Section 1: Typography
        self._typography_section = _AccordionSection('Typography', '01')
        self._build_typography_section()
        self._layout.addWidget(self._typography_section)

        # Section 2: Colors
        self._colors_section = _AccordionSection('Colors', '02')
        self._build_colors_section()
        self._layout.addWidget(self._colors_section)

        # Section 3: Icon Colors
        self._icon_colors_section = _AccordionSection('Icon Colors', '03')
        self._build_icon_colors_section()
        self._layout.addWidget(self._icon_colors_section)

        # Section 4: Icons
        self._icons_section = _AccordionSection('Icons', '04')
        self._build_icons_section()
        self._layout.addWidget(self._icons_section)

        # Section 5: Capture Card
        self._capture_card_section = _AccordionSection('Capture Card', '05')
        self._build_capture_card_section()
        self._layout.addWidget(self._capture_card_section)

        # Section 6: Sounds
        self._sounds_section = _AccordionSection('Sounds & Volumes', '06')
        self._build_sounds_section()
        self._layout.addWidget(self._sounds_section)

        # Fixed action row above every customization section.
        action_bar = QHBoxLayout()
        action_bar.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_4)
        action_bar.setSpacing(Sizes.ROW_GAP)

        export_btn = QPushButton('Export theme')
        set_theme_style(export_btn, lambda: (button_secondary_qss()))
        export_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        export_btn.clicked.connect(self._on_export)
        self._export_btn = export_btn
        action_bar.addWidget(export_btn)

        import_btn = QPushButton('Import theme')
        set_theme_style(import_btn, lambda: (button_outline_qss()))
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        self._import_btn = import_btn
        action_bar.addWidget(import_btn)

        action_bar.addStretch()

        reset_btn = QPushButton('Reset all')
        set_theme_style(reset_btn, button_outline_qss)
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.setToolTip(
            'Restore default fonts, colors, icons, capture-card styling, sounds and volumes.')
        reset_btn.clicked.connect(self._on_reset_all)
        self._reset_btn = reset_btn
        action_bar.addWidget(reset_btn)

        apply_btn = QPushButton('Apply theme')
        set_theme_style(apply_btn, lambda: (button_primary_qss()))
        apply_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        apply_btn.clicked.connect(self._on_apply)
        self._apply_btn = apply_btn
        action_bar.addWidget(apply_btn)

        outer.addLayout(action_bar)
        self._layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll, 1)

    # Typography section

    def _build_typography_section(self):
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        note = QLabel('Fonts: .ttf and .otf files are supported.')
        note.setWordWrap(True)
        set_theme_style(note, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        layout.addWidget(note)

        families = [*BUILTIN_FONT_FAMILIES, *sorted(
            (family for family in self._theme.get_custom_font_paths()
             if family not in BUILTIN_FONT_FAMILIES),
            key=str.casefold,
        )]
        self._font_combos = {}
        for role, title in (('display', 'Heading font'), ('body', 'Interface font')):
            row = QHBoxLayout()
            row.setSpacing(12)
            label = QLabel(title)
            label.setFixedWidth(140)
            set_theme_style(label, lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_LABEL, 1)))
            row.addWidget(label)
            combo = WheelSafeComboBox()
            # Keep the typography selectors lifted from the pure-black canvas;
            # the color preview below intentionally remains a true black sample.
            set_theme_style(combo, lambda: (combo_qss(background=Colors.SURFACE_3)))
            current = self._theme.get_font(role)
            for family in families:
                combo.addItem(family, family)
            if current not in families:
                current = DEFAULT_FONTS[role]
                self._theme.set_font(role, current)
                self._theme.save()
            index = combo.findData(current)
            combo.setCurrentIndex(max(0, index))
            combo.currentIndexChanged.connect(
                lambda _index, selected_role=role: self._on_font_changed(
                    selected_role))
            row.addWidget(combo, 1)
            self._font_combos[role] = combo

            import_btn = QPushButton('Import font')
            set_theme_style(import_btn, lambda: (button_outline_qss()))
            import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            import_btn.clicked.connect(
                lambda _checked=False, selected_role=role:
                self._on_import_font(selected_role))
            row.addWidget(import_btn)
            layout.addLayout(row)

        self._font_preview = QLabel(
            "Your privacy isn't currency.")
        self._font_preview.setMinimumHeight(52)
        self._font_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_theme_style(self._font_preview, lambda: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER}; '
            f'color: {Colors.TEXT}; padding: 10px;'))
        layout.addWidget(self._font_preview)

        reset_row = QHBoxLayout()
        reset_row.addStretch()
        reset = QPushButton('Reset typography')
        set_theme_style(reset, lambda: (button_outline_qss()))
        reset.clicked.connect(self._on_reset_fonts)
        reset_row.addWidget(reset)
        layout.addLayout(reset_row)

        self._update_font_preview()
        self._typography_section.add_content(wrapper)

    def _on_font_changed(self, role: str):
        combo = self._font_combos.get(role)
        if combo is None:
            return
        self._theme.set_font(role, combo.currentData() or combo.currentText())
        self._theme.save()
        self._update_font_preview()

    def _on_import_font(self, role: str):
        formats = ' '.join(f'*{ext}' for ext in SUPPORTED_FONT_FORMATS)
        file_path, _ = QFileDialog.getOpenFileName(
            self, 'Import font', '', f'Font files ({formats})')
        if not file_path:
            return

        font_id = QFontDatabase.addApplicationFont(file_path)
        families = (QFontDatabase.applicationFontFamilies(font_id)
                    if font_id >= 0 else [])
        if not families:
            _fthr_message_box(
                self, 'Font not supported',
                'Choose a valid OpenType (.otf) or TrueType (.ttf) font file.',
                ok_cancel=False,
            )
            return

        try:
            self._theme.set_custom_font(list(families), Path(file_path))
            self._theme.save()
        except (OSError, ValueError) as exc:
            _fthr_message_box(
                self, 'Font import failed', str(exc), ok_cancel=False)
            return

        for combo in self._font_combos.values():
            for family in families:
                if combo.findData(family) < 0:
                    combo.addItem(family, family)

        selected = families[0]
        combo = self._font_combos[role]
        combo.setCurrentIndex(combo.findData(selected))

    def _on_reset_fonts(self):
        self._theme.reset_fonts()
        self._theme.save()
        for role, family in DEFAULT_FONTS.items():
            combo = self._font_combos.get(role)
            if combo is None:
                continue
            index = combo.findData(family)
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, index))
            combo.blockSignals(False)
        self._update_font_preview()

    def _update_font_preview(self):
        display = self._theme.get_font('display').replace('"', '')
        body = self._theme.get_font('body').replace('"', '')
        set_theme_style(self._font_preview, lambda body=body, display=display: (f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER}; '
            f'border-radius: {Sizes.RADIUS_MD}px; '
            f'color: {Colors.TEXT}; padding: 10px; '
            f'font-family: "{display}", "{body}"; font-size: {Fonts.SIZE_BODY_L}px;'))

    # Colors section

    def _build_colors_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(12)

        # Preview mockup at top
        self._preview = _ColorPreviewMockup()
        wl.addWidget(self._preview)
        wl.addSpacing(8)

        # Group colors by category — label + description on the left, swatches right
        for group_name, tokens in CUSTOMIZABLE_COLOR_GROUPS:
            row = QHBoxLayout()
            row.setSpacing(16)

            # Category label on the left
            cat_col = QVBoxLayout()
            cat_col.setSpacing(2)
            cat_lbl = QLabel(group_name)
            cat_lbl.setFixedWidth(90)
            set_theme_style(cat_lbl, lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY}px;'
                f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
                f' letter-spacing: {Fonts.TRACK_LABEL}px;'
                f' background: transparent; padding-top: 4px;'))
            cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            cat_col.addWidget(cat_lbl)
            cat_col.addStretch()
            row.addLayout(cat_col)

            # Swatches grid on the right
            grid = QGridLayout()
            grid.setSpacing(8)
            max_cols = 6
            for i, (token, label) in enumerate(tokens):
                hex_val = self._theme.get_color(token)
                swatch = _ColorSwatch(token, label, hex_val)
                swatch.color_changed.connect(self._on_color_changed)
                grid.addWidget(swatch, i // max_cols, i % max_cols)
                self._swatches.append(swatch)
            row.addLayout(grid)
            row.addStretch()

            wl.addLayout(row)

        # Reset all colors button
        reset_row = QHBoxLayout()
        reset_row.addStretch()
        reset_btn = QPushButton('Reset all colors')
        set_theme_style(reset_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 6px 14px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset_all_colors)
        reset_row.addWidget(reset_btn)
        wl.addLayout(reset_row)

        self._colors_section.add_content(wrapper)

    def _on_color_changed(self, token: str, hex_value: str):
        self._theme.set_color(token, hex_value)
        self._theme.save()
        self._preview.update_single_color(token, hex_value)

    def _on_reset_all_colors(self):
        self._theme.reset_all_colors()
        self._theme.save()
        for swatch in self._swatches:
            swatch.set_color(DEFAULT_COLORS.get(swatch.token, '#000000'))
        self._preview.update_colors(DEFAULT_COLORS)

    # Icons section

    def _build_icons_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        for filename, label in CUSTOMIZABLE_ICONS.items():
            row = _IconRow(filename, label, self._theme)
            row.icon_changed.connect(self._on_icon_changed)
            wl.addWidget(row)

        self._icons_section.add_content(wrapper)

    def _on_icon_changed(self, filename: str):
        pass  # Icons apply on next app restart or when Apply is clicked

    # Icon Colors section

    def _build_icon_colors_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        # Global tint row
        global_row = QHBoxLayout()
        global_row.setSpacing(12)
        g_label = QLabel('Global tint')
        g_label.setFixedWidth(120)
        set_theme_style(g_label, lambda: (f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px; background: transparent;'))
        global_row.addWidget(g_label)

        self._global_tint_swatch = QLabel()
        self._global_tint_swatch.setFixedSize(40, 24)
        self._global_tint_swatch.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor))
        self._global_tint_swatch.setToolTip('Click to change global icon tint')
        self._update_global_tint_swatch()
        self._global_tint_swatch.mousePressEvent = self._on_global_tint_click
        global_row.addWidget(self._global_tint_swatch)
        global_row.addStretch()

        reset_all_btn = QPushButton('Reset all')
        set_theme_style(reset_all_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_all_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_all_btn.clicked.connect(self._on_reset_all_tints)
        global_row.addWidget(reset_all_btn)
        wl.addLayout(global_row)
        wl.addSpacing(4)

        # Per-icon tint rows
        self._tint_rows: list[_IconTintRow] = []
        for filename, label in CUSTOMIZABLE_ICONS.items():
            row = _IconTintRow(filename, label, self._theme)
            row.tint_changed.connect(self._on_tint_changed)
            wl.addWidget(row)
            self._tint_rows.append(row)

        self._icon_colors_section.add_content(wrapper)

    def _update_global_tint_swatch(self):
        color = self._theme.get_global_icon_tint()
        set_theme_style(self._global_tint_swatch, lambda color=color: (f'background: {color};'
            f' border: 1px solid {Colors.BORDER_HI};'))

    def _on_global_tint_click(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        initial = QColor(self._theme.get_global_icon_tint())
        dlg = QColorDialog(initial, self)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        set_theme_style(dlg, lambda: (_color_dialog_qss()
            + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'))
        _inject_custom_titlebar(dlg, 'Global icon tint')
        if dlg.exec():
            color = dlg.currentColor().name()
            self._theme.set_global_icon_tint(color)
            self._theme.save()
            self._update_global_tint_swatch()
            for row in self._tint_rows:
                row.refresh()

    def _on_reset_all_tints(self):
        self._theme.reset_all_icon_tints()
        self._theme.save()
        self._update_global_tint_swatch()
        for row in self._tint_rows:
            row.refresh()

    def _on_tint_changed(self, filename: str):
        pass  # Tints apply when user clicks Apply Theme

    # Capture Card section

    def _build_capture_card_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(12)

        tokens = [
            ('CAPTURE_CARD_BG',             'Background'),
            ('CAPTURE_CARD_ACCENT',         'Left Accent'),
            ('CAPTURE_CARD_TEXT',            'Text'),
            ('CAPTURE_CARD_DIVIDER',        'Divider'),
            ('CAPTURE_CARD_STATS_DIM',      'Stats Label'),
            ('CAPTURE_CARD_PROGRESS_TRACK', 'Progress Track'),
            ('CAPTURE_CARD_PROGRESS_FILL',  'Progress Fill'),
        ]

        self._cc_swatches: list[_ColorSwatch] = []
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, (token, label) in enumerate(tokens):
            hex_val = self._theme.get_capture_card_color(token)
            swatch = _ColorSwatch(token, label, hex_val)
            swatch.color_changed.connect(self._on_cc_color_changed)
            grid.addWidget(swatch, i // 4, i % 4)
            self._cc_swatches.append(swatch)
        wl.addLayout(grid)

        # Reset row
        reset_row = QHBoxLayout()
        reset_row.addStretch()
        reset_btn = QPushButton('Reset capture card')
        set_theme_style(reset_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: 600; letter-spacing: 0px; padding: 6px 14px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'))
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset_cc_colors)
        reset_row.addWidget(reset_btn)
        wl.addLayout(reset_row)

        self._capture_card_section.add_content(wrapper)

    def _on_cc_color_changed(self, token: str, hex_value: str):
        self._theme.set_capture_card_color(token, hex_value)
        self._theme.save()

    def _on_reset_cc_colors(self):
        self._theme.reset_capture_card_colors()
        self._theme.save()
        for swatch in self._cc_swatches:
            swatch.set_color(
                DEFAULT_CAPTURE_CARD_COLORS.get(swatch.token, '#ffffff'))

    # Sounds section

    def _build_sounds_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        note = QLabel('Any format the system can play is supported.')
        set_theme_style(note, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        note.setWordWrap(True)
        wl.addWidget(note)
        wl.addSpacing(8)

        for key, label in CUSTOMIZABLE_SOUNDS.items():
            row = _SoundRow(key, label, self._theme, self._settings)
            row.sound_changed.connect(self._on_sound_changed)
            wl.addWidget(row)

        self._sounds_section.add_content(wrapper)

    def _on_sound_changed(self, key: str):
        pass  # Sounds apply immediately (next time the event fires)

    # Export / Import

    def _on_export(self):
        dest, _ = QFileDialog.getSaveFileName(
            self, 'Export Theme',
            str(Path.home() / 'fthr_theme.zip'),
            'ZIP files (*.zip)',
        )
        if not dest:
            return
        ok = self._theme.export_theme(Path(dest))
        if ok:
            _fthr_message_box(
                self, 'Exported',
                f'Theme exported to:\n{dest}',
                ok_cancel=False,
            )

    def _on_import(self):
        src, _ = QFileDialog.getOpenFileName(
            self, 'Import Theme', '', 'ZIP files (*.zip)',
        )
        if not src:
            return

        if not _fthr_message_box(
            self, 'Import Theme',
            'This will fully replace your current theme '
            '(typography, colors, icons, and sounds).\n\nContinue?',
        ):
            return

        ok = self._theme.import_theme(Path(src))
        if ok:
            self._refresh_all()
            _fthr_message_box(
                self, 'Imported',
                'Theme imported successfully.\n'
                'Click APPLY THEME to see changes in the app.',
                ok_cancel=False,
            )

    def refresh_theme(self):
        """Restyle existing controls without rebuilding sections or sound players."""
        from ui.style import refresh_theme_styles
        refresh_theme_styles(self)
        for section in (self._typography_section, self._colors_section,
                        self._icon_colors_section, self._icons_section,
                        self._capture_card_section, self._sounds_section):
            section.refresh_theme()
        self._update_font_preview()
        self._preview.update_colors(self._theme.get_all_colors())
        for swatch in (*self._swatches, *self._cc_swatches):
            swatch.update()

    def _on_reset_all(self):
        for row in self.findChildren(_SoundRow):
            row._sound_playback.stop()
        self._theme.reset_all()
        if self._settings is not None:
            for key in _SoundRow._VOLUME_KEYS.values():
                self._settings.set(f'sound_volume_{key}', 100)
            self._settings.save_settings()
        self._refresh_all()
        self.theme_applied.emit()

    def _on_apply(self):
        self.theme_applied.emit()

    def _refresh_all(self):
        """Reload all UI elements from current theme state."""
        colors = self._theme.get_all_colors()
        if hasattr(self, '_font_combos'):
            for configured_family, font_path in (
                    self._theme.get_custom_font_paths().items()):
                if all(combo.findData(configured_family) >= 0
                       for combo in self._font_combos.values()):
                    continue
                font_id = QFontDatabase.addApplicationFont(str(font_path))
                registered = (QFontDatabase.applicationFontFamilies(font_id)
                              if font_id >= 0 else [])
                names = set(registered) | {configured_family}
                for combo in self._font_combos.values():
                    for family in sorted(names, key=str.casefold):
                        if combo.findData(family) < 0:
                            combo.addItem(family, family)
            allowed_families = set(BUILTIN_FONT_FAMILIES) | set(
                self._theme.get_custom_font_paths())
            for role, combo in self._font_combos.items():
                blocked = combo.blockSignals(True)
                for index in reversed(range(combo.count())):
                    if combo.itemData(index) not in allowed_families:
                        combo.removeItem(index)
                combo.blockSignals(blocked)
                family = self._theme.get_font(role)
                index = combo.findData(family)
                if index < 0:
                    family = DEFAULT_FONTS[role]
                    self._theme.set_font(role, family)
                    index = combo.findData(family)
                combo.blockSignals(True)
                combo.setCurrentIndex(index)
                combo.blockSignals(False)
                set_theme_style(combo, lambda: (combo_qss(background=Colors.SURFACE_3)))
            self._update_font_preview()
        for swatch in self._swatches:
            swatch.set_color(colors.get(swatch.token, '#000000'))
        self._preview.update_colors(colors)
        if hasattr(self, '_global_tint_swatch'):
            self._update_global_tint_swatch()
        if hasattr(self, '_tint_rows'):
            for row in self._tint_rows:
                row.refresh()
        if hasattr(self, '_cc_swatches'):
            cc_colors = self._theme.get_all_capture_card_colors()
            for swatch in self._cc_swatches:
                swatch.set_color(cc_colors.get(swatch.token, '#ffffff'))

        for row in self.findChildren(_IconRow):
            row._load_reference_icon()
            row._load_current_icon()
        for row in self.findChildren(_SoundRow):
            row._sound_playback.stop()
            row._update_file_label()
            volume = int(self._settings.get(row._volume_setting, 100)
                         if self._settings is not None else 100)
            blocked = row._volume_slider.blockSignals(True)
            row._volume_slider.setValue(volume)
            row._volume_slider.blockSignals(blocked)
            row._volume_value.setText(f'{row._volume_slider.value()}%')
            row._sound_playback.set_volume(row._volume_slider.value())
