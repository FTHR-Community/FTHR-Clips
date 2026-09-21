# clip_viewer.py - FTHR clip editor
import ctypes, json, math, os, sys, threading, subprocess, time
from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import Enum
from core.ffmpeg_tools import (
    FFmpegUnavailable,
    get_ffmpeg_exe,
    get_ffprobe_exe,
    maximum_quality_video_args,
    size_constrained_video_args,
    # Kept as a module-level compatibility import for integrations/tests that
    # patch the historical helper; export/share use maximum_quality_video_args.
    software_video_args,  # noqa: F401 - patched by compatibility integrations
)
from core.library_ownership import MediaOwnership, classify_media_path
from core.settings_manager import clips_directory_from
from core.ffmpeg_playback import (
    FFmpegPlaybackController, PlaybackError, discover_playback_sources,
)
from core.export_lifecycle import ExportJob, ExportState
from core.media_metadata import (
    format_bitrate,
    format_fps,
    probe_video_metadata,
)
from core.playback_mix_model import (
    PlaybackSource, SourceMixState, ffmpeg_mix_filter, source_display_name,
    source_icon_key,
)
from core.transactional_output import (
    commit_staged_output,
    create_staged_output_path,
    discard_staged_output,
)
from core.export_profiles import (
    DISCORD_PRESET,
    ExportPreset,
    ExportPresetManager,
    plan_export,
    plan_video_filters,
    probe_media,
)
from core.capture_card_watermark import capture_card_watermark_filters
from core.theme_manager import ThemeManager
from core.field_diagnostics import (
    DiagnosticError,
    emit_event,
    playback_instance_created,
    playback_instance_destroyed,
)
from pathlib import Path
from datetime import datetime

_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


def _run_bounded_validation(process, cancel_event: threading.Event | None,
                            timeout: float = 15.0) -> tuple[str, str]:
    """Communicate with an ffprobe/one-frame child without blocking teardown."""

    deadline = time.monotonic() + max(0.1, timeout)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            try:
                process.terminate()
            except (AttributeError, OSError):
                # Validation may have completed between poll and cancellation.
                pass
            try:
                process.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, TimeoutError, OSError):
                # A stubborn validator gets the same bounded kill treatment as
                # the main encoder process.
                try:
                    process.kill()
                except (AttributeError, OSError):
                    # The child may have exited while termination was attempted.
                    pass
                try:
                    process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    # No further wait is allowed during dialog shutdown.
                    pass
            raise RuntimeError('Export validation cancelled')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            try:
                process.terminate()
            except (AttributeError, OSError):
                # The timeout path is already terminal even if the child exited.
                pass
            try:
                process.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, TimeoutError, OSError):
                # Escalate once, then return a bounded validation failure.
                try:
                    process.kill()
                except (AttributeError, OSError):
                    # The child may have exited while termination was attempted.
                    pass
                try:
                    process.wait(timeout=1.0)
                except (subprocess.TimeoutExpired, TimeoutError, OSError):
                    # Do not let a broken validator hold the UI shutdown.
                    pass
            raise RuntimeError('Export validation timed out')
        try:
            stdout, stderr = process.communicate(timeout=min(0.2, remaining))
            if isinstance(stdout, bytes):
                stdout = stdout.decode('utf-8', errors='replace')
            if isinstance(stderr, bytes):
                stderr = stderr.decode('utf-8', errors='replace')
            return str(stdout or ''), str(stderr or '')
        except subprocess.TimeoutExpired:
            # Re-enter the loop to observe cancellation and the deadline.
            continue


def _validate_export_output(path: Path, ffmpeg: str,
                            cancel_event: threading.Event | None = None) -> None:
    """Probe media and decode one frame, both with bounded child ownership."""

    probe = get_ffprobe_exe()
    probe_process = subprocess.Popen(
        [probe, '-v', 'error', '-show_streams', '-of', 'json', str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding='utf-8', errors='replace', **_NO_WINDOW)
    probe_stdout, probe_stderr = _run_bounded_validation(
        probe_process, cancel_event)
    if probe_process.returncode != 0:
        raise RuntimeError(probe_stderr.strip() or 'FFprobe rejected the export')
    try:
        streams = json.loads(probe_stdout).get('streams', [])
    except (AttributeError, json.JSONDecodeError) as error:
        raise RuntimeError('FFprobe returned invalid metadata') from error
    if not any(stream.get('codec_type') == 'video'
               for stream in streams if isinstance(stream, dict)):
        raise RuntimeError('Export has no video stream')

    checked_process = subprocess.Popen(
        [ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:v:0',
         '-frames:v', '1', '-f', 'null', '-'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding='utf-8', errors='replace', **_NO_WINDOW)
    _checked_stdout, checked_stderr = _run_bounded_validation(
        checked_process, cancel_event)
    if checked_process.returncode != 0:
        raise RuntimeError(checked_stderr.strip() or 'first-frame decode failed')


if sys.platform == 'win32':
    # Direct editor/test entry points may bypass main.py; select the qualified
    # bounded-resource backend before the first QMediaPlayer is constructed.
    os.environ.setdefault('QT_MEDIA_BACKEND', 'windows')

import cv2
import numpy as np

from PySide6.QtWidgets import (
    QApplication, QDialog, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QFrame, QSizePolicy, QLineEdit, QCheckBox,
    QFileIconProvider, QScrollArea, QMenu, QStyle, QStyleOptionSlider,
)
from PySide6.QtCore import (
    Qt, Signal, QUrl, QTimer, QSize, QRect, QPoint, QEvent, QFileInfo,
    QPropertyAnimation, QEasingCurve, QMimeData, QObject, QRunnable,
    QThreadPool,
)
from PySide6.QtMultimedia import (
    QAudioOutput, QMediaPlayer, QVideoFrameFormat, QVideoSink,
)
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtGui import (
    QPainter, QBrush, QPen, QColor, QFont, QPixmap, QImage, QPolygon, QDrag, QIcon,
    QFontMetrics, QKeySequence, QShortcut, QRadialGradient,
)

from ui.style import (
    Colors, Fonts, button_primary_qss, combo_qss,
    ThemedDropdownButton, WheelSafeComboBox,
)
from ui.dialogs import FthrMessageDialog, install_fthr_titlebar


_WINDOWS_APP_ICON_CACHE: dict[str, QIcon] = {}


class PlayerLifecycleState(str, Enum):
    """Observable lifecycle of the one video player owned by a viewer."""

    PREPARING = 'PREPARING'
    READY = 'READY'
    PLAY_QUEUED = 'PLAY_QUEUED'
    PLAYING = 'PLAYING'
    PAUSED = 'PAUSED'
    FAILED = 'FAILED'
    STOPPED = 'STOPPED'
    CLOSING = 'CLOSING'
    CLOSED = 'CLOSED'


def _theme_color_with_alpha(value: str, alpha: int) -> QColor:
    color = QColor(value)
    color.setAlpha(alpha)
    return color


def _theme_rgba(value: str, alpha: int) -> str:
    color = QColor(value)
    return f'rgba({color.red()},{color.green()},{color.blue()},{alpha})'


def _load_themed_icon(filename: str, size: int, tint: str | None = None) -> QIcon:
    """Resolve a viewer control icon through the shared theme registry."""
    theme = ThemeManager()
    custom = theme.get_custom_icon_path(filename)
    path = custom or Path(__file__).parent.parent / 'assets' / 'icons' / filename
    pixmap = QPixmap(str(path)) if path.exists() else QPixmap()
    if pixmap.isNull():
        return QIcon()
    if custom is None or tint is not None:
        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.GlobalColor.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(
            tinted.rect(), QColor(tint or theme.get_icon_tint(filename)))
        painter.end()
        pixmap = tinted
    return QIcon(pixmap.scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation))


def _running_windows_app_icon(persistent_identity: str | None) -> QIcon | None:
    """Resolve an icon from a running process matching the manifest identity.

    Persist no executable path. Use a semantic glyph if no process matches.
    """

    if sys.platform != 'win32' or not persistent_identity:
        return None
    identity = persistent_identity.casefold()
    if identity.endswith('.exe'):
        identity = identity[:-4]
    cached = _WINDOWS_APP_ICON_CACHE.get(identity)
    if cached is not None and not cached.isNull():
        return cached

    try:
        from ctypes import wintypes

        class _PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                            ctypes.POINTER(_PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            return None
        executable_path = None
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            more = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
            while more:
                candidate = entry.szExeFile.casefold()
                if candidate.endswith('.exe'):
                    candidate = candidate[:-4]
                if candidate == identity:
                    process = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                    if process:
                        try:
                            path = ctypes.create_unicode_buffer(32768)
                            size = wintypes.DWORD(len(path))
                            if kernel32.QueryFullProcessImageNameW(
                                    process, 0, path, ctypes.byref(size)):
                                executable_path = path.value
                                break
                        finally:
                            kernel32.CloseHandle(process)
                more = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
        finally:
            kernel32.CloseHandle(snapshot)
        if not executable_path:
            return None
        icon = QFileIconProvider().icon(QFileInfo(executable_path))
        if icon.isNull():
            return None
        _WINDOWS_APP_ICON_CACHE[identity] = icon
        return icon
    except (AttributeError, OSError, TypeError, ValueError):
        return None


# Timeline primitives


@dataclass
class TimelineSegment:
    """One non-destructive source interval in the editor timeline."""

    start_pct: float
    end_pct: float
    deleted: bool = False


@dataclass(frozen=True)
class EditorSnapshot:
    """One reversible edit state; playback and timeline navigation are excluded."""

    trim_start: float
    trim_end: float
    segments: tuple[tuple[float, float, bool], ...]
    selected_segment: int
    crop_rect: tuple[int, int, int, int] | None
    stretch_ratio: float
    effects: tuple[tuple[str, int], ...]
    speed_rate: float = 1.0
    preserve_pitch: bool = True


_EDITOR_EFFECT_LIMITS = {
    'exposure': (-100, 100),
    'contrast': (-100, 100),
    'saturation': (-100, 100),
    'temperature': (-100, 100),
    'sharpness': (0, 100),
    'vignette': (0, 100),
}


def _editor_snapshot_to_draft(state: EditorSnapshot) -> dict:
    """Convert an immutable history snapshot into compact JSON-safe data."""

    return {
        'trim_start': state.trim_start,
        'trim_end': state.trim_end,
        'segments': [list(segment) for segment in state.segments],
        'selected_segment': state.selected_segment,
        'crop_rect': list(state.crop_rect) if state.crop_rect else None,
        'stretch_ratio': state.stretch_ratio,
        'effects': dict(state.effects),
        'speed_rate': state.speed_rate,
        'preserve_pitch': state.preserve_pitch,
    }


def _editor_snapshot_from_draft(payload: dict) -> EditorSnapshot | None:
    """Validate persisted editor data before it is allowed near the widgets."""

    if not isinstance(payload, dict):
        return None
    try:
        trim_start = float(payload.get('trim_start', 0.0))
        trim_end = float(payload.get('trim_end', 1.0))
        stretch_ratio = float(payload.get('stretch_ratio', 1.0))
        speed_rate = float(payload.get('speed_rate', 1.0))
    except (TypeError, ValueError):
        # A malformed external draft is ignored in favor of editor defaults.
        return None
    if (not all(math.isfinite(value) for value in
                (trim_start, trim_end, stretch_ratio, speed_rate))
            or not 0.0 <= trim_start < trim_end <= 1.0
            or not 0.25 <= stretch_ratio <= 4.0
            or not 0.25 <= speed_rate <= 2.0):
        return None

    raw_segments = payload.get('segments')
    if not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 500:
        return None
    segments = []
    previous_end = 0.0
    try:
        for index, raw in enumerate(raw_segments):
            if not isinstance(raw, (list, tuple)) or len(raw) != 3:
                return None
            start, end = float(raw[0]), float(raw[1])
            deleted = bool(raw[2])
            if (not math.isfinite(start) or not math.isfinite(end)
                    or start < 0.0 or end > 1.0 or end <= start
                    or (index == 0 and abs(start) > 0.0001)
                    or (index > 0 and abs(start - previous_end) > 0.0001)):
                return None
            segments.append((start, end, deleted))
            previous_end = end
    except (TypeError, ValueError):
        # Invalid persisted coordinates must never reach timeline paint logic.
        return None
    if abs(previous_end - 1.0) > 0.0001 or not any(
            not segment[2] for segment in segments):
        return None

    try:
        selected = int(payload.get('selected_segment', 0))
    except (TypeError, ValueError):
        selected = 0
    selected = max(0, min(selected, len(segments) - 1))

    crop_rect = None
    raw_crop = payload.get('crop_rect')
    if isinstance(raw_crop, (list, tuple)) and len(raw_crop) == 4:
        try:
            candidate = tuple(int(value) for value in raw_crop)
            x, y, width, height = candidate
            if (0 <= x <= 100_000 and 0 <= y <= 100_000
                    and 2 <= width <= 100_000 and 2 <= height <= 100_000):
                crop_rect = candidate
        except (TypeError, ValueError):
            crop_rect = None

    raw_effects = payload.get('effects', {})
    if not isinstance(raw_effects, dict):
        raw_effects = {}
    effects = []
    for key, (minimum, maximum) in _EDITOR_EFFECT_LIMITS.items():
        try:
            value = int(raw_effects.get(key, 0))
        except (TypeError, ValueError):
            value = 0
        effects.append((key, max(minimum, min(maximum, value))))

    raw_preserve_pitch = payload.get('preserve_pitch', True)
    preserve_pitch = (raw_preserve_pitch
                      if isinstance(raw_preserve_pitch, bool) else True)

    return EditorSnapshot(
        trim_start=trim_start,
        trim_end=trim_end,
        segments=tuple(segments),
        selected_segment=selected,
        crop_rect=crop_rect,
        stretch_ratio=stretch_ratio,
        effects=tuple(effects),
        speed_rate=speed_rate,
        preserve_pitch=preserve_pitch,
    )


_TIMELINE_CACHE_LIMIT = 8
_TIMELINE_IMAGE_CACHE: OrderedDict[
    tuple[str, int, int, int], tuple[QImage, ...]
] = OrderedDict()
_TIMELINE_CACHE_LOCK = threading.Lock()


def _timeline_cache_key(clip_path: str, count: int) -> tuple[str, int, int, int] | None:
    """Key filmstrips by stable file identity so overwritten clips cannot reuse frames."""

    try:
        stat = os.stat(clip_path)
    except OSError:
        # A missing/in-flight clip simply cannot contribute a reusable filmstrip.
        return None
    return (os.path.normcase(os.path.realpath(clip_path)), stat.st_mtime_ns,
            stat.st_size, count)


def _cached_timeline_images(key) -> tuple[QImage, ...] | None:
    if key is None:
        return None
    with _TIMELINE_CACHE_LOCK:
        images = _TIMELINE_IMAGE_CACHE.get(key)
        if images is not None:
            _TIMELINE_IMAGE_CACHE.move_to_end(key)
        return images


def _store_timeline_images(key, images: list[QImage]) -> None:
    if key is None or not images:
        return
    with _TIMELINE_CACHE_LOCK:
        _TIMELINE_IMAGE_CACHE[key] = tuple(images)
        _TIMELINE_IMAGE_CACHE.move_to_end(key)
        while len(_TIMELINE_IMAGE_CACHE) > _TIMELINE_CACHE_LIMIT:
            _TIMELINE_IMAGE_CACHE.popitem(last=False)


class _ClipMetadataSignals(QObject):
    ready = Signal(object)


class _PlaybackSourceWorker(QRunnable):
    def __init__(self, path: str, cancelled: threading.Event):
        super().__init__()
        self.path = path
        self.cancelled = cancelled
        self.signals = _ClipMetadataSignals()

    def run(self):
        from core.playback_proxy import prepare_playback_path
        path = prepare_playback_path(self.path, self.cancelled)
        if not self.cancelled.is_set():
            self.signals.ready.emit(path)


class _ClipMetadataWorker(QRunnable):
    """Probe clip metadata without blocking the editor's first paint."""

    def __init__(self, clip_path: str, cancelled: threading.Event):
        super().__init__()
        self.clip_path = clip_path
        self.cancelled = cancelled
        self.signals = _ClipMetadataSignals()

    def run(self):
        if self.cancelled.is_set():
            return
        metadata = None
        try:
            probed = probe_video_metadata(self.clip_path)
            if probed is not None and not self.cancelled.is_set():
                metadata = (
                    float(probed.duration_seconds or 0.0),
                    int(probed.width or 0),
                    int(probed.height or 0),
                    float(probed.average_fps or 0.0),
                    int(probed.video_bitrate_bps or 0),
                    int(probed.total_bitrate_bps or 0),
                )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            print(f'[ClipMetadata] probe unavailable: {error}')
            metadata = None

        # Make the next editor open a cache hit even when the user opened the
        # clip before its grid thumbnail worker had finished.
        if metadata is not None and not self.cancelled.is_set():
            try:
                from ui.clip_grid import (
                    _get_cached_duration_path, _get_cached_thumb_path,
                    _write_cached_duration,
                )
                thumb_path = _get_cached_thumb_path(self.clip_path)
                duration_path = _get_cached_duration_path(thumb_path)
                os.makedirs(os.path.dirname(duration_path), exist_ok=True)
                _write_cached_duration(
                    duration_path,
                    float(metadata[0]), int(metadata[1]), int(metadata[2]),
                    float(metadata[3]), int(metadata[4]), int(metadata[5]),
                )
            except (ImportError, OSError, TypeError, ValueError) as error:
                print(f'[ClipMetadata] cache write skipped: {error}')

        if self.cancelled.is_set():
            return
        try:
            self.signals.ready.emit(metadata)
        except RuntimeError as error:
            # The dialog may have been closed while the probe was running.
            print(f'[ClipMetadata] result dropped after close: {error}')
            return


SPEED_PRESETS: tuple[tuple[str, float], ...] = (
    ('0.25×  SLOW', 0.25),
    ('0.5×  SLOW', 0.5),
    ('0.75×  SLOW', 0.75),
    ('1×  NORMAL', 1.0),
    ('1.25×  FAST', 1.25),
    ('1.5×  FAST', 1.5),
    ('2×  FAST', 2.0),
)


def _normalise_speed(value: float, fallback: float = 1.0) -> float:
    """Return a safe speed supported by preview and export."""

    try:
        speed = float(value)
    except (TypeError, ValueError, OverflowError):
        speed = fallback
    if not math.isfinite(speed) or speed <= 0:
        speed = fallback
    return max(0.25, min(2.0, speed))


def _speed_text(value: float) -> str:
    return f'{_normalise_speed(value):.6f}'.rstrip('0').rstrip('.')


def _atempo_chain(speed: float) -> str | None:
    """Build an FFmpeg atempo chain, keeping every stage in its valid range."""

    remaining = _normalise_speed(speed)
    filters: list[str] = []
    while remaining > 2.0 + 1e-6:
        filters.append('atempo=2')
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        filters.append('atempo=0.5')
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-6:
        filters.append(f'atempo={_speed_text(remaining)}')
    return ','.join(filters) or None


def _pitch_changed_chain(speed: float) -> str | None:
    """Build an audio chain that changes pitch along with playback speed.

    FFmpeg's ``asetrate`` changes both pitch and duration. Normalize to the
    editor's canonical sample rate first so source clips recorded at 44.1 kHz
    or another rate do not get a small unintended pitch shift at 1×.
    """

    normalized = _normalise_speed(speed)
    if abs(normalized - 1.0) <= 1e-6:
        return None
    shifted_rate = int(round(48_000 * normalized))
    return f'aresample=48000,asetrate={shifted_rate},aresample=48000'


def _speed_audio_chain(speed: float, preserve_pitch: bool = True) -> str | None:
    """Return the audio speed stage for either pitch mode."""

    return (_atempo_chain(speed) if preserve_pitch
            else _pitch_changed_chain(speed))


def _speed_adjusted_duration(duration_s: float, speed: float) -> float:
    return max(0.1, float(duration_s) / _normalise_speed(speed))


def _video_filter_chain(crop_rect=None, effects: dict | None = None,
                        stretch_ratio: float = 1.0,
                        ensure_even: bool = False,
                        speed_rate: float = 1.0) -> list[str]:
    """Build the shared FFmpeg video filter chain for Export and Share."""

    filters: list[str] = []
    if crop_rect:
        x, y, w, h = crop_rect
        filters.append(f'crop={w & ~1}:{h & ~1}:{x}:{y}')

    values = effects or {}
    exposure = max(-100, min(100, int(values.get('exposure', 0))))
    contrast = max(-100, min(100, int(values.get('contrast', 0))))
    saturation = max(-100, min(100, int(values.get('saturation', 0))))
    if exposure or contrast:
        # The bundled FFmpeg lacks eq; use a three-point RGB curve for basic
        # exposure and contrast controls.
        brightness_value = exposure * 0.003
        contrast_value = 1.0 + contrast * 0.006
        curve_points = []
        for source_value in (0.0, 0.5, 1.0):
            output_value = max(
                0.0, min(1.0, (source_value - 0.5) * contrast_value
                             + 0.5 + brightness_value))
            curve_points.append(f'{source_value:.3f}/{output_value:.3f}')
        filters.append(f'curves=all={" ".join(curve_points)}')
    if saturation:
        filters.append(f'hue=s={1.0 + saturation * 0.01:.3f}')

    temperature = max(-100, min(100, int(values.get('temperature', 0))))
    if temperature:
        balance = temperature * 0.003
        filters.append(f'colorbalance=rs={balance:.3f}:bs={-balance:.3f}')

    sharpness = max(0, min(100, int(values.get('sharpness', 0))))
    if sharpness:
        filters.append(f'unsharp=5:5:{sharpness * 0.015:.3f}:5:5:0.0')

    vignette = max(0, min(100, int(values.get('vignette', 0))))
    if vignette:
        # PI/20 is subtle; PI/5 is a strong but still usable edge falloff.
        denominator = 20.0 - vignette * 0.15
        filters.append(f'vignette=PI/{denominator:.2f}')

    ratio = max(0.25, min(4.0, float(stretch_ratio or 1.0)))
    if abs(ratio - 1.0) > 0.005:
        filters.append(f'scale=trunc(iw*{ratio:.4f}/2)*2:trunc(ih/2)*2')
        # Scale preserves display aspect by changing sample aspect ratio unless
        # explicitly reset. Stretch is intentionally non-uniform, so square
        # pixels are part of the edit contract.
        filters.append('setsar=1')
    elif ensure_even:
        filters.append('scale=trunc(iw/2)*2:trunc(ih/2)*2')
    speed = _normalise_speed(speed_rate)
    if abs(speed - 1.0) > 1e-6:
        filters.append(f'setpts=PTS/{_speed_text(speed)}')
    return filters


class ClickableSlider(QSlider):
    """Compact slider with a larger hit area and absolute-click positioning.

    Left clicks start the same gesture as dragging for live updates and undo.
    """

    _HIT_HEIGHT = 26

    def __init__(self, orientation: Qt.Orientation, parent=None):
        super().__init__(orientation, parent)
        self.setMinimumHeight(self._HIT_HEIGHT)
        self._dragging_from_anywhere = False

    def _value_from_position(self, position: QPoint) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderHandle, self)

        if self.orientation() == Qt.Orientation.Horizontal:
            span = max(1, groove.width() - handle.width())
            slider_min = groove.x()
            slider_position = position.x() - slider_min - handle.width() / 2
        else:
            span = max(1, groove.height() - handle.height())
            slider_min = groove.y()
            slider_position = position.y() - slider_min - handle.height() / 2

        slider_position = max(0.0, min(float(span), slider_position))
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(round(slider_position)), span,
            option.upsideDown)

    def _set_value_from_position(self, position: QPoint) -> None:
        self.setSliderPosition(self._value_from_position(position))

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.isEnabled()):
            self._dragging_from_anywhere = True
            self.setSliderDown(True)
            self._set_value_from_position(event.position().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._dragging_from_anywhere
                and event.buttons() & Qt.MouseButton.LeftButton):
            self._set_value_from_position(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._dragging_from_anywhere
                and event.button() == Qt.MouseButton.LeftButton):
            self._set_value_from_position(event.position().toPoint())
            self._dragging_from_anywhere = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TrimSlider(QFrame):
    """Thumbnail timeline with trim, zoom, split, delete and click-to-seek."""

    range_changed  = Signal(float, float)
    seek_requested = Signal(float)
    segments_changed = Signal()
    selection_changed = Signal(int, bool)
    zoom_changed = Signal(float)
    thumbnails_ready = Signal(object)
    range_edit_started = Signal()
    range_edit_finished = Signal()
    context_split_requested = Signal(float)
    context_toggle_requested = Signal(int)

    def __init__(self, duration_ms: int, parent=None):
        super().__init__(parent)
        self.duration_ms  = max(duration_ms, 1)
        self.start_pct    = 0.0
        self.end_pct      = 1.0
        self.playhead_pct = 0.0
        self.dragging     = None
        self.segments = [TimelineSegment(0.0, 1.0)]
        self.selected_segment = 0
        self.zoom_factor = 1.0
        # Keep roughly a five-second source window reachable even for
        # multi-hour recordings, while preserving the familiar 8× cap for
        # ordinary replay clips.
        self.max_zoom_factor = max(
            8.0, min(2048.0, self.duration_ms / 5000.0))
        self.view_start_pct = 0.0
        self._thumbnails: list[QPixmap] = []
        self._thumbnail_job = 0
        self._thumbnail_cancel = threading.Event()
        self._thumbnail_worker_starts = 0
        self.setFixedHeight(116)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setStyleSheet('QFrame { background-color: transparent; border: none; }')
        self.thumbnails_ready.connect(self._set_thumbnail_images)

    @property
    def view_span_pct(self) -> float:
        return 1.0 / max(self.zoom_factor, 1.0)

    def set_zoom(self, factor: float, anchor_pct: float | None = None):
        old_span = self.view_span_pct
        factor = max(1.0, min(self.max_zoom_factor, float(factor)))
        anchor = self.playhead_pct if anchor_pct is None else anchor_pct
        local = (anchor - self.view_start_pct) / max(old_span, 1e-6)
        self.zoom_factor = factor
        span = self.view_span_pct
        self.view_start_pct = max(0.0, min(1.0 - span, anchor - local * span))
        self.zoom_changed.emit(self.zoom_factor)
        self.update()

    def pan(self, direction: int):
        span = self.view_span_pct
        self.view_start_pct = max(
            0.0, min(1.0 - span, self.view_start_pct + direction * span * 0.7))
        self.update()

    def ensure_visible(self, pct: float, align_start: bool = False):
        span = self.view_span_pct
        if self.view_start_pct <= pct <= self.view_start_pct + span:
            return
        offset = span * (0.08 if align_start else 0.5)
        self.view_start_pct = max(0.0, min(1.0 - span, pct - offset))
        self.update()

    def load_thumbnails(self, clip_path: str, count: int = 24):
        """Decode an evenly-spaced filmstrip off the UI thread.

        The viewer starts this only while playback is idle. A clip change or
        Play request cancels between decoder operations, and an LRU reuses the
        decoded images when a clip is reopened.
        """

        self.cancel_thumbnail_loading()
        self._thumbnail_job += 1
        job = self._thumbnail_job
        cancelled = threading.Event()
        self._thumbnail_cancel = cancelled
        cache_key = _timeline_cache_key(clip_path, count)
        cached = _cached_timeline_images(cache_key)
        if cached is not None:
            self._set_thumbnail_images((job, cached))
            return
        self._thumbnail_worker_starts += 1

        def _worker():
            images: list[QImage] = []
            try:
                # Decode once, sequentially. Random OpenCV seeks made a
                # 24-image strip decode the same long GOP up to 24 times,
                # competing with interactive seeking and surviving cancel.
                from core.media_process import run_media_process
                duration_seconds = max(0.001, self.duration_ms / 1000.0)
                result = run_media_process(
                    [get_ffmpeg_exe(), '-v', 'error', '-threads', '1',
                     '-filter_threads', '1', '-i', clip_path, '-an', '-sn',
                     '-vf', (f'fps={count / duration_seconds:.9f},'
                             'scale=180:100:force_original_aspect_ratio=decrease,'
                             'pad=180:100:(ow-iw)/2:(oh-ih)/2'),
                     '-frames:v', str(count), '-pix_fmt', 'rgb24',
                     '-f', 'rawvideo', '-threads:v', '1', '-'],
                    cancelled, timeout_seconds=30.0)
                if cancelled.is_set() or result is None or result[0] != 0:
                    return
                frame_bytes = 180 * 100 * 3
                for offset in range(0, len(result[1]) - frame_bytes + 1, frame_bytes):
                    pixels = result[1][offset:offset + frame_bytes]
                    image = QImage(
                        pixels, 180, 100, 180 * 3,
                        QImage.Format.Format_RGB888).copy()
                    images.append(image)
            except (FFmpegUnavailable, OSError, RuntimeError, TypeError, ValueError) as error:
                print(f'[ClipTimeline] thumbnail filmstrip unavailable: {error}')
                images = []
            if cancelled.is_set():
                return
            _store_timeline_images(cache_key, images)
            try:
                self.thumbnails_ready.emit((job, images))
            except RuntimeError:
                # The editor was closed while background decoding completed;
                # there is no remaining widget that needs the result.
                return

        threading.Thread(target=_worker, daemon=True).start()

    def cancel_thumbnail_loading(self):
        """Cooperatively stop a filmstrip job without blocking the GUI thread."""

        self._thumbnail_cancel.set()
        self._thumbnail_job += 1

    def _set_thumbnail_images(self, payload):
        job, images = payload
        if job != self._thumbnail_job:
            return
        self._thumbnails = [QPixmap.fromImage(image) for image in images]
        self.update()

    def set_playhead(self, pct: float):
        self.playhead_pct = max(0.0, min(1.0, pct))
        self.update()

    def split_at_playhead(self) -> bool:
        pct = self.playhead_pct
        index = self._segment_index_at(pct)
        if index < 0 or self.segments[index].deleted:
            return False
        segment = self.segments[index]
        minimum = max(0.001, 250 / self.duration_ms)
        if pct - segment.start_pct < minimum or segment.end_pct - pct < minimum:
            return False
        self.segments[index:index + 1] = [
            TimelineSegment(segment.start_pct, pct),
            TimelineSegment(pct, segment.end_pct),
        ]
        self.selected_segment = index + 1
        self.segments_changed.emit()
        self.selection_changed.emit(self.selected_segment, False)
        self.update()
        return True

    def toggle_selected_deleted(self) -> bool:
        if not (0 <= self.selected_segment < len(self.segments)):
            return False
        selected = self.segments[self.selected_segment]
        if not selected.deleted:
            active = sum(not segment.deleted for segment in self.segments)
            if len(self.segments) < 2 or active <= 1:
                return False
        selected.deleted = not selected.deleted
        self.segments_changed.emit()
        self.selection_changed.emit(self.selected_segment, selected.deleted)
        self.update()
        return True

    def kept_ranges(self) -> list[tuple[float, float]]:
        ranges = []
        for segment in self.segments:
            start = max(segment.start_pct, self.start_pct)
            end = min(segment.end_pct, self.end_pct)
            if not segment.deleted and end - start > 0.0001:
                ranges.append((start, end))
        return ranges

    def _segment_index_at(self, pct: float) -> int:
        for index, segment in enumerate(self.segments):
            if segment.start_pct <= pct <= segment.end_pct:
                return index
        return -1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        pad  = 14
        tw   = w - pad * 2
        ty, th = 22, 70
        view_end = self.view_start_pct + self.view_span_pct

        # Source-time ticks make zoom level legible without adding chrome.
        font = QFont(Fonts.BODY_FAMILY, 7)
        painter.setFont(font)
        painter.setPen(QPen(QColor(Colors.TEXT_DIM)))
        for tick in range(5):
            pct = self.view_start_pct + self.view_span_pct * tick / 4
            x = pad + int(tw * tick / 4)
            align = Qt.AlignmentFlag.AlignLeft if tick == 0 else (
                Qt.AlignmentFlag.AlignRight if tick == 4 else Qt.AlignmentFlag.AlignHCenter)
            painter.drawText(x - 34, 2, 68, 16, align, self._fmt(pct * self.duration_ms))

        painter.setPen(QPen(QColor(Colors.BORDER_HI), 1))
        painter.setBrush(QBrush(QColor(Colors.SURFACE_2)))
        painter.drawRect(pad, ty, tw, th)

        # Filmstrip frames are sampled in source time, including while zoomed.
        slots = max(4, min(14, tw // 92))
        for slot in range(slots):
            left = pad + int(slot * tw / slots)
            right = pad + int((slot + 1) * tw / slots)
            pct = self.view_start_pct + self.view_span_pct * (slot + 0.5) / slots
            if self._thumbnails:
                thumb_index = min(len(self._thumbnails) - 1,
                                  int(pct * len(self._thumbnails)))
                source = self._thumbnails[thumb_index]
                target = QRect(left, ty, max(1, right - left), th)
                scaled = source.scaled(
                    target.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation)
                sx = max(0, (scaled.width() - target.width()) // 2)
                sy = max(0, (scaled.height() - target.height()) // 2)
                painter.drawPixmap(target, scaled, QRect(sx, sy, target.width(), target.height()))
            painter.setPen(QPen(_theme_color_with_alpha(Colors.TEXT, 24), 1))
            painter.drawLine(right, ty, right, ty + th)

        # Dim outside trim and deleted partitions, then outline the selection.
        def x_for_pct(pct: float) -> int:
            return pad + int((pct - self.view_start_pct) / self.view_span_pct * tw)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.BG, 176)))
        if self.start_pct > self.view_start_pct:
            painter.drawRect(pad, ty, max(0, min(tw, x_for_pct(self.start_pct) - pad)), th)
        if self.end_pct < view_end:
            ex = max(pad, min(pad + tw, x_for_pct(self.end_pct)))
            painter.drawRect(ex, ty, pad + tw - ex, th)

        for index, segment in enumerate(self.segments):
            left_pct = max(segment.start_pct, self.view_start_pct)
            right_pct = min(segment.end_pct, view_end)
            if right_pct <= left_pct:
                continue
            left = max(pad, x_for_pct(left_pct))
            right = min(pad + tw, x_for_pct(right_pct))
            if segment.deleted:
                painter.setBrush(QBrush(_theme_color_with_alpha(Colors.SURFACE_2, 218)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawRect(left, ty, max(1, right - left), th)
                painter.save()
                painter.setClipRect(QRect(left, ty, max(1, right - left), th))
                painter.setPen(QPen(QColor(Colors.DELETE), 1))
                for hatch_x in range(left - th, right + th, 10):
                    painter.drawLine(hatch_x, ty + th, hatch_x + th, ty)
                painter.restore()
            if index == self.selected_segment:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                color = Colors.DELETE if segment.deleted else Colors.ACCENT
                painter.setPen(QPen(QColor(color), 2))
                painter.drawRect(left + 1, ty + 1, max(1, right - left - 2), th - 2)
            if segment.start_pct > self.view_start_pct:
                cut_x = x_for_pct(segment.start_pct)
                painter.setPen(QPen(QColor(Colors.TEXT), 1))
                painter.drawLine(cut_x, ty + 8, cut_x, ty + th - 8)

        # Trim handles stay visually distinct from cuts.
        for pct in (self.start_pct, self.end_pct):
            if self.view_start_pct <= pct <= view_end:
                x = x_for_pct(pct)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(QColor(Colors.ACCENT)))
                painter.drawRect(x - 4, ty - 3, 8, th + 6)

        if self.view_start_pct <= self.playhead_pct <= view_end:
            ph_x = x_for_pct(self.playhead_pct)
            painter.setPen(QPen(QColor(Colors.TEXT), 2))
            painter.drawLine(ph_x, ty - 7, ph_x, ty + th + 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(Colors.TEXT)))
            painter.drawPolygon(QPolygon([
                QPoint(ph_x - 5, ty - 7), QPoint(ph_x + 5, ty - 7),
                QPoint(ph_x, ty - 1),
            ]))

        # Mini-map: full clip, deleted ranges, and the currently zoomed viewport.
        overview_y, overview_h = 103, 7
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(Colors.BORDER_HI)))
        painter.drawRect(pad, overview_y, tw, overview_h)
        painter.setBrush(QBrush(QColor(Colors.DELETE)))
        for segment in self.segments:
            if segment.deleted:
                painter.drawRect(pad + int(segment.start_pct * tw), overview_y,
                                 max(1, int((segment.end_pct - segment.start_pct) * tw)),
                                 overview_h)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.ACCENT), 1))
        painter.drawRect(pad + int(self.view_start_pct * tw), overview_y,
                         max(3, int(self.view_span_pct * tw)), overview_h)

    def _pct_from_x(self, x: int) -> float:
        tw = self.width() - 28
        local = max(0.0, min(1.0, (x - 14) / max(tw, 1)))
        return self.view_start_pct + local * self.view_span_pct

    def _overview_pct_from_x(self, x: int) -> float:
        tw = self.width() - 28
        return max(0.0, min(1.0, (x - 14) / max(tw, 1)))

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        event_pos = event.position().toPoint()
        if event_pos.y() >= 98:
            pct = self._overview_pct_from_x(event_pos.x())
            span = self.view_span_pct
            self.view_start_pct = max(0.0, min(1.0 - span, pct - span / 2))
            self.playhead_pct = pct
            self.selected_segment = max(0, self._segment_index_at(pct))
            self.selection_changed.emit(
                self.selected_segment, self.segments[self.selected_segment].deleted)
            self.seek_requested.emit(pct)
            self.update()
            return
        pct     = self._pct_from_x(event_pos.x())
        px_per_pct = max(1.0, (self.width() - 28) / self.view_span_pct)
        dist_s_px = abs(pct - self.start_pct) * px_per_pct
        dist_e_px = abs(pct - self.end_pct) * px_per_pct
        if dist_s_px <= 9:
            self.dragging = 'start'
            self.range_edit_started.emit()
        elif dist_e_px <= 9:
            self.dragging = 'end'
            self.range_edit_started.emit()
        else:
            # The primary interaction is jump-to-click; the same gesture can
            # continue as a playhead scrub if the pointer moves.
            self.dragging = 'playhead'
            self.playhead_pct = pct
            index = self._segment_index_at(pct)
            if index >= 0:
                self.selected_segment = index
                self.selection_changed.emit(index, self.segments[index].deleted)
            self.seek_requested.emit(pct)
            self.update()

    def mouseMoveEvent(self, event):
        if not self.dragging:
            return
        pct = self._pct_from_x(event.position().toPoint().x())
        if self.dragging == 'playhead':
            self.playhead_pct = pct
            self.seek_requested.emit(pct)
            self.update()
        elif self.dragging == 'start':
            minimum = max(0.001, 250 / self.duration_ms)
            self.start_pct = max(0.0, min(pct, self.end_pct - minimum))
            self.range_changed.emit(self.start_pct, self.end_pct)
            self.update()
        else:
            minimum = max(0.001, 250 / self.duration_ms)
            self.end_pct = min(1.0, max(pct, self.start_pct + minimum))
            self.range_changed.emit(self.start_pct, self.end_pct)
            self.update()

    def mouseReleaseEvent(self, event):
        was_range_edit = self.dragging in ('start', 'end')
        self.dragging = None
        if was_range_edit:
            self.range_edit_finished.emit()

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.pan(-1 if delta > 0 else 1)
        else:
            anchor = self._pct_from_x(int(event.position().x()))
            self.set_zoom(self.zoom_factor * (1.25 if delta > 0 else 0.8), anchor)
        event.accept()

    def _context_menu_for_pct(self, pct: float) -> QMenu:
        """Build a segment menu targeted at source time, not stale selection."""

        pct = max(0.0, min(1.0, float(pct)))
        index = self._segment_index_at(pct)
        if index < 0:
            index = max(0, min(self.selected_segment, len(self.segments) - 1))
            pct = self.segments[index].start_pct

        self.playhead_pct = pct
        self.selected_segment = index
        segment = self.segments[index]
        self.selection_changed.emit(index, segment.deleted)
        self.seek_requested.emit(pct)
        self.update()

        menu = QMenu(self)
        menu.setObjectName('timelineContextMenu')
        menu.setStyleSheet(f'''
            QMenu#timelineContextMenu {{
                background: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};
                color: {Colors.TEXT}; padding: 5px;
                font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px;
            }}
            QMenu#timelineContextMenu::item {{ padding: 7px 30px 7px 10px; }}
            QMenu#timelineContextMenu::item:selected {{
                background: {Colors.ACCENT_SOFT}; color: {Colors.ACCENT};
            }}
            QMenu#timelineContextMenu::item:disabled {{ color: {Colors.TEXT_MUTED}; }}
        ''')

        split_action = menu.addAction('Split here')
        minimum = max(0.001, 250 / self.duration_ms)
        can_split = (not segment.deleted
                     and pct - segment.start_pct >= minimum
                     and segment.end_pct - pct >= minimum)
        split_action.setEnabled(can_split)
        split_action.triggered.connect(
            lambda _checked=False, at=pct: self.context_split_requested.emit(at))

        toggle_text = 'Restore segment' if segment.deleted else 'Delete segment'
        toggle_action = menu.addAction(toggle_text)
        active = sum(not item.deleted for item in self.segments)
        toggle_action.setEnabled(segment.deleted or (len(self.segments) > 1 and active > 1))
        toggle_action.triggered.connect(
            lambda _checked=False, at=index: self.context_toggle_requested.emit(at))
        return menu

    def contextMenuEvent(self, event):
        pos = event.pos()
        pct = (self._overview_pct_from_x(pos.x())
               if pos.y() >= 98 else self._pct_from_x(pos.x()))
        self._context_menu = self._context_menu_for_pct(pct)
        self._context_menu.popup(event.globalPos())
        event.accept()

    def _fmt(self, ms: float) -> str:
        s = int(ms / 1000)
        return f'{s // 60}:{s % 60:02d}'


