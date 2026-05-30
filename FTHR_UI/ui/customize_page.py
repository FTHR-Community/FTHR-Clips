"""
Customize settings page — accordion-based UI for colors, icons, and sounds.

Performance notes:
- Color preview uses a small static mockup, not the live app.
- Icons are loaded lazily via QPixmap with size caching.
- Sounds store paths only; playback is on-demand via QMediaPlayer.
- Theme is applied to the main app only on explicit "Apply" click (batch QSS regen).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import (
    Qt, QSize, QPropertyAnimation, QEasingCurve, pyqtSignal, QUrl, QRect,
    QPoint, QTimer,
)
from PyQt6.QtGui import (
    QPixmap, QIcon, QColor, QPainter, QPen, QBrush, QFont, QCursor,
    QMouseEvent, QPaintEvent, QResizeEvent,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame, QColorDialog, QFileDialog, QSizePolicy, QGridLayout,
    QApplication, QGraphicsOpacityEffect,
)
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput

from ui.style import (
    Colors, Fonts, Sizes,
    label_display, label_uppercase, label_body,
    BUTTON_PRIMARY_QSS, BUTTON_OUTLINE_QSS, BUTTON_SECONDARY_QSS,
    SCROLLBAR_QSS, SLIDER_QSS,
)
from core.theme_manager import (
    ThemeManager, DEFAULT_COLORS, CUSTOMIZABLE_ICONS,
    CUSTOMIZABLE_SOUNDS, SUPPORTED_IMAGE_FORMATS, SUPPORTED_SOUND_FORMATS,
    DEFAULT_ICON_TINT, DEFAULT_CAPTURE_CARD_COLORS,
)


# Dark-themed stylesheet for the non-native QColorDialog
_COLOR_DIALOG_QSS = f"""
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
    font-weight: bold;
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
    font-weight: bold;
    font-size: {Fonts.SIZE_BODY}px;
    letter-spacing: 1px;
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
    title_bar.setStyleSheet(
        f'QFrame {{ background-color: {Colors.SURFACE_1};'
        f' border-bottom: 1px solid {Colors.BORDER}; }}'
    )
    tb_layout = QHBoxLayout(title_bar)
    tb_layout.setContentsMargins(12, 0, 6, 0)
    tb_layout.setSpacing(0)

    title_lbl = QLabel(title.upper())
    title_lbl.setStyleSheet(
        f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;'
        f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
        f' letter-spacing: {Fonts.TRACK_LABEL}px;'
        f' background: transparent;'
    )
    tb_layout.addWidget(title_lbl)
    tb_layout.addStretch()

    close_btn = QPushButton('×')
    close_btn.setFixedSize(28, 28)
    close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    close_btn.setStyleSheet(
        f'QPushButton {{ background: transparent; border: none;'
        f' color: {Colors.TEXT_DIM}; font-size: 16px; font-weight: bold; }}'
        f' QPushButton:hover {{ background-color: {Colors.ERROR};'
        f' color: {Colors.TEXT}; }}'
    )
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
    from PyQt6.QtWidgets import QDialog

    dlg = QDialog(parent)
    dlg.setWindowFlags(
        Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
    )
    dlg.setFixedWidth(420)
    dlg.setStyleSheet(
        f'QDialog {{ background: {Colors.SURFACE_1};'
        f' border: 1px solid {Colors.BORDER_HI}; }}'
    )

    layout = QVBoxLayout(dlg)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)

    # Title bar
    title_bar = QFrame()
    title_bar.setFixedHeight(36)
    title_bar.setStyleSheet(
        f'QFrame {{ background: {Colors.SURFACE_2};'
        f' border-bottom: 1px solid {Colors.BORDER}; }}'
    )
    tb_layout = QHBoxLayout(title_bar)
    tb_layout.setContentsMargins(12, 0, 6, 0)
    tb_layout.setSpacing(0)
    title_lbl = QLabel(title.upper())
    title_lbl.setStyleSheet(
        f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;'
        f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
        f' letter-spacing: {Fonts.TRACK_LABEL}px;'
        f' background: transparent;'
    )
    tb_layout.addWidget(title_lbl)
    tb_layout.addStretch()
    close_btn = QPushButton('×')
    close_btn.setFixedSize(28, 28)
    close_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    close_btn.setStyleSheet(
        f'QPushButton {{ background: transparent; border: none;'
        f' color: {Colors.TEXT_DIM}; font-size: 16px; font-weight: bold; }}'
        f' QPushButton:hover {{ background-color: {Colors.ERROR};'
        f' color: {Colors.TEXT}; }}'
    )
    close_btn.clicked.connect(dlg.reject)
    tb_layout.addWidget(close_btn)
    layout.addWidget(title_bar)

    # Body
    body = QFrame()
    body.setStyleSheet(f'QFrame {{ background: {Colors.SURFACE_1}; }}')
    bl = QVBoxLayout(body)
    bl.setContentsMargins(20, 16, 20, 16)
    bl.setSpacing(12)
    msg_lbl = QLabel(message)
    msg_lbl.setWordWrap(True)
    msg_lbl.setStyleSheet(
        f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY}px;'
        f' font-family: {Fonts.BODY}; background: transparent;'
    )
    bl.addWidget(msg_lbl)

    # Buttons
    btn_row = QHBoxLayout()
    btn_row.addStretch()
    ok_btn = QPushButton('OK')
    ok_btn.setFixedWidth(80)
    ok_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
    ok_btn.setStyleSheet(
        f'QPushButton {{ background: {Colors.ACCENT}; color: {Colors.BG};'
        f' border: none; padding: 6px 14px; font-family: {Fonts.DISPLAY};'
        f' font-weight: bold; font-size: {Fonts.SIZE_LABEL}px;'
        f' letter-spacing: 1px; }}'
        f' QPushButton:hover {{ background: {Colors.TEXT}; }}'
    )
    ok_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(ok_btn)

    if ok_cancel:
        cancel_btn = QPushButton('Cancel')
        cancel_btn.setFixedWidth(80)
        cancel_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        cancel_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; color: {Colors.TEXT_DIM};'
            f' border: 1px solid {Colors.BORDER}; padding: 6px 14px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' font-size: {Fonts.SIZE_LABEL}px; letter-spacing: 1px; }}'
            f' QPushButton:hover {{ border-color: {Colors.TEXT};'
            f' color: {Colors.TEXT}; }}'
        )
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

    from PyQt6.QtWidgets import QDialog as _D
    return dlg.exec() == _D.DialogCode.Accepted


