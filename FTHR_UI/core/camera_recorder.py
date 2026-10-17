from __future__ import annotations

import threading
import time
import re
import subprocess
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

    @classmethod
    def is_available(cls) -> bool:
        return _AVAILABLE

    @classmethod
    def list_devices(cls) -> list[dict]:
        """Return camera indices with real Windows device names when available."""
        if not _AVAILABLE:
            return []
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
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
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
            if (self._running and self._device_index == device_index
                    and self._cap is not None):
                try:
                    if self._cap.isOpened():
                        return True
                except (AttributeError, RuntimeError):
                    # A stale backend is treated as closed and replaced below.
                    pass
            self._stop_locked()
            self._device_index = device_index
            self._cap = _cv2.VideoCapture(device_index)
            if not self._cap.isOpened():
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._capture_loop, daemon=True, name='fthr-camera')
            self._thread.start()
            return True

    def start_async(self, device_index: int = 0, on_result=None):
        """Open the device off the UI thread. on_result(ok: bool) is invoked
        from the worker thread — marshal back to Qt yourself if needed."""
        def _work():
            ok = self.start(device_index)
            if on_result:
                try:
                    on_result(ok)
                except Exception:
                    pass
        threading.Thread(target=_work, daemon=True,
                         name='fthr-camera-open').start()

    def stop(self):
        with self._op_lock:
            self._stop_locked()

    def _stop_locked(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        with self._lock:
            self._chunks.clear()
        self._latest_frame = None

    @property
    def latest_frame(self):
        return self._latest_frame

    def _capture_loop(self):
        while self._running:
            if not self._cap or not self._cap.isOpened():
                break
            ret, frame = self._cap.read()
            if not ret:
                time.sleep(0.033)
                continue
            ts = time.monotonic()
            self._latest_frame = frame  # raw BGR for UI preview (single frame)
            ok, buf = _cv2.imencode('.jpg', frame,
                                    [_cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if not ok:
                continue
            cutoff = ts - KEEP_SECONDS
            with self._lock:
                self._chunks.append((ts, buf))
                while self._chunks and self._chunks[0][0] < cutoff:
                    self._chunks.popleft()

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