# CropOverlay — transparent drag-handle crop net

class CropOverlay(QWidget):
    """
    Transparent overlay on top of a frame label.
    8 drag handles (corners + edge midpoints), rule-of-thirds grid,
    click-and-drag to create a new crop.
    """
    crop_changed = Signal(QRect)

    _HS = 9    # handle square size px
    _HZ = 14   # hit-zone radius px

    # 0=TL 1=T 2=TR 3=R 4=BR 5=B 6=BL 7=L
    _CURSORS = [
        Qt.CursorShape.SizeFDiagCursor,
        Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeBDiagCursor,
        Qt.CursorShape.SizeHorCursor,
        Qt.CursorShape.SizeFDiagCursor,
        Qt.CursorShape.SizeVerCursor,
        Qt.CursorShape.SizeBDiagCursor,
        Qt.CursorShape.SizeHorCursor,
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._crop        = QRect()
        self._bounds      = QRect()
        self._drag        = None
        self._drag_origin = QPoint()
        self._drag_rect   = QRect()

    def set_bounds(self, rect: QRect):
        self._bounds = QRect(rect)
        self.update()

    def set_crop(self, rect: QRect):
        self._crop = QRect(rect)
        self.update()

    def clear(self):
        self._crop = QRect()
        self.update()

    def has_crop(self) -> bool:
        return self._crop.isValid() and self._crop.width() > 4 and self._crop.height() > 4

    def _handle_pts(self):
        r  = self._crop
        cx = r.center().x()
        cy = r.center().y()
        return [
            QPoint(r.left(),  r.top()),
            QPoint(cx,        r.top()),
            QPoint(r.right(), r.top()),
            QPoint(r.right(), cy),
            QPoint(r.right(), r.bottom()),
            QPoint(cx,        r.bottom()),
            QPoint(r.left(),  r.bottom()),
            QPoint(r.left(),  cy),
        ]

    def _hit(self, pos: QPoint):
        if not self.has_crop():
            return None
        hz = self._HZ
        for i, p in enumerate(self._handle_pts()):
            if abs(pos.x() - p.x()) <= hz and abs(pos.y() - p.y()) <= hz:
                return i
        if self._crop.contains(pos):
            return 'move'
        return None

    def _clamp(self, rect: QRect) -> QRect:
        if self._bounds.isNull():
            return rect
        r = QRect(rect)
        if r.left()   < self._bounds.left():   r.setLeft(self._bounds.left())
        if r.top()    < self._bounds.top():    r.setTop(self._bounds.top())
        if r.right()  > self._bounds.right():  r.setRight(self._bounds.right())
        if r.bottom() > self._bounds.bottom(): r.setBottom(self._bounds.bottom())
        return r

    def paintEvent(self, event):
        if not self.has_crop():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        r = self._crop

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.BG, 148)))
        painter.drawRect(0, 0, w, r.top())
        painter.drawRect(0, r.bottom() + 1, w, h - r.bottom() - 1)
        painter.drawRect(0, r.top(), r.left(), r.height() + 1)
        painter.drawRect(r.right() + 1, r.top(), w - r.right() - 1, r.height() + 1)

        painter.setPen(QPen(QColor(Colors.TEXT), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(r)

        painter.setPen(QPen(_theme_color_with_alpha(Colors.TEXT, 42), 1))
        t3w = r.width()  // 3
        t3h = r.height() // 3
        painter.drawLine(r.left() + t3w,     r.top(), r.left() + t3w,     r.bottom())
        painter.drawLine(r.left() + t3w * 2, r.top(), r.left() + t3w * 2, r.bottom())
        painter.drawLine(r.left(), r.top() + t3h,     r.right(), r.top() + t3h)
        painter.drawLine(r.left(), r.top() + t3h * 2, r.right(), r.top() + t3h * 2)

        hs = self._HS
        painter.setPen(QPen(QColor(Colors.BG), 1))
        painter.setBrush(QBrush(QColor(Colors.TEXT)))
        for p in self._handle_pts():
            painter.drawRect(p.x() - hs // 2, p.y() - hs // 2, hs, hs)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.pos()
        hit = self._hit(pos)
        if hit is not None:
            self._drag        = hit
            self._drag_origin = QPoint(pos)
            self._drag_rect   = QRect(self._crop)
        elif self._bounds.isNull() or self._bounds.contains(pos):
            self._drag        = 'new'
            self._drag_origin = QPoint(pos)
            self._crop        = QRect(pos.x(), pos.y(), 0, 0)

    def mouseMoveEvent(self, event):
        pos = event.pos()
        if self._drag is None:
            hit = self._hit(pos)
            if isinstance(hit, int):
                self.setCursor(self._CURSORS[hit])
            elif hit == 'move':
                self.setCursor(Qt.CursorShape.SizeAllCursor)
            elif self._bounds.isNull() or self._bounds.contains(pos):
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        dx = pos.x() - self._drag_origin.x()
        dy = pos.y() - self._drag_origin.y()
        r  = QRect(self._drag_rect)

        if self._drag == 'new':
            ox, oy = self._drag_origin.x(), self._drag_origin.y()
            self._crop = self._clamp(QRect(
                min(ox, pos.x()), min(oy, pos.y()),
                abs(pos.x() - ox), abs(pos.y() - oy),
            ))
        elif self._drag == 'move':
            moved = r.translated(dx, dy)
            if not self._bounds.isNull():
                moved.moveLeft(max(self._bounds.left(),
                                   min(moved.left(), self._bounds.right()  - moved.width())))
                moved.moveTop( max(self._bounds.top(),
                                   min(moved.top(),  self._bounds.bottom() - moved.height())))
            self._crop = moved
        else:
            i  = self._drag
            nr = QRect(r)
            if i in (0, 6, 7): nr.setLeft(r.left()     + dx)
            if i in (2, 3, 4): nr.setRight(r.right()   + dx)
            if i in (0, 1, 2): nr.setTop(r.top()       + dy)
            if i in (4, 5, 6): nr.setBottom(r.bottom() + dy)
            nr = nr.normalized()
            if nr.width() > 4 and nr.height() > 4:
                self._crop = self._clamp(nr)

        self.update()
        if self.has_crop():
            self.crop_changed.emit(QRect(self._crop))

    def mouseReleaseEvent(self, event):
        self._drag = None


# CropPreviewOverlay — paints the active crop region over a video surface

class CropPreviewOverlay(QWidget):
    """
    Transparent crop-guide painter used by the live preview surface.
    Draws a teal rectangle around the crop region so the user can see exactly
    what will be exported while still seeing the rest of the video around it.
    WA_TransparentForMouseEvents so playback controls still work.
    """

    def __init__(self, src_w: int, src_h: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._src_w     = max(src_w, 1)
        self._src_h     = max(src_h, 1)
        self._crop_rect = None   # (x, y, w, h) in source pixels, or None

    def set_crop(self, crop_rect):
        self._crop_rect = crop_rect
        self.update()

    def _video_display_rect(self) -> QRect:
        """Compute the letterboxed video rectangle inside the preview."""
        vw, vh = self.width(), self.height()
        src_ar = self._src_w / self._src_h
        wid_ar = vw / max(vh, 1)
        if src_ar > wid_ar:
            dw = vw
            dh = int(vw / src_ar)
        else:
            dh = vh
            dw = int(vh * src_ar)
        ox = (vw - dw) // 2
        oy = (vh - dh) // 2
        return QRect(ox, oy, dw, dh)

    def paintEvent(self, event):
        if not self._crop_rect:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        dr   = self._video_display_rect()
        x, y, w, h = self._crop_rect

        # Crop rect mapped to display coords
        cx = dr.x() + int(x / self._src_w * dr.width())
        cy = dr.y() + int(y / self._src_h * dr.height())
        cw = int(w / self._src_w * dr.width())
        ch = int(h / self._src_h * dr.height())

        # Keep the full frame visible, but lightly veil what will be discarded.
        # This makes a tall/narrow crop unmistakable on Windows video surfaces
        # without turning the main editor into a cropped-only preview.
        crop_display = QRect(cx, cy, max(1, cw), max(1, ch))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.BG, 82)))
        painter.drawRect(dr.left(), dr.top(), dr.width(), max(0, crop_display.top() - dr.top()))
        painter.drawRect(dr.left(), crop_display.bottom(), dr.width(),
                         max(0, dr.bottom() - crop_display.bottom()))
        painter.drawRect(dr.left(), crop_display.top(),
                         max(0, crop_display.left() - dr.left()), crop_display.height())
        painter.drawRect(crop_display.right(), crop_display.top(),
                         max(0, dr.right() - crop_display.right()), crop_display.height())

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.ACCENT), 3))
        painter.drawRect(cx - 1, cy - 1, cw + 2, ch + 2)

        label = f'CROP  ·  {w} × {h}'
        painter.setFont(QFont(Fonts.DISPLAY, 9, QFont.Weight.Bold))
        metrics = painter.fontMetrics()
        label_w = metrics.horizontalAdvance(label) + 16
        label_y = max(dr.top() + 6, cy - 28)
        label_rect = QRect(max(dr.left() + 6, cx), label_y, label_w, 22)
        painter.setPen(QPen(QColor(Colors.ACCENT), 1))
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.ACCENT_SOFT, 222)))
        painter.drawRect(label_rect)
        painter.setPen(QPen(QColor(Colors.ACCENT)))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)


# LiveVideoPreview — software-backed player surface with real-time edit preview


class LiveVideoPreview(CropPreviewOverlay):
    """Composite frames, effects, and the crop guide in one Qt surface.

    Native Windows video widgets can cover ordinary overlays; QVideoSink
    allows all layers to share the paint event.
    """

    def __init__(self, src_w: int, src_h: int, parent=None):
        super().__init__(src_w, src_h, parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_sink = QVideoSink(self)
        self.video_sink.videoFrameChanged.connect(self._on_video_frame)
        self._frame_image = QImage()
        self._poster_image = QImage()
        self._frame_serial = 0
        self._effects: dict[str, int] = {}
        self._stretch_ratio = 1.0
        self._cached_key = None
        self._cached_frame = QImage()
        self._effect_pixel_budget: int | None = None
        self._effect_slow_frames = 0
        self._effect_fast_frames = 0
        self._last_effect_render_ms = 0.0

    def set_source_size(self, src_w: int, src_h: int):
        self._src_w = max(1, int(src_w))
        self._src_h = max(1, int(src_h))
        self._invalidate_processed_frame(clear_frame=False)
        self._reset_effect_budget()
        self.update()

    def set_poster(self, pixmap: QPixmap | None):
        """Keep the grid thumbnail visible until playback decodes a frame."""

        self._poster_image = (
            pixmap.toImage() if pixmap is not None and not pixmap.isNull() else QImage())
        self._invalidate_processed_frame()
        self.update()

    def set_edit_state(self, crop_rect, effects: dict | None,
                       stretch_ratio: float, preview_enabled: bool = True):
        ratio = max(0.25, min(4.0, float(stretch_ratio or 1.0)))
        normalized_effects = {
            key: int(value) for key, value in (effects or {}).items()
        }
        render_ratio = ratio if preview_enabled else 1.0
        render_effects = normalized_effects if preview_enabled else {}
        render_crop = crop_rect if preview_enabled else None
        processing_changed = (
            render_ratio != self._stretch_ratio
            or render_effects != self._effects
            or render_crop != self._crop_rect)
        self._stretch_ratio = render_ratio
        self._effects = render_effects
        self.set_crop(render_crop)
        if processing_changed:
            self._invalidate_processed_frame(clear_frame=False)
            self._reset_effect_budget()
        self.update()

    def _invalidate_processed_frame(self, *, clear_frame: bool = True):
        self._cached_key = None
        if clear_frame:
            self._cached_frame = QImage()

    def _reset_effect_budget(self):
        self._effect_pixel_budget = None
        self._effect_slow_frames = 0
        self._effect_fast_frames = 0

    def shutdown(self):
        """Detach the sink callback and release retained decoded frame buffers."""

        try:
            self.video_sink.videoFrameChanged.disconnect(self._on_video_frame)
        except (RuntimeError, TypeError):
            # The player may close between the deferred renderer refresh and
            # this callback; teardown already owns the resulting stop state.
            pass
        self._frame_image = QImage()
        self._poster_image = QImage()
        self._cached_frame = QImage()
        self._cached_key = None

    def _on_video_frame(self, frame):
        if not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        # ``toImage()`` already returns an owned QImage. Keep that image in
        # the neutral preview path; converting and copying every 1080p frame
        # here was the measured 60-fps bottleneck. Effect paths detach lazily
        # in ``_apply_color_effects`` when pixel access is actually required.
        self._frame_image = image
        self._frame_serial += 1
        # Keep the last processed frame around as a cheap visual fallback when
        # color effects are being rate-limited below decoder frame rate.
        self._invalidate_processed_frame(clear_frame=False)
        self.update()

    @staticmethod
    def _normalize_decoded_video_range(
            image: QImage, color_range: QVideoFrameFormat.ColorRange) -> QImage:
        """Return a detached display-ready image without changing its color range.

        QVideoFrame.toImage() already converts YUV to RGB; applying color_range
        again would clip highlights. Keep that argument for caller compatibility
        and preserve the image format until color effects require conversion.
        """

        del color_range
        if image.isNull():
            return image
        return image.copy()

    def _video_display_rect(self) -> QRect:
        vw, vh = self.width(), self.height()
        display_ar = (self._src_w * self._stretch_ratio) / self._src_h
        widget_ar = vw / max(vh, 1)
        if display_ar > widget_ar:
            dw = vw
            dh = int(vw / display_ar)
        else:
            dh = vh
            dw = int(vh * display_ar)
        return QRect((vw - dw) // 2, (vh - dh) // 2, max(1, dw), max(1, dh))

    @staticmethod
    def _apply_color_effects(image: QImage, effects: dict | None) -> QImage:
        """Combine linear color operations into one OpenCV affine transform.

        Apply sharpness with a blur and weighted add to avoid full-frame float
        intermediates for each effect.
        """

        values = effects or {}
        exposure = max(-100, min(100, int(values.get('exposure', 0))))
        contrast = max(-100, min(100, int(values.get('contrast', 0))))
        saturation = max(-100, min(100, int(values.get('saturation', 0))))
        temperature = max(-100, min(100, int(values.get('temperature', 0))))
        sharpness = max(0, min(100, int(values.get('sharpness', 0))))
        if not any((exposure, contrast, saturation, temperature, sharpness)):
            return image

        rgb = image.convertToFormat(QImage.Format.Format_RGB888)
        height, width = rgb.height(), rgb.width()
        if width <= 0 or height <= 0:
            return image
        stride = rgb.bytesPerLine()
        pixels = np.frombuffer(
            rgb.constBits(), dtype=np.uint8, count=stride * height,
        ).reshape((height, stride))[:, :width * 3].reshape((height, width, 3))
        output = pixels
        if exposure or contrast or saturation or temperature:
            brightness = exposure * 0.003
            contrast_factor = 1.0 + contrast * 0.006
            saturation_factor = 1.0 + saturation * 0.01
            balance = temperature * 0.003

            luminance = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)
            saturation_matrix = (
                saturation_factor * np.eye(3, dtype=np.float32)
                + (1.0 - saturation_factor) * np.tile(luminance, (3, 1)))
            temperature_matrix = np.diag(np.array(
                (1.0 + balance, 1.0, 1.0 - balance), dtype=np.float32))
            linear = temperature_matrix @ (
                saturation_matrix * contrast_factor)
            source_bias = np.full(
                3,
                255.0 * (0.5 - 0.5 * contrast_factor + brightness),
                dtype=np.float32,
            )
            bias = temperature_matrix @ saturation_matrix @ source_bias
            affine = np.column_stack((linear, bias)).astype(np.float32)
            output = cv2.transform(pixels, affine)

        if sharpness:
            amount = sharpness * 0.015
            blurred = cv2.GaussianBlur(output, (5, 5), 0)
            output = cv2.addWeighted(
                output, 1.0 + amount, blurred, -amount, 0.0)

        output = np.ascontiguousarray(output)
        return QImage(
            output.data, width, height, output.strides[0],
            QImage.Format.Format_RGB888,
        ).copy()

    def _display_frame(self, display_rect: QRect) -> QImage:
        source = self._frame_image if not self._frame_image.isNull() else self._poster_image
        if source.isNull():
            return QImage()
        key = (
            self._frame_serial, self._frame_image.isNull(),
            display_rect.width(), display_rect.height(),
            tuple(sorted(self._effects.items())), self._effect_pixel_budget,
        )
        if key != self._cached_key:
            # Ignoring the source aspect here is intentional: display_rect has
            # already incorporated the horizontal stretch ratio.
            target_size = display_rect.size()
            pixels = target_size.width() * target_size.height()
            if self._effect_pixel_budget and pixels > self._effect_pixel_budget:
                scale = (self._effect_pixel_budget / pixels) ** 0.5
                target_size = QSize(
                    max(1, int(target_size.width() * scale)),
                    max(1, int(target_size.height() * scale)),
                )
            started = time.perf_counter()
            scaled = source.scaled(
                target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._cached_frame = self._apply_color_effects(scaled, self._effects)
            self._cached_key = key
            self._last_effect_render_ms = (time.perf_counter() - started) * 1000.0
            # Full display resolution is the normal path. Only after three
            # measured misses of the 60-fps frame budget do we reduce the
            # preview processing surface; export remains full resolution.
            if self._last_effect_render_ms > 14.0:
                self._effect_slow_frames += 1
                self._effect_fast_frames = 0
                if self._effect_slow_frames >= 3:
                    current_pixels = target_size.width() * target_size.height()
                    next_budget = max(
                        1_000_000,
                        int(current_pixels * 12.0 / self._last_effect_render_ms),
                    )
                    self._effect_pixel_budget = min(current_pixels, next_budget)
                    self._effect_slow_frames = 0
            elif self._effect_pixel_budget and self._last_effect_render_ms < 7.0:
                self._effect_fast_frames += 1
                if self._effect_fast_frames >= 60:
                    full_pixels = display_rect.width() * display_rect.height()
                    self._effect_pixel_budget = min(
                        full_pixels, int(self._effect_pixel_budget * 1.25))
                    if self._effect_pixel_budget >= full_pixels:
                        self._effect_pixel_budget = None
                    self._effect_fast_frames = 0
            else:
                self._effect_slow_frames = 0
                self._effect_fast_frames = 0
        return self._cached_frame

    def _effect_display_rect(self, display_rect: QRect) -> QRect:
        if not self._crop_rect:
            return display_rect
        x, y, width, height = self._crop_rect
        return QRect(
            display_rect.x() + int(x / self._src_w * display_rect.width()),
            display_rect.y() + int(y / self._src_h * display_rect.height()),
            max(1, int(width / self._src_w * display_rect.width())),
            max(1, int(height / self._src_h * display_rect.height())),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        # Neutral playback draws the decoder-owned image directly for speed.
        # QPainter otherwise uses its fast sampler while shrinking a desktop
        # frame into the editor viewport, which turns small UI text into the
        # visibly jagged/blocky result users do not see in the source file.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(Colors.BG))
        display_rect = self._video_display_rect()
        source = self._frame_image if not self._frame_image.isNull() else self._poster_image
        color_effects_active = any(
            self._effects.get(name, 0)
            for name in ('exposure', 'contrast', 'saturation',
                         'temperature', 'sharpness'))
        # QPainter can scale the decoded frame directly. Building a second
        # smoothly-scaled QImage for every neutral frame was pure allocation
        # and consumed roughly 1 ms of every frame on the profiled machine.
        display_frame = (
            self._display_frame(display_rect) if color_effects_active else source)
        if not display_frame.isNull():
            painter.drawImage(display_rect, display_frame)

        vignette = max(0, min(100, int(self._effects.get('vignette', 0))))
        if vignette and not display_frame.isNull():
            effect_rect = self._effect_display_rect(display_rect)
            radius = max(effect_rect.width(), effect_rect.height()) * 0.72
            gradient = QRadialGradient(effect_rect.center(), max(1.0, radius))
            gradient.setColorAt(0.35, _theme_color_with_alpha(Colors.BG, 0))
            gradient.setColorAt(
                1.0, _theme_color_with_alpha(Colors.BG, int(vignette * 1.8)))
            painter.fillRect(effect_rect, QBrush(gradient))
        painter.end()

        # CropPreviewOverlay paints last, so its veil, border and label can
        # never be covered by the video backend.
        super().paintEvent(event)


# StretchOverlay — interactive horizontal output-aspect handles


class StretchOverlay(QWidget):
    """Crop-aware preview that visibly stretches as either edge is dragged."""

    stretch_changed = Signal(float)
    _HANDLE_HIT = 18

    def __init__(self, source_w: int, source_h: int, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._base_w = max(1, source_w)
        self._base_h = max(1, source_h)
        self._ratio = 1.0
        self._drag_side: str | None = None
        self._drag_origin_x = 0
        self._drag_initial_ratio = 1.0
        self._active = False
        self._preview_pixmap = QPixmap()

    @property
    def ratio(self) -> float:
        return self._ratio

    def set_preview_pixmap(self, pixmap: QPixmap):
        self._preview_pixmap = QPixmap(pixmap)
        self.update()

    def set_base_size(self, width: int, height: int):
        self._base_w = max(1, int(width))
        self._base_h = max(1, int(height))
        self.update()

    def set_ratio(self, ratio: float):
        self._ratio = max(0.25, min(4.0, float(ratio)))
        self.update()

    def set_active(self, active: bool):
        self._active = bool(active)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not active)
        self.setVisible(active)
        self.update()

    def _rect_for_ratio(self, ratio: float) -> QRect:
        available_w = max(1, self.width() - 64)
        available_h = max(1, self.height() - 54)
        base_aspect = self._base_w / self._base_h
        # The 1x frame is the stable drag reference. Ratios wider than the
        # viewport remain clamped visually, while the label retains exact output.
        # Leave visible horizontal runway at 1× so a conventional 16:9 frame
        # can actually be dragged wider without immediately hitting the dialog.
        base_w = min(int(available_w * 0.62), int(available_h * base_aspect))
        base_h = min(available_h, int(base_w / max(base_aspect, 0.001)))
        target_w = max(24, int(base_w * ratio))
        if target_w > available_w:
            scale = available_w / target_w
            target_w = available_w
            base_h = max(24, int(base_h * scale))
        left = (self.width() - target_w) // 2
        top = (self.height() - base_h) // 2
        return QRect(left, top, target_w, base_h)

    def paintEvent(self, event):
        if not self._active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._rect_for_ratio(self._ratio)

        painter.fillRect(self.rect(), QColor(Colors.SURFACE_1))
        if not self._preview_pixmap.isNull():
            # Drawing into the output rect deliberately ignores source aspect:
            # this is the actual horizontal stretch the export will receive.
            painter.drawPixmap(rect, self._preview_pixmap, self._preview_pixmap.rect())

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.BG, 74)))
        painter.drawRect(0, 0, self.width(), rect.top())
        painter.drawRect(0, rect.bottom(), self.width(), self.height() - rect.bottom())
        painter.drawRect(0, rect.top(), rect.left(), rect.height())
        painter.drawRect(rect.right(), rect.top(), self.width() - rect.right(), rect.height())

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.ACCENT), 2))
        painter.drawRect(rect)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(Colors.ACCENT)))
        for x in (rect.left(), rect.right()):
            painter.drawRect(x - 5, rect.center().y() - 24, 10, 48)
            painter.setBrush(QBrush(QColor(Colors.SURFACE_2)))
            painter.drawRect(x - 1, rect.center().y() - 10, 2, 20)
            painter.setBrush(QBrush(QColor(Colors.ACCENT)))

        target_w = max(2, int(self._base_w * self._ratio)) & ~1
        target_h = self._base_h & ~1
        label = f'{self._ratio:.2f}×   {target_w} × {target_h}'
        metrics = QFontMetrics(QFont(Fonts.BODY_FAMILY, 9, QFont.Weight.DemiBold))
        label_w = metrics.horizontalAdvance(label) + 20
        label_rect = QRect((self.width() - label_w) // 2, rect.bottom() + 10, label_w, 24)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.SURFACE_2, 226)))
        painter.setPen(QPen(QColor(Colors.ACCENT), 1))
        painter.drawRect(label_rect)
        painter.setPen(QPen(QColor(Colors.TEXT)))
        painter.setFont(QFont(Fonts.BODY_FAMILY, 9, QFont.Weight.DemiBold))
        painter.drawText(label_rect, Qt.AlignmentFlag.AlignCenter, label)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        rect = self._rect_for_ratio(self._ratio)
        x = event.position().toPoint().x()
        if abs(x - rect.left()) <= self._HANDLE_HIT:
            self._drag_side = 'left'
        elif abs(x - rect.right()) <= self._HANDLE_HIT:
            self._drag_side = 'right'
        if self._drag_side is not None:
            self._drag_origin_x = x
            self._drag_initial_ratio = self._ratio
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_side is None:
            rect = self._rect_for_ratio(self._ratio)
            x = event.position().toPoint().x()
            near_edge = (abs(x - rect.left()) <= self._HANDLE_HIT
                         or abs(x - rect.right()) <= self._HANDLE_HIT)
            self.setCursor(Qt.CursorShape.SizeHorCursor if near_edge
                           else Qt.CursorShape.ArrowCursor)
            return
        x = event.position().toPoint().x()
        direction = -1 if self._drag_side == 'left' else 1
        delta = (x - self._drag_origin_x) * direction
        ratio = max(0.25, min(4.0, self._drag_initial_ratio + delta / 180.0))
        self._ratio = ratio
        self.stretch_changed.emit(ratio)
        self.update()

    def mouseReleaseEvent(self, event):
        self._drag_side = None


class StretchDialog(QDialog):
    """Focused stretch editor with a real, crop-aware frame preview."""

    def __init__(self, clip_path: str, position_ms: int, source_w: int,
                 source_h: int, crop_rect=None, initial_ratio: float = 1.0,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle('Stretch video')
        self.setModal(True)
        self.setMinimumSize(680, 500)
        self.resize(760, 540)
        self.stretch_ratio = max(0.25, min(4.0, float(initial_ratio)))

        if crop_rect:
            base_w, base_h = crop_rect[2], crop_rect[3]
        else:
            base_w, base_h = source_w, source_h
        self._base_w = max(1, int(base_w))
        self._base_h = max(1, int(base_h))

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 18)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title_row.addStretch()
        self._dimensions = QLabel()
        self._dimensions.setObjectName('stretchDialogDimensions')
        title_row.addWidget(self._dimensions)
        root.addLayout(title_row)

        self.preview = StretchOverlay(self._base_w, self._base_h)
        self.preview.setMinimumHeight(350)
        self.preview.set_ratio(self.stretch_ratio)
        self.preview.set_preview_pixmap(
            self._extract_preview_frame(clip_path, position_ms, crop_rect))
        self.preview.set_active(True)
        self.preview.stretch_changed.connect(self._on_ratio_changed)
        root.addWidget(self.preview, stretch=1)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        reset_btn = QPushButton('RESET TO SOURCE')
        reset_btn.setObjectName('stretchDialogSecondary')
        reset_btn.clicked.connect(self._reset)
        controls.addWidget(reset_btn)
        controls.addStretch()
        cancel_btn = QPushButton('CANCEL')
        cancel_btn.setObjectName('stretchDialogSecondary')
        cancel_btn.clicked.connect(self.reject)
        controls.addWidget(cancel_btn)
        apply_btn = QPushButton('APPLY STRETCH')
        apply_btn.setObjectName('stretchDialogApply')
        apply_btn.clicked.connect(self.accept)
        controls.addWidget(apply_btn)
        root.addLayout(controls)

        self.setStyleSheet(f'''
            QDialog {{ background-color: {Colors.SHELL_BG}; }}
            QLabel#stretchDialogTitle {{
                color: {Colors.TEXT}; font-family: {Fonts.DISPLAY}; font-size: 13px;
                font-weight: bold; letter-spacing: 2px; background: transparent;
            }}
            QLabel#stretchDialogDimensions {{
                color: {Colors.ACCENT}; font-family: {Fonts.BODY}; font-size: 11px;
                background: transparent;
            }}
            QPushButton#stretchDialogSecondary, QPushButton#stretchDialogApply {{
                min-height: 34px; padding: 0 16px; border-radius: 0px;
                font-family: {Fonts.DISPLAY}; font-size: 10px; font-weight: bold;
                letter-spacing: 1px;
            }}
            QPushButton#stretchDialogSecondary {{
                background: transparent; border: 1px solid {Colors.BORDER_HI};
                color: {Colors.TEXT};
            }}
            QPushButton#stretchDialogSecondary:hover {{
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
            QPushButton#stretchDialogApply {{
                background: {Colors.ACCENT}; border: 1px solid {Colors.ACCENT};
                color: {Colors.BG};
            }}
            QPushButton#stretchDialogApply:hover {{ background: {Colors.TEXT}; }}
        ''')
        install_fthr_titlebar(self, 'Stretch video')
        self._update_dimensions()

    def _extract_preview_frame(self, clip_path: str, position_ms: int,
                               crop_rect) -> QPixmap:
        try:
            cap = cv2.VideoCapture(clip_path)
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0, int(position_ms)))
            ok, frame = cap.read()
            if not ok or frame is None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return QPixmap()
            if crop_rect:
                x, y, w, h = crop_rect
                frame_h, frame_w = frame.shape[:2]
                frame = frame[max(0, y):min(frame_h, y + h),
                              max(0, x):min(frame_w, x + w)]
            if frame.size == 0:
                return QPixmap()
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).copy()
            height, width = rgb.shape[:2]
            image = QImage(
                rgb.data, width, height, rgb.strides[0],
                QImage.Format.Format_RGB888).copy()
            return QPixmap.fromImage(image)
        except (cv2.error, OSError, RuntimeError, TypeError, ValueError) as error:
            print(f'[StretchDialog] preview frame unavailable: {error}')
            return QPixmap()

    def _on_ratio_changed(self, ratio: float):
        self.stretch_ratio = ratio
        self._update_dimensions()

    def _reset(self):
        self.stretch_ratio = 1.0
        self.preview.set_ratio(1.0)
        self._update_dimensions()

    def _update_dimensions(self):
        target_w = max(2, int(self._base_w * self.stretch_ratio)) & ~1
        target_h = max(2, self._base_h) & ~1
        self._dimensions.setText(
            f'{self.stretch_ratio:.2f}×  ·  {target_w} × {target_h}')


