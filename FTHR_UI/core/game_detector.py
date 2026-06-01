from PyQt6.QtCore import QObject, QTimer, pyqtSignal


class GameDetector(QObject):
    game_appeared = pyqtSignal(dict)   # new is_game=True window
    game_closed   = pyqtSignal(int)    # hwnd of a game that disappeared

    def __init__(self, enumerate_fn=None, parent=None):
        super().__init__(parent)
        if enumerate_fn is None:
            from ui.capture_settings_widget import _enumerate_capturable_windows
            enumerate_fn = _enumerate_capturable_windows
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
