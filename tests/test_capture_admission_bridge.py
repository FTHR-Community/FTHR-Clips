"""Read-only retained terminal response tests for the capture bridge."""

from __future__ import annotations

import ctypes
from dataclasses import FrozenInstanceError
import mmap
import threading
import weakref
from types import SimpleNamespace

import pytest

from core.capture_bridge import CaptureBridge
from core.capture_bridge import CommandType
from core.capture_health import CaptureHealthFlag
from core.capture_bridge import ResponseType
from core.capture_bridge import SharedMemoryLayout
from core.capture_bridge import TerminalResponseSnapshot
from core.capture_bridge import _TerminalResponseOwner


def _make_fake_layout():
    buffer = (ctypes.c_byte * ctypes.sizeof(SharedMemoryLayout))()
    layout = SharedMemoryLayout.from_buffer(buffer)
    layout.is_initialized = True
    return layout, buffer


def test_focus_paused_save_reaches_native_replay_validation(fake_bridge):
    bridge, layout, _buffer = fake_bridge
    layout.capture_health_flags = CaptureHealthFlag.ACTIVE | CaptureHealthFlag.PAUSED
    layout.engine_response = ResponseType.CLIP_SAVED
    assert bridge.save_clip('C:/clips/retained.mp4', 60)
    assert layout.ui_command == CommandType.SAVE_CLIP
    assert layout.ui_param1 == 60
    assert layout.engine_response == ResponseType.CLIP_SAVED
    # Pausing must not permit overwriting an in-flight command.
    assert not bridge.save_clip('C:/clips/second.mp4', 30)
    assert layout.ui_param1 == 60


@pytest.mark.parametrize('failure_flag', [
    CaptureHealthFlag.RECOVERING,
    CaptureHealthFlag.BACKEND_FAILED,
    CaptureHealthFlag.RECONFIGURE_REQUIRED,
])
def test_focus_paused_save_still_rejects_failed_backend(fake_bridge, failure_flag):
    bridge, layout, buffer = fake_bridge
    layout.capture_health_flags = CaptureHealthFlag.PAUSED | failure_flag
    before = bytes(buffer)
    assert not bridge.save_clip('C:/clips/retained.mp4', 60)
    assert bytes(buffer) == before


def test_focus_paused_highlight_stays_blocked(fake_bridge):
    bridge, layout, buffer = fake_bridge
    layout.capture_health_flags = (
        CaptureHealthFlag.ACTIVE | CaptureHealthFlag.PAUSED | bridge.HIGHLIGHT_CAPABILITY)
    before = bytes(buffer)
    assert not bridge.mark_highlight('12345678-1234-1234-1234-123456789abc')
    assert bytes(buffer) == before


def _set_engine_string(layout, value: str):
    if isinstance(layout.engine_string, bytes):
        layout.engine_string = value.encode('utf-8')
    else:
        layout.engine_string = value


@pytest.fixture
def fake_bridge():
    previous = CaptureBridge._instance
    CaptureBridge._instance = None
    bridge = CaptureBridge()
    layout, buffer = _make_fake_layout()
    bridge._layout = layout
    bridge._initialized = True
    bridge._mem_handle = None
    bridge._win_map_ptr = None
    bridge._linux_mmap = None
    bridge._save_acknowledged_layout = None
    bridge._mapping_lock = threading.RLock()
    try:
        yield bridge, layout, buffer
    finally:
        bridge._initialized = False
        bridge._layout = None
        bridge._mem_handle = None
        bridge._win_map_ptr = None
        bridge._linux_mmap = None
        bridge._save_acknowledged_layout = None
        CaptureBridge._instance = previous


class _CloseRecorder:
    def __init__(self, *, error=None):
        self.close_calls = 0
        self.error = error
        self.closed = False

    def close(self):
        self.close_calls += 1
        if self.error is not None:
            raise self.error
        self.closed = True


class _Kernel32Recorder:
    def __init__(self, *, unmap=True, close=True):
        self.unmap_result = unmap
        self.close_result = close
        self.unmap_calls = []
        self.close_calls = []

    def UnmapViewOfFile(self, pointer):
        self.unmap_calls.append(pointer)
        return self.unmap_result

    def CloseHandle(self, handle):
        self.close_calls.append(handle)
        return self.close_result