# _DragZone — proper QWidget subclass that initiates a file drag

class _DragZone(QWidget):
    """Transparent overlay that initiates a QDrag on mouse-move (>10px threshold)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._file_path  = None
        self._drag_start = None

    def set_file(self, path: str):
        self._file_path = path
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._file_path:
            self._drag_start = event.pos()

    def mouseMoveEvent(self, event):
        if not self._file_path or not self._drag_start:
            return
        if (event.pos() - self._drag_start).manhattanLength() < 10:
            return
        self._drag_start = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(self._file_path)])
        drag.setMimeData(mime)
        # Ghost pixmap shown while dragging
        px = QPixmap(100, 56)
        px.fill(QColor(Colors.SURFACE_2))
        p = QPainter(px)
        p.setPen(QPen(QColor(Colors.TEXT_DIM)))
        p.setFont(QFont(Fonts.DISPLAY_FAMILY, 7, QFont.Weight.Bold))
        p.drawText(px.rect(), Qt.AlignmentFlag.AlignCenter, 'FTHRClips')
        p.end()
        drag.setPixmap(px)
        drag.setHotSpot(QPoint(50, 28))
        drag.exec(Qt.DropAction.CopyAction)

    def mouseReleaseEvent(self, event):
        self._drag_start = None


# CropDialog — frame display with CropOverlay

class CropDialog(QDialog):
    """Show first frame of clip; user creates/adjusts crop with 8-handle net."""

    def __init__(self, clip_path: str, initial_crop=None, parent=None):
        super().__init__(parent)
        self.clip_path     = clip_path
        self.crop_rect     = None
        self._initial_crop = initial_crop
        self._src_w        = 1
        self._src_h        = 1
        self._offset_x     = 0
        self._offset_y     = 0
        self._pixmap       = None

        self.setWindowTitle('FTHR — SET CROP')
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setModal(True)
        self.setMinimumSize(900, 580)
        self.setStyleSheet(f'QDialog {{ background-color: {Colors.BG}; }}')

        self._build_ui()
        self._load_frame()   # reads frame, stores self._pixmap (no display yet)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        hdr_frame = QFrame()
        hdr_frame.setFixedHeight(40)
        hdr_frame.setStyleSheet(
            f'background-color: {Colors.SHELL_BG};'
            f' border-bottom: 1px solid {Colors.TEXT};')
        hdr = QHBoxLayout(hdr_frame)
        hdr.setContentsMargins(20, 0, 12, 0)
        title = QLabel('CROP')
        title.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: 9px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; '
            'background: transparent;')
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton('✕')
        close_btn.setFixedSize(40, 40)
        close_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; border: none;'
            f' color: {Colors.TEXT}; font-size: 13px; }}'
            f'QPushButton:hover {{ color: {Colors.ERROR}; }}')
        close_btn.clicked.connect(self.reject)
        hdr.addWidget(close_btn)
        root.addWidget(hdr_frame)

        instr = QLabel(
            'DRAG HANDLES TO RESIZE  ·  DRAG INSIDE TO MOVE  ·  16:9 / 9:16 FOR REFERENCE FRAME')
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        instr.setFixedHeight(28)
        instr.setStyleSheet(f'color: {Colors.ACCENT}; font-size: 8px; '
                            f'font-family: {Fonts.DISPLAY}; '
                            f'letter-spacing: 1px; background: {Colors.BG};')
        root.addWidget(instr)

        self.frame_label = QLabel()
        self.frame_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_label.setStyleSheet(
            f'background-color: {Colors.BG};')
        self.frame_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self.frame_label, stretch=1)

        # Overlay child of frame_label — starts 1×1, sized properly in _sync_overlay
        self._overlay = CropOverlay(self.frame_label)
        self._overlay.setGeometry(0, 0, 1, 1)
        self._overlay.crop_changed.connect(self._on_overlay_changed)

        bot = QFrame()
        bot.setFixedHeight(48)
        bot.setStyleSheet(
            f'background-color: {Colors.SHELL_BG};'
            f' border-top: 1px solid {Colors.TEXT};')
        bot_lay = QHBoxLayout(bot)
        bot_lay.setContentsMargins(16, 0, 16, 0)
        bot_lay.setSpacing(10)

        self.info_lbl = QLabel('')
        self.info_lbl.setStyleSheet(
            f'color: {Colors.ACCENT}; font-size: 9px; '
            f'font-family: {Fonts.DISPLAY}; background: transparent;')
        bot_lay.addWidget(self.info_lbl)
        bot_lay.addStretch()

        # Reference-aspect buttons — instantly create a centered crop with a
        # standard ratio. Useful when targeting widescreen (16:9) or vertical
        # short-form / mobile (9:16) destinations.
        ratio_btn_qss = (
            f'QPushButton {{ background: transparent; border: 1px solid {Colors.TEXT}; '
            f'color: {Colors.TEXT}; font-size: 9px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 1px; }}'
            f'QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}'
        )

        ratio_16_9 = QPushButton('16:9')
        ratio_16_9.setFixedSize(60, 30)
        ratio_16_9.setToolTip('Set crop to a centered 16:9 reference frame')
        ratio_16_9.setStyleSheet(ratio_btn_qss)
        ratio_16_9.clicked.connect(lambda: self._set_aspect_ratio(16, 9))
        bot_lay.addWidget(ratio_16_9)

        ratio_9_16 = QPushButton('9:16')
        ratio_9_16.setFixedSize(60, 30)
        ratio_9_16.setToolTip('Set crop to a centered 9:16 reference frame')
        ratio_9_16.setStyleSheet(ratio_btn_qss)
        ratio_9_16.clicked.connect(lambda: self._set_aspect_ratio(9, 16))
        bot_lay.addWidget(ratio_9_16)

        clear_btn = QPushButton('CLEAR')
        clear_btn.setFixedSize(80, 30)
        clear_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; border: 1px solid {Colors.TEXT}; color: {Colors.TEXT};'
            f' font-size: 9px; font-weight: bold; font-family: {Fonts.DISPLAY}; letter-spacing: 1px; }}'
            f'QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}')
        clear_btn.clicked.connect(self._clear_crop)
        bot_lay.addWidget(clear_btn)

        apply_btn = QPushButton('APPLY CROP')
        apply_btn.setFixedSize(110, 30)
        apply_btn.setStyleSheet(
            f'QPushButton {{ background: {Colors.ACCENT}; border: none; color: {Colors.BG};'
            f' font-size: 9px; font-weight: bold; font-family: {Fonts.DISPLAY}; letter-spacing: 1px; }}'
            f'QPushButton:hover {{ background: {Colors.TEXT}; }}')
        apply_btn.clicked.connect(self._apply)
        bot_lay.addWidget(apply_btn)
        root.addWidget(bot)

    def _load_frame(self):
        """Read first frame; store pixmap without displaying (size not known yet)."""
        cap = cv2.VideoCapture(self.clip_path)
        ok, frame = cap.read()
        cap.release()
        if not ok or frame is None:
            self.frame_label.setText('Could not load frame')
            return
        self._src_h, self._src_w = frame.shape[:2]
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w      = frame_rgb.shape[:2]
        # Copy data before array goes out of scope
        qimg = QImage(frame_rgb.copy().data, w, h, w * 3, QImage.Format.Format_RGB888)
        self._pixmap = QPixmap.fromImage(qimg)

    def showEvent(self, event):
        """Called after Qt has computed final widget sizes — safe to sync overlay now."""
        super().showEvent(event)
        self._sync_overlay()

    def _sync_overlay(self):
        if self._pixmap is None:
            return
        lw, lh = self.frame_label.width(), self.frame_label.height()
        if lw <= 0 or lh <= 0:
            return

        scaled = self._pixmap.scaled(QSize(lw, lh),
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        self.frame_label.setPixmap(scaled)

        sw, sh         = scaled.width(), scaled.height()
        self._offset_x = (lw - sw) // 2
        self._offset_y = (lh - sh) // 2

        self._overlay.setGeometry(0, 0, lw, lh)
        self._overlay.raise_()
        self._overlay.set_bounds(QRect(self._offset_x, self._offset_y, sw, sh))

        if self._initial_crop:
            x, y, w, h = self._initial_crop
            sc = sw / max(self._src_w, 1)
            self._overlay.set_crop(QRect(
                self._offset_x + int(x * sc),
                self._offset_y + int(y * sc),
                int(w * sc), int(h * sc),
            ))
            self.crop_rect = self._initial_crop
            self.info_lbl.setText(f'{w} × {h}  at  ({x}, {y})')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_overlay()

    def _on_overlay_changed(self, rect: QRect):
        if self._pixmap is None:
            return
        lw, lh = self.frame_label.width(), self.frame_label.height()
        scaled = self._pixmap.scaled(QSize(lw, lh),
                                     Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
        sw, sh = scaled.width(), scaled.height()
        ox, oy = self._offset_x, self._offset_y
        x1 = max(0, int((rect.left()   - ox) * self._src_w / max(sw, 1)))
        y1 = max(0, int((rect.top()    - oy) * self._src_h / max(sh, 1)))
        x2 = min(self._src_w, int((rect.right()  - ox) * self._src_w / max(sw, 1)))
        y2 = min(self._src_h, int((rect.bottom() - oy) * self._src_h / max(sh, 1)))
        w, h = x2 - x1, y2 - y1
        if w > 4 and h > 4:
            self.crop_rect = (x1, y1, w, h)
            self.info_lbl.setText(f'{w} × {h}  at  ({x1}, {y1})')
        else:
            self.crop_rect = None

    def _clear_crop(self):
        self._overlay.clear()
        self.crop_rect = None
        self.info_lbl.setText('')
        self._initial_crop = None   # don't re-populate on next resize

    def _set_aspect_ratio(self, aspect_w: int, aspect_h: int):
        """Snap the crop to a centered rectangle with the given aspect ratio.

        Sized to fill the source frame as much as possible while preserving
        the ratio — i.e. inscribed inside the displayed frame, centered.
        """
        if self._pixmap is None:
            return
        bounds = self._overlay._bounds
        if bounds.isNull() or bounds.width() <= 4 or bounds.height() <= 4:
            return

        target_ratio = aspect_w / aspect_h
        bw, bh       = bounds.width(), bounds.height()
        bounds_ratio = bw / bh

        if target_ratio > bounds_ratio:
            # Wider target than bounds — limited by width
            cw = bw
            ch = max(4, int(round(cw / target_ratio)))
        else:
            # Taller target than bounds — limited by height
            ch = bh
            cw = max(4, int(round(ch * target_ratio)))

        cx = bounds.x() + (bw - cw) // 2
        cy = bounds.y() + (bh - ch) // 2

        new_rect = QRect(cx, cy, cw, ch)
        self._overlay.set_crop(new_rect)
        self._on_overlay_changed(new_rect)

    def _apply(self):
        self.accept()


# ShareModeDialog — share mode picker (frameless QDialog, same pattern as ShareWindow)

class ShareModeDialog(QDialog):
    """
    Small frameless dialog that appears centered over the ClipViewer.
    User picks FULL QUALITY or 10 MB · DISCORD; emits mode_selected then closes.
    """
    mode_selected = Signal(bool)   # True = discord 10 MB, False = full quality
    preset_selected = Signal(object)

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent,
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.Tool)
        self._diagnostic_started_at = time.monotonic()
        emit_event('export', 'requested', state='REQUESTED', kind='share')
        self._sm = settings_manager
        self._preset_manager = ExportPresetManager()
        self.setFixedSize(300, 224)
        self.setStyleSheet(
            f'QDialog {{ background-color: {Colors.BG}; '
            f'border: 1px solid {Colors.BORDER_HI}; border-radius: 0px; }}')
        self._win_drag_pos = None
        self._build_ui()
        self._center_on_parent()

    def _center_on_parent(self):
        if self.parent():
            pg = self.parent().frameGeometry()
            self.move(pg.center() - self.rect().center())
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center() - self.rect().center())

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 16)
        root.setSpacing(10)

        # Header
        hdr = QHBoxLayout()
        hdr.setSpacing(0)
        title = QLabel('SHARE AS')
        title.setStyleSheet(
            f'color: {Colors.TEXT_DIM}; font-size: 8px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 3px; '
            'background: transparent;'
        )
        hdr.addWidget(title)
        hdr.addStretch()
        x_btn = QPushButton('✕')
        x_btn.setFixedSize(22, 22)
        x_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; border: none; '
            f'color: {Colors.TEXT_MUTED}; font-size: 11px; }}'
            f'QPushButton:hover {{ color: {Colors.ERROR}; }}'
        )
        x_btn.clicked.connect(self.close)
        hdr.addWidget(x_btn)
        root.addLayout(hdr)

        # Full quality button
        fq_btn = QPushButton('FULL QUALITY')
        fq_btn.setFixedHeight(40)
        fq_btn.setStyleSheet(
            f'QPushButton {{ background-color: {Colors.TEXT}; border: none; '
            f'border-radius: 0px; color: {Colors.BG}; font-size: 10px; '
            f'font-weight: bold; font-family: {Fonts.DISPLAY}; '
            'letter-spacing: 1px; }'
            f'QPushButton:hover {{ background-color: {Colors.ACCENT}; }}'
        )
        fq_btn.clicked.connect(lambda: self._select(False))
        root.addWidget(fq_btn)

        # Discord button
        dc_btn = QPushButton('10 MB  ·  DISCORD')
        dc_btn.setFixedHeight(40)
        dc_btn.setStyleSheet(
            f'QPushButton {{ background-color: transparent; border: 1px solid '
            f'{Colors.BORDER}; border-radius: 0px; color: {Colors.TEXT_DIM}; '
            f'font-size: 10px; font-weight: bold; font-family: {Fonts.DISPLAY}; '
            'letter-spacing: 1px; }'
            f'QPushButton:hover {{ border-color: {Colors.TEXT}; '
            f'color: {Colors.TEXT}; }}'
        )
        dc_btn.clicked.connect(lambda: self._select(True))
        root.addWidget(dc_btn)

        custom = [preset for preset in self._preset_manager.custom()]
        self._preset_combo = None
        if custom:
            preset_row = QHBoxLayout()
            self._preset_combo = WheelSafeComboBox()
            for preset in custom:
                self._preset_combo.addItem(preset.name, preset.preset_id)
            selected = str(self._sm.get(
                'selected_export_preset', '') if self._sm else '')
            index = self._preset_combo.findData(selected)
            if index >= 0:
                self._preset_combo.setCurrentIndex(index)
            self._preset_combo.setStyleSheet(combo_qss())
            self._preset_combo.setFixedHeight(34)
            preset_row.addWidget(self._preset_combo, 1)
            use = QPushButton('USE')
            use.setFixedSize(88, 34)
            use.setStyleSheet(button_primary_qss())
            use.clicked.connect(self._select_custom)
            preset_row.addWidget(use)
            root.addLayout(preset_row)

    def _select(self, discord_mode: bool):
        self.mode_selected.emit(discord_mode)
        self.close()

    def _select_custom(self):
        if self._preset_combo is None:
            return
        preset_id = str(self._preset_combo.currentData() or '')
        preset = self._preset_manager.get(preset_id)
        if preset is None:
            return
        if self._sm:
            self._sm.set('selected_export_preset', preset_id)
            self._sm.save_settings()
        self.preset_selected.emit(preset)
        self.close()

    # Allow dragging the dialog by clicking anywhere on it
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._win_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._win_drag_pos and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._win_drag_pos)

    def mouseReleaseEvent(self, event):
        self._win_drag_pos = None


# ShareWindow — centered modal-ish dialog: export + drag-to-share + watermark

class ShareWindow(QDialog):
    """Non-modal export dialog with a draggable completed clip thumbnail.

    Closing during export cancels the process. The ready preview animates
    the FTHRClips watermark.
    """

    _export_sig  = Signal(bool, str)
    _plan_sig = Signal(str)
    export_error = Signal(str, str, str)   # title, detail, level

    def __init__(self, clip_path: str, start_s: float, end_s: float,
                 crop_rect, settings_info: dict, discord_mode: bool = False,
                 source_volumes: dict | None = None,
                 source_mutes: dict | None = None,
                 master_volume: int = 100,
                 playback_sources: tuple[PlaybackSource, ...] = (),
                 multitrack_audio: bool = False,
                 audio_tracks: tuple[tuple[str, int], ...] = (),
                 segments: list[tuple[float, float]] | None = None,
                 effects: dict | None = None,
                 stretch_ratio: float = 1.0,
                 speed_rate: float = 1.0,
                 preserve_pitch: bool = True,
                 export_preset: ExportPreset | None = None, parent=None):
        super().__init__(parent,
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.Tool)
        self._clip_path          = clip_path
        self.clip_path           = clip_path  # shared editor command builder
        self._start_s            = start_s
        self._end_s              = end_s
        self._crop_rect          = crop_rect
        self._segments           = list(segments or [(start_s, end_s)])
        self._effects            = dict(effects or {})
        self._stretch_ratio      = max(0.25, min(4.0, float(stretch_ratio)))
        self._speed_rate         = _normalise_speed(speed_rate)
        self._preserve_pitch     = bool(preserve_pitch)
        self._settings           = settings_info
        self._watermark_enabled  = bool(
            settings_info.get('watermark_enabled', False))
        self._export_path        = None
        self._pending_export_out = None  # set before Popen, cleared after success
        self._proc               = None
        self._export_job         = None
        self._export_thread      = None
        self._export_cancel      = threading.Event()
        self._cancelled          = False
        self._win_drag_pos       = None
        self._export_preset = export_preset or (
            DISCORD_PRESET if discord_mode else None)
        self._discord_mode = bool(
            discord_mode or (self._export_preset and
                             self._export_preset.preset_id == 'discord'))
        self._popup_anim   = None
        self._source_volumes = source_volumes or {}
        self._source_mutes = source_mutes or {}
        self._master_volume = master_volume
        self._playback_sources = tuple(playback_sources)
        self._preserve_tracks = not discord_mode
        # Clip-local semantic keys and FFmpeg audio-stream positions. This is
        # intentionally supplied by the clip manifest, never by fixed legacy
        # categories or by currently-running processes.
        self._audio_tracks = tuple(audio_tracks)
        self._multitrack_audio = bool(multitrack_audio and self._audio_tracks)

        self.setFixedSize(340, 242)
        self._build_ui()
        self._export_sig.connect(self._on_export_done)
        self._plan_sig.connect(self._on_export_plan)

        # Extract and show thumbnail before starting export
        thumb = self._make_thumbnail()
        if thumb:
            self._thumb_lbl.setPixmap(
                thumb.scaled(self._thumb_lbl.size(),
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation))
        self._center_on_parent()

    def _center_on_parent(self):
        if self.parent():
            pg = self.parent().frameGeometry()
            self.move(pg.center() - self.rect().center())
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.center() - self.rect().center())

    def _make_thumbnail(self) -> QPixmap | None:
        """Extract a frame at ~start_s+0.5s, applying crop if set."""
        try:
            cap = cv2.VideoCapture(self._clip_path)
            fps   = cap.get(cv2.CAP_PROP_FPS) or 30
            total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            target = min(int((self._start_s + 0.5) * fps), max(0, int(total) - 1))
            cap.set(cv2.CAP_PROP_POS_FRAMES, target)
            ok, frame = cap.read()
            cap.release()
            if not ok or frame is None:
                return None
            if self._crop_rect:
                x, y, w, h = self._crop_rect
                fh, fw = frame.shape[:2]
                x2 = min(fw, x + w)
                y2 = min(fh, y + h)
                frame = frame[max(0,y):y2, max(0,x):x2]
            if frame.size == 0:
                return None
            if abs(self._stretch_ratio - 1.0) > 0.005:
                fh, fw = frame.shape[:2]
                frame = cv2.resize(
                    frame, (max(2, int(fw * self._stretch_ratio)), fh),
                    interpolation=cv2.INTER_LINEAR)
            fh, fw = frame.shape[:2]
            scale  = min(310 / max(fw, 1), 174 / max(fh, 1))
            nw, nh = max(1, int(fw * scale)), max(1, int(fh * scale))
            frame  = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
            rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB).copy()
            qimg   = QImage(rgb.data, nw, nh, nw * 3, QImage.Format.Format_RGB888)
            return QPixmap.fromImage(qimg)
        except Exception:
            return None

    def _build_ui(self):
        self.setStyleSheet(f'QDialog {{ background-color: {Colors.BG}; }}')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header (window drag handle)
        hdr = QFrame()
        hdr.setFixedHeight(38)
        hdr.setObjectName('swHdr')
        hdr.setStyleSheet(
            f'#swHdr {{ background-color: {Colors.SURFACE_2};'
            f' border-bottom: 1px solid {Colors.BORDER}; }}')
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(16, 0, 8, 0)

        title_text = (
            f'SHARE  ·  {self._export_preset.name.upper()}'
            if self._export_preset else 'SHARE CLIP')
        title_lbl = QLabel(title_text)
        title_lbl.setStyleSheet(
            f'color: {Colors.TEXT_MUTED}; font-size: 8px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; '
            'background: transparent;')
        hdr_lay.addWidget(title_lbl)
        hdr_lay.addStretch()

        self._status_badge = QLabel('EXPORTING...')
        self._status_badge.setStyleSheet(
            f'color: {Colors.TEXT_GHOST}; font-size: 7px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; '
            'background: transparent;')
        hdr_lay.addWidget(self._status_badge)
        hdr_lay.addSpacing(8)

        close_btn = QPushButton('✕')
        close_btn.setFixedSize(30, 30)
        close_btn.setStyleSheet(
            f'QPushButton {{ background: transparent; border: none;'
            f' color: {Colors.TEXT_MUTED}; font-size: 11px; }}'
            f'QPushButton:hover {{ color: {Colors.ERROR}; }}')
        close_btn.clicked.connect(self.close)
        hdr_lay.addWidget(close_btn)
        root.addWidget(hdr)

        # Thumbnail area (also the drag zone once ready)
        self._drag_zone = _DragZone(self)
        self._drag_zone.setFixedHeight(174)
        self._drag_zone.setStyleSheet(
            f'background-color: {Colors.SURFACE_1};')

        dz_inner = QVBoxLayout(self._drag_zone)
        dz_inner.setContentsMargins(0, 0, 0, 0)
        dz_inner.setSpacing(0)

        self._thumb_lbl = QLabel()
        self._thumb_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb_lbl.setFixedHeight(174)
        self._thumb_lbl.setStyleSheet(
            f'background-color: {Colors.SURFACE_1};')
        dz_inner.addWidget(self._thumb_lbl)

        # "DRAG TO SHARE" hint overlay on thumbnail (bottom strip)
        self._drag_hint = QLabel('⊡  DRAG TO SHARE', self._drag_zone)
        self._drag_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._drag_hint.setFixedHeight(28)
        self._drag_hint.setStyleSheet(
            f'background-color: {_theme_rgba(Colors.BG, 180)}; '
            f'color: {Colors.TEXT_GHOST}; '
            f'font-size: 8px; font-weight: bold; font-family: {Fonts.DISPLAY}; '
            'letter-spacing: 2px;')
        self._drag_hint.hide()

        root.addWidget(self._drag_zone)

        # Filename bar
        fname_bar = QFrame()
        fname_bar.setFixedHeight(30)
        fname_bar.setStyleSheet(
            f'background-color: {Colors.SURFACE_1};'
            f' border-top: 1px solid {Colors.HAIRLINE};')
        fb_lay = QHBoxLayout(fname_bar)
        fb_lay.setContentsMargins(14, 0, 14, 0)

        self._fname_lbl = QLabel('Preparing...')
        self._fname_lbl.setStyleSheet(
            f'color: {Colors.TEXT_GHOST}; font-size: 8px; '
            f'font-family: {Fonts.DISPLAY}; background: transparent;')
        fb_lay.addWidget(self._fname_lbl)
        fb_lay.addStretch()
        root.addWidget(fname_bar)

        # FTHRClips corner popup (slides in over thumbnail when ready)
        self._popup_lbl = QLabel('FTHRClips', self._drag_zone)
        self._popup_lbl.setStyleSheet(
            f'background-color: {_theme_rgba(Colors.BG, 170)}; '
            f'color: {Colors.TEXT}; '
            f'font-size: 9px; font-weight: bold; font-family: {Fonts.DISPLAY}; '
            'letter-spacing: 1px; padding: 4px 10px;')
        self._popup_lbl.adjustSize()
        _ph = self._popup_lbl.height()
        self._popup_lbl.move(340, 174 - _ph - 10)   # start off-screen right

    # Window dragging

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._win_drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._win_drag_pos and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._win_drag_pos)

    def mouseReleaseEvent(self, event):
        self._win_drag_pos = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep drag-hint pinned to bottom of thumbnail
        if hasattr(self, '_drag_hint'):
            self._drag_hint.setGeometry(0,
                                        self._drag_zone.height() - self._drag_hint.height(),
                                        self._drag_zone.width(),
                                        self._drag_hint.height())

    # Export

    def showEvent(self, event):
        super().showEvent(event)
        # Pin drag-hint geometry once sizes are known
        self._drag_hint.setGeometry(0,
                                    self._drag_zone.height() - self._drag_hint.height(),
                                    self._drag_zone.width(),
                                    self._drag_hint.height())
        # Start export — exactly once. showEvent fires again on minimize/
        # restore on some platforms, which would launch a second concurrent
        # ffmpeg run writing the same output file.
        if not getattr(self, '_export_started', False):
            self._export_started = True
            self._export_thread = threading.Thread(
                target=self._export_worker, name='fthr-share-export', daemon=True)
            self._export_thread.start()

    def _build_audio_filter_chain(self) -> tuple[list[str], str | None]:
        """Return (filter snippets, output label) for the per-source audio mix.

        Returns ([], None) if multi-track audio isn't present so the caller
        knows to skip the filter and fall through to a stream-copy or
        single-encode path.
        """
        if not self._multitrack_audio or self._preserve_tracks:
            return [], None
        states = {
            source.source_id: SourceMixState(
                gain_percent=self._source_volumes.get(source.source_id, 100),
                muted=self._source_mutes.get(source.source_id, False))
            for source in self._playback_sources
        }
        return ffmpeg_mix_filter(self._playback_sources, states, self._master_volume)

    def _export_worker(self):
        """Run share preparation/export and surface unexpected failures."""
        try:
            self._export_worker_impl()
        except Exception as error:
            staged = getattr(self, '_pending_export_out', None)
            if staged:
                discard_staged_output(staged)
                self._pending_export_out = None
            self._export_job = None
            detail = f'{type(error).__name__}: {error}'
            emit_event('export', 'process_failed', state='FAILED', kind='share',
                       error=DiagnosticError.EXPORT_PROCESS_FAILED,
                       detail=detail)
            if not self._cancelled:
                self._export_sig.emit(
                    False, 'Export cancelled' if getattr(
                        self, '_export_cancel', threading.Event()).is_set()
                    else detail)

    def _export_worker_impl(self):
        emit_event('export', 'preparing', state='PREPARING', kind='share')
        try:
            ffmpeg = get_ffmpeg_exe()
        except FFmpegUnavailable as e:
            emit_event(
                'export', 'init_failed', state='FAILED',
                error=DiagnosticError.EXPORT_INIT_FAILED,
                kind='share', detail=str(e))
            self._export_sig.emit(False, str(e))
            self.export_error.emit(
                'FFMPEG NOT FOUND',
                'ffmpeg is required for export. It normally ships with FTHR Clips; '
                'if you are running from source, install it: '
                'sudo pacman -S ffmpeg (Arch) or sudo apt install ffmpeg (Debian).',
                'error',
            )
            return

        stem      = Path(self._clip_path).stem
        share_dir = clips_directory_from(self._settings) / 'Shared'
        try:
            share_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # An uncaught error here kills the worker thread silently and the
            # window sits on 'EXPORTING…' forever.
            self._export_sig.emit(False, f'Cannot create {share_dir}: {e}')
            emit_event(
                'export', 'init_failed', state='FAILED',
                error=DiagnosticError.EXPORT_INIT_FAILED,
                kind='share', detail=f'{type(e).__name__}: {e}')
            return
        from datetime import datetime as _dt
        ts        = _dt.now().strftime('%H-%M-%S')
        suffix    = f'_discord_{ts}.mp4' if self._discord_mode else f'_share_{ts}.mp4'
        out_p     = share_dir / f'{stem}{suffix}'
        n = 2
        while out_p.exists():   # same-second re-export must not overwrite (-y)
            out_p = share_dir / f'{stem}{suffix[:-4]}_{n}.mp4'
            n += 1
        out       = str(out_p)
        staged    = str(create_staged_output_path(out_p))
        self._pending_export_out = staged
        cr        = self._crop_rect
        source_duration_s = max(sum(end - start for start, end in self._segments), 0.1)
        duration_s = _speed_adjusted_duration(source_duration_s, self._speed_rate)

        has_timeline_edits = len(self._segments) > 1
        # Watermarking is an export-time look edit. Treating it as such forces
        # a video encode even when the selected clip range could otherwise be
        # copied, and resets the animation to this shared video's time zero.
        has_look_edits = (
            any(self._effects.values()) or self._watermark_enabled)
        has_stretch = abs(self._stretch_ratio - 1.0) > 0.005
        has_speed = abs(self._speed_rate - 1.0) > 1e-6

        if self._export_preset is not None:
            try:
                source_info = probe_media(self._clip_path)
                export_plan = plan_export(
                    source_info, self._export_preset, duration_s=duration_s)
            except Exception as exc:
                discard_staged_output(staged)
                self._export_sig.emit(False, f'Media inspection failed: {exc}')
                return
            self._plan_sig.emit(export_plan.summary)
            video_args = [
                *size_constrained_video_args(
                    bitrate_kbps=export_plan.video_bitrate_kbps, ffmpeg=ffmpeg),
                '-maxrate', f'{int(export_plan.video_bitrate_kbps * 1.15)}k',
                '-bufsize', f'{export_plan.video_bitrate_kbps * 2}k',
            ]
            extra_filters = (
                plan_video_filters(export_plan)
                if export_plan.reencode_video else [])
            cmd = ClipViewer._build_export_cmd(
                self, ffmpeg, self._start_s, self._end_s - self._start_s,
                staged, cr, video_args, segments=self._segments,
                effects=self._effects, stretch_ratio=self._stretch_ratio,
                audio_bitrate=f'{export_plan.audio_bitrate_kbps}k',
                extra_video_filters=extra_filters,
                force_video_encode=export_plan.reencode_video,
                force_audio_encode=export_plan.reencode_audio)
        elif has_timeline_edits or has_look_edits or has_stretch or has_speed:
            cmd = ClipViewer._build_export_cmd(
                self, ffmpeg, self._start_s, self._end_s - self._start_s,
                staged, cr, maximum_quality_video_args(ffmpeg),
                segments=self._segments,
                effects=self._effects, stretch_ratio=self._stretch_ratio,
                audio_bitrate='320k')
        else:
            audio_filters, audio_out = self._build_audio_filter_chain()
            if cr or audio_out:
            # Crop or mix needed — re-encode the affected stream(s).
                filters = []
                if cr:
                    x, y, w, h = cr
                    w &= ~1; h &= ~1
                    filters.append(f'[0:v]crop={w}:{h}:{x}:{y}[vout]')
                filters += audio_filters

                cmd = [ffmpeg, '-y',
                       '-ss', str(self._start_s), '-i', self._clip_path,
                       '-t', str(duration_s),
                       '-filter_complex', ';'.join(filters),
                       '-map', '[vout]' if cr else '0:v:0',
                       '-map', audio_out if audio_out else '0:a?']
                if cr:
                    cmd += maximum_quality_video_args(ffmpeg)
                else:
                    cmd += ['-c:v', 'copy']
                if audio_out:
                    cmd += ['-c:a', 'aac', '-b:a', '320k']
                else:
                    cmd += ['-c:a', 'copy']
                cmd.append(staged)
            else:
                cmd = [ffmpeg, '-y',
                       '-ss', str(self._start_s), '-i', self._clip_path,
                       '-t', str(duration_s),
                       # FFmpeg's automatic stream choice keeps every audio stem.
                       '-map', '0:v?', '-map', '0:a?', '-c', 'copy', staged]

        def _validate(path: Path, cancel_event: threading.Event) -> None:
            _validate_export_output(path, ffmpeg, cancel_event)
            if self._export_preset is not None:
                size = path.stat().st_size
                limit = self._export_preset.target_size_mb * 1024 * 1024
                if size > limit:
                    raise RuntimeError(
                        f'Output is {size / (1024 * 1024):.1f} MB, above the '
                        f'{self._export_preset.target_size_mb:g} MB target')

        def _state_changed(state: ExportState) -> None:
            events = {
                ExportState.EXPORTING: ('process_started', 'PROCESS_STARTED'),
                ExportState.FINALIZING: ('finalizing', 'FINALIZING'),
                ExportState.COMPLETED: ('completed', 'COMPLETED'),
                ExportState.FAILED: ('process_failed', 'FAILED'),
                ExportState.CANCELLED: ('cancelled', 'CANCELLED'),
                ExportState.TIMED_OUT: ('stalled', 'TIMED_OUT'),
            }
            event = events.get(state)
            if event:
                emit_event('export', event[0], state=event[1], kind='share',
                           error=(DiagnosticError.EXPORT_STALLED
                                  if state is ExportState.TIMED_OUT else None))

        job = ExportJob(
            cmd, staged, out,
            validate_output=_validate,
            commit_output=commit_staged_output,
            state_callback=_state_changed,
            popen_kwargs=_NO_WINDOW,
            inactivity_timeout=120.0,
            cancel_event=getattr(self, '_export_cancel', None),
        )
        self._export_job = job
        self._proc = None
        result = job.run(duration=duration_s)
        self._proc = None
        self._pending_export_out = None
        output_exists = os.path.isfile(out)
        emit_event(
            'export', 'terminal', state=result.state.value, kind='share',
            elapsed_ms=round(result.elapsed_seconds * 1000),
            exit_code=result.returncode,
            stderr_tail=result.stderr_tail,
            output_exists=output_exists,
            output_size_bytes=(os.path.getsize(out) if output_exists else 0))
        if result.state is ExportState.COMPLETED:
            self._export_sig.emit(True, out)
        elif not self._cancelled:
            detail = result.detail
            if result.stderr_tail:
                detail = next((line for line in reversed(
                    result.stderr_tail.splitlines()) if line.strip()), detail)
            self._export_sig.emit(False, detail or 'Export failed')

    def _on_export_plan(self, summary: str):
        if self._cancelled:
            return
        self._fname_lbl.setText(summary)
        self._fname_lbl.setToolTip(summary)

    def _on_export_done(self, success: bool, msg: str):
        if self._cancelled:
            return
        if success:
            self._export_path = msg
            fname = Path(msg).name
            if len(fname) > 42:
                fname = fname[:40] + '…'
            self._fname_lbl.setText(fname)
            self._fname_lbl.setStyleSheet(
                f'color: {Colors.TEXT_MUTED}; font-size: 8px; '
                f'font-family: {Fonts.DISPLAY}; background: transparent;')
            self._status_badge.setText('READY')
            self._status_badge.setStyleSheet(
                f'color: {Colors.ACCENT}; font-size: 7px; font-weight: bold; '
                f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; background: transparent;')
            self._drag_zone.set_file(self._export_path)
            self._drag_hint.show()
            self._drag_hint.raise_()
            QTimer.singleShot(300, self._animate_popup_in)
        else:
            short = msg if len(msg) <= 44 else msg[:42] + '…'
            self._fname_lbl.setText(short)
            self._fname_lbl.setStyleSheet(
                f'color: {Colors.ERROR}; font-size: 8px; '
                f'font-family: {Fonts.DISPLAY}; background: transparent;')
            self._status_badge.setText('FAILED')
            self._status_badge.setStyleSheet(
                f'color: {Colors.ERROR}; font-size: 7px; font-weight: bold; '
                f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; background: transparent;')
            self.export_error.emit(
                'EXPORT FAILED',
                'FFmpeg returned an error. The output file may be incomplete.',
                'warning',
            )

    # FTHRClips corner popup animation

    def _animate_popup_in(self):
        if self._cancelled:
            return
        lbl = self._popup_lbl
        pw, ph = lbl.width(), lbl.height()
        target_x = 340 - pw - 10
        target_y = 174 - ph - 10
        lbl.move(340, target_y)
        lbl.raise_()
        anim = QPropertyAnimation(lbl, b'pos', self)
        anim.setDuration(380)
        anim.setStartValue(QPoint(340, target_y))
        anim.setEndValue(QPoint(target_x, target_y))
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: QTimer.singleShot(2500, self._animate_popup_out))
        self._popup_anim = anim
        anim.start()

    def _animate_popup_out(self):
        if self._cancelled:
            return
        lbl = self._popup_lbl
        anim = QPropertyAnimation(lbl, b'pos', self)
        anim.setDuration(280)
        anim.setStartValue(QPoint(lbl.x(), lbl.y()))
        anim.setEndValue(QPoint(340, lbl.y()))
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._popup_anim = anim
        anim.start()

    # Cleanup on close

    def closeEvent(self, event):
        self._cancelled = True
        self._export_cancel.set()
        job = getattr(self, '_export_job', None)
        if job is not None:
            job.cancel()
        process_active = bool(
            job is not None
            and job.state not in {
                ExportState.COMPLETED, ExportState.FAILED,
                ExportState.CANCELLED, ExportState.TIMED_OUT,
            })
        emit_event(
            'export', 'close_requested',
            state='CANCELLED' if process_active else 'CLOSED', kind='share')
        worker = getattr(self, '_export_thread', None)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=2.5)
        # Remove the partially-written output file if the user closed before
        # the export finished (ffmpeg was killed mid-write above).
        pending = self._pending_export_out
        if pending:
            self._pending_export_out = None
            try:
                os.remove(pending)
            except OSError:
                pass
        # QDialog.closeEvent calls reject(). Our reject routes back through
        # close() for cleanup, so delegating there recursively leaves the
        # Share dialog visible. Finish the dialog directly after cleanup.
        QDialog.done(self, QDialog.DialogCode.Rejected)
        event.accept()

    def reject(self):
        """Route Escape/programmatic rejection through process cleanup."""

        self.close()


# VolumePopup — frameless dropdown with per-source mix sliders

class VolumePopup(QDialog):
    """
    Floating volume popup. Per-source labels are shown only when a verified
    clip-side manifest proves their stream identity.
    """

    master_changed = Signal(int)            # 0–100
    source_changed = Signal(str, int)       # (source_key, 0–100)
    source_muted = Signal(str, bool)        # (source_key, muted)

    _SOURCES = [('master', 'MASTER')]

    def __init__(self, master_vol: int,
                 source_volumes: dict | None = None,
                 source_mutes: dict | None = None,
                 source_tracks: tuple[PlaybackSource, ...] = (),
                 live_preview: bool = False,
                 parent=None):
        super().__init__(parent,
                         Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.Popup)
        self.setStyleSheet(
            f'QDialog {{ background-color: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER_HI}; }}')
        self._sliders:  dict[str, QSlider] = {}
        self._values:   dict[str, QLabel]  = {}
        self._labels:   dict[str, QLabel]  = {}
        self._icons:    dict[str, QLabel]  = {}
        self._mute_buttons: dict[str, QPushButton] = {}
        self._row_order: list[str] = []
        self._source_tracks = tuple(
            source for source in source_tracks if source.editable)
        self._source_mutes = source_mutes or {}
        self._build_ui(master_vol, source_volumes or {}, live_preview)
        hint = self.sizeHint()
        self.setFixedSize(max(520, hint.width()), hint.height())

    def _build_ui(self, master_vol: int, source_volumes: dict, live_preview: bool):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        title = QLabel('AUDIO MIX')
        title.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: 8px; font-weight: bold; '
            f'font-family: {Fonts.DISPLAY}; letter-spacing: 2px; '
            'background: transparent;')
        root.addWidget(title)

        sources = [(key, label, None) for key, label in self._SOURCES]
        sources.extend((source.source_id, self._display_label(source), source)
                       for source in self._source_tracks)
        for key, label, source in sources:
            self._row_order.append(key)
            row = QHBoxLayout()
            row.setSpacing(10)

            icon_key = source_icon_key(source) if source is not None else 'master'
            icon = QLabel()
            icon.setFixedSize(22, 22)
            icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            app_icon = (_running_windows_app_icon(source.persistent_identity)
                        if source is not None and source.source_type == 'application'
                        else None)
            if app_icon is not None:
                icon.setPixmap(app_icon.pixmap(18, 18))
                icon.setToolTip(f'{label} application icon')
            else:
                icon.setText({
                    'master': '◉', 'system': '⌁', 'microphone': '●',
                    'application': '◆', 'track': '◌',
                }[icon_key])
                icon.setToolTip(f'{icon_key.title()} audio source')
                icon.setStyleSheet(
                    f'color: {Colors.ACCENT}; font-size: 11px; '
                    'background: transparent;')
            self._icons[key] = icon
            row.addWidget(icon)

            label_width = 154
            lbl = QLabel()
            lbl.setFixedWidth(label_width)
            lbl.setStyleSheet(
                f'color: {Colors.TEXT}; font-size: 8px; font-weight: bold; '
                f'font-family: {Fonts.DISPLAY}; letter-spacing: 1px; '
                'background: transparent;')
            visible_label = QFontMetrics(lbl.font()).elidedText(
                label, Qt.TextElideMode.ElideRight, label_width)
            lbl.setText(visible_label)
            lbl.setToolTip(label)
            lbl.setAccessibleName(f'{label} source volume')
            if source is not None:
                self._labels[key] = lbl
                if not source.available:
                    lbl.setStyleSheet(
                        f'color: {Colors.TEXT_DIM}; font-size: 8px; font-weight: bold; '
                        f'font-family: {Fonts.DISPLAY}; letter-spacing: 1px; '
                        'background: transparent;')
                    lbl.setToolTip(f'{label} is unavailable in this clip.')
            row.addWidget(lbl)

            slider = ClickableSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setFixedWidth(170)
            if key == 'master':
                slider.setValue(master_vol)
            else:
                slider.setValue(int(source_volumes.get(key, 100)))
                slider.setToolTip(
                    f'{label} gain for this open clip only.')
                slider.setAccessibleName(f'{label} volume')
                slider.setEnabled(bool(source and source.available and live_preview))
            slider.valueChanged.connect(
                lambda v, k=key, lab=label: self._on_changed(k, v, lab))
            self._sliders[key] = slider
            row.addWidget(slider)

            value_lbl = QLabel(f'{slider.value()}%')
            value_lbl.setFixedWidth(36)
            value_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value_lbl.setStyleSheet(
                f'color: {Colors.ACCENT if source is None or source.available else Colors.TEXT_DIM}; font-size: 9px; '
                f'font-family: {Fonts.DISPLAY}; background: transparent;')
            if source is not None and not source.available:
                value_lbl.setText('—')
                value_lbl.setToolTip('The stored audio stream is unavailable.')
            self._values[key] = value_lbl
            row.addWidget(value_lbl)

            if source is not None:
                mute = QPushButton('MUTE')
                mute.setCheckable(True)
                mute.setChecked(bool(self._source_mutes.get(key, False)))
                mute.setText('MUTED' if mute.isChecked() else 'MUTE')
                mute.setEnabled(source.available and live_preview)
                mute.setFixedWidth(42)
                mute.setAccessibleName(f'Mute {label}')
                mute.setStyleSheet(
                    f'QPushButton {{ color: {Colors.TEXT_DIM}; background: transparent; border: none; '
                    f'font-size: 7px; font-weight: bold; }} '
                    f'QPushButton:checked {{ color: {Colors.DELETE}; }}')
                self._mute_buttons[key] = mute
                mute.toggled.connect(
                    lambda checked, k=key, button=mute: self._on_mute_toggled(k, button, checked))
                row.addWidget(mute)

            root.addLayout(row)

        self.setStyleSheet(self.styleSheet() + f'''
            QSlider::groove:horizontal {{ background: {Colors.BORDER_HI}; height: 2px; }}
            QSlider::handle:horizontal {{
                background: {Colors.ACCENT}; width: 10px; height: 10px;
                margin: -4px 0; border-radius: 0px;
            }}
            QSlider::handle:horizontal:hover {{ background: {Colors.TEXT}; }}
            QSlider::sub-page:horizontal {{ background: {Colors.ACCENT}; }}
        ''')

    @staticmethod
    def _display_label(source: PlaybackSource) -> str:
        return source_display_name(source)

    def _on_changed(self, key: str, value: int, _label: str):
        self._values[key].setText(f'{value}%')
        if key == 'master':
            self.master_changed.emit(value)
        else:
            self.source_changed.emit(key, value)

    def _on_mute_toggled(self, key: str, button: QPushButton, muted: bool):
        button.setText('MUTED' if muted else 'MUTE')
        self.source_muted.emit(key, muted)

    def show_above(self, anchor_widget: QWidget):
        """Pop up just above the anchor widget so it doesn't overlap the trim bar."""
        size = self.size()
        anchor_top = anchor_widget.mapToGlobal(QPoint(0, 0))
        x = anchor_top.x() + (anchor_widget.width() - size.width()) // 2
        y = anchor_top.y() - size.height() - 6
        self.move(max(8, x), max(8, y))
        self.show()


