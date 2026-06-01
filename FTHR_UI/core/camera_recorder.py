from __future__ import annotations

import threading
import time
from collections import deque
from typing import Optional

try:
    import cv2 as _cv2
    import numpy as _np
    _AVAILABLE = True
except ImportError:
    _AVAILABLE = False

KEEP_SECONDS = 90
DEFAULT_FPS   = 30


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
        self._chunks: deque = deque()  # (monotonic_ts, BGR frame)
        self._running = False
        self._thread: threading.Thread | None = None
        self._latest_frame = None

    @classmethod
    def is_available(cls) -> bool:
        return _AVAILABLE

    def is_running(self) -> bool:
        return self._running

    def start(self, device_index: int = 0) -> bool:
        if not _AVAILABLE:
            return False
        self.stop()
        self._device_index = device_index
        self._cap = _cv2.VideoCapture(device_index)
        if not self._cap.isOpened():
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop, daemon=True, name='fthr-camera')
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._cap:
            self._cap.release()
            self._cap = None
        with self._lock:
            self._chunks.clear()

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
            self._latest_frame = frame
            cutoff = ts - KEEP_SECONDS
            with self._lock:
                self._chunks.append((ts, frame))
                while self._chunks and self._chunks[0][0] < cutoff:
                    self._chunks.popleft()

    def extract_segment(self, end_time: float, duration_sec: float) -> list | None:
        start_time = end_time - duration_sec
        with self._lock:
            frames = [f for ts, f in self._chunks if start_time <= ts <= end_time]
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
        for f in frames:
            writer.write(f)
        writer.release()
        return True
