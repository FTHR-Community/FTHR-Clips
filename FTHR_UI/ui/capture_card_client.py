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

# When frozen by PyInstaller there is no python.exe in the bundle.
# Instead, relaunch the frozen exe itself with --card-process so main.py
# routes it into the card event loop rather than the main application.
_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    _LAUNCH_CMD = [sys.executable, '--card-process']
else:
    _PROCESS_SCRIPT = Path(__file__).parent / 'capture_card_process.py'
    _LAUNCH_CMD = [sys.executable, str(_PROCESS_SCRIPT)]

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
                _LAUNCH_CMD,
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
