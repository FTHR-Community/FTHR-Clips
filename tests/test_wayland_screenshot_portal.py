from __future__ import annotations

import os
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

import main
import core.wayland_screenshot_portal as portal_module
from core.wayland_screenshot_portal import (
    WaylandScreenshotPortal,
    local_file_path_from_portal_uri,
    predicted_request_path,
    validate_portal_png,
)


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_request_path_uses_sender_and_handle_token():
    assert predicted_request_path(':1.42', 'fthr_screenshot_abc') == (
        '/org/freedesktop/portal/desktop/request/1_42/'
        'fthr_screenshot_abc'
    )


@pytest.mark.parametrize('uri', [
    'https://example.test/screenshot.png',
    'file://remote-host/tmp/screenshot.png',
    'file:relative.png',
    'file:///tmp/screenshot.png?query=1',
    'file:///tmp/screenshot.png#fragment',
])
def test_portal_uri_must_be_an_absolute_local_file(uri):
    with pytest.raises(ValueError):
        local_file_path_from_portal_uri(uri)


def test_portal_png_validation_accepts_only_bounded_png(tmp_path):
    valid = tmp_path / 'portal.png'
    image = QImage(8, 6, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    assert image.save(str(valid), 'PNG')

    assert validate_portal_png(valid) == (8, 6)

    invalid = tmp_path / 'not-a-png.png'
    invalid.write_bytes(b'not an image')
    with pytest.raises(ValueError):
        validate_portal_png(invalid)


def test_portal_png_validation_rejects_symlinks_and_oversize(tmp_path):
    valid = tmp_path / 'portal.png'
    image = QImage(2, 2, QImage.Format.Format_RGB32)
    image.fill(0xFF000000)
    assert image.save(str(valid), 'PNG')

    link = tmp_path / 'link.png'
    try:
        link.symlink_to(valid)
    except (OSError, NotImplementedError):
        pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError):
        validate_portal_png(link)

    with pytest.raises(ValueError):
        validate_portal_png(valid, max_bytes=1)


def test_local_file_uri_decodes_percent_escaped_path(tmp_path):
    image_path = tmp_path / 'space screenshot.png'
    encoded = image_path.as_uri()

    assert local_file_path_from_portal_uri(encoded) == image_path


class _FakeWatcher:
    def __init__(self, reply):
        self._reply = reply
        self.finished = _FakeSignal()
        self.deleted = False
        self._finished = False

    def reply(self):
        return self._reply

    def deleteLater(self):
        self.deleted = True

    def isFinished(self):
        return self._finished

    def finish(self):
        self._finished = True
        self.finished.emit(self)


class _FakeSignal:
    def __init__(self):
        self.callback = None

    def connect(self, callback):
        self.callback = callback

    def emit(self, *args):
        if self.callback:
            self.callback(*args)


class _FakeReply:
    class MessageType:
        ErrorMessage = 'error'
        MethodReturnMessage = 'method-return'

    def __init__(self, request_path=''):
        self.request_path = request_path
        self.message_type = self.MessageType.MethodReturnMessage

    def type(self):
        return self.message_type

    def arguments(self):
        return [SimpleNamespace(path=lambda: self.request_path)]

    def errorMessage(self):
        return 'injected portal rejection'


class _FakeBus:
    def __init__(self):
        self.receiver = None
        self.signal_path = None
        self.calls = []
        self._connected = True

    def isConnected(self):
        return self._connected

    def baseService(self):
        return ':1.42'

    def connect(self, service, path, interface, signal, receiver, slot):
        self.signal_path = path
        self.receiver = receiver
        return True

    def disconnect(self, *args):
        self.receiver = None
        return True

    def asyncCall(self, message, timeout=0):
        self.calls.append((message, timeout))
        if isinstance(message, _FakeMessage):
            self.request_arguments = message.arguments
            return self.call_reply


class _FakeMessage:
    def __init__(self, service, path, interface, method):
        self.destination = service
        self.path = path
        self.interface = interface
        self.method = method
        self.arguments = []

    def setArguments(self, arguments):
        self.arguments = arguments