# ═══════════════════════════════════════════════════════════════════════════════
# Accordion Section — collapsible container matching fthrclips.com style
# ═══════════════════════════════════════════════════════════════════════════════

class _AccordionSection(QFrame):
    """Collapsible section with animated expand/collapse.

    Matches the fthrclips.com accordion: 350ms material-design ease,
    overflow hidden, no re-toggle while animating.
    """

    def __init__(self, title: str, number: str = '', parent=None):
        super().__init__(parent)
        self.setObjectName('accordionSection')
        self._expanded = False
        self._animating = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header (clickable trigger) ────────────────────────────────────
        self._header = QPushButton()
        self._header.setObjectName('accordionHeader')
        self._header.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._header.setFixedHeight(40)
        self._header.clicked.connect(self.toggle)

        h_layout = QHBoxLayout(self._header)
        h_layout.setContentsMargins(16, 0, 16, 0)
        h_layout.setSpacing(12)

        if number:
            num_lbl = QLabel(number)
            num_lbl.setStyleSheet(
                f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px;'
                f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
                f' background: transparent;'
            )
            h_layout.addWidget(num_lbl)

        title_lbl = QLabel(title.upper())
        title_lbl.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'
        )
        h_layout.addWidget(title_lbl)
        h_layout.addStretch()

        self._indicator = QLabel('+')
        self._indicator.setStyleSheet(
            f'color: {Colors.TEXT_DIM}; font-size: 18px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' background: transparent;'
        )
        h_layout.addWidget(self._indicator)

        layout.addWidget(self._header)

        # ── Body (animated content area) ──────────────────────────────────
        self._body = QWidget()
        self._body.setObjectName('accordionBody')
        self._body.setMaximumHeight(0)
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(16, 12, 16, 16)
        self._body_layout.setSpacing(8)
        layout.addWidget(self._body)

        self._anim = QPropertyAnimation(self._body, b'maximumHeight')
        self._anim.setDuration(350)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.finished.connect(self._on_anim_finished)

    def add_content(self, widget: QWidget):
        self._body_layout.addWidget(widget)

    def add_layout(self, layout):
        self._body_layout.addLayout(layout)

    def toggle(self):
        if self._animating:
            return
        self._expanded = not self._expanded
        self._animating = True
        self._indicator.setText('−' if self._expanded else '+')

        current = self._body.height()

        if self._expanded:
            # Expanding — measure content height without visual flash
            self._body.setMaximumHeight(16777215)
            end_height = self._body_layout.sizeHint().height() + 28
            self._body.setMaximumHeight(current)
        else:
            end_height = 0

        self._anim.stop()
        self._anim.setStartValue(current)
        self._anim.setEndValue(end_height)
        self._anim.start()

    def _on_anim_finished(self):
        self._animating = False
        if self._expanded:
            self._body.setMaximumHeight(16777215)

    def expand(self):
        if not self._expanded:
            self.toggle()

    def collapse(self):
        if self._expanded:
            self.toggle()

    @property
    def expanded(self) -> bool:
        return self._expanded


