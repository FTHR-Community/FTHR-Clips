import sys, time
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

import numpy as np
from core.camera_recorder import CameraRecorder

def _fresh():
    CameraRecorder._instance = None
    return CameraRecorder()


def test_no_frames_returns_none():
    r = _fresh()
    assert r.extract_segment(time.monotonic(), 5) is None


def test_extract_returns_frames_in_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    r._lock.acquire()
    r._chunks.append((now - 2.0, frame.copy()))
    r._chunks.append((now - 1.0, frame.copy()))
    r._chunks.append((now - 0.0, frame.copy()))
    r._lock.release()
    result = r.extract_segment(now, 3)
    assert result is not None
    assert len(result) == 3


def test_extract_respects_time_window():
    r = _fresh()
    now = time.monotonic()
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    r._lock.acquire()
    r._chunks.append((now - 10.0, frame.copy()))  # outside 5s window
    r._chunks.append((now - 2.0,  frame.copy()))  # inside
    r._chunks.append((now - 1.0,  frame.copy()))  # inside
    r._lock.release()
    result = r.extract_segment(now, 5)
    assert result is not None
    assert len(result) == 2