def _prepare_windows_owner(bridge, layout, generation=41):
    layout.capture_generation = generation
    bridge._win_map_ptr = 0x1234
    bridge._mem_handle = 0x5678
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=generation)
    assert owner is not None
    return owner


@pytest.mark.parametrize('response,channel,kind', [
    (ResponseType.SAVE_STARTED, 'save_response', 'started'),
    (ResponseType.CLIP_SAVED, 'save_response', 'saved'),
    (ResponseType.ERROR_OCCURRED, 'save_response', 'error'),
    (ResponseType.RECORDING_STARTED, 'manual_response', 'started'),
    (ResponseType.RECORDING_STOPPED, 'manual_response', 'stopped'),
    (ResponseType.MANUAL_RECORDING_ERROR, 'manual_response', 'error'),
])
def test_terminal_snapshot_reads_each_channel_after_native_init_false(
        fake_bridge, response, channel, kind):
    bridge, layout, _buffer = fake_bridge
    layout.capture_generation = 17
    layout.engine_response = response
    layout.is_recording = response == ResponseType.RECORDING_STARTED
    _set_engine_string(layout, 'terminal detail')

    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=17)
    assert owner is not None
    layout.is_initialized = False

    snapshot = bridge.read_terminal_response_snapshot(
        owner, process_exited=True)

    assert isinstance(snapshot, TerminalResponseSnapshot)
    assert snapshot.capture_generation == 17
    assert snapshot.response_code == int(response)
    assert snapshot.detail == 'terminal detail'
    assert getattr(snapshot, channel) == (kind, 'terminal detail')
    other_channel = ('manual_response'
                     if channel == 'save_response' else 'save_response')
    assert getattr(snapshot, other_channel) is None
    assert snapshot.is_recording is (
        response == ResponseType.RECORDING_STARTED)
    assert bridge.peek_save_response() is None
    assert bridge.peek_manual_recording_response() is None


def test_snapshot_is_read_only_repeated_and_detached(fake_bridge):
    bridge, layout, buffer = fake_bridge
    layout.capture_generation = 3
    layout.engine_response = ResponseType.ERROR_OCCURRED
    _set_engine_string(layout, 'first detail')
    before = bytes(buffer)
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=3)
    assert owner is not None

    first = bridge.read_terminal_response_snapshot(
        owner, process_exited=True)
    layout.engine_response = ResponseType.CLIP_SAVED
    _set_engine_string(layout, 'second detail')
    second = bridge.read_terminal_response_snapshot(
        owner, process_exited=True)

    assert first.detail == 'first detail'
    assert first.save_response == ('error', 'first detail')
    assert second.save_response == ('saved', 'second detail')
    assert layout.engine_response == ResponseType.CLIP_SAVED
    assert bytes(buffer) != before
    with pytest.raises(FrozenInstanceError):
        first.detail = 'changed'
    with pytest.raises(FrozenInstanceError):
        owner.capture_generation = 99


def test_snapshot_does_not_modify_layout_bytes(fake_bridge):
    bridge, layout, buffer = fake_bridge
    layout.capture_generation = 9
    layout.engine_response = ResponseType.CLIP_SAVED
    _set_engine_string(layout, 'saved')
    before = bytes(buffer)
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=9)
    snapshot = bridge.read_terminal_response_snapshot(
        owner, process_exited=True)

    assert snapshot is not None
    assert bytes(buffer) == before
    assert layout.engine_response == ResponseType.CLIP_SAVED


def test_retention_and_read_reject_invalid_exit_generation_and_owner(fake_bridge):
    bridge, layout, _buffer = fake_bridge
    layout.capture_generation = 4
    valid_owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=4)
    assert valid_owner is not None
    for generation in (True, False, -1, 0x1_0000_0000, 4.0, None, 3):
        assert bridge.retain_terminal_response_owner(
            expected_capture_generation=generation) is None
    for process_exited in (False, 1, None, 'true'):
        assert bridge.read_terminal_response_snapshot(
            valid_owner, process_exited=process_exited) is None
    assert bridge.read_terminal_response_snapshot(
        object(), process_exited=True) is None


