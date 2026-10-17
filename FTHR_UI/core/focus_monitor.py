import threading
from PySide6.QtCore import QObject, QTimer, Signal


def _get_active_window_title() -> str | None:
    """Read the foreground window title through the native Windows detector."""
    from core.game_detector import get_foreground_window
    window = get_foreground_window()
    return window.title if window else None


class FocusMonitor(QObject):
    focus_lost     = Signal()
    focus_regained = Signal()
    _title_polled  = Signal(object)  # worker thread → main thread

    def __init__(self, target_name: str = '', parent=None):
        super().__init__(parent)
        self._target  = target_name
        self._focused = True
        self._poll_running = False
        self._timer   = QTimer(self)
        self._timer.setInterval(2000)
        # Keep foreground-window lookup off the Qt main thread.
        self._timer.timeout.connect(self._start_poll)
        self._title_polled.connect(self._apply_title)

    def _start_poll(self):
        if not self._target or self._poll_running:
            return
        self._poll_running = True

        def _work():
            try:
                title = _get_active_window_title()
            except Exception:
                title = None
            self._title_polled.emit(title)

        threading.Thread(target=_work, daemon=True,
                         name='fthr-focus-poll').start()

    def _apply_title(self, title):
        self._poll_running = False
        self._evaluate(title)

    def set_target(self, name: str):
        self._target  = name
        self._focused = True

    def start(self):
        self._focused = True
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _title_matches(self, title: str) -> bool:
        if not self._target:
            return True
        return self._target.lower() in title.lower()

    def _poll(self):
        """Synchronous poll — used by tests and as a manual refresh."""
        if not self._target:
            return
        self._evaluate(_get_active_window_title())

    def _evaluate(self, title):
        if title is None or not self._target:
            return
        focused = self._title_matches(title)
        if focused and not self._focused:
            self._focused = True
            self.focus_regained.emit()
        elif not focused and self._focused:
            self._focused = False
            self.focus_lost.emit()
