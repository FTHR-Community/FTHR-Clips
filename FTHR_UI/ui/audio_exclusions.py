"""Persistent program-audio exclusion controls for Audio settings."""
from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QDialog, QSizePolicy

from core.running_apps import (
    application_icon, normalize_application_audio_exclusions,
    MAX_APPLICATION_AUDIO_EXCLUSIONS,
)
from ui.running_app_picker import RunningAppPickerDialog
from ui.style import Colors, Fonts, set_theme_style, button_outline_qss, label_body

EXCLUSIONS_KEY = 'application_audio_exclusions'


class _IdentityLabel(QLabel):
    def __init__(self, text):
        super().__init__()
        self._full_text = text
        self.setText(text)
        self.setToolTip(text)
        self.setAccessibleName(text)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideMiddle, self.width()))


class AudioExclusionsWidget(QWidget):
    changed = Signal()

    def __init__(self, settings_manager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.rows = QVBoxLayout()
        layout.addLayout(self.rows)
        self.add_button = QPushButton('Exclude running app…')
        self.add_button.setToolTip(
            'Exclude an executable from future Programs captures, including app restarts. '
            'Exclusions disable the system audio backup. Device modes and existing clips '
            'are unchanged. Nested groups containing excluded apps may also be omitted.')
        self.add_button.setAccessibleDescription(self.add_button.toolTip())
        self.add_button.setAccessibleName('Exclude a running application from future audio capture')
        self.add_button.clicked.connect(self._choose)
        set_theme_style(self.add_button, button_outline_qss)
        layout.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.error = QLabel('Could not save audio exclusions. Try again.')
        self.error.setWordWrap(True)
        set_theme_style(self.error, lambda: label_body(Colors.ERROR, Fonts.SIZE_MICRO))
        self.error.hide()
        layout.addWidget(self.error)
        self.refresh()

    def refresh(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        exclusions = normalize_application_audio_exclusions(self.sm.get(EXCLUSIONS_KEY, []))
        for identity in exclusions:
            row_widget = QWidget(self)
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            icon = QLabel()
            icon.setPixmap(application_icon(identity).pixmap(QSize(20, 20)))
            row.addWidget(icon)
            label = _IdentityLabel(f'{identity}.exe')
            label.setTextFormat(Qt.TextFormat.PlainText)
            set_theme_style(label, lambda: label_body(Colors.TEXT, Fonts.SIZE_MICRO))
            row.addWidget(label, 1)
            remove = QPushButton('Remove')
            set_theme_style(remove, button_outline_qss)
            remove.setAccessibleName(f'Remove audio exclusion for {identity}.exe')
            remove.clicked.connect(lambda _checked=False, key=identity: self.remove_exclusion(key))
            row.addWidget(remove)
            self.rows.addWidget(row_widget)
        if not exclusions:
            self.rows.addWidget(QLabel('No excluded apps.'))
        self.add_button.setEnabled(len(exclusions) < MAX_APPLICATION_AUDIO_EXCLUSIONS)

    def showEvent(self, event):
        self.refresh()
        super().showEvent(event)

    def _choose(self):
        picker = RunningAppPickerDialog(self, title='Exclude application audio',
            excluded=normalize_application_audio_exclusions(self.sm.get(EXCLUSIONS_KEY, [])))
        if picker.exec() == QDialog.DialogCode.Accepted and picker.selected_app():
            self.add_exclusion(picker.selected_app().identity)

    def add_exclusion(self, identity):
        previous = normalize_application_audio_exclusions(self.sm.get(EXCLUSIONS_KEY, []))
        self._save(normalize_application_audio_exclusions(previous + [identity]))

    def remove_exclusion(self, identity):
        previous = normalize_application_audio_exclusions(self.sm.get(EXCLUSIONS_KEY, []))
        self._save([key for key in previous if key != identity])

    def _save(self, exclusions):
        previous = self.sm.get(EXCLUSIONS_KEY, [])
        if exclusions == previous:
            return
        self.sm.set(EXCLUSIONS_KEY, exclusions)
        if self.sm.save_settings() is False:
            self.sm.set(EXCLUSIONS_KEY, previous)
            self.error.show()
            return
        self.error.hide()
        self.refresh()
        self.changed.emit()