def test_release_rejects_foreign_invalid_and_same_generation_remapped_owner(
        fake_bridge, monkeypatch):
    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr('core.capture_bridge.sys.platform', 'linux')
    layout.capture_generation = 32
    original_mapping = _CloseRecorder()
    bridge._linux_mmap = original_mapping
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=32)
    assert owner is not None

    assert bridge.release_terminal_response_owner(object()) is False
    foreign = _TerminalResponseOwner(
        object(), weakref.ref(layout), 32)
    assert bridge.release_terminal_response_owner(foreign) is False
    malformed = _TerminalResponseOwner(
        bridge, weakref.ref(layout), True)
    assert bridge.release_terminal_response_owner(malformed) is False

    replacement, _replacement_buffer = _make_fake_layout()
    replacement.capture_generation = 32
    replacement_mapping = _CloseRecorder()
    bridge._layout = replacement
    bridge._linux_mmap = replacement_mapping
    assert bridge.release_terminal_response_owner(owner) is False
    assert original_mapping.close_calls == 0
    assert replacement_mapping.close_calls == 0
    assert bridge._layout is replacement
    assert bridge._linux_mmap is replacement_mapping


def test_release_none_is_read_only_and_requires_complete_physical_absence(
        fake_bridge):
    bridge, layout, _buffer = fake_bridge
    singleton = CaptureBridge._instance
    assert bridge.release_terminal_response_owner(None) is False
    assert bridge._initialized is True
    assert bridge._layout is layout
    assert CaptureBridge._instance is singleton

    bridge._initialized = False
    bridge._layout = None
    bridge._linux_mmap = object()
    assert bridge.release_terminal_response_owner(None) is False
    assert bridge._linux_mmap is not None

    bridge._linux_mmap = None
    assert bridge.release_terminal_response_owner(None) is True
    assert CaptureBridge._instance is singleton


def test_release_linux_owner_closes_actual_mmap_without_buffer_error(
        fake_bridge, monkeypatch):
    bridge, _old_layout, _old_buffer = fake_bridge
    monkeypatch.setattr('core.capture_bridge.sys.platform', 'linux')
    mapping = mmap.mmap(-1, ctypes.sizeof(SharedMemoryLayout))
    layout = SharedMemoryLayout.from_buffer(mapping)
    layout.is_initialized = True
    layout.capture_generation = 37
    layout.ui_command = 12
    layout.ui_param1 = 101
    layout.engine_response = ResponseType.CLIP_SAVED
    bridge._layout = layout
    bridge._linux_mmap = mapping
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=37)
    assert owner is not None
    del layout

    assert bridge.release_terminal_response_owner(owner) is True
    assert mapping.closed
    assert bridge._layout is None
    assert bridge._linux_mmap is None
    assert bridge._initialized is False
    assert bridge.release_terminal_response_owner(owner) is False


def test_release_linux_close_failure_retains_original_resources(
        fake_bridge, monkeypatch):
    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr('core.capture_bridge.sys.platform', 'linux')
    mapping = _CloseRecorder(error=BufferError('exported view'))
    bridge._linux_mmap = mapping
    layout.capture_generation = 39
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=39)
    assert owner is not None

    assert bridge.release_terminal_response_owner(owner) is False
    assert mapping.close_calls == 1
    assert bridge._linux_mmap is mapping
    assert bridge._layout is layout
    assert bridge._initialized is True


def test_release_serializes_behind_mapping_lock(fake_bridge, monkeypatch):
    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr('core.capture_bridge.sys.platform', 'linux')
    layout.capture_generation = 38
    mapping = _CloseRecorder()
    bridge._linux_mmap = mapping
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=38)
    results = []
    started = threading.Event()

    def release_after_start():
        started.set()
        results.append(bridge.release_terminal_response_owner(owner))

    with bridge._mapping_lock:
        worker = threading.Thread(target=release_after_start, daemon=True)
        worker.start()
        assert started.wait(timeout=3)
        assert worker.is_alive()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert results == [True]
    assert mapping.close_calls == 1


def test_release_windows_unmap_failure_preserves_all_fields(
        fake_bridge, monkeypatch):
    import core.capture_bridge as capture_bridge_module

    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr(capture_bridge_module.sys, 'platform', 'win32')
    kernel32 = _Kernel32Recorder(unmap=False)
    monkeypatch.setattr(
        capture_bridge_module.ctypes, 'windll',
        SimpleNamespace(kernel32=kernel32), raising=False)
    owner = _prepare_windows_owner(bridge, layout)
    bridge._save_acknowledged_layout = layout
    before = (bridge._initialized, bridge._layout,
              bridge._win_map_ptr, bridge._mem_handle)

    assert bridge.release_terminal_response_owner(owner) is False
    assert (bridge._initialized, bridge._layout,
            bridge._win_map_ptr, bridge._mem_handle) == before
    assert bridge._save_acknowledged_layout is layout
    assert len(kernel32.unmap_calls) == 1
    assert kernel32.close_calls == []


