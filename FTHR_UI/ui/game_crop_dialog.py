"""Per-game normalized crop editor."""

from __future__ import annotations

from ui.style import set_theme_style

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.style import (
    Colors,
    Fonts,
    button_outline_qss,
    button_primary_qss,
    checkbox_qss,
    label_body,
)
from ui.dialogs import install_fthr_titlebar


class CropSelectionCanvas(QWidget):
    selection_changed = Signal()

    def __init__(self, preview: QPixmap, profile: object = None, parent=None):
        super().__init__(parent)
        self._preview = preview
        self._selection = QRectF(0.0, 0.0, 1.0, 1.0)
        self._drag_origin: QPointF | None = None
        self._image_rect = QRectF()
        self.setMinimumSize(720, 405)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        if isinstance(profile, dict):
            self._selection = QRectF(
                float(profile.get('x', 0.0)),
                float(profile.get('y', 0.0)),
                float(profile.get('w', 1.0)),
                float(profile.get('h', 1.0)),
            ).intersected(QRectF(0.0, 0.0, 1.0, 1.0))

    def normalized_selection(self) -> QRectF:
        return QRectF(self._selection)

    def reset_selection(self):
        self._selection = QRectF(0.0, 0.0, 1.0, 1.0)
        self.selection_changed.emit()
        self.update()

    def _fit_rect(self) -> QRectF:
        if self._preview.isNull():
            return QRectF(self.rect())
        source_ratio = self._preview.width() / max(1, self._preview.height())
        target_ratio = self.width() / max(1, self.height())
        if target_ratio > source_ratio:
            height = float(self.height())
            width = height * source_ratio
            return QRectF((self.width() - width) / 2.0, 0.0, width, height)
        width = float(self.width())
        height = width / source_ratio
        return QRectF(0.0, (self.height() - height) / 2.0, width, height)

    def _to_normalized(self, point: QPointF) -> QPointF:
        rect = self._image_rect
        return QPointF(
            min(1.0, max(0.0, (point.x() - rect.x()) / max(1.0, rect.width()))),
            min(1.0, max(0.0, (point.y() - rect.y()) / max(1.0, rect.height()))),
        )

    def _selection_pixels(self) -> QRectF:
        r = self._selection
        image = self._image_rect
        return QRectF(
            image.x() + r.x() * image.width(),
            image.y() + r.y() * image.height(),
            r.width() * image.width(),
            r.height() * image.height(),
        )

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Colors.SURFACE_2))
        self._image_rect = self._fit_rect()
        if not self._preview.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(self._image_rect.toRect(), self._preview)
        selection = self._selection_pixels()
        shade = QColor(Colors.BG)
        shade.setAlpha(165)
        image = self._image_rect
        painter.fillRect(QRectF(image.left(), image.top(), image.width(),
                                max(0.0, selection.top() - image.top())), shade)
        painter.fillRect(QRectF(image.left(), selection.bottom(), image.width(),
                                max(0.0, image.bottom() - selection.bottom())), shade)
        painter.fillRect(QRectF(image.left(), selection.top(),
                                max(0.0, selection.left() - image.left()),
                                selection.height()), shade)
        painter.fillRect(QRectF(selection.right(), selection.top(),
                                max(0.0, image.right() - selection.right()),
                                selection.height()), shade)
        painter.setPen(QPen(QColor(Colors.ACCENT), 2))
        painter.drawRect(selection)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if not self._image_rect.contains(event.position()):
            return
        self._drag_origin = self._to_normalized(event.position())
        self._selection = QRectF(self._drag_origin, self._drag_origin)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_origin is None:
            return
        current = self._to_normalized(event.position())
        self._selection = QRectF(self._drag_origin, current).normalized()
        self.selection_changed.emit()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() != Qt.MouseButton.LeftButton or self._drag_origin is None:
            return
        current = self._to_normalized(event.position())
        selection = QRectF(self._drag_origin, current).normalized()
        self._drag_origin = None
        if selection.width() < 0.01 or selection.height() < 0.01:
            self.reset_selection()
            return
        self._selection = selection
        self.selection_changed.emit()
        self.update()


class GameCropDialog(QDialog):
    """Select, preview and enable a resolution-independent game crop."""

    def __init__(self, game_name: str, preview: QPixmap,
                 profile: object = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f'Crop profile — {game_name}')
        self.setModal(True)
        self.resize(820, 590)
        set_theme_style(self, lambda: (f'QDialog {{ background: {Colors.BG}; }}'))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel(f'CROP PROFILE · {game_name.upper()}')
        set_theme_style(title, lambda: (f'color:{Colors.TEXT}; font-family:{Fonts.DISPLAY}; '
            f'font-weight:bold; font-size:18px; letter-spacing:2px;'))
        layout.addWidget(title)
        self.canvas = CropSelectionCanvas(preview, profile)
        self.canvas.selection_changed.connect(self._update_readout)
        layout.addWidget(self.canvas, 1)

        controls = QHBoxLayout()
        self.enabled_check = QCheckBox('Apply this crop when capturing this game')
        set_theme_style(self.enabled_check, lambda: (checkbox_qss()))
        self.enabled_check.setChecked(
            bool(profile.get('enabled', True)) if isinstance(profile, dict) else True)
        controls.addWidget(self.enabled_check)
        controls.addStretch(1)
        self.readout = QLabel()
        set_theme_style(self.readout, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL)))
        controls.addWidget(self.readout)
        layout.addLayout(controls)

        actions = QHBoxLayout()
        reset = QPushButton('Reset to full frame')
        set_theme_style(reset, lambda: (button_outline_qss()))
        reset.clicked.connect(self.canvas.reset_selection)
        actions.addWidget(reset)
        actions.addStretch(1)
        cancel = QPushButton('Cancel')
        set_theme_style(cancel, lambda: (button_outline_qss()))
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)
        save = QPushButton('Save profile')
        set_theme_style(save, lambda: (button_primary_qss()))
        save.clicked.connect(self.accept)
        actions.addWidget(save)
        layout.addLayout(actions)
        install_fthr_titlebar(self, f'Crop profile — {game_name}')
        self._update_readout()

    def _update_readout(self):
        rect = self.canvas.normalized_selection()
        self.readout.setText(
            f'{rect.x() * 100:.1f}%, {rect.y() * 100:.1f}%  ·  '
            f'{rect.width() * 100:.1f}% × {rect.height() * 100:.1f}%')

    def profile(self) -> dict[str, object]:
        rect = self.canvas.normalized_selection()
        return {
            'enabled': self.enabled_check.isChecked(),
            'x': round(rect.x(), 6),
            'y': round(rect.y(), 6),
            'w': round(rect.width(), 6),
            'h': round(rect.height(), 6),
        }
