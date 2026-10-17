# screenshot_editor.py - Screenshot crop tool

from ui.style import set_theme_style
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
)
from PySide6.QtCore import Qt, QRect, QSize, QPoint, QTimer, QEvent
from PySide6.QtGui import QPixmap, QPainter, QPen, QImageReader
import math
from ui.style import Colors, Sizes
from ui.dialogs import FthrMessageDialog, install_fthr_titlebar
from pathlib import Path
import os
from core.screenshot_save import (
    ScreenshotCropSaveWorker,
    ScreenshotSaveError,
    publish_staged_png,
    reserve_cropped_screenshot_paths,
)

class CropCanvas(QLabel):
    def __init__(self, pixmap: QPixmap, parent=None, *, source_size=None):
        super().__init__(parent)
        self._preview_pixmap = pixmap
        self.source_size = QSize(source_size or pixmap.size())
        self.setMinimumSize(1, 1)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_rect = QRect()
        self._source_crop = QRect()
        self._source_crop_visual = QRect()
        self.drag_start = None
        self.is_dragging = False
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(820, 560)

    def set_preview(self, pixmap):
        self._preview_pixmap = pixmap
        self.update()

    def _image_rect(self):
        shown = self.source_size.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect(QPoint((self.width() - shown.width()) // 2,
                           (self.height() - shown.height()) // 2), shown)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self._image_rect(), self._preview_pixmap)
        if self.crop_rect.isEmpty():
            return

        painter.setBrush(Qt.GlobalColor.black)
        painter.setOpacity(0.5)

        full, crop = self.rect(), self.crop_rect
        painter.drawRect(full.x(), full.y(), full.width(), crop.y() - full.y())
        painter.drawRect(full.x(), crop.bottom(), full.width(), full.bottom() - crop.bottom())
        painter.drawRect(full.x(), crop.y(), crop.x() - full.x(), crop.height())
        painter.drawRect(crop.right(), crop.y(), full.right() - crop.right(), crop.height())

        painter.setOpacity(1.0)
        painter.setPen(QPen(Qt.GlobalColor.white, 2, Qt.PenStyle.DashLine))
        painter.drawRect(self.crop_rect)

        painter.setBrush(Qt.GlobalColor.white)
        for corner in [crop.topLeft(), crop.topRight(), crop.bottomLeft(), crop.bottomRight()]:
            painter.drawRect(corner.x() - 4, corner.y() - 4, 8, 8)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.drag_start = self._clamp_point(event.position().toPoint())
        self.is_dragging = True
        self.crop_rect = QRect(self.drag_start, self.drag_start)
        self.update()

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self.crop_rect = QRect(
                self.drag_start, self._clamp_point(event.position().toPoint())).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        self.is_dragging = False

    def _clamp_point(self, point):
        bounds = self._image_rect()
        return QPoint(max(bounds.left(), min(bounds.right(), point.x())),
                      max(bounds.top(), min(bounds.bottom(), point.y())))

    def get_crop_rect(self) -> QRect:
        if self.crop_rect == self._source_crop_visual:
            return QRect(self._source_crop)
        displayed = self._image_rect()
        crop = self._map_crop_to_source(self.crop_rect, displayed)
        self._source_crop = QRect(crop)
        self._source_crop_visual = QRect(self.crop_rect)
        return crop

    def _map_crop_to_source(self, crop, displayed):
        crop = crop.intersected(displayed)
        if crop.isEmpty() or displayed.isEmpty() or self.source_size.isEmpty():
            return QRect()
        scale_x = self.source_size.width() / displayed.width()
        scale_y = self.source_size.height() / displayed.height()
        left = math.floor((crop.left() - displayed.left()) * scale_x)
        top = math.floor((crop.top() - displayed.top()) * scale_y)
        right = math.ceil((crop.x() + crop.width() - displayed.left()) * scale_x)
        bottom = math.ceil((crop.y() + crop.height() - displayed.top()) * scale_y)
        return QRect(left, top, right - left, bottom - top).intersected(
            QRect(QPoint(), self.source_size))

    def resizeEvent(self, event):
        if not self.crop_rect.isEmpty() and not event.oldSize().isEmpty():
            # Preserve source-coordinate selection through resize/letterboxing.
            old = self.source_size.scaled(event.oldSize(), Qt.AspectRatioMode.KeepAspectRatio)
            old_rect = QRect(QPoint((event.oldSize().width() - old.width()) // 2,
                                    (event.oldSize().height() - old.height()) // 2), old)
            source = (QRect(self._source_crop) if self.crop_rect == self._source_crop_visual
                      else self._map_crop_to_source(self.crop_rect, old_rect))
            new = self._image_rect()
            self.crop_rect = QRect(
                round(new.x() + source.x() * new.width() / max(1, self.source_size.width())),
                round(new.y() + source.y() * new.height() / max(1, self.source_size.height())),
                round(source.width() * new.width() / max(1, self.source_size.width())),
                round(source.height() * new.height() / max(1, self.source_size.height())))
            self._source_crop = source
            self._source_crop_visual = QRect(self.crop_rect)
        super().resizeEvent(event)

class ScreenshotEditor(QDialog):
    def __init__(self, staged_image_path: str, final_image_path: str, parent=None):
        super().__init__(parent)
        self.staged_image_path = Path(staged_image_path)
        self.final_image_path = Path(final_image_path)
        self._full_published = False
        self._crop_worker = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
        self._preview_timer.timeout.connect(self._load_preview)
        self._preview_tier = None
        self.setWindowTitle(f'FTHR - {os.path.basename(final_image_path)}')
        self.setMinimumSize(900, 700)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._setup_ui()
        self._apply_styles()
        install_fthr_titlebar(
            self, f'FTHR - {os.path.basename(final_image_path)}')

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        reader = QImageReader(str(self.staged_image_path))
        reader.setAutoTransform(True)
        source_size = reader.size()
        if int(reader.transformation().value) & 4:
            source_size.transpose()
        self.canvas = CropCanvas(QPixmap(), source_size=source_size)
        layout.addWidget(self.canvas)
        self._load_preview()

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_btn = QPushButton('Save crop')
        self.save_btn.clicked.connect(self._save_crop)
        buttons.addWidget(self.save_btn)
        self.save_full_btn = QPushButton('Save full')
        self.save_full_btn.clicked.connect(self._save_full)
        buttons.addWidget(self.save_full_btn)
        self.cancel_btn = QPushButton('Cancel')
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

    def _apply_styles(self):
        set_theme_style(self, lambda: (f'''QDialog {{ background-color: {Colors.BG}; }}
            QPushButton {{ background-color: {Colors.SURFACE_2};
            border: 1px solid {Colors.BORDER_HI}; border-radius: {Sizes.RADIUS_MD}px;
            padding: 10px 24px; color: {Colors.TEXT}; font-weight: 600; }}
            QPushButton:hover {{ border-color: {Colors.ACCENT}; }}'''))

    def _save_crop(self):
        if self._crop_worker is not None:
            return
        crop_rect = self.canvas.get_crop_rect()
        if crop_rect.isEmpty():
            self._save_full()
            return
        try:
            crop_paths = reserve_cropped_screenshot_paths(self.final_image_path)
        except ScreenshotSaveError as error:
            self._show_save_error(error.code, error.detail)
            return

        self._set_saving(True)
        worker = ScreenshotCropSaveWorker(
            self.staged_image_path, crop_rect, crop_paths.staged, self)
        self._crop_worker = worker
        worker.succeeded.connect(
            lambda _staged, paths=crop_paths: self._publish_crop(paths))
        worker.failed.connect(self._on_crop_save_failed)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _load_preview(self):
        if self._crop_worker is not None or self._full_published:
            return
        ratio = self.canvas.devicePixelRatioF()
        target = QSize(max(128, math.ceil(self.canvas.width() * ratio / 128) * 128),
                       max(128, math.ceil(self.canvas.height() * ratio / 128) * 128))
        if target == self._preview_tier:
            return
        reader = QImageReader(str(self.staged_image_path))
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isEmpty():
            return
        if int(reader.transformation().value) & 4:
            target.transpose()
        reader.setScaledSize(size.scaled(target, Qt.AspectRatioMode.KeepAspectRatio).boundedTo(size))
        image = reader.read()
        if not image.isNull():
            self.canvas.set_preview(QPixmap.fromImage(image))
            self._preview_tier = QSize(max(128, math.ceil(self.canvas.width() * ratio / 128) * 128),
                                      max(128, math.ceil(self.canvas.height() * ratio / 128) * 128))

    def resizeEvent(self, event):
        self._preview_timer.start()
        super().resizeEvent(event)

    def event(self, event):
        if (event.type() == QEvent.Type.DevicePixelRatioChange
                and hasattr(self, '_preview_timer')):
            self._preview_timer.start()
        return super().event(event)

    def _save_full(self):
        try:
            self._publish_full_if_needed()
        except ScreenshotSaveError as error:
            self._show_save_error(error.code, error.detail)
            return
        self.accept()

    def _publish_crop(self, crop_paths) -> None:
        self._crop_worker = None
        try:
            self._publish_full_if_needed()
            publish_staged_png(crop_paths.staged, crop_paths.final)
        except ScreenshotSaveError as error:
            crop_paths.staged.unlink(missing_ok=True)
            self._set_saving(False)
            self._show_save_error(error.code, error.detail)
            return
        self.accept()

    def _on_crop_save_failed(self, code: str, detail: str) -> None:
        self._crop_worker = None
        self._set_saving(False)
        self._show_save_error(code, detail)

    def _publish_full_if_needed(self) -> None:
        if not self._full_published:
            publish_staged_png(self.staged_image_path, self.final_image_path)
            self._full_published = True

    def _set_saving(self, saving: bool) -> None:
        self.save_btn.setEnabled(not saving)
        self.save_full_btn.setEnabled(not saving)
        self.cancel_btn.setEnabled(not saving)

    def _show_save_error(self, code: str, detail: str) -> None:
        FthrMessageDialog.warning(self, 'Screenshot Failed', f'{code}\n\n{detail}')

    def reject(self):
        if self._crop_worker is not None and self._crop_worker.isRunning():
            return
        super().reject()
