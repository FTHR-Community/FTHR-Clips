from __future__ import annotations

import threading
import time
import re
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from typing import Optional

try:
    import cv2 as _cv2
    import numpy as _np  # noqa: F401  — availability probe, not used directly
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

KEEP_SECONDS  = 90
DEFAULT_FPS   = 30
JPEG_QUALITY  = 85  # ~150-300 KB/frame at 1080p vs ~6 MB raw
_LOG_MESSAGE_LIMIT = 512


@dataclass(frozen=True)
class CameraRuntimeSnapshot:
    """Detached camera state used to bind a capture job to one provider run."""

    device_index: int
    generation: int
    running: bool
    ready: bool


def _log_exception(context: str, exc: Exception) -> None:
    try:
        detail = str(exc)
    except Exception:
        detail = '<exception text unavailable>'
    print(f'[Camera] {context}: {type(exc).__name__}: '
          f'{detail[:_LOG_MESSAGE_LIMIT]}')


class CameraRecorder:
    """Continuous webcam capture singleton. Always-on when a device is selected."""

    _instance: Optional['CameraRecorder'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self):
        self._cap = None
        self._device_index = 0
        self._lock = threading.Lock()
        # Serializes start/stop — rapid enable/disable toggles from the UI
        # would otherwise interleave open/release on the same VideoCapture.
        self._op_lock = threading.Lock()
        self._chunks: deque = deque()  # (monotonic_ts, JPEG bytes)
        self._running = False
        self._thread: threading.Thread | None = None
        self._latest_frame = None
        self._generation = 0
        self._stop_event = None
        self._run_context = None
        self._retired_thread = None
        self._retired_context = None
        self._opening_context = None
        self._cleanup_threads = {}
        self._pending_cleanup = {}

    @classmethod
    def is_available(cls) -> bool:
        return _AVAILABLE

    @classmethod
    def list_devices(cls) -> list[dict]:
        """Return camera indices with real Windows device names when available."""
        if not _AVAILABLE:
            return []
        if sys.platform == 'win32':
            try:
                from core.ffmpeg_tools import get_ffmpeg_exe
                ffmpeg = get_ffmpeg_exe()
                result = subprocess.run(
                    [ffmpeg, '-hide_banner', '-list_devices', 'true',
                     '-f', 'dshow', '-i', 'dummy'],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    timeout=12,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                output = (result.stderr or '') + '\n' + (result.stdout or '')
                names = re.findall(r'"([^"]+)"\s+\(video\)', output)
                if names:
                    return [
                        {'index': index, 'name': name}
                        for index, name in enumerate(dict.fromkeys(names))
                    ]
            except Exception as exc:
                print(f'[Camera] Named device scan failed: {exc}')

        # Cross-platform fallback: do not invent four entries. Only expose
        # indices that OpenCV can actually open.
        devices = []
        for index in range(8):
            try:
                cap = _cv2.VideoCapture(index)
                opened = cap.isOpened()
                cap.release()
                if opened:
                    devices.append({'index': index, 'name': f'Camera {index + 1}'})
            except Exception:
                pass
        return devices

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def runtime_snapshot(self) -> CameraRuntimeSnapshot:
        """Return a consistent, immutable provider receipt.

        The lock covers both the run identity and readiness marker.  Callers
        receive no references to the backend, frame deque, or worker thread.
        """
        with self._lock:
            return CameraRuntimeSnapshot(
                device_index=int(self._device_index),
                generation=int(self._generation),
                running=bool(self._running),
                ready=bool(self._running and self._latest_frame is not None),
            )

    def owned_cleanup_complete(self, *, owner_token) -> bool:
        """Report whether this exact owner has no unproved camera cleanup.

        The query is deliberately a readonly observation.  It never waits for
        a worker, probes a backend, or changes provider state; callers retain
        their own admission latch while this observation is used.
        """
        if owner_token is None:
            return False
        with self._lock:
            contexts = [
                self._opening_context,
                self._run_context,
                self._retired_context,
            ]
            contexts.extend(self._pending_cleanup.values())
            for context in contexts:
                if context is None or not self._owned_context_matches(
                        context, owner_token):
                    continue
                cleanup_done = context.get('cleanup_done')
                if (cleanup_done is not None
                        and cleanup_done.is_set()
                        and context.get('cleanup_ok') is True):
                    continue
                return False
        return True

    @staticmethod
    def _token_matches(actual, expected) -> bool:
        return actual is expected

    @classmethod
    def _owned_context_matches(cls, context, owner_token) -> bool:
        return cls._token_matches(context.get('owner_token'), owner_token)

    @staticmethod
    def _integer_matches(actual, expected) -> bool:
        return (type(expected) is int and expected >= 0
                and type(actual) is int and actual == expected)

    @staticmethod
    def _callback_once(callback, label='Camera-result callback'):
        called = False
        callback_lock = threading.Lock()

        def report(value):
            nonlocal called
            with callback_lock:
                if called:
                    return
                called = True
            if callback:
                try:
                    callback(bool(value))
                except Exception as exc:
                    _log_exception(f'{label} failed', exc)

        return report

    def start(self, device_index: int = 0, *, owner_token=None,
              request_token=None) -> bool:
        """Blocking open — cv2.VideoCapture can take seconds on Windows/MSMF.
        Never call this from the UI thread; use start_async there."""
        if not _AVAILABLE:
            return False
        with self._op_lock:
            context = self._new_opening_context(device_index, owner_token,
                                                request_token)
            if context is None:
                return False
            return self._start_with_context(context, op_lock_held=True)

    def _new_opening_context(self, device_index, owner_token, request_token):
        event = threading.Event()
        context = {
            'cap': None,
            'event': event,
            'cancel_event': event,
            'release_lock': threading.Lock(),
            'released': False,
            'release_ok': None,
            'release_error': None,
            'owner_token': owner_token,
            'request_token': request_token,
            'device_index': int(device_index),
            'generation': None,
            'phase': 'opening',
            'cancelled': False,
            'start_done': threading.Event(),
            'worker_started': threading.Event(),
            'cleanup_done': threading.Event(),
            'cleanup_ok': None,
            'predecessors': [],
            'blocks_successors': False,
            'thread': None,
            'cleanup_started': False,
            'failed_acknowledged': False,
            'natural_exit': False,
            'strict_worker_liveness': False,
            'start_failure_cleanup': False,
        }
        superseded = None
        with self._lock:
            if any(context.get('blocks_successors')
                   or context.get('cleanup_ok') is False
                   for context in self._pending_cleanup.values()):
                return None
            existing = self._opening_context
            if existing is not None:
                if (owner_token is None
                        or not self._owned_context_matches(existing,
                                                           owner_token)):
                    return None
                existing['cancelled'] = True
                existing['superseded'] = True
                existing['supersede_dependency'] = True
                existing['cancel_event'].set()
                self._opening_context = None
                superseded = existing
                context['predecessors'] = list(
                    existing.get('predecessors', ())) + [existing]
                # Retain the superseded context before publishing its
                # successor.  A concurrent stop/start must therefore see the
                # exact predecessor barrier, even while the cleanup worker is
                # still being queued outside this lock.
                self._pending_cleanup[id(existing)] = existing
            self._opening_context = context
        if superseded is not None:
            self._queue_cleanup(
                superseded, None, 'opening', self._callback_once(None))
        return context

    def _clear_opening_context(self, context):
        with self._lock:
            if self._opening_context is context:
                self._opening_context = None

    def _predecessor_status_locked(self, context):
        for predecessor in context.get('predecessors', ()):
            if not predecessor['cleanup_done'].is_set():
                return 'waiting'
            if predecessor.get('cleanup_ok') is not True:
                return 'failed'
        return 'ready'

    @staticmethod
    def _context_depends_on_locked(context, target):
        pending = list(context.get('predecessors', ()))
        seen = set()
        while pending:
            predecessor = pending.pop()
            if predecessor is target:
                return True
            marker = id(predecessor)
            if marker in seen:
                continue
            seen.add(marker)
            pending.extend(predecessor.get('predecessors', ()))
        return False

    def _link_successor_context_locked(self, predecessor, successor):
        if (successor is None or predecessor is None
                or successor is predecessor):
            return successor is None
        owner = successor.get('owner_token')
        if (owner is None
                or not self._owned_context_matches(predecessor, owner)
                or self._context_depends_on_locked(predecessor, successor)):
            return False
        if not any(existing is predecessor
                   for existing in successor.get('predecessors', ())):
            successor.setdefault('predecessors', []).append(predecessor)
        return True

    def _pending_cleanup_is_clear(self, context):
        predecessors = tuple(context.get('predecessors', ()))
        return not any(
            not (
                any(
                    pending is predecessor
                    and predecessor['cleanup_done'].is_set()
                    and predecessor.get('cleanup_ok') is True
                    for predecessor in predecessors
                )
            )
            and (
                pending.get('blocks_successors')
                or pending.get('cleanup_ok') is False
                or not pending['cleanup_done'].is_set()
            )
            for pending in self._pending_cleanup.values())

    def _natural_retired_can_be_reaped_locked(self, context):
        retired = self._retired_context
        if (retired is None
                or not retired.get('natural_exit')
                or retired['cleanup_done'].is_set()
                or not self._owned_context_matches(
                    retired, context.get('owner_token'))):
            return False
        return all(
            pending is retired
            or (pending['cleanup_done'].is_set()
                and pending.get('cleanup_ok') is True)
            for pending in self._pending_cleanup.values())

    def _retain_release_failure(self, context):
        with self._lock:
            context['blocks_successors'] = True
            context['cleanup_ok'] = False
            context['phase'] = 'cleanup_failed'
            if 'failed_acknowledged' not in context:
                context['failed_acknowledged'] = False
            self._pending_cleanup[id(context)] = context
            if self._retired_context is None:
                self._retired_context = context
                self._retired_thread = None
            context['cleanup_done'].set()

    def _retain_release_failure_if_needed(self, context):
        release_ok = self._release_capture(context)
        if not release_ok:
            self._retain_release_failure(context)
        return release_ok

    @staticmethod
    def _reset_opening_capture_after_release(context):
        with context['release_lock']:
            context['cap'] = None
            context['released'] = False
            context['release_ok'] = None
            context['release_error'] = None

    def _wait_for_predecessors(self, context, *, wait_all=False):
        while True:
            with self._lock:
                predecessors = tuple(context.get('predecessors', ()))
            all_ok = True
            for predecessor in predecessors:
                predecessor['cleanup_done'].wait()
                with self._lock:
                    predecessor_ok = predecessor.get('cleanup_ok') is True
                    if not predecessor_ok:
                        all_ok = False
                    if not predecessor_ok and not wait_all:
                        return False
            with self._lock:
                current = tuple(context.get('predecessors', ()))
            if (len(current) == len(predecessors)
                    and all(left is right
                            for left, right in zip(
                                current, predecessors, strict=True))):
                return all_ok

    def _start_with_context(self, context, *, op_lock_held=False):
        """Run one registered open, keeping cancellation outside _op_lock."""
        try:
            while True:
                if op_lock_held:
                    operation_acquired = False
                else:
                    if not self._wait_for_predecessors(context):
                        return False
                    self._op_lock.acquire()
                    operation_acquired = True
                retry_after_predecessor = False
                try:
                    with self._lock:
                        if (self._opening_context is not context
                                or context['cancel_event'].is_set()
                                or context['cancelled']):
                            return False
                        predecessor_status = self._predecessor_status_locked(
                            context)
                        if predecessor_status == 'failed':
                            return False
                        if predecessor_status == 'waiting':
                            if op_lock_held:
                                return False
                            retry_after_predecessor = True
                        elif (not self._pending_cleanup_is_clear(context)
                              and not self._natural_retired_can_be_reaped_locked(
                                  context)):
                            return False
                        if retry_after_predecessor:
                            continue
                        current_context = self._run_context
                        current_cap = self._cap
                        same_device = (
                            self._running
                            and self._device_index == context['device_index']
                            and current_cap is not None)
                        owner_compatible = (
                            context['owner_token'] is None
                            or (current_context is not None
                                and self._owned_context_matches(
                                    current_context,
                                    context['owner_token'])))
                    if same_device:
                        try:
                            healthy = bool(current_cap.isOpened())
                        except Exception as exc:
                            healthy = False
                            _log_exception(
                                'Active device health check failed', exc)
                        with self._lock:
                            predecessor_status = (
                                self._predecessor_status_locked(context))
                            gate_clear = (
                                self._pending_cleanup_is_clear(context)
                                or self._natural_retired_can_be_reaped_locked(
                                    context))
                            if predecessor_status == 'failed':
                                return False
                            if predecessor_status == 'waiting':
                                if op_lock_held:
                                    return False
                                retry_after_predecessor = True
                            elif healthy and owner_compatible and (
                                    self._run_context is current_context
                                    and not context['cancel_event'].is_set()
                                    and not context['cancelled']
                                    and gate_clear):
                                if context['owner_token'] is not None:
                                    current_context['request_token'] = context[
                                        'request_token']
                                self._opening_context = None
                                context['phase'] = 'reused'
                                return True
                        if retry_after_predecessor:
                            continue
                        if not owner_compatible:
                            return False
                    with self._lock:
                        foreign_active = (
                            self._run_context is not None
                            and context['owner_token'] is not None
                            and not self._owned_context_matches(
                                self._run_context, context['owner_token']))
                    if foreign_active:
                        return False
                    with self._lock:
                        predecessor_status = (
                            self._predecessor_status_locked(context))
                    if predecessor_status == 'failed':
                        return False
                    if predecessor_status == 'waiting':
                        if op_lock_held:
                            return False
                        continue
                    stop_ok = self._stop_locked(
                        successor_context=(
                            context if context['owner_token'] is not None
                            else None))
                    if not stop_ok:
                        with self._lock:
                            predecessor_status = (
                                self._predecessor_status_locked(context))
                            still_current = (
                                self._opening_context is context
                                and not context['cancel_event'].is_set()
                                and not context['cancelled'])
                        if (predecessor_status == 'waiting'
                                and still_current and not op_lock_held):
                            continue
                        return False
                    with self._lock:
                        predecessor_status = (
                            self._predecessor_status_locked(context))
                        still_admitted = (
                            self._opening_context is context
                            and not context['cancel_event'].is_set()
                            and not context['cancelled']
                            and predecessor_status == 'ready'
                            and (self._pending_cleanup_is_clear(context)
                                 or self._natural_retired_can_be_reaped_locked(
                                     context))
                            and self._run_context is None)
                    if predecessor_status == 'failed' or not still_admitted:
                        if (predecessor_status == 'waiting'
                                and not op_lock_held):
                            continue
                        return False
                    cap = None
                    try:
                        cap = _cv2.VideoCapture(context['device_index'])
                        context['cap'] = cap
                        if not cap.isOpened():
                            print(
                                f"[Camera] Device {context['device_index']} "
                                "did not open")
                            self._retain_release_failure_if_needed(context)
                            return False
                    except Exception as exc:
                        _log_exception(
                            f"Could not open device {context['device_index']}", exc)
                        if cap is not None:
                            context['cap'] = cap
                            self._retain_release_failure_if_needed(context)
                        return False
                    with self._lock:
                        predecessor_status = (
                            self._predecessor_status_locked(context))
                        still_current = (
                            self._opening_context is context
                            and not context['cancel_event'].is_set()
                            and not context['cancelled']
                            and predecessor_status == 'ready'
                            and self._pending_cleanup_is_clear(context)
                            and self._run_context is None)
                        generation = self._generation + 1
                    if not still_current:
                        release_ok = self._retain_release_failure_if_needed(
                            context)
                        with self._lock:
                            opening_is_current = (
                                self._opening_context is context)
                        if (release_ok and predecessor_status == 'waiting'
                                and not op_lock_held
                                and opening_is_current):
                            self._reset_opening_capture_after_release(context)
                            continue
                        return False
                    try:
                        thread = threading.Thread(
                            target=self._capture_loop,
                            args=(cap, context['event'], generation, context),
                            daemon=True, name='fthr-camera')
                    except Exception as exc:
                        _log_exception('Could not create capture worker', exc)
                        self._retain_release_failure_if_needed(context)
                        return False
                    with self._lock:
                        predecessor_status = (
                            self._predecessor_status_locked(context))
                        still_current = (
                            self._opening_context is context
                            and not context['cancel_event'].is_set()
                            and not context['cancelled']
                            and predecessor_status == 'ready'
                            and self._pending_cleanup_is_clear(context)
                            and self._run_context is None)
                        if still_current:
                            context['generation'] = generation
                            context['phase'] = 'running'
                            context['thread'] = thread
                            self._generation = generation
                            self._device_index = context['device_index']
                            self._cap = cap
                            self._stop_event = context['event']
                            self._run_context = context
                            self._running = True
                            self._latest_frame = None
                            self._chunks.clear()
                            self._thread = thread
                            self._opening_context = None
                    if not still_current:
                        release_ok = self._retain_release_failure_if_needed(
                            context)
                        with self._lock:
                            opening_is_current = (
                                self._opening_context is context)
                        if (release_ok and predecessor_status == 'waiting'
                                and not op_lock_held
                                and opening_is_current):
                            self._reset_opening_capture_after_release(context)
                            continue
                        return False
                    try:
                        thread.start()
                        context['worker_started'].set()
                    except Exception as exc:
                        context['worker_started'].set()
                        _log_exception(
                            'Could not create or start capture worker', exc)
                        cleanup_now = False
                        queue_cleanup = False
                        with self._lock:
                            context['strict_worker_liveness'] = True
                            context['start_failure_cleanup'] = True
                            context['cancelled'] = True
                            context['event'].set()
                            if (self._generation == generation
                                    and self._run_context is context):
                                context['stopping'] = True
                                context['async_cleanup'] = True
                                context['blocks_successors'] = True
                                context['phase'] = 'retired'
                                self._pending_cleanup[id(context)] = context
                                self._retired_thread = thread
                                self._retired_context = context
                                self._running = False
                                self._cap = None
                                self._thread = None
                                self._stop_event = None
                                self._run_context = None
                                self._latest_frame = None
                                self._chunks.clear()
                                self._generation += 1
                                queue_cleanup = True
                            elif (not context['cleanup_done'].is_set()
                                  and id(context) not in
                                  self._pending_cleanup):
                                context['async_cleanup'] = True
                                context['blocks_successors'] = True
                                context['phase'] = 'retired'
                                self._pending_cleanup[id(context)] = context
                                if self._retired_context is None:
                                    self._retired_thread = thread
                                    self._retired_context = context
                                queue_cleanup = True
                            elif (not context['cleanup_done'].is_set()
                                  and not context.get('cleanup_started')):
                                context['async_cleanup'] = True
                                queue_cleanup = True
                            if op_lock_held:
                                cleanup_now = queue_cleanup
                                queue_cleanup = False
                        if cleanup_now:
                            try:
                                if thread is not threading.current_thread():
                                    try:
                                        thread.join(timeout=2.0)
                                    except RuntimeError:
                                        # A just-created thread may reject
                                        # join(); is_alive below remains the
                                        # required proof before release.
                                        pass
                                alive = thread.is_alive()
                            except Exception as liveness_exc:
                                alive = True
                                _log_exception(
                                    'Could not prove failed capture worker ended',
                                    liveness_exc)
                            if not alive:
                                release_ok = self._release_capture(context)
                                with self._lock:
                                    if release_ok:
                                        context['cleanup_ok'] = True
                                        context['cleanup_done'].set()
                                        self._pending_cleanup.pop(
                                            id(context), None)
                                        if self._retired_context is context:
                                            self._retired_thread = None
                                            self._retired_context = None
                                        context['phase'] = 'cleaned'
                                    else:
                                        context['blocks_successors'] = True
                                        context['async_cleanup'] = False
                                        context['cleanup_ok'] = False
                                        context['cleanup_done'].set()
                                        context['phase'] = 'cleanup_failed'
                            else:
                                queue_cleanup = True
                        if queue_cleanup:
                            self._queue_cleanup(
                                context, thread, 'running',
                                self._callback_once(None))
                        return False
                    return True
                finally:
                    if operation_acquired:
                        self._op_lock.release()
                if retry_after_predecessor:
                    continue
        finally:
            with self._lock:
                if self._opening_context is context:
                    self._opening_context = None
                    if context['phase'] == 'opening':
                        context['phase'] = 'failed'
            context['start_done'].set()

    def start_async(self, device_index: int = 0, on_result=None, *,
                    owner_token=None, request_token=None):
        """Open the device off the UI thread. on_result(ok: bool) is invoked
        from the worker thread — marshal back to Qt yourself if needed."""
        report = self._callback_once(on_result, 'Open-result callback')
        if owner_token is None:
            def _legacy_work():
                try:
                    ok = self.start(device_index)
                except Exception as exc:
                    _log_exception('Asynchronous open failed', exc)
                    ok = False
                report(ok)

            try:
                threading.Thread(target=_legacy_work, daemon=True,
                                  name='fthr-camera-open').start()
            except Exception as exc:
                _log_exception('Could not start asynchronous open', exc)
                report(False)
            return
        if not _AVAILABLE:
            report(False)
            return
        context = self._new_opening_context(device_index, owner_token,
                                            request_token)
        if context is None:
            report(False)
            return

        def _work():
            try:
                ok = self._start_with_context(context)
            except Exception as exc:
                _log_exception('Asynchronous open failed', exc)
                ok = False
            report(ok)

        try:
            threading.Thread(target=_work, daemon=True,
                              name='fthr-camera-open').start()
        except Exception as exc:
            _log_exception('Could not start asynchronous open', exc)
            self._clear_opening_context(context)
            context['cancelled'] = True
            context['cancel_event'].set()
            context['start_done'].set()
            report(False)

    def stop(self):
        with self._op_lock:
            with self._lock:
                opening = self._opening_context
                if opening is not None:
                    opening['cancelled'] = True
                    opening['cancel_event'].set()
                    self._opening_context = None
            self._stop_locked()

    def _owned_receipt_matches_locked(self, context, owner_token,
                                      expected_generation,
                                      expected_device_index, request_token,
                                      generation):
        if (context is None
                or not self._owned_context_matches(context, owner_token)):
            return False
        if (expected_generation is not None
                and not self._integer_matches(
                    generation, expected_generation)):
            return False
        if (expected_device_index is not None
                and not self._integer_matches(
                    context.get('device_index'), expected_device_index)):
            return False
        if (request_token is not None
                and not self._token_matches(
                    context.get('request_token'), request_token)):
            return False
        return True

    def request_stop_async(self, *, expected_generation=None,
                           expected_device_index=None, owner_token=None,
                           request_token=None, on_result=None) -> bool:
        """Cancel exactly one owned run or opening without waiting.

        The front half only changes provider state under ``_lock``.  Joining
        a worker or releasing a backend is deliberately deferred to a daemon
        cleanup worker, so a caller may use this method from the UI thread.
        """
        if owner_token is None:
            return False
        target = None
        target_thread = None
        target_kind = None
        failed_acknowledge = False
        with self._lock:
            opening = self._opening_context
            opening_matches = (
                opening is not None
                and self._owned_receipt_matches_locked(
                    opening, owner_token, None, expected_device_index,
                    request_token, None)
                and expected_generation is None)
            if opening_matches:
                opening['cancelled'] = True
                opening['cancel_event'].set()
                self._opening_context = None
                target = opening
                target_kind = 'opening'
                opening['blocks_successors'] = True
            else:
                active = self._run_context
                active_matches = self._owned_receipt_matches_locked(
                    active, owner_token, expected_generation,
                    expected_device_index, request_token, self._generation)
                if active_matches and not active.get('stopping'):
                    opening = self._opening_context
                    if (opening is not None
                            and self._owned_context_matches(
                                opening, owner_token)
                            and not any(existing is active
                                        for existing in opening.get(
                                            'predecessors', ()) )):
                        opening.setdefault('predecessors', []).append(active)
                    target = active
                    target_thread = self._thread
                    target_kind = 'running'
                    active['stopping'] = True
                    active['async_cleanup'] = True
                    active['phase'] = 'retired'
                    active['cancelled'] = True
                    active['event'].set()
                    self._running = False
                    self._thread = None
                    self._cap = None
                    self._stop_event = None
                    self._run_context = None
                    self._generation += 1
                    self._latest_frame = None
                    self._chunks.clear()
                    self._retired_thread = target_thread
                    self._retired_context = target
                    active['blocks_successors'] = True
                else:
                    retired = self._retired_context
                    retired_matches = self._owned_receipt_matches_locked(
                        retired, owner_token, expected_generation,
                        expected_device_index, request_token,
                        retired.get('generation') if retired is not None
                        else None)
                    failed_cleanup = (
                        retired_matches
                        and retired.get('cleanup_ok') is False
                        and retired.get('cleanup_done').is_set()
                        and not retired.get('failed_acknowledged')
                        and not retired.get('async_cleanup'))
                    if failed_cleanup:
                        retired['failed_acknowledged'] = True
                        failed_acknowledge = True
                    elif (not retired_matches
                          or (retired.get('stopping')
                              and retired.get('async_cleanup'))
                          or retired.get('cleanup_done').is_set()):
                        return False
                    if not failed_acknowledge:
                        target = retired
                        target_thread = self._retired_thread
                        target_kind = 'running'
                        retired['stopping'] = True
                        retired['async_cleanup'] = True
                        retired['phase'] = 'retired'
                        retired['cancelled'] = True
                        retired['event'].set()
                        retired['blocks_successors'] = True
            if target is not None:
                self._pending_cleanup[id(target)] = target
        if failed_acknowledge:
            report = self._callback_once(on_result)
            report(False)
            return True
        report = self._callback_once(on_result)
        self._queue_cleanup(target, target_thread, target_kind, report)
        return True

    def _queue_cleanup(self, context, thread, target_kind, report):
        key = id(context)
        with self._lock:
            if key in self._cleanup_threads:
                return
            context['cleanup_started'] = True

        def _cleanup():
            ok = True
            try:
                if target_kind == 'opening':
                    context['start_done'].wait()
                    ok = (self._wait_for_predecessors(
                        context, wait_all=True) and ok)
                elif thread is not None:
                    context['worker_started'].wait()
                if thread is not None and thread is not threading.current_thread():
                    try:
                        thread.join()
                    except RuntimeError:
                        if not context.get('strict_worker_liveness'):
                            raise
                        try:
                            if thread.is_alive():
                                ok = False
                        except AttributeError as exc:
                            ok = False
                            _log_exception(
                                'Could not prove capture worker ended', exc)
                if thread is not None:
                    try:
                        ok = (not thread.is_alive()) and ok
                    except AttributeError as exc:
                        if context.get('strict_worker_liveness'):
                            ok = False
                            _log_exception(
                                'Could not prove capture worker ended', exc)
                        else:
                            # Thread adapters may not execute the target or
                            # expose is_alive.
                            pass
                if ok and context.get('cap') is not None:
                    ok = self._release_capture(context) and ok
            except Exception as exc:
                ok = False
                _log_exception('Asynchronous camera cleanup failed', exc)
            finally:
                with self._lock:
                    if ok and self._retired_context is context:
                        self._retired_context = None
                        self._retired_thread = None
                    if ok:
                        self._pending_cleanup.pop(key, None)
                    else:
                        context['blocks_successors'] = True
                        if context.get('start_failure_cleanup'):
                            context['async_cleanup'] = False
                        self._pending_cleanup[key] = context
                    self._cleanup_threads.pop(key, None)
                    context['phase'] = 'cleaned' if ok else 'cleanup_failed'
                    context['cleanup_ok'] = ok
                    context['cleanup_done'].set()
                report(ok)

        try:
            worker = threading.Thread(target=_cleanup, daemon=True,
                                      name='fthr-camera-cleanup')
            with self._lock:
                self._cleanup_threads[key] = worker
            worker.start()
        except Exception as exc:
            with self._lock:
                self._cleanup_threads.pop(key, None)
                context['blocks_successors'] = True
                if context.get('start_failure_cleanup'):
                    context['async_cleanup'] = False
                context['phase'] = 'cleanup_failed'
                context['cleanup_ok'] = False
                self._pending_cleanup[key] = context
                context['cleanup_done'].set()
            _log_exception('Could not start asynchronous camera cleanup', exc)
            report(False)

    def _stop_locked(self, successor_context=None):
        cleanup_to_queue = None
        with self._lock:
            active_context = self._run_context
            thread = self._thread or self._retired_thread
            context = active_context or self._retired_context
            if context is not None and successor_context is not None:
                if not self._link_successor_context_locked(
                        context, successor_context):
                    return False
            if context is not None:
                cleanup_done = context['cleanup_done'].is_set()
                if (context.get('async_cleanup') and not cleanup_done):
                    return False
                if cleanup_done and context.get('cleanup_ok') is False:
                    return False
                if (context.get('async_cleanup') and cleanup_done
                        and context.get('cleanup_ok') is True):
                    if self._retired_context is context:
                        self._retired_thread = None
                        self._retired_context = None
                    context = None
                    thread = None
                    active_context = None
            if self._stop_event is not None:
                self._stop_event.set()
            if (active_context is None and context is not None
                    and successor_context is not None):
                context['async_cleanup'] = True
                context['blocks_successors'] = True
                self._pending_cleanup[id(context)] = context
                cleanup_to_queue = context
            if active_context is not None:
                active_context['stopping'] = True
                active_context['cancelled'] = True
                active_context['phase'] = 'retired'
                if successor_context is not None:
                    active_context['async_cleanup'] = True
                    active_context['blocks_successors'] = True
                    self._pending_cleanup[id(active_context)] = active_context
                    cleanup_to_queue = active_context
                self._running = False
                self._thread = None
                self._cap = None
                self._stop_event = None
                self._run_context = None
                self._generation += 1
                self._chunks.clear()
                self._latest_frame = None
                self._retired_thread = thread
                self._retired_context = active_context
        if thread is not None:
            try:
                if thread is not threading.current_thread():
                    thread.join(timeout=2.0)
            except RuntimeError as exc:
                _log_exception('Could not join capture worker', exc)
            try:
                alive = thread.is_alive()
            except AttributeError:
                # Thread adapters may not execute the target or expose is_alive.
                alive = False
            if not alive and context is not None:
                release_ok = self._release_capture(context)
                if not release_ok:
                    self._retain_release_failure(context)
                    return False
                with self._lock:
                    context['cleanup_ok'] = True
                    context['cleanup_done'].set()
                    self._pending_cleanup.pop(id(context), None)
                    if self._retired_context is context:
                        self._retired_thread = None
                        self._retired_context = None
                    context['phase'] = 'cleaned'
                return True
            if alive and context is not None:
                with self._lock:
                    self._retired_thread = thread
                    self._retired_context = context
                if cleanup_to_queue is not None:
                    self._queue_cleanup(
                        cleanup_to_queue, thread, 'running',
                        self._callback_once(None))
                _log_exception('Capture worker cleanup is still pending',
                               RuntimeError(
                                   'previous capture worker is still active'))
                return False
        elif context is not None:
            release_ok = self._release_capture(context)
            if not release_ok:
                self._retain_release_failure(context)
                return False
            with self._lock:
                context['cleanup_ok'] = True
                context['cleanup_done'].set()
                self._pending_cleanup.pop(id(context), None)
                if self._retired_context is context:
                    self._retired_thread = None
                    self._retired_context = None
                context['phase'] = 'cleaned'
            return True
        return True

    @property
    def latest_frame(self):
        return self._latest_frame

    @staticmethod
    def _release_capture(context):
        with context['release_lock']:
            if context['released']:
                return context.get('release_ok', False)
            context['released'] = True
            cap = context.get('cap')
            if cap is None:
                context['release_ok'] = True
                return True
        try:
            release_result = cap.release()
            if release_result is False:
                context['release_error'] = RuntimeError(
                    'Capture release returned false')
                context['release_ok'] = False
                _log_exception('Capture release failed',
                               context['release_error'])
                return False
            context['release_ok'] = True
            return True
        except Exception as exc:
            context['release_error'] = exc
            context['release_ok'] = False
            _log_exception('Capture release failed', exc)
            return False

    def _capture_loop(self, cap, stop_event, generation, context):
        try:
            while not stop_event.is_set():
                if not cap.isOpened():
                    break
                ret, frame = cap.read()
                if stop_event.is_set():
                    break
                if not ret:
                    time.sleep(0.033)
                    continue
                ts = time.monotonic()
                with self._lock:
                    if (stop_event.is_set() or self._generation != generation
                            or self._run_context is not context):
                        break
                    self._latest_frame = frame
                ok, buf = _cv2.imencode(
                    '.jpg', frame,
                    [_cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if stop_event.is_set():
                    break
                if not ok:
                    continue
                cutoff = ts - KEEP_SECONDS
                with self._lock:
                    if (stop_event.is_set() or self._generation != generation
                            or self._run_context is not context):
                        break
                    self._chunks.append((ts, buf))
                    while self._chunks and self._chunks[0][0] < cutoff:
                        self._chunks.popleft()
        except Exception as exc:
            _log_exception('Capture worker failed', exc)
        finally:
            release_ok = self._release_capture(context)
            if not release_ok:
                self._retain_release_failure(context)
            with self._lock:
                if (self._generation == generation
                        and self._run_context is context):
                    self._running = False
                    self._cap = None
                    self._thread = None
                    self._stop_event = None
                    self._run_context = None
                    self._latest_frame = None
                    self._chunks.clear()
                    if context.get('owner_token') is not None:
                        # The worker cannot join itself. Keep the exact owned
                        # context and thread as a retired barrier until an
                        # external stop or next start performs the join.
                        context['natural_exit'] = True
                        context['phase'] = (
                            'retired' if release_ok else 'cleanup_failed')
                        self._retired_thread = (
                            context.get('thread') or
                            threading.current_thread())
                        self._retired_context = context
                        self._pending_cleanup[id(context)] = context
                elif (self._retired_context is context
                      and not context.get('async_cleanup')):
                    if release_ok:
                        self._retired_thread = None
                        self._retired_context = None
                        context['cleanup_ok'] = True
                        context['cleanup_done'].set()
                        context['phase'] = 'cleaned'
                    else:
                        context['blocks_successors'] = True
                        context['cleanup_ok'] = False
                        context['cleanup_done'].set()
                        context['phase'] = 'cleanup_failed'

    def _iter_segment_frames(self, end_time: float, duration_sec: float, *,
                             expected_generation: int | None = None,
                             expected_device_index: int | None = None):
        start_time = end_time - duration_sec
        with self._lock:
            if (expected_generation is not None
                    and int(expected_generation) != self._generation):
                return
            if (expected_device_index is not None
                    and int(expected_device_index) != self._device_index):
                return
            if not self._running:
                return
            bufs = [buf for ts, buf in self._chunks if start_time <= ts <= end_time]
        for buf in bufs:
            frame = _cv2.imdecode(buf, _cv2.IMREAD_COLOR)
            if frame is not None:
                yield frame

    def extract_segment(self, end_time: float, duration_sec: float, *,
                        expected_generation: int | None = None,
                        expected_device_index: int | None = None
                        ) -> list | None:
        frames = list(self._iter_segment_frames(
            end_time, duration_sec,
            expected_generation=expected_generation,
            expected_device_index=expected_device_index))
        return frames if frames else None

    def write_segment(self, path: str, end_time: float, duration_sec: float,
                      fps: float = DEFAULT_FPS, *,
                      expected_generation: int | None = None,
                      expected_device_index: int | None = None) -> bool:
        if not _AVAILABLE:
            return False
        frames = self._iter_segment_frames(
            end_time, duration_sec,
            expected_generation=expected_generation,
            expected_device_index=expected_device_index,
        )
        frame = next(frames, None)
        if frame is None:
            return False
        h, w = frame.shape[:2]
        fourcc = _cv2.VideoWriter_fourcc(*'mp4v')
        writer = _cv2.VideoWriter(path, fourcc, fps, (w, h))
        try:
            if not writer.isOpened():
                return False
            while frame is not None:
                if frame.shape[:2] == (h, w):  # skip any mismatched frames
                    writer.write(frame)
                frame = next(frames, None)
            return True
        finally:
            writer.release()
