from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject

from core import window_preview as preview

_TEST_BACKENDS = []


@pytest.fixture(autouse=True)
def _qt_runtime(qapp):
    """Use pytest-qt's shared QApplication and clean every backend per test."""
    _TEST_BACKENDS.clear()
    yield
    for backend in list(_TEST_BACKENDS):
        if isinstance(backend, preview.LinuxX11PreviewBackend):
            backend.dispose()
            process = backend._process
            if process is not None:
                process.state_value = preview.QProcess.ProcessState.NotRunning
                process.finished.emit(0, preview.QProcess.ExitStatus.CrashExit)
        else:
            backend.dispose()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    _TEST_BACKENDS.clear()


def track(backend):
    _TEST_BACKENDS.append(backend)
    return backend


class FakeDwmApi:
    def __init__(self):
        self.valid = {11, 22}
        self.minimized = set()
        self.sizes = {101: (1600, 900)}
        self.pid = {11: 99}
        self.registrations = []
        self.updates = []
        self.unregistered = []
        self.fail_source_size = False

    def is_window(self, hwnd):
        return hwnd in self.valid

    def is_iconic(self, hwnd):
        return hwnd in self.minimized

    def process_id(self, hwnd):
        return self.pid.get(hwnd)

    def register(self, destination, source):
        self.registrations.append((destination, source))
        return 101 + len(self.registrations) - 1

    def source_size(self, thumbnail):
        if self.fail_source_size:
            raise OSError("source size failure")
        return self.sizes.get(thumbnail, (1600, 900))

    def update(self, thumbnail, rect, visible, client_only=True):
        self.updates.append((thumbnail, rect, visible, client_only))

    def unregister(self, thumbnail):
        self.unregistered.append(thumbnail)


def test_dwm_struct_layout_is_fixed_width_and_hresult_is_signed():
    assert preview.ctypes.sizeof(preview._DwmSize) == 8
    assert preview.ctypes.sizeof(preview._DwmThumbnailProperties) == 48
    assert preview._DwmThumbnailProperties.fVisible.offset == 40
    assert preview._DwmThumbnailProperties.fSourceClientAreaOnly.offset == 44


def test_letterbox_centers_source_aspect_inside_physical_client_rect():
    assert preview._letterbox((1600, 900), (10, 20, 410, 320)) == (10, 57, 410, 282)
    assert preview._letterbox((900, 1600), (0, 0, 400, 300)) == (115, 0, 284, 300)


def test_dwm_registers_exact_source_and_releases_thumbnail(monkeypatch):
    monkeypatch.setattr(preview, "_preview_is_headless", lambda: False)
    api = FakeDwmApi()
    backend = track(preview.WindowsDwmPreviewBackend(native_api=api, poll_interval_ms=10000))
    statuses = []
    backend.status_changed.connect(statuses.append)

    backend.start({"hwnd": 11, "title": "untrusted title"}, 22, (0, 0, 400, 300))

    assert api.registrations == [(22, 11)]
    assert api.updates == [(101, (0, 37, 400, 262), True, False)]
    assert statuses[-1] == "Live window preview"
    backend.stop()
    assert api.unregistered == [101]


def test_dwm_geometry_change_and_duplicate_process_entries_stay_window_specific(monkeypatch):
    monkeypatch.setattr(preview, "_preview_is_headless", lambda: False)
    api = FakeDwmApi()
    api.valid.add(12)
    api.pid[12] = 99  # Same process, distinct top-level HWND.
    backend = track(preview.WindowsDwmPreviewBackend(native_api=api, poll_interval_ms=10000))
    backend.start({"hwnd": 12}, 22, (0, 0, 400, 300))
    backend.update_geometry(22, (20, 30, 220, 130))
    assert api.registrations == [(22, 12)]
    assert api.updates[-1][1] == (31, 30, 209, 130)
    backend.update_geometry(33, (0, 0, 400, 300))
    assert api.registrations[-1] == (33, 12)
    assert api.unregistered == [101]
    backend.stop()
    assert api.unregistered == [101, 102]


def test_dwm_minimized_closed_and_native_failures_clear_preview(monkeypatch):
    monkeypatch.setattr(preview, "_preview_is_headless", lambda: False)
    api = FakeDwmApi()
    backend = track(preview.WindowsDwmPreviewBackend(native_api=api, poll_interval_ms=10000))
    statuses = []
    backend.status_changed.connect(statuses.append)
    api.minimized.add(11)
    backend.start({"hwnd": 11}, 22, (0, 0, 400, 300))
    assert not api.registrations
    assert "minimized" in statuses[-1]
    api.minimized.clear()
    backend.start({"hwnd": 11}, 22, (0, 0, 400, 300))
    api.valid.remove(11)
    backend._poll()
    assert api.unregistered == [101]
    assert "closed" in statuses[-1]
    api.valid.add(11)
    api.fail_source_size = True
    backend.start({"hwnd": 11}, 22, (0, 0, 400, 300))
    assert api.unregistered[-1] == 102
    assert "source size failure" in statuses[-1]


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in list(self.callbacks):
            callback(*args)


