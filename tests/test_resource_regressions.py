"""Resource changes must preserve media bytes and reject retired worker results."""
from collections import deque
from contextlib import nullcontext
import hashlib
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from core import camera_recorder, encoder_capabilities, focus_monitor, game_detector, mp4_timing


def _game(hwnd):
    return game_detector.GameWindow(
        hwnd=hwnd, pid=202, title='Example Game', exe_path=r'C:\Games\game.exe',
        exe_name='game.exe', class_name='GameWindow', is_known_game=True,
        right=1920, bottom=1080)


def test_detector_reuses_blocked_worker_and_rejects_old_result(qtbot):
    entered, release, queried_again = (threading.Event() for _ in range(3))
    calls = []

    def provider(_pid):
        calls.append(threading.get_ident())
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return _game(101)
        queried_again.set()
        return _game(102)

    detector = game_detector.ForegroundGameDetector(window_provider=provider, stable_polls=1)
    detected = []
    detector.game_detected.connect(detected.append)
    try:
        assert detector.start()
        qtbot.waitUntil(entered.is_set)
        worker, old_token = detector._thread, detector._stop_event
        detector.stop()
        assert worker.is_alive()
        assert detector._thread is worker
        assert not detector.is_running
        assert detector.start()
        assert detector._thread is worker
        assert old_token.is_set() and old_token is not detector._stop_event
        release.set()
        qtbot.waitUntil(queried_again.is_set)
        qtbot.waitUntil(lambda: bool(detected))
        assert [window.hwnd for window in detected] == [102]
        assert len(set(calls)) == 1
    finally:
        release.set()
        detector.stop()


@pytest.mark.parametrize('stop_first', [False, True])
def test_detector_discards_notification_queued_before_restart(qtbot, qapp, stop_first):
    detector = game_detector.ForegroundGameDetector(window_provider=lambda _pid: None)
    detected = []
    detector.game_detected.connect(detected.append)
    worker = threading.Thread(target=lambda: detector._observe(_game(101)))
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    if stop_first:
        detector.stop()
    try:
        assert detector.start()
        qapp.processEvents()
        assert detected == []
    finally:
        detector.stop()


