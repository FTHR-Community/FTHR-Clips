"""
CaptureCardClient — looks like a CaptureCard, walks like a CaptureCard, but it's
actually a puppet that pokes a whole separate process running the real card.

Yeah this looks cursed but hear me out: the capture card is that little animated
"CLIP SAVED" toast. It needs to slide in and play a smooth animation EXACTLY at
the moment you hit the hotkey — which is also the moment the main process is
busy doing slow blocking junk (spin-waiting on the engine, muxing mic audio with
ffmpeg, refreshing the clip grid). If the card shared the main Qt event loop, the
animation would stutter or freeze right when you want it buttery. Plus on Wayland
you can't cleanly drive a separate top-level animated window from a busy loop.

So the card lives in its own subprocess with its own event loop, and this class
just shoots it one-line text commands over stdin. Nothing the main process does
can stall the animation. don't @ me.

Public API is identical to the real CaptureCard so callers don't know the diff:
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

# Here's the frozen-build sentinel trick. In dev we just run the helper script
# with the same python. But once PyInstaller freezes us into an .exe/AppImage,
# there IS no python.exe to call and no loose .py scripts to point at — it's all
# baked into one binary. So instead we relaunch OURSELVES (sys.executable) with a
# magic '--card-process' flag, and main.py sees that flag on startup and says
# "ah, I'm the card today" and boots the card event loop instead of the full app.
# Same binary, two personalities. classic "it works on my machine" averted.
_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    _LAUNCH_CMD = [sys.executable, '--card-process']
else:
    _PROCESS_SCRIPT = Path(__file__).parent / 'capture_card_process.py'
    _LAUNCH_CMD = [sys.executable, str(_PROCESS_SCRIPT)]

# Stops Windows from flashing a black console window every time the card spawns.
# Looks unprofessional as hell otherwise. No-op on Linux.
_CREATE_NO_WINDOW = 0x08000000
_NO_WINDOW = {'creationflags': _CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


class CaptureCardClient:
    """Sends show commands to a CaptureCard subprocess over stdin."""

    def __init__(self, settings_manager=None):
        self._sm = settings_manager
        self._proc: subprocess.Popen | None = None
        self._launch()

    def _launch(self):
        try:
            # Force XWayland so the card subprocess can position its own window.
            # On Wayland, compositors ignore client-side move() calls for toplevel
            # windows and center them instead. xcb/X11 via XWayland respects them.
            env = {**os.environ, 'QT_QPA_PLATFORM': 'xcb', 'DISPLAY': os.environ.get('DISPLAY', ':0')}
            monitor = self._sm.get('notification_monitor', 'auto') if self._sm else 'auto'
            env['FTHR_CARD_SCREEN_NAME'] = monitor
            self._proc = subprocess.Popen(
                _LAUNCH_CMD,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding='utf-8',
                env=env,
                **_NO_WINDOW,
            )
        except Exception as e:
            print(f'[CaptureCard] Failed to launch card process: {e}')
            self._proc = None

    def restart(self):
        """Terminate the card subprocess; it will relaunch on the next show call."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._launch()

    def _send(self, cmd: str) -> None:
        if self._proc is None:
            return
        # If the card process died (crashed, got OOM-killed, whatever), poll()
        # returns non-None. Just respawn it lazily before sending. Self-healing
        # on a budget — narrator: it was not, in fact, fine without this check.
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

    def show_prompt(self, text: str) -> None:
        self._send(f'prompt|{text}')

    def close(self) -> None:
        self._send('quit')
        if self._proc:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.terminate()
            self._proc = None
