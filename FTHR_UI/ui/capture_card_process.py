"""Host CaptureCard in its own Qt event loop.

CaptureCardClient sends UTF-8, pipe-delimited commands on stdin.
See handle() for command names, fields, and optional hold durations.
"""

import math
import sys
import os

# Add project root to sys.path so ui/ and core/ modules resolve correctly.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Direct card-process launches must receive the same early process guard.  The
# frozen main entry point and the PyInstaller runtime hook also call this
# policy; the operation is idempotent, so the normal --card-process route is
# safe when it imports this module after the guard has already run.
if __name__ == '__main__':
    from core.windows_startup import apply_startup_policy
    _CARD_STARTUP_POLICY_RESULT = apply_startup_policy()

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QThread, Signal

from ui.app_style import apply_app_style, configure_qt_style
from ui.capture_card import CaptureCard


class _StdinReader(QThread):
    """Reads lines from stdin on a background thread and emits them as signals."""
    command   = Signal(str)
    eof_ready = Signal()   # fired when stdin closes (parent process died)

    def run(self):
        try:
            # Enforce the protocol in frozen hosts too, where Python's startup
            # environment options may be ignored.
            sys.stdin.reconfigure(encoding='utf-8', errors='replace')
            while True:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self.command.emit(line)
        except Exception:
            pass
        self.eof_ready.emit()


def main():
    configure_qt_style()
    app = QApplication(sys.argv)
    apply_app_style(app)
    card = CaptureCard(
        visuals_enabled=os.environ.get('FTHR_CARD_VISUALS_ENABLED', '1')
        not in {'0', 'false', 'off'})

    def optional_hold(parts: list[str], index: int) -> dict:
        if len(parts) <= index:
            return {}
        try:
            return {'hold_duration_ms': max(1, int(parts[index]))}
        except (TypeError, ValueError, OverflowError):
            return {}

    def handle(cmd: str):
        if cmd == 'quit':
            app.quit()
        elif cmd.startswith('screenshot'):
            parts = cmd.split('|', 1)
            card.show_screenshot(**optional_hold(parts, 1))
        elif cmd == 'captured':
            card.show_captured()
        elif cmd.startswith('captured|'):
            parts = cmd.split('|', 3)
            if len(parts) == 4:
                try:
                    seconds = float(parts[1])
                    fps = int(parts[2])
                    if math.isfinite(seconds) and seconds >= 0 and fps > 0:
                        card.show_captured(seconds, fps, parts[3])
                except (ValueError, OverflowError):
                    pass
        elif cmd == 'saving':
            card.show_saving()
        elif cmd.startswith('clip|'):
            parts = cmd.split('|', 4)
            if len(parts) >= 4:
                try:
                    seconds = float(parts[1]) if parts[1] else None
                    if seconds is not None and (not math.isfinite(seconds) or seconds < 0):
                        seconds = None
                    card.show_clip(
                        seconds, int(parts[2]), parts[3],
                        **optional_hold(parts, 4))
                except ValueError:
                    pass
        elif cmd.startswith('error'):
            parts = cmd.split('|', 2)
            detail = parts[1] if len(parts) > 1 else ''
            card.show_error(detail, **optional_hold(parts, 2))
        elif cmd.startswith('upload_failed'):
            parts = cmd.split('|', 2)
            detail = parts[1] if len(parts) > 1 else ''
            card.show_upload_failed(detail, **optional_hold(parts, 2))
        elif cmd.startswith('recording_saved'):
            parts = cmd.split('|', 3)
            filename = parts[1] if len(parts) > 1 else ''
            duration = {}
            if len(parts) > 3:
                try:
                    seconds = float(parts[3])
                    if math.isfinite(seconds) and seconds >= 0:
                        duration = {'duration_seconds': seconds}
                except (ValueError, OverflowError):
                    # Invalid optional timing leaves the saved-file notification intact.
                    pass
            card.show_recording_saved(
                filename, **optional_hold(parts, 2), **duration)
        elif cmd.startswith('upload'):
            parts = cmd.split('|', 2)
            filename = parts[1] if len(parts) > 1 else ''
            card.show_upload(filename, **optional_hold(parts, 2))
        elif cmd == 'startup':
            card.play_startup()
        elif cmd.startswith('prompt|'):
            parts = cmd.split('|', 2)
            card.show_prompt(parts[1], **optional_hold(parts, 2))
        elif cmd.startswith('capturing|'):
            parts = cmd.split('|', 2)
            card.show_capturing(parts[1], **optional_hold(parts, 2))
        elif cmd.startswith('background|'):
            parts = cmd.split('|', 2)
            card.show_background_capture(
                parts[1], **optional_hold(parts, 2))
        elif cmd == 'background_hide':
            card.hide_background_capture()
        elif cmd.startswith('visuals|'):
            card.set_visuals_enabled(cmd[8:] == '1')

    reader = _StdinReader()
    reader.command.connect(handle)
    reader.eof_ready.connect(app.quit)   # exit when parent process closes stdin
    reader.start()

    ret = app.exec()
    reader.wait(1000)
    sys.exit(ret)


if __name__ == '__main__':
    main()
