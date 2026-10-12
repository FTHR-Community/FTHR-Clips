"""Asynchronous XDG Screenshot portal support for Wayland desktops.

The compositor owns screenshot source selection.  This deliberately does not
pretend the result came from FTHR's replay-monitor setting.
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QImageReader


_ACTIVE_IMAGE_WORKERS: set['_PortalImageWorker'] = set()
_ACTIVE_IMAGE_WORKERS_LOCK = threading.Lock()


PORTAL_SERVICE = 'org.freedesktop.portal.Desktop'
PORTAL_PATH = '/org/freedesktop/portal/desktop'
SCREENSHOT_INTERFACE = 'org.freedesktop.portal.Screenshot'
REQUEST_INTERFACE = 'org.freedesktop.portal.Request'
MAX_PORTAL_FILE_BYTES = 64 * 1024 * 1024
MAX_IMAGE_DIMENSION = 16384
MAX_IMAGE_PIXELS = 100_000_000


def predicted_request_path(sender: str, handle_token: str) -> str:
    """Build the portal request path so Response can be subscribed in advance."""

    if not sender.startswith(':') or not handle_token:
        raise ValueError('The session bus did not provide a valid sender name.')
    sender_component = sender[1:].replace('.', '_')
    return (
        f'{PORTAL_PATH}/request/{sender_component}/{handle_token}'
    )


def local_file_path_from_portal_uri(uri: str) -> Path:
    """Accept only absolute local file URIs without query/fragment components."""

    parsed = urlsplit(str(uri))
    if (parsed.scheme.lower() != 'file' or parsed.netloc
            or parsed.query or parsed.fragment or not parsed.path.startswith('/')):
        raise ValueError('The screenshot portal returned a non-local file URI.')
    raw_path = unquote(parsed.path)
    if os.name == 'nt' and len(raw_path) >= 3 and raw_path[0] == '/' and raw_path[2] == ':':
        raw_path = raw_path[1:]
    path = Path(raw_path)
    if not path.is_absolute():
        raise ValueError('The screenshot portal returned a non-absolute file path.')
    return path


def validate_portal_png(
    path: str | Path,
    *,
    max_bytes: int = MAX_PORTAL_FILE_BYTES,
) -> tuple[int, int]:
    """Validate bounded local PNG metadata before decoding pixel data."""

    source = Path(path)
    try:
        if source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
            raise ValueError('The screenshot portal result is not a regular file.')
        if source.stat().st_size <= 0 or source.stat().st_size > max_bytes:
            raise ValueError('The screenshot portal PNG is empty or exceeds the size limit.')
        with source.open('rb') as stream:
            if stream.read(8) != b'\x89PNG\r\n\x1a\n':
                raise ValueError('The screenshot portal result is not a PNG image.')
    except OSError as error:
        raise ValueError(f'Could not read the screenshot portal result: {error}') from error

    reader = QImageReader(str(source), b'png')
    size = reader.size()
    width, height = size.width(), size.height()
    if (not reader.canRead() or width <= 0 or height <= 0
            or width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS):
        raise ValueError('The screenshot portal PNG has invalid or oversized dimensions.')
    return width, height


class _PortalImageWorker(QThread):
    succeeded = Signal()
    failed = Signal(str)

    def __init__(self, source: Path, staged_path: Path):
        super().__init__()
        self._source = source
        self._staged_path = staged_path
        self.cancelled = threading.Event()
        self.commit_lock = threading.Lock()

    def run(self) -> None:
        temporary_path = None
        try:
            validate_portal_png(self._source)
            image = QImageReader(str(self._source), b'png').read()
            if image.isNull():
                raise ValueError('The screenshot portal PNG could not be decoded.')
            if self.cancelled.is_set():
                return
            descriptor, temporary = tempfile.mkstemp(
                prefix=f'.{self._staged_path.name}.portal-',
                suffix='.tmp',
                dir=self._staged_path.parent,
            )
            os.close(descriptor)
            temporary_path = Path(temporary)
            if not image.save(str(temporary_path), 'PNG'):
                raise ValueError('Could not encode the screenshot portal PNG.')
            staged_size = temporary_path.stat().st_size
            if staged_size <= 0 or staged_size > MAX_PORTAL_FILE_BYTES:
                raise ValueError('The encoded screenshot exceeds the size limit.')
            with self.commit_lock:
                if self.cancelled.is_set():
                    return
                os.replace(temporary_path, self._staged_path)
        except (OSError, RuntimeError, ValueError) as error:
            if not self.cancelled.is_set():
                self.failed.emit(str(error)[:500])
        else:
            if not self.cancelled.is_set():
                self.succeeded.emit()
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


class WaylandScreenshotPortal(QObject):
    """One bounded, cancelable Screenshot portal request and PNG import."""

    def __init__(self, staged_path: str | Path, completed, parent=None):
        super().__init__(parent)
        self._staged_path = Path(staged_path)
        self._completed = completed
        self._active = True
        self._bus = None
        self._request_path = ''
        self._handle_token = ''
        self._signal_slot = '1on_response(uint,QVariantMap)'
        self._connected = False
        self._method_reply_done = False
        self._method_reply_handled = False
        self._early_response = None
        self._response_consumed = False
        self._watcher = None
        self._worker = None

        self._method_timer = QTimer(self)
        self._method_timer.setSingleShot(True)
        self._method_timer.timeout.connect(self._on_method_timeout)
        self._response_timer = QTimer(self)
        self._response_timer.setSingleShot(True)
        self._response_timer.timeout.connect(self._on_response_timeout)

    def start(self) -> None:
        """Start the portal request without waiting on the GUI thread."""

        try:
            from PySide6.QtCore import SLOT
            from PySide6.QtDBus import (
                QDBusConnection,
                QDBusMessage,
                QDBusPendingCallWatcher,
                QDBusVariant,
            )

            self._bus = QDBusConnection.sessionBus()
            if not self._bus.isConnected():
                raise RuntimeError('The desktop session bus is unavailable.')
            self._handle_token = f'fthr_screenshot_{uuid.uuid4().hex}'
            self._request_path = predicted_request_path(
                self._bus.baseService(), self._handle_token)
            self._connected = self._bus.connect(
                PORTAL_SERVICE,
                self._request_path,
                REQUEST_INTERFACE,
                'Response',
                self,
                SLOT(self._signal_slot[1:]),
            )
            if not self._connected:
                raise RuntimeError('Could not subscribe to the screenshot portal response.')

            options = {
                'handle_token': QDBusVariant(self._handle_token),
                'interactive': QDBusVariant(True),
                'modal': QDBusVariant(False),
            }
            message = QDBusMessage.createMethodCall(
                PORTAL_SERVICE,
                PORTAL_PATH,
                SCREENSHOT_INTERFACE,
                'Screenshot',
            )
            message.setArguments(['', options])
            watcher = QDBusPendingCallWatcher(
                self._bus.asyncCall(message, 5000), self)
            self._watcher = watcher
            watcher.finished.connect(self._on_method_reply)
            self._method_timer.start(5000)
            if watcher.isFinished():
                QTimer.singleShot(0, lambda target=watcher:
                                  self._on_method_reply(target))
        except (ImportError, OSError, RuntimeError, TypeError, ValueError) as error:
            self._finish(False, f'Screenshot portal unavailable: {str(error)[:400]}')

    @Slot('uint', 'QVariantMap')
    def on_response(self, response: int, results: dict) -> None:
        if not self._active:
            return
        normalized = (int(response), dict(results or {}))
        if not self._method_reply_done:
            if self._early_response is None:
                self._early_response = normalized
            return
        self._consume_response(*normalized)

    def _on_method_reply(self, watcher) -> None:
        if not self._active or self._method_reply_handled:
            return
        self._method_reply_handled = True
        self._method_timer.stop()
        try:
            from PySide6.QtDBus import QDBusMessage

            reply = watcher.reply()
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                detail = reply.errorMessage() or 'The portal rejected the screenshot request.'
                raise RuntimeError(detail)
            arguments = reply.arguments()
            returned_handle = arguments[0] if arguments else None
            path_getter = getattr(returned_handle, 'path', None)
            returned_path = (
                str(path_getter()) if callable(path_getter)
                else str(returned_handle or ''))
            if returned_path != self._request_path:
                raise RuntimeError('The portal returned an unexpected request handle.')
        except (ImportError, OSError, RuntimeError, TypeError, IndexError) as error:
            self._finish(False, f'Screenshot portal request failed: {str(error)[:400]}')
            return
        self._method_reply_done = True
        if watcher is self._watcher:
            self._watcher = None
            watcher.deleteLater()
        if self._early_response is not None:
            response = self._early_response
            self._early_response = None
            self._consume_response(*response)
        else:
            self._response_timer.start(60_000)

    def _consume_response(self, response: int, results: dict) -> None:
        if not self._active or self._response_consumed:
            return
        self._response_consumed = True
        self._response_timer.stop()
        if response == 1:
            self._finish(False, 'Screenshot was canceled in the compositor dialog.')
            return
        if response != 0:
            self._finish(False, 'The compositor could not capture a screenshot.')
            return
        uri = results.get('uri')
        if hasattr(uri, 'variant'):
            uri = uri.variant()
        try:
            source = local_file_path_from_portal_uri(str(uri or ''))
            worker = _PortalImageWorker(source, self._staged_path)
            self._worker = worker
            with _ACTIVE_IMAGE_WORKERS_LOCK:
                _ACTIVE_IMAGE_WORKERS.add(worker)
            worker.finished.connect(
                lambda target=worker: _release_image_worker(target))
            worker.finished.connect(worker.deleteLater)
            self._worker.succeeded.connect(self._on_image_saved)
            self._worker.failed.connect(self._on_image_failed)
            try:
                worker.start()
            except RuntimeError:
                _release_image_worker(worker)
                worker.deleteLater()
                self._worker = None
                raise
        except (OSError, RuntimeError, ValueError) as error:
            self._finish(False, f'Invalid screenshot portal result: {str(error)[:400]}')

    @Slot()
    def _on_image_saved(self) -> None:
        if not self._active:
            self._staged_path.unlink(missing_ok=True)
            return
        self._finish(True, '')

    @Slot(str)
    def _on_image_failed(self, detail: str) -> None:
        if self._active:
            self._finish(False, f'Could not import screenshot portal image: {detail[:400]}')

    def _on_method_timeout(self) -> None:
        self._cancel_request()
        self._finish(False, 'Screenshot portal did not answer within 5 seconds.')

    def _on_response_timeout(self) -> None:
        self._cancel_request()
        self._finish(False, 'The compositor screenshot dialog timed out.')

    def _cancel_request(self) -> None:
        if self._bus is None or not self._request_path:
            return
        try:
            from PySide6.QtDBus import QDBusMessage

            message = QDBusMessage.createMethodCall(
                PORTAL_SERVICE,
                self._request_path,
                REQUEST_INTERFACE,
                'Close',
            )
            self._bus.asyncCall(message, 1000)
        except (ImportError, OSError, RuntimeError):
            # Close is best-effort: the request may already have disappeared,
            # and local completion remains bounded by the response timer.
            pass

    def _disconnect_response(self) -> None:
        if not self._connected or self._bus is None:
            return
        try:
            from PySide6.QtCore import SLOT

            self._bus.disconnect(
                PORTAL_SERVICE,
                self._request_path,
                REQUEST_INTERFACE,
                'Response',
                self,
                SLOT(self._signal_slot[1:]),
            )
        except (ImportError, RuntimeError, TypeError):
            # The bus may vanish during shutdown; _active already prevents
            # late deliveries from reviving a completed screenshot request.
            pass
        self._connected = False

    def _finish(self, succeeded: bool, detail: str) -> None:
        if not self._active:
            return
        self._active = False
        self._method_timer.stop()
        self._response_timer.stop()
        self._disconnect_response()
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            watcher.deleteLater()
        if not succeeded:
            self._staged_path.unlink(missing_ok=True)
        callback = self._completed
        self._completed = None
        self.deleteLater()
        if callback is not None:
            callback(succeeded, detail)

    def close(self) -> None:
        """Cancel callbacks and the portal request during application shutdown."""

        if not self._active:
            return
        self._cancel_request()
        self._active = False
        self._method_timer.stop()
        self._response_timer.stop()
        self._disconnect_response()
        watcher = self._watcher
        self._watcher = None
        if watcher is not None:
            watcher.deleteLater()
        worker = self._worker
        if worker is not None:
            with worker.commit_lock:
                worker.cancelled.set()
            if worker.isRunning():
                worker.wait(1500)
        self._staged_path.unlink(missing_ok=True)
        self.deleteLater()


def _release_image_worker(worker: _PortalImageWorker) -> None:
    with _ACTIVE_IMAGE_WORKERS_LOCK:
        _ACTIVE_IMAGE_WORKERS.discard(worker)
