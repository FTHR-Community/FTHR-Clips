"""Local file handoff after exporting a diagnostic report."""
from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QDrag
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from ui.dialogs import FthrDialog
from ui.style import button_outline_qss, button_primary_qss, set_theme_style


class DiagnosticReportFile(QPushButton):
    """Drag an existing ZIP as a native file; a click opens its folder."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._path: Path | None = None
        self._drag_start = None
        self._dragged = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        set_theme_style(self, button_outline_qss)
        self.clicked.connect(self.show_folder)
        self.hide()

    def set_file(self, path) -> None:
        self._path = Path(path).resolve()
        self.setText(f'DRAG ZIP  ·  {self._path.name}')
        self.setToolTip(f'Drag this ZIP into another app, or click to open its folder.\n{self._path}')
        self.setAccessibleName('Exported diagnostic ZIP. Drag to attach or click to open folder.')
        self.show()

    def show_folder(self) -> None:
        if self._path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._path.parent)))

    def mousePressEvent(self, event):
        self._dragged = False
        self._drag_start = (event.position().toPoint()
                            if event.button() == Qt.MouseButton.LeftButton else None)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._drag_start is not None
                and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._drag_start).manhattanLength()
                >= QApplication.startDragDistance()):
            self._drag_start = None
            self._dragged = True
            self.setDown(False)
            if self._path is None or not self._path.is_file():
                self.setText('Zip no longer available — export again')
                return
            data = QMimeData()
            data.setUrls([QUrl.fromLocalFile(str(self._path))])
            drag = QDrag(self)
            drag.setMimeData(data)
            # Sharing a report must never move/delete the original export.
            drag.exec(Qt.DropAction.CopyAction)
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_start = None
        if self._dragged:
            self._dragged = False
            event.accept()
            return
        super().mouseReleaseEvent(event)


class DiagnosticReportDialog(FthrDialog):
    def __init__(self, path, parent=None):
        super().__init__('Diagnostic report exported', parent, width=480)
        message = QLabel('Your report is ready. Drag the ZIP below into your bug report.\n'
                         'You can also drag it later from Settings → Troubleshooting.')
        message.setWordWrap(True)
        self.body_layout.addWidget(message)
        self.file_button = DiagnosticReportFile(self)
        self.file_button.set_file(path)
        self.body_layout.addWidget(self.file_button)
        self.action_layout.addStretch()
        close = QPushButton('Done')
        set_theme_style(close, button_primary_qss)
        close.clicked.connect(self.accept)
        self.action_layout.addWidget(close)
