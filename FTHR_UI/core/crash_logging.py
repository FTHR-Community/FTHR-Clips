"""Put native crashes and Qt's own messages into fthr.log.

Python's stdout/stderr tee only sees text written through ``sys.stderr``.
Qt writes its warnings and fatal errors (for example "could not load the Qt
platform plugin") straight to file descriptor 2, and a segfault in native code
prints nothing at all, so a launch that dies before the window appears left
the log ending at the start banner. Both hooks write to the same file.
"""

from __future__ import annotations

import faulthandler
import sys

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


def enable_fault_handler(log_file) -> bool:
    """Dump Python tracebacks for SIGSEGV/SIGABRT/SIGBUS/SIGFPE/SIGILL."""
    global _fault_file
    if log_file is None:
        return False
    try:
        faulthandler.enable(file=log_file, all_threads=True)
    except (AttributeError, OSError, ValueError, RuntimeError):
        # No usable file descriptor (closed log, frozen GUI build without a
        # real file): crash tracebacks are a diagnostic extra, not required.
        return False
    _fault_file = log_file
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
