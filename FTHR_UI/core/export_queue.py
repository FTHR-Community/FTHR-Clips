"""Application-owned exports, shared admission control, and bounded shutdown."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from core.export_encoding import choose_video_args, encode_command, software_args
from core.export_lifecycle import ExportJob, ExportResult, ExportState
from core.field_diagnostics import DiagnosticError, emit_event
from core.transactional_output import commit_staged_output, create_staged_output_path

_condition = threading.Condition()
_active = 0
_limit = 1
_waiting = []
_records = []


def configure(settings):
    global _limit
    try:
        requested = int(settings.get('parallel_export_limit', 2))
    except (TypeError, ValueError):
        requested = 2
    with _condition:
        _limit = (max(2, min(4, requested))
                  if settings.get('parallel_exports', False) else 1)
        _condition.notify_all()


@contextmanager
def export_slot(cancel):
    global _active
    ticket = object()
    admitted = False
    with _condition:
        _waiting.append(ticket)
        while not cancel.is_set():
            if _active < _limit and _waiting[0] is ticket:
                _waiting.remove(ticket)
                _active += 1
                admitted = True
                _condition.notify_all()
                break
            _condition.wait(.1)
        if not admitted:
            _waiting.remove(ticket)
            _condition.notify_all()
    try:
        yield admitted
    finally:
        if admitted:
            with _condition:
                _active -= 1
                _condition.notify_all()


def run_export(template, staged, output, *, quality, duration, cancel,
               validate, bitrate=None, progress=None, status=None, state_callback=None):
    """Retry a failed hardware encode using software, never after cancellation."""
    if status:
        status('Queued')
    with export_slot(cancel) as admitted:
        if not admitted:
            return ExportResult(ExportState.CANCELLED, 'Export cancelled')
        if status:
            status('Preparing')
        ffmpeg = template[0]
        needs_encode = 'fthr_export_encoder' in template
        args = choose_video_args(ffmpeg, quality, bitrate, cancel=cancel) if needs_encode else []
        if cancel.is_set():
            return ExportResult(ExportState.CANCELLED, 'Export cancelled')
        hardware = any(name in args for name in ('h264_nvenc', 'h264_amf', 'h264_qsv'))
        for attempt in range(2 if hardware else 1):
            if attempt:
                args = software_args(quality, ffmpeg, bitrate)
            if status:
                encoder_label = {'h264_nvenc': 'NVIDIA hardware', 'h264_amf': 'AMD hardware',
                                 'h264_qsv': 'Intel hardware', 'ffv1': 'lossless'}.get(
                                     args[1] if args else '', 'software' if args else 'stream copy')
                status(('Exporting · ' if not attempt else 'Software fallback · ') +
                       encoder_label)
            kwargs = ({'creationflags': subprocess.CREATE_NO_WINDOW |
                       subprocess.BELOW_NORMAL_PRIORITY_CLASS} if sys.platform == 'win32' else {})
            job = ExportJob(
                encode_command(template, args, quality), staged, output,
                cancel_event=cancel, validate_output=validate,
                commit_output=commit_staged_output, popen_kwargs=kwargs,
                progress_callback=progress, inactivity_timeout=120,
                state_callback=state_callback,
            )
            result = job.run(duration=duration)
            if result.state != ExportState.FAILED or cancel.is_set():
                break
        return result


@dataclass
class ExportTask:
    source: str
    output: str
    quality: str
    status: str = 'Queued'
    progress: float = 0
    detail: str = ''
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    done: bool = False


def tasks():
    with _condition:
        return tuple(_records)


def submit(source, directory, quality, build_command, duration, validate,
           *, kind='editor', segment_count=None,
           kept_duration_seconds=None):
    """Build the complete edit snapshot on the GUI thread before dispatch."""
    source_path = str(source)
    event_kind = str(kind)
    segment_total = (None if segment_count is None else int(segment_count))
    kept_duration = (None if kept_duration_seconds is None
                     else float(kept_duration_seconds))
    suffix = '.mkv' if quality == 'lossless' else '.mp4'
    output = Path(directory) / f'{Path(source).stem}_export_{uuid.uuid4().hex[:12]}{suffix}'
    staged = create_staged_output_path(output)
    template = tuple(build_command(str(staged)))
    output_path = str(output)
    task = ExportTask(source_path, output_path, quality)

    emit_event(
        'export', 'requested', state='REQUESTED', kind=event_kind,
        source=source_path, output=output_path, quality=str(quality),
        segment_count=segment_total)
    emit_event(
        'export', 'preparing', state='PREPARING', kind=event_kind,
        source=source_path, output=output_path, quality=str(quality),
        segment_count=segment_total,
        kept_duration_seconds=kept_duration)

    state_events = {
        ExportState.EXPORTING: ('process_started', 'PROCESS_STARTED'),
        ExportState.FINALIZING: ('finalizing', 'FINALIZING'),
        ExportState.COMPLETED: ('completed', 'COMPLETED'),
        ExportState.FAILED: ('process_failed', 'FAILED'),
        ExportState.CANCELLED: ('cancelled', 'CANCELLED'),
        ExportState.TIMED_OUT: ('stalled', 'TIMED_OUT'),
    }

    def _state_changed(state: ExportState) -> None:
        event = state_events.get(state)
        if event is None:
            return
        emit_event(
            'export', event[0], state=event[1], kind=event_kind,
            source=source_path, output=output_path,
            error=(DiagnosticError.EXPORT_STALLED
                   if state is ExportState.TIMED_OUT else
                   DiagnosticError.EXPORT_PROCESS_FAILED
                   if state is ExportState.FAILED else None))

    def work():
        started = time.monotonic()
        result = None
        failure = None
        try:
            result = run_export(
                template, staged, output, quality=quality, duration=duration,
                cancel=task.cancel, validate=validate,
                progress=lambda value: setattr(task, 'progress', value),
                status=lambda value: setattr(task, 'status', value),
                state_callback=_state_changed)
            task.status = result.state.value.title()
            task.detail = '' if result.succeeded else (result.stderr_tail or result.detail)
            if result.state == ExportState.COMPLETED:
                task.progress = 100
        except Exception as error:
            failure = error
            task.status, task.detail = 'Failed', str(error)
        finally:
            result_state = result.state.value if result is not None else 'FAILED'
            result_elapsed = (result.elapsed_seconds if result is not None
                              else time.monotonic() - started)
            result_returncode = result.returncode if result is not None else None
            result_stderr = result.stderr_tail if result is not None else ''
            try:
                try:
                    output_exists = output.is_file()
                    output_size = output.stat().st_size if output_exists else 0
                except OSError:
                    # A final output can disappear between existence and size
                    # checks.  Diagnostics must never strand the queue task.
                    output_exists = False
                    output_size = 0
                emit_event(
                    'export', 'terminal', state=result_state, kind=event_kind,
                    source=source_path, output=output_path,
                    elapsed_ms=round(result_elapsed * 1000),
                    exit_code=result_returncode, stderr_tail=result_stderr,
                    output_exists=output_exists,
                    output_size_bytes=output_size,
                    error=(DiagnosticError.EXPORT_PROCESS_FAILED
                           if failure is not None else None),
                    detail=str(failure) if failure is not None else None)
            finally:
                from core.transactional_output import discard_staged_output
                discard_staged_output(staged)
                task.done = True

    task.thread = threading.Thread(target=work, name='fthr-export', daemon=True)
    with _condition:
        # Retain recent results without keeping an unbounded session history.
        _records[:] = [item for item in _records[:-50] if not item.done] + _records[-50:]
        _records.append(task)
    task.thread.start()
    return task


def shutdown(timeout=12):
    pending = tasks()
    for task in pending:
        if not task.done:
            task.cancel.set()
    deadline = time.monotonic() + timeout
    for task in pending:
        if task.thread:
            task.thread.join(max(0, deadline - time.monotonic()))


