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

from core.export_encoding import (
    choose_video_args, encode_command, software_args, choose_hdr_video_args, hdr_safe_template,
)
from core.hdr_color import sdr_command, probe_hdr
from core.export_lifecycle import ExportJob, ExportResult, ExportState
from core.field_diagnostics import DiagnosticError, emit_event
from core.library_index import canonical_media_path
from core.transactional_output import commit_staged_output, create_staged_output_path

_condition = threading.Condition()
_active = 0
_limit = 1
_waiting = []
_records = []
_path_claims: dict[str, int] = {}
_local_mutations: set[str] = set()
_mutation_tokens: dict[LocalMutationReservation, frozenset[str]] = {}
_initializing = False


def set_initializing(initializing: bool) -> None:
    global _initializing
    with _condition:
        _initializing = bool(initializing)
        _condition.notify_all()


@dataclass(frozen=True, eq=False)
class LocalMutationReservation:
    """Opaque ownership of exactly the keys acquired by this queue."""

    keys: frozenset[str]


def acquire_local_mutations(paths) -> LocalMutationReservation | None:
    keys = frozenset(canonical_media_path(path) for path in paths)
    with _condition:
        if _initializing or keys.intersection(_local_mutations) or any(_path_claims.get(key, 0) for key in keys):
            return None
        reservation = LocalMutationReservation(keys)
        _mutation_tokens[reservation] = keys
        _local_mutations.update(keys)
        return reservation


def release_local_mutations(reservation) -> None:
    if not isinstance(reservation, LocalMutationReservation):
        return
    with _condition:
        keys = _mutation_tokens.pop(reservation, None)
        if keys is not None:
            _local_mutations.difference_update(keys)
            _condition.notify_all()


def begin_local_mutations(paths) -> bool:
    keys = {canonical_media_path(path) for path in paths}
    with _condition:
        if _initializing or keys.intersection(_local_mutations) or any(_path_claims.get(key, 0) for key in keys):
            return False
        _local_mutations.update(keys)
        return True


def end_local_mutations(paths) -> None:
    with _condition:
        owned = set().union(*_mutation_tokens.values()) if _mutation_tokens else set()
        _local_mutations.difference_update(
            {canonical_media_path(path) for path in paths}.difference(owned))
        _condition.notify_all()


def _claim_paths(paths) -> set[str]:
    keys = {canonical_media_path(path) for path in paths}
    with _condition:
        if _initializing or keys.intersection(_local_mutations):
            raise RuntimeError('Library recovery or a local file change blocks this export')
        for key in keys:
            _path_claims[key] = _path_claims.get(key, 0) + 1
    return keys


def _release_paths(keys) -> None:
    with _condition:
        for key in keys:
            remaining = _path_claims.get(key, 0) - 1
            if remaining > 0:
                _path_claims[key] = remaining
            else:
                _path_claims.pop(key, None)
        _condition.notify_all()


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
            if not _initializing and _active < _limit and _waiting[0] is ticket:
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
        hdr_args = None
        if (needs_encode and quality != 'lossless' and bitrate is None
                and '-i' in template and hdr_safe_template(template)):
            color = probe_hdr(template[template.index('-i') + 1], cancel)
            if color is not None:
                hdr_args = choose_hdr_video_args(ffmpeg, color, quality, cancel)
        args = (hdr_args or choose_video_args(ffmpeg, quality, bitrate, cancel=cancel)) if needs_encode else []
        if cancel.is_set():
            return ExportResult(ExportState.CANCELLED, 'Export cancelled')
        hardware = any(name in args for name in (
            'h264_nvenc', 'h264_amf', 'h264_qsv', 'hevc_nvenc', 'hevc_amf', 'hevc_qsv'))
        for attempt in range(2 if hardware else 1):
            if attempt:
                args = software_args(quality, ffmpeg, bitrate)
            if status:
                encoder_label = {'h264_nvenc': 'NVIDIA hardware', 'h264_amf': 'AMD hardware',
                                 'h264_qsv': 'Intel hardware', 'hevc_nvenc': 'NVIDIA HDR',
                                 'hevc_qsv': 'Intel HDR', 'hevc_amf': 'AMD HDR', 'ffv1': 'lossless'}.get(
                                     args[1] if args else '', 'software' if args else 'stream copy')
                status(('Exporting · ' if not attempt else 'Software fallback · ') +
                       encoder_label)
            kwargs = ({'creationflags': subprocess.CREATE_NO_WINDOW |
                       subprocess.BELOW_NORMAL_PRIORITY_CLASS} if sys.platform == 'win32' else {})
            job = ExportJob(
                sdr_command(encode_command(template, args, quality), cancelled=cancel), staged, output,
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
    label: str | None = None
    result: object | None = None


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
    claimed = _claim_paths((source_path, output))
    prepared = False
    try:
        staged = create_staged_output_path(output)
        template = tuple(build_command(str(staged)))
        output_path = str(output)
        task = ExportTask(source_path, output_path, quality)
        prepared = True
    finally:
        # Also release on interpreter/user interruption without catching it.
        if not prepared:
            _release_paths(claimed)

    diagnosed = False
    try:
        emit_event(
            'export', 'requested', state='REQUESTED', kind=event_kind,
            source=source_path, output=output_path, quality=str(quality),
            segment_count=segment_total)
        emit_event(
            'export', 'preparing', state='PREPARING', kind=event_kind,
            source=source_path, output=output_path, quality=str(quality),
            segment_count=segment_total,
            kept_duration_seconds=kept_duration)
        diagnosed = True
    finally:
        if not diagnosed:
            _release_paths(claimed)

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
            task.detail = result.failure_message(full_stderr=True)
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
            result_detail = result.detail if result is not None else task.detail
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
                    detail=result_detail or None)
            finally:
                from core.transactional_output import discard_staged_output
                try:
                    discard_staged_output(staged)
                finally:
                    task.done = True
                    _release_paths(claimed)

    dispatched = False
    try:
        task.thread = threading.Thread(target=work, name='fthr-export', daemon=True)
        with _condition:
            # Retain recent results without keeping an unbounded session history.
            _records[:] = [item for item in _records[:-50] if not item.done] + _records[-50:]
            _records.append(task)
        task.thread.start()
        dispatched = True
    finally:
        # A live/finished worker owns release even if start was interrupted
        # after launching it. Only an unstarted thread can be reclaimed here.
        if not dispatched and getattr(task.thread, 'ident', None) is None:
            task.thread = None
            task.done = True
            _release_paths(claimed)
    return task


