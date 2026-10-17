"""A persistent editor window whose per-clip resources can be replaced."""

from pathlib import Path
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QDialog, QMessageBox, QPushButton, QVBoxLayout

from ui.clip_viewer import ClipViewer
from ui.shell_transitions import SnapshotSlide
from ui.motion import reduce_motion
from ui.style import set_theme_style


class ClipEditorWindow(QDialog):
    upload_requested = Signal(str)
    export_error = Signal(str, str, str)
    clip_navigation_requested = Signal(int)
    minimize_requested = Signal()
    sidebar_navigation_requested = Signal(int)
    panel_switch_requested = Signal()

    def __init__(self, clip_path, bridge, parent=None, *, session_state=None, **options):
        super().__init__(parent)
        self._page = None
        self._clip_slide = None
        self._last_navigation_at = None
        self._bridge = bridge
        self._options = options
        self._native_style_applied = False
        self._idle_memory_timer = QTimer(self)
        self._idle_memory_timer.setSingleShot(True)
        self._idle_memory_timer.setInterval(15_000)
        self._idle_memory_timer.timeout.connect(self._release_idle_memory)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowMinimizeButtonHint
                            | Qt.WindowType.WindowMaximizeButtonHint)
        self.setMinimumSize(960, 600)
        self._content = QVBoxLayout(self)
        self._content.setContentsMargins(0, 0, 0, 0)
        self._content.setSpacing(0)
        self.replace_clip(clip_path, session_state=session_state, autoplay=True)

    @property
    def clip_page(self):
        return self._page

    def __getattr__(self, name):
        # MainWindow's lifecycle operations act on the currently displayed
        # clip. The native window remains the same across replacements.
        page = self.__dict__.get('_page')
        if page is not None:
            return getattr(page, name)
        raise AttributeError(name)

    def replace_clip(self, clip_path, *, session_state=None, autoplay=True,
                     navigation_step=0, **options):
        self._idle_memory_timer.stop()
        previous = self._page
        worker = getattr(previous, '_export_thread', None)
        if worker is not None and worker.is_alive():
            QMessageBox.information(self, 'Export in progress',
                                    'Finish or cancel the export before switching clips.')
            return False
        now = time.monotonic()
        rapid_navigation = (self._last_navigation_at is not None
                            and now - self._last_navigation_at < .25)
        self._last_navigation_at = now if navigation_step else None
        # Preserve single-step motion, but never recapture a desktop-sized
        # image for every keypress in a browsing burst. Each clip still opens.
        self._finish_clip_slide()
        outgoing = (self.grab() if navigation_step and not rapid_navigation and self.isVisible()
                    and not reduce_motion(previous) else None)
        self.setUpdatesEnabled(False)
        replacement_error = None
        settings = {**self._options, **options}
        try:
            if previous is None:
                page = ClipViewer(clip_path, self._bridge, self, embedded=True,
                                  autoplay=False, **settings)
                page.upload_requested.connect(self.upload_requested.emit)
                page.export_error.connect(self.export_error.emit)
                page.clip_navigation_requested.connect(self.clip_navigation_requested.emit)
                page.minimize_requested.connect(self.minimize_requested.emit)
                page.sidebar_navigation_requested.connect(self.sidebar_navigation_requested.emit)
                page.panel_switch_requested.connect(self.panel_switch_requested.emit)
                page.finished.connect(self._page_finished)
                self._page = page
                self._content.addWidget(page)
                for button in page.findChildren(QPushButton):
                    button.setAutoDefault(False)
                    button.setDefault(False)
            else:
                page = previous
                previous_path = page.clip_path
                previous_state = page.capture_session_state()
                try:
                    page.load_clip(clip_path, autoplay=False, **settings)
                except (OSError, RuntimeError, TypeError, ValueError) as error:
                    replacement_error = str(error)
                    clip_path, settings = previous_path, self._options
                    session_state, autoplay = previous_state, False
                    page.load_clip(clip_path, autoplay=False, **settings)
            self._options = settings
            self.setWindowTitle(f'FTHR — {Path(clip_path).name}')
            set_theme_style(self, lambda: self._page._theme_style_factory())
            if session_state is not None:
                page.restore_session_state(session_state)
            if self.isVisible():
                page.show()
            if autoplay:
                page.start_autoplay()
            page.play_btn.setFocus()
        finally:
            self.setUpdatesEnabled(True)
        if replacement_error is not None:
            QMessageBox.warning(self, 'Could not open clip', replacement_error)
            self.update()
            return False
        if outgoing is not None:
            self._content.activate()
            page.layout().activate()
            self._clip_slide = SnapshotSlide(
                self, outgoing, page, 1 if navigation_step > 0 else -1,
                horizontal=True,
            )
            self._clip_slide.show()
            self._clip_slide.raise_()
            self._clip_slide.animation.finished.connect(self._finish_clip_slide)
            self._clip_slide.animation.start()
        self.update()
        return True

    def _finish_clip_slide(self):
        if self._clip_slide is not None:
            self._clip_slide.animation.stop()
            self._clip_slide.hide()
            self._clip_slide.release_snapshots()
            self._clip_slide.deleteLater()
            self._clip_slide = None

    def resizeEvent(self, event):
        self._finish_clip_slide()
        super().resizeEvent(event)
        self._content.invalidate()
        self._content.activate()

    def hideEvent(self, event):
        self._finish_clip_slide()
        self._idle_memory_timer.start()
        super().hideEvent(event)

    def _release_idle_memory(self):
        if not self.isVisible() and self._clip_slide is None and self._page is not None:
            self._page.release_idle_ui_resources()

    def _page_finished(self, result):
        self.done(result)

    def _close(self):
        self._page._close()

    def _teardown_player(self, **options):
        self._finish_clip_slide()
        if self._page is not None:
            self._page._teardown_player(**options)

    def reject(self):
        worker = getattr(self._page, '_export_thread', None)
        if worker is not None and worker.is_alive():
            self._page._close()
            return
        self._teardown_player(wait_for_workers=False)
        super().reject()

    def accept(self):
        self._teardown_player()
        super().accept()

    def closeEvent(self, event):
        worker = getattr(self._page, '_export_thread', None)
        if worker is not None and worker.is_alive():
            event.ignore()
            self._page._close()
            return
        self._teardown_player(wait_for_workers=False)
        super().closeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        self._idle_memory_timer.stop()
        super().showEvent(event)
        if not self._native_style_applied:
            self._native_style_applied = True
            QTimer.singleShot(0, self, self._page._apply_native_style)

    def nativeEvent(self, event_type, message):
        if self._page is not None:
            return self._page.nativeEvent(event_type, message)
        return False, 0
