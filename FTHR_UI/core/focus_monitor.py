import json
import subprocess
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class FocusMonitor(QObject):
    focus_lost     = pyqtSignal()
    focus_regained = pyqtSignal()

    def __init__(self, target_name: str = '', parent=None):
        super().__init__(parent)
        self._target = target_name
        self._focused = True
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)

    def set_target(self, name: str):
        self._target = name
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
        if not self._target:
            return
        try:
            result = subprocess.run(
                ['hyprctl', 'activewindow', '-j'],
                capture_output=True, timeout=1,
            )
            data = json.loads(result.stdout.decode(errors='replace'))
            title = data.get('title', '') or data.get('class', '')
            focused = self._title_matches(title)
        except Exception:
            return
        if focused and not self._focused:
            self._focused = True
            self.focus_regained.emit()
        elif not focused and self._focused:
            self._focused = False
            self.focus_lost.emit()
