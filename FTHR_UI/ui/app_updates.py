"""Application-owned, asynchronous website updates and their settings controls."""
import hashlib
from pathlib import Path
import queue
import sys
import threading

from PySide6.QtCore import QObject, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel,
                             QDialog, QProgressBar, QPushButton, QVBoxLayout, QWidget)

from core import app_updates as backend
from ui.style import (Colors, Fonts, button_outline_qss, checkbox_qss,
                      label_body, set_theme_style, button_primary_qss)
from ui.dialogs import FthrDialog
from version import __version__

AUTO_CHECK_KEY = 'app_updates_auto_check'
AUTO_CONSENT_KEY = 'app_updates_privacy_consent'


class UpdateDialog(FthrDialog):
    """Update decisions use the same frame and action styles as the app."""

    def __init__(self, title, message, parent=None, *, action, dismiss='Cancel'):
        super().__init__(title, parent, width=500)
        self.setFixedWidth(500)
        self.message = QLabel(message)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        set_theme_style(self.message, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY_L))
        self.body_layout.setContentsMargins(20, 20, 20, 12)
        self.message.ensurePolished()
        self.message.setMinimumHeight(self.message.heightForWidth(460))
        self.body_layout.addWidget(self.message)
        self.action_layout.addStretch()
        self.dismiss_button = QPushButton(dismiss)
        self.action_button = QPushButton(action)
        for button, style, callback in (
                (self.dismiss_button, button_outline_qss, self.reject),
                (self.action_button, button_primary_qss, self.accept)):
            button.setMinimumHeight(36)
            button.setMinimumWidth(100)
            button.setAutoDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            set_theme_style(button, style)
            button.clicked.connect(callback)
            self.action_layout.addWidget(button)
        self.dismiss_button.setDefault(True)
        self.dismiss_button.setFocus()

    @classmethod
    def confirm(cls, parent, title, message, *, action):
        dialog = cls(title, message, parent, action=action)
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()


class AppUpdateController(QObject):
    changed = Signal()
    update_available = Signal(object)

    def __init__(self, settings, parent=None, *, api=None, platform=None,
                 packaged=None, destination=None, launcher=None):
        super().__init__(parent)
        self.settings = settings
        self.api = api or backend
        self.supported = (platform or sys.platform) == 'win32'
        self.packaged = bool(getattr(sys, 'frozen', False)) if packaged is None else packaged
        self.destination = Path(destination or Path.home() / '.fthr' / 'updates')
        self.launcher = launcher
        self.release = None
        self.installer = None
        self.busy = False
        self.operation = ''
        self.progress = (0, 0)
        self.status = ('Check for a new version of FTHRClips.' if self.supported else
                       'Updates provide a Windows installer only. This platform is not supported.')
        self._events = queue.Queue()
        self._cancel = threading.Event()
        self._closed = False
        self._poll = QTimer(self)
        self._poll.setInterval(75)
        self._poll.timeout.connect(self._drain)
        self._daily = QTimer(self)
        self._daily.setInterval(24 * 60 * 60 * 1000)
        self._daily.timeout.connect(self.auto_check)
        self._startup = QTimer(self)
        self._startup.setSingleShot(True)
        self._startup.timeout.connect(self.auto_check)
        app = QApplication.instance()
        if app:
            app.aboutToQuit.connect(self.shutdown)

    @property
    def automatic(self):
        return (self.settings is not None
                and self.settings.get(AUTO_CHECK_KEY, False) is True
                and self.settings.get(AUTO_CONSENT_KEY, False) is True)

    def set_automatic(self, enabled):
        if self.settings is not None:
            previous = self.automatic
            self.settings.set(AUTO_CHECK_KEY, bool(enabled))
            self.settings.set(AUTO_CONSENT_KEY, bool(enabled))
            if self.settings.save_settings() is False:
                self.settings.set(AUTO_CHECK_KEY, previous)
                self.settings.set(AUTO_CONSENT_KEY, previous)
                self.status = 'Could not save the automatic update preference. Try again.'
        self.changed.emit()

    def start(self):
        if self.supported and self.packaged:
            self._startup.start(12000)
            self._daily.start()

    def auto_check(self):
        if self.supported and self.packaged and self.automatic:
            self.check(notify=True)

    def _begin(self, operation, work):
        if self._closed or self.busy or not self.supported:
            return False
        self.busy, self.operation = True, operation
        self._cancel = threading.Event()
        self.progress = (0, 0)
        self.status = {'check': 'Checking for updates…', 'download': 'Downloading update…',
                       'verify': 'Verifying installer…'}[operation]
        # Workers only hold plain Python objects. They never touch a QObject,
        # including during interpreter or window teardown.
        events, cancel = self._events, self._cancel
        def run():
            try:
                result = work(cancel, events)
                events.put(('done', result))
            except Exception as exc:
                events.put(('error', exc))
        self._poll.start()
        threading.Thread(target=run, name='app-update-' + operation, daemon=True).start()
        self.changed.emit()
        return True

    def check(self, *, notify=False):
        if self._closed or self.busy or not self.supported:
            return False
        self._notify_check = notify
        api = self.api
        return self._begin('check', lambda cancel, events:
                           api.check_for_update(api.DEFAULT_MANIFEST_URL, __version__))

    def download(self):
        if self.release is None:
            return False
        api, release, destination = self.api, self.release, self.destination
        return self._begin('download', lambda cancel, events: api.download_update(
            release, destination, progress=lambda done, total: events.put(('progress', (done, total))),
            cancel_event=cancel))

    def cancel(self):
        if self.busy:
            self._cancel.set()
            self.status = 'Cancelling…'
            self.changed.emit()

    def open_installer(self, parent=None):
        if self.busy or not self.supported or self.installer is None:
            return False
        if self.launcher is None:
            self.status = 'Open updates from the main app to close capture safely before installing.'
            self.changed.emit()
            return False
        if not UpdateDialog.confirm(parent, 'Install update and close FTHRClips',
                'Finish recording and exports before continuing.\n\n'
                'FTHRClips will shut down completely, including background capture and the engine. '
                'The installer will open after the app has exited.',
                action='Close app and install'):
            return False
        path, expected = self.installer, self.release.sha256
        cancelled = self.api.UpdateCancelled
        def safe_verify(cancel, events):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    if cancel.is_set():
                        raise cancelled('Cancelled')
                    digest.update(chunk)
            if digest.hexdigest().lower() != expected.lower():
                raise ValueError('Installer verification failed. Download the update again.')
            return path
        return self._begin('verify', safe_verify)

    def show_in_folder(self):
        if self.installer is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.installer.parent)))

    def _drain(self):
        while not self._events.empty():
            kind, value = self._events.get_nowait()
            if kind == 'progress':
                self.progress = value
                continue
            operation = self.operation
            self.busy, self.operation = False, ''
            self._poll.stop()
            if self._cancel.is_set() or isinstance(value, self.api.UpdateCancelled):
                self.status = 'Update operation cancelled. You can try again.'
            elif kind == 'error':
                self.status = f'Update failed: {value}'
                if operation == 'verify':
                    self.installer = None
            elif operation == 'check':
                self.release = value
                self.installer = None
                self.status = (f'Version {value.version} is available.' if value else
                               'You are using the latest version.')
                if value and self._notify_check:
                    self.update_available.emit(value)
            elif operation == 'download':
                self.installer = Path(value)
                self.status = 'Installer ready. Finish recording and exports before opening it.'
            elif operation == 'verify':
                try:
                    self.launcher(value)
                    self.status = 'Closing FTHRClips and background capture before opening the installer.'
                except Exception as exc:
                    self.status = f'Could not start installation: {exc}. Resolve this before trying again.'
        self.changed.emit()

    def shutdown(self):
        self._closed = True
        self._cancel.set()
        self._poll.stop()
        self._daily.stop()
        self._startup.stop()