class FakeProcess(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.readyReadStandardOutput = FakeSignal()
        self.readyReadStandardError = FakeSignal()
        self.started = FakeSignal()
        self.finished = FakeSignal()
        self.errorOccurred = FakeSignal()
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.start_args = None
        self.state_value = preview.QProcess.ProcessState.NotRunning
        self.terminated = False
        self.killed = False

    def setProcessChannelMode(self, _mode):
        return

    def start(self, program, args, _mode):
        self.start_args = (program, list(args))
        self.state_value = preview.QProcess.ProcessState.Running
        self.started.emit()

    def readAllStandardOutput(self):
        data = bytes(self.stdout)
        self.stdout.clear()
        return data

    def readAllStandardError(self):
        data = bytes(self.stderr)
        self.stderr.clear()
        return data

    def state(self):
        return self.state_value

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.state_value = preview.QProcess.ProcessState.NotRunning

    def errorString(self):
        return "fake process error"


def make_x11_backend(monkeypatch, environment=None):
    monkeypatch.setattr(preview, "_preview_is_headless", lambda: False)
    processes = []

    def factory(parent):
        process = FakeProcess(parent)
        processes.append(process)
        return process

    backend = track(preview.LinuxX11PreviewBackend(
        process_factory=factory,
        ffmpeg_resolver=lambda: "C:/fthr/ffmpeg.exe",
        environment={"DISPLAY": ":3", **(environment or {})},
    ))
    monkeypatch.setattr(backend, "_native_x11", lambda: True)
    return backend, processes


def test_x11_uses_exact_selected_xid_shell_free_no_audio_low_rate_command(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    backend.start({"hwnd": 0x1234}, 55, (0, 0, 100, 50))
    assert len(processes) == 1
    program, args = processes[0].start_args
    assert program == "C:/fthr/ffmpeg.exe"
    assert args[args.index("-window_id") + 1] == str(0x1234)
    assert args[args.index("-framerate") + 1] == "2"
    assert "x11grab" in args
    assert "-nostdin" in args
    assert "-window_id" in args
    assert "-f" in args and "rawvideo" in args and "pipe:1" in args
    assert "-i" in args and ":3" in args
    input_index = args.index("-i")
    assert args[args.index("-probesize") + 1] == "32"
    assert args.index("-probesize") < input_index
    assert args[args.index("-analyzeduration") + 1] == "0"
    assert args.index("-analyzeduration") < input_index
    assert not any("audio" in arg.casefold() for arg in args)
    assert "-an" in args
    assert args[args.index("-threads") + 1] == "1"
    assert args[args.index("-filter_threads") + 1] == "1"
    assert args.count("-window_id") == 1
    assert "scale=320:180:force_original_aspect_ratio=decrease" in args[args.index("-vf") + 1]


def test_x11_rejects_invalid_ids_and_wayland_before_start(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    statuses = []
    backend.status_changed.connect(statuses.append)
    backend.start({"hwnd": "not-an-id"}, 55, (0, 0, 100, 50))
    assert not processes
    assert "unavailable" in statuses[-1]
    backend2, processes2 = make_x11_backend(monkeypatch, {"WAYLAND_DISPLAY": "wayland-1"})
    monkeypatch.setattr(backend2, "_native_x11", preview.LinuxX11PreviewBackend._native_x11.__get__(backend2))
    backend2.start({"hwnd": 123}, 55, (0, 0, 100, 50))
    assert not processes2


def test_x11_latest_complete_frame_emitted_and_stderr_and_tail_bounded(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    images = []
    backend.image_ready.connect(images.append)
    backend.start({"hwnd": 123}, 55, (0, 0, 100, 50))
    process = processes[0]
    frame_size = 320 * 180 * 3
    process.stdout.extend(b"a" * (frame_size + 19))
    process.readyReadStandardOutput.emit()
    assert len(images) == 1
    assert images[0].size().width() == 320
    assert images[0].size().height() == 180
    process.stderr.extend(b"x" * (preview._MAX_STDERR_BYTES + 300))
    process.readyReadStandardError.emit()
    assert len(backend._stderr) == preview._MAX_STDERR_BYTES
    process.stdout.extend(b"b" * 19 + b"c" * (preview._FRAME_BYTES * 5) + b"d" * 12)
    process.readyReadStandardOutput.emit()
    assert len(backend._buffer) < preview._FRAME_BYTES


def test_x11_switch_terminates_old_process_and_caps_pending_selection(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    backend.start({"hwnd": 1}, 55, (0, 0, 100, 50))
    first = processes[0]
    backend.start({"hwnd": 2}, 55, (0, 0, 100, 50))
    backend.start({"hwnd": 3}, 55, (0, 0, 100, 50))
    assert first.terminated
    assert len(processes) == 1
    assert backend._pending[0] == 3
    backend._frame_watchdog_expired()
    assert first.terminated
    backend._force_kill()
    assert first.killed


def test_x11_drops_old_selection_frames_then_reaps_before_latest_launch(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    images = []
    backend.image_ready.connect(images.append)
    backend.start({"hwnd": 1}, 55, (0, 0, 100, 50))
    old = processes[0]
    backend.start({"hwnd": 2}, 55, (0, 0, 100, 50))
    old.stdout.extend(b"x" * preview._FRAME_BYTES)
    old.readyReadStandardOutput.emit()
    assert not images
    old.state_value = preview.QProcess.ProcessState.NotRunning
    old.finished.emit(0, preview.QProcess.ExitStatus.NormalExit)
    assert len(processes) == 2
    assert processes[1].start_args[1][
        processes[1].start_args[1].index("-window_id") + 1
    ] == "2"


def test_dispose_detaches_and_kills_async_then_deletes_after_process_finished(monkeypatch):
    backend, processes = make_x11_backend(monkeypatch)
    owner = QObject()
    backend.setParent(owner)
    destroyed = []
    backend.destroyed.connect(lambda *_: destroyed.append(True))
    backend.start({"hwnd": 7}, 55, (0, 0, 100, 50))
    process = processes[0]

    backend.dispose()
    assert backend.parent() is QCoreApplication.instance()
    assert process.killed
    backend.start({"hwnd": 8}, 55, (0, 0, 100, 50))
    assert len(processes) == 1
    assert not destroyed

    process.finished.emit(0, preview.QProcess.ExitStatus.CrashExit)
    assert backend._process is None
    assert not destroyed  # deleteLater remains asynchronous
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert destroyed == [True]
