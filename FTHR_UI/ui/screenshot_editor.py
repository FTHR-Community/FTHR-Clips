# screenshot_editor.py - Screenshot crop tool
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
from PyQt6.QtCore import Qt, QRect
from PyQt6.QtGui import QPixmap, QPainter, QPen
from ui.style import Colors
import os

class CropCanvas(QLabel):
    def __init__(self, pixmap: QPixmap, parent=None):
        super().__init__(parent)
        self.original_pixmap = pixmap
        self.setPixmap(pixmap)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.crop_rect = QRect()
        self.drag_start = None
        self.is_dragging = False
        self.setMouseTracking(True)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.crop_rect.isEmpty():
            return

        painter = QPainter(self)
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
        self.drag_start = event.pos()
        self.is_dragging = True
        self.crop_rect = QRect(self.drag_start, self.drag_start)
        self.update()

    def mouseMoveEvent(self, event):
        if self.is_dragging:
            self.crop_rect = QRect(self.drag_start, event.pos()).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        self.is_dragging = False

    def get_crop_rect(self) -> QRect:
        displayed = self.pixmap().rect()
        actual = self.original_pixmap.rect()
        scale_x = actual.width() / displayed.width()
        scale_y = actual.height() / displayed.height()
        return QRect(int(self.crop_rect.x() * scale_x), int(self.crop_rect.y() * scale_y),
            int(self.crop_rect.width() * scale_x), int(self.crop_rect.height() * scale_y))

class ScreenshotEditor(QDialog):
    def __init__(self, image_path: str, parent=None):
        super().__init__(parent)
        self.image_path = image_path
        self.setWindowTitle(f'FTHR - {os.path.basename(image_path)}')
        self.setMinimumSize(900, 700)
        self._setup_ui()
        self._apply_styles()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        hint = QLabel('DRAG TO SELECT CROP AREA')
        hint.setStyleSheet(f'color: {Colors.ACCENT}; font-size: 12px;')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        pixmap = QPixmap(self.image_path)
        self.canvas = CropCanvas(pixmap)
        layout.addWidget(self.canvas)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.save_btn = QPushButton('SAVE CROP')
        self.save_btn.clicked.connect(self._save_crop)
        buttons.addWidget(self.save_btn)
        self.save_full_btn = QPushButton('SAVE FULL')
        self.save_full_btn.clicked.connect(self._save_full)
        buttons.addWidget(self.save_full_btn)
        self.cancel_btn = QPushButton('CANCEL')
        self.cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_btn)
        layout.addLayout(buttons)

    def _apply_styles(self):
        self.setStyleSheet(f'''QDialog {{ background-color: #0d0d0d; }}
            QPushButton {{ background-color: #1a1a1a;
            border: 1px solid #333333; border-radius: 0px;
            padding: 10px 24px; color: #ffffff; font-weight: bold; }}
            QPushButton:hover {{ border-color: {Colors.ACCENT}; }}''')

    def _save_crop(self):
        crop_rect = self.canvas.get_crop_rect()
        pixmap = QPixmap(self.image_path)
        cropped = pixmap.copy(crop_rect)
        output_path = self.image_path.replace('.', '_cropped.')
        cropped.save(output_path)
        self.accept()

    def _save_full(self):
        self.accept()