# ═══════════════════════════════════════════════════════════════════════════════
# Color Swatch — clickable color picker tile
# ═══════════════════════════════════════════════════════════════════════════════

class _ColorSwatch(QWidget):
    """Small clickable color tile that opens a QColorDialog on click."""

    color_changed = pyqtSignal(str, str)  # (token, new_hex)

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
        p.drawRect(swatch_rect)

        # Label text below
        p.setPen(QPen(QColor(Colors.TEXT_DIM)))
        p.setFont(QFont('Segoe UI', 7))
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
            dlg.setStyleSheet(
                _COLOR_DIALOG_QSS
                + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'
            )
            _inject_custom_titlebar(dlg, f'Pick color: {self._label}')
            if dlg.exec():
                color = dlg.currentColor()
                self._color = color.name()
                self.update()
                self.color_changed.emit(self.token, self._color)


# ═══════════════════════════════════════════════════════════════════════════════
# Color Preview Mockup — miniature app UI that reflects current theme colors
# ═══════════════════════════════════════════════════════════════════════════════

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
        self._colors = dict(colors)
        self._rebuild()

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
        top_bar.setStyleSheet(
            f'background-color: {c["SHELL_BG"]};'
            f' border-bottom: 1px solid {c["SHELL_DIVIDER"]};'
        )
        tb_layout = QHBoxLayout(top_bar)
        tb_layout.setContentsMargins(10, 0, 10, 0)
        logo_lbl = QLabel('FTHR')
        logo_lbl.setStyleSheet(
            f'color: {c["TEXT"]}; font-size: 10px; font-weight: bold;'
            f' font-family: {Fonts.DISPLAY}; letter-spacing: 2px;'
            f' background: transparent;'
        )
        tb_layout.addWidget(logo_lbl)
        tb_layout.addStretch()
        status = QLabel('CAPTURING')
        status.setStyleSheet(
            f'color: {c["BG"]}; background: {c["ACCENT"]};'
            f' font-size: 8px; font-weight: bold; padding: 2px 6px;'
            f' font-family: {Fonts.DISPLAY};'
        )
        tb_layout.addWidget(status)
        layout.addWidget(top_bar)

        # Content area with cards
        content = QFrame()
        content.setStyleSheet(f'background-color: {c["BG"]};')
        cl = QVBoxLayout(content)
        cl.setContentsMargins(10, 8, 10, 8)
        cl.setSpacing(6)

        # Mini clip cards
        cards_row = QHBoxLayout()
        cards_row.setSpacing(6)
        for i in range(3):
            card = QFrame()
            card.setFixedSize(80, 48)
            card.setStyleSheet(
                f'background-color: {c["CARD_BG"]};'
                f' border: 1px solid {c["CARD_BORDER"]};'
            )
            card_l = QVBoxLayout(card)
            card_l.setContentsMargins(6, 4, 6, 4)
            thumb = QFrame()
            thumb.setFixedHeight(22)
            thumb.setStyleSheet(f'background-color: {c["SURFACE_2"]};')
            card_l.addWidget(thumb)
            title = QLabel(f'clip_{i+1}.mp4')
            title.setStyleSheet(
                f'color: {c["TEXT_DIM"]}; font-size: 7px; background: transparent;'
            )
            card_l.addWidget(title)
            cards_row.addWidget(card)
        cards_row.addStretch()
        cl.addLayout(cards_row)

        # Mini button row
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        primary_btn = QLabel('SAVE CLIP')
        primary_btn.setStyleSheet(
            f'background-color: {c["ACCENT"]}; color: {c["BG"]};'
            f' font-size: 8px; font-weight: bold; padding: 4px 10px;'
            f' font-family: {Fonts.DISPLAY};'
        )
        btn_row.addWidget(primary_btn)
        outline_btn = QLabel('EXPORT')
        outline_btn.setStyleSheet(
            f'background-color: transparent; color: {c["TEXT"]};'
            f' border: 1px solid {c["BORDER_HI"]};'
            f' font-size: 8px; font-weight: bold; padding: 4px 10px;'
            f' font-family: {Fonts.DISPLAY};'
        )
        btn_row.addWidget(outline_btn)
        btn_row.addStretch()
        cl.addLayout(btn_row)

        # Mini text samples
        text_row = QVBoxLayout()
        text_row.setSpacing(2)
        t1 = QLabel('Primary text sample')
        t1.setStyleSheet(f'color: {c["TEXT"]}; font-size: 9px; background: transparent;')
        t2 = QLabel('Secondary text sample')
        t2.setStyleSheet(f'color: {c["TEXT_DIM"]}; font-size: 9px; background: transparent;')
        t3 = QLabel('Muted text sample')
        t3.setStyleSheet(f'color: {c["TEXT_MUTED"]}; font-size: 9px; background: transparent;')
        text_row.addWidget(t1)
        text_row.addWidget(t2)
        text_row.addWidget(t3)
        cl.addLayout(text_row)

        # Error / delete samples
        state_row = QHBoxLayout()
        state_row.setSpacing(8)
        err = QLabel('ERROR')
        err.setStyleSheet(
            f'color: {c["ERROR"]}; font-size: 8px; font-weight: bold;'
            f' border: 1px solid {c["ERROR"]}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'
        )
        state_row.addWidget(err)
        delete_color = c.get("DELETE", c["ERROR"])
        delbtn = QLabel('DELETE')
        delbtn.setStyleSheet(
            f'color: {delete_color}; font-size: 8px; font-weight: bold;'
            f' border: 1px solid {delete_color}; padding: 2px 6px;'
            f' background: transparent; font-family: {Fonts.DISPLAY};'
        )
        state_row.addWidget(delbtn)
        state_row.addStretch()
        cl.addLayout(state_row)

        layout.addWidget(content, stretch=1)

    def update_single_color(self, token: str, hex_value: str):
        self._colors[token] = hex_value
        self._rebuild()


