import ctypes
import sys
import pytest

sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.capture_bridge import CaptureBridge, SharedMemoryLayout, CommandType


def _make_fake_layout():
    buf = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
    layout = SharedMemoryLayout.from_buffer(buf)
    layout.is_initialized = True
    return layout, buf


class _FakeBridge(CaptureBridge):
    """CaptureBridge subclass backed by in-process fake layout."""
    def __init__(self, layout):
        self._layout = layout
        self._initialized = True


def test_set_encoder_config_writes_fields():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    result = bridge.set_encoder_config('hevc', 5)
    assert result is True
    assert layout.cfg_codec_pref == 2           # hevc = 2
    assert layout.cfg_preset     == 5
    assert layout.ui_command     == CommandType.RECONFIGURE_ENCODER


def test_set_encoder_config_auto_maps_to_zero():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    bridge.set_encoder_config('auto', 4)
    assert layout.cfg_codec_pref == 0


def test_get_active_codec_reads_string():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.active_codec = b'hevc_nvenc'
    assert bridge.get_active_codec() == 'hevc_nvenc'


def test_get_active_codec_empty_when_not_set():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    assert bridge.get_active_codec() == ''


def test_set_encoder_config_clamps_preset():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    bridge.set_encoder_config('h264', 0)    # below min
    assert layout.cfg_preset == 1
    bridge.set_encoder_config('h264', 99)   # above max
    assert layout.cfg_preset == 7


def test_save_clip_timeout_survives_backward_clock_jump(monkeypatch):
    """H-01: save_clip() must not hang when system clock jumps backward (NTP).
    Uses a watchdog thread to detect an infinite loop within 5 seconds."""
    import time as _time
    import threading

    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    # engine_response stays NONE — engine never acks, so timeout must fire

    original_time = _time.time
    call_count = [0]

    def patched_time():
        call_count[0] += 1
        # After a few calls simulate NTP adjusting clock backward by 10 seconds
        if call_count[0] > 5:
            return original_time() - 10.0
        return original_time()

    monkeypatch.setattr(_time, 'time', patched_time)

    result = [None]
    def run():
        result[0] = bridge.save_clip('/tmp/test.mp4', 30)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), (
        "save_clip() hung — time.time() clock-jump caused infinite loop; "
        "use time.monotonic() instead"
    )
    assert result[0] is False


def test_wait_for_clip_timeout_survives_backward_clock_jump(monkeypatch):
    """H-01: wait_for_clip_completion() must not hang when clock jumps backward."""
    import time as _time
    import threading

    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    original_time = _time.time
    call_count = [0]

    def patched_time():
        call_count[0] += 1
        if call_count[0] > 5:
            return original_time() - 10.0
        return original_time()

    monkeypatch.setattr(_time, 'time', patched_time)

    result = [None]
    def run():
        result[0] = bridge.wait_for_clip_completion(timeout_ms=500)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5.0)

    assert not t.is_alive(), (
        "wait_for_clip_completion() hung — clock-jump with time.time(); "
        "use time.monotonic() instead"
    )
    assert result[0] is False
