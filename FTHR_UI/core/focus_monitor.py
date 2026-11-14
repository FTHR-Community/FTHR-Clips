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
        self._generation = 0
        self._timer   = QTimer(self)
        self._timer.setInterval(2000)
        # Keep foreground-window lookup off the Qt main thread.
        self._timer.timeout.connect(self._start_poll)
        self._title_polled.connect(self._apply_title)

    def _start_poll(self):
        if not self._timer.isActive() or not self._target or self._poll_running:
            return
        self._poll_running = True
        generation = self._generation

        def _work():
            try:
                title = _get_active_window_title()
            except Exception:
                title = None
            try:
                self._title_polled.emit((generation, title))
            except RuntimeError:
                # The owning QObject can be deleted while a lookup finishes.
                pass

        threading.Thread(target=_work, daemon=True,
                         name='fthr-focus-poll').start()

    def _apply_title(self, result):
        generation, title = result
        self._poll_running = False
        if not self._timer.isActive():
            return
        if generation == self._generation:
            self._evaluate(title)
        else:
            self._start_poll()

    def set_target(self, name: str):
        if name != self._target:
            self._generation += 1
        self._target  = name
        self._focused = True

    def start(self):
        self._generation += 1
        self._focused = True
        self._timer.start()

    def stop(self):
        self._generation += 1
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