class AppUpdatesWidget(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(10)
        self.version = QLabel(f'Installed version: {__version__}')
        self.automatic = QCheckBox('Check automatically for updates')
        set_theme_style(self.automatic, checkbox_qss)
        self.automatic.setChecked(controller.automatic)
        self.automatic.toggled.connect(self._toggle_automatic)
        self.privacy = QLabel(
            'Automatic checks are off by default. Check now and Download update also use the internet.')
        self.status = QLabel()
        self.notes = QLabel()
        for label in (self.version, self.privacy, self.status, self.notes):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            set_theme_style(label, lambda: label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
            layout.addWidget(label)
        layout.insertWidget(1, self.automatic)
        self.progress = QProgressBar()
        self.progress.setAccessibleName('Update download progress')
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.check = QPushButton('Check now')
        self.download = QPushButton('Download update')
        self.cancel = QPushButton('Cancel')
        for button, callback in ((self.check, controller.check), (self.download, controller.download),
                                 (self.cancel, controller.cancel)):
            set_theme_style(button, button_outline_qss)
            button.clicked.connect(callback)
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        ready_row = QHBoxLayout()
        self.open = QPushButton('Open installer')
        self.folder = QPushButton('Show in folder')
        for button, callback in ((self.open, lambda: controller.open_installer(self)),
                                 (self.folder, controller.show_in_folder)):
            set_theme_style(button, button_outline_qss)
            button.clicked.connect(callback)
            ready_row.addWidget(button)
        ready_row.addStretch()
        layout.addLayout(ready_row)
        controller.changed.connect(self.refresh)
        self.refresh()

    def _toggle_automatic(self, enabled):
        if enabled and not self.controller.automatic:
            if not UpdateDialog.confirm(self, 'Automatic update checks',
                    'Automatic checks use the internet. FTHRClips will no longer stay offline.\n\n'
                    'It will contact downloads.fthrclips.com shortly after startup and every '
                    '24 hours while running. The server and hosting provider can see your '
                    'IP address and request metadata. Your clips and recordings are not uploaded.\n\n'
                    'Downloads and installation still need your action. '
                    'You can turn automatic checks off here at any time.',
                    action='Enable automatic checks'):
                self.refresh()
                return
        self.controller.set_automatic(enabled)

    def refresh(self):
        c = self.controller
        self.status.setText(c.status)
        self.automatic.setEnabled(c.supported)
        self.automatic.blockSignals(True)
        self.automatic.setChecked(c.automatic)
        self.automatic.blockSignals(False)
        self.check.setEnabled(c.supported and not c.busy)
        self.download.setVisible(c.supported and c.release is not None)
        self.download.setEnabled(c.supported and not c.busy and c.release is not None)
        self.cancel.setVisible(c.busy)
        self.cancel.setEnabled(not c._cancel.is_set())
        self.progress.setVisible(c.busy and c.operation == 'download')
        done, total = c.progress
        self.progress.setRange(0, 100 if total else 0)
        self.progress.setValue(min(100, int(done * 100 / total)) if total else 0)
        self.notes.setText(str(c.release.notes) if c.release else '')
        self.notes.setVisible(bool(c.release and c.release.notes))
        for button in (self.open, self.folder):
            button.setVisible(c.installer is not None)
            button.setEnabled(c.supported and not c.busy)
