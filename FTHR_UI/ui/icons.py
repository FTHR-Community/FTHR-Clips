"""Shared rendering for the supplied interface SVGs and legacy theme keys."""
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QSize
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QPushButton

from core.theme_manager import ThemeManager


ICON_ASSETS = {
    'clip.png': 'clips.svg', 'sound.png': 'sound.svg',
    'visuals.png': 'visuals.svg', 'settings(general).png': 'settings.svg',
    'updates.png': 'update.svg', 'personalize.png': 'customise.svg',
    'performance.png': 'performance.svg', 'shortcuts.png': 'shortcuts.svg',
    'home.png': 'home.svg', 'refresh.png': 'refresh.svg',
    'play.png': 'play.svg', 'pause.png': 'pause.svg', 'close.png': 'close.svg',
    'minimize.png': 'minimise.svg', 'maximize.png': 'maximise.svg',
    'shutdown.png': 'shutdown.svg', 'dropdown.png': 'dropdown.svg',
    'upload.png': 'upload.svg', 'link.png': 'copylink.svg',
    'delete.png': 'delete.svg', 'camera.png': 'camera.svg',
    'increase.png': 'plus.svg', 'decrease.png': 'minimise.svg',
    'step-back.png': 'chevronleft.svg', 'step-forward.png': 'chevronright.svg',
}


_BUNDLED_ASSETS = Path(__file__).resolve().parents[1] / 'assets'


@lru_cache(maxsize=256)
def bundled_icon_path(name: str) -> Path:
    assets = _BUNDLED_ASSETS
    if name == 'favicon.ico':
        return assets / name
    return assets / 'icons' / ICON_ASSETS.get(name, name)


def icon_pixmap(name: str, size: int = 20, tint: str | None = None,
                *, custom_path: str | None = None, bundled: bool = False) -> QPixmap:
    theme = ThemeManager()
    custom = None if bundled else (Path(custom_path) if custom_path else theme.get_custom_icon_path(name))
    path = custom or bundled_icon_path(name)
    color = (tint or theme.get_icon_tint(name)) if (
        name != 'favicon.ico' and (custom is None or tint is not None)) else None
    if not path.is_file():
        return QPixmap()
    return _render_pixmap(str(path), path.stat().st_mtime_ns, size, color)


@lru_cache(maxsize=256)
def _render_pixmap(filename: str, modified: int, size: int, color: str | None) -> QPixmap:
    # Include the source timestamp so importing over an existing file reloads it.
    path = Path(filename)
    pixels = size * 2
    if path.suffix.lower() == '.svg':
        renderer = QSvgRenderer(str(path))
        if not renderer.isValid():
            return QPixmap()
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.GlobalColor.transparent)
        natural = renderer.defaultSize()
        fitted = natural.scaled(pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF((pixels - fitted.width()) / 2,
                                      (pixels - fitted.height()) / 2,
                                      fitted.width(), fitted.height()))
        painter.end()
    else:
        pixmap = QPixmap(str(path)).scaled(
            pixels, pixels, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
    if pixmap.isNull():
        return pixmap
    if color is not None:
        painter = QPainter(pixmap)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), QColor(color))
        painter.end()
    pixmap.setDevicePixelRatio(2)
    return pixmap


def load_icon(name: str, size: int = 20, tint: str | None = None) -> QIcon:
    return QIcon(icon_pixmap(name, size, tint))


class IconButton(QPushButton):
    """An SVG button that also follows subsequent theme changes."""
    def __init__(self, name: str, parent=None, *, text: str = '', size: int = 14,
                 label: str = ''):
        super().__init__(text, parent)
        self._icon_name = name
        self._icon_size = size
        self._theme_key = None
        self.setIconSize(QSize(size, size))
        self.setAccessibleName(label or text or name.split('.')[0])
        self._refresh_icon()

    def _refresh_icon(self):
        theme = ThemeManager()
        key = (theme.get_icon_tint(self._icon_name),
               theme.get_custom_icon_path(self._icon_name))
        if key != self._theme_key:
            self.setIcon(load_icon(self._icon_name, self._icon_size))
            self._theme_key = key

    def paintEvent(self, event):
        self._refresh_icon()
        super().paintEvent(event)