def test_release_windows_close_failure_keeps_handle_after_unmap(
        fake_bridge, monkeypatch):
    import core.capture_bridge as capture_bridge_module

    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr(capture_bridge_module.sys, 'platform', 'win32')
    kernel32 = _Kernel32Recorder(unmap=True, close=False)
    monkeypatch.setattr(
        capture_bridge_module.ctypes, 'windll',
        SimpleNamespace(kernel32=kernel32), raising=False)
    owner = _prepare_windows_owner(bridge, layout)

    assert bridge.release_terminal_response_owner(owner) is False
    assert bridge._layout is None
    assert bridge._win_map_ptr is None
    assert bridge._mem_handle == 0x5678
    assert bridge._initialized is False
    assert len(kernel32.unmap_calls) == 1
    assert kernel32.close_calls == [0x5678]
    assert bridge.release_terminal_response_owner(None) is False


def test_release_windows_success_closes_original_resources_once(
        fake_bridge, monkeypatch):
    import core.capture_bridge as capture_bridge_module

    bridge, layout, _buffer = fake_bridge
    monkeypatch.setattr(capture_bridge_module.sys, 'platform', 'win32')
    kernel32 = _Kernel32Recorder()
    monkeypatch.setattr(
        capture_bridge_module.ctypes, 'windll',
        SimpleNamespace(kernel32=kernel32), raising=False)
    owner = _prepare_windows_owner(bridge, layout)

    assert bridge.release_terminal_response_owner(owner) is True
    assert bridge._layout is None
    assert bridge._win_map_ptr is None
    assert bridge._mem_handle is None
    assert bridge._initialized is False
    assert len(kernel32.unmap_calls) == 1
    assert kernel32.close_calls == [0x5678]

    replacement, _replacement_buffer = _make_fake_layout()
    replacement.capture_generation = 41
    bridge._initialized = True
    bridge._layout = replacement
    bridge._win_map_ptr = 0x9999
    bridge._mem_handle = 0xAAAA
    assert bridge.release_terminal_response_owner(owner) is False
    assert len(kernel32.unmap_calls) == 1
    assert kernel32.close_calls == [0x5678]


def test_read_rejects_wrong_bridge_closed_wrapper_none_or_remapped_layout(
        fake_bridge):
    bridge, layout, _buffer = fake_bridge
    layout.capture_generation = 5
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=5)
    assert owner is not None

    other = object.__new__(CaptureBridge)
    other._mapping_lock = threading.RLock()
    other._initialized = True
    other._layout = layout
    assert other.read_terminal_response_snapshot(
        owner, process_exited=True) is None

    bridge._initialized = False
    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True) is None
    bridge._initialized = True
    bridge._layout = None
    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True) is None

    replacement, replacement_buffer = _make_fake_layout()
    replacement.capture_generation = 5
    bridge._layout = replacement
    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True) is None
    assert replacement_buffer is not None

    bridge._layout = layout
    layout.capture_generation = 6
    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True) is None


def test_retained_read_does_not_call_live_connection_or_consume_apis(
        fake_bridge, monkeypatch):
    bridge, layout, _buffer = fake_bridge
    layout.capture_generation = 6
    layout.engine_response = ResponseType.CLIP_SAVED
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=6)
    assert owner is not None
    monkeypatch.setattr(
        bridge, 'is_connected',
        lambda: (_ for _ in ()).throw(AssertionError('must not call')))
    monkeypatch.setattr(
        bridge, 'consume_save_response',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('must not consume')))

    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True).save_response == ('saved', '')


def test_retained_read_serializes_behind_mapping_lock(fake_bridge):
    bridge, layout, _buffer = fake_bridge
    layout.capture_generation = 8
    layout.engine_response = ResponseType.CLIP_SAVED
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=8)
    results = []

    with bridge._mapping_lock:
        reader = threading.Thread(
            target=lambda: results.append(
                bridge.read_terminal_response_snapshot(
                    owner, process_exited=True)),
            daemon=True,
        )
        reader.start()
        assert reader.is_alive()
    reader.join(timeout=2)

    assert not reader.is_alive()
    assert results and results[0].save_response == ('saved', '')


