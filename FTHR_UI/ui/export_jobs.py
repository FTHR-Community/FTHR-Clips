"""A persistent, nonmodal view of application-owned export jobs."""
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, QSize
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
    QListWidgetItem, QWidget, QProgressBar, QSizePolicy,
)

from core.export_queue import tasks
from core.export_encoding import QUALITY_OPTIONS
from ui.style import Colors, Fonts, button_primary_qss, set_theme_style
from ui.dialogs import FthrDialog

_window = None


class _ExportJobRow(QWidget):
    def __init__(self, task):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(16)
        copy = QVBoxLayout()
        copy.setSpacing(3)
        self.name = QLabel(Path(task.source).name)
        self.name.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.name.setToolTip(Path(task.source).name)
        set_theme_style(self.name, lambda: (
            f'color: {Colors.TEXT}; font: 14px {Fonts.DISPLAY}; background: transparent;'))
        self.status = QLabel()
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        set_theme_style(self.status, lambda: (
            f'color: {Colors.TEXT_DIM}; font: 11px {Fonts.DISPLAY}; background: transparent;'))
        copy.addWidget(self.name)
        copy.addWidget(self.status)
        row.addLayout(copy, 1)
        self.percent = QLabel()
        self.percent.setFixedWidth(42)
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_theme_style(self.percent, lambda: (
            f'color: {Colors.TEXT}; font: 13px {Fonts.DISPLAY}; background: transparent;'))
        row.addWidget(self.percent)
        self.progress = QProgressBar()
        self.progress.setAccessibleName(f'Export progress for {Path(task.source).name}')
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setFixedSize(110, 8)
        set_theme_style(self.progress, lambda: (
            f'QProgressBar {{ background: {Colors.SURFACE_3}; border: none; border-radius: 4px; }}'
            f'QProgressBar::chunk {{ background: {Colors.ACCENT}; border-radius: 4px; }}'))
        row.addWidget(self.progress)
        self.update_task(task)

    def update_task(self, task):
        label = next((label for label, value in QUALITY_OPTIONS if value == task.quality), task.quality)
        self.status.setText(f'{label} · {task.status}')
        self.status.setToolTip(task.detail or task.output)
        percent = max(0, min(100, round(task.progress)))
        self.percent.setText(f'{percent}%')
        self.progress.setValue(percent)


class ExportJobsDialog(FthrDialog):
    def __init__(self):
        super().__init__('Exports', width=620)
        self.setModal(False)
        self.resize(720, 400)
        self.setMinimumHeight(280)
        layout = self.body_layout
        hint = QLabel('Keep browsing while your clips export. Closing this window keeps exports running.')
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: (
            f'color: {Colors.TEXT_DIM}; font: 12px {Fonts.DISPLAY}; background: transparent;'))
        layout.addWidget(hint)
        self.empty_label = QLabel('No export jobs yet. Start an export from a clip.')
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)
        self.list = QListWidget()
        set_theme_style(self.list, lambda: (
            f'QListWidget {{ background: {Colors.SURFACE_1}; color: {Colors.TEXT}; '
            f'font-family: {Fonts.DISPLAY}; border: 1px solid {Colors.BORDER}; padding: 0; }}'
            f'QListWidget::item {{ border-bottom: 1px solid {Colors.BORDER}; }}'
            f'QListWidget::item:selected {{ background: {Colors.SURFACE_3}; }}'))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.list, 1)
        row = self.action_layout
        self.cancel_button = QPushButton('Cancel selected')
        self.cancel_button.clicked.connect(self.cancel_selected)
        row.addWidget(self.cancel_button)
        self.open_button = QPushButton('Open output folder')
        self.open_button.clicked.connect(self.open_selected)
        row.addWidget(self.open_button)
        for button in (self.cancel_button, self.open_button):
            set_theme_style(button, button_primary_qss)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.refresh)
        self.list.currentRowChanged.connect(self.update_actions)
        self.rows = ()
        self._row_widgets = {}

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        records = tasks()
        if tuple(map(id, records)) != tuple(map(id, self.rows)):
            selected_task = self.selected()
            self.list.clear()
            self._row_widgets = {}
            for task in records:
                item = QListWidgetItem()
                item.setSizeHint(QSize(0, 68))
                self.list.addItem(item)
                widget = _ExportJobRow(task)
                self.list.setItemWidget(item, widget)
                self._row_widgets[id(task)] = widget
            self.rows = records
            selected = next((index for index, task in enumerate(records)
                             if task is selected_task), 0)
            self.list.setCurrentRow(selected if records else -1)
        for task in self.rows:
            self._row_widgets[id(task)].update_task(task)
        self.list.setVisible(bool(records))
        self.empty_label.setVisible(not records)
        self.update_actions()

    def selected(self):
        index = self.list.currentRow()
        return self.rows[index] if 0 <= index < len(self.rows) else None

    def update_actions(self, *_):
        task = self.selected()
        self.cancel_button.setEnabled(task is not None and not task.done)
        self.open_button.setEnabled(task is not None)

    def cancel_selected(self):
        task = self.selected()
        if task and not task.done:
            task.cancel.set()
            task.status = 'Cancelling'
        self.refresh()

    def open_selected(self):
        task = self.selected()
        if task:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(task.output).parent)))


def show_export_jobs():
    global _window
    if _window is None:
        _window = ExportJobsDialog()
    _window.show()
    _window.raise_()
    _window.activateWindow()