def _install_fake_dbus(monkeypatch, bus):
    module = ModuleType('PySide6.QtDBus')

    class FakeVariant:
        def __init__(self, value):
            self.value = value

        def variant(self):
            return self.value

    class FakeConnection:
        @staticmethod
        def sessionBus():
            return bus

    class FakePendingCallWatcher(_FakeWatcher):
        def __init__(self, call, _parent):
            super().__init__(call)
            bus.watcher = self
            self._finished = bool(getattr(bus, 'finished_before_connect', False))

    class FakeMessage:
        MessageType = _FakeReply.MessageType

        @staticmethod
        def createMethodCall(service, path, interface, method):
            return _FakeMessage(service, path, interface, method)

    module.QDBusConnection = FakeConnection
    module.QDBusPendingCallWatcher = FakePendingCallWatcher
    module.QDBusVariant = FakeVariant
    module.QDBusMessage = FakeMessage
    monkeypatch.setitem(sys.modules, 'PySide6.QtDBus', module)


def _start_fake_portal(monkeypatch, tmp_path, *, finished_before_connect=False):
    bus = _FakeBus()
    bus.finished_before_connect = finished_before_connect
    reply = _FakeReply()
    reply.request_path = predicted_request_path(':1.42', 'fthr_screenshot_test')
    bus.call_reply = reply
    monkeypatch.setattr(
        portal_module.uuid, 'uuid4',
        lambda: SimpleNamespace(hex='test'))
    _install_fake_dbus(monkeypatch, bus)
    staged = tmp_path / 'staged.png'
    staged.touch()
    completed = []
    portal = WaylandScreenshotPortal(
        staged, lambda success, detail: completed.append((success, detail)))
    portal.start()
    return portal, bus, staged, completed


def test_portal_subscribes_before_async_call_and_handles_early_cancel(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)

    assert bus.signal_path == bus.call_reply.request_path
    assert bus.calls[0][0].method == 'Screenshot'
    assert bus.calls[0][0].path == '/org/freedesktop/portal/desktop'
    assert bus.request_arguments[0] == ''
    assert {key: value.value for key, value in bus.request_arguments[1].items()} == {
        'handle_token': 'fthr_screenshot_test',
        'interactive': True,
        'modal': False,
    }

    bus.receiver.on_response(1, {})
    assert completed == []
    bus.watcher.finish()

    assert completed == [(False, 'Screenshot was canceled in the compositor dialog.')]
    assert not staged.exists()
    bus.receiver = None
    portal.close()


