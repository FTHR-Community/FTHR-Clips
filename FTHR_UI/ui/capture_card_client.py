"""
CaptureCardClient — drop-in replacement for CaptureCard.

Spawns capture_card_process.py as a subprocess so the card's Qt event loop
runs independently from the main process. Main-thread blocking (clip grid
refresh, bridge spin-wait, mic mux, etc.) can never stall the animation.

Public API matches CaptureCard exactly:
    client.show_clip(duration_s, fps, resolution)
    client.show_screenshot()
    client.show_error(detail='')
    client.show_upload(filename='')
    client.close()
"""

import os
import sys
import subprocess
from pathlib import Path

# When frozen by PyInstaller, sys.executable is the .exe launcher, not python,
# and the process script lives inside the extracted _MEIPASS bundle.
if getattr(sys, 'frozen', False):
    _PROCESS_SCRIPT = Path(sys._MEIPASS) / 'ui' / 'capture_card_process.py'

    # sys.executable is the .exe launcher; find the real interpreter instead.
    _PYTHON_EXE = os.path.join(sys._MEIPASS, '..', 'python.exe')
    if not os.path.exists(_PYTHON_EXE):
        # Fallback: look for python.exe next to the exe.
        _PYTHON_EXE = os.path.join(os.path.dirname(sys.executable), 'python.exe')
else:
    _PROCESS_SCRIPT = Path(__file__).parent / 'capture_card_process.py'
    _PYTHON_EXE = sys.executable

# Suppress the console window flash on Windows.
_CREATE_NO_WINDOW = 0x08000000
_NO_WINDOW = {'creationflags': _CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


class CaptureCardClient:
    """Sends show commands to a CaptureCard subprocess over stdin."""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._launch()

    def _launch(self):
        try:
            self._proc = subprocess.Popen(
                [_PYTHON_EXE, str(_PROCESS_SCRIPT)],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                **_NO_WINDOW,
            )
        except Exception as e:
            print(f'[CaptureCard] Failed to launch card process: {e}')
            self._proc = None

    def _send(self, cmd: str) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._launch()
            if self._proc is None:
                return
        try:
            self._proc.stdin.write(cmd + '\n')
            self._proc.stdin.flush()
        except Exception:
            self._proc = None

    def show_clip(self, duration_s: int, fps: int, resolution: str) -> None:
        self._send(f'clip|{duration_s}|{fps}|{resolution}')

    def show_screenshot(self) -> None:
        self._send('screenshot')

    def show_error(self, detail: str = '') -> None:
        self._send(f'error|{detail}')

    def show_upload(self, filename: str = '') -> None:
        self._send(f'upload|{filename}')

    def close(self) -> None:
        self._send('quit')
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
            self._proc = None
