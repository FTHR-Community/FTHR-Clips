"""File logging shared by the application and its lightweight card entry point."""
from datetime import datetime
from pathlib import Path
import sys


class _LogTee:
    """Mirror a stream (may be None in frozen GUI builds) into a log file."""
    _MAX_BYTES = 2 * 1024 * 1024

    def __init__(self, stream, log_path):
        self._stream = stream
        self._log_path = log_path
        try:
            if log_path.exists() and log_path.stat().st_size > self._MAX_BYTES:
                log_path.replace(log_path.with_suffix('.log.old'))
            self._log = open(log_path, 'a', encoding='utf-8', errors='replace')
        except Exception:
            self._log = None

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                pass
        if self._log is not None:
            try:
                self._log.write(text)
                self._log.flush()
            except Exception:
                pass

    def flush(self):
        for stream in (self._stream, self._log):
            if stream is not None:
                try:
                    stream.flush()
                except Exception:
                    pass


def setup_file_logging(*, startup_policy_result=None):
    try:
        log_dir = Path.home() / '.fthr' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'fthr.log'
        sys.stdout = _LogTee(sys.stdout, log_file)
        sys.stderr = _LogTee(sys.stderr, log_file)
        from version import __version__
        print(f'\n===== FTHRClips {__version__} started '
              f'{datetime.now():%Y-%m-%d %H:%M:%S} ({sys.platform}) =====')
        from core import diagnostics
        diagnostics.configure(log_file=log_file)
    except Exception as error:
        print(f'[Startup] File logging unavailable: {type(error).__name__}: {error}',
              file=sys.__stderr__ or sys.stderr)
    if startup_policy_result is not None:
        detail = (f' error={startup_policy_result.error_code}'
                  if startup_policy_result.error_code else '')
        print(f'[Startup] Extension-point policy: {startup_policy_result.status}{detail}')
