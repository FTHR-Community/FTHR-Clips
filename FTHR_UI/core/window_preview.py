"""Small, isolated live previews for a selected application window.

The preview is deliberately independent of the capture/recording pipeline.  On
Windows it asks DWM to composite a thumbnail into the UI's top-level window;
on native X11 it asks the bundled FFmpeg to sample that exact X window at a
low rate.  Wayland and unsupported platforms fail closed with a status message.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Any, Callable

from PySide6.QtCore import QCoreApplication, QObject, QProcess, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QImage


_PREVIEW_WIDTH = 320
_PREVIEW_HEIGHT = 180
_FRAME_BYTES = _PREVIEW_WIDTH * _PREVIEW_HEIGHT * 3
_MAX_STDERR_BYTES = 4096
_POLL_MS = 500
_STARTUP_TIMEOUT_MS = 3000
_DISPOSING_BACKENDS: set[QObject] = set()


def _valid_rect(rect: object) -> tuple[int, int, int, int] | None:
    if not isinstance(rect, (tuple, list)) or len(rect) != 4:
        return None
    try:
        left, top, right, bottom = (int(value) for value in rect)
    except (TypeError, ValueError, OverflowError):
        # Reject malformed UI geometry instead of requesting an unsafe native preview.
        return None
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _preview_is_headless() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        return str(app.platformName()).casefold() in {"offscreen", "minimal"}
    except RuntimeError:
        # Qt may be shutting down; fail closed rather than initializing a native backend.
        return True


class WindowPreviewBackend(QObject):
    """Common asynchronous preview backend contract."""

    image_ready = Signal(QImage)
    status_changed = Signal(str)

    def start(
        self,
        window: dict[str, Any],
        destination_hwnd: int,
        destination_rect: tuple[int, int, int, int],
    ) -> None:
        raise NotImplementedError

    def update_geometry(
        self, destination_hwnd: int, destination_rect: tuple[int, int, int, int]
    ) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def dispose(self) -> None:
        """Stop the backend and defer QObject deletion safely."""
        self.stop()
        self.deleteLater()


class _DwmThumbnailProperties(ctypes.Structure):
    _fields_ = [
        ("dwFlags", ctypes.c_uint32),
        ("rcDestination", ctypes.c_int32 * 4),
        ("rcSource", ctypes.c_int32 * 4),
        ("opacity", ctypes.c_uint8),
        ("fVisible", ctypes.c_int32),
        ("fSourceClientAreaOnly", ctypes.c_int32),
    ]


class _DwmSize(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_int32), ("cy", ctypes.c_int32)]


class _WindowsDwmApi:
    """Thin ctypes boundary, kept injectable for non-Windows unit tests."""

    TNP_RECTDESTINATION = 0x00000001
    TNP_VISIBLE = 0x00000008
    TNP_OPACITY = 0x00000004
    TNP_SOURCECLIENTAREAONLY = 0x00000010

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("DWM thumbnails are available only on Windows.")
        self._dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._register = self._dwm.DwmRegisterThumbnail
        self._register.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.POINTER(ctypes.c_void_p)]
        self._register.restype = ctypes.c_int32
        self._query = self._dwm.DwmQueryThumbnailSourceSize
        self._query.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DwmSize)]
        self._query.restype = ctypes.c_int32
        self._update = self._dwm.DwmUpdateThumbnailProperties
        self._update.argtypes = [ctypes.c_void_p,
                                 ctypes.POINTER(_DwmThumbnailProperties)]
        self._update.restype = ctypes.c_int32
        self._unregister = self._dwm.DwmUnregisterThumbnail
        self._unregister.argtypes = [ctypes.c_void_p]
        self._unregister.restype = ctypes.c_int32
        self._is_window = self._user32.IsWindow
        self._is_window.argtypes = [ctypes.c_void_p]
        self._is_window.restype = ctypes.c_int32
        self._is_iconic = self._user32.IsIconic
        self._is_iconic.argtypes = [ctypes.c_void_p]
        self._is_iconic.restype = ctypes.c_int32
        self._get_pid = self._user32.GetWindowThreadProcessId
        self._get_pid.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._get_pid.restype = ctypes.c_uint32

    @staticmethod
    def _hwnd(value: int) -> ctypes.c_void_p:
        return ctypes.c_void_p(value)

    def is_window(self, hwnd: int) -> bool:
        return bool(self._is_window(self._hwnd(hwnd)))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._is_iconic(self._hwnd(hwnd)))

    def process_id(self, hwnd: int) -> int | None:
        result = ctypes.c_uint32()
        if not self._get_pid(self._hwnd(hwnd), ctypes.byref(result)):
            return None
        return int(result.value)

    def register(self, destination: int, source: int) -> int:
        thumbnail = ctypes.c_void_p()
        result = self._register(self._hwnd(destination), self._hwnd(source),
                                ctypes.byref(thumbnail))
        self.check(result, "DwmRegisterThumbnail")
        if not thumbnail.value:
            raise OSError("DWM returned an empty thumbnail handle.")
        return int(thumbnail.value)

    def source_size(self, thumbnail: int) -> tuple[int, int]:
        size = _DwmSize()
        self.check(self._query(self._hwnd(thumbnail), ctypes.byref(size)),
                   "DwmQueryThumbnailSourceSize")
        return int(size.cx), int(size.cy)

    def update(self, thumbnail: int, rect: tuple[int, int, int, int], visible: bool,
               source_client_only: bool = True) -> None:
        props = _DwmThumbnailProperties()
        props.dwFlags = self.TNP_RECTDESTINATION | self.TNP_VISIBLE | self.TNP_OPACITY
        props.rcDestination[:] = rect
        props.opacity = 255
        props.fVisible = int(visible)
        props.fSourceClientAreaOnly = int(source_client_only)
        self.check(self._update(self._hwnd(thumbnail), ctypes.byref(props)),
                   "DwmUpdateThumbnailProperties")

    def unregister(self, thumbnail: int) -> None:
        self.check(self._unregister(self._hwnd(thumbnail)), "DwmUnregisterThumbnail")

    @staticmethod
    def check(result: int, function: str) -> None:
        if result < 0:
            raise OSError(f"{function} failed with HRESULT 0x{result & 0xffffffff:08x}.")


def _letterbox(
    source_size: tuple[int, int], rect: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    left, top, right, bottom = rect
    source_width, source_height = source_size
    target_width, target_height = right - left, bottom - top
    if source_width <= 0 or source_height <= 0:
        return left, top, left, top
    scale = min(target_width / source_width, target_height / source_height)
    width = max(1, min(target_width, int(round(source_width * scale))))
    height = max(1, min(target_height, int(round(source_height * scale))))
    x = left + (target_width - width) // 2
    y = top + (target_height - height) // 2
    return x, y, x + width, y + height


class WindowsDwmPreviewBackend(WindowPreviewBackend):
    def __init__(self, parent: QObject | None = None, *, native_api: Any = None,
                 poll_interval_ms: int = _POLL_MS) -> None:
        super().__init__(parent)
        self._api = native_api
        self._thumbnail: int | None = None
        self._source_hwnd = 0
        self._source_pid: int | None = None
        self._destination_hwnd = 0
        self._registered_destination = 0
        self._rect: tuple[int, int, int, int] | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(max(100, int(poll_interval_ms)))
        self._timer.timeout.connect(self._poll)

    def start(self, window: dict[str, Any], destination_hwnd: int,
              destination_rect: tuple[int, int, int, int]) -> None:
        self.stop()
        if _preview_is_headless():
            self.status_changed.emit("Window previews are unavailable in this display mode.")
            return
        try:
            source = int(window.get("hwnd", 0))
            destination = int(destination_hwnd)
            rect = _valid_rect(destination_rect)
            if source <= 0 or destination <= 0 or rect is None:
                raise ValueError("The selected window or preview area is invalid.")
            if not self._native().is_window(source):
                raise ValueError("The selected window is no longer available.")
            if not self._native().is_window(destination):
                raise ValueError("The preview window is no longer available.")
            if self._native().is_iconic(source):
                self.status_changed.emit("The selected window is minimized.")
                return
            thumbnail = self._native().register(destination, source)
            self._thumbnail = thumbnail
            width, height = self._native().source_size(thumbnail)
            if width <= 0 or height <= 0:
                raise ValueError("The selected window has no previewable content.")
            fitted = _letterbox((width, height), rect)
            self._native().update(thumbnail, fitted, True, False)
            self._source_hwnd = source
            self._source_pid = self._native().process_id(source)
            self._destination_hwnd = destination
            self._registered_destination = destination
            self._rect = rect
            self._timer.start()
            self.status_changed.emit("Live window preview")
        except Exception as error:
            self._release()
            self.status_changed.emit(f"Window preview unavailable: {error}")

    def update_geometry(self, destination_hwnd: int,
                        destination_rect: tuple[int, int, int, int]) -> None:
        rect = _valid_rect(destination_rect)
        try:
            destination = int(destination_hwnd)
            if rect is None or destination <= 0:
                raise ValueError("The preview area is invalid.")
            self._destination_hwnd = destination
            self._rect = rect
            if self._thumbnail is not None:
                if destination != self._registered_destination:
                    self._native().unregister(self._thumbnail)
                    self._thumbnail = None
                    self._thumbnail = self._native().register(
                        destination, self._source_hwnd
                    )
                    self._registered_destination = destination
                fitted = _letterbox(
                    self._native().source_size(self._thumbnail), rect
                )
                self._native().update(self._thumbnail, fitted, True, False)
        except Exception as error:
            self._release()
            self.status_changed.emit(f"Window preview unavailable: {error}")

    def stop(self) -> None:
        self._release()

    def _native(self) -> Any:
        if self._api is None:
            self._api = _WindowsDwmApi()
        return self._api

    def _poll(self) -> None:
        try:
            if self._thumbnail is None or not self._native().is_window(self._source_hwnd):
                raise ValueError("The selected window has closed.")
            pid = self._native().process_id(self._source_hwnd)
            if self._source_pid is not None and pid is not None and pid != self._source_pid:
                raise ValueError("The selected window is no longer the same application.")
            if self._native().is_iconic(self._source_hwnd):
                self._native().update(self._thumbnail, (0, 0, 0, 0), False, False)
                raise ValueError("The selected window is minimized.")
            if not self._native().is_window(self._destination_hwnd):
                raise ValueError("The preview window is no longer available.")
        except Exception as error:
            self._release()
            self.status_changed.emit(f"Window preview unavailable: {error}")

    def _release(self) -> None:
        self._timer.stop()
        thumbnail, self._thumbnail = self._thumbnail, None
        self._source_hwnd = 0
        self._source_pid = None
        self._registered_destination = 0
        if thumbnail is not None:
            try:
                self._native().unregister(thumbnail)
            except Exception as error:
                self.status_changed.emit(f"Could not release window preview: {error}")


class LinuxX11PreviewBackend(WindowPreviewBackend):
    """Low-rate exact-XID FFmpeg sampler with bounded asynchronous buffering."""

    def __init__(self, parent: QObject | None = None, *,
                 process_factory: Callable[..., QProcess] = QProcess,
                 ffmpeg_resolver: Callable[[], str] | None = None,
                 environment: dict[str, str] | None = None,
                 poll_interval_ms: int = _POLL_MS,
                 startup_timeout_ms: int = _STARTUP_TIMEOUT_MS) -> None:
        super().__init__(parent)
        self._process_factory = process_factory
        self._ffmpeg_resolver = ffmpeg_resolver
        self._environment = os.environ if environment is None else environment
        self._poll_interval_ms = max(100, int(poll_interval_ms))
        self._startup_timeout_ms = max(100, int(startup_timeout_ms))
        self._process: QProcess | None = None
        self._token = 0
        self._pending: tuple[int, tuple[int, int, int, int]] | None = None
        self._desired: tuple[int, tuple[int, int, int, int]] | None = None
        self._buffer = bytearray()
        self._stderr = bytearray()
        self._got_frame = False
        self._stopping = False
        self._disposing = False
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._frame_watchdog_expired)
        self._kill_timer = QTimer(self)
        self._kill_timer.setSingleShot(True)
        self._kill_timer.timeout.connect(self._force_kill)

    def start(self, window: dict[str, Any], destination_hwnd: int,
              destination_rect: tuple[int, int, int, int]) -> None:
        if self._disposing:
            return
        if _preview_is_headless():
            self.stop()
            self.status_changed.emit("Window previews are unavailable in this display mode.")
            return
        if not self._native_x11():
            self.stop()
            self.status_changed.emit("Live window previews require a native X11 session.")
            return
        try:
            xid = int(window.get("hwnd", 0))
            rect = _valid_rect(destination_rect)
            if xid <= 0 or xid > 0xFFFFFFFF or rect is None:
                raise ValueError("The selected X11 window or preview area is invalid.")
            # The destination is a QWidget handle on Linux; its numeric value is
            # intentionally unused because FFmpeg delivers images to the UI.
            desired = (xid, rect)
        except Exception as error:
            self.stop()
            self.status_changed.emit(f"Window preview unavailable: {error}")
            return
        self._stopping = False
        self._desired = desired
        if self._process is not None:
            self._pending = desired
            self._terminate_process()
            return
        self._launch(desired)

    def update_geometry(self, destination_hwnd: int,
                        destination_rect: tuple[int, int, int, int]) -> None:
        rect = _valid_rect(destination_rect)
        if rect is None:
            self.stop()
            self.status_changed.emit("Window preview unavailable: the preview area is invalid.")
            return
        if self._desired is not None:
            self._desired = (self._desired[0], rect)
            if self._pending is not None:
                self._pending = (self._pending[0], rect)

    def stop(self) -> None:
        self._stopping = True
        self._desired = None
        self._pending = None
        self._watchdog.stop()
        if self._process is not None:
            self._terminate_process()
        self._buffer.clear()
        self._stderr.clear()

    def dispose(self) -> None:
        if self._disposing:
            return
        self._disposing = True
        self._stopping = True
        self._desired = None
        self._pending = None
        self._watchdog.stop()
        self._kill_timer.stop()
        self._buffer.clear()
        self._stderr.clear()

        # A widget may be destroyed while X11 is stuck inside a capture read.
        # Detach first so widget teardown cannot synchronously destroy an
        # active QProcess child.  Kill is non-blocking; reaping happens via its
        # finished/error callback before this backend is deleted.
        app = QCoreApplication.instance()
        if app is not None:
            self.setParent(app)
        else:
            self.setParent(None)
            _DISPOSING_BACKENDS.add(self)
            self.destroyed.connect(
                lambda _obj=None, backend=self: _DISPOSING_BACKENDS.discard(backend)
            )

        process = self._process
        if process is None:
            self.deleteLater()
        elif process.state() == QProcess.ProcessState.NotRunning:
            # A finished callback may already be queued.  Keep ownership until
            # it has deleted the process object and finalized disposal.
            return
        else:
            process.kill()

    def _native_x11(self) -> bool:
        app = QGuiApplication.instance()
        try:
            platform = str(app.platformName()).casefold() if app is not None else ""
        except RuntimeError:
            platform = ""
        if "wayland" in platform:
            return False
        if self._environment.get("WAYLAND_DISPLAY"):
            return False
        if self._environment.get("XDG_SESSION_TYPE", "").strip().casefold() == "wayland":
            return False
        return platform in {"xcb", "x11"} or bool(self._environment.get("DISPLAY"))

    def _launch(self, desired: tuple[int, tuple[int, int, int, int]]) -> None:
        xid, _rect = desired
        self._token += 1
        token = self._token
        try:
            if self._ffmpeg_resolver is None:
                from core.ffmpeg_tools import get_ffmpeg_exe
                ffmpeg = get_ffmpeg_exe()
            else:
                ffmpeg = self._ffmpeg_resolver()
            process = self._process_factory(self)
            self._process = process
            self._buffer.clear()
            self._stderr.clear()
            self._got_frame = False
            process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
            process.readyReadStandardOutput.connect(
                lambda p=process, t=token: self._read_stdout(p, t)
            )
            process.readyReadStandardError.connect(
                lambda p=process, t=token: self._read_stderr(p, t)
            )
            process.started.connect(lambda p=process, t=token: self._on_started(p, t))
            process.finished.connect(
                lambda code, status, p=process, t=token: self._on_finished(
                    p, t, int(code), status
                )
            )
            process.errorOccurred.connect(
                lambda error, p=process, t=token: self._on_error(p, t, error)
            )
            args = [
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "x11grab", "-draw_mouse", "0", "-framerate", "2",
                "-window_id", str(xid), "-probesize", "32", "-analyzeduration", "0",
                "-i", self._environment.get("DISPLAY", ":0.0"),
                "-an", "-threads", "1", "-filter_threads", "1",
                "-vf", (f"scale={_PREVIEW_WIDTH}:{_PREVIEW_HEIGHT}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={_PREVIEW_WIDTH}:{_PREVIEW_HEIGHT}:(ow-iw)/2:(oh-ih)/2"),
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ]
            process.start(ffmpeg, args, QProcess.OpenModeFlag.ReadOnly)
            self._watchdog.start(self._startup_timeout_ms)
            self.status_changed.emit("Starting live window preview…")
        except Exception as error:
            process = self._process
            self._process = None
            if process is not None:
                process.deleteLater()
            self.status_changed.emit(f"Window preview unavailable: {error}")

    def _current(self, process: QProcess, token: int) -> bool:
        return self._process is process and token == self._token

    def _on_started(self, process: QProcess, token: int) -> None:
        if self._current(process, token):
            self.status_changed.emit("Waiting for window image…")

    def _read_stdout(self, process: QProcess, token: int) -> None:
        if not self._current(process, token):
            process.readAllStandardOutput()
            return
        incoming = bytes(process.readAllStandardOutput())
        if not incoming:
            return
        if self._stopping or self._pending is not None:
            return
        self._buffer.extend(incoming)
        complete = len(self._buffer) // _FRAME_BYTES
        if complete > 3:
            del self._buffer[:(complete - 3) * _FRAME_BYTES]
            complete = 3
        if complete:
            offset = (complete - 1) * _FRAME_BYTES
            frame = bytes(self._buffer[offset:offset + _FRAME_BYTES])
            del self._buffer[:complete * _FRAME_BYTES]
            image = QImage(frame, _PREVIEW_WIDTH, _PREVIEW_HEIGHT,
                           _PREVIEW_WIDTH * 3, QImage.Format.Format_RGB888).copy()
            self._got_frame = True
            self._watchdog.start(self._startup_timeout_ms)
            self.status_changed.emit("Live window preview")
            self.image_ready.emit(image)

    def _read_stderr(self, process: QProcess, token: int) -> None:
        chunk = bytes(process.readAllStandardError())
        if (self._current(process, token) and not self._stopping and
                self._pending is None and len(self._stderr) < _MAX_STDERR_BYTES):
            remaining = _MAX_STDERR_BYTES - len(self._stderr)
            self._stderr.extend(chunk[:remaining])

    def _on_finished(self, process: QProcess, token: int,
                     exit_code: int, _exit_status: Any) -> None:
        if not self._current(process, token):
            process.deleteLater()
            return
        self._watchdog.stop()
        self._kill_timer.stop()
        self._process = None
        self._buffer.clear()
        process.deleteLater()
        if self._disposing:
            self.deleteLater()
            return
        if not self._stopping and self._pending is None and self._desired is not None:
            message = self._stderr.decode("utf-8", errors="replace").strip()
            self.status_changed.emit(
                "Window preview stopped: " + (message[:300] if message else
                                               f"FFmpeg exited with code {exit_code}.")
            )
        pending, self._pending = self._pending, None
        self._stderr.clear()
        if not self._stopping and pending is not None:
            self._launch(pending)

    def _on_error(self, process: QProcess, token: int, error: Any) -> None:
        if not self._current(process, token):
            return
        if error == QProcess.ProcessError.FailedToStart:
            self._watchdog.stop()
            self._process = None
            process.deleteLater()
            if self._disposing:
                self.deleteLater()
                return
            self.status_changed.emit(f"Window preview could not start: {process.errorString()}")
            pending, self._pending = self._pending, None
            if not self._stopping and pending is not None:
                self._launch(pending)

    def _frame_watchdog_expired(self) -> None:
        if self._process is None:
            return
        self.status_changed.emit(
            "Window preview timed out; the selected window may be unavailable."
        )
        self._terminate_process()

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.state() == QProcess.ProcessState.NotRunning:
            return
        process.terminate()
        self._kill_timer.start(250)

    def _force_kill(self) -> None:
        if self._process is not None and self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()


class UnsupportedWindowPreviewBackend(WindowPreviewBackend):
    def start(self, window: dict[str, Any], destination_hwnd: int,
              destination_rect: tuple[int, int, int, int]) -> None:
        self.status_changed.emit("Live window previews are not supported on this platform.")

    def update_geometry(self, destination_hwnd: int,
                        destination_rect: tuple[int, int, int, int]) -> None:
        return

    def stop(self) -> None:
        return


def create_window_preview_backend(parent: QObject | None = None) -> WindowPreviewBackend:
    """Select a light-weight backend without importing multimedia components."""
    if sys.platform == "win32":
        return WindowsDwmPreviewBackend(parent)
    if sys.platform.startswith("linux"):
        return LinuxX11PreviewBackend(parent)
    return UnsupportedWindowPreviewBackend(parent)
