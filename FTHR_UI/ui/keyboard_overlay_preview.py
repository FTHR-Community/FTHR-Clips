"""Small live source preview with an on-canvas chroma-color eyedropper."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Signal, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget

from ui.style import Colors, Fonts


class KeyboardSourcePreview(QWidget):
    """Render the external keyboard window and sample its key color."""

    color_picked = Signal(QColor)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._pick_mode = False
        self._empty_text = 'SELECT A WINDOW TO PREVIEW'
        self._idle_image_timer = QTimer(self)
        self._idle_image_timer.setSingleShot(True)
        self._idle_image_timer.setInterval(15_000)
        self._idle_image_timer.timeout.connect(self._release_hidden_image)
        self.setMinimumSize(220, 110)
        self.setMouseTracking(True)

    def _release_hidden_image(self):
        if not self.isVisible():
            self._image = QImage()

    def hideEvent(self, event):
        self._idle_image_timer.start()
        super().hideEvent(event)

    def showEvent(self, event):
        self._idle_image_timer.stop()
        super().showEvent(event)

    def set_image(self, image: QImage | QPixmap | None, *, share_owned=False) -> None:
        if isinstance(image, QPixmap):
            image = image.toImage()
        # The live updater supplies an owned image. Other callers retain the
        # historical deep-copy contract, including externally backed QImages.
        if image is None or image.isNull():
            self._image = QImage()
        else:
            self._image = QImage(image) if share_owned else image.copy()
        self.update()

    def set_empty_text(self, text: str) -> None:
        text = str(text)
        if text != self._empty_text:
            self._empty_text = text
            self.update()

    def set_pick_mode(self, enabled: bool) -> None:
        self._pick_mode = bool(enabled)
        self.setCursor(
            Qt.CursorShape.CrossCursor if self._pick_mode
            else Qt.CursorShape.ArrowCursor)
        self.update()

    @property
    def pick_mode(self) -> bool:
        return self._pick_mode

    def _image_rect(self) -> QRectF:
        bounds = QRectF(self.rect()).adjusted(7, 7, -7, -7)
        if self._image.isNull() or self._image.height() <= 0:
            return bounds
        scale = min(
            bounds.width() / max(1, self._image.width()),
            bounds.height() / max(1, self._image.height()),
        )
        shown_w = self._image.width() * scale
        shown_h = self._image.height() * scale
        return QRectF(
            bounds.left() + (bounds.width() - shown_w) / 2,
            bounds.top() + (bounds.height() - shown_h) / 2,
            shown_w, shown_h)

    def mousePressEvent(self, event) -> None:
        if (self._pick_mode and event.button() == Qt.MouseButton.LeftButton
                and not self._image.isNull()):
            image_rect = self._image_rect()
            point = event.position()
            if image_rect.contains(point):
                x = max(0, min(
                    self._image.width() - 1,
                    int((point.x() - image_rect.left())
                        * self._image.width() / max(1.0, image_rect.width()))))
                y = max(0, min(
                    self._image.height() - 1,
                    int((point.y() - image_rect.top())
                        * self._image.height() / max(1.0, image_rect.height()))))
                self._pick_mode = False
                self.setCursor(Qt.CursorShape.ArrowCursor)
                self.color_picked.emit(self._image.pixelColor(x, y))
                self.update()
                event.accept()
                return
        super().mousePressEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(Colors.BG))
        bounds = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(
            QColor(Colors.ACCENT if self._pick_mode else Colors.BORDER_HI),
            2 if self._pick_mode else 1))
        painter.drawRect(bounds)
        if self._image.isNull():
            painter.setPen(QColor(Colors.TEXT_DIM))
            painter.setFont(QFont(Fonts.BODY_FAMILY, 8))
            painter.drawText(
                bounds.adjusted(10, 10, -10, -10),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._empty_text)
        else:
            target = self._image_rect()
            painter.drawImage(target, self._image)
            if self._pick_mode:
                painter.fillRect(
                    QRectF(bounds.left(), bounds.top(), bounds.width(), 22),
                    QColor(Colors.BG + 'dd' if len(Colors.BG) == 7 else Colors.BG))
                painter.setPen(QColor(Colors.ACCENT))
                painter.setFont(QFont(Fonts.DISPLAY, 8))
                painter.drawText(
                    QRectF(bounds.left() + 8, bounds.top(),
                           bounds.width() - 16, 22),
                    Qt.AlignmentFlag.AlignVCenter,
                    'CLICK THE KEY COLOR')
        painter.end()
