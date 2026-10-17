"""Capture Windows keyboard visualizers into a timestamped JPEG ring.

Win32/GDI capture runs outside the GUI thread; the latest raw frame serves
the settings preview. This source is independent of native screen capture.
"""

from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path

from core.camera_overlay import clamp_overlay_rect


DEFAULT_KEYBOARD_OVERLAY_RECT = {
    'x': 0.30,
    'y': 0.70,
    'w': 0.40,
    'h': 0.25,
}
DEFAULT_KEYBOARD_COLOR = '#00ff00'
DEFAULT_KEYBOARD_INTENSITY = 58
KEYBOARD_RING_SECONDS = 305
# Keyboard visualizers change only when a key state changes.  Fifteen samples
# per second keeps transitions responsive while avoiding a permanent 30 FPS
# PrintWindow/JPEG workload next to video playback in the clip editor.
KEYBOARD_CAPTURE_FPS = 15
KEYBOARD_COMPOSITE_FPS = 15
KEYBOARD_RING_MAX_WIDTH = 960
KEYBOARD_JPEG_QUALITY = 88

_HEX_COLOR = re.compile(r'^#[0-9a-fA-F]{6}$')
_KEYBOARD_HINTS = (
    'noboard', 'nohboard', 'keyviz', 'keycast', 'keycastow',
    'keyboard visualizer', 'keystrokes', 'key overlay', 'kps',
)


def normalize_keyboard_color(value: object) -> str:
    """Return a safe ``#rrggbb`` color for settings and filter arguments."""
    text = str(value or '').strip()
    if not text.startswith('#'):
        text = f'#{text}'
    if not _HEX_COLOR.fullmatch(text):
        return DEFAULT_KEYBOARD_COLOR
    return text.lower()


def keyboard_color_rgb(value: object) -> tuple[int, int, int]:
    color = normalize_keyboard_color(value)
    return tuple(int(color[offset:offset + 2], 16) for offset in (1, 3, 5))


def normalize_keyboard_intensity(value: object) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return DEFAULT_KEYBOARD_INTENSITY


def chroma_similarity_for_intensity(value: object) -> float:
    """Map the user-facing intensity control to OBS-like key similarity."""
    intensity = normalize_keyboard_intensity(value)
    # Keep a useful key even at the lowest setting, while allowing typical
    # anti-aliased green-screen edges to disappear at the high end.
    return 0.06 + (0.54 * intensity / 100.0)


def third_party_keyboard_settings(settings) -> dict:
    """Read and sanitize the nested third-party keyboard settings object."""
    getter = settings.get
    raw = getter('third_party_keyboard', {})
    raw = raw if isinstance(raw, dict) else {}
    try:
        hwnd = max(0, int(raw.get('hwnd', 0) or 0))
    except (TypeError, ValueError):
        hwnd = 0
    return {
        'enabled': bool(raw.get('enabled', False)) and hwnd != 0,
        'hwnd': hwnd,
        'window_name': str(raw.get('window_name', '') or ''),
        'color': normalize_keyboard_color(raw.get(
            'color', DEFAULT_KEYBOARD_COLOR)),
        'intensity': normalize_keyboard_intensity(raw.get(
            'intensity', DEFAULT_KEYBOARD_INTENSITY)),
        'rect': clamp_overlay_rect(
            raw.get('rect'), DEFAULT_KEYBOARD_OVERLAY_RECT),
    }


def is_windows_keyboard_source() -> bool:
    return sys.platform == 'win32'


import ctypes.wintypes as wintypes

class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]

class _RgbQuad(ctypes.Structure):
    _fields_ = [
        ('rgbBlue', wintypes.BYTE),
        ('rgbGreen', wintypes.BYTE),
        ('rgbRed', wintypes.BYTE),
        ('rgbReserved', wintypes.BYTE),
    ]

class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ('bmiHeader', _BitmapInfoHeader),
        ('bmiColors', _RgbQuad),
    ]


def _window_process_path(hwnd: int) -> str:
    if sys.platform != 'win32':
        return ''
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ''
        handle = kernel32.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return ''
        try:
            path_buffer = ctypes.create_unicode_buffer(1024)
            length = wintypes.DWORD(len(path_buffer))
            if kernel32.QueryFullProcessImageNameW(
                    handle, 0, path_buffer, ctypes.byref(length)):
                return path_buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ''
    return ''


