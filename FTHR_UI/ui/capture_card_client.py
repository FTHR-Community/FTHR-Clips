"""Forward CaptureCard methods to a separate notification process.

Line-delimited stdin commands keep its Qt event loop independent of
main-process finalization and library scans.
"""

import os
import sys
import subprocess
import threading
from pathlib import Path

# Development launches the helper script with the current Python interpreter.
# Frozen builds relaunch the packaged executable with ``--card-process`` because
# no standalone interpreter or source script is available in the package.
_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    _LAUNCH_CMD = [sys.executable, '--card-process']
else:
    _PROCESS_SCRIPT = Path(__file__).parent / 'capture_card_process.py'
    _LAUNCH_CMD = [sys.executable, str(_PROCESS_SCRIPT)]

# Prevent a console window from flashing when the helper starts on Windows.
# This flag is not used on Linux.
_CREATE_NO_WINDOW = 0x08000000
_NO_WINDOW = {'creationflags': _CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


class CaptureCardClient:
    """Sends show commands to a CaptureCard subprocess over stdin."""

    def __init__(self, settings_manager=None):
        self._sm = settings_manager
        self._visuals_enabled = bool(
            settings_manager.get('capture_card_enabled', True)
            if settings_manager is not None else True)
        self._proc: subprocess.Popen | None = None
        self._launch()

    def _launch(self):
        try:
            env = dict(os.environ)
            # Popen's encoding controls the parent pipe only. The detached
            # Python child otherwise decodes redirected stdin with the Windows
            # locale (e.g. cp1252), corrupting names such as KovaaK’s.
            env['PYTHONIOENCODING'] = 'utf-8'
            # Force XWayland only on Linux. Windows must retain its native Qt
            # platform plugin; setting xcb there makes the card process exit.
            if sys.platform != 'win32' and os.environ.get('WAYLAND_DISPLAY'):
                env['QT_QPA_PLATFORM'] = 'xcb'
                env['DISPLAY'] = os.environ.get('DISPLAY', ':0')
            monitor = self._sm.get('notification_monitor', 'auto') if self._sm else 'auto'
            env['FTHR_CARD_SCREEN_NAME'] = monitor
            env['FTHR_CARD_VISUALS_ENABLED'] = (
                '1' if self._visuals_enabled else '0')
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
        """Replace the card helper without waiting for its exit on the UI thread."""
        retired = self._proc
        self._proc = None
        if retired and retired.poll() is None:
            retired.terminate()

            def reap():
                try:
                    retired.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    retired.kill()
                    retired.wait()
                finally:
                    if retired.stdin is not None:
                        retired.stdin.close()

            threading.Thread(
                target=reap, daemon=True, name='fthr-card-reaper').start()
        self._launch()

    def set_visuals_enabled(self, enabled: bool) -> None:
        """Enable or disable the card visuals without muting notification sounds."""
        self._visuals_enabled = bool(enabled)
        self._send(f'visuals|{1 if self._visuals_enabled else 0}')

    def _send(self, cmd: str) -> None:
        if self._sm is not None:
            event = cmd.split('|', 1)[0]
            if event not in {'visuals', 'background_hide', 'quit', 'startup'}:
                mode = self._sm.get('capture_card_mode', None)
                if mode is not None:
                    capture = event in {'clip', 'screenshot', 'recording_saved', 'error'}
                    detection = event in {'prompt', 'capturing'}
                    visible = (mode == 'all' or (mode == 'capture' and capture)
                               or (mode == 'detection' and detection))
                    self.set_visuals_enabled(visible)
        # A broken helper must not permanently disable notifications. Retry
        # the exact event once on a fresh process; later events can also
        # relaunch after a previous launch failure.
        for attempt in range(2):
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._launch()
            if self._proc is None:
                return
            try:
                self._proc.stdin.write(cmd + '\n')
                self._proc.stdin.flush()
                return
            except Exception:
                failed = self._proc
                self._proc = None
                if failed.poll() is None:
                    try:
                        failed.terminate()
                    except Exception:
                        pass
                if attempt == 1:
                    return

    @staticmethod
    def _field(value: str) -> str:
        """Keep the small line protocol unambiguous for window/file names."""
        return str(value).replace('|', '/').replace('\r', ' ').replace('\n', ' ')

    @staticmethod
    def _hold_suffix(hold_duration_ms: int | None) -> str:
        if hold_duration_ms is None:
            return ''
        try:
            value = max(1, int(hold_duration_ms))
        except (TypeError, ValueError, OverflowError):
            return ''
        return f'|{value}'

    def show_clip(self, duration_s: int, fps: int, resolution: str,
                  hold_duration_ms: int | None = None) -> None:
        self._send(
            f'clip|{duration_s}|{fps}|{self._field(resolution)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_screenshot(self, hold_duration_ms: int | None = None) -> None:
        self._send(f'screenshot{self._hold_suffix(hold_duration_ms)}')

    def show_error(self, detail: str = '',
                   hold_duration_ms: int | None = None) -> None:
        self._send(
            f'error|{self._field(detail)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_upload(self, filename: str = '',
                    hold_duration_ms: int | None = None) -> None:
        self._send(
            f'upload|{self._field(filename)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_upload_failed(self, detail: str = '',
                           hold_duration_ms: int | None = None) -> None:
        self._send(
            f'upload_failed|{self._field(detail)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_recording_saved(self, filename: str = '',
                             hold_duration_ms: int | None = None) -> None:
        self._send(
            f'recording_saved|{self._field(filename)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def play_startup(self) -> None:
        self._send('startup')

    def show_prompt(self, text: str,
                    hold_duration_ms: int | None = None) -> None:
        self._send(
            f'prompt|{self._field(text)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_capturing(self, source: str,
                       hold_duration_ms: int | None = None) -> None:
        self._send(
            f'capturing|{self._field(source)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def show_background_capture(
            self, source: str, hold_duration_ms: int | None = None) -> None:
        self._send(
            f'background|{self._field(source)}'
            f'{self._hold_suffix(hold_duration_ms)}')

    def hide_background_capture(self) -> None:
        self._send('background_hide')

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.stdin.write('quit\n')
            proc.stdin.flush()
            # The child joins its blocking stdin reader after app.quit(). EOF
            # releases that reader immediately; leaving the pipe open forced
            # every normal shutdown to wait for the timeout.
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            proc.wait(timeout=0.75)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                proc.kill()
