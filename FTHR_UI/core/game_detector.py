import sys
import json
import subprocess
from PyQt6.QtCore import QObject, QTimer, pyqtSignal


def _enumerate_linux_windows() -> list:
    """
    Return a list of open windows on Hyprland via hyprctl clients.
    Falls back to empty list on non-Hyprland compositors.
    Each dict has keys: hwnd (int), display_name (str), is_game (bool).
    """
    try:
        result = subprocess.run(
            ['hyprctl', 'clients', '-j'],
            capture_output=True, timeout=2,
        )
        clients = json.loads(result.stdout.decode(errors='replace'))
        windows = []
        for c in clients:
            title = c.get('title') or c.get('class') or ''
            if not title:
                continue
            # Use address hash as a stable pseudo-hwnd
            hwnd = int(c.get('address', '0x0'), 16) & 0xFFFFFFFF
            is_game = bool(c.get('fullscreen')) or c.get('fullscreenMode', 0) > 0
            windows.append({
                'hwnd':         hwnd,
                'display_name': title,
                'title':        title,
                'is_game':      is_game,
            })
        return windows
    except Exception:
        return []


class GameDetector(QObject):
    game_appeared = pyqtSignal(dict)   # new is_game=True window
    game_closed   = pyqtSignal(int)    # hwnd of a game that disappeared

    def __init__(self, enumerate_fn=None, parent=None):
        super().__init__(parent)
        if enumerate_fn is None:
            if sys.platform == 'win32':
                from ui.capture_settings_widget import _enumerate_capturable_windows
                enumerate_fn = _enumerate_capturable_windows
            else:
                enumerate_fn = _enumerate_linux_windows
        self._enumerate = enumerate_fn
        self._known: dict[int, dict] = {}   # hwnd → window dict
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self._poll)

    def start(self):
        self._known.clear()
        self._timer.start()

    def stop(self):
        self._timer.stop()

    def _poll(self):
        current = {w['hwnd']: w for w in self._enumerate() if w.get('is_game')}
        for hwnd, window in current.items():
            if hwnd not in self._known:
                self._known[hwnd] = window
                self.game_appeared.emit(window)
        for hwnd in list(self._known):
            if hwnd not in current:
                del self._known[hwnd]
                self.game_closed.emit(hwnd)
