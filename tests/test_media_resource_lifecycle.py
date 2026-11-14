"""Bound diagnostic memory and keep pipe/file ownership through failures."""
import io
from pathlib import Path
import sys
import threading
import tracemalloc
from types import SimpleNamespace

from core.field_diagnostics import EngineLogCapture
from core import media_process


def test_engine_log_reuses_writer_and_closes_it_at_eof(monkeypatch, tmp_path):
    path = tmp_path / 'engine.log'
    lines = ''.join(f'line {index}\n' for index in range(500))
    capture = EngineLogCapture(path)
    capture._stream = io.StringIO(lines)
    opened = []
    original = Path.open

    def open_file(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        if target == path and args and args[0] == 'a':
            opened.append(result)
        return result

    monkeypatch.setattr(Path, 'open', open_file)
    capture._reader()
    assert path.read_text() == lines
    assert len(opened) == 1
    assert opened[0].closed


def test_engine_log_rotates_without_exceeding_utf8_budget(tmp_path):
    path = tmp_path / 'engine.log'
    lines = ''.join(f'{index}: ' + 'é' * 12 + '\n' for index in range(20))
    capture = EngineLogCapture(path, max_bytes=80)
    capture._stream = io.StringIO(lines)
    capture._reader()
    assert 0 < path.stat().st_size <= 80
    assert 0 < path.with_suffix('.log.old').stat().st_size <= 80
    assert path.read_text(encoding='utf-8').endswith('19: ' + 'é' * 12 + '\n')
    assert capture.startup_text() == lines


def test_log_write_failure_reopens_and_keeps_native_events(monkeypatch, tmp_path):
    path = tmp_path / 'engine.log'
    events = []
    session = SimpleNamespace(emit_native=events.append)
    capture = EngineLogCapture(path, session)
    capture._stream = io.StringIO(
        'FTHR_DIAGNOSTIC_EVENT {"event":"first"}\n'
        'FTHR_DIAGNOSTIC_EVENT {"event":"second"}\n')
    original = Path.open
    opened = []

    class FailingWriter:
        def __init__(self, handle):
            self.handle = handle

        def fileno(self):
            return self.handle.fileno()

        def write(self, _line):
            raise OSError('storage unavailable')

        def close(self):
            self.handle.close()

    def open_file(target, *args, **kwargs):
        result = original(target, *args, **kwargs)
        if target == path and args and args[0] == 'a':
            opened.append(result)
            if len(opened) == 1:
                return FailingWriter(result)
        return result

    monkeypatch.setattr(Path, 'open', open_file)
    capture._reader()
    assert [event['event'] for event in events] == ['first', 'second']
    assert len(opened) == 2 and all(handle.closed for handle in opened)
    assert 'second' in path.read_text() and 'first' not in path.read_text()


def test_nonstreaming_child_keeps_only_stderr_tail_in_memory():
    before = media_process.media_process_snapshot()[0]
    tracemalloc.start()
    try:
        result = media_process.run_media_process(
            [sys.executable, '-B', '-c',
             'import sys;sys.stderr.buffer.write(b"e"*(16*1024*1024)+b"END");'
             'sys.stdout.buffer.write(b"payload")'],
            threading.Event(), timeout_seconds=10)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == (0, b'payload', b'e' * (32_768 - 3) + b'END')
    assert peak < 4 * 1024 * 1024
    assert media_process.media_process_snapshot()[0] == before


def test_nonstreaming_child_preserves_failure_output_and_exitcode():
    result = media_process.run_media_process(
        [sys.executable, '-B', '-c',
         'import sys;sys.stdout.write("output");sys.stderr.write("error");sys.exit(7)'],
        threading.Event(), timeout_seconds=5)
    assert result == (7, b'output', b'error')


def test_nonstreaming_timeout_reaps_child_and_readers():
    before = media_process.media_process_snapshot()[0]
    result = media_process.run_media_process(
        [sys.executable, '-B', '-c', 'import time;time.sleep(10)'],
        threading.Event(), timeout_seconds=0.1)
    assert result is None
    assert media_process.media_process_snapshot()[0] == before
