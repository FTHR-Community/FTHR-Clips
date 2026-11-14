"""GUI-owned coordination for uploading edited editor clips.

The editor owns mutable Qt state, while the export queue owns the worker.  This
module is the small boundary between them: callers provide an immutable command
builder and validation callback, and the coordinator only observes queue task
state from a GUI timer.  It never calls back into a widget from an export
thread.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import threading
from typing import Callable, Iterable

from PySide6.QtCore import QObject, QTimer, Signal

from core.media_identity import file_identity


@dataclass(frozen=True)
class EditorUploadRequest:
    """A complete, GUI-thread snapshot for one editor upload export."""

    source: str
    directory: str | Path
    quality: str
    duration: float
    build_command: Callable[[str], Iterable[str]]
    validate: Callable[[Path, threading.Event], None]
    edited: bool = True
    audio_ready: bool = True
    segment_count: int | None = None
    kept_duration_seconds: float | None = None


@dataclass(frozen=True)
class _SourceIdentity:
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    inode: int


@dataclass
class _Pending:
    request: EditorUploadRequest
    task: object
    identity: _SourceIdentity
    key: tuple


class EditorUploadCoordinator(QObject):
    """Submit immutable editor exports and publish only committed outputs.

    ``upload_requested`` is emitted with the committed export path.  A neutral
    request may use its source path directly, but only after the caller has
    established that all editor and audio state is known to be neutral.
    """

    upload_requested = Signal(str)
    export_error = Signal(str, str, str)
    status_changed = Signal(str, str)

    def __init__(self, parent: QObject | None = None, *, poll_ms: int = 40,
                 cache_size: int = 50):
        super().__init__(parent)
        self._pending: dict[tuple, _Pending] = {}
        self._finished: OrderedDict[
            tuple, tuple[str, _SourceIdentity, _SourceIdentity]] = OrderedDict()
        self._cache_size = max(1, int(cache_size))
        self._timer = QTimer(self)
        self._timer.setInterval(max(10, int(poll_ms)))
        self._timer.timeout.connect(self._poll)

    @staticmethod
    def _identity(path: str | Path) -> _SourceIdentity:
        resolved, size, mtime_ns, ctime_ns, inode = file_identity(path)
        return _SourceIdentity(
            str(resolved), int(size), int(mtime_ns), int(ctime_ns), int(inode))

    @staticmethod
    def _command_fingerprint(request: EditorUploadRequest) -> str:
        """Hash the full command with a stable output placeholder."""
        dummy = '<FTHR-EDITOR-UPLOAD-OUTPUT>'
        command = tuple(str(item) for item in request.build_command(dummy))
        normalized = tuple(
            '<OUTPUT>' if item == dummy else item.replace(dummy, '<OUTPUT>')
            for item in command)
        digest = hashlib.sha256()
        digest.update(str(request.quality).encode('utf-8'))
        digest.update(b'\0')
        digest.update('\0'.join(normalized).encode('utf-8'))
        return digest.hexdigest()

    @staticmethod
    def _key(request: EditorUploadRequest, identity: _SourceIdentity,
             fingerprint: str) -> tuple:
        return (identity, str(request.quality), fingerprint)

    def _drop_finished(self, key: tuple) -> None:
        self._finished.pop(key, None)

    def _remember_finished(self, key: tuple, output: str,
                           identity: _SourceIdentity,
                           output_identity: _SourceIdentity) -> None:
        self._finished[key] = (output, identity, output_identity)
        self._finished.move_to_end(key)
        while len(self._finished) > self._cache_size:
            self._finished.popitem(last=False)

    def _error(self, source: str, title: str, detail: str,
               level: str = 'warning') -> None:
        self.status_changed.emit(str(source), 'error')
        self.export_error.emit(str(title), str(detail), str(level))

    def submit(self, request: EditorUploadRequest) -> bool:
        """Queue one request, or use a valid cached result.

        ``False`` means no queue task was started.  It is returned for a
        duplicate, a cache hit, and a rejected request; the corresponding
        signal communicates whether the caller should display queued/error
        state.  A successful export emits ``upload_requested`` exactly once.
        """
        if not isinstance(request, EditorUploadRequest):
            raise TypeError('submit expects an EditorUploadRequest')
        source = str(request.source)
        if not request.audio_ready:
            self._error(source, 'UPLOAD NOT READY',
                        'Audio inspection is still in progress; try again shortly.')
            return False
        try:
            identity = self._identity(source)
        except (OSError, TypeError, ValueError) as error:
            self._error(source, 'UPLOAD EXPORT UNAVAILABLE', str(error))
            return False

        # A source has one running export at a time, even if the user changed
        # an edit while the earlier snapshot is still being encoded.  A task
        # may already be done before the first timer tick; let that tick's
        # processing publish it instead of starting a second job.
        source_key = os.path.normcase(os.path.realpath(source))
        for pending in tuple(self._pending.values()):
            pending_source = os.path.normcase(os.path.realpath(pending.request.source))
            if pending_source != source_key:
                continue
            if bool(getattr(pending.task, 'done', False)):
                self._poll()
            else:
                self.status_changed.emit(source, 'exporting')
            return False

        if not request.edited:
            self.status_changed.emit(source, 'queued')
            self.upload_requested.emit(source)
            return False

        try:
            fingerprint = self._command_fingerprint(request)
        except (OSError, TypeError, ValueError) as error:
            self._error(source, 'UPLOAD EXPORT UNAVAILABLE', str(error))
            return False

        key = self._key(request, identity, fingerprint)
        cached = self._finished.get(key)
        if cached is not None:
            output, cached_identity, cached_output_identity = cached
            try:
                valid = (cached_identity == self._identity(source)
                         and cached_output_identity == self._identity(output))
            except OSError:
                valid = False
            if valid:
                self._finished.move_to_end(key)
                self.status_changed.emit(source, 'queued')
                self.upload_requested.emit(output)
                return False
            self._drop_finished(key)

        try:
            from core import export_queue
            Path(request.directory).mkdir(parents=True, exist_ok=True)
            task = export_queue.submit(
                source, request.directory, str(request.quality),
                request.build_command, float(request.duration), request.validate,
                kind='editor-upload', segment_count=request.segment_count,
                kept_duration_seconds=request.kept_duration_seconds)
        except Exception as error:
            self._error(source, 'UPLOAD EXPORT FAILED', str(error))
            return False

        self._pending[key] = _Pending(request, task, identity, key)
        self.status_changed.emit(source, 'exporting')
        self._timer.start()
        return True

    def cancel(self, source: str | None = None) -> None:
        """Cancel pending exports for a source, without touching cached files."""
        for pending in tuple(self._pending.values()):
            if source is None or os.path.normcase(pending.request.source) == os.path.normcase(str(source)):
                pending.task.cancel.set()

    def _poll(self) -> None:
        for key, pending in tuple(self._pending.items()):
            task = pending.task
            if not bool(getattr(task, 'done', False)):
                continue
            self._pending.pop(key, None)
            source = pending.request.source
            output = str(getattr(task, 'output', ''))
            completed = str(getattr(task, 'status', '')) == 'Completed'
            try:
                unchanged = self._identity(source) == pending.identity
                output_valid = bool(output) and Path(output).is_file()
            except OSError:
                unchanged = False
                output_valid = False
            if completed and unchanged and output_valid:
                try:
                    output_identity = self._identity(output)
                except OSError:
                    output_identity = None
                if output_identity is None:
                    self._error(source, 'UPLOAD EXPORT FAILED',
                                'The committed upload export is missing or unreadable.')
                    continue
                self._remember_finished(key, output, pending.identity, output_identity)
                self.status_changed.emit(source, 'queued')
                self.upload_requested.emit(output)
                continue
            detail = str(getattr(task, 'detail', '') or 'Export did not complete.')
            if not unchanged:
                detail = 'The source clip changed while its upload export was running.'
            elif completed and not output_valid:
                detail = 'The committed upload export is missing or unreadable.'
            self._error(source, 'UPLOAD EXPORT FAILED', detail)
        if not self._pending:
            self._timer.stop()
