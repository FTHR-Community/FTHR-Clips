from __future__ import annotations

import threading
import time
import re
import subprocess
import sys
from collections import deque
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

        # Cross-platform fallback: do not invent inactive entries. On Linux,
        # an active V4L2 device can be exclusive, so preserve that known-open
        # entry without attempting a second open.
        devices = []
        active_device = None
        if sys.platform.startswith('linux'):
            instance = cls._instance
            if instance is not None:
                with instance._lock:
                    if instance._running and instance._cap is not None:
                        active_device = instance._device_index
                if active_device is not None:
                    devices.append({
                        'index': active_device,
                        'name': f'Camera {active_device + 1}',
                    })
        for index in range(8):
            if index == active_device:
                continue
            cap = None
            try:
                cap = _cv2.VideoCapture(index)
                if cap.isOpened():
                    devices.append({'index': index, 'name': f'Camera {index + 1}'})
            except Exception:
                pass
            finally:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        # A failed probe may have lost its backend; it must not
                        # prevent releasing other probes or retaining the feed.
                        pass
        return sorted(devices, key=lambda device: device['index'])

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, device_index: int = 0) -> bool:
        """Blocking open — cv2.VideoCapture can take seconds on Windows/MSMF.
        Never call this from the UI thread; use start_async there."""
        if not _AVAILABLE:
            return False
        with self._op_lock:
            # Settings refreshes and unrelated UI updates can ask for the
            # already-active device again. Reopening MSMF here caused visible
            # camera freezes and could briefly leave two native sessions alive.
            with self._lock:
                current_cap = self._cap
                already_running = (self._running
                                   and self._device_index == device_index
                                   and current_cap is not None)
            if already_running:
                try:
                    if current_cap.isOpened():
                        return True
                except Exception as exc:
                    # A stale backend is treated as closed and replaced below.
                    _log_exception('Active device health check failed', exc)
            if not self._stop_locked():
                return False

            cap = None
            try:
                cap = _cv2.VideoCapture(device_index)
                if not cap.isOpened():
                    print(f'[Camera] Device {device_index} did not open')
                    self._release_capture({
                        'cap': cap, 'released': False,
                        'release_lock': threading.Lock(),
                    })
                    return False
            except Exception as exc:
                _log_exception(f'Could not open device {device_index}', exc)
                if cap is not None:
                    self._release_capture({
                        'cap': cap, 'released': False,
                        'release_lock': threading.Lock(),
                    })
                return False

            stop_event = threading.Event()
            context = {'cap': cap, 'event': stop_event,
                       'release_lock': threading.Lock(), 'released': False}
            try:
                with self._lock:
                    generation = self._generation + 1
                    thread = threading.Thread(
                        target=self._capture_loop,
                        args=(cap, stop_event, generation, context),
                        daemon=True, name='fthr-camera')
                    self._generation = generation
                    self._device_index = device_index
                    self._cap = cap
                    self._stop_event = stop_event
                    self._run_context = context
                    self._running = True
                    self._latest_frame = None
                    self._chunks.clear()
                    self._thread = thread
                thread.start()
            except Exception as exc:
                _log_exception('Could not create or start capture worker', exc)
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
                self._release_capture(context)
                return False
            return True

    def start_async(self, device_index: int = 0, on_result=None):
        """Open the device off the UI thread. on_result(ok: bool) is invoked
        from the worker thread — marshal back to Qt yourself if needed."""
        def _report(ok):
            if on_result:
                try:
                    on_result(ok)
                except Exception as exc:
                    _log_exception('Open-result callback failed', exc)

        def _work():
            try:
                ok = self.start(device_index)
            except Exception as exc:
                _log_exception('Asynchronous open failed', exc)
                ok = False
            _report(ok)

        try:
            threading.Thread(target=_work, daemon=True,
                              name='fthr-camera-open').start()
        except Exception as exc:
            _log_exception('Could not start asynchronous open', exc)
            _report(False)

    def stop(self):
        with self._op_lock:
            self._stop_locked()

    def _stop_locked(self):
        with self._lock:
            active_context = self._run_context
            thread = self._thread or self._retired_thread
            context = active_context or self._retired_context
            if self._stop_event is not None:
                self._stop_event.set()
            if active_context is not None:
                self._running = False
                self._thread = None
                self._cap = None
                self._stop_event = None
                self._run_context = None
                self._generation += 1
                self._chunks.clear()
                self._latest_frame = None
        if thread is not None:
            try:
                if thread is not threading.current_thread():
                    thread.join(timeout=2.0)
            except RuntimeError as exc:
                _log_exception('Could not join capture worker', exc)
            try:
                alive = thread.is_alive()
            except AttributeError:
                # Lightweight adapters may not run the target or expose health.
                alive = False
            if not alive and context is not None:
                self._release_capture(context)
                with self._lock:
                    if self._retired_context is context:
                        self._retired_thread = None
                        self._retired_context = None
            elif alive and context is not None:
                with self._lock:
                    self._retired_thread = thread
                    self._retired_context = context
                _log_exception('Capture worker cleanup is still pending',
                               RuntimeError('previous capture worker is still active'))
                return False
        elif context is not None:
            self._release_capture(context)
        return True

    @property
    def latest_frame(self):
        return self._latest_frame

    @staticmethod
    def _release_capture(context):
        with context['release_lock']:
            if context['released']:
                return
            context['released'] = True
        try:
            context['cap'].release()
        except Exception as exc:
            _log_exception('Capture release failed', exc)

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
            self._release_capture(context)
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
                elif self._retired_context is context:
                    self._retired_thread = None
                    self._retired_context = None

    def extract_segment(self, end_time: float, duration_sec: float) -> list | None:
        start_time = end_time - duration_sec
        with self._lock:
            bufs = [buf for ts, buf in self._chunks if start_time <= ts <= end_time]
        frames = [_cv2.imdecode(b, _cv2.IMREAD_COLOR) for b in bufs]
        frames = [f for f in frames if f is not None]
        return frames if frames else None

    def write_segment(self, path: str, end_time: float, duration_sec: float,
                      fps: float = DEFAULT_FPS) -> bool:
        if not _AVAILABLE:
            return False
        frames = self.extract_segment(end_time, duration_sec)
        if not frames:
            return False
        h, w = frames[0].shape[:2]
        fourcc = _cv2.VideoWriter_fourcc(*'mp4v')
        writer = _cv2.VideoWriter(path, fourcc, fps, (w, h))
        if not writer.isOpened():
            return False
        for f in frames:
            if f.shape[:2] == (h, w):  # skip any mismatched frames
                writer.write(f)
        writer.release()
        return True