def test_weak_owner_does_not_block_linux_mmap_shutdown(
        fake_bridge, monkeypatch):
    """The retained owner must not keep ctypes' mmap export alive."""
    import mmap

    import core.capture_bridge as capture_bridge_module

    bridge, _old_layout, _old_buffer = fake_bridge
    mapping = mmap.mmap(-1, ctypes.sizeof(SharedMemoryLayout))
    layout = SharedMemoryLayout.from_buffer(mapping)
    layout.is_initialized = True
    layout.capture_generation = 19
    layout.engine_response = ResponseType.CLIP_SAVED
    bridge._layout = layout
    bridge._linux_mmap = mapping
    bridge._initialized = True
    monkeypatch.setattr(capture_bridge_module.sys, 'platform', 'linux')

    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=19)
    assert owner is not None

    # The external ctypes view is gone before shutdown. The only retained
    # reference is the owner's weak reference, so mmap.close() can succeed.
    del layout
    bridge.shutdown()

    assert mapping.closed
    assert bridge._initialized is False
    assert bridge._linux_mmap is None
    assert bridge.read_terminal_response_snapshot(
        owner, process_exited=True) is None


def test_blocked_reader_observes_linux_shutdown_after_lock_release(
        fake_bridge, monkeypatch):
    """A reader blocked by shutdown must fail closed after the unmap."""
    import mmap

    import core.capture_bridge as capture_bridge_module

    bridge, _old_layout, _old_buffer = fake_bridge
    mapping = mmap.mmap(-1, ctypes.sizeof(SharedMemoryLayout))
    layout = SharedMemoryLayout.from_buffer(mapping)
    layout.is_initialized = True
    layout.capture_generation = 23
    layout.engine_response = ResponseType.CLIP_SAVED
    bridge._layout = layout
    bridge._linux_mmap = mapping
    bridge._initialized = True
    monkeypatch.setattr(capture_bridge_module.sys, 'platform', 'linux')
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=23)
    assert owner is not None
    del layout

    started = threading.Event()
    results = []
    errors = []

    def read_after_start():
        started.set()
        try:
            results.append(bridge.read_terminal_response_snapshot(
                owner, process_exited=True))
        except BaseException as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    reader = threading.Thread(target=read_after_start, daemon=True)
    try:
        with bridge._mapping_lock:
            reader.start()
            assert started.wait(timeout=3)
            bridge.shutdown()
            assert mapping.closed
        reader.join(timeout=5)
        assert not reader.is_alive()
    finally:
        reader.join(timeout=5)

    assert not reader.is_alive()
    assert not errors
    assert results == [None]
    assert bridge._initialized is False
    assert bridge._linux_mmap is None
    assert mapping.closed


def test_blocked_reader_rejects_locked_layout_remap_without_writing(
        fake_bridge):
    """A remapped owner cannot read or mutate the replacement layout."""
    bridge, layout, _old_buffer = fake_bridge
    layout.capture_generation = 29
    layout.engine_response = ResponseType.CLIP_SAVED
    owner = bridge.retain_terminal_response_owner(
        expected_capture_generation=29)
    assert owner is not None

    replacement, replacement_buffer = _make_fake_layout()
    replacement.capture_generation = 31
    replacement.engine_response = ResponseType.ERROR_OCCURRED
    _set_engine_string(replacement, 'replacement')
    replacement_before = bytes(replacement_buffer)
    started = threading.Event()
    results = []
    errors = []

    def read_after_start():
        started.set()
        try:
            results.append(bridge.read_terminal_response_snapshot(
                owner, process_exited=True))
        except BaseException as exc:  # pragma: no cover - failure detail
            errors.append(exc)

    reader = threading.Thread(target=read_after_start, daemon=True)
    with bridge._mapping_lock:
        reader.start()
        assert started.wait(timeout=3)
        bridge._layout = replacement
    reader.join(timeout=5)

    assert not reader.is_alive()
    assert not errors
    assert results == [None]
    assert bridge._layout is replacement
    assert bytes(replacement_buffer) == replacement_before