def enumerate_keyboard_windows() -> list[dict]:
    """Return visible Windows windows suitable for a keyboard source.

    All titled windows remain selectable because third-party tools often let
    users rename their window.  Known keyboard visualizers are sorted first
    and marked with ``is_keyboard_candidate`` for the UI.
    """
    if sys.platform != 'win32':
        return []
    user32 = ctypes.windll.user32
    current_pid = ctypes.windll.kernel32.GetCurrentProcessId()
    results: list[dict] = []
    hwnd_type = ctypes.c_void_p
    user32.IsWindowVisible.argtypes = [hwnd_type]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [hwnd_type]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        hwnd_type, ctypes.c_wchar_p, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = [hwnd_type, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowThreadProcessId.argtypes = [
        hwnd_type, ctypes.POINTER(wintypes.DWORD)]
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        if title_len <= 0:
            return True
        title_buffer = ctypes.create_unicode_buffer(title_len + 1)
        user32.GetWindowTextW(hwnd, title_buffer, title_len + 1)
        title = title_buffer.value.strip()
        if not title or title.casefold() == 'program manager':
            return True
        ex_style = user32.GetWindowLongW(hwnd, -20)
        if ex_style & 0x00000080:  # WS_EX_TOOLWINDOW
            return True
        window_pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == current_pid:
            return True
        process_path = _window_process_path(int(hwnd))
        haystack = f'{title} {process_path}'.casefold()
        is_candidate = any(hint in haystack for hint in _KEYBOARD_HINTS)
        results.append({
            'hwnd': int(hwnd),
            'title': title,
            'display_name': title,
            'process_path': process_path,
            'is_keyboard_candidate': is_candidate,
        })
        return True

    user32.EnumWindows(callback_type(callback), 0)
    results.sort(key=lambda item: (
        not item['is_keyboard_candidate'], item['display_name'].casefold()))
    return results


def capture_window(hwnd: int):
    """Capture the client area as BGR without native window chrome.

    PrintWindow supports occluded sources; BitBlt is the fallback for visible
    visualizers that reject PrintWindow.
    """
    if sys.platform != 'win32':
        return None
    try:
        import numpy as np

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hwnd = int(hwnd)
        hwnd_type = ctypes.c_void_p
        user32.IsWindow.argtypes = [hwnd_type]
        user32.IsWindow.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [hwnd_type, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.GetDC.argtypes = [hwnd_type]
        user32.GetDC.restype = ctypes.c_void_p
        user32.PrintWindow.argtypes = [hwnd_type, ctypes.c_void_p, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        user32.ReleaseDC.argtypes = [hwnd_type, ctypes.c_void_p]
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
        gdi32.CreateCompatibleBitmap.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.BitBlt.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            wintypes.DWORD]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = wintypes.BOOL
        if not hwnd or not user32.IsWindow(hwnd):
            return None
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width < 2 or height < 2 or width > 8192 or height > 8192:
            return None

        window_dc = user32.GetDC(hwnd)
        if not window_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
            return None

        old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
        try:
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT. Some visualizers only
            # honour the latter, while the former is what removes native
            # non-client chrome from the captured pixels.
            rendered = bool(user32.PrintWindow(
                hwnd, memory_dc, 0x00000001 | 0x00000002))
            if not rendered:
                rendered = bool(gdi32.BitBlt(
                    memory_dc, 0, 0, width, height, window_dc, 0, 0,
                    0x00CC0020 | 0x40000000))  # SRCCOPY | CAPTUREBLT
            if not rendered:
                return None

            info = _BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down output
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            raw = (ctypes.c_ubyte * (width * height * 4))()
            copied = gdi32.GetDIBits(
                memory_dc, bitmap, 0, height, ctypes.byref(raw),
                ctypes.byref(info), 0)
            if copied != height:
                return None
            pixels = np.frombuffer(raw, dtype=np.uint8).reshape(
                (height, width, 4))
            return pixels[:, :, :3].copy()
        finally:
            if old_bitmap:
                gdi32.SelectObject(memory_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)
    except Exception:
        # A source window can disappear between the Win32 calls; the capture
        # loop reports that transient state through last_error instead.
        return None


def chroma_key_rgba(frame, color=DEFAULT_KEYBOARD_COLOR, intensity=DEFAULT_KEYBOARD_INTENSITY):
    """Return an RGBA copy with the selected color keyed to transparent."""
    import numpy as np

    if frame is None or getattr(frame, 'ndim', 0) != 3 or frame.shape[2] < 3:
        return None
    rgb = frame[:, :, :3][:, :, ::-1].astype(np.float32) / 255.0
    key = np.asarray(keyboard_color_rgb(color), dtype=np.float32) / 255.0
    distance = np.sqrt(np.sum((rgb - key) ** 2, axis=2) / 3.0)
    similarity = chroma_similarity_for_intensity(intensity)
    blend = max(0.025, 0.11 - 0.045 * normalize_keyboard_intensity(intensity) / 100.0)
    alpha = np.clip((distance - (similarity - blend)) / (2.0 * blend), 0.0, 1.0)

    # JPEG chroma subsampling and anti-aliased keycaps can leave a thin green
    # fringe even after the background has been keyed.  Suppress only the
    # selected channel and only where the pixel is already near transparency;
    # fully opaque colored keys retain their original color.  This runs before
    # both the live preview and final clip compositor, so they stay identical.
    dominant = int(np.argmax(key))
    other_channels = [index for index in range(3) if index != dominant]
    neutral = np.max(rgb[:, :, other_channels], axis=2)
    spill = np.clip(
        (rgb[:, :, dominant] - neutral) / np.maximum(1e-6, 1.0 - neutral),
        0.0, 1.0)
    suppression = spill * (1.0 - alpha) * 0.92
    rgb[:, :, dominant] = (
        rgb[:, :, dominant] * (1.0 - suppression)
        + neutral * suppression)

    result = np.rint(np.clip(rgb * 255.0, 0.0, 255.0)).astype(np.uint8)
    alpha_u8 = np.rint(alpha * 255.0).astype(np.uint8)
    return np.dstack((result, alpha_u8))


def _encode_jpeg(frame, quality: int = KEYBOARD_JPEG_QUALITY) -> bytes | None:
    try:
        import cv2
        import numpy as np  # noqa: F401 - validates the frame backend
        params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        # Preserve chroma edges around the keyed color.  4:2:0 JPEG is a
        # surprisingly large source of colored fringes on small overlays.
        sampling_key = getattr(cv2, 'IMWRITE_JPEG_SAMPLING_FACTOR', None)
        sampling_444 = getattr(cv2, 'IMWRITE_JPEG_SAMPLING_FACTOR_444', None)
        if sampling_key is not None and sampling_444 is not None:
            params.extend([int(sampling_key), int(sampling_444)])
        ok, encoded = cv2.imencode('.jpg', frame, params)
        return encoded.tobytes() if ok else None
    except Exception:
        # JPEG staging is optional for the live preview; the raw frame remains
        # available even when an individual encode cannot be produced.
        return None


class ThirdPartyKeyboardCapture:
    """Threaded Windows window sampler with a bounded timestamped frame ring."""

    def __init__(self, capture_fps: int = KEYBOARD_CAPTURE_FPS,
                 buffer_seconds: int = KEYBOARD_RING_SECONDS):
        self.capture_fps = max(1, int(capture_fps))
        self._buffer_seconds = max(1, int(buffer_seconds))
        self._frames = deque(maxlen=max(
            30, self.capture_fps * (self._buffer_seconds + 2)))
        self._lock = threading.RLock()
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._hwnd = 0
        self._latest_frame = None
        self._latest_timestamp = 0.0
        self._last_ring_frame = None
        self._last_error = ''

    @property
    def hwnd(self) -> int:
        with self._lock:
            return self._hwnd

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def configure(self, config: dict | None) -> None:
        """Apply source selection without restarting the native replay engine."""
        config = config if isinstance(config, dict) else {}
        enabled = bool(config.get('enabled', False))
        try:
            hwnd = max(0, int(config.get('hwnd', 0) or 0))
        except (TypeError, ValueError):
            hwnd = 0
        if sys.platform != 'win32' or not enabled or not hwnd:
            self.stop()
            return
        if self.hwnd == hwnd and self.is_running():
            return
        self.stop()
        with self._lock:
            self._hwnd = hwnd
            self._frames.clear()
            self._latest_frame = None
            self._latest_timestamp = 0.0
            self._last_ring_frame = None
            self._last_error = ''
            self._stop_event = threading.Event()
            event = self._stop_event
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(hwnd, event),
                name='FTHR-KeyboardOverlay',
                daemon=True,
            )
            self._thread.start()

    def apply_settings(self, settings) -> dict:
        config = third_party_keyboard_settings(settings)
        self.configure(config)
        return config

    def start(self, hwnd: int) -> None:
        """Start preview capture for a selected source."""
        self.configure({'enabled': True, 'hwnd': hwnd})

    def stop(self) -> None:
        with self._lock:
            event = self._stop_event
            thread = self._thread
            self._stop_event = None
            self._thread = None
            self._hwnd = 0
        if event is not None:
            event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def latest_frame(self):
        """Return ``(bgr_frame, monotonic_timestamp)`` for the live preview."""
        with self._lock:
            if self._latest_frame is None:
                return None, self._latest_timestamp
            return self._latest_frame.copy(), self._latest_timestamp

    def _capture_loop(self, hwnd: int, stop_event: threading.Event) -> None:
        period = 1.0 / self.capture_fps
        next_tick = time.monotonic()
        while not stop_event.is_set():
            frame = capture_window(hwnd)
            stamp = time.monotonic()
            if frame is None:
                with self._lock:
                    self._last_error = 'The keyboard window could not be captured.'
            else:
                ring_frame = frame
                if frame.shape[1] > KEYBOARD_RING_MAX_WIDTH:
                    import cv2
                    ring_width = KEYBOARD_RING_MAX_WIDTH
                    ring_height = max(
                        2, int(round(frame.shape[0] * ring_width / frame.shape[1])))
                    ring_frame = cv2.resize(
                        frame, (ring_width, ring_height),
                        interpolation=cv2.INTER_AREA)
                with self._lock:
                    self._latest_frame = frame
                    self._latest_timestamp = stamp
                    self._last_error = ''
                self._store_ring_frame_if_changed(stamp, ring_frame)
            next_tick += period
            wait_for = max(0.0, next_tick - time.monotonic())
            if wait_for > 0:
                stop_event.wait(wait_for)
            else:
                next_tick = time.monotonic()

    def _store_ring_frame_if_changed(self, stamp: float, frame) -> bool:
        """JPEG-encode only changed keyboard states to retain timing without
        repeatedly encoding identical frames.
        """
        try:
            import numpy as np

            with self._lock:
                previous = self._last_ring_frame
                unchanged = (
                    previous is not None
                    and previous.shape == frame.shape
                    and np.array_equal(previous, frame)
                )
            if unchanged:
                return False

            encoded = _encode_jpeg(frame)
            if not encoded:
                return False
            with self._lock:
                self._last_ring_frame = frame.copy()
                self._frames.append((float(stamp), encoded))
                # Retain one state immediately before the replay horizon so a
                # long-held key/background can seed the first output frame.
                cutoff = float(stamp) - self._buffer_seconds
                while (len(self._frames) > 1
                       and float(self._frames[1][0]) < cutoff):
                    self._frames.popleft()
            return True
        except Exception as error:
            print(f'[KeyboardOverlay] Could not stage changed source frame: {error}')
            with self._lock:
                self._last_error = (
                    f'Keyboard overlay frame staging failed: {error}')
            return False

    def iter_segment_frames(self, end_time: float,
                            duration_seconds: int,
                            output_fps: int = 30):
        """Yield timestamp-matched BGR frames with one decoded hold-last frame.

        Decode on demand to avoid expanding a full replay window of JPEGs in memory.
        """
        if sys.platform != 'win32':
            return
        try:
            import cv2
            import numpy as np

            output_fps = max(1, int(output_fps))
            end_time = float(end_time)
            duration = max(1, int(duration_seconds))
            start_time = end_time - duration
            with self._lock:
                stored = tuple(self._frames)
            prior = None
            entries = []
            for stamp, encoded in stored:
                entry = (float(stamp), encoded)
                if entry[0] <= start_time:
                    prior = entry
                elif entry[0] <= end_time + (1.0 / output_fps):
                    entries.append(entry)
            if prior is not None:
                entries.insert(0, prior)
            if not entries:
                return

            def decode(encoded):
                return cv2.imdecode(
                    np.frombuffer(encoded, dtype=np.uint8),
                    cv2.IMREAD_COLOR)

            frame_count = max(1, duration * output_fps)
            cursor = 0
            current = None
            for index in range(frame_count):
                wanted = start_time + index / output_fps
                while cursor < len(entries) and entries[cursor][0] <= wanted:
                    decoded = decode(entries[cursor][1])
                    cursor += 1
                    if decoded is not None and decoded.size:
                        current = decoded
                if current is None:
                    # A source that started slightly after the replay window
                    # still gets a useful first frame rather than a blank clip.
                    while cursor < len(entries) and current is None:
                        decoded = decode(entries[cursor][1])
                        cursor += 1
                        if decoded is not None and decoded.size:
                            current = decoded
                if current is not None:
                    yield current
        except Exception as error:
            with self._lock:
                self._last_error = f'Keyboard overlay frame streaming failed: {error}'

    def iter_segment_rgba(self, end_time: float, duration_seconds: int,
                          target_size: tuple[int, int], color=DEFAULT_KEYBOARD_COLOR,
                          intensity=DEFAULT_KEYBOARD_INTENSITY,
                          output_fps: int = 30):
        """Yield target-sized RGBA frames keyed with the live-preview math."""
        if sys.platform != 'win32':
            return
        try:
            import cv2

            target_w = max(2, int(target_size[0]))
            target_h = max(2, int(target_size[1]))
            previous_source = None
            previous_rgba = None
            for frame in self.iter_segment_frames(
                    end_time, duration_seconds, output_fps):
                source_frame = frame
                if source_frame is previous_source and previous_rgba is not None:
                    yield previous_rgba
                    continue
                if frame.shape[1] != target_w or frame.shape[0] != target_h:
                    frame = cv2.resize(
                        frame, (target_w, target_h),
                        interpolation=cv2.INTER_AREA)
                rgba = chroma_key_rgba(frame, color, intensity)
                if rgba is not None:
                    previous_source = source_frame
                    previous_rgba = rgba
                    yield rgba
        except Exception as error:
            with self._lock:
                self._last_error = f'Keyboard overlay RGBA streaming failed: {error}'

    def write_segment(self, output_path: str, end_time: float,
                      duration_seconds: int, output_fps: int = 30) -> bool:
        """Write the most recent keyboard frames as a small source MP4.

        Kept for callers that need a standalone source file; the clip
        finalizer uses :meth:`iter_segment_rgba` directly so it does not need
        this lossy staging pass.
        """
        if sys.platform != 'win32':
            return False
        try:
            import cv2

            output_fps = max(1, int(output_fps))

            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = None
            target_w = target_h = 0
            try:
                for image in self.iter_segment_frames(
                        end_time, duration_seconds, output_fps):
                    if writer is None:
                        source_h, source_w = image.shape[:2]
                        target_w = min(960, max(2, int(source_w))) & ~1
                        target_h = max(
                            2, int(round(source_h * target_w / max(1, source_w)))) & ~1
                        if target_w < 2 or target_h < 2:
                            return False
                        writer = cv2.VideoWriter(
                            str(path), cv2.VideoWriter_fourcc(*'mp4v'),
                            output_fps, (target_w, target_h))
                        if not writer.isOpened():
                            return False
                    if image.shape[1] != target_w or image.shape[0] != target_h:
                        image = cv2.resize(image, (target_w, target_h),
                                           interpolation=cv2.INTER_AREA)
                    writer.write(image)
            finally:
                if writer is not None:
                    writer.release()
            return path.is_file() and path.stat().st_size > 0
        except Exception as error:
            with self._lock:
                self._last_error = f'Keyboard overlay staging failed: {error}'
            return False