# AudioMixPanel — sidebar audio controls using the editor's effect-slider layout

class AudioMixPanel(QFrame):
    """Compact in-sidebar audio mixer with one effect-style control per source."""

    master_changed = Signal(int)            # 0–100
    source_changed = Signal(str, int)       # (source_key, 0–100)
    source_muted = Signal(str, bool)        # (source_key, muted)

    _SOURCES = [('master', 'MASTER')]

    def __init__(self, master_vol: int,
                 source_volumes: dict | None = None,
                 source_mutes: dict | None = None,
                 source_tracks: tuple[PlaybackSource, ...] = (),
                 live_preview: bool = False,
                 parent=None):
        super().__init__(parent)
        self.setObjectName('audioMixPanel')
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(14, 4, 14, 14)
        self._root.setSpacing(8)
        self._sliders: dict[str, QSlider] = {}
        self._values: dict[str, QLabel] = {}
        self._labels: dict[str, QLabel] = {}
        self._mute_buttons: dict[str, QPushButton] = {}
        self._row_order: list[str] = []
        self.set_state(master_vol, source_volumes or {}, source_mutes or {},
                       source_tracks, live_preview)

    def set_state(self, master_vol: int, source_volumes: dict,
                  source_mutes: dict,
                  source_tracks: tuple[PlaybackSource, ...],
                  live_preview: bool):
        """Refresh source rows after asynchronous audio discovery or fallback."""
        self._source_tracks = tuple(
            source for source in source_tracks if source.editable)
        self._source_volumes = dict(source_volumes)
        self._source_mutes = dict(source_mutes)
        self._master_vol = max(0, min(100, int(master_vol)))
        self._live_preview = bool(live_preview)

        while self._root.count():
            item = self._root.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._sliders.clear()
        self._values.clear()
        self._labels.clear()
        self._mute_buttons.clear()
        self._row_order.clear()

        sources = [(key, label, None) for key, label in self._SOURCES]
        sources.extend((source.source_id, self._display_label(source), source)
                       for source in self._source_tracks)
        for key, label, source in sources:
            self._row_order.append(key)
            row = QFrame()
            row.setObjectName('audioMixControl')
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(10, 8, 10, 8)
            row_layout.setSpacing(4)

            header = QHBoxLayout()
            header.setSpacing(6)
            name = QLabel(label)
            name.setObjectName('audioMixName')
            name.setToolTip(label)
            name.setAccessibleName(f'{label} source volume')
            if source is not None and not source.available:
                name.setEnabled(False)
            self._labels[key] = name
            header.addWidget(name, 1)

            slider_value = (self._master_vol if key == 'master'
                            else int(self._source_volumes.get(key, 100)))
            value = QLabel(f'{slider_value}%')
            value.setObjectName('audioMixValue')
            value.setFixedWidth(34)
            value.setAlignment(Qt.AlignmentFlag.AlignRight |
                               Qt.AlignmentFlag.AlignVCenter)
            if source is not None and not source.available:
                value.setText('—')
                value.setEnabled(False)
            self._values[key] = value
            header.addWidget(value)

            if source is not None:
                mute = QPushButton('MUTED' if self._source_mutes.get(key, False)
                                   else 'MUTE')
                mute.setObjectName('audioMixMute')
                mute.setCheckable(True)
                mute.setChecked(bool(self._source_mutes.get(key, False)))
                mute.setFixedWidth(48)
                mute.setAccessibleName(f'Mute {label}')
                mute.setEnabled(source.available and self._live_preview)
                self._mute_buttons[key] = mute
                mute.toggled.connect(
                    lambda checked, k=key, button=mute:
                    self._on_mute_toggled(k, button, checked))
                header.addWidget(mute)
            row_layout.addLayout(header)

            slider = ClickableSlider(Qt.Orientation.Horizontal)
            slider.setObjectName('audioMixSlider')
            slider.setRange(0, 100)
            slider.setValue(max(0, min(100, slider_value)))
            slider.setToolTip(f'{label} gain for this open clip only.')
            slider.setAccessibleName(f'{label} volume')
            if source is not None:
                slider.setEnabled(source.available and self._live_preview)
            slider.valueChanged.connect(
                lambda value, k=key, lab=label: self._on_changed(k, value, lab))
            self._sliders[key] = slider
            row_layout.addWidget(slider)
            self._root.addWidget(row)

        self._root.addStretch(1)

    @staticmethod
    def _display_label(source: PlaybackSource) -> str:
        return source_display_name(source)

    def _on_changed(self, key: str, value: int, _label: str):
        self._values[key].setText(f'{value}%')
        if key == 'master':
            self.master_changed.emit(value)
        else:
            self.source_changed.emit(key, value)

    def _on_mute_toggled(self, key: str, button: QPushButton, muted: bool):
        button.setText('MUTED' if muted else 'MUTE')
        self.source_muted.emit(key, muted)



