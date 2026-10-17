"""Click-through, always-on-top image response used by Gary Mode."""
from __future__ import annotations

from pathlib import Path
import random

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import QWidget


class GaryOverlay(QWidget):
    """Paint a calming image over the screen without stealing input or focus."""

    def __init__(self, default_image: Path, anchor: QWidget | None = None):
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._anchor = anchor
        self._default_image = Path(default_image)
        self._cat_image = self._default_image.with_name('gatze.png')
        self._cat_pixmap = QPixmap(str(self._cat_image))
        self._base_pixmap = QPixmap()
        self._base_image_path = ''
        self._pixmap = QPixmap()
        self._intensity = 0.0
        self._image_path = ''
        self.set_image(None)

    @property
    def image_path(self) -> str:
        return self._image_path

    @property
    def intensity(self) -> float:
        return self._intensity

    def set_image(self, path: str | None) -> bool:
        candidate = Path(path).expanduser() if path else self._default_image
        pixmap = QPixmap(str(candidate))
        if pixmap.isNull() and candidate != self._default_image:
            candidate = self._default_image
            pixmap = QPixmap(str(candidate))
        if pixmap.isNull():
            self._base_pixmap = QPixmap()
            self._base_image_path = ''
            self._pixmap = QPixmap()
            self._image_path = ''
            self.hide()
            return False
        self._base_pixmap = pixmap
        self._base_image_path = str(candidate)
        self._pixmap = pixmap
        self._image_path = str(candidate)
        self.update()
        return True

    def set_intensity(self, value: float) -> None:
        value = max(0.0, min(float(value), 1.0))
        if self._intensity <= 0.0 and value > 0.0:
            # Roll once per appearance, retaining the result throughout its fade.
            use_cat = not self._cat_pixmap.isNull() and random.randrange(10) == 0
            self._pixmap = self._cat_pixmap if use_cat else self._base_pixmap
            self._image_path = str(self._cat_image) if use_cat else self._base_image_path
        self._intensity = value
        if value <= 0.0 or self._pixmap.isNull():
            if self.isVisible():
                self.hide()
            return
        self._fit_to_anchor_screen()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.update()

    def _fit_to_anchor_screen(self) -> None:
        screen = None
        if self._anchor is not None:
            try:
                center = self._anchor.mapToGlobal(self._anchor.rect().center())
                screen = QGuiApplication.screenAt(center)
            except RuntimeError:
                screen = None
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is not None and self.geometry() != screen.geometry():
            self.setGeometry(screen.geometry())

    def paintEvent(self, _event) -> None:
        if self._pixmap.isNull() or self._intensity <= 0.0:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        scale = 0.55 + (0.45 * self._intensity)
        max_w = int(self.width() * 0.58 * scale)
        max_h = int(self.height() * 0.76 * scale)
        shown = self._pixmap.scaled(
            max_w, max_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = (self.width() - shown.width()) // 2
        y = (self.height() - shown.height()) // 2
        painter.setOpacity(self._intensity)
        painter.drawPixmap(QRect(x, y, shown.width(), shown.height()), shown)
        painter.end()
