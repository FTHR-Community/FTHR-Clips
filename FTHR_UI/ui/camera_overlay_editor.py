"""Interactive 16:9 placement previews for camera and image overlays."""
from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal, QTimer, QEvent
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QImageReader, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from core.camera_overlay import (
    DEFAULT_IMAGE_OVERLAY_RECT,
    DEFAULT_OVERLAY_RECT,
    clamp_overlay_rect,
)
from core.third_party_keyboard import DEFAULT_KEYBOARD_OVERLAY_RECT
from ui.style import Colors, Fonts
from ui.image_memory import ImageLRU, start_image_cache_maintenance


_IMAGE_PREVIEW_MAX_PIXELS = 4_000_000
_IMAGE_PREVIEW_CACHE_LIMIT = 12
_IMAGE_PREVIEW_CACHE_TOTAL_PIXELS = 4_000_000
_IMAGE_PREVIEW_CACHE = ImageLRU(_IMAGE_PREVIEW_CACHE_TOTAL_PIXELS * 4,
                               max_entries=_IMAGE_PREVIEW_CACHE_LIMIT)


def _theme_color_with_alpha(value: str, alpha: int) -> QColor:
    color = QColor(value)
    color.setAlpha(alpha)
    return color


def _cached_overlay_pixmap(path: str, target_size=None) -> QPixmap:
    """Decode at a display/DPI tier; export continues to read the source file."""
    start_image_cache_maintenance()
    target = target_size or QSize(640, 360)
    # Small tier steps keep dragging from repeatedly decoding the same image.
    tier = QSize(max(128, ((target.width() + 127) // 128) * 128),
                 max(128, ((target.height() + 127) // 128) * 128))
    try:
        resolved = os.path.normcase(os.path.realpath(path))
        stat = os.stat(resolved)
    except OSError:
        return QPixmap()
    identity = (resolved, stat.st_mtime_ns, stat.st_size)
    key = (*identity, tier.width(), tier.height())
    cached = _IMAGE_PREVIEW_CACHE.get(key)
    if cached is not None:
        return QPixmap(cached)
    reader = QImageReader(resolved)
    reader.setAutoTransform(True)
    size = reader.size()
    pixels = max(0, size.width()) * max(0, size.height())
    if pixels:
        # Cover mode must have enough pixels in both dimensions. Rotation may
        # swap them, so include the transformed orientation when sizing.
        rotated = bool(int(reader.transformation().value) & 4)
        wanted = QSize(tier.height(), tier.width()) if rotated else tier
        scale = min(1.0, max(wanted.width() / size.width(),
                            wanted.height() / size.height()),
                    (_IMAGE_PREVIEW_MAX_PIXELS / pixels) ** .5)
        reader.setScaledSize(QSize(max(1, int(size.width() * scale)),
                                  max(1, int(size.height() * scale))))
    image = reader.read()
    pixmap = QPixmap.fromImage(image) if not image.isNull() else QPixmap()
    if not pixmap.isNull():
        for old_key in tuple(_IMAGE_PREVIEW_CACHE):
            if old_key[0] == resolved and old_key[:3] != identity:
                _IMAGE_PREVIEW_CACHE.pop(old_key, None)
        _IMAGE_PREVIEW_CACHE[key] = pixmap
    return QPixmap(pixmap)


def _overlay_font(size: int, weight=QFont.Weight.Bold) -> QFont:
    """Use the themed display face with a real installed fallback."""
    families = set(QFontDatabase.families())
    preferred = Fonts.DISPLAY_FAMILY
    if preferred not in families:
        preferred = (Fonts.DISPLAY_FAMILY if Fonts.DISPLAY_FAMILY in families
                     else 'Oswald' if 'Oswald' in families else 'DejaVu Sans')
    return QFont(preferred, size, weight)


class OverlayPlacementEditor(QWidget):
    """A clip-backed preview with one draggable, resizable overlay frame."""

    rect_changed = Signal(dict)

    def __init__(self, label='OVERLAY', default_rect=None, parent=None):
        super().__init__(parent)
        self.setMinimumSize(360, 220)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self._label = str(label).upper()
        self._default_rect = default_rect or DEFAULT_OVERLAY_RECT
        self._background = QPixmap()
        self._overlay = QPixmap()
        self._rect = clamp_overlay_rect({}, self._default_rect)
        self._overlay_aspect = 4 / 3
        self._sample_text = ''
        self._empty_text = f'ENABLE {self._label} TO PREVIEW'
        self._enabled = False
        self._gesture = None
        self._gesture_start = QPointF()
        self._gesture_rect = dict(self._rect)

    def sizeHint(self) -> QSize:
        return QSize(520, 320)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.update()

    def set_background(self, pixmap: QPixmap) -> None:
        self._background = QPixmap(pixmap) if not pixmap.isNull() else QPixmap()
        self.update()

    def set_overlay_pixmap(self, pixmap: QPixmap) -> None:
        self._overlay = (
            QPixmap(pixmap) if pixmap and not pixmap.isNull() else QPixmap())
        if not self._overlay.isNull() and self._overlay.height():
            self._overlay_aspect = self._overlay.width() / self._overlay.height()
        self.update()

    def set_sample_text(self, text: str) -> None:
        self._sample_text = str(text or '')
        self.update()

    def set_overlay_aspect(self, aspect: float) -> None:
        if float(aspect) > 0:
            self._overlay_aspect = float(aspect)
        self.update()

    def set_empty_text(self, text: str) -> None:
        self._empty_text = str(text or '')
        self.update()

    def set_rect(self, rect) -> None:
        self._rect = clamp_overlay_rect(rect, self._default_rect)
        self.update()

    def rect_data(self) -> dict[str, float]:
        return dict(self._rect)

    def _height_for_width(self, width: float) -> float:
        canvas = self._canvas_rect()
        if canvas.height() <= 0 or self._overlay_aspect <= 0:
            return width
        return width * canvas.width() / (
            canvas.height() * self._overlay_aspect)

    def _canvas_rect(self) -> QRectF:
        margin = 1.0
        available_w = max(1.0, self.width() - margin * 2)
        available_h = max(1.0, self.height() - margin * 2)
        canvas_w = min(available_w, available_h * 16 / 9)
        canvas_h = canvas_w * 9 / 16
        return QRectF(
            (self.width() - canvas_w) / 2,
            (self.height() - canvas_h) / 2,
            canvas_w,
            canvas_h,
        )

    def _pixel_rect(self) -> QRectF:
        canvas = self._canvas_rect()
        return QRectF(
            canvas.left() + self._rect['x'] * canvas.width(),
            canvas.top() + self._rect['y'] * canvas.height(),
            self._rect['w'] * canvas.width(),
            self._rect['h'] * canvas.height(),
        )

    def _set_cursor_for(self, point: QPointF) -> None:
        box = self._pixel_rect()
        if QRectF(box.right() - 18, box.bottom() - 18, 24, 24).contains(point):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        elif box.contains(point):
            self.setCursor(Qt.CursorShape.SizeAllCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        box = self._pixel_rect()
        point = event.position()
        handle = QRectF(box.right() - 18, box.bottom() - 18, 24, 24)
        if handle.contains(point):
            self._gesture = 'resize'
        elif box.contains(point):
            self._gesture = 'move'
        else:
            self._gesture = None
        if self._gesture:
            self._gesture_start = point
            self._gesture_rect = dict(self._rect)
            self._set_cursor_for(point)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        if self._gesture:
            canvas = self._canvas_rect()
            dx = (point.x() - self._gesture_start.x()) / max(1.0, canvas.width())
            dy = (point.y() - self._gesture_start.y()) / max(1.0, canvas.height())
            rect = dict(self._gesture_rect)
            if self._gesture == 'move':
                rect['x'] += dx
                rect['y'] += dy
            else:
                rect['w'] = max(0.10, min(0.88, rect['w'] + dx))
                rect['h'] = self._height_for_width(rect['w'])
            self._rect = clamp_overlay_rect(rect, self._default_rect)
            self.update()
            event.accept()
            return
        self._set_cursor_for(point)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._gesture:
            self._gesture = None
            self._set_cursor_for(event.position())
            self.rect_changed.emit(dict(self._rect))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        canvas = self._canvas_rect()
        overlay = self._pixel_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_stage(painter, canvas)

        if not self._overlay.isNull():
            self._draw_fit_pixmap(painter, overlay, self._overlay)
        elif self._sample_text:
            painter.fillRect(overlay, _theme_color_with_alpha(Colors.BG, 190))
            painter.setPen(QColor(Colors.TEXT))
            painter.setFont(_overlay_font(9))
            painter.drawText(overlay.adjusted(8, 18, -8, -4),
                             Qt.AlignmentFlag.AlignCenter, self._sample_text)
        else:
            tint = QColor(Colors.ACCENT)
            tint.setAlpha(28 if self._enabled else 12)
            painter.fillRect(overlay, tint)
            painter.setPen(QColor(Colors.TEXT_DIM))
            painter.setFont(QFont(Fonts.BODY_FAMILY, 8))
            painter.drawText(
                overlay.adjusted(8, 20, -8, -6),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                self._empty_text)
        self._draw_overlay_header(
            painter, overlay, self._label, self._enabled)
        painter.end()

    def _draw_stage(self, painter: QPainter, canvas: QRectF) -> None:
        painter.fillRect(self.rect(), QColor(Colors.BG))
        if self._background.isNull():
            self._draw_clip_placeholder(painter, canvas)
        else:
            self._draw_cover_pixmap(painter, canvas, self._background)
            painter.fillRect(canvas, _theme_color_with_alpha(Colors.BG, 34))
        painter.setPen(QPen(QColor(Colors.BORDER_HI), 1))
        painter.drawRect(canvas)

    @staticmethod
    def _draw_overlay_header(painter: QPainter, box: QRectF,
                             label: str, enabled: bool,
                             selected: bool = False) -> None:
        border = QColor(Colors.ACCENT if enabled else Colors.TEXT_MUTED)
        painter.setPen(QPen(border, 3 if selected else 2))
        painter.drawRect(box)
        painter.fillRect(QRectF(box.left(), box.top(), box.width(), 20),
                         _theme_color_with_alpha(Colors.BG, 185))
        painter.setPen(QColor(Colors.TEXT if enabled else Colors.TEXT_DIM))
        painter.setFont(_overlay_font(8))
        painter.drawText(
            QRectF(box.left() + 8, box.top(), box.width() - 16, 20),
            Qt.AlignmentFlag.AlignVCenter, label)
        painter.fillRect(
            QRectF(box.right() - 13, box.bottom() - 13, 13, 13), border)

    @staticmethod
    def _draw_cover_pixmap(painter: QPainter, target: QRectF,
                           pixmap: QPixmap) -> None:
        target_ratio = target.width() / max(1.0, target.height())
        source_ratio = pixmap.width() / max(1, pixmap.height())
        source = QRectF(pixmap.rect())
        if source_ratio > target_ratio:
            wanted = pixmap.height() * target_ratio
            source.setLeft((pixmap.width() - wanted) / 2)
            source.setWidth(wanted)
        else:
            wanted = pixmap.width() / target_ratio
            source.setTop((pixmap.height() - wanted) / 2)
            source.setHeight(wanted)
        painter.drawPixmap(target, pixmap, source)

    @staticmethod
    def _draw_fit_pixmap(painter: QPainter, target: QRectF,
                         pixmap: QPixmap) -> None:
        if pixmap.isNull():
            return
        scale = min(
            target.width() / max(1, pixmap.width()),
            target.height() / max(1, pixmap.height()),
        )
        shown_w = pixmap.width() * scale
        shown_h = pixmap.height() * scale
        destination = QRectF(
            target.left() + (target.width() - shown_w) / 2,
            target.top() + (target.height() - shown_h) / 2,
            shown_w,
            shown_h,
        )
        painter.drawPixmap(destination, pixmap, QRectF(pixmap.rect()))

    @staticmethod
    def _draw_clip_placeholder(painter: QPainter, canvas: QRectF) -> None:
        painter.fillRect(canvas, QColor(Colors.SURFACE_2))
        horizon = canvas.top() + canvas.height() * 0.57
        painter.fillRect(
            QRectF(canvas.left(), horizon, canvas.width(), canvas.bottom() - horizon),
            QColor(Colors.SURFACE_1))
        muted = QColor(Colors.BORDER)
        painter.fillRect(
            QRectF(canvas.left() + canvas.width() * .08,
                   canvas.top() + canvas.height() * .14,
                   canvas.width() * .26, canvas.height() * .30), muted)
        painter.fillRect(
            QRectF(canvas.left() + canvas.width() * .66,
                   canvas.top() + canvas.height() * .20,
                   canvas.width() * .20, canvas.height() * .24), muted)
        painter.setPen(QColor(Colors.TEXT_MUTED))
        painter.setFont(_overlay_font(8))
        painter.drawText(
            canvas.adjusted(10, 8, -10, -8),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
            'CLIP PREVIEW')


class UnifiedOverlayPreview(OverlayPlacementEditor):
    """One placement canvas for a camera and any number of image layers."""

    rects_changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__('CAMERA', DEFAULT_OVERLAY_RECT, parent)
        self.setMinimumSize(640, 360)
        self._rects = {
            'camera': clamp_overlay_rect({}, DEFAULT_OVERLAY_RECT),
        }
        self._camera_pixmap = QPixmap()
        self._camera_enabled = False
        self._keyboard_pixmap = QPixmap()
        self._keyboard_enabled = False
        self._background_paths = []
        self._image_layers: list[dict] = []
        self._preview_resize_timer = QTimer(self)
        self._preview_resize_timer.setSingleShot(True)
        self._preview_resize_timer.timeout.connect(self._refresh_image_previews)
        self._idle_image_timer = QTimer(self)
        self._idle_image_timer.setSingleShot(True)
        self._idle_image_timer.setInterval(15_000)
        self._idle_image_timer.timeout.connect(self._release_hidden_previews)
        self._selected_image = -1
        self._gesture_kind = None
        self._gesture_mode = None
        self._gesture_start = QPointF()
        self._gesture_rects = {}

    def sizeHint(self) -> QSize:
        return QSize(820, 500)

    def set_rects(self, rects) -> None:
        rects = rects if isinstance(rects, dict) else {}
        self._rects['camera'] = clamp_overlay_rect(
            rects.get('camera'), DEFAULT_OVERLAY_RECT)
        self._rects['keyboard'] = clamp_overlay_rect(
            rects.get('keyboard'), DEFAULT_KEYBOARD_OVERLAY_RECT)
        for index, layer in enumerate(self._image_layers):
            key = f'image:{index}'
            fallback = layer['rect']
            self._rects[key] = clamp_overlay_rect(rects.get(key), fallback)
        self._refresh_image_previews()
        self.update()

    def set_image_layers(self, layers) -> None:
        self._image_layers = []
        retained = {
            'camera': self._rects.get('camera', DEFAULT_OVERLAY_RECT),
        }
        # Keep compatibility with callers that use this widget only for
        # camera/images. The keyboard layer is opt-in through set_rects(), as
        # the settings page does when the Windows source is available.
        if 'keyboard' in self._rects:
            retained['keyboard'] = self._rects.get(
                'keyboard', DEFAULT_KEYBOARD_OVERLAY_RECT)
        for index, layer in enumerate(layers or []):
            if not isinstance(layer, dict):
                continue
            rect = clamp_overlay_rect(
                layer.get('rect'), DEFAULT_IMAGE_OVERLAY_RECT)
            pixmap = _cached_overlay_pixmap(str(layer.get('path', '')),
                                            self._image_preview_size(rect))
            key = f'image:{index}'
            retained[key] = rect
            self._image_layers.append({
                **layer,
                'rect': rect,
                'pixmap': pixmap if not pixmap.isNull() else QPixmap(),
            })
        self._rects = retained
        if not self._image_layers:
            self._selected_image = -1
        else:
            self._selected_image = max(
                0, min(self._selected_image, len(self._image_layers) - 1))
        self.update()

    def _image_preview_size(self, rect):
        canvas = self._canvas_rect()
        ratio = self.devicePixelRatioF()
        return QSize(max(1, round(canvas.width() * rect['w'] * ratio)),
                     max(1, round(canvas.height() * rect['h'] * ratio)))

    def _refresh_image_previews(self):
        if self._background_paths:
            canvas = self._canvas_rect()
            ratio = self.devicePixelRatioF()
            target = QSize(max(1, round(canvas.width() * ratio)),
                           max(1, round(canvas.height() * ratio)))
            self._background = QPixmap()
            for path in self._background_paths:
                pixmap = _cached_overlay_pixmap(path, target)
                if not pixmap.isNull():
                    self._background = pixmap
                    break
        for index, layer in enumerate(self._image_layers):
            rect = self._rects[f'image:{index}']
            layer['pixmap'] = _cached_overlay_pixmap(
                str(layer.get('path', '')), self._image_preview_size(rect))
        self.update()

    def set_background_paths(self, paths):
        """Keep source paths so a released display image can be reconstructed."""
        self._background_paths = [str(path) for path in paths if path]
        self._background = QPixmap()
        self._refresh_image_previews()

    def set_background(self, pixmap):
        # Compatibility for callers supplying an already prepared image.
        self._background_paths = []
        super().set_background(pixmap)

    def _release_hidden_previews(self):
        if self.isVisible():
            return
        self._preview_resize_timer.stop()
        paths = {os.path.normcase(os.path.realpath(str(layer.get('path', ''))))
                 for layer in self._image_layers}
        paths.update(os.path.normcase(os.path.realpath(path))
                     for path in self._background_paths)
        if self._background_paths:
            self._background = QPixmap()
        for layer in self._image_layers:
            layer['pixmap'] = QPixmap()
        self._camera_pixmap = QPixmap()
        self._keyboard_pixmap = QPixmap()
        for key in tuple(_IMAGE_PREVIEW_CACHE):
            if key[0] in paths:
                _IMAGE_PREVIEW_CACHE.pop(key, None)

    def resizeEvent(self, event):
        if hasattr(self, '_preview_resize_timer') and self.isVisible():
            self._preview_resize_timer.start(0)
        super().resizeEvent(event)

    def event(self, event):
        if (event.type() == QEvent.Type.DevicePixelRatioChange
                and hasattr(self, '_preview_resize_timer') and self.isVisible()):
            self._preview_resize_timer.start(0)
        return super().event(event)

    def hideEvent(self, event):
        self._preview_resize_timer.stop()
        self._idle_image_timer.start()
        super().hideEvent(event)

    def showEvent(self, event):
        self._idle_image_timer.stop()
        self._refresh_image_previews()
        super().showEvent(event)

    def set_selected_image(self, index: int) -> None:
        self._selected_image = (
            max(0, min(int(index), len(self._image_layers) - 1))
            if self._image_layers else -1)
        self.update()

    def set_overlay_enabled(self, kind: str, enabled: bool) -> None:
        if kind == 'camera':
            self._camera_enabled = bool(enabled)
        elif kind == 'keyboard':
            self._keyboard_enabled = bool(enabled)
        elif kind == 'image':
            for layer in self._image_layers:
                layer['enabled'] = bool(enabled)
        self.update()

    def set_camera_pixmap(self, pixmap: QPixmap) -> None:
        self._camera_pixmap = (
            QPixmap(pixmap) if pixmap and not pixmap.isNull() else QPixmap())
        self._update_live_region('camera')

    def set_keyboard_pixmap(self, pixmap: QPixmap) -> None:
        self._keyboard_pixmap = (
            QPixmap(pixmap) if pixmap and not pixmap.isNull() else QPixmap())
        self._update_live_region('keyboard')

    def _update_live_region(self, kind):
        if kind in self._rects:
            self.update(self._pixel_rect_for(kind).toAlignedRect().adjusted(-3, -3, 3, 3))

    def rects_data(self) -> dict:
        return {key: dict(rect) for key, rect in self._rects.items()}

    def _pixel_rect_for(self, kind: str) -> QRectF:
        canvas = self._canvas_rect()
        rect = self._rects[kind]
        return QRectF(
            canvas.left() + rect['x'] * canvas.width(),
            canvas.top() + rect['y'] * canvas.height(),
            rect['w'] * canvas.width(),
            rect['h'] * canvas.height(),
        )

    def _hit_order(self) -> list[str]:
        images = [f'image:{index}' for index in range(len(self._image_layers))]
        keyboard = ['keyboard'] if 'keyboard' in self._rects else []
        return ['camera', *keyboard, *reversed(images)]

    def _set_cursor_for(self, point: QPointF) -> None:
        for kind in self._hit_order():
            box = self._pixel_rect_for(kind)
            if QRectF(box.right() - 18, box.bottom() - 18, 24, 24).contains(point):
                self.setCursor(Qt.CursorShape.SizeFDiagCursor)
                return
            if box.contains(point):
                self.setCursor(Qt.CursorShape.SizeAllCursor)
                return
        self.unsetCursor()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            QWidget.mousePressEvent(self, event)
            return
        point = event.position()
        self._gesture_kind = None
        for kind in self._hit_order():
            box = self._pixel_rect_for(kind)
            handle = QRectF(box.right() - 18, box.bottom() - 18, 24, 24)
            if handle.contains(point):
                self._gesture_kind, self._gesture_mode = kind, 'resize'
                break
            if box.contains(point):
                self._gesture_kind, self._gesture_mode = kind, 'move'
                break
        if self._gesture_kind:
            if self._gesture_kind.startswith('image:'):
                self._selected_image = int(self._gesture_kind.split(':', 1)[1])
            self._gesture_start = point
            self._gesture_rects = self.rects_data()
            self._set_cursor_for(point)
            self.update()
            event.accept()
            return
        QWidget.mousePressEvent(self, event)

    def mouseMoveEvent(self, event) -> None:
        point = event.position()
        if self._gesture_kind:
            canvas = self._canvas_rect()
            dx = (point.x() - self._gesture_start.x()) / max(1.0, canvas.width())
            dy = (point.y() - self._gesture_start.y()) / max(1.0, canvas.height())
            rect = dict(self._gesture_rects[self._gesture_kind])
            if self._gesture_mode == 'move':
                rect['x'] += dx
                rect['y'] += dy
            else:
                rect['w'] += dx
                rect['h'] += dy
            default = (
                DEFAULT_OVERLAY_RECT if self._gesture_kind == 'camera'
                else (DEFAULT_KEYBOARD_OVERLAY_RECT
                      if self._gesture_kind == 'keyboard'
                      else DEFAULT_IMAGE_OVERLAY_RECT))
            self._rects[self._gesture_kind] = clamp_overlay_rect(rect, default)
            if self._gesture_kind.startswith('image:'):
                self._preview_resize_timer.start(0)
            self.update()
            event.accept()
            return
        self._set_cursor_for(point)
        QWidget.mouseMoveEvent(self, event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._gesture_kind:
            self._gesture_kind = None
            self._gesture_mode = None
            self._set_cursor_for(event.position())
            self.rects_changed.emit(self.rects_data())
            event.accept()
            return
        QWidget.mouseReleaseEvent(self, event)

    def paintEvent(self, _event) -> None:
        canvas = self._canvas_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_stage(painter, canvas)
        for index, layer in enumerate(self._image_layers):
            box = self._pixel_rect_for(f'image:{index}')
            if _event.region().intersects(box.toAlignedRect().adjusted(-3, -3, 3, 3)):
                self._draw_image_layer(painter, index, layer, box)
        if 'keyboard' in self._rects:
            box = self._pixel_rect_for('keyboard')
            if _event.region().intersects(box.toAlignedRect().adjusted(-3, -3, 3, 3)):
                self._draw_keyboard(painter, box)
        box = self._pixel_rect_for('camera')
        if _event.region().intersects(box.toAlignedRect().adjusted(-3, -3, 3, 3)):
            self._draw_camera(painter, box)
        painter.end()

    def _draw_image_layer(self, painter: QPainter, index: int,
                          layer: dict, box: QRectF) -> None:
        enabled = bool(layer.get('enabled', True))
        selected = index == self._selected_image
        content = box.adjusted(2, 22, -2, -2)
        pixmap = layer.get('pixmap', QPixmap())
        if pixmap and not pixmap.isNull():
            painter.save()
            painter.setOpacity(
                float(layer.get('opacity', 100)) / 100.0 if enabled else .25)
            if layer.get('fit') == 'fill':
                self._draw_cover_pixmap(painter, content, pixmap)
            else:
                self._draw_fit_pixmap(painter, content, pixmap)
            painter.restore()
        else:
            tint = QColor(Colors.ACCENT)
            tint.setAlpha(22 if enabled else 10)
            painter.fillRect(content, tint)
        self._draw_overlay_header(
            painter, box, f'IMAGE {index + 1}', enabled, selected)

    def _draw_camera(self, painter: QPainter, box: QRectF) -> None:
        content = box.adjusted(2, 22, -2, -2)
        if not self._camera_pixmap.isNull():
            self._draw_fit_pixmap(painter, content, self._camera_pixmap)
        else:
            tint = QColor(Colors.ACCENT)
            tint.setAlpha(22 if self._camera_enabled else 10)
            painter.fillRect(content, tint)
            painter.setPen(QColor(Colors.TEXT_DIM))
            painter.setFont(_overlay_font(8))
            painter.drawText(
                content.adjusted(8, 4, -8, -8),
                Qt.AlignmentFlag.AlignCenter, 'CAMERA PREVIEW')
        self._draw_overlay_header(
            painter, box, 'CAMERA', self._camera_enabled)

    def _draw_keyboard(self, painter: QPainter, box: QRectF) -> None:
        content = box.adjusted(2, 22, -2, -2)
        if not self._keyboard_pixmap.isNull():
            self._draw_fit_pixmap(painter, content, self._keyboard_pixmap)
        else:
            tint = QColor(Colors.ACCENT)
            tint.setAlpha(22 if self._keyboard_enabled else 10)
            painter.fillRect(content, tint)
            painter.setPen(QColor(Colors.TEXT_DIM))
            painter.setFont(_overlay_font(8))
            painter.drawText(
                content.adjusted(8, 4, -8, -8),
                Qt.AlignmentFlag.AlignCenter, 'KEYBOARD PREVIEW')
        self._draw_overlay_header(
            painter, box, 'KEYBOARD', self._keyboard_enabled)


class CameraOverlayEditor(OverlayPlacementEditor):
    """Compatibility wrapper for the camera placement editor."""

    def __init__(self, parent=None):
        super().__init__('CAMERA', DEFAULT_OVERLAY_RECT, parent)

    def set_frame(self, pixmap: QPixmap) -> None:
        self.set_overlay_pixmap(pixmap)