def test_portal_handles_method_reply_already_finished_when_watcher_connects(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(
        monkeypatch, tmp_path, finished_before_connect=True)
    bus.receiver.on_response(1, {})
    loop = QEventLoop()
    QTimer.singleShot(100, loop.quit)
    loop.exec()

    assert completed == [(False, 'Screenshot was canceled in the compositor dialog.')]
    assert not staged.exists()
    portal.close()


def test_portal_imports_valid_response_after_method_reply_once(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    original = tmp_path / 'compositor screenshot.png'
    image = QImage(32, 18, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    assert image.save(str(original), 'PNG')

    bus.watcher.finish()
    bus.receiver.on_response(0, {'uri': original.as_uri()})
    bus.receiver.on_response(0, {'uri': 'file:///tmp/other.png'})
    loop = QEventLoop()
    portal._worker.finished.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec()

    assert completed == [(True, '')]
    assert validate_portal_png(staged) == (32, 18)
    assert original.exists()
    worker = portal._worker
    assert worker is not None
    assert worker.wait(3000)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with portal_module._ACTIVE_IMAGE_WORKERS_LOCK:
            retained = worker in portal_module._ACTIVE_IMAGE_WORKERS
        if not retained:
            break
        qapp.processEvents()
        time.sleep(0.01)
    assert not retained
    portal.close()


def test_portal_close_cancels_blocked_real_image_worker(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    original = tmp_path / 'compositor screenshot.png'
    image = QImage(32, 18, QImage.Format.Format_RGBA8888)
    image.fill(0xFF336699)
    assert image.save(str(original), 'PNG')
    entered_validation = threading.Event()
    release_validation = threading.Event()
    real_validate = portal_module.validate_portal_png

    def blocked_validation(path, **kwargs):
        entered_validation.set()
        release_validation.wait()
        return real_validate(path, **kwargs)

    monkeypatch.setattr(portal_module, 'validate_portal_png', blocked_validation)
    bus.watcher.finish()
    bus.receiver.on_response(0, {'uri': original.as_uri()})
    worker = portal._worker
    assert worker is not None
    try:
        assert entered_validation.wait(3), 'image worker never entered validation'
        with portal_module._ACTIVE_IMAGE_WORKERS_LOCK:
            assert worker in portal_module._ACTIVE_IMAGE_WORKERS

        portal.close()
        assert not staged.exists()
        assert original.exists()

        release_validation.set()
        assert worker.wait(3000)
        qapp.processEvents()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with portal_module._ACTIVE_IMAGE_WORKERS_LOCK:
                retained = worker in portal_module._ACTIVE_IMAGE_WORKERS
            if not retained:
                break
            qapp.processEvents()
            time.sleep(0.01)

        assert not retained
        assert completed == []
        assert original.exists()
        assert not staged.exists()
        assert list(tmp_path.iterdir()) == [original]
    finally:
        release_validation.set()
        if worker.isRunning():
            worker.wait(3000)


def test_portal_timeout_closes_request_and_ignores_late_response(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    bus.watcher.finish()
    receiver = bus.receiver

    portal._on_response_timeout()
    assert completed == [(False, 'The compositor screenshot dialog timed out.')]
    close_message, close_timeout = bus.calls[-1]
    assert close_message.destination == 'org.freedesktop.portal.Desktop'
    assert close_message.path == bus.call_reply.request_path
    assert close_message.interface == 'org.freedesktop.portal.Request'
    assert close_message.method == 'Close'
    assert close_timeout == 1000
    assert not staged.exists()
    receiver.on_response(0, {'uri': 'file:///tmp/late.png'})
    assert len(completed) == 1
    portal.close()


def test_portal_rejection_and_invalid_uri_clean_staging(monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    bus.call_reply.message_type = _FakeReply.MessageType.ErrorMessage
    bus.watcher.finish()
    assert completed == [(False, 'Screenshot portal request failed: injected portal rejection')]
    assert not staged.exists()
    portal.close()

    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    receiver = bus.receiver
    bus.watcher.finish()
    receiver.on_response(0, {'uri': 'https://example.test/not-a-local-image.png'})
    assert completed == [(
        False,
        'Invalid screenshot portal result: The screenshot portal returned a non-local file URI.',
    )]
    assert not staged.exists()
    portal.close()


def test_portal_shutdown_cancels_callbacks_and_owned_stage(
        monkeypatch, tmp_path, qapp):
    portal, bus, staged, completed = _start_fake_portal(monkeypatch, tmp_path)
    receiver = bus.receiver

    portal.close()
    receiver.on_response(1, {})

    assert completed == []
    assert not staged.exists()


def test_wayland_without_grim_offers_portal_without_qscreen_fallback(
        monkeypatch, tmp_path):
    calls = []
    paths = SimpleNamespace(staged=tmp_path / 'stage.png')
    paths.staged.touch()
    window = SimpleNamespace(
        _screenshot_inflight=False,
        _screenshot_pending_requests=0,
        _shutdown_requested=False,
        _capture_config=SimpleNamespace(active=SimpleNamespace(monitor='DP-1')),
        settings_manager=SimpleNamespace(get=lambda _key, default='': default),
    )
    window._resolve_screenshot_screen = lambda _monitor: None
    window._offer_wayland_portal_screenshot = (
        lambda _paths, reason: calls.append(('portal', reason)))
    window._capture_selected_qt_screen = lambda *_args: pytest.fail(
        'Wayland must not fall back to QScreen')
    monkeypatch.setattr(main, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(main, 'clips_directory_from', lambda _settings: tmp_path)
    monkeypatch.setattr(main, 'reserve_screenshot_paths', lambda _directory: paths)
    monkeypatch.setattr(main.linux_tools, 'path', lambda _tool: None)
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')

    main.MainWindow._on_hotkey_save_screenshot(window)

    assert calls == [('portal', 'grim is not installed or could not be found.')]


def test_x11_screenshot_still_uses_selected_qscreen(monkeypatch, tmp_path):
    calls = []
    paths = SimpleNamespace(staged=tmp_path / 'stage.png')
    paths.staged.touch()
    screen = object()
    window = SimpleNamespace(
        _screenshot_inflight=False,
        _screenshot_pending_requests=0,
        _shutdown_requested=False,
        _capture_config=SimpleNamespace(active=SimpleNamespace(monitor='DP-1')),
        settings_manager=SimpleNamespace(get=lambda _key, default='': default),
    )
    window._resolve_screenshot_screen = lambda _monitor: screen
    window._capture_selected_qt_screen = (
        lambda _paths, _monitor: calls.append('qscreen'))
    window._offer_wayland_portal_screenshot = lambda *_args: pytest.fail(
        'X11 should not invoke the Wayland portal')
    monkeypatch.setattr(main, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setattr(main, 'clips_directory_from', lambda _settings: tmp_path)
    monkeypatch.setattr(main, 'reserve_screenshot_paths', lambda _directory: paths)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)

    main.MainWindow._on_hotkey_save_screenshot(window)

    assert calls == ['qscreen']


def test_grim_failure_never_falls_back_to_qscreen(tmp_path):
    class Process:
        def property(self, _name):
            return False

        def readAllStandardError(self):
            return b'wlr-screencopy unsupported'

        def deleteLater(self):
            return None

    paths = SimpleNamespace(staged=tmp_path / 'stage.png')
    paths.staged.touch()
    process = Process()
    window = SimpleNamespace(
        _screenshot_capture_process=process,
        _shutdown_requested=False,
    )
    window._offer_wayland_portal_screenshot = (
        lambda _paths, detail: setattr(window, 'portal_detail', detail))
    window._capture_selected_qt_screen = lambda *_args: pytest.fail(
        'A failed Wayland grim capture must not use QScreen')

    main.MainWindow._on_grim_finished(
        window, process, paths, 'DP-1', 1, None)

    assert window.portal_detail == 'wlr-screencopy unsupported'


@pytest.mark.linux_only
@pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('FTHR_TEST_PRIVATE_DBUS') != '1',
    reason='run explicitly inside dbus-run-session',
)
def test_real_qtdbus_portal_response_before_method_reply(tmp_path, qapp):
    from PySide6.QtCore import ClassInfo, QObject, Signal, Slot
    from PySide6.QtDBus import (
        QDBusAbstractAdaptor,
        QDBusConnection,
        QDBusObjectPath,
        QDBusVariant,
    )

    bus = QDBusConnection.sessionBus()
    assert bus.isConnected()
    assert bus.registerService(portal_module.PORTAL_SERVICE)
    source = tmp_path / 'desktop-selected.png'
    image = QImage(24, 16, QImage.Format.Format_RGBA8888)
    image.fill(0xFF553377)
    assert image.save(str(source), 'PNG')
    class RequestObject(QObject):
        pass

    @ClassInfo({'D-Bus Interface': portal_module.REQUEST_INTERFACE})
    class RequestAdaptor(QDBusAbstractAdaptor):
        Response = Signal('uint', 'QVariantMap')

        @Slot()
        def Close(self):
            return None

    @ClassInfo({'D-Bus Interface': portal_module.SCREENSHOT_INTERFACE})
    class PortalAdaptor(QDBusAbstractAdaptor):
        @Slot(str, 'QVariantMap', result=QDBusObjectPath)
        def Screenshot(self, parent_window, options):
            assert parent_window == ''
            assert isinstance(options, dict)
            assert isinstance(options['interactive'], QDBusVariant)
            assert options['interactive'].variant() is True
            assert options['modal'].variant() is False
            token = options['handle_token'].variant()
            request_path = predicted_request_path(bus.baseService(), token)
            request = RequestObject()
            RequestAdaptor(request)
            assert bus.registerObject(
                request_path,
                request,
                QDBusConnection.RegisterOption.ExportAdaptors,
            )
            requests.append((request_path, request))
            # Emit before replying with the object path to exercise the early
            # response race using actual QtDBus and a private bus.
            request.findChild(RequestAdaptor).Response.emit(
                0, {'uri': source.as_uri()})
            return QDBusObjectPath(request_path)

    class PortalObject(QObject):
        pass

    requests = []
    portal_object = PortalObject()
    PortalAdaptor(portal_object)
    assert bus.registerObject(
        portal_module.PORTAL_PATH,
        portal_object,
        QDBusConnection.RegisterOption.ExportAdaptors,
    )
    staged = tmp_path / 'published-stage.png'
    staged.touch()
    completed = []
    loop = QEventLoop()

    def on_completed(success, detail):
        completed.append((success, detail))
        loop.quit()

    client = WaylandScreenshotPortal(staged, on_completed)
    client.start()
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(loop.quit)
    deadline.start(5000)
    if not completed:
        loop.exec()
    client.close()
    bus.unregisterObject(portal_module.PORTAL_PATH)
    for request_path, _request in requests:
        bus.unregisterObject(request_path)
    bus.unregisterService(portal_module.PORTAL_SERVICE)

    assert completed == [(True, '')]
    assert validate_portal_png(staged) == (24, 16)