def submit_originals(records, directory, *, metadata_manager=None, readiness=None):
    """One owned queue task for a frozen, byte-exact original bundle."""
    from core.audio_manifest import manifest_path_for
    from core.original_export import export_originals, prepare_originals, OriginalExportResult

    records = tuple(records)
    if not records:
        raise ValueError('Select at least one original to export')
    readiness = dict(readiness) if readiness is not None else None
    directory = str(Path(directory).absolute())
    task = ExportTask(str(records[0].path), directory, 'originals', label=f'{len(records)} originals')
    snapshot = getattr(metadata_manager, 'export_members_snapshot', None)
    if metadata_manager is not None and not callable(snapshot):
        raise OSError('Library metadata cannot validate original export members')

    def work():
        claims = []
        failures = []
        prepared = []
        try:
            for record in records:
                if task.cancel.is_set():
                    break
                try:
                    # Both potential members remain claimed until terminal,
                    # including a sidecar that is absent at the freeze point.
                    keys = _claim_paths((record.path, manifest_path_for(record.path)))
                    claims.append(keys)
                    if readiness is not None and readiness.get(record.path) is not True:
                        raise OSError('Clip is not ready for original export')
                    items, errors = prepare_originals((record,), member_snapshot=snapshot)
                    prepared.extend(items)
                    failures.extend(errors)
                except (OSError, ValueError, RuntimeError, TypeError) as error:
                    failures.append((record.path, str(error)))
            with export_slot(task.cancel) as admitted:
                if not admitted:
                    task.result = OriginalExportResult(failures=tuple(failures), cancelled=True)
                else:
                    task.status = 'Copying originals'
                    task.result = export_originals(
                        records, directory, cancel=task.cancel, member_snapshot=snapshot,
                        prepared=tuple(prepared), initial_failures=tuple(failures),
                        progress=lambda value: setattr(task, 'progress', value))
            result = task.result
            if result.output:
                task.output = result.output
                task.progress = 100
                task.status = 'Completed with errors' if result.failures else 'Completed'
            else:
                task.status = 'Cancelled' if result.cancelled else 'Failed'
            task.detail = (f'{len(result.successes)} exported, {len(result.failures)} failed'
                           + ('; cancelled' if result.cancelled else ''))
            if result.failures:
                task.detail += '\n' + '\n'.join(f'{Path(path).name}: {error}'
                                                   for path, error in result.failures[:10])
                if len(result.failures) > 10:
                    task.detail += f'\n… and {len(result.failures) - 10} more failed originals'
        except Exception as error:
            # Copy/publication failures leave sources untouched and must always
            # release the task's reader claims so later mutations can proceed.
            task.status, task.detail = 'Failed', str(error)
        finally:
            task.done = True
            for keys in claims:
                _release_paths(keys)

    dispatched = False
    try:
        task.thread = threading.Thread(target=work, name='fthr-original-export', daemon=True)
        with _condition:
            _records[:] = [item for item in _records[:-50] if not item.done] + _records[-50:]
            _records.append(task)
        task.thread.start()
        dispatched = True
    except Exception as error:
        # A thread that did launch owns terminal state and reader-claim release.
        if getattr(task.thread, 'ident', None) is None:
            task.status, task.detail = 'Failed', str(error)
        raise
    finally:
        if not dispatched and getattr(task.thread, 'ident', None) is None:
            task.thread = None
            task.done = True
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


def submit_work(sources, output, quality, work, *, label=None):
    """Own a multi-input media job with the same claims, slots and shutdown.

    ``work(task)`` runs off the GUI thread and returns an ExportResult. Unlike
    the single-source editor helper it owns its encoding/validation contract.
    """
    sources = tuple(str(source) for source in sources)
    if not sources:
        raise ValueError('A media job needs at least one source')
    claimed = _claim_paths((*sources, output))
    task = ExportTask(sources[0], str(output), quality, label=label)

    def run():
        try:
            with export_slot(task.cancel) as admitted:
                if admitted:
                    task.status = 'Preparing'
                    task.result = work(task)
                else:
                    task.result = ExportResult(ExportState.CANCELLED, 'Job cancelled')
            task.status = task.result.state.value.title()
            task.detail = task.result.failure_message(full_stderr=True)
            if task.result.succeeded:
                task.progress = 100
        except Exception as error:
            task.status, task.detail = 'Failed', str(error)
            task.result = ExportResult(ExportState.FAILED, str(error))
        finally:
            _release_paths(claimed)
            task.done = True

    try:
        task.thread = threading.Thread(target=run, name='fthr-media-job', daemon=True)
        with _condition:
            _records[:] = [item for item in _records[:-50] if not item.done] + _records[-50:]
            _records.append(task)
        task.thread.start()
    finally:
        if getattr(task.thread, 'ident', None) is None:
            task.thread = None
            _release_paths(claimed)
            task.done = True
    return task
