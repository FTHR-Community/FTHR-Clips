"""Native crashes and Qt messages must reach fthr.log, not only fd 2."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import crash_logging

ROOT = Path(__file__).resolve().parent.parent


def _run(script: str, log: Path) -> subprocess.CompletedProcess:
    code = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT / 'FTHR_UI')!r})
        log = open({str(log)!r}, 'a', encoding='utf-8')

        class Tee:
            def __init__(self, f):
                self._log = f
            def write(self, text):
                self._log.write(text)
                self._log.flush()
            def flush(self):
                self._log.flush()

        sys.stderr = Tee(log)
        from core import crash_logging
    """) + textwrap.dedent(script)
    return subprocess.run([sys.executable, '-c', code], capture_output=True,
                          text=True, timeout=60,
                          env={'QT_QPA_PLATFORM': 'offscreen', 'PATH': '/usr/bin:/bin'})


def test_format_names_level_and_category():
    mode = SimpleNamespace(name='QtWarningMsg')
    assert crash_logging.format_qt_message(
        mode, SimpleNamespace(category='qt.qpa.wayland'), 'no EGL') == \
        '[Qt warning] qt.qpa.wayland: no EGL'
    assert crash_logging.format_qt_message(
        SimpleNamespace(name='QtFatalMsg'), SimpleNamespace(category='default'),
        'could not load platform plugin') == \
        '[Qt fatal] could not load platform plugin'


def test_enable_fault_handler_without_a_log_file_is_a_no_op():
    assert crash_logging.enable_fault_handler(None) is False


def test_qt_warning_reaches_the_python_stderr_tee(tmp_path):
    pytest.importorskip('PySide6.QtCore')
    log = tmp_path / 'fthr.log'
    result = _run("""
        assert crash_logging.install_qt_message_handler()
        from PySide6.QtCore import qWarning
        qWarning('fthr-test-warning')
    """, log)
    assert result.returncode == 0, result.stderr
    assert '[Qt warning] fthr-test-warning' in log.read_text(encoding='utf-8')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX signal crash')
def test_segfault_leaves_a_traceback_in_the_log(tmp_path):
    log = tmp_path / 'fthr.log'
    result = _run("""
        assert crash_logging.enable_fault_handler(sys.stderr._log)
        import ctypes
        ctypes.string_at(0)
    """, log)
    assert result.returncode != 0
    text = log.read_text(encoding='utf-8')
    assert 'Fatal Python error' in text
    assert 'Segmentation fault' in text
