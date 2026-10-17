"""Cancellable FFmpeg exports with staged output and bounded waits.

The UI owns the worker thread; ExportJob owns its child process and files.
The lifecycle is independent of Qt so editor and share paths can reuse it.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import queue
import inspect
import subprocess
import threading
import time
from typing import Callable, Sequence


class ExportState(str, Enum):
    IDLE = 'IDLE'
    REQUESTED = 'REQUESTED'
    PREPARING = 'PREPARING'
    EXPORTING = 'EXPORTING'
    FINALIZING = 'FINALIZING'
    COMPLETED = 'COMPLETED'
    FAILED = 'FAILED'
    CANCELLED = 'CANCELLED'
    TIMED_OUT = 'TIMED_OUT'


_TERMINAL = frozenset({
    ExportState.COMPLETED, ExportState.FAILED,
    ExportState.CANCELLED, ExportState.TIMED_OUT,
})
_ALLOWED: dict[ExportState, frozenset[ExportState]] = {
    ExportState.IDLE: frozenset({ExportState.REQUESTED}),
    ExportState.REQUESTED: frozenset({ExportState.PREPARING,
                                      ExportState.CANCELLED,
                                      ExportState.FAILED}),
    ExportState.PREPARING: frozenset({ExportState.EXPORTING,
                                      ExportState.CANCELLED,
                                      ExportState.FAILED,
                                      ExportState.TIMED_OUT}),
    ExportState.EXPORTING: frozenset({ExportState.FINALIZING,
                                      ExportState.CANCELLED,
                                      ExportState.FAILED,
                                      ExportState.TIMED_OUT}),
    ExportState.FINALIZING: frozenset({ExportState.COMPLETED,
                                       ExportState.CANCELLED,
                                       ExportState.FAILED,
                                       ExportState.TIMED_OUT}),
    ExportState.COMPLETED: frozenset(),
    ExportState.FAILED: frozenset(),
    ExportState.CANCELLED: frozenset(),
    ExportState.TIMED_OUT: frozenset(),
}


@dataclass(frozen=True)
class ExportResult:
    state: ExportState
    detail: str = ''
    returncode: int | None = None
    stderr_tail: str = ''
    elapsed_seconds: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.state is ExportState.COMPLETED


class ExportJob:
    """Run an export, validate its staged output, then publish the final path.

    ``command`` must write to ``staged_path``. The default validator checks
    only existence and size; media callers should supply a probe/decode check.
    """

    def __init__(
            self,
            command: Sequence[str],
            staged_path: str | os.PathLike[str],
            final_path: str | os.PathLike[str],
            *,
            overall_timeout: float | None = None,
            inactivity_timeout: float = 120.0,
            cancel_wait: float = 2.0,
            stderr_limit: int = 8192,
            popen_factory: Callable[..., object] | None = None,
            validate_output: Callable[[Path], object] | None = None,
            commit_output: Callable[[str, str], object] | None = None,
            state_callback: Callable[[ExportState], None] | None = None,
            progress_callback: Callable[[float], None] | None = None,
            clock: Callable[[], float] = time.monotonic,
            popen_kwargs: dict | None = None,
            cancel_event: threading.Event | None = None):
        self.command = self._with_progress_args(tuple(str(item) for item in command))
        self.staged_path = Path(staged_path)
        self.final_path = Path(final_path)
        self.overall_timeout = (
            None if overall_timeout is None else max(0.1, float(overall_timeout)))
        self.inactivity_timeout = max(0.1, float(inactivity_timeout))
        self.cancel_wait = max(0.05, float(cancel_wait))
        self.stderr_limit = max(256, int(stderr_limit))
        self._popen_factory = popen_factory or subprocess.Popen
        self._validate_output = validate_output
        self._commit_output = commit_output or self._default_commit
        self._state_callback = state_callback
        self._progress_callback = progress_callback
        self._clock = clock
        self._popen_kwargs = dict(popen_kwargs or {})
        self._final_existed_at_start = self.final_path.exists()
        self._cancel_event = cancel_event or threading.Event()
        self._process = None
        self._process_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = ExportState.IDLE
        self._stderr_lines: deque[str] = deque()
        self._stderr_chars = 0
        self._stderr_lock = threading.Lock()
        self._last_progress_seconds = -1.0

    @staticmethod
    def _with_progress_args(command: tuple[str, ...]) -> tuple[str, ...]:
        if '-progress' in command:
            return command
        if not command:
            return command
        # The output is conventionally the last argument.  Inserting before
        # it works for FFmpeg's global options and preserves the output path.
        return command[:-1] + ('-progress', 'pipe:1', '-nostats', command[-1])

    @property
    def state(self) -> ExportState:
        with self._state_lock:
            return self._state

    @property
    def process(self):
        with self._process_lock:
            return self._process

    def _transition(self, state: ExportState) -> None:
        with self._state_lock:
            current = self._state
            if state is current:
                return
            if state not in _ALLOWED[current]:
                raise RuntimeError(f'illegal export transition {current.value} -> {state.value}')
            self._state = state
        if self._state_callback is not None:
            self._state_callback(state)

    def cancel(self) -> None:
        """Request cancellation and promptly terminate a running child."""

        # Arbitration with COMPLETED/CANCELLED must happen under the same lock
        # used by _complete_after_commit.  Otherwise a cancel arriving between
        # the terminal check and Event.set() can be lost after publication.
        if not self.request_cancel():
            return
        process = self.process
        if process is not None and self._poll(process) is None:
            self._terminate_and_reap(process)

    def request_cancel(self) -> bool:
        """Let the export worker terminate and reap its child off the UI thread."""
        with self._state_lock:
            if self._state in _TERMINAL:
                return False
            self._cancel_event.set()
        return True

    @staticmethod
    def _poll(process) -> int | None:
        try:
            return process.poll()
        except (AttributeError, OSError):
            # A foreign test double or closing process may not expose poll;
            # the caller treats this as still running and applies its deadline.
            return None

    def _set_process(self, process) -> None:
        with self._process_lock:
            self._process = process

    def _terminate_and_reap(self, process) -> None:
        try:
            process.terminate()
        except (AttributeError, OSError):
            # Termination is best effort; the kill fallback below remains bounded.
            pass
        try:
            process.wait(timeout=self.cancel_wait)
            return
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            # A child that ignores terminate is handled by the kill fallback.
            pass
        try:
            process.kill()
        except (AttributeError, OSError):
            # The process may have exited between terminate and kill.
            pass
        try:
            process.wait(timeout=self.cancel_wait)
        except (subprocess.TimeoutExpired, TimeoutError, OSError):
            # The parent has made the bounded best effort.  The result remains
            # terminal and the staged path is never published.
            pass

    def _append_stderr(self, line: str) -> None:
        with self._stderr_lock:
            normalized = line.rstrip()
            self._stderr_lines.append(normalized)
            self._stderr_chars += len(normalized)
            while self._stderr_lines and self._stderr_chars > self.stderr_limit:
                self._stderr_chars -= len(self._stderr_lines.popleft())

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            return '\n'.join(self._stderr_lines)[-self.stderr_limit:]

    @staticmethod
    def _default_commit(staged: str, final: str) -> None:
        os.replace(staged, final)

    def _discard_staged(self) -> None:
        try:
            self.staged_path.unlink(missing_ok=True)
        except OSError:
            # Cleanup is best effort; the file is never promoted after failure.
            pass

    def _discard_new_final(self) -> None:
        if self._final_existed_at_start:
            return
        try:
            self.final_path.unlink(missing_ok=True)
        except OSError:
            # Do not mask the original finalization failure with cleanup noise.
            pass

    def _reader(self, stream, channel: str,
                progress_events: queue.Queue[str]) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                if isinstance(line, bytes):
                    line = line.decode('utf-8', errors='replace')
                line = str(line)
                if channel == 'stderr':
                    # Keep the newest bounded diagnostic tail directly from
                    # the reader.  Stderr can never consume progress capacity.
                    self._append_stderr(line)
                    continue
                try:
                    progress_events.put_nowait(line)
                except queue.Full:
                    # Preserve the newest progress record when a noisy child
                    # outruns the UI-side poll loop; never block the pipe.
                    try:
                        progress_events.get_nowait()
                    except queue.Empty:
                        # The consumer may have drained the queue meanwhile.
                        pass
                    try:
                        progress_events.put_nowait(line)
                    except queue.Full:
                        # A newer progress line already won the race.
                        pass
        except (OSError, ValueError):
            # Reader shutdown races are expected after bounded process cleanup.
            pass

    def _handle_progress(self, line: str, duration: float | None) -> bool:
        key, separator, value = line.strip().partition('=')
        if not separator:
            return False
        if key not in {'out_time_ms', 'out_time_us'}:
            return line.strip() == 'progress=end'
        try:
            seconds = int(value) / 1_000_000
        except ValueError:
            # Malformed progress telemetry must not keep an export alive.
            return False
        if seconds < self._last_progress_seconds:
            return False
        if seconds == self._last_progress_seconds:
            return False
        self._last_progress_seconds = seconds
        if self._progress_callback is not None:
            percent = (min(99.0, max(0.0, seconds / duration * 100.0))
                       if duration and duration > 0 else seconds)
            self._progress_callback(percent)
        return True

    def _result(self, state: ExportState, detail: str, started: float,
                returncode: int | None = None) -> ExportResult:
        self._discard_staged()
        if state is not ExportState.COMPLETED:
            self._discard_new_final()
        return ExportResult(state, detail, returncode, self._stderr_tail(),
                            max(0.0, self._clock() - started))

    def _complete_after_commit(self, started: float,
                               returncode: int | None) -> ExportResult | None:
        """Atomically arbitrate cancellation against the final transition."""

        with self._state_lock:
            if self._cancel_event.is_set():
                self._state = ExportState.CANCELLED
                cancelled = True
            else:
                self._state = ExportState.COMPLETED
                cancelled = False
        if self._state_callback is not None:
            self._state_callback(
                ExportState.CANCELLED if cancelled else ExportState.COMPLETED)
        if cancelled:
            return self._result(
                ExportState.CANCELLED, 'Export cancelled', started, returncode)
        return None

    def _validate_staged_output(self) -> None:
        if self._validate_output is None:
            return
        try:
            parameters = inspect.signature(self._validate_output).parameters.values()
            accepts_cancel = any(
                parameter.kind is parameter.VAR_POSITIONAL
                or parameter.kind is parameter.KEYWORD_ONLY
                and parameter.name == 'cancel_event'
                for parameter in parameters)
            positional = [parameter for parameter in parameters
                          if parameter.kind in (parameter.POSITIONAL_ONLY,
                                                parameter.POSITIONAL_OR_KEYWORD)]
            accepts_cancel = accepts_cancel or len(positional) >= 2
        except (TypeError, ValueError):
            # C-extension callables may not expose a signature; preserve the
            # historical one-argument validator contract in that case.
            accepts_cancel = False
        if accepts_cancel:
            self._validate_output(self.staged_path, self._cancel_event)
        else:
            self._validate_output(self.staged_path)

    def run(self, *, duration: float | None = None) -> ExportResult:
        started = self._clock()
        process = None
        readers: list[threading.Thread] = []
        reader_streams = []
        try:
            self._transition(ExportState.REQUESTED)
            if self._cancel_event.is_set():
                self._transition(ExportState.CANCELLED)
                return self._result(ExportState.CANCELLED, 'Export cancelled', started)
            self._transition(ExportState.PREPARING)
            if self._cancel_event.is_set():
                self._transition(ExportState.CANCELLED)
                return self._result(ExportState.CANCELLED, 'Export cancelled', started)
            try:
                process = self._popen_factory(
                    list(self.command), stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True, encoding='utf-8',
                    errors='replace', **self._popen_kwargs)
            except Exception as error:
                self._transition(ExportState.FAILED)
                return self._result(ExportState.FAILED, str(error), started)
            self._set_process(process)
            self._transition(ExportState.EXPORTING)
            progress_events: queue.Queue[str] = queue.Queue(maxsize=512)
            for channel in ('stdout', 'stderr'):
                stream = getattr(process, channel, None)
                if stream is None:
                    continue
                reader_streams.append(stream)
                reader = threading.Thread(
                    target=self._reader, args=(stream, channel, progress_events),
                    name=f'fthr-export-{channel}', daemon=True)
                reader.start()
                readers.append(reader)

            last_progress = started
            timed_out = False
            while True:
                now = self._clock()
                if self._cancel_event.is_set():
                    self._terminate_and_reap(process)
                    self._transition(ExportState.CANCELLED)
                    return self._result(ExportState.CANCELLED, 'Export cancelled', started)
                returncode = self._poll(process)
                if returncode is not None:
                    break
                if (self.overall_timeout is not None
                        and now - started >= self.overall_timeout):
                    timed_out = True
                elif now - last_progress >= self.inactivity_timeout:
                    timed_out = True
                if timed_out:
                    self._terminate_and_reap(process)
                    self._transition(ExportState.TIMED_OUT)
                    return self._result(
                        ExportState.TIMED_OUT, 'Export timed out', started)
                try:
                    line = progress_events.get(timeout=0.05)
                except queue.Empty:
                    # No progress arrived during this poll interval; the
                    # monotonic deadline below remains authoritative.
                    continue
                if self._handle_progress(line, duration):
                    last_progress = self._clock()

            if self._cancel_event.is_set():
                self._transition(ExportState.CANCELLED)
                return self._result(ExportState.CANCELLED, 'Export cancelled', started,
                                    returncode)
            if returncode != 0:
                self._transition(ExportState.FAILED)
                return self._result(
                    ExportState.FAILED,
                    f'FFmpeg exited with code {returncode}', started, returncode)

            self._transition(ExportState.FINALIZING)
            if not self.staged_path.is_file() or self.staged_path.stat().st_size <= 0:
                raise RuntimeError('Export produced no usable output')
            self._validate_staged_output()
            if self._cancel_event.is_set():
                self._transition(ExportState.CANCELLED)
                return self._result(ExportState.CANCELLED, 'Export cancelled', started,
                                    returncode)
            self._commit_output(str(self.staged_path), str(self.final_path))
            # A custom transactional commit may copy rather than rename.  A
            # successful export must never leave a final-looking partial next
            # to the published file.
            self._discard_staged()
            if (not self.final_path.is_file()
                    or self.final_path.stat().st_size <= 0):
                raise RuntimeError('Export finalization produced no usable output')
            cancelled_result = self._complete_after_commit(started, returncode)
            if cancelled_result is not None:
                return cancelled_result
            return ExportResult(
                ExportState.COMPLETED, '', returncode, self._stderr_tail(),
                max(0.0, self._clock() - started))
        except Exception as error:
            if self._cancel_event.is_set() and self.state not in _TERMINAL:
                self._transition(ExportState.CANCELLED)
                self._discard_staged()
                self._discard_new_final()
                return ExportResult(
                    ExportState.CANCELLED, 'Export cancelled', None,
                    self._stderr_tail(), max(0.0, self._clock() - started))
            if self.state not in _TERMINAL:
                self._transition(ExportState.FAILED)
            self._discard_staged()
            self._discard_new_final()
            return ExportResult(
                ExportState.FAILED, str(error), None, self._stderr_tail(),
                max(0.0, self._clock() - started))
        finally:
            # Every return path, including cancellation and timeout, must
            # terminate the child before closing its pipes. Closing a live
            # FFmpeg pipe can otherwise race the reader and leak a daemon
            # thread into the next sequential export.
            if process is not None and self._poll(process) is None:
                self._terminate_and_reap(process)
            for stream in reader_streams:
                try:
                    stream.close()
                except (AttributeError, OSError, ValueError):
                    # The process may have already closed the pipe itself.
                    pass
            for reader in readers:
                reader.join(timeout=0.25)
            with self._process_lock:
                self._process = None


__all__ = ['ExportJob', 'ExportResult', 'ExportState']
