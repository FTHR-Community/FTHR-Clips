import sys, time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))

import cv2
import numpy as np
from core.camera_recorder import CameraRecorder, JPEG_QUALITY

def _fresh():
    CameraRecorder._instance = None
    return CameraRecorder()

def _encode(frame):
    _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return buf

def test_no_frames_returns_none():
    r = _fresh()
    assert r.extract_segment(time.monotonic(), 5) is None


def test_extract_returns_frames_in_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    buf = _encode(frame)
    r._lock.acquire()
    r._running = True
    r._chunks.append((now - 2.0, buf))
    r._chunks.append((now - 1.0, buf))
    r._chunks.append((now - 0.0, buf))
    r._lock.release()
    result = r.extract_segment(now, 3)
    assert result is not None
    assert len(result) == 3


def test_extract_respects_time_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    buf = _encode(frame)
    r._lock.acquire()
    r._running = True
    r._chunks.append((now - 10.0, buf))  # outside 5s window
    r._chunks.append((now - 2.0,  buf))  # inside
    r._chunks.append((now - 1.0,  buf))  # inside
    r._lock.release()
    result = r.extract_segment(now, 5)
    assert result is not None
    assert len(result) == 2
