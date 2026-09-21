import ctypes
import sys

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from core.capture_bridge import CaptureBridge, SharedMemoryLayout, CommandType, ResponseType


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


def test_request_engine_shutdown_uses_only_the_existing_command_field():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)

    assert bridge.request_engine_shutdown()
    assert layout.ui_command == CommandType.SHUTDOWN


def test_get_active_codec_reads_string():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.active_codec = b'hevc_nvenc'
    assert bridge.get_active_codec() == 'hevc_nvenc'


def test_get_active_codec_empty_when_not_set():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    assert bridge.get_active_codec() == ''


def test_get_status_returns_stable_python_numeric_types():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.frames_captured = 27
    layout.capture_generation = 3
    layout.content_luma_mean = 12.5

    status = bridge.get_status()

    assert status['frames_captured'] == 27
    assert type(status['frames_captured']) is int
    assert type(status['capture_generation']) is int
    assert type(status['content_luma_mean']) is float


def test_set_encoder_config_clamps_preset():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    bridge.set_encoder_config('h264', 0)    # below min
    assert layout.cfg_preset == 1
    bridge.set_encoder_config('h264', 99)   # above max
    assert layout.cfg_preset == 7


def test_save_clip_submits_without_blocking():
    """AUDIT-011: save_clip() is a command submit and must return immediately.

    The old version busy-waited up to a second for SAVE_STARTED on the Qt main
    thread. Nothing acks here, so a surviving wait loop shows up as elapsed
    time. The 100 ms ceiling is generous by three orders of magnitude.
    """
    import time as _time

    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    # engine_response stays NONE — nothing will ever acknowledge this.

    started = _time.monotonic()
    result = bridge.save_clip(_a_path(), 30)
    elapsed = _time.monotonic() - started

    assert result is True, 'submit should succeed — it only writes fields'
    assert elapsed < 0.1, (
        f'save_clip() blocked for {elapsed*1000:.0f} ms — it must not wait '
        f'for the engine (AUDIT-011)'
    )
    assert layout.ui_command == CommandType.SAVE_CLIP
    assert layout.ui_param1 == 30


def test_save_clip_writes_command_last():
    """The engine may read every other field the moment ui_command lands, so
    the command code has to be written after the path and duration."""
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    bridge.save_clip(_a_path(), 17)

    assert layout.ui_param1 == 17
    assert layout.ui_command == CommandType.SAVE_CLIP


def test_save_clip_does_not_touch_engine_response():
    """Submitting a save must preserve the previous unconsumed engine response."""
    for pending in (ResponseType.CLIP_SAVED, ResponseType.ERROR_OCCURRED,
                    ResponseType.SAVE_STARTED):
        layout, buf = _make_fake_layout()
        bridge = _FakeBridge(layout)
        layout.engine_response = pending

        bridge.save_clip(_a_path(), 30)

        assert layout.engine_response == pending, (
            f'save_clip() overwrote a pending {pending!r} (AUDIT-017)')


def test_save_clip_refuses_when_disconnected():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.is_initialized = False
    assert bridge.save_clip(_a_path(), 30) is False


def test_bridge_has_no_second_response_consumer():
    """AUDIT-011: exactly one consumer. The blocking waiter and the old
    auto-consuming poller are gone and must not come back."""
    assert not hasattr(CaptureBridge, 'wait_for_clip_completion')
    assert not hasattr(CaptureBridge, 'poll_async_result')


def test_peek_does_not_consume():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.CLIP_SAVED

    for _ in range(3):
        assert bridge.peek_save_response() == ('saved', '')
        assert layout.engine_response == ResponseType.CLIP_SAVED


def test_peek_returns_kind_and_detail_together():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_string = _encode_engine_string('SaveClip failed: disk full')
    layout.engine_response = ResponseType.ERROR_OCCURRED

    kind, detail = bridge.peek_save_response()
    assert kind == 'error'
    assert detail == 'SaveClip failed: disk full'


def test_peek_reports_save_started():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.SAVE_STARTED
    assert bridge.peek_save_response()[0] == 'started'