# ═══════════════════════════════════════════════════════════════════════════════
# Icon Crop Dialog — crop imported image to match original icon dimensions
# ═══════════════════════════════════════════════════════════════════════════════

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

    # ── painting ──────────────────────────────────────────────────────

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
        overlay = QColor(0, 0, 0, 140)
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

    # ── interaction ───────────────────────────────────────────────────

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
            # Update cursor based on hover
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

    # ── resize helpers ────────────────────────────────────────────────

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


# ═══════════════════════════════════════════════════════════════════════════════
# Icon Row — single icon customization entry
# ═══════════════════════════════════════════════════════════════════════════════

class _IconRow(QFrame):
    """Row for customizing one icon: shows reference + current + import button."""

    icon_changed = pyqtSignal(str)  # filename

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
        name_lbl.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))
        layout.addWidget(name_lbl)

        # Original icon reference
        ref_lbl = QLabel('Default:')
        ref_lbl.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL))
        layout.addWidget(ref_lbl)
        self._ref_icon = QLabel()
        self._ref_icon.setFixedSize(32, 32)
        self._ref_icon.setStyleSheet(
            f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'
        )
        self._load_reference_icon()
        layout.addWidget(self._ref_icon)

        # Current custom icon
        cur_lbl = QLabel('Current:')
        cur_lbl.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL))
        layout.addWidget(cur_lbl)
        self._cur_icon = QLabel()
        self._cur_icon.setFixedSize(32, 32)
        self._cur_icon.setStyleSheet(
            f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'
        )
        self._load_current_icon()
        layout.addWidget(self._cur_icon)

        layout.addStretch()

        # Import button
        import_btn = QPushButton('IMPORT')
        import_btn.setStyleSheet(
            f'QPushButton {{ background: {Colors.SURFACE_3};'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 12px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT};'
            f' color: {Colors.ACCENT}; }}'
        )
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        # Reset button
        reset_btn = QPushButton('RESET')
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

    def _load_reference_icon(self):
        # Load from default assets
        if self._filename == 'fthr_logo.png':
            path = Path(__file__).parent.parent / 'assets' / 'fthr_logo.png'
        else:
            path = Path(__file__).parent.parent / 'assets' / 'icons' / self._filename
        if path.exists():
            pix = QPixmap(str(path)).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
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
        if self._filename == 'fthr_logo.png':
            path = Path(__file__).parent.parent / 'assets' / 'fthr_logo.png'
        else:
            path = Path(__file__).parent.parent / 'assets' / 'icons' / self._filename
        if path.exists():
            pix = QPixmap(str(path)).scaled(
                28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
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
        if self._filename == 'fthr_logo.png':
            ref_path = Path(__file__).parent.parent / 'assets' / 'fthr_logo.png'
        else:
            ref_path = Path(__file__).parent.parent / 'assets' / 'icons' / self._filename
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
        final.save(str(temp_path), 'PNG')
        self._theme.set_custom_icon(self._filename, temp_path)
        temp_path.unlink(missing_ok=True)
        self._theme.save()

        # Update display
        self._load_current_icon()
        self.icon_changed.emit(self._filename)

    def _show_crop_dialog(self, source_pix: QPixmap, target_size: int) -> Optional[QPixmap]:
        from PyQt6.QtWidgets import QDialog, QDialogButtonBox

        dlg = QDialog(self)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        dlg.setMinimumSize(500, 400)
        dlg.setStyleSheet(
            f'QDialog {{ background: {Colors.BG};'
            f' border: 1px solid {Colors.BORDER_HI}; }}'
            f' QLabel {{ color: {Colors.TEXT}; background: transparent; }}'
        )
        _inject_custom_titlebar(dlg, f'Crop Icon — {self._label}')

        dl = QVBoxLayout(dlg)
        dl.setSpacing(12)

        info = QLabel(
            f'Drag to move, drag corners to resize, scroll to zoom.\n'
            f'The selected area will be scaled to {target_size}×{target_size}px.'
        )
        info.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        dl.addWidget(info)

        # Side by side: crop widget + reference
        row = QHBoxLayout()
        crop_widget = _IconCropWidget(source_pix, target_size)
        row.addWidget(crop_widget, stretch=3)

        # Reference column
        ref_col = QVBoxLayout()
        ref_col.setSpacing(8)
        ref_title = QLabel('REFERENCE')
        ref_title.setStyleSheet(label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        ref_col.addWidget(ref_title)
        ref_display = QLabel()
        ref_display.setFixedSize(64, 64)
        ref_display.setStyleSheet(f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};')
        if self._filename == 'fthr_logo.png':
            ref_path = Path(__file__).parent.parent / 'assets' / 'fthr_logo.png'
        else:
            ref_path = Path(__file__).parent.parent / 'assets' / 'icons' / self._filename
        if ref_path.exists():
            ref_display.setPixmap(QPixmap(str(ref_path)).scaled(
                60, 60, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        ref_col.addWidget(ref_display)
        ref_col.addStretch()
        row.addLayout(ref_col, stretch=1)

        dl.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.setStyleSheet(
            f'QPushButton {{ background: {Colors.SURFACE_3}; color: {Colors.TEXT};'
            f' border: 1px solid {Colors.BORDER}; padding: 6px 16px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}'
        )
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


# ═══════════════════════════════════════════════════════════════════════════════
# Icon Tint Row — per-icon color control for recolorable default icons
# ═══════════════════════════════════════════════════════════════════════════════


def _tint_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
    """Recolor all opaque pixels to *color*, preserving alpha."""
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    p = QPainter(tinted)
    p.drawPixmap(0, 0, pixmap)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(tinted.rect(), color)
    p.end()
    return tinted


class _IconTintRow(QFrame):
    """Row for setting the tint color of one default icon."""

    tint_changed = pyqtSignal(str)  # filename

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
        self._preview.setStyleSheet(
            f'background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};'
        )
        self._refresh_preview()
        layout.addWidget(self._preview)

        # Label
        name_lbl = QLabel(label)
        name_lbl.setFixedWidth(120)
        name_lbl.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))
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
        reset_btn = QPushButton('RESET')
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

    def _current_tint(self) -> str:
        return self._theme.get_icon_tint(self._filename)

    def _update_swatch(self):
        self._swatch.setStyleSheet(
            f'background: {self._current_tint()};'
            f' border: 1px solid {Colors.BORDER_HI};'
        )

    def _refresh_preview(self):
        if self._filename == 'fthr_logo.png':
            path = Path(__file__).parent.parent / 'assets' / 'fthr_logo.png'
        else:
            path = Path(__file__).parent.parent / 'assets' / 'icons' / self._filename
        if not path.exists():
            return
        pix = QPixmap(str(path)).scaled(
            28, 28, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
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
        dlg.setStyleSheet(
            _COLOR_DIALOG_QSS
            + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'
        )
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


# ═══════════════════════════════════════════════════════════════════════════════
# Sound Row — single sound customization entry with preview
# ═══════════════════════════════════════════════════════════════════════════════

class _SoundRow(QFrame):
    """Row for customizing one sound: shows current + import + preview buttons."""

    sound_changed = pyqtSignal(str)  # key

    def __init__(self, key: str, label: str, theme_mgr: ThemeManager, parent=None):
        super().__init__(parent)
        self._key = key
        self._label = label
        self._theme = theme_mgr
        self._player: Optional[QMediaPlayer] = None
        self._audio_output: Optional[QAudioOutput] = None
        self.setFixedHeight(48)
        self.setObjectName('soundRow')

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(12)

        # Label
        name_lbl = QLabel(label)
        name_lbl.setFixedWidth(150)
        name_lbl.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))
        layout.addWidget(name_lbl)

        # Current file indicator
        self._file_lbl = QLabel()
        self._file_lbl.setFixedWidth(180)
        self._file_lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        self._update_file_label()
        layout.addWidget(self._file_lbl)

        layout.addStretch()

        # Preview button
        preview_btn = QPushButton('▶  PREVIEW')
        preview_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.ACCENT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT}; }}'
        )
        preview_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        preview_btn.clicked.connect(self._on_preview)
        layout.addWidget(preview_btn)

        # Import button
        import_btn = QPushButton('IMPORT')
        import_btn.setStyleSheet(
            f'QPushButton {{ background: {Colors.SURFACE_3};'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 12px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ACCENT};'
            f' color: {Colors.ACCENT}; }}'
        )
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        layout.addWidget(import_btn)

        # Reset button
        reset_btn = QPushButton('RESET')
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset)
        layout.addWidget(reset_btn)

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

        # Lazy-init player
        if self._player is None:
            self._audio_output = QAudioOutput()
            self._player = QMediaPlayer()
            self._player.setAudioOutput(self._audio_output)

        self._player.stop()
        self._player.setSource(QUrl.fromLocalFile(str(sound_path)))
        self._audio_output.setVolume(0.8)
        self._player.play()

    def _find_default_sound(self) -> Optional[Path]:
        sounds_dir = Path(__file__).parent.parent / 'assets' / 'sounds'
        mapping = {
            'clip_captured': 'clip_captured.mp3',
            'error': 'error.mp3',
            'screenshot_captured': 'screenshot_saved.mp3',
            'screenshot_saved': 'screenshot_saved.mp3',
            'startup': None,
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
        self._theme.set_custom_sound(self._key, source)
        self._theme.save()
        self._update_file_label()
        self.sound_changed.emit(self._key)

    def _on_reset(self):
        self._theme.remove_custom_sound(self._key)
        self._theme.save()
        self._update_file_label()
        # Stop playback if active
        if self._player:
            self._player.stop()
        self.sound_changed.emit(self._key)


# ═══════════════════════════════════════════════════════════════════════════════
# CustomizePage — full settings tab assembling all sections
# ═══════════════════════════════════════════════════════════════════════════════

class CustomizePage(QWidget):
    """Complete Customize settings tab with accordion sections."""

    theme_applied = pyqtSignal()  # Emitted when user clicks Apply

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = ThemeManager()
        self._swatches: list[_ColorSwatch] = []
        self._setup_ui()

    def _setup_ui(self):
        # Scroll area wrapper for the entire page
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f'QScrollArea {{ border: none; background: transparent; }}')

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(12)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # ── Format info bar ───────────────────────────────────────────────
        info_bar = QLabel(
            'Supported formats  —  '
            f'Icons: {", ".join(SUPPORTED_IMAGE_FORMATS)}  |  '
            f'Sounds: {", ".join(SUPPORTED_SOUND_FORMATS)}'
        )
        info_bar.setStyleSheet(
            f'color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_LABEL}px;'
            f' font-family: {Fonts.BODY}; background: {Colors.SURFACE_1};'
            f' padding: 8px 16px; border: 1px solid {Colors.BORDER};'
        )
        info_bar.setWordWrap(True)
        self._layout.addWidget(info_bar)

        # ── Section 1: Colors ─────────────────────────────────────────────
        self._colors_section = _AccordionSection('Colors', '01')
        self._build_colors_section()
        self._layout.addWidget(self._colors_section)

        # ── Section 2: Icon Colors ────────────────────────────────────────
        self._icon_colors_section = _AccordionSection('Icon Colors', '02')
        self._build_icon_colors_section()
        self._layout.addWidget(self._icon_colors_section)

        # ── Section 3: Icons ──────────────────────────────────────────────
        self._icons_section = _AccordionSection('Icons', '03')
        self._build_icons_section()
        self._layout.addWidget(self._icons_section)

        # ── Section 4: Capture Card ───────────────────────────────────────
        self._capture_card_section = _AccordionSection('Capture Card', '04')
        self._build_capture_card_section()
        self._layout.addWidget(self._capture_card_section)

        # ── Section 5: Sounds ─────────────────────────────────────────────
        self._sounds_section = _AccordionSection('Sounds', '05')
        self._build_sounds_section()
        self._layout.addWidget(self._sounds_section)

        # ── Export / Import bar ───────────────────────────────────────────
        self._layout.addSpacing(16)
        action_bar = QHBoxLayout()
        action_bar.setSpacing(10)

        export_btn = QPushButton('EXPORT THEME')
        export_btn.setStyleSheet(BUTTON_SECONDARY_QSS)
        export_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        export_btn.clicked.connect(self._on_export)
        action_bar.addWidget(export_btn)

        import_btn = QPushButton('IMPORT THEME')
        import_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        import_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        import_btn.clicked.connect(self._on_import)
        action_bar.addWidget(import_btn)

        action_bar.addStretch()

        apply_btn = QPushButton('APPLY THEME')
        apply_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        apply_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        apply_btn.clicked.connect(self._on_apply)
        action_bar.addWidget(apply_btn)

        self._layout.addLayout(action_bar)
        self._layout.addStretch()

        scroll.setWidget(container)
        outer.addWidget(scroll)

    # ── Colors section ────────────────────────────────────────────────────

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
        groups = [
            ('Surfaces', 'App backgrounds, panels, popups', [
                ('BG', 'Background'),
                ('SURFACE_1', 'Surface 1'),
                ('SURFACE_2', 'Surface 2'),
                ('SURFACE_3', 'Surface 3'),
                ('SHELL_BG', 'Shell BG'),
                ('SHELL_DIVIDER', 'Shell Line'),
            ]),
            ('Accent', 'Highlights, buttons, progress bars, active states', [
                ('ACCENT', 'Accent'),
                ('ACCENT_DIM', 'Accent Dim'),
                ('ACCENT_SOFT', 'Accent Soft'),
            ]),
            ('Text', 'All text throughout the app', [
                ('TEXT', 'Primary'),
                ('TEXT_DIM', 'Secondary'),
                ('TEXT_MUTED', 'Muted'),
            ]),
            ('Borders', 'Dividers, outlines, separators', [
                ('BORDER', 'Border'),
                ('BORDER_HI', 'Border Light'),
                ('HAIRLINE', 'Hairline'),
            ]),
            ('Cards', 'Clip cards in the grid', [
                ('CARD_BG', 'Card BG'),
                ('CARD_BG_HI', 'Card Hover'),
                ('CARD_BORDER', 'Card Border'),
            ]),
            ('Status', 'Error and functional indicators', [
                ('ERROR', 'Error'),
                ('DELETE', 'Delete Button'),
            ]),
        ]

        for group_name, description, tokens in groups:
            row = QHBoxLayout()
            row.setSpacing(16)

            # Category label + description on the left
            cat_col = QVBoxLayout()
            cat_col.setSpacing(2)
            cat_lbl = QLabel(group_name.upper())
            cat_lbl.setFixedWidth(90)
            cat_lbl.setStyleSheet(
                f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY}px;'
                f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
                f' letter-spacing: {Fonts.TRACK_LABEL}px;'
                f' background: transparent; padding-top: 4px;'
            )
            cat_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            cat_col.addWidget(cat_lbl)
            desc_lbl = QLabel(description)
            desc_lbl.setFixedWidth(90)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(
                f'color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_MICRO}px;'
                f' font-family: {Fonts.BODY}; background: transparent;'
            )
            desc_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            cat_col.addWidget(desc_lbl)
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
        reset_btn = QPushButton('RESET ALL COLORS')
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 6px 14px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
        reset_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_btn.clicked.connect(self._on_reset_all_colors)
        reset_row.addWidget(reset_btn)
        wl.addLayout(reset_row)

        self._colors_section.add_content(wrapper)

    def _on_color_changed(self, token: str, hex_value: str):
        self._theme.set_color(token, hex_value)
        self._theme.save()
        # Update preview mockup
        self._preview.update_single_color(token, hex_value)

    def _on_reset_all_colors(self):
        self._theme.reset_all_colors()
        self._theme.save()
        # Update all swatches
        for swatch in self._swatches:
            swatch.set_color(DEFAULT_COLORS.get(swatch.token, '#000000'))
        # Update preview
        self._preview.update_colors(DEFAULT_COLORS)

    # ── Icons section ─────────────────────────────────────────────────────

    def _build_icons_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        note = QLabel(
            'Replace any app icon with your own image. '
            'Images will be cropped to match the original dimensions. '
            'Smaller files (<100 KB) load faster at startup.'
        )
        note.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        note.setWordWrap(True)
        wl.addWidget(note)
        wl.addSpacing(8)

        for filename, label in CUSTOMIZABLE_ICONS.items():
            row = _IconRow(filename, label, self._theme)
            row.icon_changed.connect(self._on_icon_changed)
            wl.addWidget(row)

        self._icons_section.add_content(wrapper)

    def _on_icon_changed(self, filename: str):
        pass  # Icons apply on next app restart or when Apply is clicked

    # ── Icon Colors section ───────────────────────────────────────────────

    def _build_icon_colors_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        note = QLabel(
            'Default icons are tinted with a color (signature teal by default). '
            'Change the global tint or set a unique color per icon. '
            'Imported custom icons are not affected.'
        )
        note.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        note.setWordWrap(True)
        wl.addWidget(note)
        wl.addSpacing(8)

        # Global tint row
        global_row = QHBoxLayout()
        global_row.setSpacing(12)
        g_label = QLabel('GLOBAL TINT')
        g_label.setFixedWidth(120)
        g_label.setStyleSheet(
            f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px; background: transparent;'
        )
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

        reset_all_btn = QPushButton('RESET ALL')
        reset_all_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 4px 10px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
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
        self._global_tint_swatch.setStyleSheet(
            f'background: {color};'
            f' border: 1px solid {Colors.BORDER_HI};'
        )

    def _on_global_tint_click(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        initial = QColor(self._theme.get_global_icon_tint())
        dlg = QColorDialog(initial, self)
        dlg.setWindowFlags(
            Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
        )
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog)
        dlg.setStyleSheet(
            _COLOR_DIALOG_QSS
            + f'QColorDialog {{ border: 1px solid {Colors.BORDER_HI}; }}'
        )
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

    # ── Capture Card section ──────────────────────────────────────────────

    def _build_capture_card_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(12)

        note = QLabel(
            'Customize the colors of the capture notification card '
            'that appears when a clip is saved.'
        )
        note.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        note.setWordWrap(True)
        wl.addWidget(note)
        wl.addSpacing(4)

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
        reset_btn = QPushButton('RESET CAPTURE CARD')
        reset_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: {Fonts.SIZE_LABEL}px; font-family: {Fonts.DISPLAY};'
            f' font-weight: bold; letter-spacing: 1px; padding: 6px 14px; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR};'
            f' color: {Colors.ERROR}; }}'
        )
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

    # ── Sounds section ────────────────────────────────────────────────────

    def _build_sounds_section(self):
        wrapper = QWidget()
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(4)

        note = QLabel(
            'Replace notification sounds with your own audio files. '
            'Any format the system can play is supported. '
            'Click PREVIEW to hear the current sound.'
        )
        note.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        note.setWordWrap(True)
        wl.addWidget(note)
        wl.addSpacing(8)

        for key, label in CUSTOMIZABLE_SOUNDS.items():
            row = _SoundRow(key, label, self._theme)
            row.sound_changed.connect(self._on_sound_changed)
            wl.addWidget(row)

        self._sounds_section.add_content(wrapper)

    def _on_sound_changed(self, key: str):
        pass  # Sounds apply immediately (next time the event fires)

    # ── Export / Import ───────────────────────────────────────────────────

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
            '(colors, icons, sounds).\n\nContinue?',
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

    def _on_apply(self):
        self.theme_applied.emit()

    def _refresh_all(self):
        """Reload all UI elements from current theme state."""
        colors = self._theme.get_all_colors()
        # Update color swatches
        for swatch in self._swatches:
            swatch.set_color(colors.get(swatch.token, '#000000'))
        # Update preview
        self._preview.update_colors(colors)
        # Update icon tint swatches
        if hasattr(self, '_global_tint_swatch'):
            self._update_global_tint_swatch()
        if hasattr(self, '_tint_rows'):
            for row in self._tint_rows:
                row.refresh()
        # Update capture card swatches
        if hasattr(self, '_cc_swatches'):
            cc_colors = self._theme.get_all_capture_card_colors()
            for swatch in self._cc_swatches:
                swatch.set_color(cc_colors.get(swatch.token, '#ffffff'))