@pytest.mark.parametrize('now', [0.0, 120.0])
def test_game_database_first_load_ignores_uptime_ttl(monkeypatch, now):
    opened = []
    path = r'C:\Games\registered.exe'

    def open_key(parent, name):
        opened.append(name)
        return nullcontext(name)

    def enum_key(_root, index):
        if index:
            raise OSError('end of enumeration')
        return 'entry'

    monkeypatch.setitem(sys.modules, 'winreg', SimpleNamespace(
        HKEY_CURRENT_USER=object(), OpenKey=open_key, EnumKey=enum_key,
        QueryValueEx=lambda _key, name: (path if name == 'MatchedExeFullPath' else '', 1)))
    monkeypatch.setattr(game_detector, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(game_detector, 'time', SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(game_detector, '_GAME_DATABASE_LOADED_AT', None)
    monkeypatch.setattr(game_detector, '_GAME_DATABASE_PATHS', set())
    monkeypatch.setattr(game_detector, '_GAME_DATABASE_PARENTS', set())
    assert game_detector._is_windows_known_game(path)
    assert game_detector._is_windows_known_game(path)
    assert len(opened) == 2  # one root and one entry, not another refresh


@pytest.mark.parametrize('restart', [False, True])
def test_focus_discards_result_after_stop_or_restart(monkeypatch, qtbot, restart):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def title():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return 'Discord'
        return 'Minecraft'

    monkeypatch.setattr(focus_monitor, '_get_active_window_title', title)
    monitor = focus_monitor.FocusMonitor('Minecraft')
    lost = []
    monitor.focus_lost.connect(lambda: lost.append(True))
    try:
        monitor.start()
        monitor._start_poll()
        qtbot.waitUntil(entered.is_set)
        monitor.stop()
        if restart:
            monitor.start()
            monitor._start_poll()
            assert len(calls) == 1
        release.set()
        qtbot.waitUntil(lambda: not monitor._poll_running)
        assert lost == []
        assert len(calls) == (2 if restart else 1)
    finally:
        release.set()
        monitor.stop()


def test_focus_discards_result_for_previous_target(monkeypatch, qtbot):
    jobs = []

    class Thread:
        def __init__(self, *, target, **_kwargs):
            jobs.append(target)

        def start(self):
            pass

    monkeypatch.setattr(focus_monitor, 'threading', SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(focus_monitor, '_get_active_window_title', lambda: 'Old Game')
    monitor = focus_monitor.FocusMonitor('Old Game')
    lost = []
    monitor.focus_lost.connect(lambda: lost.append(True))
    try:
        monitor.start()
        monitor._start_poll()
        monitor.set_target('New Game')
        jobs[0]()
        assert lost == []
        assert len(jobs) == 2
        monkeypatch.setattr(focus_monitor, '_get_active_window_title', lambda: 'New Game')
        jobs[1]()
        assert not monitor._poll_running
        assert lost == []
    finally:
        monitor.stop()


@pytest.mark.parametrize('writer_open,write_error', [(True, False), (False, False), (True, True)])
def test_camera_writes_incrementally_and_always_releases(monkeypatch, tmp_path,
                                                        writer_open, write_error):
    monkeypatch.setattr(camera_recorder.CameraRecorder, '_instance', None)
    recorder = camera_recorder.CameraRecorder()
    recorder._running = True
    recorder._chunks = deque([(float(i), i) for i in range(6)])
    events = []

    class Writer:
        released = False

        def isOpened(self):
            return writer_open

        def write(self, frame):
            value = int(frame[0, 0, 0])
            events.append(('write', value))
            if write_error:
                raise OSError('write failed')
            # The selected JPEG snapshot must survive a capture stop/restart.
            recorder._chunks.clear()
            recorder._running = False
            recorder._generation += 1

        def release(self):
            self.released = True

    writer = Writer()

    def decode(value, _mode):
        events.append(('decode', value))
        if value == 0:
            return None
        return np.full((3 if value == 2 else 2, 2, 3), value, dtype=np.uint8)

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder, '_cv2', SimpleNamespace(
        IMREAD_COLOR=1, imdecode=decode, VideoWriter_fourcc=lambda *_args: 0,
        VideoWriter=lambda *_args: writer))
    if write_error:
        with pytest.raises(OSError, match='write failed'):
            recorder.write_segment(str(tmp_path / 'camera.mp4'), 5, 5)
    else:
        assert recorder.write_segment(str(tmp_path / 'camera.mp4'), 5, 5) is writer_open
    assert writer.released
    assert events[:2] == [('decode', 0), ('decode', 1)]
    if writer_open:
        assert events[2] == ('write', 1)
        if not write_error:
            assert [value for event, value in events if event == 'write'] == [1, 3, 4, 5]
    else:
        assert len(events) == 2


def test_camera_streamed_file_matches_original_writer(monkeypatch, tmp_path):
    cv2 = pytest.importorskip('cv2')
    monkeypatch.setattr(camera_recorder.CameraRecorder, '_instance', None)
    recorder = camera_recorder.CameraRecorder()
    recorder._running = True
    for index in range(12):
        frame = np.full((48, 64, 3), index * 19, dtype=np.uint8)
        ok, encoded = cv2.imencode('.jpg', frame)
        assert ok
        recorder._chunks.append((float(index), encoded))
    original, streamed = tmp_path / 'original.mp4', tmp_path / 'streamed.mp4'
    writer = cv2.VideoWriter(str(original), cv2.VideoWriter_fourcc(*'mp4v'), 30, (64, 48))
    if not writer.isOpened():
        writer.release()
        pytest.skip('OpenCV MPEG-4 writer unavailable')
    try:
        for frame in recorder.extract_segment(11, 11):
            writer.write(frame)
    finally:
        writer.release()
    assert recorder.write_segment(str(streamed), 11, 11, fps=30)
    assert streamed.read_bytes() == original.read_bytes()


def test_native_signatures_stream_dlls_without_read_bytes(monkeypatch, tmp_path):
    engine = tmp_path / 'FTHRClips.exe'
    engine.write_bytes(encoder_capabilities._PROBE_MARKER)
    expected = {}
    for index, name in enumerate(encoder_capabilities._REQUIRED_NATIVE_DLLS):
        payload = bytes([index]) * (1024 * 1024 + 17)
        (tmp_path / name).write_bytes(payload)
        expected[name] = hashlib.sha256(payload).hexdigest()
    path_type = type(engine)
    read_bytes = path_type.read_bytes

    def guarded_read(path):
        assert path.suffix != '.dll', 'DLL unexpectedly loaded in full'
        return read_bytes(path)

    monkeypatch.setattr(path_type, 'read_bytes', guarded_read)
    signature = encoder_capabilities._native_signature(str(tmp_path / 'ffmpeg.exe'), str(engine))
    assert signature is not None
    assert {name: signature['sha256'][name] for name in expected} == expected


@pytest.mark.parametrize('content', [None, 'invalid json', '[]', '{}', '{"capabilities": null}'])
def test_unusable_capability_cache_never_hashes_binaries(monkeypatch, tmp_path, content):
    cache = tmp_path / 'capabilities.json'
    if content is not None:
        cache.write_text(content)

    def unexpected(*_args):
        pytest.fail('unusable cache must not start binary or device signature work')

    monkeypatch.setattr(encoder_capabilities, '_ffmpeg_signature', unexpected)
    assert encoder_capabilities.load_cached_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), cache_file=cache) is None


def test_nested_mp4_payloads_share_one_immutable_buffer():
    import struct
    leaf = struct.pack('>I4s', 12, b'free') + b'data'
    data = struct.pack('>I4s', len(leaf) + 8, b'moov') + leaf
    root = mp4_timing._parse_boxes(data)[0]
    child = root.children[0]
    assert isinstance(root.payload, memoryview)
    assert root.payload.obj is data and child.payload.obj is data
    assert root.payload.readonly and child.payload.readonly
    assert mp4_timing._box_bytes(root) == data


@pytest.mark.parametrize('readonly_view', [False, True])
def test_mp4_parser_preserves_snapshot_of_mutable_input(readonly_view):
    import struct
    data = bytearray(struct.pack('>I4s', 12, b'free') + b'data')
    original = bytes(data)
    parsed = mp4_timing._parse_boxes(memoryview(data).toreadonly() if readonly_view else data)
    data[-1] = ord('x')
    assert mp4_timing._box_bytes(parsed[0]) == original
