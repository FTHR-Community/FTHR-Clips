"""Put native crashes and Qt's own messages into fthr.log.

Python's stdout/stderr tee only sees text written through ``sys.stderr``.
Qt writes its warnings and fatal errors (for example "could not load the Qt
platform plugin") straight to file descriptor 2, and a segfault in native code
prints nothing at all, so a launch that dies before the window appears left
the log ending at the start banner.

Qt messages go through the fthr.log tee. Crash tracebacks go to their own
fthr-crash.log: faulthandler writes through a raw file descriptor, and fthr.log
is renamed by the diagnostics RotatingFileHandler, after which that descriptor
would point at fthr.log.1 or at an unlinked file.
"""

from __future__ import annotations

import faulthandler
import os
import sys
from datetime import datetime
from pathlib import Path

_QT_LEVELS = {
    'QtDebugMsg': 'debug',
    'QtInfoMsg': 'info',
    'QtWarningMsg': 'warning',
    'QtCriticalMsg': 'critical',
    'QtFatalMsg': 'fatal',
}

# faulthandler writes through the file descriptor at crash time; keep the
# file object alive for the life of the process.
_fault_file = None
_CRASH_LOG_MAX_BYTES = 1024 * 1024


def enable_fault_handler(crash_log: Path | None) -> bool:
    """Dump Python and C tracebacks for SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL.

    ``crash_log`` is owned by this module alone; it is rotated only here, at
    startup, so the descriptor faulthandler holds never goes stale.
    """
    global _fault_file
    if crash_log is None:
        return False
    try:
        crash_log = Path(crash_log)
        if crash_log.exists() and crash_log.stat().st_size > _CRASH_LOG_MAX_BYTES:
            crash_log.replace(crash_log.with_suffix('.log.old'))
        handle = open(crash_log, 'a', encoding='utf-8', errors='replace')
        handle.write(f'===== started {datetime.now():%Y-%m-%d %H:%M:%S} '
                     f'pid {os.getpid()} =====\n')
        handle.flush()
        faulthandler.enable(file=handle, all_threads=True)
    except (AttributeError, OSError, ValueError, RuntimeError):
        # No writable log directory or no usable descriptor: crash
        # tracebacks are a diagnostic extra, not required to run.
        return False
    _fault_file = handle
    return True


def format_qt_message(mode, context, message: str) -> str:
    level = _QT_LEVELS.get(getattr(mode, 'name', str(mode)), str(mode))
    category = getattr(context, 'category', None) or ''
    prefix = f'[Qt {level}]' if not category or category == 'default' \
        else f'[Qt {level}] {category}:'
    return f'{prefix} {message}'


def _qt_message_handler(mode, context, message):
    try:
        sys.stderr.write(format_qt_message(mode, context, message) + '\n')
        sys.stderr.flush()
    except Exception:
        # A logging hook must never be the thing that crashes Qt.
        pass


def install_qt_message_handler() -> bool:
    """Route qDebug/qWarning/qFatal through sys.stderr (the fthr.log tee).

    Install before QApplication so platform-plugin failures are captured; Qt
    still aborts after a fatal message, but the reason is in the log first.
    """
    try:
        from PySide6.QtCore import qInstallMessageHandler
    except ImportError:
        # Tools that import main.py without Qt installed have no Qt messages
        # to route.
        return False
    qInstallMessageHandler(_qt_message_handler)
    return True
