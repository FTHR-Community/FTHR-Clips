"""Shared clip preparation workers with GUI-owned result channels."""
from PySide6.QtCore import QObject, QRunnable, Signal
from PySide6.QtWidgets import QApplication


class _ClipWorkerSignals(QObject):
    ready = Signal(object)


class ClipWorker(QRunnable):
    """Own one clip worker's queued result channel until GUI-side deletion."""

    def __init__(self):
        super().__init__()
        self.signals = _ClipWorkerSignals(QApplication.instance())

    def run(self):
        try:
            self._run()
        finally:
            self.signals.deleteLater()


class CompatibilitySourceWorker(ClipWorker):
    """Prepare a display-compatible source before opening the player."""

    def __init__(self, path, cancelled):
        super().__init__()
        self.path = path
        self.cancelled = cancelled

    def _run(self):
        from core.playback_proxy import prepare_compatibility_path
        path = prepare_compatibility_path(self.path, self.cancelled)
        if not self.cancelled.is_set():
            self.signals.ready.emit(path)