class ClipViewer(QDialog):
    """Full FTHR clip editor — video preview, trim bar, export/delete/crop sidebar."""

    _export_done     = Signal(bool, str)
    upload_requested = Signal(str)
    export_error     = Signal(str, str, str)   # title, detail, level

    def __init__(self, clip_path: str, bridge, parent=None,
                 thumb_pixmap: QPixmap = None, settings_manager=None,
                 upload_enabled: bool = False, metadata_manager=None,
                 linked_import: bool = False):
        super().__init__(parent)
        self.clip_path       = clip_path
        from core.playback_proxy import cached_playback_path
        self._playback_path = cached_playback_path(clip_path)
        self._prepared_playback_path = None
        self._playback_source_worker = None
        self.bridge          = bridge
        self.sm              = settings_manager
        self._mm             = metadata_manager
        self._upload_enabled = upload_enabled
        self._linked_import = linked_import
        self._closing = False
        self._teardown_complete = False
        self._prepare_cancel = threading.Event()
        self._media_ready = False
        self._audio_preparation_ready = False
        self._playback_ready = False
        self._playback_preparing_at = time.monotonic()
        # Direct QVideoWidget is selected only for neutral playback; the
        # software sink remains available for composited edit previews.
        # QVideoWidget must be attached before QMediaPlayer.setSource() on
        # Windows MediaFoundation; attaching it after LoadedMedia can advance
        # audio/position without delivering any video frames.
        self._video_renderer = 'native'
        self._native_frame_count = 0
        self._renderer_resume_position_ms: int | None = None
        self._renderer_resume_playing = False
        self._player_lifecycle_state = PlayerLifecycleState.PREPARING
        self._player_failure_detail = ''
        self._play_when_ready = bool(
            self.sm.get('clip_viewer_autoplay', True)) if self.sm else True
        self._audio_prepare_timed_out = False
        self._last_stable_position_ms = 0
        self._resume_anchor_ms: int | None = None
        self._resume_guard_deadline = 0.0
        self._native_audio_source_id: str | None = None
        self._deferred_audio_mixer_sources: tuple[PlaybackSource, ...] = ()
        self._live_clip_preview = bool(
            self.sm.get('clip_editor_live_preview', True)) if self.sm else True
        self._crop_rect    = None
        self._stretch_ratio = 1.0
        self._speed_rate = 1.0
        self._preserve_pitch = True
        self._effects = {
            'exposure': 0,
            'contrast': 0,
            'saturation': 0,
            'temperature': 0,
            'sharpness': 0,
            'vignette': 0,
        }
        self._undo_stack: list[EditorSnapshot] = []
        self._redo_stack: list[EditorSnapshot] = []
        self._restoring_state = False
        self._pending_trim_snapshot: EditorSnapshot | None = None
        self._pending_effect_snapshot: EditorSnapshot | None = None
        self._preview_update_pending = False
        self._preview_neutral_applied = False
        self._preview_update_timer = QTimer(self)
        self._preview_update_timer.setSingleShot(True)
        self._preview_update_timer.setInterval(70)
        self._preview_update_timer.timeout.connect(self._flush_live_preview)
        # Editing state is tiny, but writing it on every slider tick would
        # create avoidable I/O during preview. Restarting this single-shot timer
        # makes drafts durable shortly after interaction stops and closeEvent
        # performs the final flush for accidental exits.
        self._draft_dirty = False
        self._last_persisted_snapshot: EditorSnapshot | None = None
        self._discard_editor_draft = False
        self._draft_save_timer = QTimer(self)
        self._draft_save_timer.setSingleShot(True)
        self._draft_save_timer.setInterval(450)
        self._draft_save_timer.timeout.connect(self._flush_editor_draft)
        self._thumb_pixmap = thumb_pixmap
        # The source rows are filled asynchronously from either a hash-bound
        # FTHR manifest or actual container metadata. Imported media therefore
        # gets generic track labels instead of fabricated app identities.
        self._playback_sources: tuple[PlaybackSource, ...] = ()
        self._audio_tracks: tuple[tuple[str, int], ...] = ()
        self._audio_mixer: FFmpegPlaybackController | None = None
        self._audio_mixer_live = False
        self._diagnostic_player_counted = False
        self._playback_requested_at = 0.0
        self._export_diagnostic_started_at = 0.0
        self._export_job: ExportJob | None = None
        self._export_thread: threading.Thread | None = None
        self._export_cancel = threading.Event()
        self._export_staged_path: str | None = None

        self.setWindowTitle(f'FTHR — {Path(clip_path).name}')
        self.setWindowFlags(
            Qt.WindowType.Dialog |
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setMinimumSize(960, 600)
        self._drag_pos: QPoint | None = None
        self._native_style_applied = False

        # Try the metadata cache that clip_grid maintains as a side effect of
        # building each thumbnail. On a cache hit we skip cv2.VideoCapture
        # entirely — that single call is the biggest contributor to first-open
        # lag (cold cv2 + FFmpeg demux is typically 50–150 ms on the UI thread).
        meta = None
        try:
            from ui.clip_grid import get_cached_clip_metadata
            meta = get_cached_clip_metadata(clip_path)
        except Exception:
            meta = None

        self._metadata_probe_pending = False
        self._metadata_loaded = False
        if meta and meta[1] > 0 and meta[2] > 0:
            duration_sec, w, h, fps, video_bitrate, total_bitrate = meta
            self._src_w  = int(w)
            self._src_h  = int(h)
            self._fps    = float(fps)
            self.duration_ms = max(int(duration_sec * 1000), 1)
            self._video_bitrate_bps = int(video_bitrate) or None
            self._total_bitrate_bps = int(total_bitrate) or None
            self._metadata_loaded = True
        else:
            # A cold OpenCV/FFmpeg demux can take 50–150 ms on the UI thread.
            # Start with a safe 16:9 shell and replace it from a worker after
            # the dialog has painted. The grid poster is already available,
            # so this is visually useful even before metadata arrives.
            self._src_w  = 1920
            self._src_h  = 1080
            self._fps    = 0.0
            cached_duration = int(meta[0]) if meta and meta[0] > 0 else 30
            self.duration_ms = max(cached_duration * 1000, 1)
            self._video_bitrate_bps = None
            self._total_bitrate_bps = None
            self._metadata_probe_pending = True

        try:
            self._size_mb = os.path.getsize(clip_path) / (1024 * 1024)
        except OSError:
            self._size_mb = 0.0
        try:
            self._file_date = datetime.fromtimestamp(os.path.getmtime(clip_path))
        except OSError:
            self._file_date = datetime.now()

        self._export_done.connect(self._on_export_done)

        self._setup_ui()
        self._apply_styles()
        self._refresh_quick_crop_btn()

        # Non-playback assets are staged only while the player is idle. In
        # particular, the timeline filmstrip must never open a competing video
        # decoder during playback.
        self._timeline_prepare_timer = QTimer(self)
        self._timeline_prepare_timer.setSingleShot(True)
        self._timeline_prepare_timer.setInterval(300)
        self._timeline_prepare_timer.timeout.connect(
            self._load_timeline_thumbnails_if_idle)

        # A broken ffprobe must not hold Play for its full subprocess timeout.
        # After this bounded window Qt's ordinary container audio is safe; a
        # late multi-track result is prepared the next time playback is idle.
        self._audio_prepare_deadline = QTimer(self)
        self._audio_prepare_deadline.setSingleShot(True)
        self._audio_prepare_deadline.setInterval(1000)
        self._audio_prepare_deadline.timeout.connect(
            self._on_audio_preparation_timeout)
        self._audio_prepare_deadline.start()

        self._playback_stall_timer = QTimer(self)
        self._playback_stall_timer.setSingleShot(True)
        self._playback_stall_timer.setInterval(15000)
        self._playback_stall_timer.timeout.connect(
            self._on_playback_diagnostic_stall)
        # A viewer must never remain in PREPARING/PLAY_QUEUED forever, even
        # when a platform multimedia backend stops sending status signals.
        self._playback_stall_timer.start()

        if self._metadata_probe_pending:
            QTimer.singleShot(0, self._start_metadata_probe)

        # A dialog-level shortcut wins over whichever child currently has
        # focus, preventing Space from activating a button or closing the
        # editor through a parent-level action.
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._space_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._space_shortcut.activated.connect(self._toggle_play)

        self._undo_shortcut = QShortcut(QKeySequence('Ctrl+Z'), self)
        self._undo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._undo_shortcut.activated.connect(self._undo)
        self._redo_shortcut = QShortcut(QKeySequence('Ctrl+Y'), self)
        self._redo_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._redo_shortcut.activated.connect(self._redo)
        self._redo_alt_shortcut = QShortcut(QKeySequence('Ctrl+Shift+Z'), self)
        self._redo_alt_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._redo_alt_shortcut.activated.connect(self._redo)
        self._update_history_controls()

        # setWindowOpacity not supported on Wayland — skip fade there
        from PySide6.QtWidgets import QApplication as _App
        _app = _App.instance()
        _wayland = _app and _app.platformName() == 'wayland'
        self._fade_in_anim = None
        if not _wayland:
            self.setWindowOpacity(0.0)
            self._fade_in_anim = QPropertyAnimation(self, b'windowOpacity')
            self._fade_in_anim.setDuration(180)
            self._fade_in_anim.setStartValue(0.0)
            self._fade_in_anim.setEndValue(1.0)
            self._fade_in_anim.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._ph_timer = QTimer(self)
        self._ph_timer.setInterval(80)
        self._ph_timer.timeout.connect(self._update_playhead)

        # Coalesce seeks through a 50 ms timer, keeping only the latest position.
        # Rapid setPosition calls can freeze or crash the Windows media backend.
        self._pending_seek_ms: int | None = None
        self._seek_timer = QTimer(self)
        self._seek_timer.setSingleShot(True)
        self._seek_timer.setInterval(50)
        self._seek_timer.timeout.connect(self._flush_pending_seek)

        # Gain changes need to invalidate PCM already mixed ahead of the video
        # clock. Apply at most once per 40 ms while a slider is dragged.
        self._mix_refresh_timer = QTimer(self)
        self._mix_refresh_timer.setSingleShot(True)
        self._mix_refresh_timer.setInterval(40)
        self._mix_refresh_timer.timeout.connect(self._refresh_live_mix)

        # Guard against re-entrant play/pause calls. setChecked() on the
        # QPushButton emits clicked, so toggling the icon programmatically
        # used to recurse back into _toggle_play.
        self._play_pause_busy = False
        self._audio_sources_discovered.connect(self._on_audio_sources_discovered)
        self._restore_persisted_editor_draft()
        QTimer.singleShot(0, self._discover_audio_sources_async)
        QTimer.singleShot(0, self._prepare_playback_source)

    # UI layout

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(40)
        header.setObjectName('editorHeader')
        hdr = QHBoxLayout(header)
        hdr.setContentsMargins(20, 0, 0, 0)
        hdr.setSpacing(0)

        title = QLabel(Path(self.clip_path).stem[:50].upper())
        title.setObjectName('editorTitle')
        hdr.addWidget(title)
        hdr.addStretch()

        min_btn = QPushButton('—')
        min_btn.setObjectName('editorMin')
        min_btn.setFixedSize(40, 40)
        min_btn.clicked.connect(self.showMinimized)
        hdr.addWidget(min_btn)

        close_btn = QPushButton('✕')
        close_btn.setObjectName('editorClose')
        close_btn.setFixedSize(40, 40)
        close_btn.clicked.connect(self._close)
        hdr.addWidget(close_btn)
        root.addWidget(header)

        # Drag/double-click on the header background (buttons absorb their own clicks)
        header.mousePressEvent       = self._bar_mouse_press
        header.mouseMoveEvent        = self._bar_mouse_move
        header.mouseReleaseEvent     = self._bar_mouse_release
        header.mouseDoubleClickEvent = self._bar_double_click
        self._editor_header = header
        self._editor_header_h = 40

        # Main
        main = QHBoxLayout()
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # Video column
        vid_col = QVBoxLayout()
        vid_col.setContentsMargins(0, 0, 0, 0)
        vid_col.setSpacing(0)

        # Clipping container for the software-backed live preview.
        self._video_clip_frame = QFrame()
        self._video_clip_frame.setObjectName('videoClipFrame')
        self._video_clip_frame.setStyleSheet(
            f'QFrame#videoClipFrame {{ background-color: {Colors.BG}; border: none; }}')
        self._video_clip_frame.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.video_widget = LiveVideoPreview(
            self._src_w, self._src_h, self._video_clip_frame)
        self.video_widget.set_poster(self._thumb_pixmap)
        self.video_widget.setGeometry(0, 0, 1, 1)

        # Keep a native/direct renderer beside the software surface. Only one
        # is connected to the shared QMediaPlayer at a time; software remains
        # required for live crop/effect compositing.
        self._native_video_widget = QVideoWidget(self._video_clip_frame)
        self._native_video_widget.setObjectName('nativeVideoWidget')
        self._native_video_widget.setGeometry(0, 0, 1, 1)
        # Keep the poster-bearing software widget visible until media status
        # confirms that the native surface has frames to present.
        self._native_video_widget.hide()
        try:
            self._native_video_widget.videoSink().videoFrameChanged.connect(
                self._on_native_video_frame)
        except (AttributeError, RuntimeError, TypeError):
            # Older Qt builds may not expose videoSink() on QVideoWidget. The
            # direct output still works; frame-rate diagnostics are optional.
            pass

        vid_col.addWidget(self._video_clip_frame, stretch=1)

        self._video_clip_frame.installEventFilter(self)


        ctrl = QFrame()
        ctrl.setObjectName('ctrlBar')
        ctrl.setFixedHeight(46)
        ctrl_lay = QHBoxLayout(ctrl)
        ctrl_lay.setContentsMargins(16, 0, 16, 0)
        ctrl_lay.setSpacing(12)

        self.play_btn = QPushButton()
        self.play_btn.setObjectName('playBtn')
        self.play_btn.setCheckable(True)
        self.play_btn.setFixedWidth(96)
        self.play_btn.clicked.connect(self._toggle_play)
        self._play_icon = _load_themed_icon('play.png', 16)
        self._pause_icon = _load_themed_icon('pause.png', 16)
        # The checked button uses the teal accent as its background. Keep a
        # dark pause glyph for that state; a teal glyph on a teal button made
        # the pause control look like an empty rectangle.
        self._play_active_icon = _load_themed_icon('play.png', 16, Colors.BG)
        self._pause_active_icon = _load_themed_icon('pause.png', 16, Colors.BG)
        self.play_btn.setIconSize(QSize(16, 16))
        self._set_playback_button_visual(False)
        ctrl_lay.addWidget(self.play_btn)

        self.time_label = QLabel(f'0:00 / {self._fmt(self.duration_ms)}')
        self.time_label.setObjectName('timeLabel')
        ctrl_lay.addWidget(self.time_label)
        self._playback_status_lbl = QLabel('PREPARING CLIP…')
        self._playback_status_lbl.setObjectName('timeLabel')
        self._playback_status_lbl.setToolTip(
            'Loading the media pipeline and inspecting the clip audio.')
        ctrl_lay.addWidget(self._playback_status_lbl)
        ctrl_lay.addStretch()

        # Audio state is rendered in the right sidebar's AUDIO MIX panel.
        # Keep the state here with the playback controls so discovery and
        # playback fallback can update the panel without moving ownership.
        if self.sm:
            self._master_volume = int(self.sm.get('master_volume', 80))
        else:
            self._master_volume = 80
        self._source_volumes: dict[str, int] = {}
        self._source_mutes: dict[str, bool] = {}
        self._multitrack_audio = False
        self._volume_popup: VolumePopup | None = None
        vid_col.addWidget(ctrl)

        trim_frame = QFrame()
        trim_frame.setObjectName('trimBar')
        trim_lay = QVBoxLayout(trim_frame)
        trim_lay.setContentsMargins(16, 10, 16, 12)
        trim_lay.setSpacing(6)

        trim_hdr = QHBoxLayout()
        trim_hdr.setSpacing(8)
        trim_lbl = QLabel('TIMELINE')
        trim_lbl.setObjectName('trimLabel')
        trim_hdr.addWidget(trim_lbl)
        trim_hdr.addStretch()

        self.trim_range_lbl = QLabel(f'0:00 → {self._fmt(self.duration_ms)}')
        self.trim_range_lbl.setObjectName('trimRangeLbl')
        trim_hdr.addWidget(self.trim_range_lbl)
        trim_lay.addLayout(trim_hdr)

        # Navigation and edit actions get a dedicated row. This avoids the
        # right-edge pile-up visible at 1080p while preserving a wide filmstrip.
        trim_tools = QHBoxLayout()
        trim_tools.setSpacing(6)

        self.undo_btn = QPushButton('UNDO')
        self.undo_btn.setObjectName('timelineHistory')
        self.undo_btn.setToolTip('Undo last edit  ·  Ctrl+Z')
        self.undo_btn.clicked.connect(self._undo)
        trim_tools.addWidget(self.undo_btn)

        self.redo_btn = QPushButton('REDO')
        self.redo_btn.setObjectName('timelineHistory')
        self.redo_btn.setToolTip('Redo edit  ·  Ctrl+Y or Ctrl+Shift+Z')
        self.redo_btn.clicked.connect(self._redo)
        trim_tools.addWidget(self.redo_btn)

        trim_tools.addSpacing(8)

        self.timeline_pan_left_btn = QPushButton('‹')
        self.timeline_pan_left_btn.setObjectName('timelineTool')
        self.timeline_pan_left_btn.setToolTip('Pan timeline left')
        self.timeline_pan_left_btn.clicked.connect(lambda: self.trim_slider.pan(-1))
        trim_tools.addWidget(self.timeline_pan_left_btn)

        self.timeline_zoom_out_btn = QPushButton('−')
        self.timeline_zoom_out_btn.setObjectName('timelineTool')
        self.timeline_zoom_out_btn.setToolTip('Zoom out')
        self.timeline_zoom_out_btn.clicked.connect(lambda: self._zoom_timeline(0.8))
        trim_tools.addWidget(self.timeline_zoom_out_btn)

        self.timeline_zoom_lbl = QLabel('100%')
        self.timeline_zoom_lbl.setObjectName('timelineZoomLabel')
        self.timeline_zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.timeline_zoom_lbl.setFixedWidth(44)
        trim_tools.addWidget(self.timeline_zoom_lbl)

        self.timeline_zoom_in_btn = QPushButton('+')
        self.timeline_zoom_in_btn.setObjectName('timelineTool')
        self.timeline_zoom_in_btn.setToolTip('Zoom in')
        self.timeline_zoom_in_btn.clicked.connect(lambda: self._zoom_timeline(1.25))
        trim_tools.addWidget(self.timeline_zoom_in_btn)

        self.timeline_pan_right_btn = QPushButton('›')
        self.timeline_pan_right_btn.setObjectName('timelineTool')
        self.timeline_pan_right_btn.setToolTip('Pan timeline right')
        self.timeline_pan_right_btn.clicked.connect(lambda: self.trim_slider.pan(1))
        trim_tools.addWidget(self.timeline_pan_right_btn)

        trim_tools.addStretch()
        self.split_btn = QPushButton('SPLIT')
        self.split_btn.setObjectName('timelineAction')
        self.split_btn.setToolTip('Split the selected segment at the playhead')
        self.split_btn.clicked.connect(self._split_at_playhead)
        trim_tools.addWidget(self.split_btn)

        self.delete_segment_btn = QPushButton('DELETE SEGMENT')
        self.delete_segment_btn.setObjectName('timelineActionDanger')
        self.delete_segment_btn.setToolTip('Select a split segment, then remove it from the edit')
        self.delete_segment_btn.setEnabled(False)
        self.delete_segment_btn.clicked.connect(self._toggle_selected_segment)
        trim_tools.addWidget(self.delete_segment_btn)
        trim_lay.addLayout(trim_tools)

        self.trim_slider = TrimSlider(self.duration_ms)
        self.trim_slider.range_changed.connect(self._on_trim_changed)
        self.trim_slider.seek_requested.connect(self._on_seek_requested)
        self.trim_slider.segments_changed.connect(self._on_segments_changed)
        self.trim_slider.selection_changed.connect(self._on_segment_selected)
        self.trim_slider.range_edit_started.connect(self._begin_trim_edit)
        self.trim_slider.range_edit_finished.connect(self._finish_trim_edit)
        self.trim_slider.context_split_requested.connect(self._split_at_position)
        self.trim_slider.context_toggle_requested.connect(self._toggle_segment_at)
        self.trim_slider.zoom_changed.connect(
            lambda factor: self.timeline_zoom_lbl.setText(f'{int(round(factor * 100))}%'))
        trim_lay.addWidget(self.trim_slider)
        vid_col.addWidget(trim_frame)
        main.addLayout(vid_col, stretch=1)

        # Sidebar (Medal-style)
        sidebar = QFrame()
        sidebar.setObjectName('editorSidebar')
        sidebar.setFixedWidth(280)

        # Editing controls can expand substantially, so the sidebar is a real
        # scroll surface instead of letting lower actions disappear off-screen.
        sb_shell = QVBoxLayout(sidebar)
        sb_shell.setContentsMargins(0, 0, 0, 0)
        self._sidebar_scroll = QScrollArea()
        self._sidebar_scroll.setObjectName('editorSidebarScroll')
        self._sidebar_scroll.setWidgetResizable(True)
        self._sidebar_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sidebar_content = QWidget()
        sidebar_content.setObjectName('editorSidebarContent')
        # QScrollArea otherwise honors the widest collapsed panel's size hint
        # (318 px here) instead of the 280 px sidebar viewport. Opening Clip
        # Details then appears to push the content sideways. Ignored lets the
        # resizable scroll area keep every panel pinned to the viewport width.
        sidebar_content.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        sidebar_content.setMinimumWidth(0)
        sb_outer = QVBoxLayout(sidebar_content)
        sb_outer.setContentsMargins(14, 16, 14, 16)
        sb_outer.setSpacing(8)
        self._sidebar_scroll.setWidget(sidebar_content)
        sb_shell.addWidget(self._sidebar_scroll)

        # PRIMARY ACTIONS ──
        self.share_btn = QPushButton('Share clip')
        self.share_btn.setObjectName('shareBtn')
        self.share_btn.setFixedHeight(42)
        self.share_btn.clicked.connect(self._show_share_overlay)
        sb_outer.addWidget(self.share_btn)

        self.export_btn = QPushButton('Export')
        self.export_btn.setObjectName('exportBtn')
        self.export_btn.setFixedHeight(38)
        self.export_btn.clicked.connect(self._on_export_button)
        sb_outer.addWidget(self.export_btn)

        self.upload_btn = QPushButton('Upload')
        self.upload_btn.setObjectName('uploadBtn')
        self.upload_btn.setFixedHeight(38)
        self.upload_btn.setVisible(self._upload_enabled)
        self.upload_btn.clicked.connect(self._on_upload_click)
        sb_outer.addWidget(self.upload_btn)

        sb_outer.addSpacing(6)

        # CLIP SPEED ──
        speed_panel, speed_body = self._build_collapsible_panel(
            'CLIP SPEED', expanded=True)
        spb = QVBoxLayout()
        spb.setContentsMargins(14, 4, 14, 14)
        spb.setSpacing(8)

        spb.addWidget(self._kv_label('Playback speed'))
        self.speed_combo = WheelSafeComboBox()
        self.speed_combo.setObjectName('speedCombo')
        self.speed_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.speed_combo.setFixedHeight(30)
        self.speed_combo.setToolTip(
            'Playback and export speed. Audio follows this rate.')
        for label, rate in SPEED_PRESETS:
            self.speed_combo.addItem(label, rate)
        self.speed_combo.setCurrentIndex(3)
        self.speed_combo.setStyleSheet(combo_qss())
        self.speed_combo.currentIndexChanged.connect(self._on_speed_changed)
        spb.addWidget(self.speed_combo)

        self.preserve_pitch_toggle = QCheckBox('Preserve pitch')
        self.preserve_pitch_toggle.setObjectName('pitchToggle')
        self.preserve_pitch_toggle.setChecked(self._preserve_pitch)
        self.preserve_pitch_toggle.setToolTip(
            'Keep voices and music at their original pitch while changing speed.')
        self.preserve_pitch_toggle.toggled.connect(
            self._on_preserve_pitch_toggled)
        spb.addWidget(self.preserve_pitch_toggle)

        speed_body.addLayout(spb)
        sb_outer.addWidget(speed_panel)

        # EDIT TOOLS ──
        edit_panel, edit_body = self._build_collapsible_panel('EDIT TOOLS', expanded=True)
        epb = QVBoxLayout()
        epb.setContentsMargins(14, 4, 14, 14)
        epb.setSpacing(8)

        crop_row = QHBoxLayout()
        crop_row.setSpacing(6)
        self.crop_btn = QPushButton('SET CROP')
        self.crop_btn.setObjectName('cropBtn')
        self.crop_btn.setFixedHeight(32)
        self.crop_btn.clicked.connect(self._open_crop_dialog)
        crop_row.addWidget(self.crop_btn, stretch=1)
        self.quick_crop_btn = QPushButton('QUICK')
        self.quick_crop_btn.setObjectName('quickCropBtn')
        self.quick_crop_btn.setFixedSize(62, 32)
        self.quick_crop_btn.setToolTip('Apply saved Quick Crop')
        self.quick_crop_btn.clicked.connect(self._apply_quick_crop)
        crop_row.addWidget(self.quick_crop_btn)
        epb.addLayout(crop_row)

        self.save_qc_btn = QPushButton('SAVE AS QUICK CROP')
        self.save_qc_btn.setObjectName('saveQcBtn')
        self.save_qc_btn.setFixedHeight(26)
        self.save_qc_btn.setEnabled(False)
        self.save_qc_btn.clicked.connect(self._save_quick_crop)
        epb.addWidget(self.save_qc_btn)

        stretch_row = QHBoxLayout()
        stretch_row.setSpacing(6)
        self.stretch_btn = QPushButton('STRETCH VIDEO')
        self.stretch_btn.setObjectName('stretchBtn')
        self.stretch_btn.setFixedHeight(32)
        self.stretch_btn.setToolTip('Open a crop-aware preview and drag either side')
        self.stretch_btn.clicked.connect(self._open_stretch_dialog)
        stretch_row.addWidget(self.stretch_btn, stretch=1)
        self.reset_stretch_btn = QPushButton('RESET')
        self.reset_stretch_btn.setObjectName('resetStretchBtn')
        self.reset_stretch_btn.setFixedSize(62, 32)
        self.reset_stretch_btn.clicked.connect(self._reset_stretch)
        stretch_row.addWidget(self.reset_stretch_btn)
        epb.addLayout(stretch_row)

        self.crop_info_lbl = QLabel('')
        self.crop_info_lbl.setObjectName('cropInfoLbl')
        self.crop_info_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        epb.addWidget(self.crop_info_lbl)
        edit_body.addLayout(epb)
        sb_outer.addWidget(edit_panel)

        # AUDIO MIX ──
        audio_panel, audio_body = self._build_collapsible_panel(
            'AUDIO MIX', expanded=True)
        self._audio_mix_panel = AudioMixPanel(
            master_vol=self._master_volume,
            source_volumes=self._source_volumes,
            source_mutes=self._source_mutes,
            source_tracks=self._playback_sources,
            live_preview=False,
        )
        self._audio_mix_panel.master_changed.connect(
            self._on_master_volume_changed)
        self._audio_mix_panel.source_changed.connect(
            self._on_source_volume_changed)
        self._audio_mix_panel.source_muted.connect(
            self._on_source_muted)
        audio_body.addWidget(self._audio_mix_panel)
        sb_outer.addWidget(audio_panel)

        # VIDEO LOOK ──
        look_panel, look_body = self._build_collapsible_panel('COLOR & EFFECTS', expanded=False)
        lpb = QVBoxLayout()
        lpb.setContentsMargins(14, 4, 14, 14)
        lpb.setSpacing(8)
        self._effect_sliders: dict[str, QSlider] = {}
        self._effect_value_labels: dict[str, QLabel] = {}
        for key, title, minimum, maximum in (
                ('exposure', 'Exposure', -100, 100),
                ('contrast', 'Contrast', -100, 100),
                ('saturation', 'Saturation', -100, 100),
                ('temperature', 'Temperature', -100, 100),
                ('sharpness', 'Sharpness', 0, 100),
                ('vignette', 'Vignette', 0, 100)):
            lpb.addWidget(self._build_effect_control(key, title, minimum, maximum))
        reset_look_btn = QPushButton('RESET LOOK')
        reset_look_btn.setObjectName('resetLookBtn')
        reset_look_btn.setFixedHeight(28)
        reset_look_btn.clicked.connect(self._reset_effects)
        lpb.addWidget(reset_look_btn)
        look_body.addLayout(lpb)
        sb_outer.addWidget(look_panel)

        # CLIP DETAILS panel ──
        details_panel, details_body = self._build_collapsible_panel('CLIP DETAILS', expanded=False)
        dpb = QVBoxLayout()
        dpb.setContentsMargins(14, 4, 14, 14)
        dpb.setSpacing(10)

        dur_str  = self._fmt(self.duration_ms)
        date_str = self._file_date.strftime('%b %d, %Y  %H:%M')
        fname    = Path(self.clip_path).name
        title_text = fname.rsplit('.', 1)[0]
        if len(title_text) > 38:
            title_text = title_text[:36] + '…'

        dpb.addWidget(self._kv_label('Title'))
        title_val = QLabel(title_text)
        title_val.setObjectName('detailValBig')
        title_val.setWordWrap(True)
        dpb.addWidget(title_val)

        dpb.addWidget(self._kv_label('Tag'))
        meta = self._mm.get(self.clip_path) if self._mm else {'tag': '', 'description': ''}
        _edit_style = (
            f'QLineEdit#detailEdit {{ background: transparent; border: none;'
            f' border-bottom: 1px solid {Colors.TEXT_DIM}; color: {Colors.TEXT};'
            f' font-size: 12px; padding: 2px 0; }}'
            f'QLineEdit#detailEdit:focus {{ border-bottom-color: {Colors.ACCENT}; }}'
        )
        tag_edit = QLineEdit(meta['tag'])
        tag_edit.setPlaceholderText('e.g. Gaming, Highlight …')
        tag_edit.setObjectName('detailEdit')
        tag_edit.setStyleSheet(_edit_style)
        if self._mm:
            tag_edit.editingFinished.connect(
                lambda: self._mm.set(self.clip_path, tag=tag_edit.text().strip())
            )
        dpb.addWidget(tag_edit)

        dpb.addWidget(self._kv_label('Description'))
        desc_edit = QLineEdit(meta['description'])
        desc_edit.setPlaceholderText('Short clip description …')
        desc_edit.setObjectName('detailEdit')
        desc_edit.setStyleSheet(_edit_style)
        if self._mm:
            desc_edit.editingFinished.connect(
                lambda: self._mm.set(self.clip_path, description=desc_edit.text().strip())
            )
        dpb.addWidget(desc_edit)

        details_body.addLayout(dpb)
        sb_outer.addWidget(details_panel)

        # FILE DETAILS panel ──
        file_panel, file_body = self._build_collapsible_panel('FILE DETAILS', expanded=False)
        fpb = QVBoxLayout()
        fpb.setContentsMargins(14, 4, 14, 14)
        fpb.setSpacing(10)

        fpb.addWidget(self._kv_label('Created'))
        fpb.addWidget(self._kv_value(date_str))
        fpb.addWidget(self._kv_label('Video Quality'))
        quality_text = (
            f'{self._src_w}x{self._src_h}, {format_fps(self._fps)}'
            if self._metadata_loaded else 'Loading…')
        self._quality_value = self._kv_value(quality_text)
        fpb.addWidget(self._quality_value)
        # Multiple AAC stems make total file bitrate different from the video
        # stream bitrate, so present the two factual values separately.
        fpb.addWidget(self._kv_label('Video Bitrate'))
        self._video_bitrate_value = self._kv_value(
            format_bitrate(self._video_bitrate_bps)
            if self._metadata_loaded else 'Loading…')
        fpb.addWidget(self._video_bitrate_value)
        fpb.addWidget(self._kv_label('Total Bitrate'))
        self._total_bitrate_value = self._kv_value(
            format_bitrate(self._total_bitrate_bps)
            if self._metadata_loaded else 'Loading…')
        fpb.addWidget(self._total_bitrate_value)
        fpb.addWidget(self._kv_label('Duration'))
        self._duration_value = self._kv_value(dur_str)
        fpb.addWidget(self._duration_value)
        fpb.addWidget(self._kv_label('Size'))
        fpb.addWidget(self._kv_value(f'{self._size_mb:.1f} MB'))
        # Location (truncated)
        fpb.addWidget(self._kv_label('Location'))
        location_path = str(Path(self.clip_path).parent)
        if len(location_path) > 36:
            location_path = '…' + location_path[-34:]
        fpb.addWidget(self._kv_value(location_path))

        file_body.addLayout(fpb)
        sb_outer.addWidget(file_panel)

        sb_outer.addStretch(1)

        self.delete_btn = QPushButton('Delete')
        self.delete_btn.setObjectName('deleteBtn')
        self.delete_btn.setFixedHeight(36)
        self.delete_btn.clicked.connect(self._delete_clip)
        if self._linked_import:
            self.delete_btn.setText('Linked original — protected')
            self.delete_btn.setEnabled(False)
            self.delete_btn.setToolTip(
                'Remove its import folder from FTHR; the original stays on disk.')
        sb_outer.addWidget(self.delete_btn)

        main.addWidget(sidebar)
        root.addLayout(main, stretch=1)

        # Media player
        self.player = QMediaPlayer(self)
        playback_instance_created()
        self._diagnostic_player_counted = True
        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(self._master_volume / 100.0)
        self.player.setAudioOutput(self.audio_output)
        # Attach the direct surface before _set_media_source() is scheduled.
        # Persisted non-neutral edits call _update_video_renderer() during
        # draft restore and switch to the software sink before source open.
        self.player.setVideoOutput(self._native_video_widget)
        self.player.playbackStateChanged.connect(self._on_state_changed)
        self.player.mediaStatusChanged.connect(self._on_media_status_changed)
        self.player.positionChanged.connect(self._on_player_position_changed)
        # Surface backend errors instead of letting them propagate up as a
        # hard crash. This connects QMediaPlayer.errorOccurred where available
        # (Qt 6.5+); older bindings simply ignore the AttributeError.
        try:
            self.player.errorOccurred.connect(self._on_player_error)
        except AttributeError:
            # Older supported Qt builds have no errorOccurred signal.
            pass
        # Let the dialog paint its poster and controls before MediaFoundation
        # starts opening the source. This removes decoder startup from the
        # perceived click-to-editor latency.
        QTimer.singleShot(0, self._set_media_source)

    # Event filter — keep the software video surface sized to the clip frame

    @property
    def player_state(self) -> PlayerLifecycleState:
        return self._player_lifecycle_state

    def _set_player_state(self, state: PlayerLifecycleState,
                          detail: str = '') -> None:
        if state is self._player_lifecycle_state:
            if detail:
                self._player_failure_detail = detail
            return
        self._player_lifecycle_state = state
        if detail:
            self._player_failure_detail = detail
        emit_event('playback', 'lifecycle_state', state=state.value,
                   detail=detail or None)

    def _fail_playback(self, detail: str,
                       error: DiagnosticError = DiagnosticError.PLAYBACK_STALLED) -> None:
        """Make a readiness/decoder failure terminal and cancel queued play."""

        if self._closing:
            return
        self._play_when_ready = False
        self._playback_ready = False
        self._playback_stall_timer.stop()
        self._set_player_state(PlayerLifecycleState.FAILED, detail)
        self._playback_status_lbl.setText('PLAYBACK FAILED')
        self.play_btn.blockSignals(True)
        self.play_btn.setChecked(False)
        self.play_btn.blockSignals(False)
        self._set_playback_button_visual(False)
        emit_event('playback', 'readiness_failed', state='FAILED',
                   error=error, detail=detail)

    def eventFilter(self, obj, event):
        if obj is self._video_clip_frame and event.type() == QEvent.Type.Resize:
            self._relayout_video()
        return super().eventFilter(obj, event)

    def _requires_software_video(self) -> bool:
        """Use composited preview when crop guides or visual effects require it.

        Neutral preview uses the cheaper native surface; renderer choice does not
        change export settings.
        """

        if not self._live_clip_preview:
            return False
        if self._crop_rect:
            return True
        if abs(float(self._stretch_ratio) - 1.0) > 0.005:
            return True
        return any(int(value) != 0 for value in self._effects.values())

    def _on_native_video_frame(self, frame):
        """Record a cheap native-frame count without converting the frame."""

        try:
            if frame.isValid():
                self._native_frame_count += 1
        except (AttributeError, RuntimeError):
            # Diagnostics must never interfere with multimedia delivery.
            pass

    def _refresh_software_renderer_frame(self, position: int,
                                         was_playing: bool):
        """Ask the software sink for the current frame after an output switch."""

        if (self._closing or self._video_renderer != 'software'
                or not self._media_ready):
            return
        try:
            self.player.setPosition(max(0, int(position)))
            if (was_playing
                    and self.player.playbackState()
                    != QMediaPlayer.PlaybackState.PlayingState):
                self.player.play()
        except (RuntimeError, TypeError):
            pass

    def _connect_media_player_signals(self, player: QMediaPlayer) -> None:
        """Connect one replacement-safe set of player callbacks."""

        player.playbackStateChanged.connect(self._on_state_changed)
        player.mediaStatusChanged.connect(self._on_media_status_changed)
        player.positionChanged.connect(self._on_player_position_changed)
        try:
            player.errorOccurred.connect(self._on_player_error)
        except AttributeError:
            pass

    def _recreate_player_for_renderer(self, target: str, position: int,
                                      was_playing: bool) -> bool:
        """Recreate QMediaPlayer with its output set before setSource().

        On Windows, switching outputs after loading can leave the new sink starved.
        Retain the viewer, audio controller, and playback position.
        """

        if self._closing:
            return False
        old_player = self.player
        try:
            player = QMediaPlayer(self)
            player.setAudioOutput(self.audio_output)
            if target == 'native':
                player.setVideoOutput(self._native_video_widget)
            else:
                player.setVideoOutput(self.video_widget.video_sink)
        except (RuntimeError, TypeError):
            try:
                player.deleteLater()
            except (NameError, RuntimeError, TypeError):
                # Construction either failed before assignment or Qt already
                # destroyed the incomplete replacement.
                pass
            return False
        try:
            if self._audio_mixer is not None:
                self._audio_mixer.pause()
        except (AttributeError, RuntimeError, TypeError):
            # A mixer already closing has no audio left to pause.
            pass
        for signal, callback in (
                (old_player.playbackStateChanged, self._on_state_changed),
                (old_player.mediaStatusChanged, self._on_media_status_changed),
                (old_player.positionChanged, self._on_player_position_changed)):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                # An already-disconnected/deleted old player has no callback
                # capable of reaching the replacement.
                pass
        try:
            old_player.errorOccurred.disconnect(self._on_player_error)
        except (AttributeError, RuntimeError, TypeError):
            # Qt versions without the signal, or an earlier disconnect, need
            # no additional error callback cleanup.
            pass
        try:
            old_player.stop()
            old_player.setVideoOutput(None)
            old_player.setAudioOutput(None)
            old_player.setSource(QUrl())
            old_player.deleteLater()
        except (RuntimeError, TypeError):
            player.deleteLater()
            return False
        self.player = player
        self._connect_media_player_signals(player)
        self._apply_playback_rate()
        self._renderer_resume_position_ms = max(0, int(position))
        self._renderer_resume_playing = bool(was_playing)
        self._media_ready = False
        self._playback_ready = False
        self._play_when_ready = bool(was_playing)
        self._set_player_state(PlayerLifecycleState.PREPARING)
        self._playback_preparing_at = time.monotonic()
        self._playback_stall_timer.start()
        self._timeline_prepare_timer.stop()
        self.trim_slider.cancel_thumbnail_loading()
        try:
            player.setSource(QUrl.fromLocalFile(self._playback_path))
        except (RuntimeError, TypeError) as exc:
            self._fail_playback(
                f'{type(exc).__name__}: {exc}',
                DiagnosticError.PLAYBACK_INIT_FAILED)
            return False
        return True

    def _update_video_renderer(self):
        """Attach the cheapest safe output for the current preview state."""

        native = getattr(self, '_native_video_widget', None)
        software = getattr(self, 'video_widget', None)
        player = getattr(self, 'player', None)
        frame = getattr(self, '_video_clip_frame', None)
        if native is None or software is None or player is None or frame is None:
            return

        target = 'software' if self._requires_software_video() else 'native'
        cw, ch = frame.width(), frame.height()
        if cw > 0 and ch > 0:
            software.setGeometry(0, 0, cw, ch)
            native.setGeometry(0, 0, cw, ch)

        if target == self._video_renderer:
            native.setVisible(target == 'native' and self._media_ready)
            software.setVisible(target == 'software' or not self._media_ready)
            return

        was_playing = (
            player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState)
        position = max(0, int(player.position()))
        try:
            source = player.source()
            source_loaded = bool(source and not source.isEmpty())
        except (AttributeError, RuntimeError, TypeError):
            source_loaded = bool(self._media_ready)
        try:
            if source_loaded:
                if not self._recreate_player_for_renderer(
                        target, position, was_playing):
                    raise RuntimeError('Could not recreate media player')
            elif target == 'native':
                player.setVideoOutput(native)
            else:
                player.setVideoOutput(software.video_sink)
        except (RuntimeError, TypeError):
            # If the native backend rejects the output, leave the proven
            # software sink attached rather than breaking playback entirely.
            try:
                player.setVideoOutput(software.video_sink)
            except (RuntimeError, TypeError):
                # Both Qt surfaces are already invalid during teardown; the
                # closing path will release the remaining player resources.
                pass
            self._video_renderer = 'software'
            software.show()
            native.hide()
            return

        self._video_renderer = target
        if target == 'software':
            software.show()
            native.hide()
        else:
            native.setVisible(self._media_ready)
            software.setVisible(not self._media_ready)
        if target == 'software' and self._media_ready and not source_loaded:
            # Let Qt finish attaching the sink before asking it to decode the
            # current position. This also works for a paused player.
            QTimer.singleShot(
                0, lambda: self._refresh_software_renderer_frame(
                    position, was_playing))

    def _relayout_video(self):
        """
        Position both video surfaces inside their clipping frame.
        """
        cw = self._video_clip_frame.width()
        ch = self._video_clip_frame.height()
        if cw <= 0 or ch <= 0:
            return

        self.video_widget.setGeometry(0, 0, cw, ch)
        if hasattr(self, '_native_video_widget'):
            self._native_video_widget.setGeometry(0, 0, cw, ch)
        self._refresh_live_preview()

    def _set_media_source(self):
        if self._closing:
            return
        try:
            self._playback_preparing_at = time.monotonic()
            emit_event('playback', 'media_requested', state='PREPARING')
            self.player.setSource(QUrl.fromLocalFile(self._playback_path))
        except Exception as e:
            print(f'[ClipViewer] setSource failed: {e}')
            detail = f'{type(e).__name__}: {e}'
            self._fail_playback(detail, DiagnosticError.PLAYBACK_INIT_FAILED)
            emit_event('playback', 'media_request_failed', state='FAILED',
                       error=DiagnosticError.PLAYBACK_INIT_FAILED,
                       detail=detail)

    def _prepare_playback_source(self):
        if self._closing or self._playback_path != self.clip_path:
            return
        worker = _PlaybackSourceWorker(self.clip_path, self._prepare_cancel)
        self._playback_source_worker = worker
        worker.signals.ready.connect(self._on_playback_source_ready)
        QThreadPool.globalInstance().start(worker)

    def _on_playback_source_ready(self, path):
        self._playback_source_worker = None
        if self._closing or path == self._playback_path:
            return
        self._prepared_playback_path = path
        self._apply_prepared_playback_source()

    def _apply_prepared_playback_source(self):
        if (self._closing or not self._prepared_playback_path
                or self.player.playbackState() == QMediaPlayer.PlayingState):
            return
        position = (self._pending_seek_ms if self._pending_seek_ms is not None
                    else int(self.player.position()))
        self._seek_timer.stop()
        self._pending_seek_ms = None
        self._playback_path = self._prepared_playback_path
        self._prepared_playback_path = None
        self._renderer_resume_position_ms = position
        self._media_ready = False
        self._playback_ready = False
        self._set_player_state(PlayerLifecycleState.PREPARING)
        self._set_media_source()

    def _on_media_status_changed(self, status):
        if (self._closing
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        if status in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
                QMediaPlayer.MediaStatus.BufferingMedia):
            if (status == QMediaPlayer.MediaStatus.LoadedMedia
                    and not self.player.hasVideo()):
                self._fail_playback(
                    'Loaded media has no video stream',
                    DiagnosticError.PLAYBACK_DECODER_FAILED)
                emit_event(
                    'playback', 'decoder_failed', state='FAILED',
                    error=DiagnosticError.PLAYBACK_DECODER_FAILED,
                    detail='Loaded media has no video stream')
                return
            self._media_ready = True
            resume_position = self._renderer_resume_position_ms
            if resume_position is not None:
                self._renderer_resume_position_ms = None
                try:
                    self.player.setPosition(resume_position)
                except (RuntimeError, TypeError):
                    # A source that became invalid during renderer reopen will
                    # report its own media error; no stale seek is retried.
                    pass
            emit_event('playback', 'decoder_initialized', state='READY',
                       media_status=getattr(status, 'name', str(status)))
            self._update_video_renderer()
            self._update_playback_readiness()
        elif status == QMediaPlayer.MediaStatus.StalledMedia:
            self._playback_status_lbl.setText('BUFFERING…')
            emit_event(
                'playback', 'backend_stalled', state='STALLED',
                error=DiagnosticError.PLAYBACK_STALLED)
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            if self._playback_path != self.clip_path:
                self._on_player_error()
                return
            self._playback_status_lbl.setText('CLIP UNAVAILABLE')
            self._fail_playback('Media backend reported InvalidMedia',
                                DiagnosticError.PLAYBACK_DECODER_FAILED)
            emit_event('playback', 'decoder_failed', state='FAILED',
                       error=DiagnosticError.PLAYBACK_DECODER_FAILED)
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            emit_event('playback', 'end_of_media', state='STOPPED')

    def _on_audio_preparation_timeout(self):
        if self._closing or self._audio_preparation_ready:
            return
        self._audio_prepare_timed_out = True
        self._audio_preparation_ready = True
        emit_event(
            'playback', 'audio_preparation_timeout', state='DEGRADED',
            error=DiagnosticError.AUDIO_OUTPUT_INIT_FAILED)
        self._update_playback_readiness()

    def _update_playback_readiness(self):
        if self._player_lifecycle_state in {
                PlayerLifecycleState.FAILED,
                PlayerLifecycleState.CLOSING,
                PlayerLifecycleState.CLOSED}:
            return
        ready = self._media_ready and self._audio_preparation_ready
        if ready == self._playback_ready:
            return
        self._playback_ready = ready
        if not ready:
            if self._player_lifecycle_state is not PlayerLifecycleState.FAILED:
                self._set_player_state(PlayerLifecycleState.PREPARING)
            self._playback_status_lbl.setText('PREPARING CLIP…')
            self._update_video_renderer()
            return
        self._audio_prepare_deadline.stop()
        self._playback_stall_timer.stop()
        self._playback_status_lbl.setText('READY')
        self._set_player_state(PlayerLifecycleState.READY)
        self._update_video_renderer()
        emit_event(
            'playback', 'player_ready', state='READY',
            elapsed_ms=(round((time.monotonic() - self._playback_requested_at) * 1000)
                        if self._playback_requested_at else None),
            audio_prepare_timed_out=self._audio_prepare_timed_out)
        self._schedule_timeline_thumbnails()
        QTimer.singleShot(900, self._clear_ready_status)
        if self._play_when_ready:
            self._play_when_ready = False
            QTimer.singleShot(0, self._toggle_play)

    def _on_playback_diagnostic_stall(self):
        if (self._closing or self._playback_ready
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        started_at = self._playback_requested_at or getattr(
            self, '_playback_preparing_at', time.monotonic())
        emit_event(
            'playback', 'readiness_stalled', state='STALLED',
            error=DiagnosticError.PLAYBACK_STALLED,
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            media_ready=self._media_ready,
            audio_ready=self._audio_preparation_ready)
        self._fail_playback(
            'Playback preparation did not reach READY before the deadline.')

    def _clear_ready_status(self):
        if (not self._closing and self._playback_ready
                and self._playback_status_lbl.text() == 'READY'):
            self._playback_status_lbl.clear()

    def _schedule_timeline_thumbnails(self, delay_ms: int = 300):
        if (self._closing or self._timeline_prepare_timer.isActive()
                or (self.trim_slider._thumbnails
                    and not self._deferred_audio_mixer_sources)):
            return
        self._timeline_prepare_timer.start(max(0, int(delay_ms)))

    def _load_timeline_thumbnails_if_idle(self):
        if self._closing:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            return
        if self._deferred_audio_mixer_sources:
            self._prepare_audio_mixer(self._deferred_audio_mixer_sources)
            return
        if self.trim_slider._thumbnails:
            return
        duration_seconds = max(1.0, self.duration_ms / 1000.0)
        thumbnail_count = min(
            120, max(24, 24 + int(duration_seconds / 60.0)))
        self.trim_slider.load_thumbnails(
            self.clip_path, count=thumbnail_count)

    def _on_player_position_changed(self, position: int):
        """Update paused seeks without adding a second playback UI loop."""

        if (self._closing
                or self.player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState):
            return
        display_position = self._guarded_display_position(position)
        if self.duration_ms > 0 and self.trim_slider.dragging != 'playhead':
            self.trim_slider.set_playhead(display_position / self.duration_ms)
        self.time_label.setText(
            f'{self._fmt(display_position)} / {self._fmt(self.duration_ms)}')

    def _guarded_display_position(self, position: int) -> int:
        """Hide MediaFoundation's brief zero-position report during resume."""

        position = max(0, int(position))
        anchor = self._resume_anchor_ms
        if anchor is not None:
            if time.monotonic() >= self._resume_guard_deadline:
                self._resume_anchor_ms = None
            elif position + 250 < anchor:
                return anchor
        self._last_stable_position_ms = position
        return position

    def _start_metadata_probe(self):
        if self._closing or not self._metadata_probe_pending:
            return
        worker = _ClipMetadataWorker(self.clip_path, self._prepare_cancel)
        self._metadata_worker = worker
        worker.signals.ready.connect(self._on_metadata_ready)
        QThreadPool.globalInstance().start(worker)

    def _on_metadata_ready(self, metadata):
        if self._closing or not self._metadata_probe_pending:
            return
        self._metadata_probe_pending = False
        if not metadata:
            self._refresh_file_metadata_labels()
            return

        (duration_sec, src_w, src_h, fps,
         video_bitrate, total_bitrate) = metadata
        if src_w > 0 and src_h > 0:
            self._src_w = int(src_w)
            self._src_h = int(src_h)
        self._fps = max(0.0, float(fps))
        if duration_sec > 0:
            self.duration_ms = max(int(float(duration_sec) * 1000), 1)
        self._video_bitrate_bps = int(video_bitrate) or None
        self._total_bitrate_bps = int(total_bitrate) or None
        self._metadata_loaded = True
        self._refresh_file_metadata_labels()
        self.video_widget.set_source_size(self._src_w, self._src_h)
        self.trim_slider.duration_ms = self.duration_ms
        if self._crop_rect:
            x, y, width, height = self._crop_rect
            x = max(0, min(x, self._src_w - 1))
            y = max(0, min(y, self._src_h - 1))
            width = min(width, self._src_w - x)
            height = min(height, self._src_h - y)
            self._crop_rect = ((x, y, width, height)
                               if width >= 2 and height >= 2 else None)
        self.time_label.setText(
            f'{self._fmt(self._last_stable_position_ms)} / '
            f'{self._fmt(self.duration_ms)}')
        self._on_segments_changed()
        self._update_crop_ui()
        self._refresh_live_preview(immediate=True)

    def _refresh_file_metadata_labels(self):
        """Refresh only factual labels without disturbing editor layout."""

        loaded = self._metadata_loaded
        if hasattr(self, '_quality_value'):
            self._quality_value.setText(
                f'{self._src_w}x{self._src_h}, {format_fps(self._fps)}'
                if loaded else 'Unavailable')
        if hasattr(self, '_video_bitrate_value'):
            self._video_bitrate_value.setText(
                format_bitrate(self._video_bitrate_bps)
                if loaded else 'Unavailable')
        if hasattr(self, '_total_bitrate_value'):
            self._total_bitrate_value.setText(
                format_bitrate(self._total_bitrate_bps)
                if loaded else 'Unavailable')
        if hasattr(self, '_duration_value'):
            self._duration_value.setText(
                self._fmt(self.duration_ms) if loaded else 'Unavailable')

    def _refresh_live_preview(self, immediate: bool = False):
        if not self._live_clip_preview:
            self._preview_update_pending = False
            self._preview_update_timer.stop()
            if not self._preview_neutral_applied:
                self.video_widget.set_edit_state(
                    None, None, 1.0, preview_enabled=False)
                self._preview_neutral_applied = True
            self._update_video_renderer()
            return
        self._preview_neutral_applied = False
        if immediate:
            self._preview_update_pending = False
            self._preview_update_timer.stop()
            self.video_widget.set_edit_state(
                self._crop_rect, self._effects, self._stretch_ratio,
                preview_enabled=True)
            self._update_video_renderer()
            return
        self._preview_update_pending = True
        self._update_video_renderer()
        if not self._preview_update_timer.isActive():
            self._preview_update_timer.start()

    def _flush_live_preview(self):
        if not self._preview_update_pending or self._closing:
            return
        self._preview_update_pending = False
        self.video_widget.set_edit_state(
            self._crop_rect, self._effects, self._stretch_ratio,
            preview_enabled=True)
        self._update_video_renderer()

    def _build_effect_control(self, key: str, title: str,
                              minimum: int, maximum: int) -> QFrame:
        row = QFrame()
        row.setObjectName('effectControl')
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        header = QHBoxLayout()
        name = QLabel(title)
        name.setObjectName('effectName')
        header.addWidget(name)
        header.addStretch()
        value_label = QLabel('0')
        value_label.setObjectName('effectValue')
        value_label.setFixedWidth(34)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(value_label)
        layout.addLayout(header)
        slider = ClickableSlider(Qt.Orientation.Horizontal)
        slider.setObjectName('effectSlider')
        slider.setRange(minimum, maximum)
        slider.setValue(0)
        slider.setToolTip(f'Adjust {title.lower()}')
        slider.sliderPressed.connect(self._begin_effect_adjustment)
        slider.sliderReleased.connect(self._finish_effect_adjustment)
        slider.valueChanged.connect(
            lambda value, effect_key=key: self._on_effect_changed(effect_key, value))
        layout.addWidget(slider)
        self._effect_sliders[key] = slider
        self._effect_value_labels[key] = value_label
        return row

    # Sidebar helpers (collapsible panels + key/value labels)

    def _build_collapsible_panel(self, title: str, expanded: bool = True):
        """
        Returns (panel_frame, body_layout). Body_layout is where callers add
        their content. Header click toggles visibility of an inner widget.
        """
        panel = QFrame()
        panel.setObjectName('detailsPanel')
        panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header_btn = ThemedDropdownButton(title)
        header_btn.setObjectName('panelHeader')
        header_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        header_btn.setCheckable(True)
        header_btn.setChecked(expanded)
        header_btn.setExpanded(expanded)
        outer.addWidget(header_btn)

        body_widget = QFrame()
        body_widget.setObjectName('panelBody')
        body_layout = QVBoxLayout(body_widget)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        body_widget.setVisible(expanded)
        outer.addWidget(body_widget)

        def _toggle():
            shown = body_widget.isVisible()
            body_widget.setVisible(not shown)
            header_btn.setExpanded(not shown)
        header_btn.clicked.connect(_toggle)

        return panel, body_layout

    def _kv_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName('detailKey')
        return lbl

    def _kv_value(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName('detailVal')
        lbl.setWordWrap(True)
        return lbl

    # Styles

    def _apply_styles(self):
        # Medal-style clip viewer — dark canvas, accented primary action,
        # square technical panels, monochrome secondary actions, error tone for delete.
        self.setStyleSheet(f'''
            QDialog {{ background-color: {Colors.BG}; }}

            /* Header */
            QFrame#editorHeader {{
                background-color: {Colors.SHELL_BG};
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#editorTitle {{
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px; font-weight: bold;
                font-family: {Fonts.DISPLAY}; letter-spacing: 2px;
                background: transparent;
            }}
            QPushButton#editorMin {{
                background: transparent; border: none; color: {Colors.TEXT_DIM};
                font-size: 14px; font-weight: bold;
            }}
            QPushButton#editorMin:hover {{
                background-color: {Colors.SURFACE_3}; color: {Colors.TEXT};
            }}
            QPushButton#editorClose {{
                background: transparent; border: none; color: {Colors.TEXT_DIM}; font-size: 13px;
            }}
            QPushButton#editorClose:hover {{ background-color: {Colors.ERROR}; color: {Colors.TEXT}; }}

            /* Control + trim bars */
            QFrame#ctrlBar {{
                background-color: {Colors.SURFACE_1};
                border-top: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QPushButton#playBtn {{
                background-color: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};
                border-radius: 0px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold;
                letter-spacing: 1px; padding: 5px 14px;
            }}
            QPushButton#playBtn:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
            QPushButton#playBtn:checked {{
                background-color: {Colors.ACCENT}; border-color: {Colors.ACCENT}; color: {Colors.BG};
            }}
            QLabel#timeLabel {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_BODY}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QComboBox#speedCombo {{
                min-width: 0px; max-width: 999px; min-height: 30px; max-height: 30px;
                padding: 4px 8px; background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER}; border-radius: 0px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold;
            }}
            QComboBox#speedCombo:hover, QComboBox#speedCombo:focus {{
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
            QCheckBox#pitchToggle {{
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
                spacing: 8px; padding: 2px 0;
            }}
            QCheckBox#pitchToggle::indicator {{
                width: 14px; height: 14px; border: 1px solid {Colors.TEXT_DIM};
                background-color: {Colors.SURFACE_2}; border-radius: 0px;
            }}
            QCheckBox#pitchToggle::indicator:hover {{ border-color: {Colors.ACCENT}; }}
            QCheckBox#pitchToggle::indicator:checked {{
                background-color: {Colors.ACCENT}; border-color: {Colors.ACCENT};
            }}
            QLabel#pitchHint {{
                color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#volLabel {{
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY}; letter-spacing: 1px; background: transparent;
            }}
            QPushButton#volBtn {{
                background-color: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};
                border-radius: 0px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold;
                letter-spacing: 1px; padding: 4px 12px;
            }}
            QPushButton#volBtn:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}

            QFrame#trimBar {{
                background-color: {Colors.SURFACE_1};
                border-top: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#trimLabel {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_MICRO}px; font-weight: bold;
                font-family: {Fonts.DISPLAY}; letter-spacing: 2px; background: transparent;
            }}
            QLabel#trimRangeLbl {{
                color: {Colors.ACCENT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#timelineZoomLabel {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QPushButton#timelineTool {{
                min-width: 24px; max-width: 24px; min-height: 24px; max-height: 24px;
                background-color: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER};
                border-radius: 0px; color: {Colors.TEXT_DIM}; font-size: 13px;
                font-family: {Fonts.BODY}; padding: 0;
            }}
            QPushButton#timelineTool:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
            QPushButton#timelineHistory {{
                min-width: 48px; min-height: 24px; max-height: 24px; padding: 0 8px;
                background-color: transparent; border: 1px solid {Colors.BORDER_HI};
                border-radius: 0px; color: {Colors.TEXT}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton#timelineHistory:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
            QPushButton#timelineHistory:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}
            QPushButton#timelineAction, QPushButton#timelineActionDanger {{
                min-height: 24px; max-height: 24px; padding: 0 9px;
                background-color: {Colors.SURFACE_2}; border: 1px solid {Colors.BORDER_HI};
                border-radius: 0px; color: {Colors.TEXT}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold; letter-spacing: 1px;
            }}
            QPushButton#timelineAction:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
            QPushButton#timelineActionDanger:hover {{ border-color: {Colors.DELETE}; color: {Colors.DELETE}; }}
            QPushButton#timelineActionDanger[restore="true"] {{
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
            QPushButton#timelineActionDanger:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}

            /* Sidebar */
            QFrame#editorSidebar {{
                background-color: {Colors.SHELL_BG};
                border-left: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QScrollArea#editorSidebarScroll, QWidget#editorSidebarContent {{
                background-color: {Colors.SHELL_BG}; border: none;
            }}
            QScrollBar:vertical {{
                background: {Colors.SHELL_BG}; width: 8px; margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.BORDER_HI}; min-height: 28px; border-radius: 0px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}

            /* Primary "Share clip" — teal solid */
            QPushButton#shareBtn {{
                background-color: {Colors.ACCENT}; border: none;
                border-radius: 0px;
                color: {Colors.BG}; font-size: {Fonts.SIZE_BODY_L}px;
                font-family: {Fonts.BODY}; font-weight: bold;
            }}
            QPushButton#shareBtn:hover {{ background-color: {Colors.TEXT}; }}
            QPushButton#shareBtn:pressed {{ background-color: {Colors.ACCENT_DIM}; color: {Colors.TEXT}; }}

            /* Secondary "Export" / "Upload" — dark surface */
            QPushButton#exportBtn, QPushButton#uploadBtn {{
                background-color: {Colors.SURFACE_3}; border: 1px solid {Colors.BORDER};
                border-radius: 0px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px;
                font-family: {Fonts.BODY}; font-weight: bold;
            }}
            QPushButton#exportBtn:hover, QPushButton#uploadBtn:hover {{ background-color: {Colors.BORDER}; border-color: {Colors.BORDER_HI}; }}
            QPushButton#exportBtn:disabled, QPushButton#uploadBtn:disabled {{ background-color: {Colors.SURFACE_2}; color: {Colors.TEXT_MUTED}; }}

            /* Crop / quick-crop */
            QPushButton#cropBtn, QPushButton#quickCropBtn, QPushButton#saveQcBtn,
            QPushButton#stretchBtn, QPushButton#resetStretchBtn, QPushButton#resetLookBtn {{
                background-color: transparent; border: 1px solid {Colors.BORDER_HI};
                border-radius: 0px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px; font-weight: bold;
                font-family: {Fonts.DISPLAY}; letter-spacing: 1px;
            }}
            QPushButton#cropBtn:hover, QPushButton#quickCropBtn:hover, QPushButton#saveQcBtn:enabled:hover,
            QPushButton#stretchBtn:hover, QPushButton#resetStretchBtn:hover, QPushButton#resetLookBtn:hover {{
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
            QPushButton#cropBtn[active="true"] {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
            QPushButton#quickCropBtn:disabled, QPushButton#saveQcBtn:disabled {{
                color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER};
            }}
            QPushButton#quickCropBtn[saved="true"] {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}

            QLabel#cropInfoLbl {{
                color: {Colors.ACCENT}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QFrame#effectControl, QFrame#audioMixControl {{
                background-color: {Colors.SURFACE_1}; border: none;
            }}
            QFrame#audioMixPanel {{ background: transparent; border: none; }}
            QLabel#effectName {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#effectValue {{
                color: {Colors.ACCENT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#audioMixName {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#audioMixValue {{
                color: {Colors.ACCENT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QPushButton#audioMixMute {{
                background: transparent; border: none;
                color: {Colors.TEXT_MUTED}; font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold;
                letter-spacing: 1px; padding: 0;
            }}
            QPushButton#audioMixMute:hover {{ color: {Colors.ACCENT}; }}
            QPushButton#audioMixMute:checked {{ color: {Colors.DELETE}; }}
            QSlider#effectSlider, QSlider#audioMixSlider {{
                background: transparent;
            }}

            /* Collapsible panels */
            QFrame#detailsPanel {{
                background-color: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER};
                border-radius: 0px;
            }}
            QPushButton#panelHeader {{
                background-color: transparent; border: none;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY}; font-weight: bold;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                padding: 10px 12px; text-align: left;
            }}
            QPushButton#panelHeader:hover {{ color: {Colors.ACCENT}; }}
            QFrame#panelBody {{ background: transparent; }}

            /* Key/value rows inside panels */
            QLabel#detailKey {{
                color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_LABEL}px; font-weight: bold;
                font-family: {Fonts.BODY}; letter-spacing: 0px; background: transparent;
            }}
            QLabel#detailVal {{
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY}px;
                font-family: {Fonts.BODY}; background: transparent;
            }}
            QLabel#detailValBig {{
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY_L}px; font-weight: bold;
                font-family: {Fonts.BODY}; background: transparent;
            }}

            /* Delete — outlined, independently themed */
            QPushButton#deleteBtn {{
                background: transparent; border: 1px solid {Colors.DELETE};
                border-radius: 0px;
                color: {Colors.DELETE}; font-size: {Fonts.SIZE_BODY_L}px; font-weight: bold;
                font-family: {Fonts.BODY};
            }}
            QPushButton#deleteBtn:hover {{ background-color: {Colors.DELETE}; color: {Colors.TEXT}; }}

            QSlider::groove:horizontal {{ background: {Colors.BORDER}; height: 2px; }}
            QSlider::handle:horizontal {{
                background: {Colors.ACCENT}; width: 10px; height: 10px;
                margin: -4px 0; border-radius: 0px;
            }}
            QSlider::handle:horizontal:hover {{ background: {Colors.TEXT}; }}
            QSlider::sub-page:horizontal {{ background: {Colors.ACCENT}; }}
        ''')

    # Reversible editor history

    def _restore_persisted_editor_draft(self):
        loader = getattr(self._mm, 'get_editor_draft', None)
        if not callable(loader):
            return
        try:
            state = _editor_snapshot_from_draft(loader(self.clip_path))
        except (OSError, TypeError, ValueError):
            state = None
        if state is None:
            return
        self._restore_editor_state(state, persist=False)
        self._last_persisted_snapshot = state

    def _schedule_editor_draft_save(self):
        if self._restoring_state or self._closing or self._discard_editor_draft:
            return
        if not callable(getattr(self._mm, 'set_editor_draft', None)):
            return
        self._draft_dirty = True
        self._draft_save_timer.start()

    def _flush_editor_draft(self):
        if (not self._draft_dirty or self._discard_editor_draft
                or not callable(getattr(self._mm, 'set_editor_draft', None))):
            return
        state = self._capture_editor_state()
        if state == self._last_persisted_snapshot:
            self._draft_dirty = False
            return
        try:
            saved = self._mm.set_editor_draft(
                self.clip_path, _editor_snapshot_to_draft(state))
        except (OSError, TypeError, ValueError) as error:
            print(f'[ClipViewer] editor draft save failed: {error}')
            return
        if saved is not False:
            self._last_persisted_snapshot = state
            self._draft_dirty = False

    def _capture_editor_state(self) -> EditorSnapshot:
        crop_rect = tuple(self._crop_rect) if self._crop_rect else None
        return EditorSnapshot(
            trim_start=self.trim_slider.start_pct,
            trim_end=self.trim_slider.end_pct,
            segments=tuple(
                (segment.start_pct, segment.end_pct, segment.deleted)
                for segment in self.trim_slider.segments),
            selected_segment=self.trim_slider.selected_segment,
            crop_rect=crop_rect,
            stretch_ratio=self._stretch_ratio,
            effects=tuple(sorted((key, int(value))
                                 for key, value in self._effects.items())),
            speed_rate=_normalise_speed(getattr(self, '_speed_rate', 1.0)),
            preserve_pitch=bool(getattr(self, '_preserve_pitch', True)),
        )

    def _commit_editor_change(self, before: EditorSnapshot):
        if self._restoring_state or before == self._capture_editor_state():
            return
        self._undo_stack.append(before)
        if len(self._undo_stack) > 100:
            self._undo_stack.pop(0)
        self._redo_stack.clear()
        self._update_history_controls()
        scheduler = getattr(self, '_schedule_editor_draft_save', None)
        if callable(scheduler):
            scheduler()

    def _restore_editor_state(self, state: EditorSnapshot, *, persist: bool = True):
        self._restoring_state = True
        try:
            self.trim_slider.start_pct = state.trim_start
            self.trim_slider.end_pct = state.trim_end
            self.trim_slider.segments = [
                TimelineSegment(start, end, deleted)
                for start, end, deleted in state.segments]
            self.trim_slider.selected_segment = max(
                0, min(state.selected_segment,
                       len(self.trim_slider.segments) - 1))
            self.trim_slider.update()
            self._crop_rect = state.crop_rect
            self._stretch_ratio = state.stretch_ratio
            self._speed_rate = _normalise_speed(state.speed_rate)
            self._preserve_pitch = bool(state.preserve_pitch)
            self._effects = dict(state.effects)
            for key, slider in self._effect_sliders.items():
                slider.setValue(self._effects.get(key, 0))
            self._on_segments_changed()
            self._update_crop_ui()
            self._on_stretch_changed(self._stretch_ratio)
            if hasattr(self, 'speed_combo'):
                index = self.speed_combo.findData(self._speed_rate)
                self.speed_combo.blockSignals(True)
                self.speed_combo.setCurrentIndex(max(0, index))
                self.speed_combo.blockSignals(False)
            if hasattr(self, 'preserve_pitch_toggle'):
                self.preserve_pitch_toggle.blockSignals(True)
                self.preserve_pitch_toggle.setChecked(self._preserve_pitch)
                self.preserve_pitch_toggle.blockSignals(False)
                self._update_pitch_hint()
            apply_rate = getattr(self, '_apply_playback_rate', None)
            if hasattr(self, 'player') and callable(apply_rate):
                apply_rate()
        finally:
            self._restoring_state = False

        ranges = self._kept_segments_ms()
        if ranges:
            try:
                position = self.player.position()
            except Exception:
                position = ranges[0][0]
            if not any(start <= position < end for start, end in ranges):
                self._on_seek_requested(ranges[0][0] / self.duration_ms)
        self._update_history_controls()
        if persist:
            scheduler = getattr(self, '_schedule_editor_draft_save', None)
            if callable(scheduler):
                scheduler()

    def _undo(self):
        if not self._undo_stack:
            return
        current = self._capture_editor_state()
        state = self._undo_stack.pop()
        self._redo_stack.append(current)
        self._restore_editor_state(state)

    def _redo(self):
        if not self._redo_stack:
            return
        current = self._capture_editor_state()
        state = self._redo_stack.pop()
        self._undo_stack.append(current)
        self._restore_editor_state(state)

    def _update_history_controls(self):
        if hasattr(self, 'undo_btn'):
            self.undo_btn.setEnabled(bool(self._undo_stack))
        if hasattr(self, 'redo_btn'):
            self.redo_btn.setEnabled(bool(self._redo_stack))

    def _begin_trim_edit(self):
        if not self._restoring_state and self._pending_trim_snapshot is None:
            self._pending_trim_snapshot = self._capture_editor_state()

    def _finish_trim_edit(self):
        before = self._pending_trim_snapshot
        self._pending_trim_snapshot = None
        if before is not None:
            self._commit_editor_change(before)

    def _begin_effect_adjustment(self):
        if not self._restoring_state and self._pending_effect_snapshot is None:
            self._pending_effect_snapshot = self._capture_editor_state()

    def _finish_effect_adjustment(self):
        before = self._pending_effect_snapshot
        self._pending_effect_snapshot = None
        # Never leave the final slider position waiting behind the coalescing
        # timer. The drag stays cheap, while release always feels exact.
        self._refresh_live_preview(immediate=True)
        if before is not None:
            self._commit_editor_change(before)

    # Playback

    def _apply_playback_rate(self):
        """Apply one speed to the video clock and any live audio mixer."""

        speed = _normalise_speed(getattr(self, '_speed_rate', 1.0))
        self._speed_rate = speed
        preserve_pitch = bool(getattr(self, '_preserve_pitch', True))
        try:
            set_pitch_compensation = getattr(self.player, 'setPitchCompensation', None)
            if callable(set_pitch_compensation):
                set_pitch_compensation(preserve_pitch)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        try:
            self.player.setPlaybackRate(speed)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        # MediaFoundation commonly reports that it accepts pitch compensation
        # while still coupling pitch to rate.  Keep its cheap direct path at
        # 1×, but move a direct track to our FFmpeg path whenever a changed
        # speed must preserve pitch.
        if (preserve_pitch and abs(speed - 1.0) > 1e-6
                and getattr(self, '_audio_mixer', None) is None
                and getattr(self, '_playback_sources', ())):
            self._prepare_audio_mixer(self._playback_sources)
        mixer = getattr(self, '_audio_mixer', None)
        if mixer is not None:
            try:
                mixer.set_playback_rate(
                    speed, int(self.player.position()),
                    preserve_pitch=preserve_pitch)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass

    def _on_speed_changed(self, index: int):
        rate = self.speed_combo.itemData(index)
        before = None if self._restoring_state else self._capture_editor_state()
        self._speed_rate = _normalise_speed(rate)
        self._apply_playback_rate()
        if before is not None:
            self._commit_editor_change(before)

    def _update_pitch_hint(self):
        if not hasattr(self, 'pitch_mode_hint'):
            return
        if getattr(self, '_preserve_pitch', True):
            self.pitch_mode_hint.setText(
                'Audio stays at its original pitch while speed changes.')
        else:
            self.pitch_mode_hint.setText(
                'Audio pitch follows the selected playback speed.')

    def _on_preserve_pitch_toggled(self, checked: bool):
        before = None if self._restoring_state else self._capture_editor_state()
        self._preserve_pitch = bool(checked)
        self._update_pitch_hint()
        self._apply_playback_rate()
        if before is not None:
            self._commit_editor_change(before)

    def keyPressEvent(self, event):
        # QShortcut handles child focus; this fallback covers synthetic and
        # platform-specific key delivery directly to the dialog.
        if event.key() == Qt.Key.Key_Escape:
            self._close()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Space:
            self._toggle_play()
            event.accept()
            return
        super().keyPressEvent(event)

    def _toggle_play(self):
        if (self._closing or self._player_lifecycle_state in {
                PlayerLifecycleState.FAILED,
                PlayerLifecycleState.CLOSING,
                PlayerLifecycleState.CLOSED}):
            return
        # Re-entrancy guard: setIcon() does not recurse, but the button is
        # checkable and any future change to programmatic-toggle behavior
        # could. The flag is also useful as a coarse "command in flight"
        # marker to keep us honest about state transitions.
        if self._play_pause_busy:
            return
        self._play_pause_busy = True
        try:
            # Always derive the desired action from the player's reported state
            # rather than from the QPushButton's checked state. The two can drift
            # if Qt buffers a click while the previous play()/pause() is still
            # being processed by MediaFoundation, which is exactly the scenario
            # the user hit when spamming the button.
            try:
                state = self.player.playbackState()
            except Exception:
                state = QMediaPlayer.PlaybackState.StoppedState

            want_play = state != QMediaPlayer.PlaybackState.PlayingState
            if want_play and self._play_when_ready:
                # A second click while preparation is pending is a real cancel,
                # not another queued play command.
                self._play_when_ready = False
                self.play_btn.blockSignals(True)
                self.play_btn.setChecked(False)
                self.play_btn.blockSignals(False)
                self._set_playback_button_visual(False)
                return
            if want_play and not self._playback_ready:
                self._play_when_ready = True
                self._set_player_state(PlayerLifecycleState.PLAY_QUEUED)
                self._playback_requested_at = time.monotonic()
                self._playback_stall_timer.start()
                emit_event('playback', 'play_requested', state='PLAY_QUEUED',
                           media_ready=self._media_ready,
                           audio_ready=self._audio_preparation_ready)
                self._playback_status_lbl.setText('PREPARING · PLAY QUEUED')
                self.play_btn.blockSignals(True)
                self.play_btn.setChecked(True)
                self.play_btn.blockSignals(False)
                self._set_playback_button_visual(None)
                return
            try:
                if want_play:
                    # A play click can beat the scrub debounce. Apply its
                    # latest destination before starting either video/audio,
                    # so playback never starts then immediately flushes again.
                    self._seek_timer.stop()
                    self._flush_pending_seek()
                    self._playback_requested_at = time.monotonic()
                    emit_event('playback', 'play_requested', state='REQUESTED')
                    self._timeline_prepare_timer.stop()
                    self.trim_slider.cancel_thumbnail_loading()
                    position = self.player.position()
                    if state == QMediaPlayer.PlaybackState.PausedState:
                        anchor = max(0, int(position))
                        if self._last_stable_position_ms > anchor + 250:
                            anchor = self._last_stable_position_ms
                        self._resume_anchor_ms = anchor
                        self._resume_guard_deadline = time.monotonic() + 0.75
                    playable = self._kept_segments_ms()
                    if playable:
                        containing = next(
                            ((start, end) for start, end in playable
                             if start <= position < end - 60), None)
                        if containing is None:
                            next_range = next(
                                ((start, end) for start, end in playable if start > position),
                                playable[0])
                            self.player.setPosition(next_range[0])
                    self.player.play()
                else:
                    emit_event('playback', 'pause_requested', state='REQUESTED')
                    self._resume_anchor_ms = None
                    self._last_stable_position_ms = max(
                        0, int(self.player.position()))
                    self.player.pause()
            except Exception as e:
                print(f'[ClipViewer] play/pause command rejected: {e}')

            # Reflect the *requested* state on the button so the icon doesn't
            # lag the click. _on_state_changed will reconcile if the player
            # ends up somewhere else (e.g. StoppedState at end-of-clip).
            self.play_btn.blockSignals(True)
            self.play_btn.setChecked(want_play)
            self.play_btn.blockSignals(False)
            self._set_playback_button_visual(want_play)
        finally:
            self._play_pause_busy = False

    def _set_playback_button_visual(self, playing: bool | None) -> None:
        """Keep the icon/text legible against the button's checked state."""

        if playing is None:
            self.play_btn.setIcon(QIcon())
            self.play_btn.setText('PREPARING…')
            return
        icon = (self._pause_active_icon if playing else self._play_icon)
        label = '⏸  PAUSE' if playing else '▶  PLAY'
        if icon.isNull():
            self.play_btn.setIcon(QIcon())
            self.play_btn.setText(label)
        else:
            self.play_btn.setIcon(icon)
            self.play_btn.setText('')

    def _on_state_changed(self, state):
        if (self._closing
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        # Keep the button visually consistent with whatever the player ends up
        # doing — including auto-stop at end of clip.
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self._set_player_state(PlayerLifecycleState.PLAYING)
            self._playback_stall_timer.stop()
            emit_event(
                'playback', 'playing', state='PLAYING',
                elapsed_ms=(round(
                    (time.monotonic() - self._playback_requested_at) * 1000)
                    if self._playback_requested_at else None))
            self._timeline_prepare_timer.stop()
            self.trim_slider.cancel_thumbnail_loading()
            if not self._ph_timer.isActive():
                self._ph_timer.start()
            if self._audio_mixer is not None:
                mixer_position = self._guarded_display_position(
                    self.player.position())
                self._audio_mixer.play(mixer_position)
            self.play_btn.blockSignals(True)
            self.play_btn.setChecked(True)
            self.play_btn.blockSignals(False)
            self._set_playback_button_visual(True)
        else:
            self._set_player_state(
                PlayerLifecycleState.PAUSED
                if state == QMediaPlayer.PlaybackState.PausedState
                else PlayerLifecycleState.STOPPED)
            emit_event(
                'playback',
                ('paused' if state == QMediaPlayer.PlaybackState.PausedState
                 else 'stopped'),
                state=('PAUSED' if state == QMediaPlayer.PlaybackState.PausedState
                       else 'STOPPED'))
            self._ph_timer.stop()
            self._update_playhead()
            if self._audio_mixer is not None:
                self._audio_mixer.pause()
            # PausedState OR StoppedState — both show the play glyph.
            self.play_btn.blockSignals(True)
            self.play_btn.setChecked(False)
            self.play_btn.blockSignals(False)
            self._set_playback_button_visual(False)
            # A rapid pause/resume should only touch the already-open player.
            # Give the user a generous idle window before preparing any late
            # audio mixer or opening OpenCV's second decoder for the filmstrip.
            delay = (1500 if state == QMediaPlayer.PlaybackState.PausedState
                     else 300)
            self._schedule_timeline_thumbnails(delay)
            if self._prepared_playback_path:
                QTimer.singleShot(0, self._apply_prepared_playback_source)

    def _update_playhead(self):
        try:
            pos = self.player.position()
        except Exception:
            return
        display_pos = self._guarded_display_position(pos)
        if self.duration_ms > 0 and self.trim_slider.dragging != 'playhead':
            self.trim_slider.set_playhead(display_pos / self.duration_ms)
        self.time_label.setText(
            f'{self._fmt(display_pos)} / {self._fmt(self.duration_ms)}')

        # Preview the edit, not the discarded source: playback hops over
        # deleted segments and loops back to the first kept frame at edit end.
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            ranges = self._kept_segments_ms()
            if ranges:
                current_index = next(
                    (index for index, (start, end) in enumerate(ranges)
                     if start - 25 <= pos < end - 25), None)
                if current_index is None:
                    next_range = next(
                        ((start, end) for start, end in ranges if start > pos), None)
                    if next_range:
                        self.player.setPosition(next_range[0])
                        if self._audio_mixer is not None:
                            self._audio_mixer.seek(next_range[0])
                        return
                    self.player.pause()
                    self.player.setPosition(ranges[0][0])
                    if self._audio_mixer is not None:
                        self._audio_mixer.seek(ranges[0][0])
                    return
        if (self._audio_mixer is not None
                and self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState):
            self._audio_mixer.sync_to_video_position(pos)

    def _on_seek_requested(self, pct: float):
        # Coalesce: the slider can fire this 100+ times per second during a
        # drag. We remember only the most-recent target and let the timer
        # apply it once playback can keep up.
        self._timeline_prepare_timer.stop()
        self.trim_slider.cancel_thumbnail_loading()
        self._pending_seek_ms = max(0, min(self.duration_ms,
                                            int(pct * self.duration_ms)))
        self._resume_anchor_ms = None
        self._last_stable_position_ms = self._pending_seek_ms
        self.trim_slider.set_playhead(pct)
        # A running single-shot timer is restarted here, making this a true
        # debounce.  The previous throttle-style behaviour issued a native
        # seek every 50 ms during a scrub even when the preceding seek had not
        # settled yet, which could leave both video and audio pipelines
        # repeatedly flushing the same deterministic point.
        self._seek_timer.start()

    def _flush_pending_seek(self):
        if self._pending_seek_ms is None:
            return
        target = self._pending_seek_ms
        self._pending_seek_ms = None
        emit_event('playback', 'seek', target_ms=target)
        if self._audio_mixer is not None:
            self._audio_mixer.seek(target)
        try:
            self.player.setPosition(target)
        except Exception as e:
            # Swallow MediaFoundation hiccups so the editor stays alive even
            # when the underlying session is in a transitional state.
            print(f'[ClipViewer] setPosition rejected ({target} ms): {e}')

    def _on_player_error(self, *args):
        # QMediaPlayer.errorOccurred fires for unsupported codecs, locked
        # files, and a few transient seek failures. Logging keeps the editor
        # alive — the alternative was an uncaught exception ricocheting up
        # through the Qt event loop.
        if (not self._closing and self._playback_path != self.clip_path):
            # A deleted/corrupt disposable preview cannot invalidate the clip.
            from core.playback_proxy import discard_playback_cache
            discard_playback_cache(self.clip_path, self._playback_path)
            self._prepared_playback_path = self.clip_path
            QTimer.singleShot(0, self._apply_prepared_playback_source)
            return
        try:
            err_str = self.player.errorString()
        except Exception:
            err_str = '(unknown)'
        print(f'[ClipViewer] media player error: {err_str}')
        emit_event(
            'playback', 'backend_error', state='FAILED',
            error=DiagnosticError.PLAYBACK_DECODER_FAILED,
            detail=err_str)
        if not self._closing:
            self._fail_playback(err_str, DiagnosticError.PLAYBACK_DECODER_FAILED)

    # Audio mix controls

    def _toggle_volume_popup(self):
        """Compatibility hook for older callers; the mixer now lives in the sidebar."""
        self._refresh_audio_mix_panel()

    def _refresh_audio_mix_panel(self):
        panel = getattr(self, '_audio_mix_panel', None)
        if panel is None:
            return
        panel.set_state(
            master_vol=self._master_volume,
            source_volumes=self._source_volumes,
            source_mutes=self._source_mutes,
            source_tracks=self._playback_sources,
            live_preview=(self._audio_mixer_live
                          or self._native_audio_source_id is not None),
        )

    def _on_master_volume_changed(self, value: int):
        self._master_volume = value
        if self._audio_mixer is not None:
            self._audio_mixer.set_master_percent(value)
        if self._audio_mixer_live:
            self._schedule_mix_refresh()
        else:
            self._apply_container_audio_volume()
        if self.sm:
            self.sm.set('master_volume', value)
            self.sm.save_settings()

    def _on_source_volume_changed(self, key: str, value: int):
        self._source_volumes[key] = value
        if self._audio_mixer is not None:
            self._audio_mixer.set_source_state(key, gain_percent=value)
            if self._audio_mixer_live:
                self._schedule_mix_refresh()
        if key == self._native_audio_source_id:
            self._apply_container_audio_volume()

    def _on_source_muted(self, key: str, muted: bool):
        self._source_mutes[key] = muted
        if self._audio_mixer is not None:
            self._audio_mixer.set_source_state(key, muted=muted)
            if self._audio_mixer_live:
                self._schedule_mix_refresh()
        if key == self._native_audio_source_id:
            self._apply_container_audio_volume()

    def _apply_container_audio_volume(self):
        source_gain = 1.0
        source_id = self._native_audio_source_id
        if source_id is not None:
            source_gain = (
                0.0 if self._source_mutes.get(source_id, False)
                else self._source_volumes.get(source_id, 100) / 100.0)
        self.audio_output.setMuted(False)
        self.audio_output.setVolume(
            max(0.0, min(1.0, self._master_volume / 100.0 * source_gain)))

    def _schedule_mix_refresh(self):
        if not self._mix_refresh_timer.isActive():
            self._mix_refresh_timer.start()

    def _refresh_live_mix(self):
        if self._audio_mixer is not None and self._audio_mixer_live:
            self._audio_mixer.refresh_mix(self.player.position())

    # Discovery and native decoder creation happen away from the UI thread.
    _audio_sources_discovered = Signal(object, str)

    def _discover_audio_sources_async(self):
        clip_path = self.clip_path
        signal = self._audio_sources_discovered
        cancelled = self._prepare_cancel

        def _worker():
            if cancelled.is_set():
                return
            try:
                sources = discover_playback_sources(clip_path)
            except PlaybackError as error:
                sources, detail = (), str(error)
            else:
                detail = ''
            if cancelled.is_set():
                return
            try:
                signal.emit(sources, detail)
            except RuntimeError:
                return

        threading.Thread(target=_worker, name='FTHR-audio-probe', daemon=True).start()

    def _on_audio_sources_discovered(self, sources, error: str):
        if (self._closing
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        if error:
            print(f'[ClipViewer] audio source probe failed: {error}')
            emit_event(
                'playback', 'audio_probe_failed', state='DEGRADED',
                error=DiagnosticError.AUDIO_OUTPUT_INIT_FAILED,
                detail=error)
            self._audio_preparation_ready = True
            self._update_playback_readiness()
            return
        self._playback_sources = tuple(sources)
        emit_event(
            'playback', 'audio_sources_discovered',
            source_count=len(self._playback_sources),
            available_source_count=sum(
                1 for source in self._playback_sources if source.available))
        self._audio_tracks = tuple(
            (source.source_id, source.audio_index)
            for source in self._playback_sources
            if source.available and source.audio_index is not None)
        self._multitrack_audio = bool(self._audio_tracks)
        for source in self._playback_sources:
            self._source_volumes.setdefault(source.source_id, 100)
            self._source_mutes.setdefault(source.source_id, False)
        available = tuple(
            source for source in self._playback_sources
            if source.available and source.audio_index is not None)
        # A single ordinary track stays on QMediaPlayer at 1×. On the Windows
        # backend its pitch-compensation request is only advisory, however, so
        # changed-speed preview promotes it to the controlled FFmpeg path.
        if len(available) == 1 and available[0].mix_role == 'direct':
            if (bool(getattr(self, '_preserve_pitch', True))
                    and abs(_normalise_speed(self._speed_rate) - 1.0) > 1e-6):
                self._native_audio_source_id = None
                self._prepare_audio_mixer(self._playback_sources)
            else:
                self._native_audio_source_id = (
                    available[0].source_id if available[0].editable else None)
                self._audio_preparation_ready = True
                emit_event('playback', 'audio_initialized', state='READY',
                           backend='qt-container')
                self._apply_container_audio_volume()
                self._update_playback_readiness()
        elif available:
            self._native_audio_source_id = None
            if (self._audio_prepare_timed_out
                    and self.player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState):
                self._deferred_audio_mixer_sources = self._playback_sources
            else:
                self._prepare_audio_mixer(self._playback_sources)
        else:
            self._native_audio_source_id = None
            self._audio_preparation_ready = True
            emit_event('playback', 'audio_initialized', state='READY',
                       backend='qt-container', source_count=0)
            self._apply_container_audio_volume()
            self._update_playback_readiness()
        self._refresh_audio_mix_panel()

    def _prepare_audio_mixer(self, sources):
        if (self._closing or self._audio_mixer is not None
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        self._deferred_audio_mixer_sources = ()
        self._audio_prepare_deadline.stop()
        self._audio_preparation_ready = False
        self._playback_ready = False
        self._playback_preparing_at = time.monotonic()
        self._playback_status_lbl.setText('PREPARING AUDIO…')
        try:
            self._audio_mixer = FFmpegPlaybackController(
                self.clip_path, sources, self)
            self._audio_mixer.ready_changed.connect(self._on_audio_mixer_ready)
            self._audio_mixer.audio_failed.connect(self._on_audio_mixer_failed)
            self._audio_mixer.source_failed.connect(self._on_audio_source_failed)
            self._audio_mixer.reached_eof.connect(self._on_audio_mixer_eof)
            self._audio_mixer.set_master_percent(self._master_volume)
            for source in sources:
                self._audio_mixer.set_source_state(
                    source.source_id,
                    gain_percent=self._source_volumes[source.source_id],
                    muted=self._source_mutes[source.source_id])
            self._audio_mixer.set_playback_rate(
                self._speed_rate, int(self.player.position()),
                preserve_pitch=bool(getattr(self, '_preserve_pitch', True)))
            self._audio_mixer.start()
        except PlaybackError as mixer_error:
            print(f'[ClipViewer] custom audio mixer unavailable: {mixer_error}')
            emit_event(
                'playback', 'audio_init_failed', state='DEGRADED',
                error=DiagnosticError.AUDIO_OUTPUT_INIT_FAILED,
                backend='ffmpeg-qtaudiosink', detail=str(mixer_error))
            self._audio_mixer = None
            self._audio_preparation_ready = True
            self._apply_container_audio_volume()
            self._update_playback_readiness()

    def _on_audio_mixer_ready(self, ready: bool, detail: str):
        if (self._closing
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        self._audio_mixer_live = ready
        if ready and self._audio_mixer is not None:
            emit_event('playback', 'audio_initialized', state='READY',
                       backend='ffmpeg-qtaudiosink')
            # Switch only after the custom path has proven it can decode. This
            # prevents a missed/failed readiness signal from stranding Qt at 0.
            if (self.player.playbackState()
                    == QMediaPlayer.PlaybackState.PlayingState):
                self._audio_mixer.play(int(self.player.position()))
            self.audio_output.setMuted(True)
            self._audio_preparation_ready = True
            self._update_playback_readiness()
            self._refresh_audio_mix_panel()
            return
        print(f'[ClipViewer] custom audio mixer failed to start: {detail}')
        emit_event(
            'playback', 'audio_init_failed', state='DEGRADED',
            error=DiagnosticError.AUDIO_OUTPUT_INIT_FAILED,
            backend='ffmpeg-qtaudiosink', detail=detail)
        self._fall_back_to_container_audio()

    def _on_audio_mixer_failed(self, detail: str):
        if (self._closing
                or self._player_lifecycle_state in {
                    PlayerLifecycleState.FAILED,
                    PlayerLifecycleState.CLOSING,
                    PlayerLifecycleState.CLOSED,
                }):
            return
        print(f'[ClipViewer] custom audio mixer error: {detail}')
        emit_event(
            'playback', 'audio_failed', state='FAILED',
            error=DiagnosticError.AUDIO_OUTPUT_INIT_FAILED,
            backend='ffmpeg-qtaudiosink', detail=detail)
        self._fall_back_to_container_audio()

    def _fall_back_to_container_audio(self):
        mixer = self._audio_mixer
        self._audio_mixer = None
        self._audio_mixer_live = False
        if mixer is not None:
            mixer.stop()
            mixer.deleteLater()
        self._audio_preparation_ready = True
        self._apply_container_audio_volume()
        self._update_playback_readiness()
        self._refresh_audio_mix_panel()

    def _on_audio_source_failed(self, source_id: str):
        # Keep failed source rows visible with disabled controls while healthy
        # stems continue playing.
        self._playback_sources = tuple(
            replace(source, available=False) if source.source_id == source_id else source
            for source in self._playback_sources)
        refresh_panel = getattr(self, '_refresh_audio_mix_panel', None)
        if callable(refresh_panel):
            refresh_panel()

    def _on_audio_mixer_eof(self):
        # QMediaPlayer remains the media clock and will normally stop at the
        # same point. Do not issue a competing seek or restart here.
        return

    # Trim

    def _zoom_timeline(self, multiplier: float):
        self.trim_slider.set_zoom(self.trim_slider.zoom_factor * multiplier)
        self.timeline_zoom_lbl.setText(f'{int(round(self.trim_slider.zoom_factor * 100))}%')

    def _split_at_playhead(self):
        before = self._capture_editor_state()
        if self.trim_slider.split_at_playhead():
            self._commit_editor_change(before)
        else:
            self.split_btn.setText('MOVE PLAYHEAD')
            QTimer.singleShot(1200, lambda: self.split_btn.setText('SPLIT'))

    def _split_at_position(self, pct: float):
        self.trim_slider.set_playhead(pct)
        self._on_seek_requested(pct)
        self._split_at_playhead()

    def _toggle_selected_segment(self):
        before = self._capture_editor_state()
        if self.trim_slider.toggle_selected_deleted():
            self._commit_editor_change(before)
            position = self.player.position()
            ranges = self._kept_segments_ms()
            if ranges and not any(start <= position < end for start, end in ranges):
                next_start = next((start for start, _ in ranges if start > position),
                                  ranges[0][0])
                self._on_seek_requested(next_start / self.duration_ms)

    def _toggle_segment_at(self, index: int):
        if not (0 <= index < len(self.trim_slider.segments)):
            return
        self.trim_slider.selected_segment = index
        selected = self.trim_slider.segments[index]
        self.trim_slider.selection_changed.emit(index, selected.deleted)
        self.trim_slider.update()
        self._toggle_selected_segment()

    def _on_segment_selected(self, index: int, deleted: bool):
        enabled = len(self.trim_slider.segments) > 1
        self.delete_segment_btn.setEnabled(enabled)
        self.delete_segment_btn.setText('RESTORE SEGMENT' if deleted else 'DELETE SEGMENT')
        self.delete_segment_btn.setProperty('restore', 'true' if deleted else 'false')
        self.delete_segment_btn.style().unpolish(self.delete_segment_btn)
        self.delete_segment_btn.style().polish(self.delete_segment_btn)

    def _on_segments_changed(self):
        selected = self.trim_slider.segments[self.trim_slider.selected_segment]
        self._on_segment_selected(self.trim_slider.selected_segment, selected.deleted)
        ranges = self._kept_segments_ms()
        kept_duration = sum(end - start for start, end in ranges)
        cut_count = max(0, len(self.trim_slider.segments) - 1)
        self.trim_range_lbl.setText(
            f'{self._fmt(kept_duration)} kept  ·  {cut_count} cut'
            f'{"s" if cut_count != 1 else ""}')

    def _kept_segments_ms(self) -> list[tuple[int, int]]:
        return [
            (int(start * self.duration_ms), int(end * self.duration_ms))
            for start, end in self.trim_slider.kept_ranges()
        ]

    def _kept_segments_seconds(self) -> list[tuple[float, float]]:
        return [(start / 1000, end / 1000)
                for start, end in self._kept_segments_ms()]

    def _on_trim_changed(self, start_pct: float, end_pct: float):
        s = int(start_pct * self.duration_ms)
        e = int(end_pct   * self.duration_ms)
        self.trim_range_lbl.setText(f'{self._fmt(s)} → {self._fmt(e)}')
        try:
            position = self.player.position()
        except Exception:
            position = 0
        if position < s or position >= e:
            # If the trim start moves beyond the current view/playhead, jump
            # straight to the start of the kept area and reveal it in zoom.
            target = next((start for start, _ in self._kept_segments_ms()), s)
            self.trim_slider.ensure_visible(target / self.duration_ms, align_start=True)
            self._on_seek_requested(target / self.duration_ms)
        self._schedule_editor_draft_save()

    # Color effects / stretch

    def _on_effect_changed(self, key: str, value: int):
        before = None
        if not self._restoring_state and self._pending_effect_snapshot is None:
            before = self._capture_editor_state()
        self._effects[key] = int(value)
        label = self._effect_value_labels.get(key)
        if label is not None:
            label.setText(f'{value:+d}' if value else '0')
        self._refresh_live_preview()
        self._schedule_editor_draft_save()
        if before is not None:
            self._commit_editor_change(before)

    def _reset_effects(self):
        before = self._capture_editor_state()
        was_restoring = self._restoring_state
        self._restoring_state = True
        try:
            for slider in self._effect_sliders.values():
                slider.setValue(0)
        finally:
            self._restoring_state = was_restoring
        self._refresh_live_preview(immediate=True)
        self._commit_editor_change(before)

    def _open_stretch_dialog(self):
        was_playing = (
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState)
        if was_playing:
            self.player.pause()
        before = self._capture_editor_state()
        dialog = StretchDialog(
            self.clip_path,
            self.player.position(),
            self._src_w,
            self._src_h,
            crop_rect=self._crop_rect,
            initial_ratio=self._stretch_ratio,
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._on_stretch_changed(dialog.stretch_ratio)
            self._commit_editor_change(before)
        if was_playing:
            self._toggle_play()

    def _on_stretch_changed(self, ratio: float):
        self._stretch_ratio = max(0.25, min(4.0, float(ratio)))
        self._refresh_live_preview(immediate=True)

    def _reset_stretch(self):
        before = self._capture_editor_state()
        self._stretch_ratio = 1.0
        self._refresh_live_preview(immediate=True)
        self._commit_editor_change(before)

    # Crop

    def _open_crop_dialog(self):
        was_playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        if was_playing:
            self.player.pause()
        before = self._capture_editor_state()
        dlg = CropDialog(self.clip_path, initial_crop=self._crop_rect, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._crop_rect = dlg.crop_rect
            self._update_crop_ui()
            self._commit_editor_change(before)
        if was_playing:
            self._toggle_play()

    def _update_crop_ui(self):
        if self._crop_rect:
            x, y, w, h = self._crop_rect
            self.crop_info_lbl.setText(f'{w} × {h}')
            self.crop_btn.setProperty('active', 'true')
            self.crop_btn.setText('EDIT CROP')
            self.save_qc_btn.setEnabled(True)
        else:
            self.crop_info_lbl.setText('')
            self.crop_btn.setProperty('active', 'false')
            self.crop_btn.setText('SET CROP')
            self.save_qc_btn.setEnabled(False)
        self.crop_btn.style().unpolish(self.crop_btn)
        self.crop_btn.style().polish(self.crop_btn)
        # Refresh the crop-preview overlay so the dimmed regions update immediately
        self._relayout_video()
        self._on_stretch_changed(self._stretch_ratio)

    # Quick crop

    def _refresh_quick_crop_btn(self):
        qc = self.sm.get('quick_crop') if self.sm else None
        if qc and all(k in qc for k in ('x', 'y', 'w', 'h')):
            sw, sh = qc.get('src_w', 0), qc.get('src_h', 0)
            self.quick_crop_btn.setToolTip(f"Saved: {qc['w']}×{qc['h']} ({sw}×{sh} source)")
            self.quick_crop_btn.setEnabled(True)
            self.quick_crop_btn.setProperty('saved', 'true')
        else:
            self.quick_crop_btn.setToolTip('No quick crop saved yet')
            self.quick_crop_btn.setEnabled(False)
            self.quick_crop_btn.setProperty('saved', 'false')
        self.quick_crop_btn.style().unpolish(self.quick_crop_btn)
        self.quick_crop_btn.style().polish(self.quick_crop_btn)

    def _apply_quick_crop(self):
        if not self.sm:
            return
        qc = self.sm.get('quick_crop')
        if not qc:
            return
        sx = self._src_w / max(qc.get('src_w', 1), 1)
        sy = self._src_h / max(qc.get('src_h', 1), 1)
        x  = int(qc['x'] * sx)
        y  = int(qc['y'] * sy)
        w  = min(int(qc['w'] * sx), self._src_w - x)
        h  = min(int(qc['h'] * sy), self._src_h - y)
        if w > 4 and h > 4:
            before = self._capture_editor_state()
            self._crop_rect = (x, y, w, h)
            self._update_crop_ui()
            self._commit_editor_change(before)

    def _save_quick_crop(self):
        if not self.sm or not self._crop_rect:
            return
        x, y, w, h = self._crop_rect
        self.sm.set('quick_crop', {'x': x, 'y': y, 'w': w, 'h': h,
                                   'src_w': self._src_w, 'src_h': self._src_h})
        self.sm.save_settings()
        self._refresh_quick_crop_btn()
        orig = self.save_qc_btn.text()
        self.save_qc_btn.setText('✓  SAVED')
        QTimer.singleShot(1800, lambda: self.save_qc_btn.setText(orig))

    # Export

    def _on_upload_click(self):
        # Don't show 'Queued ✓' when the manager will silently drop the
        # request — the user would believe the clip was uploaded.
        if not self._upload_enabled:
            self.upload_btn.setText('Upload off')
            self.export_error.emit(
                'UPLOAD NOT CONFIGURED',
                'Install and enable the optional uploader in Upload Settings first.',
                'warning',
            )
            QTimer.singleShot(2500, lambda: self.upload_btn.setText('Upload'))
            return
        self.upload_requested.emit(self.clip_path)
        self.upload_btn.setText('Queued ✓')
        self.upload_btn.setEnabled(False)
        QTimer.singleShot(2500, lambda: (
            self.upload_btn.setText('Upload'),
            self.upload_btn.setEnabled(True),
        ))

    def _on_export_button(self):
        """Start one export or cancel the currently owned export job."""

        worker = getattr(self, '_export_thread', None)
        if worker is not None and worker.is_alive():
            self._export_cancel.set()
            job = getattr(self, '_export_job', None)
            if job is not None:
                job.cancel()
            self.export_btn.setText('CANCELLING…')
            self.export_btn.setEnabled(False)
            return
        self._export_clip()

    def _export_clip(self):
        worker = getattr(self, '_export_thread', None)
        if worker is not None and worker.is_alive():
            return
        self._export_cancel = threading.Event()
        self._export_diagnostic_started_at = time.monotonic()
        emit_event('export', 'requested', state='REQUESTED')
        segments = self._kept_segments_seconds()
        if not segments:
            self.export_error.emit(
                'EXPORT FAILED', 'The edit has no kept segments.', 'warning')
            return
        start_s = segments[0][0]
        end_s = segments[-1][1]
        kept_duration = sum(end - start for start, end in segments)
        # Zero/negative trim windows make ffmpeg fail with a cryptic error —
        # validate before touching the button state.
        if kept_duration < 0.1:
            self.export_error.emit(
                'EXPORT FAILED',
                'Trim window is empty — drag the trim handles apart first.',
                'warning',
            )
            return

        self.export_btn.setText('CANCEL EXPORT')
        self.export_btn.setEnabled(True)
        emit_event('export', 'preparing', state='PREPARING',
                   segment_count=len(segments),
                   kept_duration_seconds=round(kept_duration, 3))

        export_dir = clips_directory_from(self.sm) / 'Exported'
        try:
            export_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Without this the exception escapes the slot and the button
            # stays disabled on 'EXPORTING...' forever.
            self.export_btn.setText('EXPORT CLIP')
            self.export_btn.setEnabled(True)
            self.export_error.emit(
                'EXPORT FAILED',
                f'Cannot create export folder: {e}',
                'error',
            )
            emit_event(
                'export', 'init_failed', state='FAILED',
                error=DiagnosticError.EXPORT_INIT_FAILED,
                detail=f'{type(e).__name__}: {e}')
            return
        from datetime import datetime as _dt
        ts = _dt.now().strftime('%H-%M-%S')
        out_path = export_dir / f'{Path(self.clip_path).stem}_export_{ts}.mp4'
        # Two exports in the same second (Export + Share side by side) must
        # not overwrite each other — ffmpeg runs with -y.
        n = 2
        while out_path.exists():
            out_path = export_dir / f'{Path(self.clip_path).stem}_export_{ts}_{n}.mp4'
            n += 1
        out = str(out_path)
        crop_rect = self._crop_rect
        effects = dict(self._effects)
        stretch_ratio = self._stretch_ratio

        self._export_thread = threading.Thread(
            target=self._export_worker,
            args=(start_s, end_s, out, crop_rect, segments, effects, stretch_ratio),
            name='fthr-editor-export', daemon=True,
        )
        self._export_thread.start()

    def _build_export_cmd(self, ffmpeg: str, start_s: float, duration_s: float,
                          out_path: str, crop_rect, video_args: list,
                          segments: list[tuple[float, float]] | None = None,
                          effects: dict | None = None,
                          stretch_ratio: float = 1.0,
                          audio_bitrate: str = '192k',
                          extra_video_filters: list[str] | None = None,
                          force_video_encode: bool = False,
                          force_audio_encode: bool = False) -> list:
        """Build the editor export command.

        Use video_args when edits require re-encoding; stream-copy compatible
        video when no visual transform is needed.
        """
        clip_path = getattr(self, 'clip_path', getattr(self, '_clip_path', ''))
        selected_segments = list(segments or [(start_s, start_s + duration_s)])
        selected_segments = [
            (max(0.0, float(start)), max(0.0, float(end)))
            for start, end in selected_segments if end - start >= 0.001
        ]
        if not selected_segments:
            raise ValueError('at least one non-empty segment is required')

        speed_rate = _normalise_speed(getattr(self, '_speed_rate', 1.0))
        speed_audio = _speed_audio_chain(
            speed_rate, bool(getattr(self, '_preserve_pitch', True)))
        video_filters = _video_filter_chain(
            crop_rect, effects, stretch_ratio, speed_rate=speed_rate)
        video_filters.extend(extra_video_filters or [])
        settings = getattr(self, 'sm', None)
        if settings is not None:
            watermark_enabled = bool(settings.get('watermark_enabled', False))
        else:
            watermark_enabled = bool(
                getattr(self, '_watermark_enabled', False))
        use_video_filter = bool(video_filters) or watermark_enabled
        use_audio_filter = bool(getattr(self, '_audio_tracks', ()))

        # Multiple kept ranges require a filter concat. A single range retains
        # the efficient stream-copy path used by the editor before this feature.
        if len(selected_segments) > 1:
            base = [ffmpeg, '-y', '-i', clip_path]
            filters: list[str] = []
            video_labels: list[str] = []
            for index, (segment_start, segment_end) in enumerate(selected_segments):
                chain = [
                    f'trim=start={segment_start:.6f}:end={segment_end:.6f}',
                    'setpts=PTS-STARTPTS',
                    *video_filters,
                ]
                filters.append(f'[0:v]{",".join(chain)}[v{index}]')
                video_labels.append(f'[v{index}]')

            audio_labels: list[str] = []
            states = {
                source.source_id: SourceMixState(
                    gain_percent=getattr(self, '_source_volumes', {}).get(
                        source.source_id, 100),
                    muted=getattr(self, '_source_mutes', {}).get(
                        source.source_id, False))
                for source in getattr(self, '_playback_sources', ())
            }
            has_audio = bool(getattr(self, '_playback_sources', ()))
            if use_audio_filter:
                mix_filters, audio_source = ffmpeg_mix_filter(
                    getattr(self, '_playback_sources', ()), states,
                    getattr(self, '_master_volume', 100), output_label='amixed')
                filters.extend(mix_filters)
                has_audio = audio_source is not None
                if has_audio:
                    split_labels = ''.join(f'[amix{index}]'
                                           for index in range(len(selected_segments)))
                    filters.append(
                        f'{audio_source}asplit={len(selected_segments)}{split_labels}')
                    for index, (segment_start, segment_end) in enumerate(selected_segments):
                        filters.append(
                            f'[amix{index}]atrim=start={segment_start:.6f}:'
                            f'end={segment_end:.6f},asetpts=PTS-STARTPTS[a{index}]')
                        audio_labels.append(f'[a{index}]')
            elif has_audio:
                split_labels = ''.join(f'[adirect{index}]'
                                       for index in range(len(selected_segments)))
                filters.append(
                    f'[0:a:0]asplit={len(selected_segments)}{split_labels}')
                for index, (segment_start, segment_end) in enumerate(selected_segments):
                    filters.append(
                        f'[adirect{index}]atrim=start={segment_start:.6f}:'
                        f'end={segment_end:.6f},asetpts=PTS-STARTPTS[a{index}]')
                    audio_labels.append(f'[a{index}]')

            video_concat_label = 'vbase' if watermark_enabled else 'vout'
            if has_audio and audio_labels:
                concat_inputs = ''.join(
                    video + audio
                    for video, audio in zip(video_labels, audio_labels, strict=True))
                filters.append(
                    f'{concat_inputs}concat=n={len(selected_segments)}:v=1:a=1'
                    f'[{video_concat_label}][aout]')
            else:
                filters.append(
                    f'{"".join(video_labels)}concat=n={len(selected_segments)}:'
                    f'v=1:a=0[{video_concat_label}]')

            if watermark_enabled:
                filters.extend(capture_card_watermark_filters('vbase'))

            cmd = base + ['-filter_complex', ';'.join(filters), '-map', '[vout]']
            if has_audio and audio_labels:
                audio_map = '[aout]'
                if speed_audio:
                    filters.append(f'[aout]{speed_audio}[aspeed]')
                    # The audio speed stage must be part of the same graph as
                    # concat; rebuild the graph argument after adding it.
                    cmd[cmd.index('-filter_complex') + 1] = ';'.join(filters)
                    audio_map = '[aspeed]'
                cmd += ['-map', audio_map]
            cmd += video_args
            if has_audio and audio_labels:
                cmd += ['-c:a', 'aac', '-b:a', audio_bitrate, '-shortest']
            cmd.append(out_path)
            return cmd

        segment_start, segment_end = selected_segments[0]
        base = [ffmpeg, '-y', '-ss', str(segment_start), '-i', clip_path,
                '-t', str(_speed_adjusted_duration(
                    segment_end - segment_start, speed_rate))]

        if not use_video_filter and not use_audio_filter:
            if not force_video_encode and not force_audio_encode:
                return base + [
                    '-map', '0:v?', '-map', '0:a?', '-c', 'copy', out_path]
            command = base + ['-map', '0:v:0', '-map', '0:a?']
            command += video_args if force_video_encode else ['-c:v', 'copy']
            command += (
                ['-c:a', 'aac', '-b:a', audio_bitrate]
                if force_audio_encode else ['-c:a', 'copy'])
            command.append(out_path)
            return command

        filters: list[str] = []
        if use_video_filter:
            video_base_label = 'vbase' if watermark_enabled else 'vout'
            chain = ','.join(video_filters) if video_filters else 'null'
            filters.append(f'[0:v]{chain}[{video_base_label}]')
            if watermark_enabled:
                filters.extend(capture_card_watermark_filters('vbase'))
        audio_output = None
        if use_audio_filter:
            states = {
                source.source_id: SourceMixState(
                    gain_percent=self._source_volumes.get(source.source_id, 100),
                    muted=self._source_mutes.get(source.source_id, False))
                for source in self._playback_sources
            }
            audio_filters, audio_output = ffmpeg_mix_filter(
                self._playback_sources, states, self._master_volume)
            filters.extend(audio_filters)
            if audio_output and speed_audio:
                filters.append(f'{audio_output}{speed_audio}[aspeed]')

        cmd = base + ['-filter_complex', ';'.join(filters)]
        cmd += ['-map', '[vout]' if use_video_filter else '0:v:0']
        cmd += ['-map', '[aspeed]' if use_audio_filter and speed_audio else
                audio_output if use_audio_filter else '0:a?']

        if use_video_filter:
            cmd += video_args
        else:
            cmd += ['-c:v', 'copy']

        if use_audio_filter or speed_audio or force_audio_encode:
            if speed_audio and not use_audio_filter:
                cmd += ['-af', speed_audio]
            cmd += ['-c:a', 'aac', '-b:a', audio_bitrate]
        else:
            cmd += ['-c:a', 'copy']

        cmd.append(out_path)
        return cmd

    def _export_worker(self, start_s: float, end_s: float, out: str, crop_rect,
                       segments: list[tuple[float, float]] | None = None,
                       effects: dict | None = None,
                       stretch_ratio: float = 1.0):
        """Run editor preparation/export and surface unexpected failures."""
        try:
            self._export_worker_impl(
                start_s, end_s, out, crop_rect, segments, effects, stretch_ratio)
        except Exception as error:
            staged = getattr(self, '_export_job', None)
            if staged is not None:
                staged.cancel()
            staged_path = getattr(self, '_export_staged_path', None)
            if staged_path:
                discard_staged_output(staged_path)
                self._export_staged_path = None
            detail = f'{type(error).__name__}: {error}'
            emit_event(
                'export', 'process_failed', state='FAILED',
                error=DiagnosticError.EXPORT_PROCESS_FAILED, detail=detail)
            if not getattr(self, '_closing', False):
                self._export_done.emit(
                    False, 'Export cancelled' if getattr(
                        self, '_export_cancel', threading.Event()).is_set()
                    else detail)

    def _export_worker_impl(self, start_s: float, end_s: float, out: str, crop_rect,
                            segments: list[tuple[float, float]] | None = None,
                            effects: dict | None = None,
                            stretch_ratio: float = 1.0):
        try:
            ffmpeg = get_ffmpeg_exe()
        except FFmpegUnavailable as e:
            emit_event(
                'export', 'init_failed', state='FAILED',
                error=DiagnosticError.EXPORT_INIT_FAILED,
                detail=str(e))
            self._export_done.emit(False, str(e))
            self.export_error.emit(
                'FFMPEG NOT FOUND',
                'ffmpeg is required for export. It normally ships with FTHR Clips; '
                'if you are running from source, install it: '
                'sudo pacman -S ffmpeg (Arch) or sudo apt install ffmpeg (Debian).',
                'error',
            )
            return

        staged = create_staged_output_path(out)
        self._export_staged_path = str(staged)
        cmd = self._build_export_cmd(
            ffmpeg=ffmpeg,
            start_s=start_s,
            duration_s=end_s - start_s,
            out_path=str(staged),
            crop_rect=crop_rect,
            video_args=maximum_quality_video_args(ffmpeg),
            segments=segments,
            effects=effects,
            stretch_ratio=stretch_ratio,
            audio_bitrate='320k',
        )

        def _state_changed(state: ExportState) -> None:
            events = {
                ExportState.EXPORTING: ('process_started', 'PROCESS_STARTED'),
                ExportState.FINALIZING: ('finalizing', 'FINALIZING'),
                ExportState.COMPLETED: ('completed', 'COMPLETED'),
                ExportState.FAILED: ('process_failed', 'FAILED'),
                ExportState.CANCELLED: ('cancelled', 'CANCELLED'),
                ExportState.TIMED_OUT: ('stalled', 'TIMED_OUT'),
            }
            event = events.get(state)
            if event:
                emit_event(
                    'export', event[0], state=event[1],
                    error=(DiagnosticError.EXPORT_STALLED
                           if state is ExportState.TIMED_OUT else None))

        job = ExportJob(
            cmd, staged, out,
            validate_output=lambda path, cancel_event: _validate_export_output(
                path, ffmpeg, cancel_event),
            commit_output=commit_staged_output,
            state_callback=_state_changed,
            popen_kwargs=_NO_WINDOW,
            inactivity_timeout=120.0,
            cancel_event=getattr(self, '_export_cancel', None),
        )
        self._export_job = job
        result = job.run(duration=(
            sum(end - start for start, end in (segments or [(start_s, end_s)]))))
        self._export_job = None
        self._export_staged_path = None
        output_exists = os.path.isfile(out)
        emit_event(
            'export', 'terminal', state=result.state.value,
            elapsed_ms=round(result.elapsed_seconds * 1000),
            exit_code=result.returncode,
            stderr_tail=result.stderr_tail,
            output_exists=output_exists,
            output_size_bytes=(os.path.getsize(out) if output_exists else 0))
        if result.state is ExportState.COMPLETED:
            self._export_done.emit(True, out)
        elif not getattr(self, '_cancelled', False):
            detail = result.detail
            if result.stderr_tail:
                detail = next((line for line in reversed(
                    result.stderr_tail.splitlines()) if line.strip()), detail)
            self._export_done.emit(False, detail or 'Export failed')

    def _on_export_done(self, success: bool, msg: str):
        if getattr(self, '_closing', False):
            return
        cancelled = (not success and str(msg).strip().lower().startswith(
            'export cancelled'))
        if cancelled:
            self.export_btn.setText('EXPORT CANCELLED')
            self.crop_info_lbl.setStyleSheet(
                f'color: {Colors.TEXT_MUTED}; font-size: 8px; '
                f'font-family: {Fonts.DISPLAY}; background: transparent;')
            self.crop_info_lbl.setText('Export cancelled')
            QTimer.singleShot(3000, self._reset_export_button)
            return
        if success:
            self.export_btn.setText('✓  EXPORTED')
        else:
            self.export_btn.setText('EXPORT FAILED')
            self.export_error.emit(
                'EXPORT FAILED',
                'FFmpeg returned an error. The output file may be incomplete.',
                'warning',
            )
            self.crop_info_lbl.setStyleSheet(
                f'color: {Colors.ERROR}; font-size: 8px; '
                f'font-family: {Fonts.DISPLAY}; background: transparent;')
            short = msg if len(msg) <= 40 else msg[:38] + '…'
            self.crop_info_lbl.setText(short)
            QTimer.singleShot(6000, self._clear_export_error)

        def _reset():
            self.export_btn.setText('EXPORT CLIP')
            self.export_btn.setEnabled(True)
        QTimer.singleShot(3000, _reset)

    def _reset_export_button(self):
        if not getattr(self, '_closing', False):
            self.export_btn.setText('EXPORT CLIP')
            self.export_btn.setEnabled(True)

    def _clear_export_error(self):
        self.crop_info_lbl.setStyleSheet(
            f'color: {Colors.ACCENT}; font-size: 8px; '
            f'font-family: {Fonts.DISPLAY}; background: transparent;')
        self._update_crop_ui()

    # Share

    def _show_share_overlay(self):
        dlg = ShareModeDialog(self.sm, parent=self)
        dlg.mode_selected.connect(self._open_share_window)
        dlg.preset_selected.connect(self._open_share_preset)
        dlg.show()

    def _open_share_preset(self, preset: ExportPreset):
        self._open_share_window(False, preset)

    def _open_share_window(
            self, discord_mode: bool, export_preset: ExportPreset | None = None):
        segments = self._kept_segments_seconds()
        if not segments:
            self.export_error.emit(
                'SHARE FAILED', 'The edit has no kept segments.', 'warning')
            return
        start_s = segments[0][0]
        end_s = segments[-1][1]
        settings_info = {
            'resolution': self.sm.get('resolution', 'source') if self.sm else 'source',
            'fps':        int(self._fps),
            'bitrate':    self.sm.get('bitrate_level', 'medium') if self.sm else 'medium',
            'watermark_enabled': bool(
                self.sm.get('watermark_enabled', False)) if self.sm else False,
        }
        sw = ShareWindow(
            clip_path=self.clip_path,
            start_s=start_s,
            end_s=end_s,
            crop_rect=self._crop_rect,
            settings_info=settings_info,
            discord_mode=discord_mode,
            source_volumes=self._source_volumes,
            source_mutes=self._source_mutes,
            master_volume=self._master_volume,
            playback_sources=self._playback_sources,
            multitrack_audio=self._multitrack_audio,
            audio_tracks=self._audio_tracks,
            segments=segments,
            effects=dict(self._effects),
            stretch_ratio=self._stretch_ratio,
            speed_rate=self._speed_rate,
            preserve_pitch=self._preserve_pitch,
            export_preset=export_preset,
            parent=self,
        )
        sw.export_error.connect(self.export_error)
        sw.show()

    # Delete

    def _delete_clip(self):
        imported_roots = (
            self.sm.get('imported_clip_folders', []) if self.sm else [])
        ownership = classify_media_path(
            self.clip_path, clips_directory_from(self.sm), imported_roots)
        if self._linked_import or ownership is not MediaOwnership.FTHR_OWNED:
            FthrMessageDialog.information(
                self,
                'Linked Original Protected',
                'This clip is linked from another folder. FTHR will not '
                'delete the original with the generic Delete action.',
            )
            return
        reply = FthrMessageDialog.question(
            self, 'Delete Clip',
            f'Delete {os.path.basename(self.clip_path)}?\nThis cannot be undone.',
        )
        if not reply:
            return

        # Release the MediaFoundation handle before deleting.
        # On Windows, player.stop() alone doesn't free the file handle —
        # setSource(QUrl()) is required to detach the media pipeline entirely.
        # Without this, os.remove() fails with WinError 32 immediately.
        self._teardown_player()
        try:
            from PySide6.QtCore import QUrl as _QUrl
            self.player.setSource(_QUrl())
        except Exception:
            pass

        # Compute cache paths before deleting (needs mtime while file still exists)
        cache_path = None
        dur_path = None
        try:
            from ui.clip_grid import _get_cached_thumb_path, _get_cached_duration_path
            cache_path = _get_cached_thumb_path(self.clip_path)
            dur_path = _get_cached_duration_path(cache_path)
        except Exception:
            pass

        import time as _time
        for attempt in range(3):
            try:
                os.remove(self.clip_path)
                for p in (cache_path, dur_path):
                    if p:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
                self._discard_editor_draft = True
                self._draft_save_timer.stop()
                clear_draft = getattr(self._mm, 'clear_editor_draft', None)
                if callable(clear_draft):
                    try:
                        clear_draft(self.clip_path)
                    except (OSError, TypeError, ValueError) as error:
                        print(f'[ClipViewer] editor draft cleanup failed: {error}')
                self.accept()
                return
            except OSError as e:
                if sys.platform == 'win32' and getattr(e, 'winerror', 0) == 32:
                    if attempt < 2:
                        _time.sleep(0.2)
                        continue
                    FthrMessageDialog.warning(
                        self, 'Delete Failed',
                        f'"{os.path.basename(self.clip_path)}" is still in use.\n\n'
                        'Wait for any active clip save or upload to finish, then try again.',
                    )
                    return
                FthrMessageDialog.warning(self, 'Delete Failed', str(e))
                return

    # Close / cleanup

    def _close(self):
        self._draft_save_timer.stop()
        self._flush_editor_draft()
        self._teardown_player()
        self.reject()

    def reject(self):
        """Ensure every dialog rejection releases multimedia resources."""

        self._teardown_player()
        super().reject()

    def accept(self):
        """Ensure accept/delete paths use the same idempotent teardown."""

        self._teardown_player()
        super().accept()

    def closeEvent(self, event):
        self._draft_save_timer.stop()
        self._flush_editor_draft()
        self._teardown_player()
        super().closeEvent(event)

    def _teardown_player(self):
        if self._teardown_complete:
            return
        self._teardown_complete = True
        emit_event('playback', 'close_requested', state='CLOSING')
        self._set_player_state(PlayerLifecycleState.CLOSING)
        self._closing = True
        self._prepare_cancel.set()
        self._play_when_ready = False
        self._export_cancel.set()
        export_job = getattr(self, '_export_job', None)
        if export_job is not None:
            export_job.cancel()
        export_thread = getattr(self, '_export_thread', None)
        if (export_thread is not None
                and export_thread is not threading.current_thread()):
            export_thread.join(timeout=2.5)
        self._export_job = None
        try:
            self._timeline_prepare_timer.stop()
        except Exception:
            pass
        try:
            self._audio_prepare_deadline.stop()
        except Exception:
            pass
        try:
            self._playback_stall_timer.stop()
        except Exception:
            # A partially constructed viewer may close before the diagnostic
            # timer exists; the remaining teardown must still run.
            pass
        try:
            self.trim_slider.cancel_thumbnail_loading()
        except Exception:
            pass
        # Stop timers first so no late tick can re-issue a seek into a player
        # we are about to release. Each call is wrapped because Qt can raise
        # if a timer was already deleted by a parent during a fast close.
        try:
            self._seek_timer.stop()
        except Exception:
            pass
        try:
            self._mix_refresh_timer.stop()
        except Exception:
            # Parent-driven Qt destruction can delete this timer before a
            # duplicate close event; there is no remaining callback to cancel.
            pass
        try:
            self._preview_update_timer.stop()
        except Exception:
            pass
        try:
            self._draft_save_timer.stop()
        except Exception:
            pass
        self._preview_update_pending = False
        self._pending_seek_ms = None
        try:
            self._ph_timer.stop()
        except Exception:
            pass
        try:
            self.player.stop()
        except Exception:
            pass
        if self._audio_mixer is not None:
            mixer = self._audio_mixer
            self._audio_mixer = None
            try:
                mixer.stop()
                mixer.deleteLater()
            except (RuntimeError, TypeError):
                pass
        # Detach native multimedia resources explicitly. Parent ownership is a
        # final safety net, but switching clips should not wait for Python/Qt
        # object collection to release decoders, file handles, and sinks.
        try:
            self.player.setVideoOutput(None)
        except (RuntimeError, TypeError):
            pass
        try:
            self.player.setAudioOutput(None)
        except (RuntimeError, TypeError):
            pass
        try:
            self.player.setSource(QUrl())
        except (RuntimeError, TypeError):
            pass
        try:
            self.video_widget.shutdown()
        except (RuntimeError, TypeError):
            pass
        try:
            native_sink = self._native_video_widget.videoSink()
            native_sink.videoFrameChanged.disconnect(self._on_native_video_frame)
        except (AttributeError, RuntimeError, TypeError):
            # Qt may destroy the native sink before parent teardown runs.
            pass
        try:
            self._native_video_widget.hide()
        except (AttributeError, RuntimeError):
            # A parent-deleted renderer is already no longer visible.
            pass
        try:
            self.audio_output.setMuted(True)
        except (RuntimeError, TypeError):
            pass
        if self._diagnostic_player_counted:
            self._diagnostic_player_counted = False
            playback_instance_destroyed()
        self._set_player_state(PlayerLifecycleState.CLOSED)

    # Window drag / native resize (matches MainWindow)

    def _bar_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (event.globalPosition().toPoint()
                              - self.frameGeometry().topLeft())

    def _bar_mouse_move(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def _bar_mouse_release(self, event):
        self._drag_pos = None

    def _bar_double_click(self, event):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def showEvent(self, event):
        super().showEvent(event)
        if self._fade_in_anim is not None:
            self._fade_in_anim.start()
        if not self._native_style_applied:
            self._native_style_applied = True
            QTimer.singleShot(0, self._apply_native_style)

    def _apply_native_style(self):
        """Apply WS_THICKFRAME so native resize / Aero-snap work on a frameless dialog."""
        try:
            import ctypes
            hwnd = int(self.winId())
            GWL_STYLE      = -16
            WS_THICKFRAME  = 0x00040000
            WS_MAXIMIZEBOX = 0x00010000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_STYLE, style | WS_THICKFRAME | WS_MAXIMIZEBOX)
            SWP_FRAMECHANGED = 0x0020
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            ctypes.windll.user32.SetWindowPos(
                hwnd, None, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_FRAMECHANGED)
        except Exception:
            pass

    def nativeEvent(self, eventType, message):
        if eventType == b'windows_generic_MSG':
            try:
                import ctypes, ctypes.wintypes
                ptr = int(message)
                if ptr:
                    msg_type = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.c_uint32))[2]
                    if msg_type == 0x0084:  # WM_NCHITTEST
                        msg = ctypes.wintypes.MSG.from_address(ptr)
                        lp  = msg.lParam
                        cx  = ctypes.c_short(lp & 0xFFFF).value
                        cy  = ctypes.c_short((lp >> 16) & 0xFFFF).value
                        g   = self.frameGeometry()
                        bw  = 6  # resize border width

                        left   = cx <  g.left()   + bw
                        right  = cx >= g.right()  - bw
                        top    = cy <  g.top()    + bw
                        bottom = cy >= g.bottom() - bw

                        if not self.isMaximized():
                            if top    and left:  return True, 13  # HTTOPLEFT
                            if top    and right: return True, 14  # HTTOPRIGHT
                            if bottom and left:  return True, 16  # HTBOTTOMLEFT
                            if bottom and right: return True, 17  # HTBOTTOMRIGHT
                            if top:              return True, 12  # HTTOP
                            if bottom:           return True, 15  # HTBOTTOM
                            if left:             return True, 10  # HTLEFT
                            if right:            return True, 11  # HTRIGHT

                        if cy < g.top() + self._editor_header_h:
                            from PySide6.QtWidgets import QPushButton
                            local = self.mapFromGlobal(QPoint(cx, cy))
                            w = self.childAt(local)
                            if w is None or not isinstance(w, QPushButton):
                                return True, 2  # HTCAPTION
            except Exception:
                pass
        return False, 0

    # Helpers

    def _fmt(self, ms) -> str:
        s = int(ms / 1000)
        return f'{s // 60}:{s % 60:02d}'