def test_ack_consumption_preserves_completion_published_after_peek():
    for terminal, kind in ((ResponseType.CLIP_SAVED, 'saved'),
                           (ResponseType.ERROR_OCCURRED, 'error')):
        layout, buf = _make_fake_layout()
        bridge = _FakeBridge(layout)
        layout.engine_response = ResponseType.SAVE_STARTED
        peeked_kind, _ = bridge.peek_save_response()
        # The save worker finishes while the UI interprets its earlier ack.
        layout.engine_string = _encode_engine_string('terminal payload')
        layout.engine_response = terminal

        assert bridge.consume_save_response(peeked_kind)
        assert layout.engine_response == terminal
        assert bridge.peek_save_response() == (kind, 'terminal payload')
        assert bridge.consume_save_response(kind)
        assert layout.engine_response == ResponseType.NONE


def test_ack_is_delivered_once_and_rearmed_for_next_save():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    for _ in range(3):
        assert bridge.save_clip(_a_path(), 5)
        layout.engine_response = ResponseType.SAVE_STARTED
        assert bridge.peek_save_response() == ('started', '')
        assert bridge.consume_save_response('started')
        # No response means the poller advances timeout deadlines normally.
        assert bridge.peek_save_response() is None
        assert not bridge.consume_save_response('started')
        layout.engine_response = ResponseType.CLIP_SAVED
        assert bridge.peek_save_response() == ('saved', '')
        assert bridge.consume_save_response('saved')


def test_peek_ignores_unrelated_responses():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    for resp in (ResponseType.RECORDING_STARTED, ResponseType.RECORDING_STOPPED,
                 ResponseType.STATUS_UPDATE):
        layout.engine_response = resp
        assert bridge.peek_save_response() is None, f'{resp!r} misread as a save verdict'
        assert bridge.consume_save_response() is False
        assert layout.engine_response == resp


def test_manual_recording_responses_have_an_independent_consumer():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.RECORDING_STARTED

    assert bridge.peek_save_response() is None
    assert bridge.peek_manual_recording_response() == ('started', '')
    assert bridge.consume_manual_recording_response()
    assert layout.engine_response == ResponseType.NONE


def test_manual_recording_error_is_not_misread_as_save_failure():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.MANUAL_RECORDING_ERROR

    assert bridge.peek_save_response() is None
    assert bridge.peek_manual_recording_response()[0] == 'error'


def test_consume_clears_exactly_once():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.ERROR_OCCURRED

    assert bridge.consume_save_response() is True
    assert layout.engine_response == ResponseType.NONE
    assert bridge.consume_save_response() is False


def test_peek_survives_garbled_engine_string():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.ERROR_OCCURRED
    if sys.platform != 'win32':
        layout.engine_string = bytes([0xff,0xfe]) + b' invalid utf8 ' + bytes([0xc3])

    peeked = bridge.peek_save_response()
    assert peeked is not None
    assert peeked[0] == 'error'
    assert isinstance(peeked[1], str)


def test_peek_returns_none_when_disconnected():
    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_response = ResponseType.CLIP_SAVED
    layout.is_initialized = False
    assert bridge.peek_save_response() is None


def _a_path() -> str:
    """A path both layouts accept (Windows ui_string is c_wchar * 256)."""
    return 'C:/clips/test.mp4' if sys.platform == 'win32' else '/tmp/test.mp4'


def _encode_engine_string(text: str):
    """engine_string is wchar on Windows and raw bytes on Linux."""
    return text if sys.platform == 'win32' else text.encode('utf-8')


def test_get_status_exposes_failure_detail_only_with_backend_failed():
    from core.capture_health import CaptureHealthFlag

    layout, buf = _make_fake_layout()
    bridge = _FakeBridge(layout)
    layout.engine_string = _encode_engine_string(
        "Screen sharing was declined in the desktop's dialog.")

    # engine_string also carries save results; it is only a capture failure
    # detail while the engine publishes BACKEND_FAILED alongside it.
    layout.capture_health_flags = CaptureHealthFlag.ACTIVE
    assert bridge.get_status()['capture_failure_detail'] == ''

    layout.capture_health_flags = CaptureHealthFlag.BACKEND_FAILED
    # Only the Linux engine writes the reason there; on Windows the string
    # still holds the last save error, so the bridge reports nothing.
    expected = ('' if sys.platform == 'win32'
                else "Screen sharing was declined in the desktop's dialog.")
    assert bridge.get_status()['capture_failure_detail'] == expected
