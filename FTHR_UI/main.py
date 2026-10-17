"""Application entry point, main window, capture coordination, and save flow."""

from ui.icons import IconButton
from ui.motion import reduce_motion, set_reduce_motion
from ui.shell_transitions import (
    ShellPageStack, SnapshotFlight, SnapshotSlide, sidebar_rect, compact_snapshot,
)

import sys
import json
import subprocess
import time
import math
import os
import shutil
import tempfile
import threading
from dataclasses import asdict
from pathlib import Path
from datetime import datetime
_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}


def _playback_device_labels() -> tuple[str, str, str]:
    """(default entry, fallback name, hint) for the playback-device picker.

    On Linux the engine captures the monitor of a PulseAudio sink and uses the
    default output's monitor when the chosen sink is missing at startup.
    """
    if sys.platform == 'win32':
        return ('Windows default', 'Windows default',
                'If this device disconnects, capture switches to the Windows default.')
    return ('System default', 'the default output',
            'If this device is missing when capture starts, the default output is '
            'captured instead.')


_BACKGROUND_NO_WINDOW = {
    'creationflags': (
        subprocess.CREATE_NO_WINDOW
        | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0)
    ),
} if sys.platform == 'win32' else {}

# Qt 6.11 defaults to its FFmpeg multimedia plugin on Windows. Profiling this
# editor found that plugin retained one native handle on every media-source
# cycle and grew without bound across repeated clip switches. FTHR's supported
# Windows playback path is Media Foundation; it delivers every source frame in
# the same smoke clip and reaches a stable handle/memory plateau. Keep an
# explicit environment override available for backend diagnostics.
# Linux has no Media Foundation backend; Qt keeps its FFmpeg plugin there.
if sys.platform == 'win32':
    os.environ.setdefault('QT_MEDIA_BACKEND', 'windows')


# File logging - all output also goes to ~/.fthr/logs/fthr.log
# Important for packaged apps (pythonw) which have no console

class _LogTee:
    """Mirror a stream (may be None in frozen GUI builds) into a log file."""
    _MAX_BYTES = 2 * 1024 * 1024  # rotate at 2 MB, keep one .old

    def __init__(self, stream, log_path):
        self._stream = stream
        self._log_path = log_path
        try:
            if log_path.exists() and log_path.stat().st_size > self._MAX_BYTES:
                log_path.replace(log_path.with_suffix('.log.old'))
            self._log = open(log_path, 'a', encoding='utf-8', errors='replace')
        except Exception:
            self._log = None

    def write(self, text):
        if self._stream is not None:
            try:
                self._stream.write(text)
            except Exception:
                # console may have disappeared
                pass
        if self._log is not None:
            try:
                self._log.write(text)
                self._log.flush()
            except Exception:
                # can't recursively try to log this
                pass

    def flush(self):
        for s in (self._stream, self._log):
            if s is not None:
                try:
                    s.flush()
                except Exception:
                    # may be closed at shutdown
                    pass


def _setup_file_logging():
    try:
        log_dir = Path.home() / '.fthr' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / 'fthr.log'
        sys.stdout = _LogTee(sys.stdout, log_file)
        sys.stderr = _LogTee(sys.stderr, log_file)
        from version import __version__ as _ver
        print(f"\n===== FTHR Clips {_ver} started "
              f"{datetime.now():%Y-%m-%d %H:%M:%S} ({sys.platform}) =====")
        # Structured logging uses the same file; print() still works
        from core import diagnostics
        diagnostics.configure(log_file=log_file)
        if sys.platform != 'win32':
            # Native crashes (Qt, drivers) leave a Python stack here (#79).
            from core import crash_logging
            crash_logging.enable_fault_handler(log_dir / 'fthr-crash.log')
    except Exception as e:
        # Log setup failed - report on stderr before redirection
        print(f'[Startup] File logging unavailable: {type(e).__name__}: {e}',
              file=sys.__stderr__ or sys.stderr)


_setup_file_logging()

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QScrollArea, QFrame, QComboBox, QGridLayout,
    QSizePolicy,
    QCheckBox, QSlider,
    QToolButton, QButtonGroup, QFileDialog, QLineEdit, QMenu,
    QSystemTrayIcon, QSpinBox, QAbstractSpinBox,
    QLayout, QDialog, QStyle, QStyleOptionSlider, QMessageBox,
)
from PySide6.QtCore import (QUrl, 
    QTimer, Signal, Qt, QPoint, QSize, QRect, QEvent,
    QPropertyAnimation, QEasingCurve, Slot,
)
from PySide6.QtGui import QDesktopServices, QImage, QPixmap, QFontDatabase, QFont, QCursor, QPainter, QPen, QColor, QIcon, QBrush, QPalette, QAction

if sys.platform != 'win32':
    # Route Qt warnings and fatals into the log next to the fault handler.
    from core import crash_logging
    crash_logging.install_qt_message_handler()
    from core import linux_runtime, linux_tools
    from core.compositor import detect_compositor
    from core.x11_monitor import (
        X11MonitorError,
        is_native_x11_session,
        resolve_x11_capture_target,
    )
    from ui.frameless_window_linux import install_resize_filter

from version import __version__ as APP_VERSION, APP_NAME
from core.capture_bridge import CaptureBridge
from core.alpha_capabilities import (
    encoder_preset_supported,
    filter_alpha_preset,
    focus_pause_supported,
)
from core.capture_settings import (
    FPS_VALUES,
    NORMAL_CLIP_VALUES,
    CaptureConfig,
    CaptureConfigTracker,
    compute_buffer_seconds,
    AUDIO_CAPTURE_MODE_COMBINED,
    AUDIO_CAPTURE_MODE_SEPARATED,
    normalize_audio_capture_mode,
    validate_fps,
    validate_normal_clip_length,
    custom_clip_lengths,
)
from core.encoder_capabilities import (
    EncoderCapability,
    available_codecs,
    load_cached_encoder_capabilities,
    probe_encoder_capabilities,
    save_encoder_capabilities_cache,
)
from core.capture_health import (
    CaptureHealthFlag,
    CaptureHealthMonitor,
    CaptureHealthState,
    evaluate_save_admission,
)
from core.error_recovery import RecoveryTracker
from core.error_codes import APP_FAILURE_CODE_BY_TITLE, format_error_title
from core.diagnostics import get_logger
from core.field_diagnostics import (
    DiagnosticError,
    EngineLogCapture,
    build_adapter_chain,
    emit_event,
    end_diagnostic_session,
    get_diagnostic_session,
    qt_display_snapshot,
    start_diagnostic_session,
)
from core.engine_startup_diagnostics import (
    EngineLaunchContext,
    extract_startup_failure,
    extract_startup_warnings,
    format_engine_launch_failure,
)
from core.clip_files import (
    is_completed_video_path,
    start_stale_partial_cleanup,
)
from core.audio_manifest import (
    manifest_path_for, read_manifest_for_media, rebind_manifest_after_media_replace,
)
from core.clip_readiness import ClipReadinessState, get_clip_readiness_registry
from core.save_state import (SaveStateMachine, EngineEvent, OutcomeKind)
from core.hotkey_manager import (
    CONTROLLER_BUTTON_ORDER,
    HotkeyManager,
    format_controller_combo,
    format_keyboard_combo,
    normalize_controller_combo,
    normalize_keyboard_combo,
)
from core.game_detector import (
    ForegroundGameDetector,
    GameWindow,
    is_capture_window_valid,
    matching_custom_game_rule,
    normalise_custom_game_rules,
)
from core.focus_monitor import FocusMonitor
from core.presets_manager import PresetsManager, PRESET_KEYS
from core.settings_manager import (
    SettingsManager, StoragePathError, clips_directory_from,
    default_clips_directory, ensure_storage_directory,
    prepare_clips_storage, recording_directory_from,
)
from core.theme_manager import ThemeManager
from core.windows_monitor import (
    MonitorSelectionError,
    MonitorSelectionResult,
    MonitorSelectionState,
    enumerate_windows_monitors,
    is_monitor_capture_identity,
    is_monitor_selection_sentinel,
    resolve_monitor_selection,
    validate_engine_monitor_argument,
)
from core.screenshot_target import (
    select_qt_screen,
)
from core.screenshot_save import (
    ScreenshotPngSaveWorker,
    ScreenshotSaveError,
    publish_staged_png,
    reserve_screenshot_paths,
)
from core.library_ownership import add_import_root, remove_import_root
from core.mic_recorder import MicRecorder, write_wav
from core.gary_mode import (
    DEFAULT_MAX_LEVEL, DEFAULT_MIN_LEVEL, MIN_THRESHOLD_GAP,
    clamp_thresholds, intensity_for_level, step_intensity,
)
from core.camera_overlay import (
    DEFAULT_IMAGE_OVERLAY_RECT, DEFAULT_OVERLAY_RECT,
    clamp_overlay_rect, image_overlay_layers, legacy_overlay_rect,
    new_image_overlay_layer,
)
from core.third_party_keyboard import (
    DEFAULT_KEYBOARD_COLOR,
    DEFAULT_KEYBOARD_INTENSITY,
    DEFAULT_KEYBOARD_OVERLAY_RECT,
    KEYBOARD_COMPOSITE_FPS,
    ThirdPartyKeyboardCapture,
    chroma_key_rgba,
    enumerate_keyboard_windows,
    third_party_keyboard_settings,
)
from core.windows_microphone_devices import (
    MicrophoneDiscoveryCancelled,
    MicrophoneDiscoveryJob,
    MicrophoneDiscoveryResult,
    discovery_diagnostic_code,
    discovery_result_event_fields,
    migrate_legacy_microphone_name,
)
from core.combined_audio_policy import (
    build_combined_audio_filter,
    descriptors_from_ffprobe_streams,
    select_combined_audio_streams,
)
from core.av_sync_diagnostics import (
    TimelineProbeError,
    probe_clip_av_timeline,
)
from core.ffmpeg_tools import (
    get_ffmpeg_exe, get_ffprobe_exe, postprocess_video_args,
    software_video_args, FFmpegUnavailable)
from core.export_profiles import probe_media
from core.media_metadata import (
    probe_video_cfr_evidence,
    probe_video_metadata,
)
from ui.capture_card_client import CaptureCardClient
from ui.error_bar import ErrorBar
from ui.frameless_window_windows import (
    apply_native_resize_style,
    decode_nchittest_point,
    native_hit_test,
    start_system_move,
)
from core.window_geometry import (
    is_rect_visible,
    popup_rect_for_anchor,
    restore_window_geometry,
    save_window_geometry,
)
from core.windows_dpi import dpi_awareness_snapshot
from ui.smooth_scroll_area import SmoothScrollArea
from ui.clip_grid import ClipGrid, _show_in_file_manager
from ui.gary_overlay import GaryOverlay
from ui.camera_overlay_editor import (
    CameraOverlayEditor, OverlayPlacementEditor, UnifiedOverlayPreview,
)
from ui.keyboard_overlay_preview import KeyboardSourcePreview
from ui.game_crop_dialog import GameCropDialog
from ui.capture_settings_widget import (
    _enumerate_capturable_windows,
)
from ui.style import set_theme_style, refresh_theme_styles
from ui.style import (
    Colors, Fonts, Sizes,
    label_display, label_uppercase, label_body,
    status_active_qss, status_idle_qss, status_warning_qss,
    combo_qss, button_primary_qss, button_outline_qss,
    button_secondary_qss, slider_qss, checkbox_qss, scrollbar_qss, tooltip_qss,
    ThemedDropdownButton, WheelSafeComboBox, paint_dropdown_arrow,
    retarget_widget_font_styles,
)
from ui.app_style import apply_app_style, configure_qt_style
from ui.dialogs import FthrDialog, FthrInputDialog, FthrMessageDialog

try:
    import numpy as _np
    import sounddevice as _sd
    _SD_AVAILABLE = True
except Exception:
    _SD_AVAILABLE = False


# Global constants

# The bottom bar is deliberately a failure channel, not a general-purpose
# status feed. Routine advisories and successful-but-degraded processing are
# still handled by the status label, capture card, dialogs, or logs.
_ERROR_BAR_FAILURE_TITLES = frozenset(APP_FAILURE_CODE_BY_TITLE)

_RESOLUTION_DIMS = {
    '480p': (854, 480), '720p': (1280, 720),
    '1080p': (1920, 1080), '1440p': (2560, 1440), 'source': (0, 0),
}
_DIMS_TO_LABEL = {v: k.upper() for k, v in _RESOLUTION_DIMS.items()}

BITRATE_PRESETS = {
    '480p':   {'low': 2500,  'medium': 5000,  'high': 10000},
    '720p':   {'low': 5000,  'medium': 12000, 'high': 20000},
    '1080p':  {'low': 10000, 'medium': 25000, 'high': 50000},
    '1440p':  {'low': 15000, 'medium': 35000, 'high': 60000},
    'source': {'low': 10000, 'medium': 25000, 'high': 50000},
}

def _resolution_to_dims(name: str) -> tuple[int, int]:
    return _RESOLUTION_DIMS.get(name, (0, 0))


def _load_logo_asset(path: Path) -> QPixmap:
    if QApplication.instance() is None:
        QApplication([])
    return QPixmap(str(path))


_icon_registry: list[tuple[object, str, int]] = []


def _load_icon(name: str, size: int = 20) -> QIcon:
    from ui.icons import load_icon
    return load_icon(name, size)


def _register_icon_widget(widget, name: str, size: int):
    """Register a widget so its icon refreshes on Apply Theme."""
    _icon_registry.append((widget, name, size))


def _refresh_all_icons():
    """Reload all registered icon widgets from current theme state."""
    alive = []
    for widget, name, size in _icon_registry:
        try:
            _ = widget.objectName()
            icon = _load_icon(name, size)
            widget.setIcon(icon)
            widget.setIconSize(QSize(size, size))
            alive.append((widget, name, size))
        except (RuntimeError, AttributeError):
            # Qt may delete registered widgets before a theme refresh.
            pass
    _icon_registry.clear()
    _icon_registry.extend(alive)


def _dims_to_label(w: int, h: int) -> str:
    return _DIMS_TO_LABEL.get((w, h), f'{w}×{h}' if w else 'SOURCE')


def select_post_route(*, audio_on: bool, multiband_enabled: bool,
                      mic_running: bool, watermark: bool, manual_crop: bool,
                      camera: bool, audio_capture_mode: str = 'combined',
                      native_audio: bool = False,
                      keyboard: bool = False) -> tuple[str, bool]:
    """Choose exactly one post-processing route and report asynchronous work.

    Return (route, has_async_mux); route is mic or finalize. The mic route also
    handles native Windows combined audio. Each route owns clip_ready until
    finalization finishes, preventing upload of a partially written file.
    multiband_enabled is retained for caller compatibility.
    """
    del multiband_enabled
    mic_active = audio_on and mic_running

    mode = normalize_audio_capture_mode(audio_capture_mode)
    # The Windows engine captures the system and microphone sources natively
    # as separate packets. Combined mode needs one asynchronous remux even
    # when the microphone endpoint is unavailable; separated mode can publish
    # the native result directly.
    if audio_on and native_audio and mode == AUDIO_CAPTURE_MODE_COMBINED:
        return 'mic', True

    if mic_active:
        return 'mic', True
    # Every completed clip passes through the finalizer. Native capture keeps
    # wall-clock timestamps so playback cannot speed up; the finalizer repairs
    # the sample table to CFR before the file is published. Visual options are
    # still applied by that same worker.
    return 'finalize', True


def _sanitize_foldername(name: str) -> str:
    """Strip Windows-invalid chars from a window title to make a safe folder name."""
    invalid = r'\/:*?"<>|'
    cleaned = ''.join(c for c in name
                      if c not in invalid and ord(c) >= 32).strip('. ')
    cleaned = cleaned[:32].rstrip('. ')
    # Windows reserved device names can't be folders (CON, NUL, COM1, ...)
    if cleaned.upper() in {'CON', 'PRN', 'AUX', 'NUL',
                           *(f'COM{i}' for i in range(1, 10)),
                           *(f'LPT{i}' for i in range(1, 10))}:
        cleaned = f'{cleaned}_game'
    return cleaned or 'Unknown'

# Brand accent kept as a local alias for inline f-strings sprinkled below.
# Single source of truth lives in style.Colors.
FTHR_TEAL     = Colors.ACCENT
FTHR_TEAL_DIM = Colors.ACCENT_DIM

# Animation timing — kept module-level so popups, the settings page, and the
# clip viewer all converge on the same fade duration. 180 ms is brisk enough
# that the user perceives the panel as "snappy" but slow enough that opening
# / closing doesn't read as a jump-cut.
PANEL_FADE_MS = 180




# Canonical QSS / label fragments come from style.py; aliased so call-sites stay short.
_saved_fonts = ThemeManager().get_fonts()
Fonts.configure(_saved_fonts.get('display'), _saved_fonts.get('body'))
_COMBO_STYLE = combo_qss()
_LABEL_STYLE = label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)

_NVENC_PRESET_OPTIONS = (
    (1, 'P1 — Fastest'),
    (2, 'P2'),
    (3, 'P3'),
    (4, 'P4 — Balanced'),
    (5, 'P5'),
    (6, 'P6'),
    (7, 'P7 — Best Quality'),
)


# Custom QComboBox — shows icons/dropdown.png as the arrow indicator and
# rotates it 180° while the popup is open, back to 0° when it closes.

class _DropdownCombo(WheelSafeComboBox):
    _custom_arrow_managed = True

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False
        self.refresh_theme_palette()

    def refresh_theme_palette(self):
        # Force the popup list to use our dark colors via palette, because on
        # Qt6/Linux the stylesheet alone doesn't reliably override the system
        # palette for the floating item view (white-on-white issue).
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Base,            QColor(Colors.SURFACE_2))
        pal.setColor(QPalette.ColorRole.Text,            QColor(Colors.TEXT))
        pal.setColor(QPalette.ColorRole.Highlight,       QColor(Colors.SURFACE_3))
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(Colors.ACCENT))
        pal.setColor(QPalette.ColorRole.Window,          QColor(Colors.SURFACE_2))
        pal.setColor(QPalette.ColorRole.WindowText,      QColor(Colors.TEXT))
        self.setPalette(pal)
        self.view().setPalette(pal)

    def showPopup(self):
        self._popup_open = True
        self.update()
        super().showPopup()

    def hidePopup(self):
        super().hidePopup()
        self._popup_open = False
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        # Resolve through the theme loader so imported arrows and tint changes
        # are reflected immediately after Apply Theme.
        p = QPainter(self)
        paint_dropdown_arrow(p, self.rect(), self._popup_open)
        p.end()


# Compact numeric control — editable value with the original FTHR stepper.

class _NumberSpinBox(QSpinBox):
    """Keyboard-editable value with square minus/plus controls at the right."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('fthrNumberSpin')
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setFixedHeight(34)
        self.lineEdit().setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._minus = IconButton('decrease.png', self, label='Decrease')
        self._minus.setObjectName('fthrStepDown')
        self._minus.setToolTip('Decrease')
        self._minus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._minus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._minus.clicked.connect(self.stepDown)

        self._plus = IconButton('increase.png', self, label='Increase')
        self._plus.setObjectName('fthrStepUp')
        self._plus.setToolTip('Increase')
        self._plus.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._plus.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._plus.clicked.connect(self.stepUp)

        self.valueChanged.connect(self._sync_step_buttons)
        self._sync_step_buttons()
        self._apply_style()

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        button_w = 28
        button_h = max(0, self.height() - 2)
        self._minus.setGeometry(
            self.width() - button_w * 2 - 1, 1, button_w, button_h)
        self._plus.setGeometry(
            self.width() - button_w - 1, 1, button_w, button_h)

    def _sync_step_buttons(self, *_args):
        enabled = self.isEnabled()
        self._minus.setEnabled(enabled and self.value() > self.minimum())
        self._plus.setEnabled(enabled and self.value() < self.maximum())

    def _apply_style(self):
        set_theme_style(self, lambda: (f'''
            QSpinBox#fthrNumberSpin {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_BODY}px;
                font-weight: 600;
                letter-spacing: 0.8px;
                padding: 6px 64px 6px 10px;
                selection-background-color: {Colors.ACCENT};
                selection-color: {Colors.BG};
            }}
            QSpinBox#fthrNumberSpin:hover,
            QSpinBox#fthrNumberSpin:focus {{
                border-color: {Colors.ACCENT};
            }}
            QSpinBox#fthrNumberSpin:disabled {{
                color: {Colors.TEXT_MUTED};
                background-color: {Colors.SURFACE_1};
                border-color: {Colors.BORDER};
            }}
            QPushButton#fthrStepDown,
            QPushButton#fthrStepUp {{
                background-color: {Colors.SURFACE_1};
                border: none;
                border-left: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_SM}px;
                color: {Colors.ACCENT};
                font-family: {Fonts.DISPLAY};
                font-size: 14px;
                font-weight: 600;
                padding: 0px;
            }}
            QPushButton#fthrStepDown:hover,
            QPushButton#fthrStepUp:hover {{
                background-color: {Colors.ACCENT};
                color: {Colors.BG};
            }}
            QPushButton#fthrStepDown:pressed,
            QPushButton#fthrStepUp:pressed {{
                background-color: {Colors.TEXT};
                color: {Colors.BG};
            }}
            QPushButton#fthrStepDown:disabled,
            QPushButton#fthrStepUp:disabled {{
                color: {Colors.TEXT_MUTED};
                background-color: {Colors.SURFACE_1};
            }}
        '''))


# Mic level meter — paints a horizontal RMS bar driven by a sounddevice stream

class _MicLevelMeter(QWidget):
    """Live mic-loudness bar. Updates at ~30Hz from an InputStream callback."""

    _level_changed = Signal(float)
    gary_thresholds_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(36)
        self.setMinimumWidth(120)
        self._level    = 0.0
        self._peak     = 0.0
        self._stream   = None
        self._gain     = 1.0
        self._shared_recorder = None
        self._gary_min = DEFAULT_MIN_LEVEL
        self._gary_max = DEFAULT_MAX_LEVEL
        self._gary_enabled = False
        self._dragging_gary_handle: str | None = None
        self._drag_start_thresholds = (self._gary_min, self._gary_max)
        self.setMouseTracking(True)
        self._level_changed.connect(self._on_level)
        self._update_tooltip()

        self._peak_decay = QTimer(self)
        self._peak_decay.setInterval(60)
        self._peak_decay.timeout.connect(self._decay_peak)

    def set_gain(self, g: float):
        self._gain = max(0.0, g)

    def set_gary_thresholds(self, min_level: int, max_level: int):
        self._gary_min, self._gary_max = clamp_thresholds(min_level, max_level)
        self.update()

    def set_gary_enabled(self, enabled: bool):
        self._gary_enabled = bool(enabled)
        if not self._gary_enabled:
            self._dragging_gary_handle = None
            self.unsetCursor()
        self._update_tooltip()
        self.update()

    def _update_tooltip(self):
        suffix = '' if self._gary_enabled else ' Gary is currently off.'
        self.setToolTip(
            'Drag the amber MIN and white MAX handles to set Gary Mode.'
            + suffix)

    def _threshold_x(self, value: int) -> int:
        return 1 + int((self.width() - 2) * value / 100.0)

    def _set_threshold_from_x(self, x: float):
        if self.width() <= 2 or self._dragging_gary_handle is None:
            return
        value = round(max(0.0, min(float(x) - 1, self.width() - 2))
                      * 100.0 / (self.width() - 2))
        if self._dragging_gary_handle == 'min':
            self._gary_min = max(
                1, min(value, self._gary_max - MIN_THRESHOLD_GAP))
        else:
            self._gary_max = min(
                100, max(value, self._gary_min + MIN_THRESHOLD_GAP))
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            x = event.position().x()
            min_distance = abs(x - self._threshold_x(self._gary_min))
            max_distance = abs(x - self._threshold_x(self._gary_max))
            self._dragging_gary_handle = (
                'min' if min_distance <= max_distance else 'max')
            self._drag_start_thresholds = (self._gary_min, self._gary_max)
            self.setCursor(QCursor(Qt.CursorShape.SizeHorCursor))
            self._set_threshold_from_x(x)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging_gary_handle is not None:
            self._set_threshold_from_x(event.position().x())
            event.accept()
            return
        x = event.position().x()
        near_handle = min(
            abs(x - self._threshold_x(self._gary_min)),
            abs(x - self._threshold_x(self._gary_max)),
        ) <= 12
        self.setCursor(QCursor(
            Qt.CursorShape.SizeHorCursor if near_handle
            else Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self._dragging_gary_handle is not None):
            self._set_threshold_from_x(event.position().x())
            self._dragging_gary_handle = None
            self.unsetCursor()
            thresholds = (self._gary_min, self._gary_max)
            if thresholds != self._drag_start_thresholds:
                self.gary_thresholds_changed.emit(*thresholds)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def start(self, device_index):
        self.stop()
        if not _SD_AVAILABLE:
            return False

        # Prefer sharing MicRecorder's stream via RMS callback — avoids opening
        # a second capture device which can fail on exclusive-mode backends.
        recorder = MicRecorder()
        if recorder.is_running() and recorder._device_index == device_index:
            recorder.add_rms_listener(self._on_rms_from_recorder)
            self._shared_recorder = recorder
            self._peak_decay.start()
            return True

        # MicRecorder not running on this device — open a dedicated preview stream.
        self._shared_recorder = None

        def _cb(indata, frames, time_info, status):
            try:
                arr = indata if indata.ndim == 1 else indata[:, 0]
                rms = float(_np.sqrt(_np.mean(_np.square(arr, dtype=_np.float32))))
                self._level_changed.emit(min(rms * self._gain * 4.0, 1.0))
            except Exception:
                # Audio callbacks cannot raise across the native sound thread.
                pass

        try:
            self._stream = _sd.InputStream(
                device=device_index,
                channels=1,
                dtype='float32',
                samplerate=44100,
                blocksize=1024,
                callback=_cb,
            )
            self._stream.start()
            self._peak_decay.start()
            return True
        except Exception as e:
            print(f'Mic meter start failed: {e}')
            self._stream = None
            return False

    def _on_rms_from_recorder(self, rms: float):
        """Called from MicRecorder audio thread when sharing its stream."""
        self._level_changed.emit(min(rms * self._gain * 4.0, 1.0))

    def stop(self):
        self._peak_decay.stop()
        if hasattr(self, '_shared_recorder') and self._shared_recorder is not None:
            self._shared_recorder.remove_rms_listener(self._on_rms_from_recorder)
            self._shared_recorder = None
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                # The audio device may already be detached during teardown.
                pass
            self._stream = None
        self._level = 0.0
        self._peak  = 0.0
        self.update()

    def _on_level(self, v: float):
        self._level = v
        if v > self._peak:
            self._peak = v
        self.update()

    def _decay_peak(self):
        self._peak  = max(self._peak  - 0.04, self._level)
        self._level = max(self._level - 0.06, 0.0)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w, h = self.width(), self.height()
        bar_h = 10
        bar_y = (h - bar_h) // 2

        p.fillRect(0, bar_y, w, bar_h, QColor(Colors.BG))
        p.setPen(QPen(QColor(Colors.HAIRLINE), 1))
        p.drawRect(0, bar_y, w - 1, bar_h - 1)

        fill_w = max(int((w - 2) * self._level), 0)
        if fill_w > 0:
            p.fillRect(1, bar_y + 1, fill_w, bar_h - 2, QColor(Colors.ACCENT))

        peak_x = int((w - 2) * self._peak)
        if peak_x > 0:
            p.fillRect(1 + peak_x, bar_y + 1, 2, bar_h - 2, QColor(Colors.TEXT))

        marker_specs = (
            (self._gary_min, 'MIN', QColor(Colors.WARNING)),
            (self._gary_max, 'MAX', QColor(Colors.TEXT)),
        )
        font = QFont(Fonts.BODY_FAMILY, 7)
        font.setBold(True)
        p.setFont(font)
        for value, label, color in marker_specs:
            x = self._threshold_x(value)
            p.fillRect(x - 3, bar_y - 4, 8, 5, color)
            p.fillRect(x, bar_y - 4, 2, bar_h + 7, color)
            p.setPen(color)
            if label == 'MIN':
                text_x = max(0, x - 48)
                alignment = (Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignTop)
            else:
                text_x = min(w - 48, x + 5)
                alignment = (Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignTop)
            p.drawText(QRect(text_x, 0, 44, 10), alignment,
                       f'{label} {value}')
        p.end()


# Popup panel base — shared look for all dropdown panels

class _PopupPanel(QFrame):
    """Base class for top-bar dropdown panels (capture settings, source, hotkeys)."""

    popup_hidden = Signal()

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('popupPanel')
        self.setMinimumWidth(300)
        # Do not dispatch to a subclass override before that subclass has
        # created its child widgets.
        self._apply_popup_style()
        self._show_anim: QPropertyAnimation | None = None

    def _apply_popup_style(self):
        set_theme_style(self, lambda: (f'''
            QFrame#popupPanel {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QFrame#popupPanel QLabel {{
                color: {Colors.TEXT};
                background-color: transparent;
            }}
        '''))

    def refresh_theme(self):
        """Refresh the shared popup shell after an Apply Theme action."""
        self._apply_popup_style()

    def hideEvent(self, event):  # noqa: N802 - Qt API name
        """Tell the trigger button when Qt closes the popup for any reason.

        Popup windows are also hidden by Qt when the user clicks outside them.
        That path does not pass through MainWindow's toggle handlers, so the
        trigger arrow needs the popup's actual visibility transition.
        """
        super().hideEvent(event)
        self.popup_hidden.emit()

    def show_below(self, button: QWidget):
        self.adjustSize()
        anchor_pos = button.mapToGlobal(QPoint(0, 0))
        pos = button.mapToGlobal(QPoint(0, button.height()))
        x = pos.x()
        y = pos.y()
        area = None
        margin = 8

        # Popups are wider than several top-bar buttons. Anchor from the left
        # while there is room, then right-align to the trigger and clamp to the
        # usable screen. This keeps the panel visible in maximized/fullscreen
        # windows and on secondary monitors with non-zero origins.
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()

            max_w = max(160, area.width() - margin * 2)
            max_h = max(120, area.height() - margin * 2)
            if self.width() > max_w or self.height() > max_h:
                self.resize(min(self.width(), max_w), min(self.height(), max_h))
            anchor = QRect(
                anchor_pos.x(), anchor_pos.y(),
                button.width(), button.height())
            placed = popup_rect_for_anchor(
                anchor, self.width(), self.height(), area, margin=margin)
            x, y = placed.x(), placed.y()
            if placed.size() != self.size():
                self.resize(placed.size())

        self.move(QPoint(x, y))

        app = QApplication.instance()
        animate = bool(app and app.platformName() != 'wayland' and not reduce_motion(self))
        if animate:
            self.setWindowOpacity(0.0)
        self.show()

        # Qt may add a one-pixel native popup frame after show(). Clamp the
        # realized frame as well so the visible border respects the margin.
        if area is not None:
            frame = self.frameGeometry()
            delta_x = 0
            delta_y = 0
            if frame.left() < area.left() + margin:
                delta_x = area.left() + margin - frame.left()
            elif frame.right() > area.right() - margin:
                delta_x = area.right() - margin - frame.right()
            if frame.top() < area.top() + margin:
                delta_y = area.top() + margin - frame.top()
            elif frame.bottom() > area.bottom() - margin:
                delta_y = area.bottom() - margin - frame.bottom()
            if delta_x or delta_y:
                self.move(self.pos() + QPoint(delta_x, delta_y))

        self.raise_()
        # setWindowOpacity on Popup windows is not supported on Wayland —
        # the compositor just ignores it and spams a warning every frame.
        # Skip the fade on Wayland; just show instantly. Looks fine.
        if animate:
            anim = QPropertyAnimation(self, b'windowOpacity', self)
            anim.setDuration(PANEL_FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._show_anim = anim
            anim.start()


class _ToggleSwitch(QCheckBox):
    """Rounded switch with a clickable surface covering the entire control."""

    def __init__(self, checked: bool = False, parent=None):
        super().__init__(parent)
        self.setChecked(checked)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedSize(38, 20)
        self.toggled.connect(lambda _checked: self.update())

    def hitButton(self, pos):
        return self.rect().contains(pos)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if self.isEnabled():
            track = Colors.ACCENT_SOFT if self.isChecked() else Colors.SURFACE_1
            border = (
                Colors.ACCENT
                if self.isChecked() or self.hasFocus()
                else Colors.BORDER_HI
            )
            knob = Colors.ACCENT if self.isChecked() else Colors.TEXT_DIM
        else:
            track = Colors.SURFACE_1
            border = Colors.BORDER
            knob = Colors.TEXT_MUTED

        painter.setPen(QPen(QColor(border), 1))
        painter.setBrush(QBrush(QColor(track)))
        painter.drawRoundedRect(1, 2, self.width() - 2, self.height() - 4, 8, 8)

        diameter = 12
        x = self.width() - diameter - 5 if self.isChecked() else 5
        y = (self.height() - diameter) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(knob)))
        painter.drawEllipse(x, y, diameter, diameter)
        painter.end()


# Capture Settings popup

class CustomClipLengthDialog(FthrDialog):
    """A compact minutes-only duration picker using the app's dialog shell."""

    def __init__(self, seconds=30, parent=None):
        super().__init__('Custom clip length', parent, width=340)
        validate_normal_clip_length(seconds)
        row = QHBoxLayout()
        label = QLabel('Length')
        set_theme_style(label, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        row.addWidget(label)
        self.duration_spin = _NumberSpinBox()
        self.duration_spin.setRange(1, 15)
        self.duration_spin.setValue(max(1, min(15, round(seconds / 60))))
        self.duration_spin.setSuffix(' min')
        self.duration_spin.setAccessibleName('Clip length in minutes')
        self.duration_spin.setKeyboardTracking(False)
        row.addWidget(self.duration_spin, 1)
        self.body_layout.addLayout(row)
        hint = QLabel('Choose 1–15 minutes.')
        set_theme_style(hint, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
        self.body_layout.addWidget(hint)
        self.memory_warning = QLabel('Longer clips use more memory, especially at high quality.')
        self.memory_warning.setWordWrap(True)
        set_theme_style(self.memory_warning, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
        self.body_layout.addWidget(self.memory_warning)
        cancel = QPushButton('Cancel')
        set_theme_style(cancel, lambda: (button_outline_qss()))
        cancel.clicked.connect(self.reject)
        self.add_button = QPushButton('Add length')
        set_theme_style(self.add_button, lambda: (button_primary_qss()))
        self.add_button.clicked.connect(self._accept_length)
        self.action_layout.addStretch()
        self.action_layout.addWidget(cancel)
        self.action_layout.addWidget(self.add_button)
        self.duration_spin.valueChanged.connect(self._update_warning)
        self._update_warning()

    def seconds(self):
        return self.duration_spin.value() * 60

    def _update_warning(self, _value=None):
        self.memory_warning.setVisible(self.seconds() >= 120)

    def _accept_length(self):
        self.duration_spin.interpretText()
        self.accept()


class CaptureSettingsPopup(_PopupPanel):
    """Dropdown panel for independent replay-clip and recording profiles."""

    clip_length_changed   = Signal(int)
    custom_lengths_changed = Signal()
    framerate_changed     = Signal(int)
    resolution_changed  = Signal(int, int)
    bitrate_changed     = Signal(int)
    restart_needed      = Signal()
    summary_changed     = Signal(str)   # emitted whenever any value changes

    _CLIP_VALUES  = list(NORMAL_CLIP_VALUES)
    _CLIP_LABELS  = ['5s','10s','15s','30s','45s','1m','1m 30s','2m','3m','4m','5m']
    _FPS_VALUES   = list(FPS_VALUES)
    _RES_LABELS   = ['480p','720p','1080p','1440p','Source']
    _RES_KEYS     = ['480p','720p','1080p','1440p','source']
    _QUAL_LABELS  = ['Low', 'Medium', 'High', 'Custom']
    _QUAL_KEYS    = ['low', 'medium', 'high', 'custom']
    _MIN_CUSTOM_BITRATE_KBPS = 500
    _MAX_CUSTOM_BITRATE_KBPS = 200_000
    _CUSTOM_BITRATE_STEP_KBPS = 500

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self._restart_pending = False
        self.setMinimumWidth(460)

        self.cur_clip   = self.sm.get('clip_length',    30)
        self.cur_fps    = self.sm.get('framerate',       60)
        self.cur_res    = self.sm.get('resolution',   'source')
        self.cur_qual   = self.sm.get('bitrate_level', 'high')
        self.cur_custom_bitrate = self._valid_custom_bitrate(
            self.sm.get('custom_bitrate_kbps'),
            BITRATE_PRESETS.get(self.cur_res, BITRATE_PRESETS['source']).get('high', 50_000),
        )
        self.recording_fps = self.sm.get('recording_framerate', self.cur_fps)
        self.recording_res = self.sm.get('recording_resolution', self.cur_res)
        self.recording_qual = self.sm.get('recording_bitrate_level', self.cur_qual)
        self.recording_custom_bitrate = self._valid_custom_bitrate(
            self.sm.get('recording_custom_bitrate_kbps'),
            self.cur_custom_bitrate,
        )
        self._settings_profile = 'clips'

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        def _row(label_text, combo):
            row = QHBoxLayout()
            row.setSpacing(16)
            lbl = QLabel(label_text)
            set_theme_style(lbl, lambda: (_LABEL_STYLE))
            lbl.setFixedWidth(92)
            row.addWidget(lbl)
            row.addWidget(combo, stretch=1)
            return row

        profile_row = QHBoxLayout()
        profile_row.setSpacing(8)
        self.clips_profile_btn = self._make_profile_button('Clips')
        self.recording_profile_btn = self._make_profile_button('Recording')
        self.clips_profile_btn.setChecked(True)
        self.clips_profile_btn.clicked.connect(
            lambda: self._set_settings_profile('clips'))
        self.recording_profile_btn.clicked.connect(
            lambda: self._set_settings_profile('recording'))
        profile_row.addWidget(self.clips_profile_btn)
        profile_row.addWidget(self.recording_profile_btn)
        layout.addLayout(profile_row)

        self.clip_fields = QWidget()
        self.clip_fields.setStyleSheet('background: transparent;')
        clip_fields_layout = QVBoxLayout(self.clip_fields)
        clip_fields_layout.setContentsMargins(0, 0, 0, 0)
        clip_fields_layout.setSpacing(12)

        # Clip length
        self.clip_combo = self._make_combo([], -1, self._on_clip_changed)
        self._reload_clip_choices()
        clip_fields_layout.addLayout(_row('Clip length', self.clip_combo))
        self.add_custom_length_btn = QPushButton('Add custom clip length…')
        set_theme_style(self.add_custom_length_btn, lambda: (button_outline_qss()))
        self.add_custom_length_btn.clicked.connect(self._add_custom_length)
        clip_fields_layout.addWidget(self.add_custom_length_btn)
        self.custom_lengths_scroll = QScrollArea()
        self.custom_lengths_scroll.setWidgetResizable(True)
        self.custom_lengths_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.custom_lengths_widget = QWidget()
        self.custom_lengths_layout = QVBoxLayout(self.custom_lengths_widget)
        self.custom_lengths_layout.setContentsMargins(0, 0, 0, 0)
        self.custom_lengths_scroll.setWidget(self.custom_lengths_widget)
        clip_fields_layout.addWidget(self.custom_lengths_scroll)
        self.custom_lengths_hint = QLabel('Set a separate shortcut for each length in Hotkeys.')
        self.custom_lengths_hint.setWordWrap(True)
        set_theme_style(self.custom_lengths_hint, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
        clip_fields_layout.addWidget(self.custom_lengths_hint)
        self._reload_custom_lengths()

        self.clip_memory_warning = QLabel(
            'Longer clips can use a lot of memory, especially at high quality.')
        self.clip_memory_warning.setWordWrap(True)
        set_theme_style(self.clip_memory_warning, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
        self.clip_memory_warning.setVisible(self.cur_clip >= 120)
        clip_fields_layout.addWidget(self.clip_memory_warning)

        # FPS
        fps_idx = self._FPS_VALUES.index(self.cur_fps) \
            if self.cur_fps in self._FPS_VALUES else 1
        self.fps_combo = self._make_combo(
            [str(v) for v in self._FPS_VALUES], fps_idx, self._on_fps_changed)
        clip_fields_layout.addLayout(_row('Framerate', self.fps_combo))

        # Resolution
        res_idx = self._RES_KEYS.index(self.cur_res.lower()) \
            if self.cur_res.lower() in self._RES_KEYS else 4
        self.res_combo = self._make_combo(self._RES_LABELS, res_idx,
                                          self._on_res_changed)
        clip_fields_layout.addLayout(_row('Resolution', self.res_combo))

        # Quality
        qual_idx = self._QUAL_KEYS.index(self.cur_qual) \
            if self.cur_qual in self._QUAL_KEYS else 2
        self.qual_combo = self._make_combo(self._QUAL_LABELS, qual_idx,
                                            self._on_qual_changed)
        clip_fields_layout.addLayout(_row('Quality', self.qual_combo))

        # Keep the editable value and both step buttons inside one control.
        # This restores the compact original treatment and aligns the control
        # edge with the selectors above instead of pulling the minus button
        # into the label gutter.
        bitrate_row = QHBoxLayout()
        bitrate_row.setSpacing(16)
        bitrate_label = QLabel('Custom bitrate')
        set_theme_style(bitrate_label, lambda: (_LABEL_STYLE))
        bitrate_label.setFixedWidth(92)
        bitrate_row.addWidget(bitrate_label)

        self.custom_bitrate_spin = _NumberSpinBox()
        self.custom_bitrate_spin.setRange(
            self._MIN_CUSTOM_BITRATE_KBPS,
            self._MAX_CUSTOM_BITRATE_KBPS,
        )
        self.custom_bitrate_spin.setSingleStep(self._CUSTOM_BITRATE_STEP_KBPS)
        self.custom_bitrate_spin.setAccelerated(True)
        self.custom_bitrate_spin.setKeyboardTracking(False)
        self.custom_bitrate_spin.setValue(self.cur_custom_bitrate)
        self.custom_bitrate_spin.setSuffix(' kbps')
        self.custom_bitrate_spin.setMinimumWidth(190)
        self.custom_bitrate_spin.setAccessibleName(
            'Custom bitrate in kilobits per second')
        self.custom_bitrate_spin.setToolTip(
            'Choose any video bitrate from 500 to 200,000 kbps.')
        self.custom_bitrate_spin.valueChanged.connect(self._set_custom_bitrate)
        self.custom_bitrate_spin.editingFinished.connect(
            self._commit_custom_bitrate_text)
        bitrate_row.addWidget(self.custom_bitrate_spin, stretch=1)

        # Compatibility aliases keep the public/test surface stable while the
        # visual control is now a single integrated spin box.
        self.custom_bitrate_edit = self.custom_bitrate_spin.lineEdit()
        self.bitrate_minus_btn = self.custom_bitrate_spin._minus
        self.bitrate_plus_btn = self.custom_bitrate_spin._plus
        self.bitrate_minus_btn.setAccessibleName('Decrease custom bitrate by 500 kbps')
        self.bitrate_plus_btn.setAccessibleName('Increase custom bitrate by 500 kbps')
        clip_fields_layout.addLayout(bitrate_row)
        self._sync_custom_bitrate_controls()
        layout.addWidget(self.clip_fields)

        # Recording profile. The native recorder writes encoder packets
        # directly, so MainWindow activates this profile before recording and
        # restores the clip profile after the file has been finalized.
        self.recording_fields = QWidget()
        self.recording_fields.setStyleSheet('background: transparent;')
        recording_fields_layout = QVBoxLayout(self.recording_fields)
        recording_fields_layout.setContentsMargins(0, 0, 0, 0)
        recording_fields_layout.setSpacing(12)

        recording_fps_idx = self._FPS_VALUES.index(self.recording_fps) \
            if self.recording_fps in self._FPS_VALUES else 1
        self.recording_fps_combo = self._make_combo(
            [str(v) for v in self._FPS_VALUES], recording_fps_idx,
            self._on_recording_fps_changed)
        recording_fields_layout.addLayout(
            _row('Framerate', self.recording_fps_combo))

        recording_res_idx = self._RES_KEYS.index(str(self.recording_res).lower()) \
            if str(self.recording_res).lower() in self._RES_KEYS else 4
        self.recording_res_combo = self._make_combo(
            self._RES_LABELS, recording_res_idx, self._on_recording_res_changed)
        recording_fields_layout.addLayout(
            _row('Resolution', self.recording_res_combo))

        recording_qual_idx = self._QUAL_KEYS.index(self.recording_qual) \
            if self.recording_qual in self._QUAL_KEYS else 2
        self.recording_qual_combo = self._make_combo(
            self._QUAL_LABELS, recording_qual_idx, self._on_recording_qual_changed)
        recording_fields_layout.addLayout(
            _row('Quality', self.recording_qual_combo))

        recording_bitrate_row = QHBoxLayout()
        recording_bitrate_row.setSpacing(16)
        recording_bitrate_label = QLabel('Custom bitrate')
        set_theme_style(recording_bitrate_label, lambda: (_LABEL_STYLE))
        recording_bitrate_label.setFixedWidth(92)
        recording_bitrate_row.addWidget(recording_bitrate_label)
        self.recording_custom_bitrate_spin = _NumberSpinBox()
        self.recording_custom_bitrate_spin.setRange(
            self._MIN_CUSTOM_BITRATE_KBPS,
            self._MAX_CUSTOM_BITRATE_KBPS,
        )
        self.recording_custom_bitrate_spin.setSingleStep(
            self._CUSTOM_BITRATE_STEP_KBPS)
        self.recording_custom_bitrate_spin.setAccelerated(True)
        self.recording_custom_bitrate_spin.setKeyboardTracking(False)
        self.recording_custom_bitrate_spin.setValue(
            self.recording_custom_bitrate)
        self.recording_custom_bitrate_spin.setSuffix(' kbps')
        self.recording_custom_bitrate_spin.setMinimumWidth(190)
        self.recording_custom_bitrate_spin.setAccessibleName(
            'Recording custom bitrate in kilobits per second')
        self.recording_custom_bitrate_spin.setToolTip(
            'Choose any recording bitrate from 500 to 200,000 kbps.')
        self.recording_custom_bitrate_spin.valueChanged.connect(
            self._set_recording_custom_bitrate)
        self.recording_custom_bitrate_spin.editingFinished.connect(
            self._commit_recording_custom_bitrate_text)
        recording_bitrate_row.addWidget(
            self.recording_custom_bitrate_spin, stretch=1)
        recording_fields_layout.addLayout(recording_bitrate_row)
        self.recording_fields.setVisible(False)
        self._sync_recording_custom_bitrate_control()
        layout.addWidget(self.recording_fields)

        # The capture state lives once in the always-visible top bar. Keep this
        # row focused on the action that belongs to this popup.
        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.restart_btn = QPushButton('Apply')
        set_theme_style(self.restart_btn, lambda: (button_primary_qss()))
        self.restart_btn.setVisible(False)
        self.restart_btn.clicked.connect(self._on_restart)
        apply_row.addWidget(self.restart_btn)
        layout.addLayout(apply_row)

    def _make_combo(self, items, idx, callback):
        c = _DropdownCombo()
        c.addItems(items)
        c.setCurrentIndex(idx)
        set_theme_style(c, lambda: (_COMBO_STYLE))
        c.currentIndexChanged.connect(callback)
        return c

    def refresh_theme(self):
        super().refresh_theme()
        for combo in self.findChildren(QComboBox):
            set_theme_style(combo, lambda: (_COMBO_STYLE))
        for label in self.findChildren(QLabel):
            if label.text() in {
                    'Clip length', 'Framerate', 'Resolution',
                    'Quality', 'Custom bitrate'}:
                set_theme_style(label, lambda: (_LABEL_STYLE))
        for button in (self.clips_profile_btn, self.recording_profile_btn):
            set_theme_style(button, lambda: (self._profile_button_qss()))
        set_theme_style(self.clip_memory_warning, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
        set_theme_style(self.restart_btn, lambda: (button_primary_qss()))

    @staticmethod
    def _profile_button_qss() -> str:
        return f'''
            QToolButton {{
                background-color: {Colors.SURFACE_1};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_LABEL}px;
                font-weight: 600;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                padding: 0 12px;
            }}
            QToolButton:hover {{
                border-color: {Colors.ACCENT}; color: {Colors.TEXT};
            }}
            QToolButton:focus {{ border-color: {Colors.ACCENT}; }}
            QToolButton:checked {{
                background-color: {Colors.ACCENT_SOFT};
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
        '''

    @staticmethod
    def _make_profile_button(text: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        button.setFixedHeight(30)
        button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        set_theme_style(button, lambda: (CaptureSettingsPopup._profile_button_qss()))
        return button

    def _set_settings_profile(self, profile: str):
        profile = 'recording' if profile == 'recording' else 'clips'
        self._settings_profile = profile
        is_clips = profile == 'clips'
        self.clips_profile_btn.setChecked(is_clips)
        self.recording_profile_btn.setChecked(not is_clips)
        self.clip_fields.setVisible(is_clips)
        self.recording_fields.setVisible(not is_clips)
        self.restart_btn.setVisible(is_clips and self._restart_pending)
        self.adjustSize()

    def _on_clip_changed(self, idx):
        if idx < 0:
            return
        seconds = self.clip_combo.itemData(idx)
        self.cur_clip = validate_normal_clip_length(seconds)
        self.sm.set('clip_length', self.cur_clip)
        self.sm.save_settings()
        self._reload_clip_choices()
        self.clip_memory_warning.setVisible(self.cur_clip >= 120)
        self.adjustSize()
        self._mark_restart()
        self.clip_length_changed.emit(self.cur_clip)
        self._update_summary()

    def _add_custom_length(self):
        dialog = CustomClipLengthDialog(self.cur_clip, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        seconds = validate_normal_clip_length(dialog.seconds())
        custom = sorted(set(self._custom_clip_lengths() + [seconds]))
        if custom == self._custom_clip_lengths():
            return
        self.sm.set('custom_clip_lengths', custom)
        self.sm.save_settings()
        self._reload_custom_lengths()
        self._mark_restart()
        self.custom_lengths_changed.emit()
        self.adjustSize()

    def _custom_clip_lengths(self):
        return custom_clip_lengths(self.sm.get('custom_clip_lengths', []))

    def _reload_custom_lengths(self):
        count = len(self._custom_clip_lengths())
        self.custom_lengths_scroll.setVisible(bool(count))
        self.custom_lengths_scroll.setFixedHeight(min(150, count * 48))
        self.custom_lengths_hint.setVisible(bool(count))
        while self.custom_lengths_layout.count():
            item = self.custom_lengths_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for seconds in self._custom_clip_lengths():
            widget = QWidget()
            row = QHBoxLayout(widget)
            row.setContentsMargins(0, 0, 0, 0)
            label = QLabel(f'Custom {self._clip_length_label(seconds)}')
            set_theme_style(label, lambda: (_LABEL_STYLE))
            row.addWidget(label, 1)
            remove = QPushButton('Remove')
            set_theme_style(remove, lambda: (button_outline_qss()))
            remove.clicked.connect(lambda _checked=False, value=seconds:
                                   self._remove_custom_length(value))
            row.addWidget(remove)
            self.custom_lengths_layout.addWidget(widget)
    def _remove_custom_length(self, seconds):
        self.sm.set('custom_clip_lengths', [value for value in
                    self._custom_clip_lengths() if value != seconds])
        self.sm.save_settings()
        self._reload_custom_lengths()
        self._mark_restart()
        self.custom_lengths_changed.emit()
        self.adjustSize()

    @staticmethod
    def _clip_length_label(seconds):
        minutes, remainder = divmod(seconds, 60)
        if not minutes:
            return f'{seconds}s'
        value = f'{seconds / 60:.2f}'.rstrip('0').rstrip('.')
        return f'{value}m'

    def _reload_clip_choices(self):
        values = sorted(set(self._CLIP_VALUES + [self.cur_clip]))
        self.clip_combo.blockSignals(True)
        self.clip_combo.clear()
        for seconds in values:
            self.clip_combo.addItem(self._clip_length_label(seconds), seconds)
        self.clip_combo.setCurrentIndex(self.clip_combo.findData(self.cur_clip))
        self.clip_combo.blockSignals(False)


    def reload_from_settings(self):
        self.cur_clip = self.sm.get('clip_length', 30)
        self._reload_custom_lengths()
        self.cur_fps  = self.sm.get('framerate', 60)
        self.cur_res  = self.sm.get('resolution', 'source')
        self.cur_qual = self.sm.get('bitrate_level', 'high')
        self.cur_custom_bitrate = self._valid_custom_bitrate(
            self.sm.get('custom_bitrate_kbps'), self.cur_custom_bitrate)
        self.recording_fps = self.sm.get(
            'recording_framerate', self.recording_fps)
        self.recording_res = self.sm.get(
            'recording_resolution', self.recording_res)
        self.recording_qual = self.sm.get(
            'recording_bitrate_level', self.recording_qual)
        self.recording_custom_bitrate = self._valid_custom_bitrate(
            self.sm.get('recording_custom_bitrate_kbps'),
            self.recording_custom_bitrate)

        self._reload_clip_choices()
        self.clip_memory_warning.setVisible(self.cur_clip >= 120)
        for combo, values, val in [
            (self.fps_combo,  self._FPS_VALUES,   self.cur_fps),
        ]:
            idx = values.index(val) if val in values else 0
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)

        res_idx = self._RES_KEYS.index(self.cur_res.lower()) \
            if self.cur_res.lower() in self._RES_KEYS else 4
        self.res_combo.blockSignals(True)
        self.res_combo.setCurrentIndex(res_idx)
        self.res_combo.blockSignals(False)

        qual_idx = self._QUAL_KEYS.index(self.cur_qual) \
            if self.cur_qual in self._QUAL_KEYS else 2
        self.qual_combo.blockSignals(True)
        self.qual_combo.setCurrentIndex(qual_idx)
        self.qual_combo.blockSignals(False)
        self.custom_bitrate_spin.blockSignals(True)
        self.custom_bitrate_spin.setValue(self.cur_custom_bitrate)
        self.custom_bitrate_spin.blockSignals(False)
        self._sync_custom_bitrate_controls()

        for combo, values, val in (
                (self.recording_fps_combo, self._FPS_VALUES, self.recording_fps),
                (self.recording_res_combo, self._RES_KEYS,
                 str(self.recording_res).lower()),
                (self.recording_qual_combo, self._QUAL_KEYS,
                 self.recording_qual)):
            idx = values.index(val) if val in values else 0
            combo.blockSignals(True)
            combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        self.recording_custom_bitrate_spin.blockSignals(True)
        self.recording_custom_bitrate_spin.setValue(
            self.recording_custom_bitrate)
        self.recording_custom_bitrate_spin.blockSignals(False)
        self._sync_recording_custom_bitrate_control()

        self._update_summary()

    def _on_fps_changed(self, idx):
        self.cur_fps = self._FPS_VALUES[idx]
        self.sm.set('framerate', self.cur_fps)
        self.sm.save_settings()
        self._mark_restart()
        self.framerate_changed.emit(self.cur_fps)
        self._update_summary()

    def _on_res_changed(self, idx):
        self.cur_res = self._RES_KEYS[idx]
        self.sm.set('resolution', self.cur_res)
        self.sm.save_settings()
        self._mark_restart()
        dims = _resolution_to_dims(self.cur_res)
        self.resolution_changed.emit(dims[0], dims[1])
        self.bitrate_changed.emit(self.get_bitrate())
        self._update_summary()

    def _on_qual_changed(self, idx):
        self.cur_qual = self._QUAL_KEYS[idx]
        self.sm.set('bitrate_level', self.cur_qual)
        if self.cur_qual == 'custom':
            self.sm.set('custom_bitrate_kbps', self.cur_custom_bitrate)
        self.sm.save_settings()
        self._mark_restart()
        self._sync_custom_bitrate_controls()
        self.bitrate_changed.emit(self.get_bitrate())
        self._update_summary()

    def _on_recording_fps_changed(self, idx):
        self.recording_fps = self._FPS_VALUES[idx]
        self.sm.set('recording_framerate', self.recording_fps)
        self.sm.save_settings()

    def _on_recording_res_changed(self, idx):
        self.recording_res = self._RES_KEYS[idx]
        self.sm.set('recording_resolution', self.recording_res)
        self.sm.save_settings()

    def _on_recording_qual_changed(self, idx):
        self.recording_qual = self._QUAL_KEYS[idx]
        self.sm.set('recording_bitrate_level', self.recording_qual)
        if self.recording_qual == 'custom':
            self.sm.set(
                'recording_custom_bitrate_kbps',
                self.recording_custom_bitrate)
        self.sm.save_settings()
        self._sync_recording_custom_bitrate_control()

    def _valid_custom_bitrate(self, value, fallback: int) -> int:
        try:
            bitrate = int(value)
        except (TypeError, ValueError):
            bitrate = int(fallback)
        return max(self._MIN_CUSTOM_BITRATE_KBPS,
                   min(self._MAX_CUSTOM_BITRATE_KBPS, bitrate))

    def _sync_custom_bitrate_controls(self):
        enabled = self.cur_qual == 'custom'
        self.custom_bitrate_spin.setEnabled(enabled)
        self.bitrate_minus_btn.setEnabled(
            enabled and self.cur_custom_bitrate > self._MIN_CUSTOM_BITRATE_KBPS)
        self.bitrate_plus_btn.setEnabled(
            enabled and self.cur_custom_bitrate < self._MAX_CUSTOM_BITRATE_KBPS)

    def _adjust_custom_bitrate(self, delta: int):
        self._set_custom_bitrate(self.cur_custom_bitrate + delta)

    def _commit_custom_bitrate_text(self):
        self._set_custom_bitrate(self._valid_custom_bitrate(
            self.custom_bitrate_spin.cleanText(), self.cur_custom_bitrate))

    def _set_custom_bitrate(self, value: int):
        bitrate = self._valid_custom_bitrate(value, self.cur_custom_bitrate)
        if bitrate == self.cur_custom_bitrate:
            self.custom_bitrate_spin.blockSignals(True)
            self.custom_bitrate_spin.setValue(bitrate)
            self.custom_bitrate_spin.blockSignals(False)
            self._sync_custom_bitrate_controls()
            return
        self.cur_custom_bitrate = bitrate
        self.custom_bitrate_spin.blockSignals(True)
        self.custom_bitrate_spin.setValue(bitrate)
        self.custom_bitrate_spin.blockSignals(False)
        self.sm.set('custom_bitrate_kbps', bitrate)
        self.sm.save_settings()
        self._sync_custom_bitrate_controls()
        if self.cur_qual == 'custom':
            self._mark_restart()
            self.bitrate_changed.emit(bitrate)
            self._update_summary()

    def _sync_recording_custom_bitrate_control(self):
        self.recording_custom_bitrate_spin.setEnabled(
            self.recording_qual == 'custom')

    def _commit_recording_custom_bitrate_text(self):
        self._set_recording_custom_bitrate(self._valid_custom_bitrate(
            self.recording_custom_bitrate_spin.cleanText(),
            self.recording_custom_bitrate))

    def _set_recording_custom_bitrate(self, value: int):
        bitrate = self._valid_custom_bitrate(
            value, self.recording_custom_bitrate)
        self.recording_custom_bitrate = bitrate
        self.recording_custom_bitrate_spin.blockSignals(True)
        self.recording_custom_bitrate_spin.setValue(bitrate)
        self.recording_custom_bitrate_spin.blockSignals(False)
        self.sm.set('recording_custom_bitrate_kbps', bitrate)
        self.sm.save_settings()

    def _mark_restart(self):
        self._restart_pending = True
        self.restart_btn.setVisible(self._settings_profile == 'clips')

    def _on_restart(self):
        self._restart_pending = False
        self.restart_btn.setEnabled(False)
        self.restart_needed.emit()

    def set_apply_state(self, applying: bool):
        self.restart_btn.setEnabled(not applying)
        if not applying and not self._restart_pending:
            self.restart_btn.setVisible(False)

    def _update_summary(self):
        self.summary_changed.emit(self.get_summary())

    def get_summary(self) -> str:
        clip_label = self._clip_length_label(self.cur_clip)
        res_label  = self._RES_LABELS[self._RES_KEYS.index(self.cur_res.lower())] \
            if self.cur_res.lower() in self._RES_KEYS else self.cur_res
        return f'{clip_label} // {self.cur_fps} fps // {res_label} // {self.get_bitrate():,} kbps'

    def get_bitrate(self) -> int:
        if self.cur_qual == 'custom':
            return self.cur_custom_bitrate
        return BITRATE_PRESETS[self.cur_res][self.cur_qual]

    def get_recording_bitrate(self) -> int:
        if self.recording_qual == 'custom':
            return self.recording_custom_bitrate
        presets = BITRATE_PRESETS.get(
            str(self.recording_res).lower(), BITRATE_PRESETS['source'])
        return presets.get(self.recording_qual, presets['high'])


# Source popup

class SourcePopup(_PopupPanel):
    """Dropdown panel: desktop / window selector."""

    source_changed = Signal(str, int)   # (mode, hwnd)
    restart_needed = Signal()
    summary_changed = Signal(str)

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.cur_mode    = self.sm.get('capture_mode',    'desktop')
        self.cur_hwnd    = self.sm.get('target_hwnd',     0)
        self.cur_monitor = self.sm.get('capture_monitor', '')
        self._window_list: list = []
        self._restart_pending = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        # This popup swaps monitor/window rows and reveals APPLY in place. Keep
        # the layout's minimum current while an explicit fit helper preserves
        # the standard 300px panel width and removes stale vertical geometry.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(Sizes.ROW_GAP)

        lbl = QLabel('Capture source')
        set_theme_style(lbl, lambda: (_LABEL_STYLE))
        layout.addWidget(lbl)

        self.mode_combo = _DropdownCombo()
        self.mode_combo.addItems(['Desktop', 'Window / Game'])
        set_theme_style(self.mode_combo, lambda: (_COMBO_STYLE))
        if self.cur_mode == 'window':
            self.mode_combo.setCurrentIndex(1)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_combo)

        # Window list row
        win_row = QHBoxLayout()
        win_row.setSpacing(Sizes.SPACE_3)
        self.window_combo = _DropdownCombo()
        set_theme_style(self.window_combo, lambda: (_COMBO_STYLE))
        self.window_combo.setMinimumWidth(240)
        self.window_combo.currentIndexChanged.connect(self._on_window_selected)
        win_row.addWidget(self.window_combo)

        self.refresh_btn = QPushButton()
        _ref_ico = _load_icon('refresh.png', 14)
        if not _ref_ico.isNull():
            self.refresh_btn.setIcon(_ref_ico)
            self.refresh_btn.setIconSize(QSize(14, 14))
            _register_icon_widget(self.refresh_btn, 'refresh.png', 14)
        else:
            self.refresh_btn.setText('Refresh')
        self.refresh_btn.setFixedSize(28, 28)
        set_theme_style(self.refresh_btn, lambda: (f'''
            QPushButton {{
                background-color: {Colors.BG};
                border: {Sizes.BORDER_W}px solid {Colors.TEXT};
                color: {Colors.TEXT};
                font-size: 14px;
            }}
            QPushButton:hover {{
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}
        '''))
        self.refresh_btn.clicked.connect(self._refresh_windows)
        win_row.addWidget(self.refresh_btn)
        layout.addLayout(win_row)

        # Monitor selector (desktop mode only)
        self.monitor_combo = _DropdownCombo()
        set_theme_style(self.monitor_combo, lambda: (_COMBO_STYLE))
        if sys.platform != 'win32':
            # The Linux engine captures one output by its compositor/xrandr
            # name, which is what QScreen.name() reports; '' is the default.
            self._monitor_selection_result = None
            self.monitor_combo.addItem('First Screen (Default)', userData='')
            for screen in QApplication.screens():
                geometry = screen.availableGeometry()
                self.monitor_combo.addItem(
                    f'{screen.name()}  ({geometry.width()}×{geometry.height()} '
                    f'@ {int(screen.refreshRate())}Hz)',
                    userData=screen.name(),
                )
            saved_mon = str(self.cur_monitor or '')
        else:
            monitor_choices = enumerate_windows_monitors()
            screens_by_name = {
                screen.name().lower(): screen for screen in QApplication.screens()
            }
            for choice in monitor_choices:
                screen = screens_by_name.get(choice.gdi_name.lower())
                details = ''
                if screen is not None:
                    geometry = screen.availableGeometry()
                    details = (
                        f'  ({geometry.width()}×{geometry.height()} '
                        f'@ {int(screen.refreshRate())}Hz)'
                    )
                primary = ' · Primary' if choice.primary else ''
                self.monitor_combo.addItem(
                    f'{choice.friendly_name}{details}{primary}',
                    userData=choice.device_path,
                )

            stored_mon = str(self.cur_monitor or '')
            selection = resolve_monitor_selection(stored_mon, monitor_choices)
            self._monitor_selection_result = selection
            saved_mon = selection.path
            if selection.persist_required and selection.path:
                previous_value = self.sm.get('capture_monitor', stored_mon)
                self.sm.set('capture_monitor', selection.path)
                if not self.sm.save_settings():
                    # Do not leave an in-memory path that was not committed to
                    # settings.  Startup will independently fail closed if the
                    # same persistence operation cannot be completed.
                    self.sm.set('capture_monitor', previous_value)
                    saved_mon = str(previous_value or '')
                    self._monitor_selection_result = MonitorSelectionResult(
                        state=MonitorSelectionState.PERSIST_FAILED,
                        code='MONITOR_PERSIST_FAILED',
                        reason='could not persist resolved monitor path',
                        previous_state=selection.previous_state,
                        previous_path=selection.previous_path,
                    )
                    emit_event(
                        'capture', 'monitor_selection_failed', state='FAILED',
                        error='MONITOR_PERSIST_FAILED',
                        monitor_selection=self._monitor_selection_result.as_dict())
            if self._monitor_selection_result.path:
                emit_event(
                    'capture', self._monitor_selection_result.code.lower(),
                    **self._monitor_selection_result.as_dict())
            if not saved_mon and stored_mon:
                if is_monitor_selection_sentinel(stored_mon):
                    # Diagnostic sentinels are not monitor identities. Keep
                    # the unresolved state visible without putting the marker
                    # into combo userData or the in-memory selection.
                    self.monitor_combo.addItem(
                        'Monitor selection requires recovery', userData='')
                elif is_monitor_capture_identity(stored_mon):
                    # A stale canonical path may remain visible as a bounded
                    # diagnostic-only row; it is still rejected by startup
                    # unless the resolver can recover it.
                    saved_mon = stored_mon
                    self.monitor_combo.addItem(
                        'Unavailable monitor (saved)', userData=stored_mon)
                else:
                    # Legacy/non-canonical values are never exposed as a
                    # selectable monitor identity when migration is ambiguous.
                    self.monitor_combo.addItem(
                        'Monitor selection requires recovery', userData='')
            if saved_mon != self.cur_monitor:
                self.cur_monitor = saved_mon
            if not monitor_choices:
                self.monitor_combo.addItem('No active Windows monitor found', userData='')
                self.monitor_combo.setEnabled(False)
        idx = self.monitor_combo.findData(saved_mon)
        if idx >= 0:
            self.monitor_combo.setCurrentIndex(idx)
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        layout.addWidget(self.monitor_combo)

        apply_row = QHBoxLayout()
        apply_row.addStretch(1)
        self.restart_btn = QPushButton('Apply')
        set_theme_style(self.restart_btn, lambda: (button_primary_qss()))
        self.restart_btn.setVisible(False)
        self.restart_btn.clicked.connect(self._on_restart)
        apply_row.addWidget(self.restart_btn)
        layout.addLayout(apply_row)

        self._update_window_visibility()
        if self.cur_mode == 'window':
            self._refresh_windows()

    def _update_window_visibility(self):
        show = (self.cur_mode == 'window')
        self.window_combo.setVisible(show)
        self.refresh_btn.setVisible(show)
        self.monitor_combo.setVisible(not show)
        self._fit_to_visible_content()

    def _fit_to_visible_content(self):
        layout = self.layout()
        if layout is None:
            return
        layout.invalidate()
        layout.activate()
        hint = self.sizeHint()
        self.setMinimumWidth(300)
        self.resize(max(300, hint.width()), hint.height())

    def refresh_theme(self):
        super().refresh_theme()
        for combo in self.findChildren(QComboBox):
            set_theme_style(combo, lambda: (_COMBO_STYLE))
        for label in self.findChildren(QLabel):
            if label.text() == 'CAPTURE SOURCE':
                set_theme_style(label, lambda: (_LABEL_STYLE))
        set_theme_style(self.refresh_btn, lambda: (f'''
            QPushButton {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                color: {Colors.TEXT}; font-size: 14px;
            }}
            QPushButton:hover {{
                border-color: {Colors.ACCENT}; color: {Colors.ACCENT};
            }}
        '''))
        set_theme_style(self.restart_btn, lambda: (button_primary_qss()))

    def show_below(self, button: QWidget):
        self._fit_to_visible_content()
        super().show_below(button)

    def _on_monitor_changed(self, _idx: int):
        self.cur_monitor = self.monitor_combo.currentData()
        self.sm.set('capture_monitor', self.cur_monitor)
        self.sm.save_settings()
        self._mark_dirty()
        self._emit_summary()

    def _on_mode_changed(self, idx):
        self.cur_mode = 'window' if idx == 1 else 'desktop'
        self.sm.set('capture_mode', self.cur_mode)
        if self.cur_mode == 'desktop':
            self.cur_hwnd = 0
            self.sm.set('target_hwnd', 0)
        self._update_window_visibility()
        if self.cur_mode == 'window':
            self._refresh_windows()
        self.sm.save_settings()
        self._mark_dirty()
        self._emit_summary()

    def _refresh_windows(self):
        self._window_list = _enumerate_capturable_windows()
        self.window_combo.blockSignals(True)
        self.window_combo.clear()
        select_idx = 0
        for i, w in enumerate(self._window_list):
            label = ('★ ' if w['is_game'] else '') + w['display_name']
            if w.get('icon'):
                self.window_combo.addItem(w['icon'], label)
            else:
                self.window_combo.addItem(label)
            if w['hwnd'] == self.cur_hwnd:
                select_idx = i
        if self._window_list:
            self.window_combo.setCurrentIndex(select_idx)
            self.cur_hwnd = self._window_list[select_idx]['hwnd']
            self.sm.set('target_hwnd', self.cur_hwnd)
        self.window_combo.blockSignals(False)

    def _on_window_selected(self, idx):
        if 0 <= idx < len(self._window_list):
            self.cur_hwnd = self._window_list[idx]['hwnd']
            self.sm.set('target_window_name',
                        self._window_list[idx].get('display_name', ''))
            self.sm.set('target_hwnd', self.cur_hwnd)
            self.sm.save_settings()
            self._mark_dirty()
            self._emit_summary()

    def set_capture_window(self, window: dict) -> bool:
        """Select a detected window and persist it as the capture source."""
        if not isinstance(window, dict):
            return False
        try:
            hwnd = int(window.get('hwnd', 0) or 0)
        except (TypeError, ValueError):
            hwnd = 0
        if not hwnd:
            return False

        existing = next(
            (i for i, item in enumerate(self._window_list)
             if int(item.get('hwnd', 0) or 0) == hwnd),
            None,
        )
        if existing is None:
            self._window_list.append(window)
            existing = len(self._window_list) - 1
            label = ('★ ' if window.get('is_game') else '') + window.get(
                'display_name', window.get('title', 'Window'))
            icon = window.get('icon')
            if icon:
                self.window_combo.addItem(icon, label)
            else:
                self.window_combo.addItem(label)

        self.cur_mode = 'window'
        self.cur_hwnd = hwnd
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(1)
        self.mode_combo.blockSignals(False)
        self._update_window_visibility()
        self.window_combo.blockSignals(True)
        self.window_combo.setCurrentIndex(existing)
        self.window_combo.blockSignals(False)

        self.sm.set('capture_mode', 'window')
        self.sm.set('target_hwnd', hwnd)
        self.sm.set('target_window_name', window.get('display_name', ''))
        self.sm.save_settings()
        self._mark_dirty()
        self._emit_summary()
        return True

    def set_capture_desktop(self) -> bool:
        """Switch the persisted capture source back to the desktop."""
        self.cur_mode = 'desktop'
        self.cur_hwnd = 0
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.blockSignals(False)
        self._update_window_visibility()
        self.sm.set('capture_mode', 'desktop')
        self.sm.set('target_hwnd', 0)
        self.sm.set('target_window_name', '')
        self.sm.save_settings()
        self._mark_dirty()
        self._emit_summary()
        return True

    def _mark_dirty(self):
        self._restart_pending = True
        self.restart_btn.setVisible(True)
        self._fit_to_visible_content()

    def _on_restart(self):
        self._restart_pending = False
        self.restart_btn.setEnabled(False)
        self.restart_needed.emit()

    def set_apply_state(self, applying: bool):
        self.restart_btn.setEnabled(not applying)
        if not applying and not self._restart_pending:
            self.restart_btn.setVisible(False)
        self._fit_to_visible_content()

    def _emit_summary(self):
        if self.cur_mode == 'desktop':
            label = 'DESKTOP'
        elif self._window_list:
            idx = self.window_combo.currentIndex()
            if 0 <= idx < len(self._window_list):
                label = self._window_list[idx]['display_name']
            else:
                label = 'WINDOW'
        else:
            label = 'WINDOW'
        self.summary_changed.emit(label)

    def get_summary(self) -> str:
        if self.cur_mode == 'desktop':
            return 'DESKTOP'
        idx = self.window_combo.currentIndex()
        if 0 <= idx < len(self._window_list):
            return self._window_list[idx]['display_name']
        return 'WINDOW'


# Key-capture button — click it, press any key, done.


class GameDetectionPopup(_PopupPanel):
    """Controls for foreground game detection and capture handoff."""

    configuration_changed = Signal(bool, str)
    custom_games_changed = Signal(object)
    summary_changed = Signal(str)

    def __init__(self, settings_manager: SettingsManager,
                 hotkey_manager: HotkeyManager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.hotkey_manager = hotkey_manager
        self.setMinimumWidth(420)
        mode = str(self.sm.get('game_detection_mode', 'auto')).lower()
        self._mode = mode if mode in ('auto', 'prompt') else 'auto'
        self._custom_rules = list(normalise_custom_game_rules(
            self.sm.get('game_detection_custom_games', [])))
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        title = QLabel('Game detection')
        set_theme_style(title, lambda: (_LABEL_STYLE))
        header.addWidget(title)
        header.addStretch()
        self.detection_switch = _ToggleSwitch(
            bool(self.sm.get('game_detection_enabled', False)))
        self.detection_switch.setAccessibleName('Enable game detection')
        self.detection_switch.toggled.connect(self._on_enabled_changed)
        self.enabled_check = self.detection_switch
        header.addWidget(self.detection_switch)
        layout.addLayout(header)

        layout.addWidget(self._section_divider())

        mode_label = QLabel('Switch mode')
        set_theme_style(mode_label, lambda: (_LABEL_STYLE))
        layout.addWidget(mode_label)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        self.auto_switch_btn = self._make_mode_button('AUTO-SWITCH')
        self.prompt_switch_btn = self._make_mode_button('PROMPT FIRST')
        self.auto_switch_btn.setChecked(self._mode == 'auto')
        self.prompt_switch_btn.setChecked(self._mode == 'prompt')
        self.auto_switch_btn.clicked.connect(lambda: self._set_mode('auto'))
        self.prompt_switch_btn.clicked.connect(lambda: self._set_mode('prompt'))
        mode_row.addWidget(self.auto_switch_btn)
        mode_row.addWidget(self.prompt_switch_btn)
        layout.addLayout(mode_row)

        fallback_row = QHBoxLayout()
        fallback_row.setSpacing(12)
        fallback_label = QLabel('Fall back to desktop')
        set_theme_style(fallback_label, lambda: (label_uppercase(
            Colors.TEXT_DIM, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        fallback_label.setToolTip(
            'Only automatic game selections are returned to desktop capture.')
        fallback_row.addWidget(fallback_label)
        fallback_row.addStretch()
        self.fallback_switch = _ToggleSwitch(bool(
            self.sm.get('game_detection_fallback_desktop', False)))
        self.fallback_switch.setAccessibleName(
            'Return to desktop when the detected game closes')
        self.fallback_switch.setToolTip(
            'Capture the desktop when an automatically detected game closes.')
        self.fallback_switch.toggled.connect(self._on_fallback_changed)
        self.fallback_check = self.fallback_switch
        fallback_row.addWidget(self.fallback_switch)
        layout.addLayout(fallback_row)

        layout.addWidget(self._section_divider())

        manual_label = QLabel('Manual games')
        set_theme_style(manual_label, lambda: (_LABEL_STYLE))
        layout.addWidget(manual_label)

        self.running_window_combo = QComboBox()
        set_theme_style(self.running_window_combo, combo_qss)
        layout.addWidget(self.running_window_combo)
        buttons = QHBoxLayout()
        for label, callback in (
                ('Refresh windows', self._refresh_running_windows),
                ('Add manual game', lambda: self._add_running_window(False)),
                ('Block detection', lambda: self._add_running_window(True))):
            button = QPushButton(label)
            set_theme_style(button, lambda: (button_outline_qss()))
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        QTimer.singleShot(0, self._refresh_running_windows)
    
        self.browser_status = QLabel('')
        self.browser_status.setWordWrap(True)
        set_theme_style(self.browser_status, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_MICRO)))
        self.browser_status.setVisible(False)
        layout.addWidget(self.browser_status)

        self._rules_widget = QWidget()
        self._rules_widget.setStyleSheet('background: transparent;')
        self._rules_layout = QVBoxLayout(self._rules_widget)
        self._rules_layout.setContentsMargins(0, 0, 0, 0)
        self._rules_layout.setSpacing(4)
        layout.addWidget(self._rules_widget)
        self._rebuild_rules()

        self._sync_state()

    @staticmethod
    def _section_divider() -> QFrame:
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        set_theme_style(divider, lambda: (f'QFrame {{ color: {Colors.BORDER}; max-height: 1px; }}'))
        return divider

    @staticmethod
    def _field_caption(text: str) -> QLabel:
        caption = QLabel(text)
        caption.setFixedWidth(76)
        set_theme_style(caption, lambda: (label_uppercase(
            Colors.TEXT_DIM, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        return caption

    def _custom_game_input_qss(self) -> str:
        return f'''
            QLineEdit {{
                background-color: {Colors.SURFACE_1};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_SM}px;
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                padding: 6px 8px;
            }}
            QLineEdit:hover {{ border-color: {Colors.BORDER_HI}; }}
            QLineEdit:focus {{ border-color: {Colors.ACCENT}; }}
        '''

    def _make_mode_button(self, text: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setCheckable(True)
        button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        button.setFixedHeight(30)
        button.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        set_theme_style(button, lambda: (self._mode_button_qss()))
        return button

    @staticmethod
    def _mode_button_qss() -> str:
        return f'''
            QToolButton {{
                background-color: {Colors.SURFACE_1};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_LABEL}px;
                font-weight: 600;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                padding: 0 10px;
            }}
            QToolButton:hover {{
                border-color: {Colors.ACCENT};
                color: {Colors.TEXT};
            }}
            QToolButton:focus {{ border-color: {Colors.ACCENT}; }}
            QToolButton:checked {{
                background-color: {Colors.ACCENT_SOFT};
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}
            QToolButton:disabled {{
                background-color: {Colors.SURFACE_1};
                border-color: {Colors.BORDER};
                color: {Colors.TEXT_MUTED};
            }}
        '''

    def refresh_theme(self):
        super().refresh_theme()
        for edit in self.findChildren(QLineEdit):
            set_theme_style(edit, lambda: (self._custom_game_input_qss()))
        for button in self.findChildren(QToolButton):
            if button in (self.auto_switch_btn, self.prompt_switch_btn):
                set_theme_style(button, lambda: (self._mode_button_qss()))
        for button in self.findChildren(QPushButton):
            if button.text() in {'BROWSE', 'FOLDER'}:
                set_theme_style(button, lambda: (button_outline_qss()))
            elif button.text() == 'ADD MANUAL GAME':
                set_theme_style(button, lambda: (button_secondary_qss()))
            else:
                set_theme_style(button, lambda: (button_outline_qss()))
        for label in self.findChildren(QLabel):
            if label.text() in {
                    'GAME DETECTION', 'SWITCH MODE', 'MANUAL GAMES'}:
                set_theme_style(label, lambda: (_LABEL_STYLE))
        self._rebuild_rules()
        self._sync_state()

    def _rebuild_rules(self):
        def clear_layout(layout):
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.hide()
                    widget.deleteLater()
                elif item.layout() is not None:
                    clear_layout(item.layout())
        clear_layout(self._rules_layout)
        if not self._custom_rules:
            empty = QLabel('No manual game rules.')
            set_theme_style(empty, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO)))
            self._rules_layout.addWidget(empty)

        for index, rule in enumerate(self._custom_rules):
            row = QHBoxLayout()
            title = rule.get('title_contains', '')
            exe = rule.get('exe_name', '') or Path(
                rule.get('exe_path', '')).name
            folder = Path(str(rule.get('folder_path', '') or '')).name
            label = title or exe or folder or 'Manual game'
            if title and exe:
                label = f'{title} · {exe}'
            text = QLabel(label)
            set_theme_style(text, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_MICRO)))
            text.setToolTip(str(rule.get('exe_path', '')
                                or rule.get('folder_path', '') or title))
            row.addWidget(text, 1)
            pass
            remove = IconButton('close.png', label='Close')
            remove.setFixedSize(24, 24)
            set_theme_style(remove, lambda: (button_outline_qss()))
            remove.clicked.connect(
                lambda _checked=False, i=index: self._remove_rule(i))
            row.addWidget(remove)
            self._rules_layout.addLayout(row)

        self._rules_layout.addWidget(QLabel('Blocked from game detection'))
        blocked = normalise_custom_game_rules(self.sm.get('game_detection_blacklist', []))
        if not blocked:
            self._rules_layout.addWidget(QLabel('No blocked applications.'))
        for index, rule in enumerate(blocked):
            row = QHBoxLayout()
            row.addWidget(QLabel(str(rule['exe_name'] or rule['title_contains'])), 1)
            remove = IconButton('close.png', label='Remove blocked application')
            remove.setFixedSize(24, 24)
            remove.clicked.connect(lambda _checked=False, i=index: self._remove_blacklist_rule(i))
            row.addWidget(remove)
            self._rules_layout.addLayout(row)

    def _refresh_running_windows(self):
        from ui.capture_settings_widget import _enumerate_capturable_windows
        selected = self.running_window_combo.currentData()
        self.running_window_combo.clear()
        windows = _enumerate_capturable_windows()
        for window in windows:
            self.running_window_combo.addItem(
                f"{window['display_name']} · {window['title']}", window['hwnd'])
        index = self.running_window_combo.findData(selected)
        if index >= 0:
            self.running_window_combo.setCurrentIndex(index)
        if not windows:
            self.running_window_combo.addItem('No running windows available', None)

    def _add_running_window(self, blacklist=False):
        from core.game_detector import get_window_info
        hwnd = self.running_window_combo.currentData()
        window = get_window_info(hwnd) if hwnd else None
        if window is None:
            self._set_browser_status('That window has closed. Refresh the list.', error=True)
            return
        rule = {'exe_path': window.exe_path, 'exe_name': window.exe_name,
                'title_contains': '' if window.exe_path or window.exe_name else window.title}
        rules = list(normalise_custom_game_rules([rule]))
        if not rules:
            return
        if blacklist:
            blocked = list(normalise_custom_game_rules(
                self.sm.get('game_detection_blacklist', [])))
            if rules[0] not in blocked:
                blocked.append(rules[0])
            self.sm.set('game_detection_blacklist', blocked)
            self.sm.save_settings()
            self._rebuild_rules()
            self.custom_games_changed.emit(list(self._custom_rules))
        else:
            if rules[0] not in self._custom_rules:
                self._custom_rules.append(rules[0])
            self._save_rules()
        self._set_browser_status('Detection blocked.' if blacklist else 'Manual game added.')

    def _remove_blacklist_rule(self, index):
        rules = list(normalise_custom_game_rules(self.sm.get('game_detection_blacklist', [])))
        if 0 <= index < len(rules):
            del rules[index]
            self.sm.set('game_detection_blacklist', rules)
            self.sm.save_settings()
            self._rebuild_rules()
            self.custom_games_changed.emit(list(self._custom_rules))

    def _browse_executable(self):
        self._open_deferred_game_picker(folder=False)

    def _browse_game_folder(self):
        self._open_deferred_game_picker(folder=True)

    def _set_browser_status(self, message: str, *, error: bool = False):
        self.browser_status.setText(message)
        set_theme_style(self.browser_status, lambda error=error: (label_body(
            Colors.ERROR if error else Colors.ACCENT,
            Fonts.SIZE_MICRO,
        )))
        self.browser_status.setVisible(bool(message))

    def _open_deferred_game_picker(self, *, folder: bool):
        """Open outside the Qt.Popup grab so the native picker can receive input."""
        self._set_browser_status(
            'Opening folder picker…' if folder else 'Opening executable picker…')
        popup_position = self.pos()
        self.hide()

        def _open():
            parent = self.window()
            if folder:
                selected = QFileDialog.getExistingDirectory(
                    parent, 'Select Game Folder', str(Path.home()))
            else:
                selected, _filter = QFileDialog.getOpenFileName(
                    parent, 'Select Game Executable', str(Path.home()),
                    ('Executables (*.exe)' if sys.platform == 'win32'
                     else 'Applications (*)') + ';;All files (*.*)')
            self.move(popup_position)
            self.show()
            self.raise_()
            if not selected:
                self._set_browser_status('Selection cancelled.')
                return
            error = self._validate_game_selection(selected, folder=folder)
            if error:
                self._set_browser_status(error, error=True)
                return
            rule = {
                'title_contains': '',
                'exe_path': '' if folder else selected,
                'folder_path': selected if folder else '',
                'exe_name': '' if folder else Path(selected).name,
            }
            normalized = normalise_custom_game_rules([rule])
            if not normalized:
                self._set_browser_status(
                    'That selection could not be added.', error=True)
                return
            if normalized[0] in self._custom_rules:
                self._set_browser_status('That game is already in the list.')
                return
            self._custom_rules.append(normalized[0])
            self._save_rules()
            self._set_browser_status(
                f'Added {Path(selected).name}.' + (' An exact crop override can be set below.'
                                                  if sys.platform != 'win32' else ''))

        QTimer.singleShot(0, _open)

    @staticmethod
    def _validate_game_selection(path: str, *, folder: bool) -> str:
        target = Path(path)
        if folder:
            if not target.is_dir():
                return 'The selected game folder no longer exists.'
            checked = 0
            try:
                for root, _dirs, names in os.walk(target):
                    for name in names:
                        checked += 1
                        candidate = Path(root) / name
                        if ((candidate.suffix.lower() == '.exe')
                                or (sys.platform != 'win32'
                                    and os.access(candidate, os.X_OK))):
                            return ''
                        if checked >= 2500:
                            break
                    if checked >= 2500:
                        break
            except OSError as exc:
                return f'Unable to inspect that folder: {exc}'
            return 'No executable was found in the selected game folder.'
        if not target.is_file():
            return 'The selected executable no longer exists.'
        if target.suffix.lower() != '.exe':
            return 'Select a Windows .exe file.'
        pass
        return ''

    def _add_rule(self):
        title = self.title_edit.text().strip()
        exe_path = self.exe_edit.text().strip()
        if exe_path and (Path(exe_path).is_absolute()
                         or '/' in exe_path or '\\' in exe_path):
            error = self._validate_game_selection(exe_path, folder=False)
            if error:
                self._set_browser_status(error, error=True)
                return
        rules = normalise_custom_game_rules([{
            'title_contains': title,
            'exe_path': exe_path,
            'exe_name': Path(exe_path).name if exe_path else '',
        }])
        if not rules:
            self._set_browser_status(
                'Enter a title match, executable name, or browse to a game.',
                error=True)
            return
        rule = rules[0]
        if rule not in self._custom_rules:
            self._custom_rules.append(rule)
            self._save_rules()
            self._set_browser_status('Manual game added.')
        else:
            self._set_browser_status('That game is already in the list.')
        self.title_edit.clear()
        self.exe_edit.clear()

    def _edit_crop(self, index: int):
        if not (0 <= index < len(self._custom_rules)):
            return
        rule = self._custom_rules[index]
        app_window = self.window()
        active_window = getattr(app_window, '_active_game_window', None)
        if isinstance(active_window, GameWindow):
            active_rule = matching_custom_game_rule(
                active_window, self._custom_rules)
            if active_rule != rule:
                active_window = None
        try:
            hwnd = int(getattr(active_window, 'hwnd', 0) or self.sm.get(
                'target_hwnd', 0) or 0)
        except (TypeError, ValueError):
            hwnd = 0
        screen = QApplication.primaryScreen()
        preview = screen.grabWindow(hwnd) if screen is not None and hwnd else QPixmap()
        if preview.isNull():
            self._set_browser_status(
                'Start or select this game first so FTHR can capture a crop preview.',
                error=True)
            return
        label = str(rule.get('title_contains', '')
                    or rule.get('exe_name', '')
                    or Path(str(rule.get('folder_path', ''))).name
                    or 'Game')
        dialog = GameCropDialog(
            label, preview, rule.get('crop_profile'), app_window)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rule['crop_profile'] = dialog.profile()
        self._save_rules()
        enabled = rule['crop_profile'].get('enabled', True)
        self._set_browser_status(
            'Crop profile saved and enabled.' if enabled
            else 'Crop profile saved but disabled.')

    def _remove_rule(self, index: int):
        if 0 <= index < len(self._custom_rules):
            del self._custom_rules[index]
            self._save_rules()

    def _save_rules(self):
        self._custom_rules = list(normalise_custom_game_rules(self._custom_rules))
        self.sm.set('game_detection_custom_games', self._custom_rules)
        self.sm.save_settings()
        self._rebuild_rules()
        self.custom_games_changed.emit(list(self._custom_rules))

    def _on_enabled_changed(self, checked: bool):
        self.sm.set('game_detection_enabled', bool(checked))
        self.sm.save_settings()
        self._sync_state()
        self._emit_summary()
        self.configuration_changed.emit(bool(checked), self._mode)

    def _set_mode(self, mode: str):
        if mode not in ('auto', 'prompt'):
            return
        self._mode = mode
        self.auto_switch_btn.setChecked(mode == 'auto')
        self.prompt_switch_btn.setChecked(mode == 'prompt')
        self.sm.set('game_detection_mode', mode)
        self.sm.save_settings()
        self._sync_state()
        self.configuration_changed.emit(
            self.detection_switch.isChecked(), self._mode)

    def _on_fallback_changed(self, checked: bool):
        self.sm.set('game_detection_fallback_desktop', bool(checked))
        self.sm.save_settings()

    def set_enabled_state(self, checked: bool):
        self.detection_switch.blockSignals(True)
        self.detection_switch.setChecked(bool(checked))
        self.detection_switch.blockSignals(False)
        self._sync_state()
        self._emit_summary()

    def _sync_state(self):
        enabled = self.detection_switch.isChecked()
        for button in (self.auto_switch_btn, self.prompt_switch_btn):
            button.setEnabled(enabled)
        self.fallback_switch.setEnabled(enabled)

    def _emit_summary(self):
        self.summary_changed.emit(
            'Game detection: On' if self.detection_switch.isChecked()
            else 'Game detection: Off')

    def get_summary(self) -> str:
        return (
            'Game detection: On'
            if self.detection_switch.isChecked()
            else 'Game detection: Off'
        )

def _record_button_qss(recording: bool = False, muted: bool = False) -> str:
    border = Colors.ACCENT if recording else Colors.BORDER
    background = Colors.ACCENT_SOFT if recording else Colors.SURFACE_1
    color = Colors.ACCENT if recording else (Colors.TEXT_DIM if muted else Colors.TEXT)
    return f'''
        QPushButton {{
            background-color: {background};
            border: {Sizes.BORDER_W}px solid {border};
            border-radius: {Sizes.RADIUS_MD}px;
            color: {color};
            font-size: {Fonts.SIZE_BODY}px;
            font-family: {Fonts.BODY};
            font-weight: 600;
            padding: 0 8px;
            min-height: 28px;
            text-align: left;
        }}
        QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
        QPushButton:pressed {{ background-color: {Colors.ACCENT_SOFT}; border-color: {Colors.ACCENT}; }}
    '''


def _clear_button_qss() -> str:
    return f'''
        QPushButton {{
            background-color: transparent;
            border: {Sizes.BORDER_W}px solid {Colors.BORDER};
            border-radius: {Sizes.RADIUS_MD}px;
            color: {Colors.TEXT_DIM};
            font-size: {Fonts.SIZE_MICRO}px;
            font-family: {Fonts.DISPLAY};
            letter-spacing: {Fonts.TRACK_LABEL}px;
            font-weight: 600;
            padding: 0 7px;
            min-height: 28px;
        }}
        QPushButton:hover {{ border-color: {Colors.ACCENT}; color: {Colors.ACCENT}; }}
        QPushButton:disabled {{ border-color: {Colors.HAIRLINE}; color: {Colors.TEXT_GHOST}; }}
    '''


def _hotkey_action_frame_qss() -> str:
    return f'''
        QFrame#hotkeyActionRow {{
            background-color: {Colors.SURFACE_1};
            border: {Sizes.BORDER_W}px solid {Colors.BORDER};
            border-radius: {Sizes.RADIUS_MD}px;
        }}
    '''


class _KeyboardBindingButton(QPushButton):
    binding_changed = Signal()

    def __init__(self, hotkey_manager: HotkeyManager, action: str, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.action = action
        self._recording = False
        self._held_keys = set()
        self._recorded_order = []
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(100)
        self.setFixedHeight(30)
        self.clicked.connect(self._toggle_recording)
        self.refresh_value()

    def refresh_value(self):
        if not self._recording:
            value = self.hotkey_manager.get_hotkey(self.action, device='keyboard')
            self.setText(format_keyboard_combo(value))
            set_theme_style(self, lambda value=value: (_record_button_qss(muted=not bool(value))))

    def cancel_recording(self):
        if self._recording:
            self._finish_recording(commit=False)

    def _toggle_recording(self):
        if self._recording:
            self._finish_recording(commit=False)
        else:
            self._recording = True
            self._held_keys.clear()
            self._recorded_order.clear()
            self.hotkey_manager.begin_input_capture()
            self.setText('Press keys')
            set_theme_style(self, lambda: (_record_button_qss(recording=True)))
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.grabKeyboard()

    def _remember_key(self, key_name: str):
        if key_name:
            self._held_keys.add(key_name)
            if key_name not in self._recorded_order:
                self._recorded_order.append(key_name)

    def keyPressEvent(self, event):
        if not self._recording:
            super().keyPressEvent(event)
            return
        if event.isAutoRepeat():
            event.accept()
            return
        for modifier in self.hotkey_manager.qt_modifier_names(event):
            self._remember_key(modifier)
        self._remember_key(self.hotkey_manager.qt_key_name(event))
        preview = normalize_keyboard_combo(self._recorded_order)
        if preview:
            self.setText(format_keyboard_combo(preview))
        event.accept()

    def keyReleaseEvent(self, event):
        if not self._recording:
            super().keyReleaseEvent(event)
            return
        if event.isAutoRepeat():
            event.accept()
            return
        key_name = self.hotkey_manager.qt_key_name(event)
        if key_name:
            self._held_keys.discard(key_name)
        still_down_modifiers = set(self.hotkey_manager.qt_modifier_names(event))
        for modifier in ('Ctrl', 'Alt', 'Shift', 'Win'):
            if modifier not in still_down_modifiers:
                self._held_keys.discard(modifier)
        if not self._held_keys and self._recorded_order:
            QTimer.singleShot(70, lambda: self._finish_recording(commit=True))
        event.accept()

    def focusOutEvent(self, event):
        self.cancel_recording()
        super().focusOutEvent(event)

    def _finish_recording(self, commit: bool):
        if not self._recording:
            return
        combo = normalize_keyboard_combo(self._recorded_order)
        self._recording = False
        self.releaseKeyboard()
        self._held_keys.clear()
        self._recorded_order.clear()
        if commit and combo and self.hotkey_manager.set_hotkey(
                self.action, combo, device='keyboard'):
            self.binding_changed.emit()
        self.hotkey_manager.end_input_capture()
        self.refresh_value()


class _ControllerBindingButton(QPushButton):
    binding_changed = Signal()

    def __init__(self, hotkey_manager: HotkeyManager, action: str, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.action = action
        self._recording = False
        self._recorded_order = []
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(108)
        self.setFixedHeight(30)
        self.setEnabled(sys.platform == 'win32')
        pass
        self.clicked.connect(self._toggle_recording)
        self.refresh_value()

    def refresh_value(self):
        if not self._recording:
            value = self.hotkey_manager.get_controller_hotkey(self.action)
            self.setText(format_controller_combo(value))
            set_theme_style(self, lambda value=value: (_record_button_qss(muted=not bool(value))))

    def cancel_recording(self):
        if self._recording:
            self._finish_recording(commit=False)

    def _toggle_recording(self):
        if self._recording:
            self._finish_recording(commit=False)
            return
        self._recording = True
        self._recorded_order.clear()
        self.hotkey_manager.begin_input_capture()
        self.hotkey_manager.controller_buttons_changed.connect(
            self._on_controller_buttons_changed)
        self.setText('Press buttons')
        set_theme_style(self, lambda: (_record_button_qss(recording=True)))
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    def _on_controller_buttons_changed(self, buttons):
        if not self._recording:
            return
        pressed = set(buttons or [])
        if pressed:
            for button_name in CONTROLLER_BUTTON_ORDER:
                if button_name in pressed and button_name not in self._recorded_order:
                    self._recorded_order.append(button_name)
            for button_name in sorted(pressed):
                if button_name not in self._recorded_order:
                    self._recorded_order.append(button_name)
            preview = normalize_controller_combo(self._recorded_order)
            if preview:
                self.setText(format_controller_combo(preview))
            return
        if self._recorded_order:
            QTimer.singleShot(70, lambda: self._finish_recording(commit=True))

    def _finish_recording(self, commit: bool):
        if not self._recording:
            return
        combo = normalize_controller_combo(self._recorded_order)
        try:
            self.hotkey_manager.controller_buttons_changed.disconnect(
                self._on_controller_buttons_changed)
        except (RuntimeError, TypeError):
            # The popup can be destroyed before its optional disconnect.
            pass
        self._recording = False
        self._recorded_order.clear()
        if commit and combo and self.hotkey_manager.set_hotkey(
                self.action, combo, device='controller'):
            self.binding_changed.emit()
        self.hotkey_manager.end_input_capture()
        self.refresh_value()


class HotkeyPopup(_PopupPanel):
    """Keyboard and controller binding selector ported from the legacy UI."""

    def __init__(self, hotkey_manager: HotkeyManager, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self.setMinimumWidth(550)
        self._binding_buttons = []
        self._keyboard_buttons = {}
        self._keyboard_clear_buttons = {}
        self._controller_buttons = {}
        self._controller_clear_buttons = {}
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 16)
        layout.setSpacing(12)

        label = QLabel('HOTKEYS')
        set_theme_style(label, lambda: (_LABEL_STYLE))
        layout.addWidget(label)
        self.actions_scroll = QScrollArea()
        self.actions_scroll.setWidgetResizable(True)
        self.actions_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.actions_widget = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_widget)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(10)
        self.actions_scroll.setWidget(self.actions_widget)
        layout.addWidget(self.actions_scroll)
        self.reload_actions()

    def reload_actions(self):
        for button in self._binding_buttons:
            button.cancel_recording()
        while self.actions_layout.count():
            item = self.actions_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._binding_buttons.clear()
        self._keyboard_buttons.clear()
        self._keyboard_clear_buttons.clear()
        self._controller_buttons.clear()
        self._controller_clear_buttons.clear()
        actions = [
            ('CAPTURE CLIP', 'save_clip'),
            ('SCREENSHOT', 'save_screenshot'),
            ('CONFIRM GAME', 'confirm_game_detection'),
            ('DISMISS GAME', 'dismiss_game_detection'),
        ]
        if sys.platform == 'win32':
            # The Linux engine has no manual recorder yet.
            actions.extend([
                ('START RECORDING', 'start_recording'),
                ('STOP RECORDING', 'stop_recording'),
            ])
        from core.hotkey_manager import custom_clip_seconds
        custom_actions = sorted(
            (custom_clip_seconds(action), action)
            for action in self.hotkey_manager.hotkeys
            if custom_clip_seconds(action) is not None)
        actions[1:1] = [(f'CLIP {CaptureSettingsPopup._clip_length_label(seconds)}', action)
                        for seconds, action in custom_actions]
        for label_text, action_key in actions:
            self.actions_layout.addWidget(self._create_action_row(label_text, action_key))
        self.actions_scroll.setFixedHeight(min(540, len(actions) * 86))
        self._add_linux_hotkey_notes()
        self.adjustSize()

    def _add_linux_hotkey_notes(self):
        """Explain who owns the keys on Linux, once per popup."""
        if sys.platform == 'win32' or getattr(self, '_linux_notes_added', False):
            return
        self._linux_notes_added = True
        layout = self.layout()
        if getattr(self.hotkey_manager, 'portal_active', False):
            # The desktop owns these keys: pressing a key above only states a
            # preference, and the compositor's answer is written back.
            note = QLabel(
                'Your desktop manages these shortcuts. If it keeps a '
                'different key, change it in the desktop shortcut settings.')
            note.setWordWrap(True)
            note.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_MICRO}px; '
                'padding-top: 6px;')
            layout.addWidget(note)
            desktop_btn = QPushButton('EDIT IN DESKTOP SETTINGS')
            desktop_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            desktop_btn.setStyleSheet(_clear_button_qss())
            desktop_btn.clicked.connect(
                self.hotkey_manager.open_desktop_shortcut_settings)
            layout.addWidget(desktop_btn, 0, Qt.AlignmentFlag.AlignLeft)
            self.hotkey_manager.desktop_bindings_changed.connect(
                self._refresh_binding_values)
            return
        instructions = self.hotkey_manager.setup_instructions(detect_compositor())
        if not instructions:
            return
        instructions_label = QLabel(instructions)
        instructions_label.setWordWrap(True)
        instructions_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        instructions_label.setStyleSheet(
            f'color: {Colors.TEXT_DIM}; font-size: {Fonts.SIZE_MICRO}px; '
            'font-family: monospace; padding-top: 6px;')
        layout.addWidget(instructions_label)

    def _refresh_binding_values(self, *_args):
        for button in self._binding_buttons:
            button.refresh_value()

    def _create_action_row(self, label_text: str, action_key: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName('hotkeyActionRow')
        set_theme_style(frame, lambda: (_hotkey_action_frame_qss()))
        grid = QGridLayout(frame)
        grid.setContentsMargins(12, 10, 12, 10)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        action_label = QLabel(label_text)
        set_theme_style(action_label, lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        action_label.setFixedWidth(108)
        grid.addWidget(action_label, 0, 0, 2, 1)

        keyboard_label = QLabel('Keyboard')
        set_theme_style(keyboard_label, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        grid.addWidget(keyboard_label, 0, 1, 1, 2)
        controller_label = QLabel('Controller')
        set_theme_style(controller_label, lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        grid.addWidget(controller_label, 0, 3, 1, 2)

        keyboard_btn = _KeyboardBindingButton(self.hotkey_manager, action_key)
        controller_btn = _ControllerBindingButton(self.hotkey_manager, action_key)
        keyboard_clear = QPushButton('Clear')
        keyboard_clear.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        keyboard_clear.setFixedSize(46, 30)
        set_theme_style(keyboard_clear, lambda: (_clear_button_qss()))
        keyboard_clear.setAccessibleName(f'Clear keyboard shortcut for {label_text.lower()}')
        keyboard_clear.clicked.connect(
            lambda _checked=False, action=action_key: self._clear_keyboard_bind(action))
        keyboard_btn.binding_changed.connect(
            lambda action=action_key: self._sync_keyboard_clear(action))
        clear_btn = QPushButton('Clear')
        clear_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        clear_btn.setFixedSize(46, 30)
        set_theme_style(clear_btn, lambda: (_clear_button_qss()))
        clear_btn.setAccessibleName(f'Clear controller shortcut for {label_text.lower()}')
        clear_btn.clicked.connect(
            lambda _checked=False, action=action_key: self._clear_controller_bind(action))
        controller_btn.binding_changed.connect(
            lambda action=action_key: self._sync_controller_clear(action))

        self._binding_buttons.extend([keyboard_btn, controller_btn])
        self._keyboard_buttons[action_key] = keyboard_btn
        self._keyboard_clear_buttons[action_key] = keyboard_clear
        self._controller_buttons[action_key] = controller_btn
        self._controller_clear_buttons[action_key] = clear_btn
        grid.addWidget(keyboard_btn, 1, 1)
        grid.addWidget(keyboard_clear, 1, 2)
        grid.addWidget(controller_btn, 1, 3)
        grid.addWidget(clear_btn, 1, 4)
        self._sync_keyboard_clear(action_key)
        self._sync_controller_clear(action_key)
        return frame

    def _clear_controller_bind(self, action_key: str):
        self._controller_buttons[action_key].cancel_recording()
        self.hotkey_manager.set_hotkey(action_key, '', device='controller')
        self._controller_buttons[action_key].refresh_value()
        self._sync_controller_clear(action_key)

    def _clear_keyboard_bind(self, action_key: str):
        self._keyboard_buttons[action_key].cancel_recording()
        self.hotkey_manager.set_hotkey(action_key, '', device='keyboard')
        self._keyboard_buttons[action_key].refresh_value()
        self._sync_keyboard_clear(action_key)

    def _sync_keyboard_clear(self, action_key: str):
        self._keyboard_clear_buttons[action_key].setEnabled(
            bool(self.hotkey_manager.get_hotkey(action_key, device='keyboard')))

    def _sync_controller_clear(self, action_key: str):
        clear_btn = self._controller_clear_buttons[action_key]
        clear_btn.setEnabled(
            sys.platform == 'win32'
            and bool(self.hotkey_manager.get_controller_hotkey(action_key)))

    def refresh_theme(self):
        super().refresh_theme()
        for button in self._binding_buttons:
            button.refresh_value()
        for action_key, button in self._keyboard_clear_buttons.items():
            set_theme_style(button, lambda: (_clear_button_qss()))
            self._sync_keyboard_clear(action_key)
        for action_key, button in self._controller_clear_buttons.items():
            set_theme_style(button, lambda: (_clear_button_qss()))
            self._sync_controller_clear(action_key)
        for frame in self.findChildren(QFrame):
            if frame.objectName() == 'hotkeyActionRow':
                set_theme_style(frame, lambda: (_hotkey_action_frame_qss()))
        for label in self.findChildren(QLabel):
            if label.text() in {'HOTKEYS', 'KEYBOARD', 'CONTROLLER'}:
                set_theme_style(label, lambda label=label: (_LABEL_STYLE if label.text() == 'HOTKEYS'
                    else label_uppercase(
                        Colors.TEXT_DIM, Fonts.SIZE_MICRO,
                        Fonts.TRACK_LABEL)))

    def hideEvent(self, event):
        for button in self._binding_buttons:
            button.cancel_recording()
        super().hideEvent(event)


# TopBarButton — a styled button for the top bar dropdowns

class TopBarButton(ThemedDropdownButton):
    """Readable capture toolbar control with a shared dropdown arrow."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setFixedHeight(Sizes.CAPTURE_CONTROL_H)
        self.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self.refresh_theme()
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def setText(self, text):
        super().setText(text)
        self.setToolTip(str(text))

    def refresh_theme(self):
        set_theme_style(self, lambda: (f'''
            QPushButton {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT}; font-size: {Fonts.SIZE_TOOLBAR}px;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px; font-weight: 400;
                padding: 0px 30px 0px 14px;
                text-align: left;
            }}
            QPushButton:hover {{ color: {Colors.ACCENT}; background-color: {Colors.SURFACE_2}; }}
            QPushButton:pressed {{
                color: {Colors.BG}; background-color: {Colors.ACCENT};
            }}
        '''))
        self.ensurePolished()
        self.updateGeometry()


# Stats strip

class _StatsStrip(QFrame):
    """28px bar below the top bar: encoder type | frame count | buffer fill bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(28)
        self.setObjectName('statsStrip')
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(Sizes.SPACE_6, 0, Sizes.SPACE_6, 0)
        layout.setSpacing(0)

        self._encoder_lbl = QLabel('—')
        self._encoder_lbl.setObjectName('statsEncoder')
        layout.addWidget(self._encoder_lbl)

        layout.addSpacing(10)
        sep = QLabel('·')
        sep.setObjectName('statsSep')
        layout.addWidget(sep)
        layout.addSpacing(10)

        self._frames_lbl = QLabel('0 frames')
        self._frames_lbl.setObjectName('statsFrames')
        layout.addWidget(self._frames_lbl)

        layout.addStretch()

        buf_text = QLabel('Buffer')
        buf_text.setObjectName('statsBufText')
        layout.addWidget(buf_text)

        layout.addSpacing(12)

        # Buffer progress bar with a thin track and themed accent fill.
        bar_wrap = QFrame()
        bar_wrap.setFixedSize(120, 3)
        set_theme_style(bar_wrap, lambda: (f'QFrame {{ background-color: {Colors.BORDER}; border: none; }}'))
        self._buf_fill = QFrame(bar_wrap)
        self._buf_fill.setGeometry(0, 0, 0, 3)
        set_theme_style(self._buf_fill, lambda: (f'QFrame {{ background-color: {Colors.ACCENT}; border: none; }}'))
        layout.addWidget(bar_wrap)

        layout.addSpacing(12)

        self._buf_time_lbl = QLabel('—')
        self._buf_time_lbl.setObjectName('statsBufTime')
        layout.addWidget(self._buf_time_lbl)

        set_theme_style(self, lambda: (f'''
            QFrame#statsStrip {{
                background-color: {Colors.BG};
                border-bottom: 1px solid {Colors.TEXT};
            }}
            QLabel#statsEncoder {{
                color: {Colors.ACCENT};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: 600;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QLabel#statsSep {{
                color: {Colors.TEXT_DIM};
                font-size: {Fonts.SIZE_MICRO}px;
                background: transparent;
            }}
            QLabel#statsFrames {{
                color: {Colors.TEXT};
                font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY};
                background: transparent;
            }}
            QLabel#statsBufText {{
                color: {Colors.TEXT_DIM};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: 600;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QLabel#statsBufTime {{
                color: {Colors.TEXT};
                font-size: {Fonts.SIZE_MICRO}px;
                font-family: {Fonts.BODY};
                background: transparent;
                min-width: 60px;
            }}
        '''))

    def update_stats(self, frames: int, fill_ratio: float, encoder: str,
                     connected: bool, buffer_sec: int):
        if not connected:
            self._encoder_lbl.setText('—')
            self._frames_lbl.setText('disconnected')
            self._buf_fill.setFixedWidth(0)
            self._buf_time_lbl.setText('—')
            return

        self._encoder_lbl.setText(encoder)
        self._frames_lbl.setText(f'{frames:,} frames')

        ratio   = min(max(fill_ratio, 0.0), 1.0)
        fill_px = int(ratio * 120)
        self._buf_fill.setFixedWidth(fill_px)

        filled_sec = int(ratio * buffer_sec)
        self._buf_time_lbl.setText(f'{filled_sec}s / {buffer_sec}s')




class MainWindow(QMainWindow):

    clip_saved = Signal(str)
    _MANUAL_RECORDING_STOP_TIMEOUT_SECONDS = 30.0
    # Cross-thread UI dispatcher. QTimer.singleShot(0, fn) from a plain
    # threading.Thread does NOT fire (no event loop in that thread) — emitting
    # this signal instead is guaranteed to queue fn onto the main thread.
    _ui_call = Signal(object)

    def __init__(self, *, background_start: bool = False):
        super().__init__()
        self._ui_call.connect(lambda fn: fn())
        self._background_start = background_start
        self._ui_ready = False
        self._background_services_started = False
        self._shutdown_requested = False
        self._shutdown_complete = False
        self._shutdown_timer_started = None
        self._tray_icon = None
        self._background_ui_paused = False
        # A freshly shown Wayland surface must paint once before UI updates
        # are paused; otherwise the compositor may never map the window.
        self._first_frame_painted = False
        self._background_ui_pause_deferred = False
        self._pending_status_display: tuple[str, str] | None = None
        self._capture_settings_applying = False
        self._capture_apply_timer = QTimer(self)
        self._capture_apply_timer.setSingleShot(True)
        self._capture_apply_timer.setInterval(30_000)
        self._capture_apply_timer.timeout.connect(self._capture_apply_timed_out)
        self._screenshot_inflight = False
        self._screenshot_pending_requests = 0
        self._screenshot_capture_process = None
        self._screenshot_capture_paths = None
        self._screenshot_save_worker = None
        self._lifecycle_log = get_logger('lifecycle')
        self._diagnostics = get_diagnostic_session()
        self._last_capture_health_event_at = 0.0
        self._last_capture_health_error = None
        self._save_diagnostic_started_at = 0.0
        self._clip_diagnostic_started_by_path: dict[str, float] = {}
        self._diagnostic_engine_start_count = 0
        self._active_clip_viewer = None
        self._active_editor_session = None
        self._editor_sessions = []

        # Frameless window --
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.settings_manager = SettingsManager()
        set_reduce_motion(self.settings_manager.get('reduce_ui_animations', False))
        # Fail early and visibly if the selected library root cannot be
        # created. Never switch to another location behind the user's back.
        clips_root = prepare_clips_storage(
            clips_directory_from(self.settings_manager), probe_writable=True)
        # Windows keyboard visualizers are sampled independently from the
        # native replay engine.  Keeping this service alive with MainWindow
        # means the timestamped source ring continues while the settings page
        # is hidden or the app is sitting in the tray.
        self._keyboard_overlay_capture = ThirdPartyKeyboardCapture()
        self._keyboard_overlay_capture.apply_settings(self.settings_manager)

        from core.clip_metadata_manager import ClipMetadataManager
        self.clip_metadata_manager = ClipMetadataManager()

        from core.upload_manager import UploadManager
        self._clip_readiness = get_clip_readiness_registry()
        self.upload_manager = UploadManager(
            self.settings_manager, self._clip_readiness)
        # Give the settings widget a back-reference so its Save button can call
        # refresh_settings() without needing a direct signal connection.
        self.settings_manager._upload_manager_ref = self.upload_manager

        # Load custom theme colors before any UI is built so QSS uses them
        self._theme_mgr = ThemeManager()
        _theme_colors = self._theme_mgr.get_all_colors()
        for _tk, _val in _theme_colors.items():
            if hasattr(Colors, _tk):
                setattr(Colors, _tk, _val)

        # settings.json is user-editable. Invalid values are rejected and
        # replaced with the documented default, with a diagnostic, rather than
        # silently clamped to a value the user did not select.
        def _validated_setting(key, default, validator):
            try:
                value = int(self.settings_manager.get(key, default))
                return validator(value)
            except (TypeError, ValueError):
                bad = self.settings_manager.get(key, default)
                print(f'[Settings] Rejected invalid {key}={bad!r}; using {default}')
                self.settings_manager.set(key, default)
                return default

        self.clip_duration = _validated_setting(
            'clip_length', 30, validate_normal_clip_length)
        self.capture_fps = _validated_setting(
            'framerate', 60, validate_fps)

        if (not focus_pause_supported(sys.platform)
                and self.settings_manager.get('anticheat_detection_enabled', False)):
            print('[Settings] Focus pause is unavailable on Windows alpha')
            self.settings_manager.set('anticheat_detection_enabled', False)
        self.settings_manager.save_settings()

        saved_res  = self.settings_manager.get('resolution',   'source')
        saved_qual = self.settings_manager.get('bitrate_level', 'medium')
        # Derive the correct kbps from the saved resolution+quality preset so
        # the engine starts with the right bitrate even before any UI interaction
        # fires the bitrate_changed signal.
        if saved_qual == 'custom':
            try:
                saved_custom_bitrate = int(
                    self.settings_manager.get('custom_bitrate_kbps', 25000))
            except (TypeError, ValueError):
                saved_custom_bitrate = 25000
            self.capture_bitrate = max(500, min(200_000, saved_custom_bitrate))
        else:
            self.capture_bitrate = BITRATE_PRESETS.get(
                saved_res, BITRATE_PRESETS['source']).get(saved_qual, 25000)

        self.capture_width, self.capture_height = _resolution_to_dims(saved_res)
        self.buffer_seconds = compute_buffer_seconds(
            self.clip_duration, self.settings_manager.get('custom_clip_lengths', []))
        self._capture_config = CaptureConfigTracker()

        self.engine_process = None
        self._engine_gen = 0
        self._engine_process_generation = None
        self._recoverable_error_incidents = {}
        self._error_recovery_tracker = RecoveryTracker()
        self._engine_connect_thread: threading.Thread | None = None
        self._engine_startup_output = None
        self.bridge         = CaptureBridge()
        self._engine_profile = 'clips'
        self._manual_record_state = 'idle'
        self._pending_game_geometry_restart = False
        self._pending_manual_record_path: Path | None = None
        self._manual_record_requested_at = 0.0
        self._manual_record_started_at = 0.0
        self._manual_record_path: Path | None = None
        self._manual_record_finalize_thread: threading.Thread | None = None
        self._manual_record_timer = QTimer(self)
        self._manual_record_timer.setInterval(200)
        self._manual_record_timer.timeout.connect(
            self._pump_manual_recording_responses)
        self._capture_health = CaptureHealthMonitor()
        self._capture_health_snapshot = None
        self._capture_health_log = get_logger('capture.health')

        if sys.platform != 'win32':
            _env_engine = os.environ.get('FTHR_ENGINE', '')
            if getattr(sys, 'frozen', False):
                # In frozen PyInstaller build the engine is in _internal/ (_MEIPASS)
                possible_paths = [Path(sys._MEIPASS) / 'FTHRclips']
            else:
                linux_root = Path(__file__).parent.parent / 'FTHRcapture_linux'
                possible_paths = [
                    *([Path(_env_engine)] if _env_engine else []),
                    linux_root / 'build' / 'FTHRclips',
                ]
        elif getattr(sys, 'frozen', False):
            possible_paths = [Path(sys._MEIPASS) / 'engine' / 'FTHRClips.exe']
        else:
            project_root   = Path(__file__).parent.parent / 'FTHRcapture'
            possible_paths = [
                # Direct project builds are useful while the running
                # solution-level engine is locked. Prefer the freshly
                # compiled source-checkout artifact; frozen releases still
                # use only their bundled _MEIPASS engine above.
                project_root / 'FTHRclips' / 'x64' / 'CFRReleasePause' / 'FTHRclips.exe',
                project_root / 'FTHRclips' / 'x64' / 'CFRReleaseFinal' / 'FTHRclips.exe',
                project_root / 'FTHRclips' / 'x64' / 'CFRRelease' / 'FTHRclips.exe',
                project_root / 'FTHRclips' / 'x64' / 'Release' / 'FTHRclips.exe',
                project_root / 'FTHRclips' / 'x64' / 'Debug'   / 'FTHRclips.exe',
                project_root / 'x64' / 'Release' / 'FTHRClips.exe',
                project_root / 'x64' / 'Debug'   / 'FTHRClips.exe',
                project_root / 'Release'          / 'FTHRClips.exe',
                project_root / 'Debug'            / 'FTHRClips.exe',
            ]
        self.engine_path = None
        existing_engines = [p for p in possible_paths if p.is_file()]
        if existing_engines:
            try:
                self.engine_path = max(
                    existing_engines, key=lambda path: path.stat().st_mtime)
            except OSError:
                self.engine_path = existing_engines[0]
            print(f"Engine found: {self.engine_path.resolve()}")
        if not self.engine_path:
            print("Engine not found.")

        # A hard kill can leave the engine's same-directory transaction file.
        # Only old, FTHR-named partials are removed; fresh files may belong to a
        # still-running save and unrelated *.mp4.partial files are user-owned.
        # Recovery walks can cover an entire user-selected drive, so they must
        # not block construction of the Qt window.
        def _report_partial_recovery(result):
            if result.removed:
                print(f'[Startup] Removed {len(result.removed)} stale partial clip(s)')
            for partial_path, error in result.failures:
                print(f'[Startup] Could not remove stale partial '
                      f'{partial_path.name}: {error}')

        (self._partial_cleanup_thread,
         self._partial_cleanup_cancel) = start_stale_partial_cleanup(
             clips_root, on_complete=_report_partial_recovery)

        self.hotkey_manager = HotkeyManager()
        self.hotkey_manager.configure_custom_clips(
            self.settings_manager.get('custom_clip_lengths', []), register=False)

        self._pending_game_window: GameWindow | dict | None = None
        self._active_game_window: GameWindow | None = None
        self._active_game_hwnd:    int  | None = None
        self._auto_capture_hwnd = 0
        self._startup_game_detection = bool(
            self.settings_manager.get('game_detection_enabled', False))
        if self._startup_game_detection:
            # Never let a window target persisted by a previous automatic
            # handoff decide the first capture generation. Start on the
            # desktop, then let a stable real-game candidate take over.
            self._prepare_startup_capture_source()
        self._game_dismiss_timer = QTimer(self)
        self._game_dismiss_timer.setSingleShot(True)
        self._game_dismiss_timer.timeout.connect(self._on_game_prompt_timeout)
        self._fallback_watch_timer = QTimer(self)
        self._fallback_watch_timer.setInterval(1000)
        self._fallback_watch_timer.timeout.connect(self._on_game_lost)

        self._game_detector = ForegroundGameDetector(
            self,
            custom_game_rules=self.settings_manager.get(
                'game_detection_custom_games', []),
            blacklist_rules=self.settings_manager.get('game_detection_blacklist', []),
        )
        self._game_detector.game_detected.connect(self._on_game_detected)
        self._game_detector.game_lost.connect(self._on_game_lost)

        self._focus_monitor = FocusMonitor()
        self._focus_monitor.focus_lost.connect(self._on_focus_lost)
        self._focus_monitor.focus_regained.connect(self._on_focus_regained)

        if (focus_pause_supported(sys.platform)
                and self.settings_manager.get('anticheat_detection_enabled', False)
                and self.settings_manager.get('capture_mode', 'desktop') == 'window'):
            target = self.settings_manager.get('target_window_name', '')
            self._focus_monitor.set_target(target)
            self._focus_monitor.start()

        from core.camera_recorder import CameraRecorder
        if (CameraRecorder.is_available()
                and self.settings_manager.get('camera_enabled', False)):
            device_idx = self.settings_manager.get('camera_device_index', 0)
            # Async: opening a camera blocks for seconds on Windows/MSMF —
            # doing it inline froze the whole UI during startup.
            def _on_cam_result(ok):
                if not ok:
                    self._ui_call.emit(lambda: self._show_camera_unavailable(
                        'Could not open the selected camera. Confirm it is '
                        'connected and not in use by another app.'))
            CameraRecorder().start_async(device_idx, on_result=_on_cam_result)

        self.is_capturing    = True
        self.capture_card    = CaptureCardClient(self.settings_manager)
        self._encoder_type   = 'DETECTING'
        # Window drag state
        self._drag_pos: QPoint | None = None

        self.setWindowTitle(f'{APP_NAME} {APP_VERSION}')
        self.setMinimumSize(1100, 720)
        self._desired_maximized = True
        self._restore_window_geometry()

        if not self._background_start:
            self.ensure_main_ui()
        self._setup_hotkeys()
        self._start_background_services()
        self._create_system_tray()
        if not self._background_start:
            QTimer.singleShot(800, self.capture_card.play_startup)

        # Start Python microphone capture where needed; Windows uses native WASAPI.
        if self.settings_manager.get('audio_capture_enabled', True):
            self._start_mic_recorder()

        # Launch the capture engine once the event loop is running. Deferring
        # past __init__ keeps the window responsive while the engine boots and
        # the bridge polls for its shared memory. Once running, the status timer
        # below handles transparent reconnection if the engine ever dies.
        QTimer.singleShot(0, self.start_engine)
        if self._startup_game_detection:
            # Register after start_engine so the first capture generation is
            # always desktop capture, even when a stale window target existed.
            QTimer.singleShot(0, self._start_game_detection)

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(500)

        # Poll saves every 50 ms while a request is outstanding; the 500 ms status
        # timer is too coarse for the one-second acknowledgement deadline. Each
        # tick reads shared memory without blocking Qt.
        self._save_state = SaveStateMachine()
        self._save_poll_timer = QTimer(self)
        self._save_poll_timer.setInterval(50)
        self._save_poll_timer.timeout.connect(self._on_save_poll_tick)
        self._published_final_clips: set[str] = set()

        app = QApplication.instance()
        if app is not None:
            app.applicationStateChanged.connect(
                self._on_application_state_changed)
        QTimer.singleShot(0, self._refresh_background_ui_pause_state)

    def _restore_window_geometry(self) -> None:
        """Restore logical geometry before the first show, if it is valid."""
        pass
        saved = self.settings_manager.get('main_window_geometry', {})
        app = QApplication.instance()
        screens = app.screens() if app is not None else []
        primary = app.primaryScreen() if app is not None else None
        rect, maximized, screen = restore_window_geometry(
            saved, screens, primary, default_size=(1100, 720),
            default_maximized=True)
        self.setGeometry(rect)
        self._desired_maximized = bool(maximized)
        if screen is not None:
            self._restored_screen_name = str(screen.name() or '')
        else:
            self._restored_screen_name = ''
        if saved:
            emit_event(
                'ui', 'window_geometry_restored',
                screen=self._restored_screen_name,
                geometry={
                    'x': rect.x(), 'y': rect.y(),
                    'width': rect.width(), 'height': rect.height(),
                }, maximized=self._desired_maximized)

    def _persist_window_geometry(self) -> None:
        """Persist normalGeometry even when the window is currently maximized."""
        pass
        try:
            payload = save_window_geometry(self)
            self.settings_manager.set('main_window_geometry', payload)
            self.settings_manager.save_settings()
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
            emit_event('ui', 'window_geometry_persist_failed',
                       detail=f'{type(error).__name__}: {error}')

    def _on_window_screen_changed(self, screen) -> None:
        """Record a bounded DPI/screen transition and recover off-screen state."""
        name = str(screen.name() or '') if screen is not None else ''
        app = QApplication.instance()
        screens = list(app.screens()) if app is not None else []
        normal = self.normalGeometry()
        recovered = False
        if screens and not is_rect_visible(normal, screens):
            payload = save_window_geometry(self)
            payload['screen_name'] = name
            rect, _maximized, _screen = restore_window_geometry(
                payload, screens, app.primaryScreen(),
                default_size=(1100, 720), default_maximized=False)
            self.setGeometry(rect)
            recovered = True
        emit_event(
            'ui', 'window_screen_changed', screen=name,
            displays=qt_display_snapshot(QApplication.instance(), self),
            dpi_awareness=dpi_awareness_snapshot(self),
            geometry_recovered=recovered,
            recovery_reason=('window_outside_current_work_areas'
                             if recovered else 'not_required'))

    def _connect_window_screen_signals(self) -> None:
        handle = self.windowHandle()
        if handle is None:
            return
        try:
            handle.screenChanged.connect(self._on_window_screen_changed)
            self._on_window_screen_changed(handle.screen())
        except (AttributeError, RuntimeError, TypeError):
            # Background startup or teardown may temporarily expose no native
            # handle; the first later show retries the normal setup path.
            pass

    def ensure_main_ui(self) -> None:
        """Build library and settings widgets on first window use.

        Capture, hotkeys, uploads, and the tray run independently during
        a --background launch.
        """
        if self._ui_ready:
            return
        self._setup_ui()
        self._settings_page_widget.gary_settings_changed.connect(
            self._sync_gary_settings)
        self._init_gary_mode()
        self._load_saved_theme()
        self._apply_styles()
        self._ui_ready = True
        self._setup_keyboard_navigation()
        # A tray-started window can build its UI after background suspension
        # was already entered. Force the new widgets into the current state.
        self._apply_background_ui_paused(
            self._background_ui_paused, force=True)
        is_connected = getattr(self.bridge, 'is_connected', lambda: False)
        if self.bridge and is_connected():
            QTimer.singleShot(
                0, self._settings_page_widget._populate_mic_devices)

    def _init_gary_mode(self):
        default_image = Path(__file__).parent / 'assets' / 'gary.png'
        self._gary_overlay = GaryOverlay(default_image, anchor=self)
        self._gary_current_intensity = 0.0
        self._gary_enabled = False
        self._gary_min_level = DEFAULT_MIN_LEVEL
        self._gary_max_level = DEFAULT_MAX_LEVEL
        self._gary_recorder_active = False
        self._sync_gary_settings()
        self._gary_timer = QTimer(self)
        self._gary_timer.setInterval(33)
        self._gary_timer.timeout.connect(self._update_gary_mode)
        self._gary_timer.start()

    def _start_background_services(self) -> None:
        """Start services that must survive hiding or deferred UI creation."""
        if self._background_services_started:
            return
        self.upload_manager.upload_finished.connect(self._on_upload_finished)
        self.upload_manager.upload_error.connect(self._on_upload_error)
        self.upload_manager.compression_required.connect(
            self._on_upload_compression_required)
        self.upload_manager.compression_started.connect(
            lambda _path, provider: self._set_status(
                f'COMPRESSING FOR {provider.upper()}', status_idle_qss()))
        self.upload_manager.compression_progress.connect(
            self._on_upload_compression_progress)
        self.upload_manager.start()
        self._background_services_started = True

    def _create_system_tray(self) -> bool:
        """Create one native Windows tray icon for the process lifetime."""
        if self._tray_icon is not None:
            return True
        if (sys.platform != 'win32'
                or not QSystemTrayIcon.isSystemTrayAvailable()):
            print('[Lifecycle] System tray unavailable; normal close remains enabled')
            return False

        icon = _load_icon('favicon.ico', 32)
        if icon.isNull():
            icon = self.windowIcon()
        if icon.isNull():
            print('[Lifecycle] System tray unavailable: FTHR icon could not load')
            return False

        tray = QSystemTrayIcon(icon, self)
        tray.setToolTip('FTHR Clips — replay capture is running')
        menu = QMenu()
        open_action = QAction('Open FTHR', menu)
        open_action.triggered.connect(self.restore_main_window)
        save_action = QAction('Save Clip', menu)
        save_action.triggered.connect(self._on_hotkey_save_clip)
        library_action = QAction('Open Clips', menu)
        library_action.triggered.connect(self._open_clip_library)
        exit_action = QAction('Exit FTHR', menu)
        exit_action.triggered.connect(self.request_full_exit)
        menu.addAction(open_action)
        menu.addAction(save_action)
        menu.addAction(library_action)
        menu.addSeparator()
        menu.addAction(exit_action)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray_icon = tray
        print('[Lifecycle] TrayReady')
        return True

    def _on_tray_activated(self, reason) -> None:
        if reason in {
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick}:
            self.restore_main_window()

    def restore_main_window(self) -> None:
        """Restore the existing UI without restarting the capture generation."""
        self.capture_card.hide_background_capture()
        self.ensure_main_ui()
        self.setWindowState(
            self.windowState() & ~Qt.WindowState.WindowMinimized)
        if (self.isMaximized()
                or (self._background_start
                    and getattr(self, '_desired_maximized', True))):
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()
        self._background_start = False
        QTimer.singleShot(0, self._refresh_background_ui_pause_state)
        print('[Lifecycle] WindowRestored')

    def _on_close_to_tray_changed(self, enabled: bool) -> None:
        """Keep the Windows title-bar affordance aligned with close behavior."""
        if hasattr(self, 'power_btn'):
            self.power_btn.setVisible(sys.platform == 'win32' and bool(enabled))

    def _on_error_notifications_changed(self, enabled: bool) -> None:
        """Apply the bottom-bar preference immediately, including open errors."""
        if not enabled and hasattr(self, 'error_bar'):
            self.error_bar.clear()

    def _export_diagnostic_report(self) -> None:
        """Let the user explicitly create a bounded offline support bundle."""
        session = self._diagnostics or get_diagnostic_session()
        if session is None:
            FthrMessageDialog.warning(
                self, 'Diagnostic report unavailable',
                'This FTHR session did not initialize diagnostics. Restart FTHR '
                'and reproduce the problem before exporting a report.')
            return
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        suggested = (Path.home() / 'Desktop' /
                     f'FTHR-Clips-Diagnostics-{timestamp}-{session.short_id}.zip')
        output, _filter = QFileDialog.getSaveFileName(
            self, 'Export Diagnostic Report', str(suggested),
            'ZIP archive (*.zip)')
        if not output:
            emit_event('diagnostics', 'report_export_cancelled', state='CANCELLED')
            return
        try:
            exported = session.export_zip(
                Path(output), displays=qt_display_snapshot(QApplication.instance()))
        except Exception as error:
            emit_event(
                'diagnostics', 'report_export_failed', state='FAILED',
                detail=f'{type(error).__name__}: {error}')
            FthrMessageDialog.warning(
                self, 'Diagnostic export failed',
                'FTHR could not create the report. Choose another writable '
                f'folder and try again.\n\n{type(error).__name__}: {error}')
            return
        from ui.diagnostic_report import DiagnosticReportDialog
        self._settings_page_widget.diagnostic_report_file.set_file(exported)
        DiagnosticReportDialog(exported, self).exec()

    def _current_capture_source_label(self) -> str:
        if self.settings_manager.get('capture_mode', 'desktop') != 'window':
            return 'Desktop'
        name = str(self.settings_manager.get('target_window_name', '')).strip()
        return name or 'Window / Game'

    def _hide_main_window_to_tray(self) -> None:
        """Hide only the main UI and confirm that replay capture continues."""
        self.hide()
        self._refresh_background_ui_pause_state()
        source = self._current_capture_source_label()
        self.capture_card.show_background_capture(source)
        print(f'[Lifecycle] WindowHiddenToTray source={source!r}')
        self._lifecycle_log.info('WindowHiddenToTray source=%r', source)

    def _open_clip_library(self) -> None:
        self.restore_main_window()
        self.main_stack.setCurrentIndex(0)

    # UI layout

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Compact draggable title strip, separate from capture controls.
        top_bar = QFrame()
        top_bar.setObjectName('topBar')
        top_bar.setFixedHeight(Sizes.TITLE_BAR_H)
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(16, 0, 0, 0)
        tb.setSpacing(Sizes.ROW_GAP)

        # Logo — check theme override first, then fall back to default asset.
        # Default asset is black-on-white so we invert RGB for the dark bar.
        # Custom logos are used as-is (user provides the final look). --
        self._logo_label = QLabel()
        self._load_logo()
        self._logo_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tb.addWidget(self._logo_label)
        brand = QLabel('FTHR Clips')
        set_theme_style(brand, lambda: (f'color: {Colors.TEXT}; font-family: {Fonts.BRAND}; '
            f'font-size: {Fonts.SIZE_BODY_L}px; font-weight: 700; background: transparent;'))
        tb.addWidget(brand)
        tb.addStretch(1)

        # One always-visible capture indicator. It is intentionally just text:
        # no badge, box, or animation competing with the controls beside it.
        self.status_label = QLabel('Starting capture')
        set_theme_style(self.status_label, lambda: (status_idle_qss()))
        self.status_label.setObjectName('statusLabel')
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight |
                                       Qt.AlignmentFlag.AlignVCenter)
        self.status_label.setMinimumWidth(110)

        # Main-mode cluster: capture-settings dropdowns + gear --
        self.main_mode_cluster = QFrame()
        self.main_mode_cluster.setObjectName('topClusterMain')
        mc = QHBoxLayout(self.main_mode_cluster)
        mc.setContentsMargins(14, 6, 14, 6)
        mc.setSpacing(8)

        self.cap_settings_popup = CaptureSettingsPopup(self.settings_manager, self)
        self.cap_settings_popup.clip_length_changed.connect(self._on_clip_length_changed)
        self.cap_settings_popup.custom_lengths_changed.connect(self._on_custom_lengths_changed)
        self.cap_settings_popup.framerate_changed.connect(self._on_framerate_changed)
        self.cap_settings_popup.resolution_changed.connect(self._on_resolution_changed)
        self.cap_settings_popup.bitrate_changed.connect(self._on_bitrate_changed)
        self.cap_settings_popup.restart_needed.connect(self._restart_capture_engine)
        self.cap_settings_popup.summary_changed.connect(self._on_cap_summary_changed)

        self.cap_btn = TopBarButton(self.cap_settings_popup.get_summary())
        self.cap_btn.clicked.connect(self._toggle_cap_settings)
        mc.addWidget(self.cap_btn, 3)

        self.source_popup = SourcePopup(self.settings_manager, self)
        self.source_popup.restart_needed.connect(self._restart_capture_engine)
        self.source_btn = TopBarButton('Source')
        self.source_btn.clicked.connect(self._toggle_source)
        mc.addWidget(self.source_btn, 2)

        self.game_detection_popup = GameDetectionPopup(
            self.settings_manager, self.hotkey_manager, self)
        self.game_detection_popup.configuration_changed.connect(
            self._on_game_detection_configuration_changed)
        self.game_detection_popup.custom_games_changed.connect(
            self._on_custom_game_rules_changed)
        self.game_detection_popup.summary_changed.connect(
            self._on_game_detection_summary_changed)
        self.game_detection_btn = TopBarButton(
            self.game_detection_popup.get_summary())
        self.game_detection_btn.clicked.connect(self._toggle_game_detection)
        mc.addWidget(self.game_detection_btn, 2)

        self.record_btn = QPushButton('Record')
        self.record_btn.setFixedHeight(Sizes.CAPTURE_CONTROL_H)
        self.record_btn.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self.record_btn.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor))
        self.record_btn.setToolTip(
            'Start a manual recording while replay capture stays active')
        set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(False)))
        self.record_btn.clicked.connect(self._toggle_manual_recording)
        if sys.platform != 'win32':
            self.record_btn.setEnabled(False)
            self.record_btn.setToolTip(
                'Manual recording currently requires the Windows capture engine')
        mc.insertWidget(0, self.record_btn, 1)

        self.hotkey_popup = HotkeyPopup(self.hotkey_manager, self)
        self.hotkey_btn = TopBarButton('Hotkeys')
        self.hotkey_btn.clicked.connect(self._toggle_hotkeys)
        mc.addWidget(self.hotkey_btn, 1)

        for popup in (
                self.cap_settings_popup,
                self.source_popup,
                self.game_detection_popup,
                self.hotkey_popup):
            popup.popup_hidden.connect(self._sync_topbar_dropdown_arrows)

        # NVENC / HW status label (hidden by default)
        self.hw_label = QLabel()
        self.hw_label.setObjectName('hwLabel')
        self.hw_label.setVisible(False)
        mc.addWidget(self.hw_label)

        self.settings_gear_btn = QToolButton()
        self.settings_gear_btn.setText('Settings')
        self.settings_gear_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.settings_gear_btn.setCheckable(True)
        _gear_ico = _load_icon('settings(general).png', 18)
        self.settings_gear_btn.setIcon(_gear_ico)
        self.settings_gear_btn.setIconSize(QSize(18, 18))
        _register_icon_widget(self.settings_gear_btn, 'settings(general).png', 18)
        self.settings_gear_btn.setObjectName('settingsGearBtn')
        self.settings_gear_btn.setFixedSize(78, 78)
        self.settings_gear_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_gear_btn.clicked.connect(lambda: self._set_main_page(1))
        mc.addWidget(self.status_label)
        self.main_mode_cluster.setMinimumHeight(Sizes.CAPTURE_CONTROL_H + 12)

        # Persistent navigation destinations are attached to the left panel below.
        self.close_settings_btn = QToolButton()
        self.close_settings_btn.setText('Clips')
        self.close_settings_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.close_settings_btn.setCheckable(True)
        self.close_settings_btn.setChecked(True)
        self.close_settings_btn.setObjectName('homeBtn')
        self.close_settings_btn.setFixedSize(78, 78)
        _home_ico = _load_icon('home.png', 18)
        if not _home_ico.isNull():
            self.close_settings_btn.setIcon(_home_ico)
            self.close_settings_btn.setIconSize(QSize(18, 18))
            _register_icon_widget(self.close_settings_btn, 'home.png', 18)
        else:
            self.close_settings_btn.setText('Home')
        self.close_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_settings_btn.setToolTip('Back to clips')
        self.close_settings_btn.clicked.connect(lambda: self._set_main_page(0))

        # Window controls (always visible) --
        self.kofi_widget = QPushButton('Help us keep the app free and open for everyone')
        self.kofi_widget.setIcon(QIcon(str(Path(__file__).parent / 'assets' / 'icons' / 'kofi_symbol.png')))
        self.kofi_widget.setIconSize(QSize(26, 22))
        self.kofi_widget.setToolTip('Support FTHRClips on Ko-fi')
        self.kofi_widget.setAccessibleName('Help us keep the app free and open for everyone — support on Ko-fi')
        self.kofi_widget.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(self.kofi_widget, button_outline_qss)
        self.kofi_widget.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl('https://ko-fi.com/fthrclips')))
        self.kofi_widget.setVisible(bool(self.settings_manager.get('show_kofi_widget', True)))
        tb.addWidget(self.kofi_widget)

        # Windows gets a dedicated full-shutdown control while X is configured
        # to hide the app to the notification area.
        self.power_btn = QPushButton()
        self.power_btn.setObjectName('powerBtn')
        self.power_btn.setFixedSize(46, Sizes.UNIFIED_BAR_H)
        _shutdown_ico = _load_icon('shutdown.png', 18)
        if not _shutdown_ico.isNull():
            self.power_btn.setIcon(_shutdown_ico)
            self.power_btn.setIconSize(QSize(18, 18))
            _register_icon_widget(self.power_btn, 'shutdown.png', 18)
        else:
            self.power_btn.setIcon(_load_icon('shutdown.png', 18))
        self.power_btn.setIconSize(QSize(18, 18))
        self.power_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.power_btn.setToolTip('Shut down FTHR Clips and stop capture')
        self.power_btn.setAccessibleName(
            'Shut down FTHR Clips and stop background capture')
        self.power_btn.clicked.connect(self.request_full_exit)
        self.power_btn.setVisible(
            sys.platform == 'win32'
            and bool(self.settings_manager.get('close_to_tray', True)))
        tb.addWidget(self.power_btn)

        self.min_btn = QPushButton()
        self.min_btn.setObjectName('winBtn')
        self.min_btn.setFixedSize(46, Sizes.UNIFIED_BAR_H)
        _min_ico = _load_icon('minimize.png', 14)
        if not _min_ico.isNull():
            self.min_btn.setIcon(_min_ico)
            self.min_btn.setIconSize(QSize(14, 14))
            _register_icon_widget(self.min_btn, 'minimize.png', 14)
        else:
            self.min_btn.setText('Minimize')
        self.min_btn.clicked.connect(self.showMinimized)
        tb.addWidget(self.min_btn)

        self._is_maximized = False
        self.max_btn = QPushButton()
        self.max_btn.setObjectName('winBtn')
        self.max_btn.setFixedSize(46, Sizes.UNIFIED_BAR_H)
        _max_ico = _load_icon('maximize.png', 14)
        if not _max_ico.isNull():
            self.max_btn.setIcon(_max_ico)
            self.max_btn.setIconSize(QSize(14, 14))
            _register_icon_widget(self.max_btn, 'maximize.png', 14)
        else:
            self.max_btn.setText('Maximize')
        self.max_btn.clicked.connect(self._toggle_maximize)
        tb.addWidget(self.max_btn)

        self.close_btn = QPushButton()
        self.close_btn.setObjectName('closeBtn')
        self.close_btn.setFixedSize(46, Sizes.UNIFIED_BAR_H)
        _close_ico = _load_icon('close.png', 14)
        if not _close_ico.isNull():
            self.close_btn.setIcon(_close_ico)
            self.close_btn.setIconSize(QSize(14, 14))
            _register_icon_widget(self.close_btn, 'close.png', 14)
        else:
            self.close_btn.setText('Close')
        self.close_btn.clicked.connect(self.close)
        tb.addWidget(self.close_btn)

        root.addWidget(top_bar)
        # Small windows scroll the toolbar horizontally instead of clipping
        # labels or shrinking their type. Large windows retain the single row.
        self.capture_toolbar_scroll = QScrollArea()
        self.capture_toolbar_scroll.setObjectName('captureToolbarScroll')
        self.capture_toolbar_scroll.setWidgetResizable(True)
        self.capture_toolbar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.capture_toolbar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.capture_toolbar_scroll.setFixedHeight(Sizes.CAPTURE_BAR_H)
        self.capture_toolbar_scroll.setWidget(self.main_mode_cluster)
        root.addWidget(self.capture_toolbar_scroll)

        # Drag/double-click on the bar background (the buttons absorb their own clicks)
        top_bar.mousePressEvent       = self._bar_mouse_press
        top_bar.mouseMoveEvent        = self._bar_mouse_move
        top_bar.mouseReleaseEvent     = self._bar_mouse_release
        top_bar.mouseDoubleClickEvent = self._bar_double_click
        self._top_bar = top_bar

        # Main content stack --
        self.main_stack = ShellPageStack()
        self.main_stack.setObjectName('mainStack')

        # Page 0: clip grid
        body_page = QWidget()
        body_page.setObjectName('body')
        body_layout = QVBoxLayout(body_page)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        scroll = SmoothScrollArea()
        self.clip_scroll_area = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.clip_grid = ClipGrid(settings_manager=self.settings_manager,
                                  metadata_manager=self.clip_metadata_manager)
        self.clip_grid.set_readiness_checker(self._clip_readiness.can_access)
        self.clip_grid.clip_opened.connect(self._on_clip_opened)
        self.clip_grid.screenshot_clicked.connect(_show_in_file_manager)
        scroll.setWidget(self.clip_grid)
        body_layout.addWidget(scroll, stretch=1)

        self.main_stack.addWidget(body_page)

        # Page 1: full-screen settings
        self._settings_page_widget = _SettingsPage(
            self.settings_manager,
            keyboard_capture=self._keyboard_overlay_capture,
        )
        self._settings_page_widget.camera_unavailable.connect(
            self._show_camera_unavailable)
        self._settings_page_widget.close_requested.connect(
            self._toggle_settings_page)
        self._settings_page_widget.clips_directory_changed.connect(
            lambda path: self.clip_grid.set_clips_directory(
                path, prepared=True))
        self._settings_page_widget.imported_folders_changed.connect(
            self.clip_grid.force_refresh)
        self._settings_page_widget.notification_monitor_changed.connect(
            self.capture_card.restart)
        self._settings_page_widget.error_notifications_changed.connect(
            self._on_error_notifications_changed)
        self._settings_page_widget.upload_connection_failed.connect(
            self._on_upload_connection_failed)
        self._settings_page_widget.close_to_tray_changed.connect(
            self._on_close_to_tray_changed)
        self._settings_page_widget.diagnostic_export_requested.connect(
            self._export_diagnostic_report)
        self._settings_page_widget.encoder_config_changed.connect(
            self._on_encoder_config_changed)
        self._settings_page_widget.audio_capture_changed.connect(
            self._on_audio_capture_changed)
        self._settings_page_widget.audio_capture_mode_changed.connect(
            self._on_audio_capture_mode_changed)
        self._settings_page_widget.capture_card_changed.connect(
            self._on_capture_card_changed)
        self._settings_page_widget.background_ui_pause_changed.connect(
            self._on_background_ui_pause_changed)
        self.main_stack.addWidget(self._settings_page_widget)

        # The manager itself starts before the optional UI exists, so background
        # replay and uploads do not depend on this screen.  These bindings are
        # the view-specific half and are created only with the library grid.
        self.clip_grid.clip_upload_requested.connect(self.upload_manager.enqueue_upload)
        self.upload_manager.upload_started.connect(
            lambda path: self.clip_grid.set_upload_status(path, 'Uploading…'))
        self.clip_grid.set_upload_checker(self.upload_manager.is_uploaded)
        self.clip_grid.set_upload_info_checker(self.upload_manager.get_upload_info)
        self.clip_grid.set_upload_enabled_checker(
            self.upload_manager.is_enabled)
        self.upload_manager.plugin_state_changed.connect(
            lambda _installed, _enabled:
            self.clip_grid.set_upload_enabled_checker(self.upload_manager.is_enabled))

        # Both destinations stay instantiated. Navigation only changes the
        # visible page; transitions paint snapshots without moving live trees.
        shell = QWidget()
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)
        self.nav_panel = QFrame()
        self.nav_panel.setObjectName('navigationPanel')
        self.nav_panel.setFixedWidth(94)
        nav = QVBoxLayout(self.nav_panel)
        nav.setContentsMargins(8, 16, 8, 12)
        nav.setSpacing(12)
        nav.addWidget(self.close_settings_btn)
        nav.addWidget(self.settings_gear_btn)
        self._sidebar_layout = nav
        nav.addStretch(1)
        self._settings_page_widget.kofi_widget_changed.connect(self.kofi_widget.setVisible)
        self._navigation_group = QButtonGroup(self)
        self._navigation_group.setExclusive(True)
        self._navigation_group.addButton(self.close_settings_btn, 0)
        self._navigation_group.addButton(self.settings_gear_btn, 1)
        shell_layout.addWidget(self.nav_panel)
        shell_layout.addWidget(self.main_stack, 1)
        root.addWidget(shell, stretch=1)
        self.clip_grid.set_mutation_callbacks(
            self._prepare_clip_mutation, self.upload_manager.end_local_mutation)
        self.clip_grid.clip_renamed.connect(self._on_clip_renamed)

        # Error bar (shown at bottom of app for warnings/errors)
        self.error_bar = ErrorBar()
        root.addWidget(self.error_bar)

    # Drag support --

    def _bar_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if start_system_move(self.windowHandle()):
                self._drag_pos = None
                return
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def _bar_mouse_move(self, event):
        if self._drag_pos and event.buttons() == Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def _bar_mouse_release(self, event):
        self._drag_pos = None

    def _bar_double_click(self, event):
        self._toggle_maximize()

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    # Native Windows resize + Aero snap --

    def paintEvent(self, event):
        super().paintEvent(event)
        self._note_first_frame_painted()

    def _note_first_frame_painted(self) -> None:
        if self._first_frame_painted:
            return
        self._first_frame_painted = True
        if self._background_ui_pause_deferred:
            self._background_ui_pause_deferred = False
            QTimer.singleShot(0, self._refresh_background_ui_pause_state)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._refresh_background_ui_pause_state)
        if not getattr(self, '_window_screen_signals_connected', False):
            self._window_screen_signals_connected = True
            QTimer.singleShot(0, self._connect_window_screen_signals)
        if sys.platform != 'win32':
            # Compositor-driven resize for the frameless window. Idempotent:
            # reuses the filter on repeated shows and catches a first show
            # where the native window did not exist yet.
            install_resize_filter(self.windowHandle())
        if not getattr(self, '_native_style_applied', False):
            self._native_style_applied = True
            # Defer SetWindowPos(SWP_FRAMECHANGED) to after the event loop starts.
            # Calling it synchronously inside showEvent sends WM_NCCALCSIZE back
            # into nativeEvent while Qt is mid-show, causing a crash.
            if sys.platform == 'win32':
                QTimer.singleShot(0, self._apply_native_style)
            # Pre-realize the settings page so the first time the user clicks
            # the gear button it doesn't pay for layout, font resolution, and
            # stylesheet compilation. The page is already constructed; we just
            # need Qt to do its first-show work for it.
            QTimer.singleShot(0, self._prerealize_settings_page)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            QTimer.singleShot(0, self._refresh_background_ui_pause_state)

    def _prerealize_settings_page(self):
        """Polish and lay out settings pages before their first visible show.

        Apply ensurePolished/adjustSize to descendants too, since nested pages
        otherwise defer this work until the user opens them.
        """
        page = self._settings_page_widget
        try:
            page.ensurePolished()
            page.adjustSize()
            for child in page.findChildren(QWidget):
                child.ensurePolished()
        except Exception as e:
            print(f'[Prerealize] settings page warm-up failed: {e}')

    def _apply_native_style(self):
        """Apply WS_THICKFRAME so native resize/Aero-snap work on the frameless window."""
        apply_native_resize_style(self)

    def nativeEvent(self, eventType, message):
        # Returning (False, 0) lets Qt's WndProc continue with default
        # processing after the read-only message inspection below.
        if eventType == b'windows_generic_MSG':
            try:
                import ctypes, ctypes.wintypes
                ptr = int(message)
                if ptr:
                    msg_type = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.c_uint32))[2]
                    if msg_type == 0x0084:  # WM_NCHITTEST
                        msg = ctypes.wintypes.MSG.from_address(ptr)
                        cx, cy = decode_nchittest_point(msg.lParam)
                        hit = native_hit_test(
                            self, cx, cy,
                            titlebar_height=Sizes.UNIFIED_BAR_H,
                            resize_border=6)
                        if hit is not None:
                            return True, hit
            except Exception:
                # Native hit testing must safely defer to Qt's WndProc when a
                # transient native value is unavailable. Logging this mouse
                # hot path would create unbounded diagnostic spam.
                pass
        return False, 0  # not handled — Qt WndProc continues normally

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj.objectName() == 'dragArea':
            if event.type() == QEvent.Type.MouseButtonPress:
                self._bar_mouse_press(event)
            elif event.type() == QEvent.Type.MouseMove:
                self._bar_mouse_move(event)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self._bar_mouse_release(event)
            elif event.type() == QEvent.Type.MouseButtonDblClick:
                self._bar_double_click(event)
        return super().eventFilter(obj, event)

    # Helpers --

    def _vsep(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        set_theme_style(sep, lambda: (f'QFrame {{ color: {Colors.BORDER_HI};'
            f' max-width: 1px; margin: 12px 4px; }}'))
        return sep

    # Popup toggles --

    def _toggle_cap_settings(self):
        if self.cap_settings_popup.isVisible():
            self.cap_settings_popup.hide()
        else:
            self.source_popup.hide()
            self.hotkey_popup.hide()
            self.game_detection_popup.hide()
            self.cap_settings_popup.show_below(self.cap_btn)
        self._sync_topbar_dropdown_arrows()

    def _toggle_source(self):
        if self.source_popup.isVisible():
            self.source_popup.hide()
        else:
            self.cap_settings_popup.hide()
            self.hotkey_popup.hide()
            self.game_detection_popup.hide()
            self.source_popup.show_below(self.source_btn)
        self._sync_topbar_dropdown_arrows()

    def _toggle_game_detection(self):
        if self.game_detection_popup.isVisible():
            self.game_detection_popup.hide()
        else:
            self.cap_settings_popup.hide()
            self.source_popup.hide()
            self.hotkey_popup.hide()
            self.game_detection_popup.show_below(self.game_detection_btn)
        self._sync_topbar_dropdown_arrows()

    def _toggle_hotkeys(self):
        if self.hotkey_popup.isVisible():
            self.hotkey_popup.hide()
        else:
            self.cap_settings_popup.hide()
            self.source_popup.hide()
            self.game_detection_popup.hide()
            self.hotkey_popup.show_below(self.hotkey_btn)
        self._sync_topbar_dropdown_arrows()

    def _sync_topbar_dropdown_arrows(self):
        """Keep the shared top-bar arrows aligned with popup visibility."""
        self.cap_btn.setExpanded(self.cap_settings_popup.isVisible())
        self.source_btn.setExpanded(self.source_popup.isVisible())
        self.game_detection_btn.setExpanded(
            self.game_detection_popup.isVisible())
        self.hotkey_btn.setExpanded(self.hotkey_popup.isVisible())

    def _on_cap_summary_changed(self, text: str):
        self.cap_btn.setText(text)

    def _on_game_detection_summary_changed(self, text: str):
        self.game_detection_btn.setText(text)

    def _toggle_settings_page(self):
        self._set_main_page(1 if self._active_clip_viewer is not None
                            else 1 - self.main_stack.currentIndex())

    def _setup_keyboard_navigation(self):
        from core.navigation_bindings import navigation_bindings_for
        from ui.keyboard_navigation import KeyboardNavigation
        bindings = navigation_bindings_for(self.settings_manager)
        self._keyboard_navigation = KeyboardNavigation(
            self, bindings, {
                'switch_panel': self._toggle_settings_page,
                'sidebar_next': lambda: self._navigate_sidebar(1),
                'sidebar_previous': lambda: self._navigate_sidebar(-1),
                'toggle_detection': lambda: self.game_detection_popup.detection_switch.toggle(),
                'restart_capture': self._restart_engine_shortcut,
                'library_scroll_up': lambda: self._scroll_clip_library(-1),
                'library_scroll_down': lambda: self._scroll_clip_library(1),
                'category_next': lambda: self._settings_page_widget.navigate_category(1),
                'category_previous': lambda: self._settings_page_widget.navigate_category(-1),
            }, lambda: {'application', 'main', 'settings'}
            if self.main_stack.currentIndex() == 1
            else {'application', 'main', 'library'},
            repeat_actions=('library_scroll_up', 'library_scroll_down'))

    def _scroll_clip_library(self, direction):
        if self.main_stack.currentIndex() != 0:
            return
        bar = self.clip_scroll_area.verticalScrollBar()
        bar.setValue(bar.value() + direction * max(40, bar.singleStep() * 3))

    def _set_main_page(self, index: int) -> None:
        if self._active_clip_viewer is not None and not self._park_active_editor():
            return
        for popup in (self.cap_settings_popup, self.source_popup,
                      self.hotkey_popup, self.game_detection_popup):
            popup.hide()
        self.main_stack.setCurrentIndex(index)
        if index == 1:
            for key in ('timeline_preview_strip_enabled', 'hide_deleted_timeline_segments',
                        'grid_show_edited_duration'):
                check = self._settings_page_widget.__dict__.get(key + '_check')
                if check is not None:
                    check.blockSignals(True)
                    check.setChecked(bool(self.settings_manager.get(
                        key, key == 'timeline_preview_strip_enabled')))
                    check.blockSignals(False)
        self.close_settings_btn.setChecked(index == 0)
        self.settings_gear_btn.setChecked(index == 1)
        self._settings_page_widget.set_background_ui_paused(
            self._background_ui_paused or index != 1)
        self.clip_grid.set_background_paused(
            self._background_ui_paused or index != 0)
        if index == 0 and hasattr(self.clip_grid, 'refresh_duration_labels'):
            self.clip_grid.refresh_duration_labels()
        camera_timer = self._settings_page_widget.__dict__.get('_camera_preview_timer')
        if camera_timer is not None:
            if (index == 1 and not self._background_ui_paused
                    and self._settings_page_widget.stack.currentIndex() == 3
                    and self.settings_manager.get('camera_enabled', False)):
                camera_timer.start()
            else:
                camera_timer.stop()
        if hasattr(self, '_keyboard_navigation'):
            (self.close_settings_btn if index == 0 else self.settings_gear_btn).setFocus()
        if self.isVisible():
            self.raise_()
            self.activateWindow()

    def _navigate_sidebar(self, step):
        destinations = [0, 1] + list(self._editor_sessions)
        current = (self._active_editor_session if self._active_clip_viewer is not None
                   else self.main_stack.currentIndex())
        index = destinations.index(current) if current in destinations else 0
        destination = destinations[(index + step) % len(destinations)]
        if isinstance(destination, int):
            self._set_main_page(destination)
        else:
            self._restore_editor_session(destination)

    def _refresh_editor_sidebar(self):
        for index, session in enumerate(self._editor_sessions, 1):
            stem = Path(session.clip_path).stem
            session.button.ensurePolished()
            short = session.button.fontMetrics().elidedText(
                stem, Qt.TextElideMode.ElideRight, session.button.width() - 22)
            session.button.setText(f'Editor {index}\n{short}')
            session.button.setAccessibleName(f'Editor {index}: {stem}')
            session.button.setToolTip(
                f'{Path(session.clip_path).name}\n'
                + ('Active editor' if session is self._active_editor_session
                   and self._active_clip_viewer is not None else 'Paused — click to restore')
                + '\nRight-click to close')
        active = self._active_editor_session if self._active_clip_viewer is not None else None
        if active is not None:
            active.button.setChecked(True)
        else:
            (self.close_settings_btn if self.main_stack.currentIndex() == 0
             else self.settings_gear_btn).setChecked(True)

    def _add_editor_session(self, clip_path):
        from ui.editor_sessions import EditorSession
        session = EditorSession(clip_path)
        button = QToolButton(self.nav_panel)
        session.button = button
        button.setObjectName('editorSidebarButton')
        button.setFixedSize(78, 78)
        button.setCheckable(True)
        button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        button.clicked.connect(lambda: self._restore_editor_session(session))
        button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        button.customContextMenuRequested.connect(
            lambda pos: self._editor_session_menu(session, pos))
        self._navigation_group.addButton(button)
        self._sidebar_layout.insertWidget(self._sidebar_layout.count() - 1, button)
        self._editor_sessions.append(session)
        self._refresh_editor_sidebar()
        return session

    def _remove_editor_session(self, session):
        if session not in self._editor_sessions:
            return
        self._editor_sessions.remove(session)
        self._navigation_group.removeButton(session.button)
        self._sidebar_layout.removeWidget(session.button)
        session.button.hide()
        session.button.deleteLater()
        if self._active_editor_session is session:
            self._active_editor_session = None
        self._refresh_editor_sidebar()

    def _editor_session_menu(self, session, pos):
        menu = QMenu(session.button)
        menu.addAction('Close editor', lambda: self._close_editor_session(session))
        menu.exec(session.button.mapToGlobal(pos))

    def _close_editor_session(self, session):
        if session is self._active_editor_session and self._active_clip_viewer is not None:
            self._active_clip_viewer._close()
        else:
            self._remove_editor_session(session)

    def _park_active_editor(self):
        viewer = self._active_clip_viewer
        if viewer is None:
            return True
        worker = getattr(viewer, '_export_thread', None)
        if worker is not None and worker.is_alive():
            QMessageBox.information(viewer, 'Export in progress',
                                    'Finish or cancel the export before minimizing this editor.')
            return False
        self._cancel_editor_transition()
        session = self._active_editor_session
        session.window_geometry = QRect(viewer.mapToGlobal(QPoint(0, 0)), viewer.size())
        session.window_maximized = viewer.isMaximized()
        session.state = viewer.capture_session_state()
        self._active_clip_viewer = None
        self._active_editor_session = None
        # One reusable shell, no active media or timers. Dormant sessions
        # continue to own only edit data; they never each retain a decoder.
        viewer._teardown_player(wait_for_workers=False)
        viewer.hide()
        self._parked_editor_shell = viewer
        self._refresh_editor_sidebar()
        return True

    def _minimize_active_editor(self, viewer):
        if viewer is not self._active_clip_viewer:
            return
        worker = getattr(viewer, '_export_thread', None)
        animate = (self.isVisible() and viewer.isVisible() and not reduce_motion(self)
                   and not (worker is not None and worker.is_alive()))
        session = self._active_editor_session
        start = QRect(viewer.mapToGlobal(QPoint(0, 0)), viewer.size())
        frame = compact_snapshot(viewer) if animate else QPixmap()
        if self._park_active_editor():
            if not animate or frame.isNull():
                self._set_main_page(self.main_stack.currentIndex())
                return
            flight = SnapshotFlight(self, frame, start, sidebar_rect(session.button), compact=True)
            self._editor_flight = flight
            # Resume library previews after the flight, so thumbnail loading
            # does not compete with animation frames on the UI thread.
            def finish_minimize():
                self._editor_transition_finished()
                if not self._shutdown_requested:
                    self._set_main_page(self.main_stack.currentIndex())

            flight._animation.finished.connect(finish_minimize)
            flight.start()

    def _editor_transition_finished(self):
        self._editor_flight = None

    def _cancel_editor_transition(self):
        flight = getattr(self, '_editor_flight', None)
        if flight is not None:
            flight._animation.stop()
            flight.hide()
            flight.deleteLater()
            self._editor_flight = None

    def _restore_editor_session(self, session):
        if session not in self._editor_sessions:
            return
        if session is self._active_editor_session and self._active_clip_viewer is not None:
            self._active_clip_viewer.raise_()
            self._active_clip_viewer.activateWindow()
            return
        if not os.path.isfile(session.clip_path) or not self._clip_readiness.can_access(session.clip_path):
            self.push_error('CLIP NOT FOUND', 'The editor clip is no longer available.', level='warning')
            self._remove_editor_session(session)
            return
        if self._park_active_editor():
            self._show_editor_session(session, QPixmap(), animate_restore=True)

    def _prepare_clip_mutation(self, path: str) -> str:
        if not self._clip_readiness.can_access(path):
            return 'This clip is still being saved or processed. Try again when it finishes.'
        from core.ffmpeg_playback import finish_audio_cleanup
        if not finish_audio_cleanup(path):
            return 'This clip is still closing in the editor. Try again when it finishes.'
        viewer = self._active_clip_viewer
        if viewer is not None and os.path.normcase(viewer.clip_path) == os.path.normcase(path):
            worker = getattr(viewer, '_export_thread', None)
            if worker is not None and worker.is_alive():
                return 'This clip is exporting. Finish or cancel the export before changing its files.'
        if not self.upload_manager.begin_local_mutation(path):
            return 'This clip is uploading. Try again when the upload finishes.'
        viewer = self._active_clip_viewer
        if viewer is not None and os.path.normcase(viewer.clip_path) == os.path.normcase(path):
            viewer.close()  # flush draft and detach all multimedia handles
            self._active_clip_viewer = None
            if not finish_audio_cleanup(path):
                self.upload_manager.end_local_mutation(path)
                return 'This clip is still closing in the editor. Try again when it finishes.'
        for session in list(self._editor_sessions):
            if os.path.normcase(session.clip_path) == os.path.normcase(path):
                self._remove_editor_session(session)
        return ''

    def _on_clip_renamed(self, old_path: str, new_path: str) -> None:
        try:
            self.clip_metadata_manager.rename_path(old_path, new_path)
            self.upload_manager.rename_history_path(old_path, new_path)
        except (OSError, ValueError) as error:
            self.push_error('Clip renamed', f'The file was renamed, but its saved metadata could not be updated: {error}', level='warning')

    # Hotkeys


    def _warn_no_engine(self, process_generation: int | None = None):
        if getattr(sys, 'frozen', False):
            detail = ('FTHRcapture binary is missing from the installation.'
                      ' Re-download the latest release.')
        else:
            detail = ('FTHRcapture binary not found. Build it:'
                      ' msbuild FTHRcapture/FTHRcapture.sln -p:Configuration=Release -p:Platform=x64'
                      if sys.platform == 'win32' else
                      'FTHRcapture binary not found. Build it:'
                      ' cd FTHRcapture_linux && bash build_linux.sh'
                      ' (or point FTHR_ENGINE at a built FTHRclips)')
        self.push_error(
            'ENGINE NOT FOUND',
            detail,
            level='error',
            actions=[('OPEN SETTINGS', self._toggle_settings_page)],
            key='engine-launch',
            diagnostic_context={
                'diagnostic_error': DiagnosticError.ENGINE_START_FAILED,
                'technical_cause': 'ENGINE_EXECUTABLE_MISSING',
                'technical_subsystem': 'process',
                'operation': 'executable_discovery',
                'process_generation': process_generation,
                'backend_generation': None,
                'recovery_kind': 'engine_lineage',
                'recovery_status': 'awaiting_replacement_engine',
            },
        )

    def _setup_hotkeys(self):
        self.hotkey_manager.save_clip_triggered.connect(self._on_hotkey_save_clip)
        self.hotkey_manager.custom_clip_triggered.connect(self._save_clip)
        self.hotkey_manager.save_screenshot_triggered.connect(
            self._on_hotkey_save_screenshot)
        self.hotkey_manager.start_recording_triggered.connect(
            self._on_hotkey_start_recording)
        self.hotkey_manager.stop_recording_triggered.connect(
            self._on_hotkey_stop_recording)
        self.hotkey_manager.confirm_game_detection_triggered.connect(
            self._on_confirm_game_detection)
        self.hotkey_manager.dismiss_game_detection_triggered.connect(
            self._on_dismiss_game_detection)
        self.hotkey_manager.error_occurred.connect(
            self._show_hotkey_warning
        )
        self.hotkey_manager.register_all()
        print("Hotkeys registered.")

        # Warn if key features are limited on the current compositor.
        if sys.platform != 'win32':
            from core.compositor import has_xtools
            if detect_compositor() not in ('hyprland', 'x11') and not has_xtools():
                QTimer.singleShot(2000, self._show_compositor_warning)

    def _show_compositor_warning(self):
        comp = detect_compositor()
        comp_name = {
            'kwin':            'KDE Plasma',
            'gnome':           'GNOME',
            'wayland-unknown': 'your Wayland compositor',
        }.get(comp, comp)
        self.push_error(
            'LIMITED FEATURE SUPPORT',
            f'FTHR Clips is running on {comp_name}. Game detection and focus '
            'monitoring need xdotool; install it with your package manager. '
            'Hotkeys remain available through the desktop or the FTHR socket.',
            level='warning',
        )


    def _on_hotkey_save_clip(self):
        config = self._capture_config.active
        self._save_clip(config.normal_clip_seconds if config else self.clip_duration)


    def _on_hotkey_start_recording(self):
        if self._manual_record_state == 'idle':
            self._start_manual_recording()

    def _on_hotkey_stop_recording(self):
        if self._manual_record_state == 'recording':
            self._stop_manual_recording()

    def _on_hotkey_save_screenshot(self):
        if self._screenshot_inflight:
            # One active plus nine pending requests covers a rapid ten-shot
            # sequence without spawning unbounded capture/PNG workers.
            if self._screenshot_pending_requests < 9:
                self._screenshot_pending_requests += 1
                print('[Screenshot] Queued request while a screenshot is saving '
                      f'({self._screenshot_pending_requests}/9 pending).')
            else:
                print('[Screenshot] Queue full; ignored request beyond ten shots.')
            return

        if self._shutdown_requested:
            return

        self._screenshot_inflight = True

        config = self._capture_config.active
        selected_monitor = (
            config.monitor if config is not None
            else self.settings_manager.get('capture_monitor', ''))
        try:
            paths = reserve_screenshot_paths(
                clips_directory_from(self.settings_manager) / 'Screenshots')
        except ScreenshotSaveError as error:
            self._finish_screenshot_request()
            self._show_screenshot_error(error.code, error.detail)
            return
        except StoragePathError as error:
            self._finish_screenshot_request()
            self._show_screenshot_error('STORAGE_UNAVAILABLE', str(error))
            return

        screen = self._resolve_screenshot_screen(selected_monitor)
        if screen is None:
            self._fail_screenshot_request(
                paths,
                'MONITOR_NOT_FOUND',
                'The configured capture monitor is unavailable. FTHR did not '
                'fall back to another display.',
            )
            return

        pass

        self._capture_selected_qt_screen(paths, selected_monitor)

    def _resolve_screenshot_screen(self, selected_monitor: str):
        """Resolve the current selected screen without cross-monitor fallback."""

        return select_qt_screen(
            selected_monitor,
            QApplication.screens(),
            platform=sys.platform,
            windows_monitors=(
                enumerate_windows_monitors() if sys.platform == 'win32' else ()),
            primary=QApplication.primaryScreen(),
        )





    def _capture_selected_qt_screen(self, paths, selected_monitor: str) -> None:
        # Resolve the current stable Windows monitor identity before capture.
        screen = self._resolve_screenshot_screen(selected_monitor)
        if screen is None:
            self._fail_screenshot_request(
                paths,
                'MONITOR_NOT_FOUND',
                'The configured capture monitor is unavailable. FTHR did not '
                'fall back to another display.',
            )
            return

        try:
            # Passing the screen bounds explicitly fixes a Windows/Qt edge
            # case where grabWindow(0) can return an empty pixmap for a hidden
            # tray-started application or a secondary display.
            geometry = screen.geometry()
            pixmap = screen.grabWindow(
                0, 0, 0, geometry.width(), geometry.height())
            if pixmap.isNull():
                pixmap = screen.grabWindow(0)
        except Exception as error:
            self._fail_screenshot_request(
                paths,
                'CAPTURE_UNAVAILABLE',
                f'Could not capture the configured monitor: {error}',
            )
            return
        if pixmap.isNull():
            self._fail_screenshot_request(
                paths,
                'CAPTURE_UNAVAILABLE',
                'The configured monitor returned an empty screenshot.',
            )
            return

        image = pixmap.toImage()
        if image.isNull():
            self._fail_screenshot_request(
                paths,
                'CAPTURE_UNAVAILABLE',
                'The configured monitor could not provide an image.',
            )
            return

        worker = ScreenshotPngSaveWorker(image, paths.staged, self)
        self._screenshot_save_worker = worker
        worker.succeeded.connect(
            lambda _staged, target=paths: self._publish_screenshot(target))
        worker.failed.connect(
            lambda code, detail, target=paths: self._on_screenshot_save_failed(
                target, code, detail))
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _publish_screenshot(self, paths) -> None:
        """Publish a completed PNG immediately and refresh the library."""
        self._screenshot_save_worker = None
        try:
            publish_staged_png(paths.staged, paths.final)
            self.capture_card.show_screenshot()
            if hasattr(self, 'clip_grid'):
                self.clip_grid.force_refresh()
        except ScreenshotSaveError as error:
            paths.staged.unlink(missing_ok=True)
            self._show_screenshot_error(error.code, error.detail)
        finally:
            self._finish_screenshot_request()

    def _on_screenshot_save_failed(self, paths, code: str, detail: str) -> None:
        paths.staged.unlink(missing_ok=True)
        self._screenshot_save_worker = None
        self._finish_screenshot_request()
        self._show_screenshot_error(code, detail)

    def _fail_screenshot_request(
            self, paths, code: str, detail: str) -> None:
        paths.staged.unlink(missing_ok=True)
        self._finish_screenshot_request()
        self._show_screenshot_error(code, detail)

    def _finish_screenshot_request(self) -> None:
        self._screenshot_inflight = False
        if (self._screenshot_pending_requests > 0
                and not self._shutdown_requested):
            self._screenshot_pending_requests -= 1
            QTimer.singleShot(0, self._on_hotkey_save_screenshot)

    def _stop_screenshot_jobs(self) -> None:
        """Bound screenshot-worker cleanup during full application shutdown."""

        self._screenshot_pending_requests = 0
        process = self._screenshot_capture_process
        paths = self._screenshot_capture_paths
        self._screenshot_capture_process = None
        self._screenshot_capture_paths = None
        if process is not None:
            process.kill()
            process.waitForFinished(250)
            process.deleteLater()
            if paths is not None:
                paths.staged.unlink(missing_ok=True)
        worker = self._screenshot_save_worker
        if worker is not None and worker.isRunning():
            if not worker.wait(1500):
                print('[Lifecycle] Screenshot worker exceeded shutdown grace')
        self._screenshot_inflight = False

    def _show_screenshot_error(self, code: str, detail: str) -> None:
        """Keep screenshot failures actionable when the main window is hidden."""

        print(f'[Screenshot] {code}: {detail}')
        self.push_error('SCREENSHOT FAILED', f'{code}: {detail}', level='error')
        FthrMessageDialog.warning(
            self if self.isVisible() else None,
            'Screenshot Failed',
            f'{code}\n\n{detail}',
        )


    def _prepare_startup_capture_source(self) -> None:
        """Start game detection from desktop capture.

        A persisted HWND may be closed, recycled, or misclassified. Wait for live
        foreground detection before selecting a game source.
        """
        current = (
            self.settings_manager.get('capture_mode', 'desktop'),
            self.settings_manager.get('target_hwnd', 0),
            self.settings_manager.get('target_window_name', ''),
        )
        desktop = ('desktop', 0, '')
        if current == desktop:
            return
        self.settings_manager.update({
            'capture_mode': desktop[0],
            'target_hwnd': desktop[1],
            'target_window_name': desktop[2],
        })
        self.settings_manager.save_settings()
        print('[GameDetection] Startup source reset to Desktop; '
              'waiting for a stable game candidate')

    def _start_game_detection(self) -> None:
        """Start detection after the initial desktop engine launch is queued."""
        if not bool(self.settings_manager.get('game_detection_enabled', False)):
            return
        self._game_detector.start()

    def _set_capture_window_source(self, capture_window: dict) -> bool:
        """Apply a detected window source with or without the deferred UI."""
        source_popup = getattr(self, 'source_popup', None)
        if source_popup is not None:
            return bool(source_popup.set_capture_window(capture_window))

        try:
            hwnd = int(capture_window.get('hwnd', 0) or 0)
        except (AttributeError, TypeError, ValueError):
            hwnd = 0
        if not hwnd:
            return False
        self.settings_manager.update({
            'capture_mode': 'window',
            'target_hwnd': hwnd,
            'target_window_name': capture_window.get('display_name', ''),
        })
        self.settings_manager.save_settings()
        return True

    def _set_capture_desktop_source(self) -> bool:
        """Apply the desktop source with or without the deferred UI."""
        source_popup = getattr(self, 'source_popup', None)
        if source_popup is not None:
            return bool(source_popup.set_capture_desktop())

        self.settings_manager.update({
            'capture_mode': 'desktop',
            'target_hwnd': 0,
            'target_window_name': '',
        })
        self.settings_manager.save_settings()
        return True

    def _game_window_name(self, window) -> str:
        if isinstance(window, GameWindow):
            return window.display_name
        if isinstance(window, dict):
            return window.get('display_name') or window.get('title') or 'Game'
        return 'Game'

    def _game_window_hwnd(self, window) -> int:
        try:
            return int(window.hwnd if isinstance(window, GameWindow)
                       else window.get('hwnd', 0) or 0)
        except (AttributeError, TypeError, ValueError):
            # Malformed/stale candidates are deliberately treated as no window.
            return 0

    def _suppress_game_candidate(self, window) -> None:
        if isinstance(window, GameWindow) and hasattr(self._game_detector, 'suppress'):
            self._game_detector.suppress(window)

    def _on_game_appeared(self, window: dict):
        self._handle_game_candidate(window)

    def _on_game_detected(self, window: GameWindow):
        self._handle_game_candidate(window)

    def _game_candidate_blocked(self, window) -> bool:
        return bool(isinstance(window, GameWindow)
                    and matching_custom_game_rule(window, self.settings_manager.get(
                        'game_detection_blacklist', [])))

    def _handle_game_candidate(self, window) -> None:
        if MainWindow._game_candidate_blocked(self, window):
            return
        if not bool(self.settings_manager.get('game_detection_enabled', False)):
            return
        if isinstance(window, GameWindow) and not is_capture_window_valid(window.hwnd):
            return

        hwnd = self._game_window_hwnd(window)
        if not hwnd:
            return
        try:
            current_hwnd = int(self.settings_manager.get('target_hwnd', 0) or 0)
        except (TypeError, ValueError):
            current_hwnd = 0
        if (self.settings_manager.get('capture_mode', 'desktop') == 'window'
                and current_hwnd == hwnd):
            active_game = getattr(self, '_active_game_window', None)
            if (isinstance(window, GameWindow)
                    and isinstance(active_game, GameWindow)
                    and window.capture_signature != active_game.capture_signature):
                # WGC frame pools and the hardware encoder are initialized for
                # one source geometry. A fullscreen game can keep the same
                # HWND while changing the monitor/display mode, so refresh the
                # snapshot and recreate the capture generation automatically.
                self._active_game_window = window
                self._on_game_capture_geometry_changed(active_game, window)
            return

        mode = str(self.settings_manager.get(
            'game_detection_mode', 'auto')).lower()
        if mode == 'prompt':
            self._show_game_detection_prompt(window)
        else:
            self._switch_capture_to_game(window)

    def _on_game_capture_geometry_changed(
            self, previous: GameWindow, current: GameWindow) -> None:
        """Recreate capture after a stable game/monitor size change."""
        print(
            '[GameDetection] Capture geometry changed for '
            f'{current.display_name}: '
            f'{previous.width}x{previous.height} -> '
            f'{current.width}x{current.height} '
            f'(monitor {previous.monitor_width}x{previous.monitor_height} -> '
            f'{current.monitor_width}x{current.monitor_height}); '
            'restarting capture generation')

        manual_state = getattr(self, '_manual_record_state', 'idle')
        if manual_state in {'starting', 'recording', 'stopping', 'finalizing'}:
            # A fragmented MP4 has one fixed video stream description. It is
            # not safe to swap source dimensions underneath an active manual
            # recording; preserve the file and let the user finish it before
            # starting the fresh capture generation. The pending flag makes
            # that restart happen as soon as the file is closed.
            self._pending_game_geometry_restart = True
            self.push_error(
                'GAME RESOLUTION CHANGED',
                'The current recording uses a fixed video size. Stop it when '
                'ready; capture will refresh for the new game resolution.',
                level='warning',
            )
            return

        self._restart_capture_engine(
            allow_recording_prepare=(manual_state == 'preparing'))

    def _show_game_detection_prompt(self, window) -> None:
        if self._pending_game_window is not None:
            self._suppress_game_candidate(self._pending_game_window)
        self._pending_game_window = window
        game_name = self._game_window_name(window)
        hotkey = self.hotkey_manager.hotkeys.get('confirm_game_detection', 'F8')
        dismiss = self.hotkey_manager.hotkeys.get('dismiss_game_detection', 'F7')
        self.capture_card.show_prompt(
            f'{game_name} detected — [{hotkey}] Record  [{dismiss}] Dismiss')
        self._game_dismiss_timer.stop()
        self._game_dismiss_timer.start(15000)

    def _on_game_closed(self, hwnd: int):
        if hwnd == self._active_game_hwnd:
            self._on_game_lost()

    def _switch_capture_to_game(self, window) -> bool:
        hwnd = self._game_window_hwnd(window)
        if not hwnd:
            return False
        if isinstance(window, GameWindow):
            if not is_capture_window_valid(hwnd):
                print(f'Detected game window closed before capture handoff: '
                      f'{window.title}')
                return False
            capture_window = window.as_capture_window()
        else:
            capture_window = window

        if not self._set_capture_window_source(capture_window):
            return False
        game_name = self._game_window_name(window)
        self._active_game_hwnd = hwnd
        self._active_game_window = window if isinstance(window, GameWindow) else None
        self._auto_capture_hwnd = hwnd
        if hasattr(self, 'source_btn'):
            self.source_btn.setText(game_name.upper())
        if (focus_pause_supported(sys.platform)
                and self.settings_manager.get('anticheat_detection_enabled', False)):
            self._focus_monitor.set_target(game_name)
            self._focus_monitor.start()
        print(f'Game detection switching capture to: {game_name} ({hwnd})')
        self.capture_card.show_capturing(game_name)
        QTimer.singleShot(0, self._restart_capture_engine)
        return True

    def _on_confirm_game_detection(self):
        window = self._pending_game_window
        if window is None:
            return
        self._pending_game_window = None
        self._game_dismiss_timer.stop()
        if MainWindow._game_candidate_blocked(self, window):
            return
        self._switch_capture_to_game(window)

    def _on_dismiss_game_detection(self):
        self._suppress_game_candidate(self._pending_game_window)
        self._pending_game_window = None
        self._game_dismiss_timer.stop()

    def _on_game_prompt_timeout(self):
        self._suppress_game_candidate(self._pending_game_window)
        self._pending_game_window = None

    def _on_game_lost(self):
        if not bool(self.settings_manager.get(
                'game_detection_fallback_desktop', False)):
            return
        if not self._auto_capture_hwnd:
            return

        # Foreground loss is not a close. Keep the selected target until the
        # window really disappears, then the timer will call this method again.
        if is_capture_window_valid(
                self._auto_capture_hwnd):
            if not self._fallback_watch_timer.isActive():
                self._fallback_watch_timer.start()
            return

        self._fallback_watch_timer.stop()
        try:
            current_hwnd = int(self.settings_manager.get('target_hwnd', 0) or 0)
        except (TypeError, ValueError):
            current_hwnd = 0
        if (self.settings_manager.get('capture_mode', 'desktop') != 'window'
                or current_hwnd != self._auto_capture_hwnd):
            self._auto_capture_hwnd = 0
            return

        self._auto_capture_hwnd = 0
        self._active_game_hwnd = None
        self._active_game_window = None
        self._focus_monitor.stop()
        if focus_pause_supported(sys.platform) and self.bridge.is_connected():
            self.bridge.resume_recording()
        if self._set_capture_desktop_source():
            if hasattr(self, 'source_btn'):
                self.source_btn.setText('DESKTOP')
            print('No detected game active; falling back to desktop capture')
            self._restart_capture_engine()
            self.capture_card.show_prompt('Game closed — switched back to Desktop')

    def _on_game_detection_configuration_changed(self, enabled: bool,
                                                   mode: str):
        settings_page = getattr(self, '_settings_page_widget', None)
        settings_check = getattr(settings_page, 'game_detection_check', None)
        if settings_check is not None:
            settings_check.blockSignals(True)
            settings_check.setChecked(bool(enabled))
            settings_check.blockSignals(False)
        started = self._game_detector.set_enabled(bool(enabled))
        if enabled and not started:
            self.game_detection_btn.setToolTip(
                'Game detection is unavailable on this platform.')
        else:
            self.game_detection_btn.setToolTip('')
        if not enabled:
            self._suppress_game_candidate(self._pending_game_window)
            self._pending_game_window = None
            self._game_dismiss_timer.stop()
        elif mode == 'auto' and self._pending_game_window is not None:
            self._on_confirm_game_detection()

    def _on_custom_game_rules_changed(self, rules: object):
        if hasattr(self._game_detector, 'set_custom_game_rules'):
            self._game_detector.set_custom_game_rules(rules)
        if hasattr(self._game_detector, 'set_blacklist_rules'):
            self._game_detector.set_blacklist_rules(
                self.settings_manager.get('game_detection_blacklist', []))

    def _on_focus_lost(self):
        if focus_pause_supported(sys.platform) and self.bridge.is_connected():
            self.bridge.pause_recording()
            self._set_status('PAUSED — GAME UNFOCUSED', status_warning_qss())

    def _on_focus_regained(self):
        if focus_pause_supported(sys.platform) and self.bridge.is_connected():
            self.bridge.resume_recording()
            # Resume is a request, not proof that fresh frames have returned.
            self._set_status('STARTING CAPTURE', status_idle_qss())

    # Settings handlers

    def _on_clip_length_changed(self, duration: int):
        self.clip_duration  = duration
        self.buffer_seconds = compute_buffer_seconds(
            duration, self.settings_manager.get('custom_clip_lengths', []))


    def _on_custom_lengths_changed(self):
        self.hotkey_manager.configure_custom_clips(
            self.settings_manager.get('custom_clip_lengths', []))
        self.hotkey_popup.reload_actions()
        self._on_clip_length_changed(self.clip_duration)

    def _on_framerate_changed(self, fps: int):        self.capture_fps = fps
    def _on_resolution_changed(self, w: int, h: int): self.capture_width, self.capture_height = w, h
    def _on_bitrate_changed(self, kbps: int):         self.capture_bitrate = kbps

    def _on_audio_capture_changed(self, _enabled: bool):
        self._restart_capture_engine()
        if hasattr(self, '_gary_overlay'):
            self._sync_gary_settings()

    def _on_audio_capture_mode_changed(self, _mode: str):
        # The mode is part of the capture generation contract. Restarting here
        # also makes the first clip after a toggle unambiguous if a save and a
        # settings change happen close together.
        self._restart_capture_engine()

    def _on_capture_card_changed(self, enabled: bool):
        setter = getattr(self.capture_card, 'set_visuals_enabled', None)
        if setter is not None:
            mode = self.settings_manager.get('capture_card_mode', 'all')
            setter(bool(enabled) and (mode == 'all'))

    def _set_capture_apply_state(self, applying: bool):
        was_applying = getattr(self, '_capture_settings_applying', False)
        self._capture_settings_applying = bool(applying)
        timer = getattr(self, '_capture_apply_timer', None)
        if timer is not None:
            if applying and not was_applying:
                timer.start()
            elif not applying:
                timer.stop()
        if not applying:
            self._engine_restart_requested = False
        for popup_name in ('cap_settings_popup', 'source_popup'):
            popup = getattr(self, popup_name, None)
            if popup is not None:
                popup.set_apply_state(applying)
        if applying:
            self._set_status(self._capture_pending_text(), status_idle_qss())

    def _capture_apply_timed_out(self):
        if (not self._capture_settings_applying
                or getattr(self, '_shutdown_requested', False)):
            return
        detail = 'Capture did not confirm the new settings within 30 seconds.'
        # This is a UI confirmation deadline, not proof of an engine failure.
        # Keep capture alive, and prevent a late handshake from reopening Apply.
        self._engine_handshake_generation = None
        self._pending_launch_config = None
        self._pending_launch_generation = None
        self._restart_pending = False
        self._restart_deferred_for_save = False
        self._capture_config.fail(detail)
        self._set_capture_apply_state(False)
        self._set_status('SETTINGS APPLY FAILED', status_warning_qss())
        self.push_error(
            'SETTINGS APPLY TIMED OUT',
            detail + ' Adjust the settings and apply again, or restart the engine.',
            level='error',
            actions=[('RESTART ENGINE', self._restart_capture_engine)],
            key='capture-apply-timeout',
        )
        if getattr(self, '_manual_record_state', 'idle') == 'preparing':
            self._fail_manual_recording(detail)

    def _capture_pending_text(self):
        return ('RESTARTING ENGINE' if getattr(self, '_engine_restart_requested', False)
                else 'APPLYING SETTINGS')

    def _restart_engine_shortcut(self):
        if getattr(self, '_manual_record_state', 'idle') == 'idle':
            self._engine_restart_requested = True
        self._restart_capture_engine()

    def _sync_requested_capture_settings(self) -> bool:
        try:
            clip = validate_normal_clip_length(
                int(self.settings_manager.get('clip_length', 30)))
            fps = validate_fps(int(self.settings_manager.get('framerate', 60)))
        except (TypeError, ValueError) as exc:
            self.push_error(
                'INVALID CAPTURE PRESET',
                f'The preset was not applied: {exc}',
                level='warning',
            )
            return False
        resolution = self.settings_manager.get('resolution', 'source')
        quality = self.settings_manager.get('bitrate_level', 'medium')
        if resolution not in BITRATE_PRESETS or quality not in {
                'low', 'medium', 'high', 'custom'}:
            self.push_error(
                'INVALID CAPTURE PRESET',
                'The preset contains an unsupported resolution or quality.',
                level='warning',
            )
            return False
        self.clip_duration = clip
        self.capture_fps = fps
        self.capture_width, self.capture_height = _resolution_to_dims(resolution)
        if quality == 'custom':
            try:
                custom_bitrate = int(self.settings_manager.get(
                    'custom_bitrate_kbps', 25_000))
            except (TypeError, ValueError):
                custom_bitrate = 25_000
            self.capture_bitrate = max(500, min(200_000, custom_bitrate))
        else:
            self.capture_bitrate = BITRATE_PRESETS[resolution][quality]
        self.buffer_seconds = compute_buffer_seconds(clip, self.settings_manager.get('custom_clip_lengths', []))
        return True

    def _requested_capture_config(self, profile: str | None = None) -> CaptureConfig:
        profile = profile or getattr(self, '_engine_profile', 'clips')
        fps = self.capture_fps
        width, height = self.capture_width, self.capture_height
        bitrate = self.capture_bitrate
        if profile == 'recording':
            try:
                fps = validate_fps(int(self.settings_manager.get(
                    'recording_framerate', self.capture_fps)))
            except (TypeError, ValueError):
                fps = self.capture_fps
            resolution = str(self.settings_manager.get(
                'recording_resolution', 'source')).lower()
            if resolution not in BITRATE_PRESETS:
                resolution = 'source'
            width, height = _resolution_to_dims(resolution)
            quality = str(self.settings_manager.get(
                'recording_bitrate_level', 'high')).lower()
            if quality == 'custom':
                try:
                    bitrate = int(self.settings_manager.get(
                        'recording_custom_bitrate_kbps', 25_000))
                except (TypeError, ValueError):
                    bitrate = 25_000
                bitrate = max(500, min(200_000, bitrate))
            else:
                bitrate = BITRATE_PRESETS[resolution].get(
                    quality, BITRATE_PRESETS[resolution]['high'])
        monitor = str(self.settings_manager.get('capture_monitor', '') or '')
        if sys.platform != 'win32':
            # The Linux engine takes an output name (QScreen/xrandr) or '' for
            # its default output. Windows device paths, desktop identities
            # and diagnostic sentinels copied from a Windows settings file
            # mean nothing there, so they fall back to the default output.
            if (is_monitor_capture_identity(monitor)
                    or is_monitor_selection_sentinel(monitor)):
                monitor = ''
        else:
            current_choices = enumerate_windows_monitors()
            selection = resolve_monitor_selection(monitor, current_choices)
            if not selection.path:
                # This failure is deliberately raised before CaptureConfig is
                # handed to start_engine, so no unresolved value can reach the
                # native process.  The event preserves stale/not-configured/
                # ambiguous/no-display semantics for field diagnostics.
                emit_event(
                    'capture', 'monitor_selection_failed', state='FAILED',
                    error=getattr(DiagnosticError, selection.code,
                                  selection.code),
                    monitor_selection=selection.as_dict())
                raise MonitorSelectionError(selection)
            if selection.persist_required:
                previous_value = self.settings_manager.get(
                    'capture_monitor', monitor)
                self.settings_manager.set('capture_monitor', selection.path)
                if self.settings_manager.save_settings() is False:
                    self.settings_manager.set('capture_monitor', previous_value)
                    failed = MonitorSelectionResult(
                        state=MonitorSelectionState.PERSIST_FAILED,
                        code='MONITOR_PERSIST_FAILED',
                        reason='could not persist resolved monitor path',
                        previous_state=selection.previous_state,
                        previous_path=selection.previous_path,
                    )
                    emit_event(
                        'capture', 'monitor_selection_failed', state='FAILED',
                        error=getattr(DiagnosticError, failed.code,
                                      failed.code),
                        monitor_selection=failed.as_dict())
                    raise MonitorSelectionError(failed)
            emit_event(
                'capture', selection.code.lower(),
                **selection.as_dict())
            monitor = selection.path
        crop = None
        getattr(self, '_active_game_window', None)
        try:
            int(self.settings_manager.get('target_hwnd', 0) or 0)
        except (TypeError, ValueError):
            pass
        pass
        crop_enabled = bool(crop and crop.get('enabled', True))
        return CaptureConfig(
            fps=fps,
            buffer_seconds=compute_buffer_seconds(
                self.clip_duration, self.settings_manager.get('custom_clip_lengths', [])),
            width=width,
            height=height,
            bitrate_kbps=bitrate,
            codec=self.settings_manager.get('codec_pref', 'auto'),
            preset=int(self.settings_manager.get('encoder_preset', 4)),
            monitor=monitor,
            scaling=self.settings_manager.get('scaling_mode', 'stretch'),
            audio_enabled=bool(
                self.settings_manager.get('audio_capture_enabled', True)),
            encoder=str(self.settings_manager.get('encoder_pref', 'auto')),
            # Kept in the engine launch ABI, permanently disabled. The new
            # audio capture mode is carried in its own argument below.
            multiband_enabled=False,
            separate_audio_enabled=(normalize_audio_capture_mode(
                self.settings_manager.get('audio_capture_mode',
                                          AUDIO_CAPTURE_MODE_COMBINED))
                                    == AUDIO_CAPTURE_MODE_SEPARATED),
            microphone_endpoint_id=str(
                self.settings_manager.get('mic_device_id') or ''),
            # argv[24]: a Windows endpoint ID, or on Linux a PulseAudio sink
            # name from `--list-playback-devices` (empty = default output).
            playback_endpoint_id=str(self.settings_manager.get('playback_device_id') or ''),
            microphone_mono=bool(self.settings_manager.get('mic_downmix_mono', False)) if sys.platform == 'win32' else False,
            normal_clip_seconds=self.clip_duration,
            crop_enabled=crop_enabled,
            crop_x=float(crop.get('x', 0.0)) if crop_enabled else 0.0,
            crop_y=float(crop.get('y', 0.0)) if crop_enabled else 0.0,
            crop_w=float(crop.get('w', 1.0)) if crop_enabled else 1.0,
            crop_h=float(crop.get('h', 1.0)) if crop_enabled else 1.0,
        )

    def _apply_active_audio_state(self, config: CaptureConfig) -> None:
        # Windows captures microphones natively; Python recording is for Linux.
        if sys.platform == 'win32':
            MicRecorder().stop()
            return
        recorder = MicRecorder()
        if config.audio_enabled:
            if not recorder.is_running():
                self._start_mic_recorder()
        else:
            recorder.stop()

    # Engine lifecycle

    @staticmethod
    def _diagnostic_capture_config(config: CaptureConfig) -> dict:
        """Serialize requested/actual settings without inventing native facts."""
        values = asdict(config)
        return {
            'monitor': values['monitor'] or 'unavailable:not_configured',
            'capture_backend': 'auto',
            'codec': values['codec'],
            'capture_resolution': {
                'width': values['width'], 'height': values['height'],
            },
            'output_resolution': {
                'width': values['width'], 'height': values['height'],
            },
            'fps': values['fps'],
            'bitrate_kbps': values['bitrate_kbps'],
            'replay_duration_seconds': values['buffer_seconds'],
            'normal_clip_seconds': values['normal_clip_seconds'],
            'encoder_backend': values['encoder'],
            'encoder_adapter': 'auto',
            'audio_enabled': values['audio_enabled'],
            'separate_audio_enabled': values['separate_audio_enabled'],
            'microphone_endpoint': (
                values['microphone_endpoint_id'] or 'system-default'),
            'scaling': values['scaling'],
            'crop_enabled': values['crop_enabled'],
        }

    def _owns_engine_attempt(self, generation: int, process, bridge) -> bool:
        """Check both the attempt token and its exact process/bridge owners."""
        return (
            getattr(self, '_engine_gen', None) == generation
            and getattr(self, 'engine_process', None) is process
            and getattr(self, 'bridge', None) is bridge
        )

    def _schedule_owned_hardware_check(
            self, generation: int, process, bridge) -> None:
        """Defer encoder inspection without letting a stale timer read new state."""
        def check_if_current():
            if not self._owns_engine_attempt(generation, process, bridge):
                return
            self._check_hardware_encoding_status(
                expected_generation=generation,
                expected_process=process,
                expected_bridge=bridge,
            )

        QTimer.singleShot(2000, check_if_current)

    def _advance_recoverable_errors(
            self, *, connected: bool, frame_count: int,
            health_state: CaptureHealthState, backend_generation: int,
            hardware_active: bool = False) -> None:
        """Clear only keyed incidents with stable current-lineage evidence."""
        incidents = getattr(self, '_recoverable_error_incidents', {})
        tracker = getattr(self, '_error_recovery_tracker', None)
        if tracker is None:
            tracker = RecoveryTracker()
            self._error_recovery_tracker = tracker

        active_process_generation = getattr(
            self, '_engine_process_generation', None)
        if active_process_generation is None:
            for key, incident in incidents.items():
                if incident['recovery_kind'] == 'encoder_init':
                    tracker.reset(key)
            return
        recovered = []
        for key, incident in tuple(incidents.items()):
            if incident['recovery_kind'] == 'encoder_init':
                if (active_process_generation
                        <= incident['process_generation']
                        or not connected
                        or hardware_active is not True
                        or health_state is not CaptureHealthState.HEALTHY):
                    tracker.reset(key)
                    continue
            if (incident['recovery_kind'] == 'engine_lineage'
                    and active_process_generation
                    <= incident['process_generation']):
                tracker.reset(key)
                continue
            allow_content_suspect = (
                incident['recovery_kind'] == 'capture_backend')
            is_recovered = tracker.observe(
                key,
                connected=connected,
                process_generation=active_process_generation,
                backend_generation=backend_generation,
                frame_count=frame_count,
                health_state=health_state,
                allow_content_suspect=allow_content_suspect,
            )
            if is_recovered:
                recovered.append((key, incident))

        for key, incident in recovered:
            incidents.pop(key, None)
            error_bar = getattr(self, 'error_bar', None)
            remove_by_key = getattr(error_bar, 'remove_by_key', None)
            if callable(remove_by_key):
                remove_by_key(key)
            emit_event(
                'application', 'failure_recovered', state='RECOVERED',
                compatibility_error=incident['public_error_code'],
                title=incident['title'],
                process_generation=active_process_generation,
                backend_generation=backend_generation,
                recovery_status='stable_progress_observed',
                incident_key=key,
            )

    def connect_to_engine(self) -> bool:
        generation = getattr(self, '_engine_gen', 0)
        process_owner = self.engine_process
        bridge_owner = self.bridge
        max_retries = 10
        for attempt in range(1, max_retries + 1):
            if not self._owns_engine_attempt(
                    generation, process_owner, bridge_owner):
                return False
            connected = bridge_owner.initialize()
            if not self._owns_engine_attempt(
                    generation, process_owner, bridge_owner):
                return False
            if connected:
                print(f"Connected to capture engine (attempt {attempt}).")
                self._set_status('STARTING CAPTURE', status_idle_qss())
                self._schedule_owned_hardware_check(
                    generation, process_owner, bridge_owner)
                return True
            if attempt < max_retries:
                print(f"Connection attempt {attempt}/{max_retries} failed, retrying...")
                self._set_status(f'CONNECTING {attempt}/{max_retries}', status_idle_qss())
                bridge_owner.shutdown()
                if not self._owns_engine_attempt(
                        generation, process_owner, bridge_owner):
                    return False
                self.bridge = CaptureBridge()
                bridge_owner = self.bridge
                time.sleep(0.5)

        print("Bridge connection failed after all retries.")
        self._set_status('DISCONNECTED', status_warning_qss())
        return False

    def start_engine(self) -> bool:
        # This token identifies the launch attempt, not the native capture
        # backend generation reported later through shared memory.
        self._engine_gen = getattr(self, '_engine_gen', 0) + 1
        _my_gen = self._engine_gen
        self._diagnostic_engine_start_count += 1
        capture_restart_count = max(
            0, self._diagnostic_engine_start_count - 1)
        try:
            launch_config = self._requested_capture_config()
        except MonitorSelectionError as monitor_error:
            selection = monitor_error.result
            detail = (f'{selection.reason}. Capture was not started; a canonical '
                      'Windows monitor device path is required.')
            self._capture_config.fail(detail)
            self._restart_pending = False
            self._set_capture_apply_state(False)
            self._set_status('MONITOR UNAVAILABLE', status_warning_qss())
            emit_event(
                'engine', 'start_failed', state='FAILED',
                error=getattr(DiagnosticError, selection.code, selection.code),
                monitor_selection=selection.as_dict(),
                detail=detail)
            self.push_error('CAPTURE FAILED', f'{selection.code}: {detail}',
                            level='error')
            return False
        self._capture_config.request(launch_config)
        self._capture_config.begin_apply()
        requested_diagnostics = self._diagnostic_capture_config(launch_config)
        if self._diagnostics is not None:
            self._diagnostics.update_summary(
                'requested_configuration', requested_diagnostics)
            self._diagnostics.update_summary(
                'adapter_topology',
                build_adapter_chain(
                    None, unavailable_reason='engine_process_not_started'))
        emit_event('engine', 'start_requested', state='REQUESTED',
                   process_generation=_my_gen,
                   requested_configuration=requested_diagnostics,
                   engine_process_start_count=self._diagnostic_engine_start_count,
                   capture_restart_count=capture_restart_count)
        if self._diagnostics is not None:
            self._diagnostics.merge_summary(
                'health',
                engine_process_start_count=self._diagnostic_engine_start_count,
                capture_restart_count=capture_restart_count)
        if not self.engine_path or not self.engine_path.exists():
            print("Engine executable not found.")
            self._set_status('NO ENGINE', status_warning_qss())
            self._capture_config.fail('engine executable not found')
            emit_event(
                'engine', 'start_failed', state='FAILED',
                error=DiagnosticError.ENGINE_START_FAILED,
                compatibility_error='Error 003',
                technical_cause='ENGINE_EXECUTABLE_MISSING',
                technical_subsystem='process', operation='executable_discovery',
                process_generation=_my_gen,
                native_capture_generation=None,
                api_call='engine_executable_discovery',
                detail='engine executable not found')
            self._warn_no_engine(process_generation=_my_gen)
            self._restart_pending = False
            self._set_capture_apply_state(False)
            return False

        # Preserve the legacy raw-capacity budget as a diagnostic input. Public
        # alpha startup refuses hardware failure, but the engine reports how
        # short the old raw history would have been instead of claiming 30/60s.
        actual_w = launch_config.width if launch_config.width else 1920
        actual_h = launch_config.height if launch_config.height else 1080
        bytes_per_frame = actual_w * actual_h * 4
        frames_needed   = launch_config.buffer_seconds * launch_config.fps
        mb_needed       = max(64, (frames_needed * bytes_per_frame + (1024*1024-1)) // (1024*1024))
        max_buffer_mb   = min(int(mb_needed) + 64, 2048)   # hard 2 GB ceiling

        capture_mode    = self.settings_manager.get('capture_mode',    'desktop')
        target_hwnd     = self.settings_manager.get('target_hwnd',     0)
        capture_monitor = launch_config.monitor
        engine_monitor_arg = capture_monitor
        if sys.platform != 'win32':
            # validate_engine_monitor_argument() accepts only Windows device
            # paths. The Linux engine takes an output name: Wayland uses the
            # QScreen name as is; native X11 resolves it through xrandr.
            if is_native_x11_session():
                xrandr = linux_tools.path('xrandr')
                if not xrandr:
                    detail = linux_tools.missing_message('xrandr')
                    self._capture_config.fail(detail)
                    self._restart_pending = False
                    self._set_capture_apply_state(False)
                    self._set_status('MONITOR UNAVAILABLE', status_warning_qss())
                    self.push_error('X11 MONITOR UNAVAILABLE', detail, level='error')
                    return False
                try:
                    x11_target = resolve_x11_capture_target(
                        capture_monitor, xrandr)
                except X11MonitorError as error:
                    detail = str(error)
                    self._capture_config.fail(detail)
                    self._restart_pending = False
                    self._set_capture_apply_state(False)
                    self._set_status('MONITOR UNAVAILABLE', status_warning_qss())
                    self.push_error('X11 MONITOR UNAVAILABLE', detail, level='error')
                    return False
                engine_monitor_arg = x11_target.engine_argument
                capture_monitor = x11_target.output.name
        else:
            try:
                engine_monitor_arg = validate_engine_monitor_argument(
                    engine_monitor_arg)
            except MonitorSelectionError as monitor_error:
                selection = monitor_error.result
                detail = (f'{selection.reason}. Capture was not started; a '
                          'canonical Windows monitor device path is required.')
                self._capture_config.fail(detail)
                self._restart_pending = False
                self._set_capture_apply_state(False)
                self._set_status('MONITOR UNAVAILABLE', status_warning_qss())
                emit_event(
                    'engine', 'start_failed', state='FAILED',
                    error=getattr(DiagnosticError, selection.code,
                                  selection.code),
                    monitor_selection=selection.as_dict(),
                    detail=detail, api_call='native_process_argument_guard')
                self.push_error('CAPTURE FAILED',
                                f'{selection.code}: {detail}', level='error')
                return False
        # target_hwnd comes from user-editable settings.json — never trust it.
        try:
            target_hwnd = int(target_hwnd)
        except (TypeError, ValueError):
            target_hwnd = 0
        mode_arg        = '1' if (capture_mode == 'window' and target_hwnd) else '0'
        hwnd_arg        = str(target_hwnd)

        # 0 = stretch (default), 1 = fit (letterbox/pillarbox)
        scaling_mode = launch_config.scaling
        scale_arg    = '1' if scaling_mode == 'fit' else '0'

        print(f"Starting engine  |  FPS={launch_config.fps}  "
              f"Buffer={launch_config.buffer_seconds}s  "
              f"Bitrate={launch_config.bitrate_kbps}kbps  Pool={max_buffer_mb}MB  "
              f"Scale={scaling_mode}"
              + (f"  Monitor={capture_monitor}" if capture_monitor else ""))
        codec_pref_int = {
            'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3
        }.get(launch_config.codec, 0)
        encoder_pref_int = {
            'auto': 0, 'nvenc': 1, 'amf': 2, 'qsv': 3, 'software': 4
        }.get(launch_config.encoder, 0)
        encoder_preset = launch_config.preset
        # argv[13] is retained for native ABI compatibility only.
        multiband_arg = '0'
        audio_arg = '1' if launch_config.audio_enabled else '0'
        audio_mode_arg = (
            '1' if launch_config.separate_audio_enabled else '0')
        microphone_id_arg = launch_config.microphone_endpoint_id
        try:
            microphone_gain = int(self.settings_manager.get('mic_volume', 100))
        except (TypeError, ValueError):
            microphone_gain = 100
        microphone_gain_arg = str(max(0, min(200, microphone_gain)))
        try:
            # A process/backend restart starts a new replay generation. Keep the
            # one-per-incident recovery budget, but never carry stale buffer age.
            self._capture_health.reset(preserve_recovery_budget=True)
            popen_options = dict(_NO_WINDOW)
            self._close_engine_startup_output()
            engine_log_path = (
                self._diagnostics.engine_log_path if self._diagnostics is not None
                else Path.home() / '.fthr' / 'logs' / 'engine.log')
            self._engine_startup_output = EngineLogCapture(
                engine_log_path, self._diagnostics)
            engine_environment = (
                os.environ.copy() if sys.platform == 'win32'
                # Host library path for the engine and the VA-API driver it
                # loads, not PyInstaller's _internal.
                else linux_runtime.child_process_environment())
            if self._diagnostics is not None:
                engine_environment['FTHR_DIAGNOSTIC_SESSION_ID'] = (
                    self._diagnostics.session_id)
            popen_options.update(
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                env=engine_environment,
            )
            try:
                self.engine_process = subprocess.Popen(
                    [str(self.engine_path),
                     str(launch_config.fps), str(launch_config.buffer_seconds),
                     str(launch_config.width), str(launch_config.height),
                     str(launch_config.bitrate_kbps), str(max_buffer_mb),
                     mode_arg, hwnd_arg, scale_arg, engine_monitor_arg,
                     str(codec_pref_int), str(encoder_preset),
                     multiband_arg, audio_arg,
                     microphone_id_arg, microphone_gain_arg,
                     str(encoder_pref_int),
                     '1' if launch_config.crop_enabled else '0',
                     f'{launch_config.crop_x:.9g}',
                     f'{launch_config.crop_y:.9g}',
                     f'{launch_config.crop_w:.9g}',
                     f'{launch_config.crop_h:.9g}',
                     audio_mode_arg, launch_config.playback_endpoint_id,
                     '1' if launch_config.microphone_mono else '0'],
                    **popen_options
                )
                self._engine_startup_output.attach(self.engine_process.stdout)
            except OSError as error:
                failure = format_engine_launch_failure(
                    error,
                    EngineLaunchContext(
                        selected_monitor_id=str(
                            engine_monitor_arg or 'unavailable:not_configured'),
                        requested_capture_mode=str(capture_mode),
                        requested_encoder=str(launch_config.encoder),
                        codec=str(launch_config.codec),
                    ),
                    api_call=('CreateProcessW' if sys.platform == 'win32'
                              else 'subprocess.Popen'),
                )
                print(f'FTHR_STARTUP_ERROR: {failure.code}: {failure.detail}')
                native_value = getattr(error, 'winerror', None)
                if native_value is None:
                    native_value = getattr(error, 'errno', None)
                native_signed = (int(native_value)
                                 if native_value is not None else None)
                native_unsigned = (native_signed & 0xFFFFFFFF
                                   if native_signed is not None else None)
                emit_event(
                    'engine', 'start_failed', state='FAILED',
                    error=DiagnosticError.ENGINE_START_FAILED,
                    compatibility_error='Error 005',
                    technical_cause='PROCESS_LAUNCH_FAILED',
                    technical_subsystem='process', operation='launch',
                    process_generation=_my_gen,
                    native_capture_generation=None,
                    native_failure={
                        'api_call': ('CreateProcessW' if sys.platform == 'win32'
                                     else 'subprocess.Popen'),
                        'native_error_signed': native_signed,
                        'native_error_unsigned': native_unsigned,
                        'native_error_hex': (
                            f'0x{native_unsigned:08X}'
                            if native_unsigned is not None else None),
                        'win32_error_decimal': (
                            native_signed if getattr(error, 'winerror', None)
                            is not None else None),
                        'symbolic_error': (
                            'ERROR_SYSTEM_INTEGRITY_POLICY_VIOLATION'
                            if getattr(error, 'winerror', None) == 4551 else None),
                        'system_message': str(error),
                    },
                    startup_context={
                        'selected_monitor_id': (
                            engine_monitor_arg or 'unavailable:not_configured'),
                        'dxgi_output': 'unavailable:engine_process_not_started',
                        'owning_adapter_luid': (
                            'unavailable:engine_process_not_started'),
                        'capture_device_adapter_luid': (
                            'unavailable:engine_process_not_started'),
                        'encoder_adapter_luid': (
                            'unavailable:engine_process_not_started'),
                        'capture_backend': (
                            'unavailable:engine_process_not_started'),
                        'encoder_backend': (
                            'unavailable:engine_process_not_started'),
                        'codec': launch_config.codec,
                    })
                self.stop_engine()
                self._set_status('ERROR', status_warning_qss())
                self.push_error(
                    'ENGINE COULD NOT START',
                    f'{failure.title}: {failure.detail}',
                    level='error',
                    actions=[('RESTART ENGINE', self._restart_capture_engine)],
                    key='engine-launch',
                    dismiss_on_action=False,
                    diagnostic_context={
                        'diagnostic_error': DiagnosticError.ENGINE_START_FAILED,
                        'technical_cause': 'PROCESS_LAUNCH_FAILED',
                        'technical_subsystem': 'process',
                        'operation': 'launch',
                        'process_generation': _my_gen,
                        'backend_generation': None,
                        'recovery_kind': 'engine_lineage',
                        'recovery_status': 'awaiting_replacement_engine',
                        'native_error': native_signed,
                        'win32_error': getattr(error, 'winerror', None),
                    },
                )
                self._capture_config.fail(failure.detail)
                self._restart_pending = False
                self._set_capture_apply_state(False)
                return False
            # Poll for up to 10 seconds off the Qt thread during hardware startup.
            # Dispatch UI changes through _ui_call. A generation check prevents an old
            # polling worker from disconnecting a replacement engine after a restart.
            process_owner = self.engine_process
            bridge_owner = getattr(self, 'bridge', None)
            startup_owner = getattr(self, '_engine_startup_output', None)
            self._engine_process_generation = _my_gen
            self._engine_handshake_generation = _my_gen

            def _poll_connect():
                for _ in range(60):
                    time.sleep(0.15)
                    if not self._owns_engine_attempt(
                            _my_gen, process_owner, bridge_owner):
                        return   # superseded by a newer start/restart
                    connected = bridge_owner.initialize()
                    if not self._owns_engine_attempt(
                            _my_gen, process_owner, bridge_owner):
                        # Stop/restart may replace the bridge while initialize
                        # is blocked. Release only the captured old mapping.
                        if getattr(self, 'bridge', None) is not bridge_owner:
                            try:
                                bridge_owner.shutdown()
                            except Exception as cleanup_error:
                                print('Old capture bridge cleanup failed: '
                                      f'{cleanup_error}')
                        return
                    if connected:
                        if not self._owns_engine_attempt(
                                _my_gen, process_owner, bridge_owner):
                            return
                        startup_output = self._read_engine_startup_output(
                            startup_owner)
                        if not self._owns_engine_attempt(
                                _my_gen, process_owner, bridge_owner):
                            return
                        startup_warnings = extract_startup_warnings(startup_output)
                        if startup_output.strip():
                            print(startup_output.rstrip())
                        def _on_connected(
                                startup_warnings=startup_warnings):
                            if not self._owns_engine_attempt(
                                    _my_gen, process_owner, bridge_owner):
                                return
                            if self._engine_handshake_generation != _my_gen:
                                return
                            self._engine_handshake_generation = None
                            # Connection proves IPC only. Promote requested to
                            # active after capture-health observes fresh frames.
                            self._pending_launch_config = launch_config
                            self._pending_launch_generation = _my_gen
                            self._set_status('STARTING CAPTURE', status_idle_qss())
                            # Native endpoint discovery is intentionally
                            # deferred until the normal engine is already
                            # running. Settings-page construction must not
                            # launch a second capture executable merely to
                            # enumerate microphones.
                            if self._ui_ready:
                                QTimer.singleShot(
                                    0, lambda: (
                                        self._settings_page_widget
                                        ._populate_mic_devices()
                                        if self._owns_engine_attempt(
                                            _my_gen, process_owner, bridge_owner)
                                        else None))
                            self._schedule_owned_hardware_check(
                                _my_gen, process_owner, bridge_owner)
                            emit_event(
                                'engine', 'handshake_completed', state='CONNECTED',
                                process_generation=_my_gen,
                                native_capture_generation=(
                                    'unavailable:status_not_polled'))
                            for warning in startup_warnings:
                                warning_action = (
                                    ('EDIT GAME CROP', self._toggle_game_detection)
                                    if warning.code.startswith('CROP_') else
                                    ('OPEN AUDIO SETTINGS', self._toggle_settings_page)
                                )
                                self.push_error(
                                    warning.title,
                                    warning.detail,
                                    level='warning',
                                    actions=[warning_action],
                                )
                        self._ui_call.emit(_on_connected)
                        print("Connected to capture engine.")
                        return
                    if process_owner.poll() is not None:
                        break
                # initialize() never succeeded — kill the orphaned process
                if not self._owns_engine_attempt(
                        _my_gen, process_owner, bridge_owner):
                    return   # a newer start owns the engine now — don't kill it
                startup_output = self._read_engine_startup_output(startup_owner)
                if not self._owns_engine_attempt(
                        _my_gen, process_owner, bridge_owner):
                    return
                if startup_output.strip():
                    print(startup_output.rstrip())
                failure = extract_startup_failure(startup_output)
                print(f"Engine did not respond — {failure.code}: "
                      f"{failure.detail}")
                explicit_native_failure = failure.code != 'ENGINE_START_FAILED'
                public_error_code = (
                    'Error 001' if explicit_native_failure else 'Error 004')
                failure_title = (
                    'CAPTURE FAILED' if explicit_native_failure
                    else 'ENGINE NOT RESPONDING')
                technical_error = getattr(
                    DiagnosticError, failure.code,
                    DiagnosticError.ENGINE_HANDSHAKE_TIMEOUT)
                emit_event(
                    'engine', 'handshake_timeout', state='FAILED',
                    error=technical_error,
                    compatibility_error=public_error_code,
                    technical_cause=failure.code,
                    technical_subsystem=('native_backend'
                                         if explicit_native_failure else 'ipc'),
                    operation='startup_handshake',
                    process_generation=_my_gen,
                    native_capture_generation=None,
                    startup_failure={
                        'code': failure.code,
                        'title': failure.title,
                        'detail': failure.detail,
                    })
                def _on_failed():
                    if not self._owns_engine_attempt(
                            _my_gen, process_owner, bridge_owner):
                        return
                    if self._engine_handshake_generation != _my_gen:
                        return
                    self._engine_handshake_generation = None
                    self.stop_engine()
                    self._capture_config.fail(failure.detail)
                    self._restart_pending = False
                    self._set_capture_apply_state(False)
                    self._set_status('DISCONNECTED', status_warning_qss())
                    self.push_error(
                        failure_title,
                        f'{failure.title}: {failure.detail}',
                        level='error',
                        actions=[('RESTART ENGINE', self._restart_capture_engine)],
                        key='engine-launch',
                        dismiss_on_action=False,
                        diagnostic_context={
                            'diagnostic_error': technical_error,
                            'technical_cause': failure.code,
                            'technical_subsystem': (
                                'native_backend' if explicit_native_failure
                                else 'ipc'),
                            'operation': 'startup_handshake',
                            'process_generation': _my_gen,
                            'backend_generation': None,
                            'recovery_kind': (
                                'capture_backend' if explicit_native_failure
                                and failure.code.startswith('CAPTURE_')
                                else 'engine_lineage' if not explicit_native_failure
                                else None),
                            'recovery_status': (
                                'awaiting_replacement_engine'
                                if not explicit_native_failure
                                else 'manual_restart_required'),
                        },
                    )
                    if self._manual_record_state == 'preparing':
                        self._fail_manual_recording(
                            'The recording quality profile could not connect to capture.')
                self._ui_call.emit(_on_failed)

            self._engine_connect_thread = threading.Thread(
                target=_poll_connect, daemon=True, name='fthr-engine-connect')
            self._engine_connect_thread.start()
            return True
        except Exception as e:
            print(f"Engine start error: {e}")
            emit_event(
                'engine', 'start_failed', state='FAILED',
                error=DiagnosticError.ENGINE_START_FAILED,
                compatibility_error='Error 005',
                technical_cause=type(e).__name__,
                technical_subsystem='process', operation='launch',
                process_generation=_my_gen,
                native_capture_generation=None,
                detail=f'{type(e).__name__}: {e}')
            self.stop_engine()
            self._set_status('ERROR', status_warning_qss())
            self.push_error(
                'ENGINE COULD NOT START',
                f'Engine failed to start: {e}',
                level='error',
                actions=[('RESTART ENGINE', self._restart_capture_engine)],
                key='engine-launch',
                dismiss_on_action=False,
                diagnostic_context={
                    'diagnostic_error': DiagnosticError.ENGINE_START_FAILED,
                    'technical_cause': type(e).__name__,
                    'technical_subsystem': 'process',
                    'operation': 'launch',
                    'process_generation': _my_gen,
                    'backend_generation': None,
                    'recovery_kind': 'engine_lineage',
                    'recovery_status': 'awaiting_replacement_engine',
                },
            )
            self._capture_config.fail(str(e))
            self._restart_pending = False
            self._set_capture_apply_state(False)
            return False

    def _read_engine_startup_output(self, capture=None) -> str:
        if capture is None:
            capture = self._engine_startup_output
        if capture is None:
            return ''
        try:
            return capture.startup_text()
        except (OSError, ValueError, AttributeError):
            return ''

    def _close_engine_startup_output(self):
        capture = self._engine_startup_output
        self._engine_startup_output = None
        if capture is not None:
            try:
                capture.close()
            except OSError:
                # The diagnostic stream may already be closed by shutdown.
                pass

    def stop_engine(self):
        # Invalidate queued handshake work before shutdown can block.
        self._engine_handshake_generation = None
        self._engine_gen = getattr(self, '_engine_gen', 0) + 1
        process = self.engine_process
        bridge = getattr(self, 'bridge', None)
        graceful_requested = False
        request_shutdown = getattr(bridge, 'request_engine_shutdown', None)
        if callable(request_shutdown):
            graceful_requested = request_shutdown()
        if process:
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                print('[Lifecycle] Engine graceful shutdown timed out; escalating')
                process.terminate()
                try:
                    process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        print('[Lifecycle] Engine could not be reaped before exit')
            print('[Lifecycle] EngineStopped '
                  f'graceful={graceful_requested} code={process.returncode}')
            emit_event(
                'engine', 'stopped', state='STOPPED',
                graceful=graceful_requested, exit_code=process.returncode)
            if self.engine_process is process:
                self.engine_process = None
            self._engine_process_generation = None
        if bridge:
            bridge.shutdown()
        self._close_engine_startup_output()

    def _restart_capture_engine(self, *, allow_recording_prepare: bool = False):
        manual_state = getattr(self, '_manual_record_state', 'idle')
        if (manual_state != 'idle'
                and not (allow_recording_prepare
                         and manual_state == 'preparing')):
            self.push_error(
                'STOP RECORDING FIRST',
                'Finish the manual recording before restarting capture or '
                'applying encoder settings.',
                level='warning',
            )
            return
        self._set_capture_apply_state(True)
        if hasattr(self, '_save_state') and self._save_state.is_busy():
            # Save completion has its own bounded lifecycle. Do not terminate
            # a live save using the encoder-startup deadline.
            timer = getattr(self, '_capture_apply_timer', None)
            if timer is not None:
                timer.stop()
            self._set_status(self._capture_pending_text(), status_idle_qss())
            if not getattr(self, '_restart_deferred_for_save', False):
                self._restart_deferred_for_save = True
                def _retry_after_save():
                    self._restart_deferred_for_save = False
                    if self._capture_settings_applying:
                        self._restart_capture_engine()
                QTimer.singleShot(500, _retry_after_save)
            return
        # Guard against button spam: each unguarded click would spawn another
        # engine process fighting over the same shared memory.
        if getattr(self, '_restart_pending', False):
            print('[Engine] Restart already in progress — ignored')
            return
        self._restart_pending = True
        timer = getattr(self, '_capture_apply_timer', None)
        if timer is not None:
            timer.start()
        self._set_status(self._capture_pending_text(), status_idle_qss())
        self.stop_engine()
        QTimer.singleShot(1000, self._finish_restart)

    def _maybe_restart_for_display_mode_change(self, status):
        if (sys.platform != 'win32'
                or not status.get('connected')
                or not int(status.get('capture_health_flags', 0)) & int(CaptureHealthFlag.RECONFIGURE_REQUIRED)
                or getattr(self, '_manual_record_state', 'idle') != 'idle'
                or getattr(self, '_restart_pending', False)
                or getattr(self, '_capture_settings_applying', False)):
            return
        generation = getattr(self, '_engine_gen', None)
        if getattr(self, '_display_mode_restart_generation', None) == generation:
            return
        now = time.monotonic()
        attempts = [stamp for stamp in getattr(self, '_display_mode_restart_times', [])
                    if now - stamp < 30]
        if len(attempts) >= 3:
            return
        self._display_mode_restart_times = attempts + [now]
        self._display_mode_restart_generation = generation
        process, bridge = self.engine_process, self.bridge
        def restart():
            if (self._owns_engine_attempt(generation, process, bridge)
                    and getattr(self, '_manual_record_state', 'idle') == 'idle'
                    and not getattr(self, '_restart_pending', False)
                    and int(bridge.get_status().get('capture_health_flags', 0))
                    & int(CaptureHealthFlag.RECONFIGURE_REQUIRED)):
                self._restart_capture_engine()
        QTimer.singleShot(750, restart)

    def _on_encoder_config_changed(self):
        self._set_status('APPLYING SETTINGS', status_idle_qss())
        self._restart_capture_engine()

    def _finish_restart(self):
        try:
            self.bridge = CaptureBridge()
            started = self.start_engine()
        except Exception as error:
            self._capture_config.fail(str(error))
            self._set_status('SETTINGS APPLY FAILED', status_warning_qss())
            self.push_error('SETTINGS APPLY FAILED', str(error), level='error')
            started = False
        if not started:
            self._restart_pending = False
            self._set_capture_apply_state(False)
            if getattr(self, '_manual_record_state', 'idle') == 'preparing':
                self._fail_manual_recording(
                    'The recording quality profile could not be started.')

    # Microphone recorder

    def _start_mic_recorder(self):
        if sys.platform == 'win32':
            # Native WASAPI capture starts with the engine so microphone and
            # video share one generation-local QPC timeline. Do not create a
            # second PortAudio ring that could later post-mix stale audio.
            return
        if not MicRecorder.is_available():
            print('[Mic] sounddevice not installed — mic-in-clips disabled')
            self.push_error(
                'MIC NOT FOUND',
                'No recording device detected. Check audio settings or permissions.',
                level='warning',
                actions=[('OPEN AUDIO SETTINGS', self._toggle_settings_page)],
            )
            return
        device_index = self._resolved_mic_device_index()
        gain = float(self.settings_manager.get('mic_volume', 100)) / 100.0
        if MicRecorder().start(device_index, gain=gain):
            print(f'[Mic] Recorder started (device={device_index}, gain={gain:.2f})')
        else:
            print('[Mic] Recorder failed to start — clips will have no mic audio')
            self.push_error(
                'MIC NOT FOUND',
                'Microphone failed to start. Clips will have no mic audio.',
                level='warning',
                actions=[('OPEN AUDIO SETTINGS', self._toggle_settings_page)],
            )

    def _resolved_mic_device_index(self):
        """Translate the saved mic_device_name into a sounddevice index, or None."""
        if not MicRecorder.is_available():
            return None
        name = self.settings_manager.get('mic_device_name')
        if not name:
            return None
        try:
            import sounddevice as _sd
            if sys.platform.startswith('linux'):
                matches = [
                    i for i, dev in enumerate(_sd.query_devices())
                    if dev.get('max_input_channels', 0) > 0
                    and dev.get('name') == name
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    print('[Mic] Saved microphone name is ambiguous; using system default')
                else:
                    print('[Mic] Saved microphone name was not found; using system default')
                return None
            for i, dev in enumerate(_sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0 and dev.get('name') == name:
                    return i
        except Exception as e:
            # Device enumeration is optional; a missing match uses system default.
            if sys.platform.startswith('linux'):
                print(f'[Mic] Device lookup failed; using system default: {e}')
            pass
        return None

    def _start_gary_mic_recorder(self) -> bool:
        """Start the lightweight PortAudio stream used by Gary on Windows."""
        if not MicRecorder.is_available():
            return False
        device_index = self._resolved_mic_device_index()
        gain = float(self.settings_manager.get('mic_volume', 100)) / 100.0
        if MicRecorder().start(device_index, gain=gain):
            self._gary_recorder_active = True
            print(f'[Gary] Recorder started (device={device_index}, gain={gain:.2f})')
            return True
        print('[Gary] Recorder failed to start')
        return False

    # Gary Mode

    def _sync_gary_settings(self, restart_recorder: bool = True):
        """Pull persisted Gary settings into the live response loop."""
        self._gary_enabled = bool(
            self.settings_manager.get('gary_mode_enabled', False))
        self._gary_min_level, self._gary_max_level = clamp_thresholds(
            self.settings_manager.get('gary_min_level', DEFAULT_MIN_LEVEL),
            self.settings_manager.get('gary_max_level', DEFAULT_MAX_LEVEL),
        )
        if not hasattr(self, '_gary_overlay'):
            return

        self._gary_overlay.set_image(
            self.settings_manager.get('gary_image_path'))
        audio_enabled = bool(
            self.settings_manager.get('audio_capture_enabled', True))
        should_run = self._gary_enabled and audio_enabled
        if should_run:
            recorder_running = (MicRecorder.is_available()
                                 and MicRecorder().is_running())
            if restart_recorder or not recorder_running:
                if sys.platform == 'win32':
                    if recorder_running and self._gary_recorder_active:
                        MicRecorder().stop()
                    self._start_gary_mic_recorder()
                elif not recorder_running:
                    self._start_mic_recorder()
        else:
            self._gary_current_intensity = 0.0
            self._gary_overlay.set_intensity(0.0)
            if self._gary_recorder_active and MicRecorder.is_available():
                MicRecorder().stop()
                self._gary_recorder_active = False

    def _update_gary_mode(self):
        audio_enabled = bool(
            self.settings_manager.get('audio_capture_enabled', True))
        if (not self._gary_enabled or not audio_enabled
                or not MicRecorder.is_available()):
            if self._gary_current_intensity:
                self._gary_current_intensity = 0.0
                self._gary_overlay.set_intensity(0.0)
            return

        level = MicRecorder().latest_level()
        target = intensity_for_level(
            level, self._gary_min_level, self._gary_max_level)
        self._gary_current_intensity = step_intensity(
            self._gary_current_intensity, target)
        self._gary_overlay.set_intensity(self._gary_current_intensity)

    # Manual recording

    @staticmethod
    def _manual_record_button_qss(active: bool) -> str:
        color = Colors.TEXT if active else Colors.BG
        border = Colors.ERROR if active else Colors.ACCENT
        background = Colors.DELETE if active else Colors.ACCENT
        return f'''
            QPushButton {{
                background-color: {background};
                border: 1px solid {border};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {color};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_H3}px;
                font-weight: 600;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                padding: 0 12px;
            }}
            QPushButton:hover {{
                background-color: {Colors.ERROR_SOFT if active else Colors.ACCENT_SOFT};
                color: {Colors.ERROR if active else Colors.ACCENT};
                border-color: {Colors.ERROR if active else Colors.ACCENT};
            }}
            QPushButton:pressed {{ background-color: {Colors.DELETE if active else Colors.ACCENT_DIM}; }}
            QPushButton:focus {{ border: 2px solid {Colors.TEXT}; }}
            QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; }}
        '''

    def _toggle_manual_recording(self):
        if self._manual_record_state == 'idle':
            self._start_manual_recording()
        elif self._manual_record_state == 'recording':
            self._stop_manual_recording()

    def _start_manual_recording(self):
        if sys.platform != 'win32':
            # The Linux engine has no continuous recorder; never ask it for a
            # recording it cannot write.
            return
        if self._save_state.is_busy():
            self.push_error(
                'WAIT FOR CLIP SAVE',
                'Start the manual recording after the current replay clip finishes saving.',
                level='warning',
            )
            return
        if not self.bridge or not self.bridge.is_connected():
            self.push_error(
                'RECORDING COULD NOT START',
                'The capture engine is not connected.',
                level='error',
                actions=[('RESTART CAPTURE', self._restart_capture_engine)],
            )
            return
        target_dir = recording_directory_from(self.settings_manager)
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(target_dir).free
        except OSError as exc:
            self.push_error(
                'RECORDING FOLDER UNAVAILABLE', str(exc), level='error')
            return
        if free_bytes < 512 * 1024 * 1024:
            self.push_error(
                'NOT ENOUGH DISK SPACE',
                'Manual recording requires at least 512 MB free before it starts.',
                level='error',
            )
            return

        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        final_path = target_dir / f'FTHR_recording_{timestamp}.mp4'
        suffix = 2
        while final_path.exists():
            final_path = target_dir / f'FTHR_recording_{timestamp}_{suffix}.mp4'
            suffix += 1
        if len(str(final_path)) > 255:
            self.push_error(
                'RECORDING PATH TOO LONG',
                'Choose a shorter recording folder; the native engine accepts '
                'paths up to 255 characters.',
                level='error',
            )
            return

        self._pending_manual_record_path = final_path
        desired = self._requested_capture_config('recording')
        active = self._capture_config.active
        quality_fields = ('fps', 'width', 'height', 'bitrate_kbps')
        needs_recording_profile = (
            active is None or any(
                getattr(active, field) != getattr(desired, field)
                for field in quality_fields))
        if needs_recording_profile:
            self._engine_profile = 'recording'
            self._manual_record_state = 'preparing'
            self.record_btn.setEnabled(False)
            self.record_btn.setText('Preparing…')
            set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(True)))
            self._set_status('PREPARING RECORDING QUALITY', status_idle_qss())
            self._restart_capture_engine(allow_recording_prepare=True)
            return
        self._submit_manual_recording_start()

    def _continue_prepared_manual_recording(self):
        if self._manual_record_state != 'preparing':
            return
        active = self._capture_config.active
        desired = self._requested_capture_config('recording')
        quality_fields = ('fps', 'width', 'height', 'bitrate_kbps')
        if (active is None or any(
                getattr(active, field) != getattr(desired, field)
                for field in quality_fields)):
            if not getattr(self, '_restart_pending', False):
                self._engine_profile = 'recording'
                self._restart_capture_engine(allow_recording_prepare=True)
            return
        self._submit_manual_recording_start()

    def _submit_manual_recording_start(self):
        final_path = self._pending_manual_record_path
        if final_path is None:
            self._fail_manual_recording(
                'The recording destination was lost before capture started.')
            return
        # Clear only a stale recording response. Replay-save responses belong
        # to their own state machine and are never touched here.
        self.bridge.consume_manual_recording_response()
        if not self.bridge.start_manual_recording(str(final_path)):
            self._fail_manual_recording(
                'The capture engine rejected the recording command.')
            return
        self._manual_record_state = 'starting'
        self._manual_record_requested_at = time.monotonic()
        # Fragmented MP4 is written directly to its destination. There is no
        # staging tree, raw-audio sidecar, or whole-file remux to lose at Stop.
        self._manual_record_path = final_path
        self._pending_manual_record_path = None
        self.record_btn.setEnabled(False)
        self.record_btn.setText('Starting…')
        set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(True)))
        self._set_status('STARTING RECORDING', status_idle_qss())
        self._manual_record_timer.start()

    def _stop_manual_recording(self):
        if self._manual_record_state != 'recording':
            return
        if not self.bridge.stop_manual_recording():
            self.push_error(
                'RECORDING COULD NOT STOP',
                'The capture engine is unavailable. FTHR will try to recover '
                'the staged recording during shutdown.',
                level='error',
            )
            return
        self._manual_record_state = 'stopping'
        self._manual_record_requested_at = time.monotonic()
        self.record_btn.setEnabled(False)
        self.record_btn.setText('Finalizing…')
        self._set_status('FINALIZING RECORDING', status_idle_qss())

    @staticmethod
    def _format_recording_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'

    def _pump_manual_recording_responses(self):
        state = self._manual_record_state
        if state == 'idle':
            self._manual_record_timer.stop()
            return
        response = self.bridge.peek_manual_recording_response()
        if response is not None:
            kind, detail = response
            self.bridge.consume_manual_recording_response()
            if kind == 'error':
                recording = self._manual_record_path
                keep_recording = False
                if state != 'starting' and recording is not None:
                    try:
                        keep_recording = (
                            recording.is_file()
                            and recording.stat().st_size > 0)
                    except OSError:
                        # A concurrently removed failed recording cannot be kept.
                        pass
                self._fail_manual_recording(
                    detail or 'The recording writer closed unexpectedly.',
                    keep_recording=keep_recording)
                return
            if kind == 'started' and state == 'starting':
                self._mark_manual_recording_started()
                state = self._manual_record_state
            elif kind == 'stopped' and state in ('stopping', 'starting'):
                self._finalize_manual_recording_file()
                return

        now = time.monotonic()
        status = self.bridge.get_status()
        native_recording = bool(status.get('is_recording', False))
        if state == 'starting':
            if native_recording:
                self._mark_manual_recording_started()
                return
            if now - self._manual_record_requested_at > 10.0:
                self._fail_manual_recording(
                    'The capture engine did not acknowledge the start request.')
            return
        if state == 'recording':
            elapsed = now - self._manual_record_started_at
            self.record_btn.setText(
                f'Stop  {self._format_recording_elapsed(elapsed)}')
            recording = self._manual_record_path
            try:
                if not native_recording and elapsed > 0.75:
                    # A writer-side failure closes the fragmented file before
                    # dropping this flag. Route a STOP command through the
                    # native command loop so its detailed close result is the
                    # only publication boundary.
                    if self.bridge.stop_manual_recording():
                        self._manual_record_state = 'stopping'
                        self._manual_record_requested_at = now
                        self.record_btn.setEnabled(False)
                        self.record_btn.setText('Recovering…')
                    else:
                        self._fail_manual_recording(
                            'The recording writer stopped unexpectedly.',
                            keep_recording=(recording is not None))
                    return
                if (recording is not None
                        and shutil.disk_usage(recording.parent).free
                        < 256 * 1024 * 1024):
                    self.push_error(
                        'RECORDING STOPPED — LOW DISK SPACE',
                        'Less than 256 MB remained, so FTHR stopped safely.',
                        level='warning',
                    )
                    self._stop_manual_recording()
            except OSError:
                self._stop_manual_recording()
            return
        if state == 'stopping':
            # StopRecording clears the native is_recording flag before its
            # encoder thread drains queued packets, writes the MP4 trailer,
            # and closes the file. RECORDING_STOPPED is published only after
            # all of that work completes, so it is the publication boundary.
            # Probing as soon as is_recording became false raced the open MP4
            # and produced the misleading "no video stream" failure.
            if (now - self._manual_record_requested_at
                    > self._MANUAL_RECORDING_STOP_TIMEOUT_SECONDS):
                self._fail_manual_recording(
                    'The capture engine did not finish closing the manual '
                    'recording. Its completed fragments were kept.',
                    keep_recording=True,
                )

    def _mark_manual_recording_started(self):
        self._manual_record_state = 'recording'
        self._manual_record_started_at = time.monotonic()
        self.record_btn.setEnabled(True)
        self.record_btn.setText('Stop  00:00:00')
        self.record_btn.setToolTip(
            'Stop and finalize the current manual recording')
        set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(True)))
        self._set_status('RECORDING', status_active_qss())

    def _restore_clip_capture_profile(self):
        geometry_restart = bool(
            getattr(self, '_pending_game_geometry_restart', False))
        if getattr(self, '_engine_profile', 'clips') != 'recording':
            if not geometry_restart or getattr(self, '_shutdown_complete', False):
                return
            self._pending_game_geometry_restart = False
            QTimer.singleShot(0, self._restart_capture_engine)
            return
        self._engine_profile = 'clips'
        if getattr(self, '_shutdown_complete', False):
            return
        self._pending_game_geometry_restart = False
        QTimer.singleShot(0, self._restart_capture_engine)

    def _finalize_manual_recording_file(self, *, publish_ui: bool = True):
        recording = self._manual_record_path
        self._manual_record_timer.stop()
        if self._manual_record_state == 'finalizing':
            return
        if recording is None:
            self._fail_manual_recording('The recording destination was lost.')
            return
        try:
            if not recording.is_file() or recording.stat().st_size == 0:
                raise OSError('The recorder produced no playable video fragments.')
        except OSError as exc:
            self._fail_manual_recording(str(exc), keep_recording=True)
            return
        self._manual_record_state = 'finalizing'
        if hasattr(self, 'record_btn'):
            self.record_btn.setEnabled(False)
            self.record_btn.setText('Finalizing…')
        if not publish_ui:
            try:
                probe_media(recording)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                self._fail_manual_recording(
                    str(exc), keep_recording=True,
                    expected_recording=recording)
                return
            self._complete_manual_recording_file(
                recording, publish_ui=False)
            return

        def _worker():
            try:
                # Validation is the only close-time work. Video and AAC are
                # already in the destination file, fragment by fragment.
                probe_media(recording)
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                detail = str(exc)
                self._ui_call.emit(
                    lambda detail=detail: self._fail_manual_recording(
                        detail, keep_recording=True,
                        expected_recording=recording))
                return
            self._ui_call.emit(
                lambda: self._complete_manual_recording_file(
                    recording, publish_ui=True))

        self._manual_record_finalize_thread = threading.Thread(
            target=_worker, name='ManualRecordingValidate', daemon=True)
        self._manual_record_finalize_thread.start()

    def _complete_manual_recording_file(
            self, recording: Path, *, publish_ui: bool):
        if self._manual_record_path != recording:
            return
        self._manual_record_state = 'idle'
        self._manual_record_path = None
        self._pending_manual_record_path = None
        self._manual_record_finalize_thread = None
        if hasattr(self, 'record_btn'):
            self.record_btn.setEnabled(sys.platform == 'win32')
            self.record_btn.setText('Record')
            self.record_btn.setToolTip(
                'Start a manual recording while replay capture stays active')
            set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(False)))
        if publish_ui:
            if hasattr(self, 'clip_grid'):
                self.clip_grid.force_refresh()
            self._set_status('RECORDING SAVED', status_active_qss())
            QTimer.singleShot(2500, self._update_status)
            self.capture_card.show_recording_saved(recording.name)
        self._restore_clip_capture_profile()

    def _fail_manual_recording(
            self, detail: str, *, keep_recording: bool = False,
            expected_recording: Path | None = None):
        if (expected_recording is not None
                and self._manual_record_path != expected_recording):
            return
        recording = self._manual_record_path
        self._manual_record_timer.stop()
        self._manual_record_state = 'idle'
        self._manual_record_path = None
        self._pending_manual_record_path = None
        self._manual_record_finalize_thread = None
        if recording is not None and not keep_recording:
            try:
                recording.unlink(missing_ok=True)
            except OSError:
                # Never make an already-written fragment less recoverable.
                pass
        if hasattr(self, 'record_btn'):
            self.record_btn.setEnabled(sys.platform == 'win32')
            self.record_btn.setText('Record')
            set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(False)))
        self._set_status('RECORDING FAILED', status_warning_qss())
        if keep_recording and recording is not None:
            detail = (
                f'{detail} Recoverable video fragments remain in '
                f'{recording.name}.')
        self.push_error(
            'MANUAL RECORDING FAILED', detail, level='error',
            actions=[('OPEN RECORDINGS', self._open_recordings_folder)],
        )
        self._restore_clip_capture_profile()

    def _open_recordings_folder(self):
        try:
            folder = ensure_storage_directory(
                recording_directory_from(self.settings_manager))
            if sys.platform != 'win32':
                self._open_folder_linux(folder)
                return
            os.startfile(str(folder))
        except OSError as error:
            self.push_error(
                'RECORDING FOLDER UNAVAILABLE', str(error), level='error')

    # Save clip

    def _save_clip(self, duration_seconds: int = 30):
        if self._manual_record_state in ('preparing', 'starting', 'stopping'):
            self._show_save_busy_feedback()
            return
        is_connected = getattr(self.bridge, 'is_connected', lambda: False)
        if not self.bridge or not is_connected():
            self.push_error(
                'CAPTURE UNAVAILABLE',
                'The capture engine is not connected, so there is no replay buffer '
                'available to save.',
                level='error',
            )
            return

        admission = evaluate_save_admission(
            self._capture_health_snapshot, duration_seconds)
        if not admission.allowed:
            self.capture_card.show_error()
            self.push_error(
                'CLIP NOT SAVED',
                admission.reason,
                level='error',
                actions=[('RESTART CAPTURE', self._restart_capture_engine)],
            )
            self._capture_health_log.warning(
                'Save rejected by capture health: state=%s reason=%s',
                (self._capture_health_snapshot.state.value
                 if self._capture_health_snapshot else 'unknown'),
                admission.reason,
            )
            return
        duration_seconds = admission.duration_seconds

        # Debounce saves to avoid timestamped filename collisions, then enforce
        # one in-flight request: the response slot has no correlation ID. Report
        # refusals so a dropped hotkey is visible to the user.
        now = time.monotonic()
        if now - getattr(self, '_last_save_request', 0.0) < 1.0:
            print('[Save] Ignored — save already in progress (spam guard)')
            self._show_save_busy_feedback()
            return
        if self._save_state.is_busy():
            print(f'[Save] Ignored — engine save still in flight '
                  f'({self._save_state.state.value})')
            self._show_save_busy_feedback()
            return
        self._last_save_request = now

        timestamp    = datetime.now().strftime('%d%b%Y_%H-%M-%S')
        try:
            clips_root = clips_directory_from(self.settings_manager)
        except StoragePathError as error:
            self.capture_card.show_error()
            self.push_error('CLIP SAVE FAILED', str(error), level='error')
            return
        capture_mode = self.settings_manager.get('capture_mode', 'desktop')

        if capture_mode == 'window':
            if hasattr(self, 'source_popup'):
                idx = self.source_popup.window_combo.currentIndex()
                if 0 <= idx < len(self.source_popup._window_list):
                    raw_name = self.source_popup._window_list[idx]['display_name']
                else:
                    raw_name = 'Unknown'
            else:
                # Background replay has no source popup.  Its launch settings
                # remain the capture authority, including the saved window name.
                raw_name = self.settings_manager.get('target_window_name', 'Unknown')
            game_name    = _sanitize_foldername(raw_name)
            clips_folder = clips_root / game_name
            filename     = f'{game_name}_clip_from_{timestamp}.mp4'
        else:
            clips_folder = clips_root / 'Desktop'
            filename     = f'desktop_clip_from_{timestamp}.mp4'

        try:
            clips_folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Read-only home / OneDrive lock / full disk — without this the
            # exception escapes the hotkey slot and the user sees nothing.
            self.capture_card.show_error()
            self.push_error(
                'CLIP SAVE FAILED',
                f'Cannot create {clips_folder}: {e}',
                level='error',
            )
            return
        output_path  = clips_folder / filename
        # Belt and suspenders: never overwrite an existing clip.
        _base_stem = output_path.stem
        n = 2
        while output_path.exists():
            output_path = clips_folder / f'{_base_stem}_{n}.mp4'
            n += 1
        # The Windows shared-memory field is c_wchar * 256, so the bridge
        # refuses anything longer. Catch it here instead: from inside the
        # bridge the failure is indistinguishable from a write error, and the
        # user got told to check disk space while the real cause was a deep
        # folder (a long game name under a long user profile path is enough).
        if sys.platform == 'win32' and len(str(output_path)) > 255:
            self.capture_card.show_error()
            self.push_error(
                'CLIP PATH TOO LONG',
                f'The target path is {len(str(output_path))} characters; the '
                'capture engine accepts at most 255. Choose a shorter clip '
                'folder or a shorter capture-source name.',
                level='error',
                actions=[('OPEN FOLDER', self._open_clips_folder)],
            )
            return

        print(f"Saving clip: {output_path.name}  ({duration_seconds}s)")
        self._save_diagnostic_started_at = time.monotonic()
        emit_event(
            'clip_save', 'save_requested', state='REQUESTED',
            requested_duration_seconds=duration_seconds,
            capture_mode=capture_mode)

        # Use the accepted save instant as the shared end boundary for engine
        # and microphone rings, so post-processing extracts the matching interval.
        mic_end_time = time.monotonic()
        crop_profile = None
        getattr(self, '_active_game_window', None)
        pass

        try:
            # Consume the previous response before submitting another save, so its
            # completion can't be attributed to the new request.
            self._pump_save_responses()

            if not self.bridge.save_clip(str(output_path), duration_seconds):
                emit_event(
                    'clip_save', 'command_rejected', state='FAILED',
                    error=DiagnosticError.CLIP_SAVE_FAILED,
                    elapsed_ms=round(
                        (time.monotonic() - self._save_diagnostic_started_at) * 1000),
                    detail='capture engine did not accept the save command')
                self.capture_card.show_error()
                self.push_error(
                    'CLIP SAVE FAILED',
                    'The capture engine did not accept the save command. '
                    f'Check disk space at {clips_root}.',
                    level='error',
                    actions=[('OPEN FOLDER', self._open_clips_folder)],
                )
                return

            # Submitted — NOT saved. Everything that asserts a file exists
            # (grid entry, clip_saved, upload, "SAVED") still waits for
            # CLIP_SAVED in _on_save_outcome(). The capture card is different:
            # it acknowledges the user's capture action as soon as the command
            # is handed off, so slow disk/mux work cannot delay feedback.
            submit = self._save_state.submit(
                str(output_path), duration_seconds, time.monotonic(),
                mic_end_time=mic_end_time,
                crop_profile=crop_profile,
            )
            if not submit.accepted:
                # is_busy() was checked above; losing the race means another
                # save slipped in. Refuse loudly rather than track two.
                print('[Save] Submit rejected by state machine after the '
                      'command was written — response will be attributed to '
                      'the operation already in flight')
                return

            self._clip_diagnostic_started_by_path[
                os.path.normcase(os.path.abspath(str(output_path)))] = (
                    self._save_diagnostic_started_at)

            self._show_clip_captured_feedback(duration_seconds)
            emit_event(
                'clip_save', 'command_submitted', state='SUBMITTED',
                elapsed_ms=round(
                    (time.monotonic() - self._save_diagnostic_started_at) * 1000))
            self._set_status('SAVING', status_idle_qss())
            self._save_poll_timer.start()

        except Exception as e:
            print(f"Save error: {e}")
            emit_event(
                'clip_save', 'save_failed', state='FAILED',
                error=DiagnosticError.CLIP_SAVE_FAILED,
                elapsed_ms=round(
                    (time.monotonic() - self._save_diagnostic_started_at) * 1000),
                detail=f'{type(e).__name__}: {e}')
            self.capture_card.show_error()
            self.push_error(
                'CLIP SAVE FAILED',
                f'Unexpected error: {e}',
                level='error',
            )

    def _show_clip_captured_feedback(self, duration_seconds: int) -> None:
        """Show immediate feedback through the detached notification process.

        This acknowledges the request; a later failure replaces it in _on_save_outcome.
        """
        config = self._capture_config.active
        fps = config.fps if config else self.capture_fps
        width = config.width if config else self.capture_width
        height = config.height if config else self.capture_height
        self.capture_card.show_clip(
            duration_seconds, fps, _dims_to_label(width, height))

    def _show_save_busy_feedback(self):
        """Visible answer to a hotkey we refused. Never confirms a clip."""
        self._set_status('SAVING', status_idle_qss())
        QTimer.singleShot(1000, self._update_status)

    # The save response channel — single reader, single interpreter

    def _pump_save_responses(self):
        """Read one save response, interpret it, then consume it.

        This poller alone consumes save events and reads each code with its detail.
        """
        if not self.bridge or not self.bridge.is_connected():
            return
        peeked = self.bridge.peek_save_response()
        now = time.monotonic()

        if peeked is None:
            outcome = self._save_state.on_tick(now)
            if outcome is not None:
                self._on_save_outcome(outcome)
            return

        kind, detail = peeked
        event = {
            'started': EngineEvent.SAVE_STARTED,
            'saved':   EngineEvent.CLIP_SAVED,
            'error':   EngineEvent.ERROR_OCCURRED,
        }[kind]
        outcome = self._save_state.on_event(event, detail, now)
        # Consume only now: the event has been interpreted and attributed.
        self.bridge.consume_save_response(kind)
        if outcome is not None:
            self._on_save_outcome(outcome)

    def _on_save_poll_tick(self):
        """Dedicated short-interval poll while a save is outstanding.

        Cheap by construction: a handful of shared-memory reads and no I/O.
        It must never sleep, wait on a subprocess, join a thread or touch the
        encoder — that is the whole point of AUDIT-011.
        """
        self._pump_save_responses()
        if not self._save_state.is_busy():
            # Nothing left in flight. A timed-out operation keeps its late
            # window open via the 500 ms status poll, which also calls
            # _pump_save_responses(), so stopping the fast timer here does not
            # lose a late result.
            self._save_poll_timer.stop()

    def _on_save_outcome(self, outcome):
        """Act on exactly one state-machine outcome."""
        op = outcome.operation

        if outcome.kind is OutcomeKind.ACCEPTED:
            # Engine has the job. Still not saved — no grid, no upload.
            self._set_status('SAVING', status_idle_qss())
            emit_event(
                'clip_save', 'engine_accepted', state='PROCESSING',
                operation_id=op.op_id,
                elapsed_ms=round(op.ack_latency_ms or 0))
            return

        if outcome.kind is OutcomeKind.TIMEOUT_NOTICE:
            # Not a verdict: the engine is slow, not proven broken. Warn, keep
            # watching, and do not emit a failure the late result would then
            # contradict.
            self._set_status('SAVE SLOW…', status_warning_qss())
            emit_event(
                'clip_save', 'save_slow', state='STALLED',
                operation_id=op.op_id,
                elapsed_ms=round((time.monotonic() - op.requested_at) * 1000))
            return

        if outcome.kind is OutcomeKind.FAILED:
            emit_event(
                'clip_save', 'save_failed', state='FAILED',
                error=DiagnosticError.CLIP_SAVE_FAILED,
                operation_id=op.op_id,
                elapsed_ms=round(op.total_latency_ms or
                                 (time.monotonic() - op.requested_at) * 1000),
                detail=outcome.detail or 'native clip save failed')
            self.capture_card.show_error()
            self._set_status('SAVE FAILED', status_warning_qss())
            QTimer.singleShot(3000, self._update_status)
            self.push_error(
                'CLIP WAS NOT SAVED',
                outcome.detail or
                'The capture engine failed while writing the clip. '
                'Check free disk space and the configured clips storage folder.',
                level='error',
                actions=[('OPEN FOLDER', self._open_clips_folder)],
                diagnostic_context={
                    'diagnostic_error': DiagnosticError.CLIP_SAVE_FAILED,
                    'technical_cause': 'NATIVE_SAVE_FAILURE',
                    'technical_subsystem': 'clip_save',
                    'operation': 'write_clip',
                    'operation_id': op.op_id,
                    'process_generation': getattr(
                        self, '_engine_process_generation', None),
                    'backend_generation': getattr(
                        self, '_last_native_capture_generation', None),
                    'recovery_status': 'terminal_not_recoverable',
                },
            )
            return

        if outcome.kind is OutcomeKind.COMPLETED:
            emit_event(
                'clip_save', 'mux_completed', state='WRITTEN',
                operation_id=op.op_id,
                elapsed_ms=round(op.total_latency_ms or
                                 (time.monotonic() - op.requested_at) * 1000),
                late=outcome.late)
            self._on_clip_written(op, late=outcome.late)

    def _on_clip_written(self, op, late: bool = False):
        """CLIP_SAVED for `op`. Runs exactly once per operation.

        The state machine guarantees single delivery (`result_emitted`), so the
        post-processing route below is started once and only once.
        """
        output_path = Path(op.output_path)

        if not is_completed_video_path(output_path):
            print(f'[Save] Refused non-final clip path from engine: {output_path.name}')
            self.capture_card.show_error()
            self._set_status('SAVE FAILED', status_warning_qss())
            self.push_error(
                'CLIP WAS NOT SAVED',
                'The capture engine returned an incomplete clip path. The file '
                'was not added to the library or post-processing pipeline.',
                level='error',
                diagnostic_context={
                    'diagnostic_error': DiagnosticError.CLIP_SAVE_FAILED,
                    'technical_cause': 'INCOMPLETE_OUTPUT_PATH',
                    'technical_subsystem': 'clip_save',
                    'operation': 'validate_output_path',
                    'operation_id': op.op_id,
                    'process_generation': getattr(
                        self, '_engine_process_generation', None),
                    'backend_generation': getattr(
                        self, '_last_native_capture_generation', None),
                    'recovery_status': 'terminal_not_recoverable',
                },
            )
            return

        # Verify the saved path before confirming success or adding a library card.
        try:
            if not output_path.exists() or output_path.stat().st_size == 0:
                print(f'[Save] CLIP_SAVED but the file is missing or empty: '
                      f'{output_path.name}')
                self.capture_card.show_error()
                self._set_status('SAVE FAILED', status_warning_qss())
                QTimer.singleShot(3000, self._update_status)
                self.push_error(
                    'CLIP WAS NOT SAVED',
                    'The capture engine reported success but no clip file was '
                    'written. Check free disk space and the configured clips '
                    'storage folder.',
                    level='error',
                    actions=[('OPEN FOLDER', self._open_clips_folder)],
                    diagnostic_context={
                        'diagnostic_error': DiagnosticError.CLIP_SAVE_FAILED,
                        'technical_cause': 'OUTPUT_FILE_MISSING_OR_EMPTY',
                        'technical_subsystem': 'clip_save',
                        'operation': 'verify_output_file',
                        'operation_id': op.op_id,
                        'process_generation': getattr(
                            self, '_engine_process_generation', None),
                        'backend_generation': getattr(
                            self, '_last_native_capture_generation', None),
                        'recovery_status': 'terminal_not_recoverable',
                    },
                )
                return
        except OSError as e:
            print(f'[Save] Could not stat the saved clip: {e}')

        duration_seconds = op.duration_seconds
        mic_end_time = op.context.get('mic_end_time', op.requested_at)
        crop_profile = op.context.get('crop_profile')

        if late:
            print(f'[Save] Late success accepted for {output_path.name} — the '
                  f'timeout warning was premature')

        print(f'Base clip committed: {output_path.name}')
        # Pick the post-processing route. Exactly one runs — see
        # select_post_route() for why that exclusivity is load-bearing.
        active_config = self._capture_config.active
        audio_mode = normalize_audio_capture_mode(
            AUDIO_CAPTURE_MODE_SEPARATED
            if active_config and getattr(
                active_config, 'separate_audio_enabled', False)
            else self.settings_manager.get(
                'audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED))
        audio_enabled = (active_config.audio_enabled if active_config else
                         self.settings_manager.get('audio_capture_enabled', True))
        keyboard_overlay_enabled = third_party_keyboard_settings(
            self.settings_manager)['enabled']
        route, has_async_mux = select_post_route(
            audio_on=audio_enabled,
            multiband_enabled=False,
            mic_running=(sys.platform != 'win32' and MicRecorder.is_available()
                         and MicRecorder().is_running()),
            watermark=self.settings_manager.get('watermark_enabled', False),
            manual_crop=bool(crop_profile),
            camera=any(self.settings_manager.get(key, False) for key in (
                'camera_enabled', 'image_overlay_enabled')),
            keyboard=keyboard_overlay_enabled,
            audio_capture_mode=audio_mode,
            native_audio=(sys.platform == 'win32'
                          and audio_enabled
                          and audio_mode == AUDIO_CAPTURE_MODE_COMBINED),
        )
        emit_event(
            'clip_save', 'post_processing_planned', state='FINALIZING',
            route=route, asynchronous=has_async_mux,
            audio_mode=audio_mode,
            elapsed_ms=round(
                (time.monotonic() - op.requested_at) * 1000))

        # Get clip_ready from the upload manager. Finalization owns this event
        # until the source file is ready for upload.
        clip_ready = self.upload_manager.notify_clip_saved(
            str(output_path), has_mic_mux=has_async_mux)

        # Register readiness before updating the library.  Otherwise the
        # new base file appears ready and can be opened while camera/image/
        # crop post-processing is still rewriting it.  Insert just this card;
        # rebuilding every existing card here made save latency scale with the
        # entire library and did the same expensive work again at publication.
        if hasattr(self, 'clip_grid'):
            try:
                self.clip_grid.upsert_saved_clip(
                    str(output_path),
                    ready=self._clip_readiness.can_access(output_path))
            except Exception as error:
                # Library presentation is post-save enrichment.  A valid base
                # clip must still finish its rewrite/publication path if the
                # card update itself fails.
                emit_event(
                    'library', 'saved_clip_upsert_failed', state='FAILED',
                    error=DiagnosticError.LIBRARY_SCAN_FAILED,
                    phase='engine_committed',
                    detail=f'{type(error).__name__}: {error}')
                print(f'[Save] Incremental library insertion failed: {error}')

        if has_async_mux:
            self._set_status('SAVING', status_idle_qss())

        args = (str(output_path), duration_seconds, mic_end_time, clip_ready,
                crop_profile)
        if route == 'mic':
            self._mux_mic_into_clip(*args, audio_mode=audio_mode)
        else:
            self._finalize_clip(*args)
        if not has_async_mux:
            self._publish_final_clip(str(output_path), duration_seconds)

    def _record_finalization_warning(self, clip_path: str, message: str) -> None:
        self._clip_readiness.record_warning(clip_path, message)

    def _complete_clip_finalization(
            self, clip_path: str, duration_seconds: int,
            *, already_normalized: bool = False) -> None:
        if (not already_normalized
                and self._clip_readiness.state(clip_path)
                is not ClipReadinessState.FINALIZATION_FAILED):
            # Covers the race where a Linux mic route loses its recorder after
            # route selection and completes directly without a worker gate.
            self._normalize_clip_to_cfr(clip_path)
        if self._clip_readiness.state(clip_path) is ClipReadinessState.FINALIZATION_FAILED:
            self._ui_call.emit(
                lambda: self._publish_final_clip(clip_path, duration_seconds))
            return
        try:
            usable = os.path.isfile(clip_path) and os.path.getsize(clip_path) > 0
        except OSError:
            usable = False
        if usable:
            self._clip_readiness.complete(clip_path)
        else:
            self._clip_readiness.finalization_failed(
                clip_path,
                'The final clip file is missing or empty.',
                base_clip_usable=False,
            )
        self._ui_call.emit(
            lambda: self._publish_final_clip(clip_path, duration_seconds))

    def _publish_final_clip(self, clip_path: str, duration_seconds: int) -> None:
        key = os.path.normcase(os.path.abspath(clip_path))
        if key in self._published_final_clips:
            return
        self._published_final_clips.add(key)
        metadata_started = time.monotonic()
        if not self._clip_readiness.can_access(clip_path):
            emit_event(
                'clip_save', 'finalization_failed', state='FAILED',
                error=DiagnosticError.CLIP_FINALIZE_FAILED,
                detail='final clip file is unavailable')
            self._set_status('FINALIZATION FAILED', status_warning_qss())
            self.push_error(
                'CLIP FINALIZATION FAILED',
                'The final clip file is unavailable. The base save was not '
                'reported as a completed clip.',
                level='error',
            )
            return

        warnings = self._clip_readiness.warnings(clip_path)
        metadata_elapsed_ms = round(
            (time.monotonic() - metadata_started) * 1000)
        emit_event(
            'clip_save', 'metadata_validation_completed',
            elapsed_ms=metadata_elapsed_ms, warning_count=len(warnings))
        print(f'Clip ready: {os.path.basename(clip_path)}')
        ui_notification_started = time.monotonic()
        self.clip_saved.emit(clip_path)
        ui_notification_ms = round(
            (time.monotonic() - ui_notification_started) * 1000)
        library_started = time.monotonic()
        library_update_error = None
        if hasattr(self, 'clip_grid'):
            try:
                self.clip_grid.upsert_saved_clip(
                    clip_path,
                    ready=self._clip_readiness.can_access(clip_path))
            except Exception as error:
                library_update_error = f'{type(error).__name__}: {error}'
                emit_event(
                    'library', 'saved_clip_upsert_failed', state='FAILED',
                    error=DiagnosticError.LIBRARY_SCAN_FAILED,
                    phase='final_publication', detail=library_update_error)
                print(f'[Save] Incremental library finalization failed: {error}')
        library_update_ms = round(
            (time.monotonic() - library_started) * 1000)
        started = getattr(
            self, '_clip_diagnostic_started_by_path', {}).pop(key, None)
        emit_event(
            'clip_save', 'finalization_completed', state='COMPLETED',
            requested_duration_seconds=duration_seconds,
            elapsed_ms=(round((time.monotonic() - started) * 1000)
                        if started is not None else None),
            library_refresh_ms=library_update_ms,
            library_update_mode='incremental_saved_clip',
            library_update_error=library_update_error,
            metadata_validation_ms=metadata_elapsed_ms,
            ui_notification_ms=ui_notification_ms,
            warning_count=len(warnings))
        if warnings:
            self._set_status('SAVED WITH WARNING', status_warning_qss())
            self.push_error(
                'CLIP SAVED WITH WARNING',
                warnings[-1],
                level='warning',
            )
        else:
            self._set_status('SAVED', status_active_qss())
        QTimer.singleShot(2000, self._update_status)

    def _open_folder_linux(self, folder) -> None:
        opener = linux_tools.path('xdg-open')
        if not opener:
            print(f'[UI] {linux_tools.missing_message("xdg-open")}')
            return
        subprocess.Popen([opener, str(folder)])

    def _open_clips_folder(self):
        """Open the configured FTHR clips folder in the platform file manager."""
        try:
            folder = ensure_storage_directory(
                clips_directory_from(self.settings_manager))
            if sys.platform != 'win32':
                self._open_folder_linux(folder)
                return
            os.startfile(str(folder))  # os.startfile exists on Windows only
        except OSError as error:
            if isinstance(error, StoragePathError):
                self.push_error('CLIP SAVE FAILED', str(error), level='error')
            print(f'[UI] Could not open clips folder: {error}')

    # Upload finished callback --

    def _on_upload_finished(self, path: str, success: bool, msg: str):
        if success:
            compressed = 'compressed copy' in str(msg).casefold()
            self._set_status(
                'UPLOADED COMPRESSED COPY' if compressed
                else 'UPLOADED ORIGINAL',
                status_active_qss())
            QTimer.singleShot(2000, self._update_status)
            display_name = os.path.basename(path)
            self.capture_card.show_upload(
                f'{display_name} · COMPRESSED COPY' if compressed
                else f'{display_name} · ORIGINAL')
            if compressed:
                self.push_error(
                    'COMPRESSED COPY UPLOADED', str(msg), level='info')
            # Refresh the badge on the matching clip card if it's visible
            widget = (self.clip_grid._thumb_widgets.get(path)
                      if hasattr(self, 'clip_grid') else None)
            if widget:
                widget.set_upload_info(self.upload_manager.get_upload_info(path))
                widget.set_uploaded(True)
        else:
            print(f'[Upload] Failed — {msg}  ({path})')

    def _on_upload_compression_required(
            self, path: str, provider: str, limit_mb: int) -> None:
        """Ask before creating a provider-sized copy; preserve the original."""
        if self._shutdown_requested:
            return
        # Immediate uploads can reach preflight while FTHR is in the tray.
        # Restore first so the required consent prompt is actually visible.
        if not self.isVisible():
            self.restore_main_window()
        try:
            actual_mb = os.path.getsize(path) / (1024 * 1024)
        except OSError:
            actual_mb = 0.0
        provider_name = provider.title()
        answer = FthrMessageDialog.question(
            self,
            'Compress before upload?',
            f'{Path(path).name} is {actual_mb:.1f} MB. {provider_name} accepts '
            f'files up to {limit_mb} MB.\n\nAutomatically compress a copy and '
            'upload it? The original clip will stay unchanged.',
        )
        if answer:
            self.upload_manager.enqueue_compressed_upload(path)

    def _on_upload_compression_progress(
            self, _path: str, percent: int, detail: str) -> None:
        self._set_status(f'COMPRESSING {max(0, min(100, percent))}%', status_idle_qss())
        if percent == 100 and detail:
            print(f'[Upload] Compression plan completed: {detail}')

    def _on_upload_error(
            self, title: str, detail: str, level: str, clip_path: str) -> None:
        detail = str(detail or '').strip()
        if not detail:
            detail = 'The upload service did not provide a failure reason.'
        if title == 'UPLOAD NOT CONFIGURED':
            actions = [('OPEN UPLOAD SETTINGS', self.restore_main_window)]
        elif title == 'UPLOAD FAILED' and clip_path:
            actions = [
                ('RETRY NOW',
                 lambda p=clip_path: self.upload_manager.enqueue_upload(p))]
        else:
            actions = []
        if title == 'UPLOAD FAILED':
            self.capture_card.show_upload_failed(detail)
        self.push_error(
            title,
            detail,
            level='error',
            actions=actions,
        )

    def _on_upload_connection_failed(self, detail: str) -> None:
        detail = str(detail or '').strip()
        if not detail:
            detail = 'The upload provider did not provide a failure reason.'
        self.push_error(
            'UPLOAD CONNECTION FAILED',
            detail,
            level='error',
            actions=[('OPEN UPLOAD SETTINGS', self.restore_main_window)],
        )

    # Mic post-mux --

    @staticmethod
    def _allow_completed_clip_pipeline(clip_path: str, clip_ready=None) -> bool:
        """Reject partial paths before any post-processing worker is started."""
        if is_completed_video_path(clip_path):
            return True
        print(f'[Post] Refused incomplete clip path: {os.path.basename(clip_path)}')
        if clip_ready is not None:
            clip_ready.set()
        return False

    def _mux_mic_into_clip(self, clip_path: str,
                           duration_seconds: int,
                           mic_end_time: float,
                           clip_ready=None, crop_profile=None,
                           *, audio_mode: str | None = None):
        """Finalize audio, then run the remaining source-clip post-processing.

        Windows supplies native system/microphone tracks; Linux supplies Python
        microphone samples for mixing or a separate track. clip_ready gates upload
        until all finalization has finished.
        """
        if not self._allow_completed_clip_pipeline(clip_path, clip_ready):
            return

        audio_mode = normalize_audio_capture_mode(
            audio_mode if audio_mode is not None
            else self.settings_manager.get(
                'audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED))
        if sys.platform == 'win32':
            if audio_mode == AUDIO_CAPTURE_MODE_COMBINED:
                self._spawn_mux_thread(
                    target=self._combine_native_audio_worker,
                    args=(clip_path, duration_seconds, mic_end_time,
                          clip_ready, crop_profile),
                )
            else:
                # Separated Windows clips are already published as distinct
                # native audio streams with a matching FTHR manifest. The
                # normal route selector sends visual-only work to finalize.
                if clip_ready is not None:
                    clip_ready.set()
            return

        if not MicRecorder.is_available() or not MicRecorder().is_running():
            self._record_finalization_warning(
                clip_path, 'Microphone capture stopped before finalization; '
                'the base clip was retained.')
            self._complete_clip_finalization(clip_path, duration_seconds)
            return

        self._spawn_mux_thread(
            target=self._mic_mux_worker,
            args=(clip_path, duration_seconds, mic_end_time, clip_ready,
                  crop_profile,
                  audio_mode == AUDIO_CAPTURE_MODE_SEPARATED),
        )


    def _spawn_mux_thread(self, target, args):
        """Start a tracked post-processing worker (mic mux / finalize).
        Tracked so closeEvent can wait for pending ones — daemon
        threads killed mid-ffmpeg/os.replace leave a corrupt clip behind."""
        if not hasattr(self, '_mux_threads'):
            self._mux_threads = []
        self._mux_threads = [t for t in self._mux_threads if t.is_alive()]

        # All current post-processing entrypoints carry clip_ready at index 3.
        # Wrap it so the upload gate is not released until the final MP4 has
        # also passed the CFR repair. This matters for native-audio early
        # returns, which otherwise could publish a VFR base file before the
        # common completion callback runs.
        worker_args = list(args)
        original_ready = worker_args[3] if len(worker_args) > 3 else None
        if original_ready is not None:
            host = self
            clip_path = str(worker_args[0])

            class _CfrReadyGate:
                def __init__(self):
                    self._released = False
                    self.normalized = False

                def set(self):
                    if self._released:
                        return
                    self._released = True
                    try:
                        host._normalize_clip_to_cfr(clip_path)
                    finally:
                        # The completion callback must not probe/re-encode the
                        # same clip a second time. Keep this true even when
                        # normalization records a failure, so the failure
                        # state remains the single source of truth.
                        self.normalized = True
                        original_ready.set()

                def fail(self):
                    """Release the completion gate without re-probing input."""
                    if self._released:
                        return
                    self._released = True
                    self.normalized = True
                    original_ready.set()

            worker_args[3] = _CfrReadyGate()

        def _run_and_publish():
            source_timeline_rejected = False
            source_timeline_validated = False
            try:
                # Reject a proven sparse native source before a crop/overlay
                # can rewrite its timestamps and before CFR repair could
                # expand the hole into repeated frames. Probe failures are
                # also terminal: an unvalidated source must never enter a
                # timestamp-rewriting or CFR-normalization path.
                if not MainWindow._source_timeline_is_safe(
                        self, str(worker_args[0])):
                    source_timeline_rejected = True
                    if original_ready is not None:
                        worker_args[3].fail()
                    return
                source_timeline_validated = True
                target(*worker_args)
            except Exception as exc:
                worker_clip_path = str(worker_args[0])
                print(f'[Finalize] Unhandled worker error: {exc}')
                if not source_timeline_validated:
                    # A probe can fail before it has recorded readiness. Keep
                    # that exception on the same fail-closed path as missing
                    # or inconclusive probe evidence.
                    source_timeline_rejected = True
                    message = (
                        'Post-processing refused because source timing '
                        f'validation failed: {type(exc).__name__}: {exc}')
                    try:
                        self._clip_readiness.finalization_failed(
                            worker_clip_path, message, base_clip_usable=False)
                        self._record_finalization_warning(
                            worker_clip_path, message)
                    except Exception:
                        # Readiness diagnostics must not reopen media processing.
                        pass
                    if original_ready is not None:
                        worker_args[3].fail()
                self._record_finalization_warning(
                    worker_clip_path, f'Optional clip processing failed: {exc}')
            finally:
                already_normalized = source_timeline_rejected
                if source_timeline_rejected:
                    # The source guard already marked readiness failed and
                    # deliberately skipped the normalization/rewrite path.
                    pass
                elif original_ready is not None:
                    worker_args[3].set()
                    already_normalized = bool(
                        getattr(worker_args[3], 'normalized', False))
                else:
                    self._normalize_clip_to_cfr(str(worker_args[0]))
                    already_normalized = True
                self._complete_clip_finalization(
                    str(worker_args[0]), int(worker_args[1]),
                    already_normalized=already_normalized)

        t = threading.Thread(target=_run_and_publish, daemon=True)
        self._mux_threads.append(t)
        t.start()
        return t

    def _source_timeline_is_safe(self, clip_path: str) -> bool:
        """Return false only when packet PTS proves a sparse source timeline.

        This gate fails closed for unavailable/incomplete probe evidence: a
        source must be proven safe before a crop/overlay can alter evidence.
        It is scoped to saved clips entering this post-processing worker.
        """
        metadata = probe_video_metadata(clip_path)
        fps = metadata.average_fps if metadata is not None else None
        if (metadata is None or fps is None or not math.isfinite(fps)
                or fps <= 0 or metadata.fps_source != 'fthr_frame_rate'):
            message = (
                'Post-processing refused because the source video timing '
                'could not be proven from the saved replay metadata.')
            self._clip_readiness.finalization_failed(
                clip_path, message, base_clip_usable=False)
            emit_event(
                'clip_save', 'source_timeline_inconclusive', state='FAILED',
                error=DiagnosticError.CLIP_FINALIZE_FAILED,
                detail=message)
            self._record_finalization_warning(clip_path, message)
            return False
        evidence = probe_video_cfr_evidence(clip_path, fps)
        if evidence.physical_timeline_bounded is True:
            return True
        message = (
            'Post-processing refused because the source video timeline '
            'was missing or contains a large packet gap; the clip was not '
            'published.')
        self._clip_readiness.finalization_failed(
            clip_path, message, base_clip_usable=False)
        emit_event(
            'clip_save', 'source_timeline_rejected', state='FAILED',
            error=DiagnosticError.CLIP_FINALIZE_FAILED,
            detail=message)
        self._record_finalization_warning(clip_path, message)
        return False

    def _combine_native_audio_worker(self, clip_path: str,
                                     duration_seconds: int,
                                     clip_end_time: float,
                                     clip_ready=None, crop_profile=None):
        """Mix native system and microphone tracks into one combined AAC stream.

        Keep source packets separate during capture to retain their timing.
        """
        try:
            ffmpeg = get_ffmpeg_exe()
            ffprobe = get_ffprobe_exe()
        except FFmpegUnavailable as error:
            print(f'[Audio] {error} — keeping native audio layout')
            self._record_finalization_warning(
                clip_path, 'Combined audio was skipped because FFmpeg is unavailable.')
            if clip_ready is not None:
                clip_ready.set()
            return

        deadline = time.monotonic() + max(duration_seconds * 2, 15)
        last_size = -1
        while time.monotonic() < deadline:
            try:
                if os.path.exists(clip_path):
                    size = os.path.getsize(clip_path)
                    if size > 0 and size == last_size:
                        break
                    last_size = size
            except OSError:
                pass
            time.sleep(0.25)
        else:
            self._record_finalization_warning(
                clip_path, 'Combined audio was skipped because the clip did not stabilize.')
            if clip_ready is not None:
                clip_ready.set()
            return

        def emit_timeline(event: str, path: str) -> None:
            try:
                timeline = probe_clip_av_timeline(ffprobe, path)
            except TimelineProbeError as error:
                emit_event(
                    'clip_save', event, state='UNAVAILABLE',
                    clip_path=clip_path,
                    timeline_time_unit='seconds',
                    detail=str(error)[:512])
                return
            emit_event(
                'clip_save', event, state='OBSERVED',
                clip_path=clip_path,
                **timeline.diagnostic_fields())

        # This is the committed native MP4 before amix decodes/re-encodes its
        # source tracks. Comparing it with the staged output identifies the
        # first finalization boundary that changes relative A/V timing.
        emit_timeline('post_native_mux_av_timeline', clip_path)

        try:
            probe = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'a',
                 '-show_entries', 'stream=index,codec_type:stream_tags=title,handler_name',
                 '-of', 'json', clip_path],
                capture_output=True, text=True, timeout=30, **_NO_WINDOW)
            streams = json.loads(probe.stdout).get('streams', [])
            stream_descriptors = descriptors_from_ffprobe_streams(streams)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError,
                json.JSONDecodeError) as error:
            print(f'[Audio] Could not inspect native audio streams: {error}')
            self._record_finalization_warning(
                clip_path, 'Combined audio was skipped because the clip audio could not be inspected.')
            if clip_ready is not None:
                clip_ready.set()
            return

        manifest = None
        manifest_sidecar = manifest_path_for(clip_path)
        try:
            manifest = read_manifest_for_media(clip_path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            print(f'[Audio] Could not inspect audio manifest: {error}')
        # A present-but-invalid sidecar must not fall back to mixing every
        # stream: that could sum application stems already contained in
        # Default Mix.  Legacy clips without a sidecar retain label/all-stream
        # compatibility behavior in the pure selection policy.
        if manifest is None and manifest_sidecar.exists():
            manifest = {'sources': []}

        selected_audio_indexes = select_combined_audio_streams(
            manifest, stream_descriptors)
        if not selected_audio_indexes:
            self._record_finalization_warning(
                clip_path,
                'Combined audio was skipped because no system or microphone '
                'streams were identified.')
            if clip_ready is not None:
                clip_ready.set()
            return

        audio_count = len(selected_audio_indexes)
        audio_filter = build_combined_audio_filter(selected_audio_indexes)

        with tempfile.TemporaryDirectory(
                prefix='.fthr-audio-', dir=os.path.dirname(clip_path)) as td:
            output = os.path.join(td, 'combined.mp4')
            command = [
                ffmpeg, '-y', '-i', clip_path,
                '-filter_complex', audio_filter,
                '-map', '0:v?', '-map', '[aout]',
                '-map_metadata', '0', '-map_chapters', '0',
                '-movflags', 'use_metadata_tags',
                '-metadata', 'comment=fthr-audio-mode=combined',
                '-metadata:s:a:0', 'title=Combined Audio',
                '-metadata:s:a:0', 'handler_name=Combined Audio',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                '-shortest', output,
            ]
            try:
                result = subprocess.run(
                    command, capture_output=True, timeout=120, **_NO_WINDOW)
            except subprocess.TimeoutExpired:
                print('[Audio] Native audio combine timed out after 120s')
                self._record_finalization_warning(
                    clip_path, 'Combined audio timed out; the native audio layout was retained.')
                return
            except Exception as error:
                print(f'[Audio] Native audio combine failed: {error}')
                self._record_finalization_warning(
                    clip_path, 'Combined audio failed; the native audio layout was retained.')
                return

            if result.returncode != 0:
                detail = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[Audio] Native audio combine ffmpeg failed: '
                      f'{detail[-1] if detail else "(no stderr)"}')
                self._record_finalization_warning(
                    clip_path, 'Combined audio failed; the native audio layout was retained.')
                return

            emit_timeline('post_final_mix_av_timeline', output)

            try:
                os.replace(output, clip_path)
                # The old sidecar described the pre-combine stream topology.
                # Its hash would already make it unreadable, but removing it
                # avoids leaving misleading metadata next to a combined clip.
                manifest_path_for(clip_path).unlink(missing_ok=True)
                print(f'[Audio] Combined {audio_count} native stream(s) into '
                      f'{os.path.basename(clip_path)}')
            except OSError as error:
                print(f'[Audio] Could not publish combined audio clip: {error}')
                self._record_finalization_warning(
                    clip_path, 'Combined audio could not replace the base clip.')
                return

        self._apply_crop(clip_path, ffmpeg, crop_profile)
        self._apply_image_overlay(clip_path, ffmpeg)
        self._apply_keyboard_overlay(
            clip_path, ffmpeg, clip_end_time, duration_seconds)
        self._apply_camera_overlay(clip_path, ffmpeg, clip_end_time, duration_seconds)

    @staticmethod
    def _cfr_video_args(clip_path: str) -> list[str]:
        """Use configured FPS and -fps_mode cfr for visual re-encodes.

        The MP4 sample table must carry timing, not just a private metadata tag.
        """
        metadata = probe_video_metadata(clip_path)
        fps = metadata.average_fps if metadata is not None else None
        if fps is None or not math.isfinite(fps) or fps <= 0:
            return []
        fps_text = (str(int(round(fps)))
                    if abs(fps - round(fps)) < 0.005
                    else f'{fps:.6f}'.rstrip('0').rstrip('.'))
        return ['-fps_mode', 'cfr', '-r', fps_text]

    def _normalize_clip_to_cfr(self, clip_path: str,
                               ffmpeg: str | None = None) -> bool:
        """Validate timing while preserving healthy variable-rate video.

        Keep bounded cadence gaps without transcoding. Repair inconclusive CFR
        evidence and reject proven large timeline gaps.
        """
        def fail(message: str) -> bool:
            self._clip_readiness.finalization_failed(
                clip_path, message, base_clip_usable=False)
            return False

        metadata = probe_video_metadata(clip_path)
        fps = metadata.average_fps if metadata is not None else None
        if fps is None or not math.isfinite(fps) or fps <= 0:
            return fail(
                'Frame-rate repair failed because the configured FPS could '
                'not be read; the clip was not published.')

        cfr_evidence = probe_video_cfr_evidence(clip_path, fps)
        if (cfr_evidence.cfr is True
                or cfr_evidence.physical_timeline_bounded is True):
            return True

        # Do not let the CFR repair path expand a native replay that contains
        # a multi-second physical PTS hole. Legitimate VFR clips remain
        # repairable when their packet timeline is bounded; an inconclusive
        # probe still follows the existing conservative repair path.
        if cfr_evidence.physical_timeline_bounded is False:
            return fail(
                'Frame-rate repair refused because the source video timeline '
                'contains a large packet gap; the clip was not published.')

        if ffmpeg is None:
            try:
                ffmpeg = get_ffmpeg_exe()
            except FFmpegUnavailable as error:
                print(f'[CFR] {error} — retaining the source clip')
                return fail(
                    'Frame-rate repair failed because FFmpeg is unavailable; '
                    'the clip was not published.')

        fps_text = (str(int(round(fps)))
                    if abs(fps - round(fps)) < 0.005
                    else f'{fps:.6f}'.rstrip('0').rstrip('.'))
        configured_bitrate = metadata.video_bitrate_bps or 16_000_000
        bitrate_kbps = max(2_500, min(200_000,
            int(round(configured_bitrate / 1000))))
        bridge = getattr(self, 'bridge', None)
        active_codec = ''
        if bridge is not None and hasattr(bridge, 'get_active_codec'):
            try:
                active_codec = bridge.get_active_codec()
            except Exception:
                active_codec = ''

        video_arg_sets = [
            postprocess_video_args(
                active_codec, bitrate_kbps, ffmpeg=ffmpeg),
            software_video_args(bitrate_kbps, ffmpeg=ffmpeg),
        ]
        # Keep one command when the active codec is already the reviewed
        # software fallback; otherwise a failed GPU post-process gets one safe
        # software retry instead of leaving a VFR clip published.
        unique_video_arg_sets = []
        for args in video_arg_sets:
            if args not in unique_video_arg_sets:
                unique_video_arg_sets.append(args)

        try:
            with tempfile.TemporaryDirectory(
                    prefix='.fthr-cfr-', dir=os.path.dirname(clip_path)) as td:
                output = os.path.join(td, 'cfr.mp4')
                common = [
                    ffmpeg, '-y',
                    # CFR repair is the only post-save path allowed to
                    # re-encode video. Keep its decoder/filter CPU footprint
                    # bounded so a save cannot monopolize a game session.
                    '-threads', '2', '-filter_threads', '2',
                    '-i', clip_path,
                    '-map', '0:v:0', '-map', '0:a?',
                    '-map_metadata', '0', '-map_chapters', '0',
                    '-movflags', 'use_metadata_tags',
                    '-fps_mode', 'cfr', '-r', fps_text,
                    '-metadata', f'fthr_frame_rate={fps_text}',
                    '-metadata',
                    f'fthr_video_bitrate_bps={bitrate_kbps * 1000}',
                ]
                timeout = max(
                    180,
                    int((metadata.duration_seconds or 30.0) * 5),
                )
                last_error = '(no stderr)'
                for video_args in unique_video_arg_sets:
                    # Keep the encoder-side thread pool bounded as well as the
                    # input decoder/filter pools configured above.
                    bounded_video_args = [*video_args, '-threads:v', '2']
                    result = subprocess.run(
                        [*common, *bounded_video_args,
                         '-c:a', 'copy', output],
                        capture_output=True, timeout=timeout,
                        **_BACKGROUND_NO_WINDOW,
                    )
                    if result.returncode == 0 and os.path.isfile(output):
                        os.replace(output, clip_path)
                        rebind_manifest_after_media_replace(clip_path)
                        print(f'[CFR] Repaired {os.path.basename(clip_path)} '
                              f'to {fps_text} FPS without changing duration')
                        return True
                    detail = (result.stderr.decode(errors='replace')
                              .strip().splitlines())
                    last_error = detail[-1] if detail else '(no stderr)'
                    try:
                        os.remove(output)
                    except FileNotFoundError:
                        pass
                print(f'[CFR] FFmpeg failed: {last_error}')
        except subprocess.TimeoutExpired:
            print(f'[CFR] FFmpeg timed out while repairing '
                  f'{os.path.basename(clip_path)}')
        except (OSError, subprocess.SubprocessError) as error:
            print(f'[CFR] Repair error: {error}')

        return fail(
            'Frame-rate repair failed; the original source clip was not '
            'published.')

    def _mic_mux_worker(self, clip_path: str,
                        duration_seconds: int,
                        mic_end_time: float,
                        clip_ready=None, crop_profile=None,
                        separate_audio: bool = False):
        try:
            ffmpeg = get_ffmpeg_exe()
        except FFmpegUnavailable as e:
            print(f'[Mic] {e} — skipping mic mux')
            self._record_finalization_warning(
                clip_path, 'Microphone mix was skipped because FFmpeg is unavailable.')
            if clip_ready is not None:
                clip_ready.set()
            return

        try:
            # The native save acknowledgement already guarantees a closed file.
            if not os.path.isfile(clip_path) or os.path.getsize(clip_path) == 0:
                raise OSError('The committed clip is missing or empty.')

            # Try to mix mic audio into the clip. Any failure is non-fatal:
            # watermark/crop/camera still apply to the original clip below.
            samples = MicRecorder().extract_segment(mic_end_time, duration_seconds)
            if samples is not None and samples.size > 0:
                with tempfile.TemporaryDirectory(
                        prefix='.fthr-audio-', dir=os.path.dirname(clip_path)) as td:
                    mic_wav = os.path.join(td, 'mic.wav')
                    mixed_mp4 = os.path.join(td, 'audio-layout.mp4')
                    if write_wav(mic_wav, samples):
                        if separate_audio:
                            cmd = [
                                ffmpeg, '-y',
                                '-i', clip_path,
                                '-i', mic_wav,
                                '-map', '0:v?',
                                '-map', '0:a?',
                                '-map', '1:a:0',
                                '-map_metadata', '0',
                                '-map_chapters', '0',
                                '-movflags', 'use_metadata_tags',
                                '-metadata', 'comment=fthr-audio-mode=separated',
                                '-metadata:s:a:0', 'title=System Audio',
                                '-metadata:s:a:0', 'handler_name=System Audio',
                                '-metadata:s:a:1', 'title=Microphone',
                                '-metadata:s:a:1', 'handler_name=Microphone',
                                '-c:v', 'copy',
                                '-c:a', 'aac', '-b:a', '192k',
                                '-shortest',
                                mixed_mp4,
                            ]
                        else:
                            cmd = [
                                ffmpeg, '-y',
                                '-i', clip_path,
                                '-i', mic_wav,
                                '-filter_complex',
                                '[0:a:0][1:a:0]amix=inputs=2:duration=first:'
                                'dropout_transition=0,alimiter=limit=0.98:'
                                'level=disabled[aout]',
                                '-map', '0:v?',
                                '-map', '[aout]',
                                '-map_metadata', '0',
                                '-map_chapters', '0',
                                '-movflags', 'use_metadata_tags',
                                '-metadata', 'comment=fthr-audio-mode=combined',
                                '-metadata:s:a:0', 'handler_name=Combined Audio',
                                '-c:v', 'copy',
                                '-c:a', 'aac', '-b:a', '192k',
                                '-shortest',
                                mixed_mp4,
                            ]
                        try:
                            result = subprocess.run(
                                cmd, capture_output=True, timeout=120,
                                **_NO_WINDOW,
                            )
                            if result.returncode == 0:
                                try:
                                    os.replace(mixed_mp4, clip_path)
                                    layout_text = (
                                        'Kept separate mic track in'
                                        if separate_audio else 'Mixed mic into')
                                    print(
                                        f'[Mic] {layout_text} '
                                        f'{os.path.basename(clip_path)}')
                                except OSError as e:
                                    print(f'[Mic] Could not replace clip: {e}')
                                    self._record_finalization_warning(
                                        clip_path, 'Microphone mix could not replace the base clip.')
                            else:
                                err = result.stderr.decode(errors='replace').strip().splitlines()
                                print(f'[Mic] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
                                self._record_finalization_warning(
                                    clip_path, 'Microphone track could not be merged; '
                                    'the base clip was retained.')
                                self._ui_call.emit(lambda: self.push_error(
                                    'MIC AUDIO FAILED',
                                    'Microphone track could not be merged.'
                                    ' Clip saved without mic audio.',
                                    level='warning',
                                    actions=[('OPEN AUDIO SETTINGS',
                                              self._toggle_settings_page)],
                                ))
                        except subprocess.TimeoutExpired:
                            print('[Mic] ffmpeg timed out after 120s — skipping mux')
                            self._record_finalization_warning(
                                clip_path, 'Microphone mix timed out; the base clip was retained.')
                            self._ui_call.emit(lambda: self.push_error(
                                'MIC AUDIO FAILED',
                                'ffmpeg timed out. Clip saved without mic audio.',
                                level='warning',
                            ))
                        except Exception as e:
                            print(f'[Mic] ffmpeg mux error: {e}')
                            self._record_finalization_warning(
                                clip_path, 'Microphone mix failed; the base clip was retained.')
            else:
                print('[Mic] No mic samples for this clip window')
                self._record_finalization_warning(
                    clip_path, 'No microphone samples were available for this clip.')

            # Always apply post-processing to whatever clip exists now
            # (either the muxed version or the original if mux failed).
            self._apply_crop(clip_path, ffmpeg, crop_profile)
            self._apply_image_overlay(clip_path, ffmpeg)
            self._apply_keyboard_overlay(
                clip_path, ffmpeg, mic_end_time, duration_seconds)
            self._apply_camera_overlay(clip_path, ffmpeg, mic_end_time, duration_seconds)
        finally:
            # Always unblock the upload worker, regardless of success or failure.
            if clip_ready is not None:
                clip_ready.set()

    @staticmethod
    def _clip_dimensions(clip_path: str, ffmpeg: str) -> tuple[int, int]:
        import re as _re
        try:
            info = subprocess.run(
                [ffmpeg, '-i', clip_path], capture_output=True,
                timeout=30, **_NO_WINDOW)
            dimensions = _re.search(
                r'(\d{3,5})x(\d{3,5})',
                info.stderr.decode(errors='replace'))
            if dimensions:
                return int(dimensions.group(1)), int(dimensions.group(2))
        except Exception as exc:
            print(f'[Post] Could not probe clip dimensions: {exc}')
        return 1920, 1080

    def _apply_image_overlay(self, clip_path: str, ffmpeg: str) -> None:
        if not self.settings_manager.get('image_overlay_enabled', False):
            return
        layers = [
            layer for layer in image_overlay_layers(self.settings_manager)
            if layer.get('enabled')]
        valid_layers = []
        for layer in layers:
            if os.path.isfile(layer['path']):
                valid_layers.append(layer)
            else:
                self._record_finalization_warning(
                    clip_path,
                    f'Image overlay source is missing: {Path(layer["path"]).name}')
        if not valid_layers:
            return

        clip_w, clip_h = self._clip_dimensions(clip_path, ffmpeg)
        image_inputs = []
        filters = []
        previous = '0:v'
        for index, layer in enumerate(valid_layers):
            input_index = index + 1
            image_inputs.extend(['-loop', '1', '-i', layer['path']])
            rect = clamp_overlay_rect(
                layer.get('rect'), DEFAULT_IMAGE_OVERLAY_RECT)
            image_w = max(64, int(clip_w * rect['w'])) & ~1
            image_h = max(48, int(clip_h * rect['h'])) & ~1
            image_x = max(0, int(clip_w * rect['x']))
            image_y = max(0, int(clip_h * rect['y']))
            opacity = max(0.10, min(
                1.0, float(layer.get('opacity', 100)) / 100.0))
            if layer.get('fit') == 'fill':
                shape = (
                    f'scale={image_w}:{image_h}:'
                    'force_original_aspect_ratio=increase,'
                    f'crop={image_w}:{image_h}')
            else:
                shape = (
                    f'scale={image_w}:{image_h}:'
                    'force_original_aspect_ratio=decrease,'
                    f'pad={image_w}:{image_h}:(ow-iw)/2:(oh-ih)/2:'
                    'color=0x00000000')
            image_label = f'image{index}'
            output_label = (
                'vout' if index == len(valid_layers) - 1 else f'layer{index}')
            filters.append(
                f'[{input_index}:v]format=rgba,{shape},'
                f'colorchannelmixer=aa={opacity:.3f}[{image_label}]')
            filters.append(
                f'[{previous}][{image_label}]overlay={image_x}:{image_y}:'
                f'eof_action=repeat:shortest=1[{output_label}]')
            previous = output_label

        tmp_dir = tempfile.mkdtemp(
            prefix='.fthr-finalize-', dir=os.path.dirname(clip_path))
        tmp_path = os.path.join(tmp_dir, 'output.mp4')
        try:
            result = subprocess.run(
                [ffmpeg, '-y',
                 '-i', clip_path,
                 *image_inputs,
                 '-filter_complex', ';'.join(filters),
                 '-map', '[vout]', '-map', '0:a?',
                 '-map_metadata', '0', '-map_chapters', '0',
                 '-movflags', 'use_metadata_tags',
                 *MainWindow._cfr_video_args(clip_path),
                 *software_video_args(),
                 '-c:a', 'copy',
                 tmp_path],
                capture_output=True,
                timeout=max(120, 45 * len(valid_layers)), **_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(tmp_path, clip_path)
                rebind_manifest_after_media_replace(clip_path)
                print(f'[ImageOverlay] Applied {len(valid_layers)} layer(s) to '
                      f'{os.path.basename(clip_path)}')
            else:
                error = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[ImageOverlay] ffmpeg failed: '
                      f'{error[-1] if error else "(no stderr)"}')
                self._record_finalization_warning(
                    clip_path, 'Image overlay failed; the base clip was retained.')
        except Exception as exc:
            print(f'[ImageOverlay] Error: {exc}')
            self._record_finalization_warning(
                clip_path, 'Image overlay failed; the base clip was retained.')
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                # FFmpeg may not have created a staging output on failure.
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                # Temporary directory cleanup is best-effort after publication.
                pass

    def _apply_keyboard_overlay(self, clip_path: str, ffmpeg: str,
                                clip_end_time: float,
                                duration_sec: int) -> None:
        """Composite timestamp-matched keyboard frames using the preview chroma key.

        Stream the source ring into the compositor. A missing source preserves
        the base clip and produces a finalization warning.
        """
        config = third_party_keyboard_settings(self.settings_manager)
        if not config['enabled']:
            return
        capture = getattr(self, '_keyboard_overlay_capture', None)
        if capture is None:
            self._record_finalization_warning(
                clip_path, 'Keyboard overlay capture was unavailable.')
            return

        tmp_dir = tempfile.mkdtemp(
            prefix='.fthr-keyboard-', dir=os.path.dirname(clip_path))
        output_path = os.path.join(tmp_dir, 'output.mp4')
        process = None
        try:
            clip_w, clip_h = self._clip_dimensions(clip_path, ffmpeg)
            rect = clamp_overlay_rect(
                config.get('rect'), DEFAULT_KEYBOARD_OVERLAY_RECT)
            keyboard_w = max(64, int(clip_w * rect['w'])) & ~1
            keyboard_h = max(48, int(clip_h * rect['h'])) & ~1
            keyboard_x = max(0, int(clip_w * rect['x']))
            keyboard_y = max(0, int(clip_h * rect['y']))
            filter_complex = (
                '[1:v]setpts=PTS-STARTPTS,format=rgba[keyboard];'
                f'[0:v][keyboard]overlay={keyboard_x}:{keyboard_y}:'
                'format=auto:eof_action=repeat:shortest=0[vout]')
            bridge = getattr(self, 'bridge', None)
            active_codec = (
                bridge.get_active_codec()
                if bridge is not None and hasattr(bridge, 'get_active_codec')
                else '')

            stream_state = {'wrote_frame': False, 'error': None}
            writer_thread = None
            with tempfile.TemporaryFile() as ffmpeg_log:
                process = subprocess.Popen(
                    [ffmpeg, '-y', '-loglevel', 'error', '-i', clip_path,
                     '-f', 'rawvideo', '-pix_fmt', 'rgba',
                     '-video_size', f'{keyboard_w}x{keyboard_h}',
                     '-framerate', str(KEYBOARD_COMPOSITE_FPS), '-i', '-',
                     '-filter_complex', filter_complex,
                     '-map', '[vout]', '-map', '0:a?',
                     '-map_metadata', '0', '-map_chapters', '0',
                     '-movflags', 'use_metadata_tags',
                     *MainWindow._cfr_video_args(clip_path),
                     *postprocess_video_args(active_codec, ffmpeg=ffmpeg),
                     '-c:a', 'copy', output_path],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    # A PIPE can fill while Python is blocked writing raw
                    # frames, deadlocking both processes before wait() gets a
                    # chance to enforce its timeout. A seekable temporary log
                    # cannot apply that back-pressure.
                    stderr=ffmpeg_log,
                    **_BACKGROUND_NO_WINDOW)

                def _feed_keyboard_frames():
                    try:
                        for frame in capture.iter_segment_rgba(
                                clip_end_time, duration_sec,
                                (keyboard_w, keyboard_h),
                                config['color'], config['intensity'],
                                KEYBOARD_COMPOSITE_FPS):
                            if process.stdin is None:
                                break
                            process.stdin.write(memoryview(frame).cast('B'))
                            stream_state['wrote_frame'] = True
                    except BrokenPipeError:
                        # FFmpeg's return code and log hold the useful error.
                        pass
                    except Exception as error:
                        stream_state['error'] = error
                    finally:
                        if process.stdin is not None and not process.stdin.closed:
                            try:
                                process.stdin.close()
                            except (BrokenPipeError, OSError):
                                pass

                # Feeding on a helper thread makes the total timeout real: if
                # FFmpeg stops consuming stdin, the finalizer can still kill
                # it and release the blocked write instead of staying stuck in
                # FINALIZING forever.
                writer_thread = threading.Thread(
                    target=_feed_keyboard_frames,
                    name='FTHR-KeyboardCompositorFeed',
                    daemon=True,
                )
                writer_thread.start()
                timeout_seconds = max(180, int(duration_sec) * 3)
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                    writer_thread.join(timeout=10)
                    print('[KeyboardOverlay] ffmpeg timed out after '
                          f'{timeout_seconds}s')
                    self._record_finalization_warning(
                        clip_path,
                        'Keyboard overlay timed out; the base clip was retained.')
                    return
                writer_thread.join(timeout=10)
                ffmpeg_log.seek(0)
                stderr = ffmpeg_log.read()

            wrote_frame = bool(stream_state['wrote_frame'])
            if stream_state['error'] is not None:
                raise stream_state['error']
            if writer_thread is not None and writer_thread.is_alive():
                self._record_finalization_warning(
                    clip_path,
                    'Keyboard overlay input did not finish; the base clip was retained.')
                return
            if not wrote_frame:
                self._record_finalization_warning(
                    clip_path,
                    'Keyboard overlay source had no recent frames; the base clip was retained.')
                return
            if returncode == 0:
                os.replace(output_path, clip_path)
                rebind_manifest_after_media_replace(clip_path)
                print(f'[KeyboardOverlay] Applied to {os.path.basename(clip_path)}')
            else:
                detail = stderr.decode(errors='replace').strip().splitlines()
                print('[KeyboardOverlay] ffmpeg failed: '
                      f'{detail[-1] if detail else "(no stderr)"}')
                self._record_finalization_warning(
                    clip_path,
                    'Keyboard overlay failed; the base clip was retained.')
        except Exception as error:
            print(f'[KeyboardOverlay] Error: {error}')
            self._record_finalization_warning(
                clip_path, 'Keyboard overlay failed; the base clip was retained.')
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if (process is not None and process.stdin is not None
                    and not process.stdin.closed):
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            for path in (output_path,):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    def _apply_camera_overlay(self, clip_path: str, ffmpeg: str,
                               clip_end_time: float, duration_sec: int) -> None:
        if not self.settings_manager.get('camera_enabled', False):
            return
        from core.camera_recorder import CameraRecorder
        if not CameraRecorder.is_available() or not CameraRecorder().is_running():
            self._record_finalization_warning(
                clip_path, 'Camera overlay was requested but the camera was unavailable.')
            return
        import tempfile as _tf

        tmp_dir = _tf.mkdtemp(
            prefix='.fthr-finalize-', dir=os.path.dirname(clip_path))
        cam_path = os.path.join(tmp_dir, 'camera.mp4')

        if not CameraRecorder().write_segment(cam_path, clip_end_time, duration_sec, 30.0):
            self._record_finalization_warning(
                clip_path, 'Camera overlay could not be rendered; the base clip was retained.')
            try:
                os.rmdir(tmp_dir)
            except OSError:
                # Camera staging may already be removed after an unavailable frame.
                pass
            return

        clip_w, clip_h = self._clip_dimensions(clip_path, ffmpeg)

        saved_rect = self.settings_manager.get('camera_overlay_rect')
        if not isinstance(saved_rect, dict):
            saved_rect = legacy_overlay_rect(
                self.settings_manager.get('camera_position', 'bottom-right'),
                self.settings_manager.get('camera_size', 'medium'),
            )
        rect = clamp_overlay_rect(saved_rect)
        cam_w = max(64, int(clip_w * rect['w'])) & ~1
        cam_h = max(48, int(clip_h * rect['h'])) & ~1
        overlay_x = max(0, int(clip_w * rect['x']))
        overlay_y = max(0, int(clip_h * rect['y']))
        overlay_pos = f'{overlay_x}:{overlay_y}'

        out_path = os.path.join(tmp_dir, 'output.mp4')

        try:
            result = subprocess.run(
                [ffmpeg, '-y',
                 '-i', clip_path,
                 '-i', cam_path,
                 '-filter_complex',
                  f'[1:v]scale={cam_w}:{cam_h}:force_original_aspect_ratio=decrease,'
                  f'pad={cam_w}:{cam_h}:(ow-iw)/2:(oh-ih)/2[cam];'
                  f'[0:v][cam]overlay={overlay_pos}[vout]',
                 '-map', '[vout]', '-map', '0:a?',
                 '-map_metadata', '0', '-map_chapters', '0',
                 '-movflags', 'use_metadata_tags',
                 *MainWindow._cfr_video_args(clip_path),
                 *software_video_args(),
                 '-c:a', 'copy',
                 out_path],
                capture_output=True, timeout=120, **_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(out_path, clip_path)
                rebind_manifest_after_media_replace(clip_path)
                print(f'[Camera] Overlay applied to {os.path.basename(clip_path)}')
            else:
                err = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[Camera] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
                self._record_finalization_warning(
                    clip_path, 'Camera overlay failed; the base clip was retained.')
                self._ui_call.emit(lambda: self.push_error(
                    'CAMERA OVERLAY FAILED',
                    'FFmpeg error. Clip saved without camera overlay.',
                    level='warning',
                ))
        except Exception as e:
            print(f'[Camera] Error: {e}')
            self._record_finalization_warning(
                clip_path, 'Camera overlay failed; the base clip was retained.')
        finally:
            for p in (cam_path, out_path):
                try:
                    os.remove(p)
                except FileNotFoundError:
                    # Failed FFmpeg runs need not create every staged file.
                    pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                # Temporary directory cleanup is best-effort after publication.
                pass

    def _apply_crop(self, clip_path: str, ffmpeg: str,
                    crop_profile=None) -> None:
        """Apply an explicitly configured manual game crop."""
        if not isinstance(crop_profile, dict) or not crop_profile.get(
                'enabled', True):
            return
        crop_source = 'GameCrop'
        try:
            src_w, src_h = self._clip_dimensions(clip_path, ffmpeg)
            norm_x = float(crop_profile.get('x', 0.0))
            norm_y = float(crop_profile.get('y', 0.0))
            norm_w = float(crop_profile.get('w', 1.0))
            norm_h = float(crop_profile.get('h', 1.0))
        except (TypeError, ValueError) as exc:
            print(f'[{crop_source}] invalid profile: {exc} — skipping')
            self._record_finalization_warning(
                clip_path, 'The game crop profile was invalid; the base clip was retained.')
            return

        x = max(0, min(src_w - 2, int(round(norm_x * src_w)))) & ~1
        y = max(0, min(src_h - 2, int(round(norm_y * src_h)))) & ~1
        width = max(2, int(round(norm_w * src_w))) & ~1
        height = max(2, int(round(norm_h * src_h))) & ~1
        width = max(2, min(width, (src_w - x) & ~1))
        height = max(2, min(height, (src_h - y) & ~1))
        if x == 0 and y == 0 and width >= src_w - 1 and height >= src_h - 1:
            return
        crop = f'{width}:{height}:{x}:{y}'
        import tempfile as _tf
        tmp_dir = _tf.mkdtemp(
            prefix='.fthr-finalize-', dir=os.path.dirname(clip_path))
        tmp_path = os.path.join(tmp_dir, 'output.mp4')
        # OpenH264 can reject its fixed 16 Mbit/s default at small cropped
        # resolutions because the resulting bitstream exceeds the H.264 level
        # limit for that frame size. Scale from the 1080p default while keeping
        # a quality floor for low-resolution clips.
        target_bitrate = max(2_500, min(
            50_000,
            int(round(16_000 * (width * height) / (1920 * 1080))),
        ))
        video_args = software_video_args(bitrate_kbps=target_bitrate)
        run_options = dict(_NO_WINDOW)
        try:
            result = subprocess.run(
                [ffmpeg, '-y', '-i', clip_path,
                 '-vf', f'crop={crop}',
                 '-map', '0:v:0', '-map', '0:a?',
                 '-map_metadata', '0', '-map_chapters', '0',
                 '-movflags', 'use_metadata_tags',
                 *MainWindow._cfr_video_args(clip_path),
                 *video_args,
                 '-c:a', 'copy', tmp_path],
                capture_output=True, timeout=120, **run_options,
            )
            if result.returncode == 0:
                os.replace(tmp_path, clip_path)
                rebind_manifest_after_media_replace(clip_path)
                print(f'[{crop_source}] crop={crop} applied to '
                      f'{os.path.basename(clip_path)}')
            else:
                err = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[{crop_source}] ffmpeg failed: '
                      f'{err[-1] if err else "(no stderr)"}')
                self._record_finalization_warning(
                    clip_path, 'The game crop failed; the base clip was retained.')
                self._ui_call.emit(lambda: self.push_error(
                    'GAME CROP FAILED',
                    'The saved crop could not be applied. Clip saved uncropped.',
                    level='warning',
                ))
        except Exception as e:
            print(f'[{crop_source}] Error: {e}')
            self._record_finalization_warning(
                clip_path, 'The game crop failed; the base clip was retained.')
            self._ui_call.emit(lambda: self.push_error(
                'GAME CROP FAILED',
                'The saved crop could not be applied. Clip saved uncropped.',
                level='warning',
            ))
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                # Failed crop runs need not create a staged output.
                pass
            try:
                os.rmdir(tmp_dir)
            except OSError:
                # Temporary directory cleanup is best-effort after publication.
                pass

    def _finalize_clip(self, clip_path: str, duration_seconds: int,
                       clip_end_time: float = 0.0, clip_ready=None,
                       crop_profile=None):
        if not self._allow_completed_clip_pipeline(clip_path, clip_ready):
            return
        self._spawn_mux_thread(
            target=self._finalize_clip_worker,
            args=(clip_path, duration_seconds, clip_end_time, clip_ready,
                  crop_profile),
        )

    def _finalize_clip_worker(self, clip_path: str, duration_seconds: int,
                               clip_end_time: float = 0.0, clip_ready=None,
                               crop_profile=None):
        finalization_started = time.monotonic()
        try:
            try:
                ffmpeg = get_ffmpeg_exe()
            except FFmpegUnavailable as e:
                print(f'[Finalize] {e} — skipping post-processing')
                self._record_finalization_warning(
                    clip_path, 'Optional processing was skipped because FFmpeg is unavailable.')
                return
            # CLIP_SAVED is emitted after native close and atomic publication.
            # Waiting for two equal file sizes here added latency without
            # providing a stronger completion boundary.
            if not os.path.isfile(clip_path) or os.path.getsize(clip_path) == 0:
                raise OSError('The committed clip is missing or empty.')
            stage_started = time.monotonic()
            self._apply_crop(clip_path, ffmpeg, crop_profile)
            emit_event('clip_save', 'crop_stage_completed',
                       elapsed_ms=round((time.monotonic() - stage_started) * 1000))
            stage_started = time.monotonic()
            self._apply_image_overlay(clip_path, ffmpeg)
            emit_event('clip_save', 'image_overlay_stage_completed',
                       elapsed_ms=round((time.monotonic() - stage_started) * 1000))
            stage_started = time.monotonic()
            self._apply_keyboard_overlay(
                clip_path, ffmpeg, clip_end_time, duration_seconds)
            emit_event('clip_save', 'keyboard_overlay_stage_completed',
                       elapsed_ms=round((time.monotonic() - stage_started) * 1000))
            stage_started = time.monotonic()
            self._apply_camera_overlay(clip_path, ffmpeg, clip_end_time, duration_seconds)
            emit_event('clip_save', 'camera_overlay_stage_completed',
                       elapsed_ms=round((time.monotonic() - stage_started) * 1000))
        finally:
            emit_event(
                'clip_save', 'optional_finalization_worker_completed',
                elapsed_ms=round(
                    (time.monotonic() - finalization_started) * 1000))
            if clip_ready is not None:
                clip_ready.set()

    # UI state

    def _connect_worker_active(self) -> bool:
        # The connect worker calls initialize() on the singleton bridge; the
        # status poll must not shut that bridge down underneath it. On Linux
        # a crashed engine leaves its /dev/shm segment behind, so the poll's
        # reconnect would otherwise race the worker for the same mapping.
        thread = getattr(self, '_engine_connect_thread', None)
        is_alive = getattr(thread, 'is_alive', None)
        return bool(callable(is_alive) and is_alive())

    def _update_status(self):
        # Detect an engine crash. The shared-memory mapping outlives the
        # process (is_initialized stays true), so is_connected() keeps lying
        # after a crash: the UI shows CAPTURING forever and every hotkey
        # burns the 1 s save timeout with a generic error.
        proc = self.engine_process
        if proc is not None and proc.poll() is not None:
            process_generation = getattr(
                self, '_engine_process_generation', None)
            bridge_owner = getattr(self, 'bridge', None)
            if (self.engine_process is not proc
                    or self.bridge is not bridge_owner):
                return
            code = proc.returncode
            self._engine_gen = getattr(self, '_engine_gen', 0) + 1
            self.engine_process = None
            self._engine_process_generation = None
            self._capture_config.deactivate(
                f'capture engine exited unexpectedly (code {code})')
            self._pending_launch_config = None
            self._pending_launch_generation = None
            self._restart_pending = False
            self._set_capture_apply_state(False)
            if bridge_owner:
                bridge_owner.shutdown()
            self.bridge = CaptureBridge()
            recovery_key = 'engine-process-exit'
            process_generation = (
                self._engine_gen - 1 if process_generation is None
                else process_generation)
            self._capture_health_snapshot = self._capture_health.observe(
                connected=False, frame_count=0, engine_flags=0)
            tracker = getattr(self, '_error_recovery_tracker', None)
            if tracker is not None:
                tracker.reset()
            self._set_status('ENGINE STOPPED', status_warning_qss())
            emit_event(
                'engine', 'process_exited', state='FAILED',
                error=DiagnosticError.ENGINE_START_FAILED,
                compatibility_error='Error 006',
                technical_cause='UNEXPECTED_PROCESS_EXIT',
                technical_subsystem='process', operation='process_poll',
                process_generation=process_generation,
                native_capture_generation=getattr(
                    self, '_last_native_capture_generation', None),
                exit_code=code,
                unexpected=True)
            self.push_error(
                'ENGINE STOPPED',
                f'The capture engine exited unexpectedly (code {code}). '
                'Clips cannot be saved until it is restarted.',
                level='error',
                actions=[('RESTART ENGINE', self._restart_capture_engine)],
                key=recovery_key,
                dismiss_on_action=False,
                diagnostic_context={
                    'diagnostic_error': DiagnosticError.ENGINE_START_FAILED,
                    'technical_cause': 'UNEXPECTED_PROCESS_EXIT',
                    'technical_subsystem': 'process',
                    'operation': 'process_poll',
                    'process_generation': process_generation,
                    'backend_generation': getattr(
                        self, '_last_native_capture_generation', None),
                    'recovery_kind': 'engine_lineage',
                    'recovery_status': 'awaiting_replacement_engine',
                },
            )
            return
        is_connected = getattr(self.bridge, 'is_connected', lambda: False)
        if not self.bridge or not is_connected():
            self._advance_recoverable_errors(
                connected=False, frame_count=0,
                health_state=CaptureHealthState.STOPPED,
                backend_generation=getattr(
                    self, '_last_native_capture_generation', 0) or 0)
            self._reconnect_counter = getattr(self, '_reconnect_counter', 0) + 1
            # Only try to reconnect while an engine process actually exists —
            # on Linux a crashed engine leaves its /dev/shm segment behind
            # with is_initialized still true, so initialize() would happily
            # "reconnect" to a corpse.
            if (self.engine_process is not None
                    and not MainWindow._connect_worker_active(self)
                    and self._reconnect_counter % 3 == 1
                    and getattr(self, '_engine_handshake_generation', None)
                    != getattr(self, '_engine_gen', 0)):
                generation = getattr(self, '_engine_gen', 0)
                process_owner = self.engine_process
                old_bridge = self.bridge
                if old_bridge:
                    old_bridge.shutdown()
                if not self._owns_engine_attempt(
                        generation, process_owner, old_bridge):
                    return
                reconnect_bridge = CaptureBridge()
                self.bridge = reconnect_bridge
                reconnected = reconnect_bridge.initialize()
                if not self._owns_engine_attempt(
                        generation, process_owner, reconnect_bridge):
                    return
                if reconnected:
                    print("Reconnected to capture engine.")
                    self._set_status('STARTING CAPTURE', status_idle_qss())
                    self._reconnect_counter = 0
                    self._schedule_owned_hardware_check(
                        generation, process_owner, reconnect_bridge)
                    return
            self._set_status(
                self._capture_pending_text() if self._capture_settings_applying else 'CONNECTING',
                status_idle_qss(),
            )
            return

        status = self.bridge.get_status()
        self._maybe_restart_for_display_mode_change(status)
        if not status.get('connected'):
            self._capture_health_snapshot = self._capture_health.observe(
                connected=False, frame_count=0, engine_flags=0)
            self._advance_recoverable_errors(
                connected=False, frame_count=0,
                health_state=CaptureHealthState.STOPPED,
                backend_generation=getattr(
                    self, '_last_native_capture_generation', 0) or 0)
            self._set_status('DISCONNECTED', status_warning_qss())
            return

        # Second driver of the same single reader. The fast save timer stops as
        # soon as nothing is in flight; this keeps a *timed-out* operation's
        # late-result window open, and catches a response for a save the fast
        # timer had already given up on. Both call the same method, so there is
        # still exactly one consumer.
        self._pump_save_responses()
        codec = self.bridge.get_active_codec()
        if codec:
            if encoder_preset_supported(sys.platform, 'nvenc') \
                    and 'nvenc' in codec.lower():
                preset = self.bridge.get_active_preset()
                new_enc_text = f'{codec} — P{preset}'
            else:
                new_enc_text = codec
            if self._ui_ready and not self._background_ui_paused:
                lbl = self._settings_page_widget.__dict__.get('active_encoder_lbl')
                if lbl is not None and lbl.text() != new_enc_text:
                    lbl.setText(new_enc_text)
        if self.is_capturing:
            frames = status.get('frames_captured', 0)
            backend_generation = status.get('capture_generation', 0)
            snapshot = self._capture_health.observe(
                connected=True,
                frame_count=frames,
                engine_flags=status.get('capture_health_flags', 0),
                generation=backend_generation,
            )
            self._capture_health_snapshot = snapshot
            self._last_native_capture_generation = backend_generation
            self._advance_recoverable_errors(
                connected=True, frame_count=frames,
                health_state=snapshot.state,
                backend_generation=backend_generation,
                # Legacy ABI flag means an initialized compressed replay encoder,
                # including explicitly selected Windows software H.264.
                hardware_active=status.get('nvenc_active') is True)
            pending_config = getattr(self, '_pending_launch_config', None)
            pending_generation = getattr(
                self, '_pending_launch_generation', None)
            if (pending_config is not None
                    and pending_generation == self._engine_gen):
                if snapshot.state in {
                        CaptureHealthState.HEALTHY,
                        CaptureHealthState.CONTENT_SUSPECT} or (
                            snapshot.state is CaptureHealthState.DEGRADED
                            and status.get('capture_health_flags', 0)
                            & int(CaptureHealthFlag.PAUSED)
                            and status.get('capture_health_flags', 0)
                            & int(CaptureHealthFlag.ACTIVE)
                            and status.get('nvenc_active') is True
                            ):
                    self._capture_config.succeed()
                    self._restart_pending = False
                    self._apply_active_audio_state(pending_config)
                    self._pending_launch_config = None
                    self._pending_launch_generation = None
                    self._set_capture_apply_state(False)
                    actual = {
                        'actual_codec': codec or pending_config.codec,
                        'capture_generation': status.get(
                            'capture_generation', 0),
                    }
                    if pending_config.width and pending_config.height:
                        actual.update({
                            'capture_dimensions': {
                                'width': pending_config.width,
                                'height': pending_config.height,
                            },
                            'encoder_dimensions': {
                                'width': pending_config.width,
                                'height': pending_config.height,
                            },
                        })
                    if self._diagnostics is not None:
                        self._diagnostics.merge_summary(
                            'actual_configuration', **actual)
                        actual = self._diagnostics.summary().get(
                            'actual_configuration', actual)
                    emit_event(
                        'capture', 'configuration_activated', state='ACTIVE',
                        actual_configuration=actual,
                        process_generation=getattr(
                            self, '_engine_process_generation', None),
                        capture_generation=status.get('capture_generation', 0))
                    if self._manual_record_state == 'preparing':
                        QTimer.singleShot(
                            0, self._continue_prepared_manual_recording)
                elif snapshot.state in {
                        CaptureHealthState.STALLED,
                        CaptureHealthState.FAILED}:
                    self._capture_config.fail(snapshot.reason)
                    self._restart_pending = False
                    self._pending_launch_config = None
                    self._pending_launch_generation = None
                    self._set_capture_apply_state(False)
                    if self._manual_record_state == 'preparing':
                        self._fail_manual_recording(
                            'The recording quality profile did not become healthy.')
            if snapshot.changed:
                diagnostic_error = None
                if snapshot.state is CaptureHealthState.STALLED:
                    diagnostic_error = DiagnosticError.CAPTURE_FRAME_STALLED
                emit_event(
                    'capture', 'health_transition',
                    state=snapshot.state.value.upper(),
                    error=diagnostic_error,
                    previous_state=snapshot.previous_state.value.upper(),
                    reason=snapshot.reason,
                    frames_acquired=frames,
                    process_generation=getattr(
                        self, '_engine_process_generation', None),
                    capture_generation=status.get('capture_generation', 0),
                    content_sample_sequence=status.get(
                        'content_sample_sequence', 0),
                    suspicious_content_streak=status.get(
                        'content_suspicious_streak', 0),
                    luma_mean=status.get('content_luma_mean', 0.0),
                    luma_variance=status.get('content_luma_variance', 0.0))
                if snapshot.state is CaptureHealthState.FAILED:
                    # Linux capture backends publish their terminal reason in
                    # engine_string so portal failures are actionable.
                    self.push_error(
                        'CAPTURE FAILED',
                        status.get('capture_failure_detail')
                        or snapshot.reason or 'The capture backend failed.',
                        level='error',
                        actions=[('RESTART ENGINE', self._restart_capture_engine)],
                        key='capture-backend',
                        dismiss_on_action=False,
                        diagnostic_context={
                            'diagnostic_error': (
                                DiagnosticError.CAPTURE_NO_FRAMES),
                            'technical_cause': (
                                status.get('capture_failure_code')
                                or 'CAPTURE_BACKEND_FAILED'),
                            'technical_subsystem': 'capture_backend',
                            'operation': 'capture_health_poll',
                            'process_generation': getattr(
                                self, '_engine_process_generation', None),
                            'backend_generation': backend_generation,
                            'recovery_kind': 'capture_backend',
                            'recovery_status': 'awaiting_stable_progress',
                        },
                    )
                self._capture_health_log.info(
                    'Capture health: %s -> %s reason=%s frame_count=%d generation=%d '
                    'sample_sequence=%d suspicious_streak=%d luma_mean=%.2f '
                    'luma_variance=%.2f',
                    snapshot.previous_state.value.upper(),
                    snapshot.state.value.upper(), snapshot.reason, frames,
                    status.get('capture_generation', 0),
                    status.get('content_sample_sequence', 0),
                    status.get('content_suspicious_streak', 0),
                    status.get('content_luma_mean', 0.0),
                    status.get('content_luma_variance', 0.0),
                )

            now = time.monotonic()
            if now - self._last_capture_health_event_at >= 10.0:
                self._last_capture_health_event_at = now
                health = {
                    'state': snapshot.state.value.upper(),
                    'frames_acquired': frames,
                    'process_generation': getattr(
                        self, '_engine_process_generation', None),
                    'capture_generation': status.get('capture_generation', 0),
                    'capture_restart_count': max(
                        0, self._diagnostic_engine_start_count - 1),
                    'capture_health_flags': status.get('capture_health_flags', 0),
                    'content_sample_sequence': status.get(
                        'content_sample_sequence', 0),
                    'suspicious_content_streak': status.get(
                        'content_suspicious_streak', 0),
                }
                if self._diagnostics is not None:
                    self._diagnostics.update_summary('capture_health', health)
                emit_event('capture', 'health_snapshot', **health)

            state_text = {
                CaptureHealthState.INITIALIZING: 'STARTING CAPTURE',
                CaptureHealthState.HEALTHY: 'CAPTURING',
                CaptureHealthState.DEGRADED: ('CAPTURING' if sys.platform == 'win32' else 'CAPTURE DEGRADED'),
                CaptureHealthState.CONTENT_SUSPECT: ('CAPTURING' if sys.platform == 'win32' else 'CAPTURE DEGRADED — DARK / UNIFORM'),
                CaptureHealthState.STALLED: 'CAPTURE STALLED',
                CaptureHealthState.FAILED: 'CAPTURE FAILED',
                CaptureHealthState.RECOVERING: 'RECOVERING CAPTURE',
                CaptureHealthState.STOPPED: 'CAPTURE STOPPED',
            }
            new_text = (
                self._capture_pending_text()
                if self._capture_settings_applying else state_text[snapshot.state]
            )
            style = (status_idle_qss()
                     if self._capture_settings_applying
                     else status_active_qss()
                     if (snapshot.state is CaptureHealthState.HEALTHY
                         or (snapshot.state in (
                             CaptureHealthState.DEGRADED, CaptureHealthState.CONTENT_SUSPECT)))
                     else status_idle_qss()
                     if snapshot.state in {
                         CaptureHealthState.INITIALIZING,
                         CaptureHealthState.RECOVERING,
                     }
                     else status_warning_qss())
            self._set_status(new_text, style)
            if snapshot.request_recovery:
                self._capture_health_log.warning(
                    'Capture stalled; bounded automatic backend recovery is '
                    'engine-owned and a manual process restart is available')

    def _set_status(self, text: str, style: str):
        if (getattr(self, '_engine_restart_requested', False)
                and getattr(self, '_capture_settings_applying', False)
                and (text == 'STARTING CAPTURE' or text.startswith('CONNECTING'))):
            text = 'RESTARTING ENGINE'
        self._pending_status_display = (text, style)
        if not hasattr(self, 'status_label'):
            print(f'[Lifecycle] Status={text}')
            return
        if self._background_ui_paused:
            return
        self.status_label.setText(text.capitalize())
        # Skip the QSS reapply when the style didn't change (CONNECTING ticks
        # every 500ms during reconnect would otherwise re-style on every tick).
        if getattr(self, '_last_status_style', None) != style:
            self.status_label.setStyleSheet(style)
            self._last_status_style = style

    def _on_application_state_changed(self, state) -> None:
        self._refresh_background_ui_pause_state(state)

    def _refresh_background_ui_pause_state(self, state=None) -> None:
        """Apply the performance preference without touching core services."""
        app = QApplication.instance()
        if state is None and app is not None:
            state = app.applicationState()
        hidden = (
            self._background_start
            or not self.isVisible()
            or self.isMinimized()
        )
        enabled = bool(self.settings_manager.get(
            'pause_ui_in_background', True))
        # Minimized/hidden UI never needs previews, even when the user opts
        # to keep them live while merely switching focus to another app.
        self._apply_background_ui_paused(
            hidden or (enabled and state != Qt.ApplicationState.ApplicationActive))

    def _apply_background_ui_paused(
            self, paused: bool, *, force: bool = False) -> None:
        """Pause presentation work; capture, cards, sounds and saves stay live."""
        paused = bool(paused)
        if paused and not self._first_frame_painted:
            self._background_ui_pause_deferred = True
            return
        if paused == self._background_ui_paused and not force:
            return
        self._background_ui_paused = paused
        if not self._ui_ready:
            return

        self.setUpdatesEnabled(not paused)
        editor_active = getattr(self, "_active_clip_viewer", None) is not None
        stack = getattr(self, "main_stack", None)
        self.clip_grid.set_background_paused(
            paused or (stack is not None and stack.currentIndex() != 0) or editor_active)
        self._settings_page_widget.set_background_ui_paused(
            paused or (stack is not None and stack.currentIndex() != 1) or editor_active)
        if not paused:
            if self._pending_status_display is not None:
                text, style = self._pending_status_display
                self._set_status(text, style)
            self.update()

    def _on_background_ui_pause_changed(self, _enabled: bool) -> None:
        self._refresh_background_ui_pause_state()

    def _on_clip_opened(self, clip_path: str, thumb_pixmap: QPixmap, card_global_rect: QRect):
        if not self._clip_readiness.can_access(clip_path):
            self.push_error(
                'CLIP STILL FINALIZING',
                'Playback, editing and upload become available after optional '
                'processing has finished.',
                level='warning',
            )
            return
        # The file may have been deleted/renamed in the file manager while its
        # card was still visible — opening the viewer on a dead path gives a
        # black player window with a cryptic media error.
        if not os.path.isfile(clip_path):
            self.push_error(
                'CLIP NOT FOUND',
                f'{os.path.basename(clip_path)} was moved or deleted outside the app.',
                level='warning',
            )
            self.clip_grid._known_files = None   # force rescan
            self.clip_grid._load_clips()
            return
        viewer = getattr(self, '_basic_clip_viewer', None)
        if viewer is not None:
            viewer.close()
        if not self.settings_manager.get('clip_editor_enabled', True):
            from ui.basic_clip_viewer import BasicClipViewer
            viewer = BasicClipViewer(clip_path, self)
            self._basic_clip_viewer = viewer
            viewer.destroyed.connect(lambda: self._forget_basic_viewer(viewer))
            viewer.show()
            return
        camera_timer = self._settings_page_widget.__dict__.get('_camera_preview_timer')
        if camera_timer is not None:
            camera_timer.stop()
        from ui.editor_sessions import MAX_EDITOR_SESSIONS
        for session in self._editor_sessions:
            if os.path.normcase(session.clip_path) == os.path.normcase(clip_path):
                self._restore_editor_session(session)
                return
        if self._active_clip_viewer is not None:
            self._active_clip_viewer._close()
        if len(self._editor_sessions) >= MAX_EDITOR_SESSIONS:
            QMessageBox.information(self, 'Three editors are open',
                                    'Close a sidebar editor before opening another clip.')
            return
        self._show_editor_session(self._add_editor_session(clip_path), thumb_pixmap)

    def _forget_basic_viewer(self, viewer):
        if getattr(self, '_basic_clip_viewer', None) is viewer:
            self._basic_clip_viewer = None

    # Error bar

    def _show_hotkey_warning(self, title: str, detail: str,
                             level: str) -> None:
        """Show native Windows hotkey warnings without adding public codes."""
        normalized_title = str(title or '').strip().upper()
        action = [('OPEN HOTKEYS', self._toggle_hotkeys)]
        if (sys.platform != 'win32'
                or not normalized_title.startswith(
                    ('HOTKEY ', 'CONTROLLER HOTKEY '))):
            self.push_error(title, detail, level, actions=action)
            return

        settings_manager = getattr(self, 'settings_manager', None)
        if (settings_manager is not None
                and not bool(settings_manager.get(
                    'error_notifications_enabled', True))):
            return

        bounded_detail = str(detail or
                             'The shortcut could not be activated.')[:240]
        error_bar = getattr(self, 'error_bar', None)
        if error_bar is None:
            print(f'[Hotkey] {normalized_title}: {bounded_detail}')
            return
        error_bar.push(
            normalized_title[:80], bounded_detail, level or 'warning', action)

    def _show_camera_unavailable(self, detail: str) -> None:
        """Show a camera warning without adding a public error code."""
        if not self.settings_manager.get('error_notifications_enabled', True):
            return
        self.error_bar.push(
            'CAMERA UNAVAILABLE',
            str(detail or 'Camera could not be opened.')[:240],
            'warning',
            [('OPEN CAMERA SETTINGS', self._toggle_settings_page)],
        )

    def push_error(self, title: str, detail: str,
                   level: str = 'error',
                   actions: list[tuple[str, callable]] | None = None,
                   *, key: str | None = None,
                   dismiss_on_action: bool = True,
                   diagnostic_context: dict | None = None) -> None:
        normalized_title = str(title or '').strip().upper()
        failure = APP_FAILURE_CODE_BY_TITLE.get(normalized_title)
        if failure is None:
            return

        context = dict(diagnostic_context or {})
        normalized_detail = str(detail or '').strip()
        if not normalized_detail:
            normalized_detail = 'No additional failure reason was provided.'

        def bounded_text(value, limit=160):
            return None if value is None else str(value)[:limit]

        public_code = failure.code
        recovery_kind = context.get('recovery_kind')
        encoder_recovery = (
            key == 'encoder-init'
            and public_code == 'Error 001'
            and context.get('technical_cause') == 'ENCODER_INIT_FAILED'
            and recovery_kind == 'encoder_init'
            and context.get('process_generation') is not None)
        default_recovery_status = (
            'awaiting_replacement_hardware_and_stable_progress'
            if encoder_recovery else
            'awaiting_stable_progress'
            if recovery_kind in {'capture_backend', 'engine_lineage'} else
            'not_recoverable')
        recovery_status = context.get('recovery_status') or (
            default_recovery_status)
        event_fields = {
            'public_error_code': public_code,
            'compatibility_error': public_code,
            'title': normalized_title,
            'detail': normalized_detail[:1000],
            'technical_cause': bounded_text(
                context.get('technical_cause') or 'UNSPECIFIED'),
            'technical_subsystem': bounded_text(
                context.get('technical_subsystem') or 'application'),
            'operation': bounded_text(context.get('operation') or 'report'),
            'process_generation': context.get('process_generation'),
            'backend_generation': context.get('backend_generation'),
            'recovery_status': bounded_text(recovery_status),
        }
        if key is not None:
            event_fields['incident_key'] = bounded_text(key, limit=128)
        operation_id = bounded_text(context.get('operation_id'))
        if operation_id is not None:
            event_fields['operation_id'] = operation_id
        emit_event(
            'application', 'failure_reported', state='FAILED',
            error=context.get('diagnostic_error'), **event_fields)

        if (key is not None and (
                (public_code in {
                    'Error 001', 'Error 003', 'Error 004',
                    'Error 005', 'Error 006'}
                 and recovery_kind in {'capture_backend', 'engine_lineage'})
                or encoder_recovery)):
            incidents = getattr(self, '_recoverable_error_incidents', None)
            if incidents is None:
                incidents = {}
                self._recoverable_error_incidents = incidents
            tracker = getattr(self, '_error_recovery_tracker', None)
            if tracker is None:
                tracker = RecoveryTracker()
                self._error_recovery_tracker = tracker
            tracker.reset(key)
            incidents[key] = {
                'title': normalized_title,
                'public_error_code': public_code,
                'recovery_kind': recovery_kind,
                'process_generation': int(
                    context.get('process_generation') or 0),
            }
        elif key is not None:
            incidents = getattr(self, '_recoverable_error_incidents', None)
            if incidents is not None:
                incidents.pop(key, None)
            tracker = getattr(self, '_error_recovery_tracker', None)
            if tracker is not None:
                tracker.reset(key)

        settings_manager = getattr(self, 'settings_manager', None)
        if (settings_manager is not None
                and not bool(settings_manager.get(
                    'error_notifications_enabled', True))):
            return

        if not hasattr(self, 'error_bar'):
            print(f'[Lifecycle] {level.upper()}: {title}: {normalized_detail}')
            return
        display_title = format_error_title(normalized_title)
        if key is None and dismiss_on_action:
            # Keep the historical positional call path for lightweight test
            # hosts and existing ErrorBar-compatible fakes.
            self.error_bar.push(
                display_title, normalized_detail, level, actions or [])
        else:
            self.error_bar.push(
                display_title, normalized_detail, level, actions or [],
                key=key, dismiss_on_action=dismiss_on_action)

    # Hardware encoding detection

    def _check_hardware_encoding_status(
            self, *, expected_generation: int | None = None,
            expected_process=None, expected_bridge=None):
        if (expected_generation is not None
                and not self._owns_engine_attempt(
                    expected_generation, expected_process, expected_bridge)):
            return
        bridge = (self.bridge if expected_bridge is None
                  else expected_bridge)
        if not bridge or not bridge.is_connected():
            return
        codec = bridge.get_active_codec()
        hw_keywords = ('nvenc', 'amf', 'qsv', 'vaapi')
        hw_active = any(k in codec.lower() for k in hw_keywords) if codec else False

        try:
            nvenc_active = bridge._layout.nvenc_active
        except Exception:
            nvenc_active = False

        software_active = (codec == 'libopenh264' and nvenc_active
                           and self.settings_manager.get('encoder_pref', 'auto') == 'software')
        is_hw = hw_active or (nvenc_active and codec != 'libopenh264')
        self._encoder_type = codec.upper() if codec else ('NVENC' if nvenc_active else 'SW')

        if not (is_hw or software_active):
            self.push_error(
                'CAPTURE FAILED',
                'HARDWARE ENCODING UNAVAILABLE: no hardware replay encoder is '
                f'active (codec={codec or "unknown"}). Encoder initialization '
                'failed; capture must be restarted. FTHR does not silently switch '
                'to a shorter raw replay buffer.',
                level='error',
                actions=[('RESTART ENGINE', self._restart_capture_engine)],
                key='encoder-init',
                dismiss_on_action=False,
                diagnostic_context={
                    'diagnostic_error': DiagnosticError.ENCODER_INIT_FAILED,
                    'technical_cause': 'ENCODER_INIT_FAILED',
                    'technical_subsystem': 'encoder',
                    'operation': 'startup_verification',
                    'recovery_kind': 'encoder_init',
                    'process_generation': getattr(
                        self, '_engine_process_generation', None),
                    'backend_generation': getattr(
                        self, '_last_native_capture_generation', None),
                    'recovery_status': (
                        'awaiting_replacement_hardware_and_stable_progress'),
                },
            )
            print(f"[UI] Hardware encoding not available (codec: {codec or 'unknown'})")
        else:
            print(f"[UI] Encoding active: {codec or 'NVENC'}")

    # Styles

    def _load_saved_theme(self):
        """Patch Colors class with saved theme so all QSS uses custom values."""
        theme = ThemeManager()
        if not theme.has_any_customization():
            return
        colors = theme.get_all_colors()
        from ui.style import Colors as C
        for token, value in colors.items():
            if hasattr(C, token):
                setattr(C, token, value)
        C.BAR_BG, C.BAR_FG = C.SHELL_BG, C.TEXT
        C.BAR_FG_DIM, C.BAR_HAIRLINE = C.TEXT_DIM, C.SHELL_DIVIDER

    def _apply_styles(self):
        set_theme_style(self, lambda: (f'''
            /* -- Window canvas -- */
            QMainWindow {{ background-color: {Colors.BG}; }}
            QWidget {{
                background-color: {Colors.BG};
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
            }}

            /* -- Unified top bar -- */
            QFrame#topBar {{
                background-color: {Colors.SHELL_BG};
                border: none;
            }}
            QFrame#topBar QLabel {{
                background-color: transparent;
                color: {Colors.TEXT};
            }}
            QFrame#topClusterMain {{
                background: {Colors.SHELL_BG_2};
                border: none;
            }}
            QStackedWidget#mainStack {{
                border: none;
                border-top: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#statusLabel {{ background: transparent; }}

            QFrame#navigationPanel {{
                background: {Colors.SHELL_BG_2};
                border: none;
                border-right: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QFrame#navigationPanel QToolButton {{
                background: transparent;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_LABEL}px;
                font-weight: 400;
                border: 1px solid transparent;
                border-radius: {Sizes.RADIUS_MD}px;
                padding: 8px;
            }}
            QFrame#navigationPanel QToolButton:hover {{
                background: {Colors.SURFACE_2};
                color: {Colors.TEXT};
            }}
            QFrame#navigationPanel QToolButton:checked {{
                background: {Colors.ACCENT_SOFT};
                color: {Colors.TEXT};
                font-weight: 600;
                border-left: 3px solid {Colors.ACCENT};
            }}
            QFrame#navigationPanel QToolButton:focus {{
                border: 1px solid {Colors.ACCENT};
            }}

            /* Settings gear — quiet square button */
            QPushButton#settingsGearBtn {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QPushButton#settingsGearBtn:hover {{
                border-color: {Colors.ACCENT};
            }}

            /* Home button — square icon, matches the settings gear style */
            QPushButton#homeBtn {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QPushButton#homeBtn:hover {{
                border-color: {Colors.ACCENT};
            }}

            /* Window controls — flat against the dark bar */
            QPushButton#winBtn {{
                background-color: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton#winBtn:hover {{
                background-color: {Colors.SURFACE_3};
                color: {Colors.TEXT};
            }}
            QPushButton#powerBtn {{
                background-color: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
            }}
            QPushButton#powerBtn:hover {{
                background-color: {Colors.ERROR};
                color: {Colors.TEXT};
            }}
            QPushButton#closeBtn {{
                background-color: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: 12px;
                font-weight: 600;
            }}
            QPushButton#closeBtn:hover {{
                background-color: {Colors.ERROR};
                color: {Colors.TEXT};
            }}

            {scrollbar_qss()}

            {tooltip_qss()}
        '''))

    def _load_logo(self):
        """Load the top-bar brand mark, retaining the user's logo override."""
        theme = ThemeManager()
        custom_logo = theme.get_custom_icon_path('favicon.ico')
        logo_path = (custom_logo if custom_logo and custom_logo.exists()
                     else Path(__file__).parent / 'assets' / 'fthr_logo.png')
        logo = _load_logo_asset(logo_path)
        if not logo.isNull():
            self._logo_label.setText('')
            self._logo_label.setPixmap(logo.scaled(
                28, 28,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            return
        self._logo_label.setText('FTHR')
        set_theme_style(self._logo_label, lambda: (label_display(Colors.TEXT, Fonts.SIZE_H2, Fonts.TRACK_HEADING)))

    def _apply_theme(self):
        """Rebuild the main window stylesheet from current Colors class values.
        Called by the Customize page after the user clicks Apply Theme."""
        self._apply_styles()
        for widget in (
                getattr(self, 'cap_settings_popup', None),
                getattr(self, 'source_popup', None),
                getattr(self, 'game_detection_popup', None),
                getattr(self, 'hotkey_popup', None),
                getattr(self, 'error_bar', None),
                getattr(self, 'cap_btn', None),
                getattr(self, 'source_btn', None),
                getattr(self, 'game_detection_btn', None),
                getattr(self, 'hotkey_btn', None)):
            refresh = getattr(widget, 'refresh_theme', None)
            if callable(refresh):
                refresh()
        if hasattr(self, 'record_btn'):
            set_theme_style(self.record_btn, lambda: (self._manual_record_button_qss(
                self._manual_record_state == 'recording')))
        _refresh_all_icons()
        self._load_logo()
        self.capture_card.restart()
        if hasattr(self, 'clip_grid'):
            self.clip_grid.refresh_theme()
        self.update()

    # Shutdown

    _FINALIZATION_GRACE_SECONDS = 1.5

    def request_full_exit(self) -> None:
        """Make full exit visually immediate, then clean up on the event loop."""
        if self._shutdown_requested:
            return
        self._shutdown_requested = True
        self._shutdown_timer_started = time.monotonic()
        print('[Lifecycle] ShutdownRequested')
        self._lifecycle_log.info('ShutdownRequested')

        # The user must never watch a frozen main window while a bounded engine
        # or finalization cleanup is in progress. Hiding is intentionally
        # separate from cleanup; X remains a tray action, and only this path
        # reaches QApplication.quit().
        self.hide()
        if self._tray_icon is not None:
            self._tray_icon.hide()
        self._shutdown_mark('UIHidden')
        QTimer.singleShot(0, self._perform_full_shutdown)

    def _shutdown_mark(self, phase: str) -> None:
        started = self._shutdown_timer_started
        elapsed = (float(time.monotonic()) - float(started)
                   if started is not None else 0.0)
        elapsed_text = f'{elapsed:.3f}'
        print(f'[Lifecycle] {phase} +{elapsed:.3f}s')
        self._lifecycle_log.info('%s +%ss', phase, elapsed_text)

    def _wait_for_finalization_grace(self) -> None:
        """Give atomic clip finalization a short shared grace period.

        Workers write through staging paths, so a worker that cannot finish in
        this grace window cannot publish a corrupt final-looking file. The
        next startup retains its normal stale-partial recovery responsibility.
        """
        deadline = time.monotonic() + self._FINALIZATION_GRACE_SECONDS
        for worker in list(getattr(self, '_mux_threads', ())):
            if not worker.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        still_running = sum(
            worker.is_alive() for worker in getattr(self, '_mux_threads', ()))
        if still_running:
            print('[Lifecycle] FinalizationGraceExpired '
                  f'workers={still_running}; staged work was not published')
        self._shutdown_mark('FinalizationGraceComplete')

    def _perform_full_shutdown(self) -> None:
        """One idempotent, bounded shutdown sequence for tray and app exit."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        from core.export_queue import shutdown as shutdown_exports
        from ui.clip_viewer import _share_windows
        for share in tuple(_share_windows):
            share.close()
        shutdown_exports()
        self._persist_window_geometry()
        self.is_capturing = False

        basic_viewer = getattr(self, '_basic_clip_viewer', None)
        if basic_viewer is not None:
            basic_viewer.close()
            self._basic_clip_viewer = None

        active_viewer = getattr(self, '_active_clip_viewer', None)
        if active_viewer is not None:
            try:
                active_viewer.close()
                active_viewer._teardown_player()
                active_viewer.deleteLater()
            except (AttributeError, RuntimeError, TypeError):
                # Shutdown continues when the dialog was destroyed by Qt.
                pass
            self._active_clip_viewer = None
        self._active_editor_session = None
        for session in list(self._editor_sessions):
            self._remove_editor_session(session)

        parked = getattr(self, '_parked_editor_shell', None)
        if parked is not None:
            parked._teardown_player()
            parked.deleteLater()
            self._parked_editor_shell = None

        if hasattr(self, 'status_timer'):
            self.status_timer.stop()
        if hasattr(self, '_save_poll_timer'):
            self._save_poll_timer.stop()
        if hasattr(self, '_gary_timer'):
            self._gary_timer.stop()
        if hasattr(self, '_gary_overlay'):
            self._gary_overlay.set_intensity(0.0)
        if getattr(self, '_gary_recorder_active', False):
            try:
                MicRecorder().stop()
            except Exception:
                # Shutdown continues even if the optional mic backend is gone.
                pass
            self._gary_recorder_active = False
        if hasattr(self, '_save_state'):
            self._save_state.cancel_active(time.monotonic(), reason='shutdown')
        self._prepare_manual_recording_shutdown()
        self._stop_screenshot_jobs()
        self._shutdown_mark('SaveCommandsStopped')

        self._fallback_watch_timer.stop()
        self._game_dismiss_timer.stop()
        self._game_detector.stop()

        self.hotkey_manager.cleanup()
        self._shutdown_mark('HotkeysStopped')
        self._wait_for_finalization_grace()
        try:
            if MicRecorder.is_available():
                MicRecorder().stop()
        except Exception:
            # Shutdown continues even if the optional mic backend is gone.
            pass
        self._shutdown_mark('MicrophoneStopped')

        try:
            from core.camera_recorder import CameraRecorder
            CameraRecorder().stop()
        except Exception as error:
            print(f'[Shutdown] Camera release failed: {error}')
        self._shutdown_mark('CameraStopped')

        try:
            self._keyboard_overlay_capture.stop()
        except Exception as error:
            print(f'[Shutdown] Keyboard overlay release failed: {error}')
        self._shutdown_mark('KeyboardOverlayStopped')

        partial_cancel = getattr(self, '_partial_cleanup_cancel', None)
        if partial_cancel is not None:
            partial_cancel.set()
        partial_worker = getattr(self, '_partial_cleanup_thread', None)
        if partial_worker is not None and partial_worker.is_alive():
            # Recovery checks cancellation between directory entries; retain a
            # hard bound if an underlying filesystem call is slow.
            partial_worker.join(timeout=0.5)
        self.upload_manager.stop()
        self._shutdown_mark('UploadsStopped')
        if hasattr(self, 'clip_grid'):
            self.clip_grid.shutdown()
            self._shutdown_mark('LibraryStopped')
        self.capture_card.close()
        self._shutdown_mark('CaptureCardStopped')
        self.stop_engine()
        # Engine shutdown closes the last fragment even if the dedicated stop
        # acknowledgement missed its bounded grace period.
        if self._manual_record_path is not None:
            self._finalize_manual_recording_file(publish_ui=False)
        self._shutdown_mark('EngineStopped')

        if self._tray_icon is not None:
            self._tray_icon.hide()
            self._tray_icon.deleteLater()
            self._tray_icon = None
        self._shutdown_mark('TrayRemoved')
        self._shutdown_mark('ShutdownComplete')
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _prepare_manual_recording_shutdown(self) -> None:
        state = getattr(self, '_manual_record_state', 'idle')
        if state == 'idle':
            return
        self._manual_record_timer.stop()
        if state == 'preparing':
            self._manual_record_state = 'idle'
            self._pending_manual_record_path = None
            self._restore_clip_capture_profile()
            return
        if state == 'finalizing':
            worker = self._manual_record_finalize_thread
            if worker is not None:
                worker.join(timeout=5.0)
            recording = self._manual_record_path
            if (recording is not None and recording.is_file()
                    and (worker is None or not worker.is_alive())):
                self._complete_manual_recording_file(
                    recording, publish_ui=False)
            return
        if self.bridge and self.bridge.is_connected():
            self.bridge.stop_manual_recording()
            self._manual_record_state = 'stopping'
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                response = self.bridge.peek_manual_recording_response()
                if response is not None:
                    kind, detail = response
                    self.bridge.consume_manual_recording_response()
                    if kind == 'stopped':
                        self._finalize_manual_recording_file(publish_ui=False)
                        return
                    if kind == 'error':
                        # Fragmented MP4 keeps every committed GOP usable even
                        # if the close itself reports an error.
                        self._fail_manual_recording(
                            detail or 'The recording closed with an error.',
                            keep_recording=True)
                        return
                time.sleep(0.05)

    def closeEvent(self, event):
        if self._shutdown_requested:
            event.accept()
            return
        close_to_tray = (
            sys.platform == 'win32'
            and bool(self.settings_manager.get('close_to_tray', True))
        )
        if (close_to_tray
                and self._tray_icon is not None
                and self._tray_icon.isVisible()):
            self._persist_window_geometry()
            event.ignore()
            self._hide_main_window_to_tray()
            return
        # With close-to-tray disabled, or when Windows cannot expose a tray,
        # X deliberately uses the same bounded full-exit path as the power icon.
        self._persist_window_geometry()
        event.ignore()
        self.request_full_exit()

    def _show_editor_session(self, session, thumb_pixmap, *, animate_restore=False):
        self._cancel_editor_transition()
        from ui.clip_editor_window import ClipEditorWindow
        clip_path = session.clip_path
        upload_on = self.upload_manager.is_enabled()
        options = dict(thumb_pixmap=thumb_pixmap, settings_manager=self.settings_manager,
                       upload_enabled=upload_on, metadata_manager=self.clip_metadata_manager,
                       linked_import=self.clip_grid.is_linked_import(clip_path))
        viewer = getattr(self, '_parked_editor_shell', None)
        self._parked_editor_shell = None
        reused = viewer is not None
        if reused:
            if not viewer.replace_clip(clip_path, session_state=session.state, **options):
                viewer._teardown_player(wait_for_workers=False)
                viewer.hide()
                self._parked_editor_shell = viewer
                return
            # A hidden maximized QWidget otherwise keeps its old window-state
            # bit and skips the platform maximize geometry negotiation.
            viewer.setWindowState(Qt.WindowState.WindowNoState)
        else:
            viewer = ClipEditorWindow(clip_path, self.bridge, self,
                                      session_state=session.state, **options)
        viewer.editor_session = session
        if not reused:
            viewer.upload_requested.connect(self.upload_manager.enqueue_upload)
            viewer.export_error.connect(self.push_error)
            viewer.clip_navigation_requested.connect(
                lambda step: self._navigate_editor_clip(viewer, step))
            viewer.minimize_requested.connect(lambda: self._minimize_active_editor(viewer))
            viewer.sidebar_navigation_requested.connect(self._navigate_sidebar)
            viewer.panel_switch_requested.connect(self._toggle_settings_page)
            viewer.finished.connect(lambda _result: self._editor_finished(viewer, viewer.editor_session))
        session.state = None
        self._active_clip_viewer = viewer
        self._active_editor_session = session
        self.clip_grid.set_background_paused(True)
        self._settings_page_widget.set_background_ui_paused(True)
        self._refresh_editor_sidebar()
        # Nonmodal ownership lets a minimized editor disappear completely,
        # without a nested exec() loop or an OS-minimized dialog on the shell.
        if animate_restore and session.window_geometry is not None:
            if session.window_maximized:
                viewer.move(session.window_geometry.topLeft())
            else:
                viewer.setGeometry(session.window_geometry)
        if animate_restore and self.isVisible() and not reduce_motion(self):
            # Let the window manager resolve the final maximized size before
            # capturing. Reveal this same native surface at the end; showing
            # it maximized a second time can change the target geometry.
            viewer.setWindowOpacity(0.0)
            if session.window_maximized:
                viewer.showMaximized()
            else:
                viewer.show()

            def begin_restore():
                if self._active_clip_viewer is not viewer:
                    return
                if reduce_motion(self):
                    viewer.setWindowOpacity(1.0)
                    viewer.raise_()
                    viewer.activateWindow()
                    return
                end = QRect(viewer.mapToGlobal(QPoint(0, 0)), viewer.size())
                flight = SnapshotFlight(self, viewer.grab(), sidebar_rect(session.button), end)
                self._editor_flight = flight
                flight._animation.finished.connect(self._editor_transition_finished)

                def reveal():
                    if self._active_clip_viewer is viewer:
                        viewer.setWindowOpacity(1.0)
                        viewer.raise_()
                        viewer.activateWindow()

                flight._animation.finished.connect(reveal)
                flight.start()

            # Native maximize/resize notifications can arrive after the first
            # queued callback. Allow two display frames for that handoff.
            QTimer.singleShot(32, viewer, begin_restore)
            return
        if (animate_restore and session.window_geometry is not None
                and not session.window_maximized):
            viewer.show()
        else:
            viewer.showMaximized()
        viewer.raise_()
        viewer.activateWindow()


    def _editor_finished(self, viewer, session):
        viewer._teardown_player()
        viewer.deleteLater()
        was_active = self._active_clip_viewer is viewer
        if was_active:
            self._active_clip_viewer = None
            self._active_editor_session = None
        if getattr(self, '_shutdown_requested', False):
            return
        if session.state is None:
            self._remove_editor_session(session)
            if was_active:
                self._set_main_page(self.main_stack.currentIndex())


    def _navigate_editor_clip(self, viewer, step):
        if viewer is not self._active_clip_viewer or viewer._closing:
            return
        destination = self.clip_grid.adjacent_clip(viewer.clip_path, step)
        if destination is not None:
            session = self._active_editor_session
            existing = next((other for other in self._editor_sessions
                             if other is not session
                             and os.path.normcase(other.clip_path) == os.path.normcase(destination)), None)
            state = existing.state if existing is not None else None
            if not viewer.replace_clip(
                    destination, session_state=state,
                    navigation_step=step,
                    thumb_pixmap=QPixmap(),
                    linked_import=self.clip_grid.is_linked_import(destination)):
                return
            if existing is not None:
                self._remove_editor_session(session)
                session = existing
            else:
                session.clip_path = destination
            session.state = None
            self._active_editor_session = session
            viewer.editor_session = session
            self._refresh_editor_sidebar()



# Settings page — shared layout helpers

def _flat_section_header(title: str) -> QWidget:
    """Natural-case heading with a quiet rule separating settings sections."""
    row = QWidget()
    row.setStyleSheet('background: transparent;')
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(Sizes.ROW_GAP)
    lbl = QLabel(title)
    set_theme_style(lbl,
        lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_H3}px; font-weight: 600;'
        f' letter-spacing: 0px; background: transparent; border: none;'
        f' font-family: {Fonts.DISPLAY};'))
    hl.addWidget(lbl)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    set_theme_style(line, lambda: (f'background: {Colors.SHELL_DIVIDER}; border: none;'))
    hl.addWidget(line, 1)
    return row


def _settings_hsep() -> QFrame:
    """Thin horizontal divider between settings sections."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    set_theme_style(line, lambda: (f'background: {Colors.SHELL_DIVIDER}; border: none;'))
    return line


def _settings_vsep() -> QFrame:
    """Thin vertical divider between settings columns."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedWidth(1)
    set_theme_style(line, lambda: (f'background: {Colors.SHELL_DIVIDER}; border: none;'))
    return line


# Lightweight settings page stack

class SlidingStackedWidget(QWidget):
    """QStackedWidget-compatible container that lays out only the selected page.

    Horizontal transitions paint snapshots without moving live settings trees.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pages = []
        self._current = -1
        self._slide = None

    # API mirrors QStackedWidget

    def addWidget(self, widget):
        widget.setParent(self)
        idx = len(self._pages)
        self._pages.append(widget)
        if idx == 0:
            self._current = 0
            widget.setGeometry(0, 0, self.width(), self.height())
            widget.show()
        else:
            widget.setGeometry(0, 0, self.width(), self.height())
            widget.hide()
        return idx

    def replaceWidget(self, index, widget):
        self._finish_slide()
        old = self._pages[index]
        old.hide()
        self._pages[index] = widget
        widget.setParent(self)
        widget.setGeometry(0, 0, self.width(), self.height())
        widget.setVisible(index == self._current)
        old.deleteLater()

    def _finish_slide(self):
        if self._slide is not None:
            self._slide.animation.stop()
            self._slide.hide()
            self._slide.deleteLater()
            self._slide = None

    def setCurrentIndex(self, new_idx, *, direction=None):
        if new_idx < 0 or new_idx >= len(self._pages):
            return
        ensure = self.__dict__.get('_ensure_page')
        if ensure is not None:
            ensure(new_idx)
        if new_idx == self._current:
            return
        previous = self._current
        animate = self.isVisible() and previous >= 0 and not reduce_motion(self)
        self._finish_slide()
        outgoing = self.grab() if animate else None
        if self._current >= 0:
            self._pages[self._current].hide()
        self._current = new_idx
        page = self._pages[new_idx]
        page.setGeometry(0, 0, self.width(), self.height())
        page.show()
        page.raise_()
        if animate:
            if page.layout() is not None:
                page.layout().activate()
            self._slide = SnapshotSlide(
                self, outgoing, page.grab(),
                direction if direction is not None else (1 if new_idx > previous else -1),
                horizontal=True, use_gpu=False,
            )
            self._slide.show()
            self._slide.raise_()
            self._slide.animation.finished.connect(self._finish_slide)
            self._slide.animation.start()

    def currentIndex(self):
        return self._current

    def currentWidget(self):
        if 0 <= self._current < len(self._pages):
            return self._pages[self._current]
        return None

    def widget(self, idx):
        if 0 <= idx < len(self._pages):
            ensure = self.__dict__.get('_ensure_page')
            if ensure is not None:
                ensure(idx)
            return self._pages[idx]
        return None

    def count(self):
        return len(self._pages)

    # Sizing

    def sizeHint(self):
        s = QSize(0, 0)
        for p in self._pages:
            s = s.expandedTo(p.sizeHint())
        return s

    def minimumSizeHint(self):
        s = QSize(0, 0)
        for p in self._pages:
            s = s.expandedTo(p.minimumSizeHint())
        return s

    def resizeEvent(self, event):
        self._finish_slide()
        super().resizeEvent(event)
        if self._current >= 0:
            w, h = self.width(), self.height()
            self._pages[self._current].setGeometry(0, 0, w, h)

    def hideEvent(self, event):
        self._finish_slide()
        super().hideEvent(event)


# Full-screen settings page (embedded in main content stack)

class _SettingsSlider(QSlider):
    """Audio slider with absolute click-to-set and a forgiving hit area."""

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
        span = max(1, groove.width() - handle.width())
        slider_position = position.x() - groove.x() - handle.width() / 2
        slider_position = max(0.0, min(float(span), slider_position))
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(round(slider_position)), span,
            option.upsideDown)

    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and self.isEnabled()):
            self._dragging_from_anywhere = True
            self.setSliderDown(True)
            self.setSliderPosition(self._value_from_position(
                event.position().toPoint()))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._dragging_from_anywhere
                and event.buttons() & Qt.MouseButton.LeftButton):
            self.setSliderPosition(self._value_from_position(
                event.position().toPoint()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if (self._dragging_from_anywhere
                and event.button() == Qt.MouseButton.LeftButton):
            self.setSliderPosition(self._value_from_position(
                event.position().toPoint()))
            self._dragging_from_anywhere = False
            self.setSliderDown(False)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class _SettingsPage(QWidget):
    close_requested           = Signal()
    camera_unavailable        = Signal(str)
    _camera_start_result      = Signal(int, int, bool, str)
    _camera_devices_result   = Signal(object, str)
    clips_directory_changed  = Signal(str)
    imported_folders_changed  = Signal()
    gary_settings_changed     = Signal()
    _playback_inventory_ready = Signal(object)
    notification_monitor_changed = Signal()
    error_notifications_changed = Signal(bool)
    upload_connection_failed = Signal(str)
    close_to_tray_changed        = Signal(bool)
    kofi_widget_changed = Signal(bool)
    encoder_config_changed    = Signal()
    audio_capture_changed     = Signal(bool)
    audio_capture_mode_changed = Signal(str)
    capture_card_changed      = Signal(bool)
    background_ui_pause_changed = Signal(bool)
    encoder_capabilities_ready = Signal(object)
    diagnostic_export_requested = Signal()

    _lazy_control_categories = {
        '_camera_preview_timer': 3,
        '_customize_page': 4,
        '_import_folders_container': 1,
        '_import_folders_layout': 1,
        '_keyboard_preview_timer': 3,
        '_mic_combo_connected': 2,
        '_scan_btn': 1,
        '_scan_pending': 1,
        '_scan_results_container': 1,
        '_scan_results_layout': 1,
        'active_encoder_lbl': 1,
        'audio_capture_check': 2,
        'audio_separation_check': 2,
        'camera_check': 3,
        'camera_device_combo': 3,
        'clear_image_overlays_btn': 3,
        'clips_directory_edit': 1,
        'codec_combo': 1,
        'encoder_apply_btn': 1,
        'encoder_combo': 1,
        'encoder_detection_lbl': 1,
        'encoder_preset_combo': 1,
        'encoder_preset_row': 1,
        'encoder_refresh_btn': 1,
        'gary_enabled_check': 2,
        'gary_preview': 2,
        'hide_deleted_timeline_segments_check': 1,
        'grid_show_edited_duration_check': 1,
        'image_overlay_check': 3,
        'image_overlay_count': 3,
        'image_overlay_fit': 3,
        'image_overlay_opacity': 3,
        'image_overlay_opacity_value': 3,
        'image_overlay_selector': 3,
        'input_overlay_preview_background_path': 3,
        'input_overlay_preview_source': 3,
        'keyboard_color_swatch': 3,
        'keyboard_color_value': 3,
        'keyboard_intensity': 3,
        'keyboard_intensity_value': 3,
        'keyboard_overlay_check': 3,
        'keyboard_pick_color': 3,
        'keyboard_source_preview': 3,
        'keyboard_window_combo': 3,
        'mic_combo': 2,
        'mic_gain_label': 2,
        'mic_gain_warning': 2,
        'mic_level_meter': 2,
        'mic_loopback_check': 2,
        'mic_mono_check': 2,
        'mic_vol_slider': 2,
        'mic_vol_value': 2,
        'notif_monitor_combo': 2,
        'notification_sounds_check': 2,
        'performance_audio_check': 5,
        'performance_background_pause_check': 5,
        'performance_clip_preview_check': 5,
        'performance_encoder_apply_btn': 5,
        'performance_encoder_preset_combo': 5,
        'performance_encoder_preset_row': 5,
        'performance_encoder_section': 5,
        'playback_combo': 2,
        'recording_directory_edit': 1,
        'reduce_ui_animations_check': 5,
        'remove_image_overlay_btn': 3,
        'separate_audio_check': 2,
        'shortcut_settings': 6,
        'sound_sliders': 2,
        'sound_value_labels': 2,
        'timeline_preview_strip_enabled_check': 1,
        'unified_overlay_editor': 3,
        'watermark_check': 1,
    }

    def __getattr__(self, name):
        # Explicit access by older integrations still materializes its category.
        # Internal lifecycle guards must use __dict__ to remain non-creating.
        index = self._lazy_control_categories.get(name)
        if (index is not None and '_loaded_categories' in self.__dict__
                and index not in self._loaded_categories
                and index not in self._building_categories):
            self._ensure_category(index)
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError(name)

    def _ensure_category(self, index):
        if index in self._loaded_categories or index in self._building_categories:
            return
        self._building_categories.add(index)
        try:
            builders = {
                0: self._make_general_page, 1: self._make_clip_page,
                2: self._make_audio_page, 3: self._make_visuals_page,
                4: self._make_customize_page, 5: self._make_performance_page,
                6: self._make_shortcuts_page,
            }
            page = builders[index]()
            self.stack.replaceWidget(index, page)
            self._loaded_categories.add(index)
            if index == 2:
                self._load_audio_settings()
            if index in (1, 5):
                if index == 1 and self._encoder_inventory_ready:
                    self._on_encoder_capabilities_ready(self._encoder_capabilities)
                else:
                    self._sync_encoder_controls(mark_dirty=False, refresh_clip=index == 1)
            page.ensurePolished()
        finally:
            self._building_categories.discard(index)

    def _make_customize_page(self):
        from ui.customize_page import CustomizePage
        self._customize_page = CustomizePage(self.sm)
        self._customize_page.theme_applied.connect(self._on_theme_applied)
        return self._customize_page

    def _make_shortcuts_page(self):
        from ui.keyboard_navigation import KeyboardShortcutSettings
        self.shortcut_settings = KeyboardShortcutSettings(self.navigation_bindings)
        return self.shortcut_settings

    def _init_autostart_checkbox(self):
        from core.windows_autostart import is_packaged_launch, read_enabled
        packaged = is_packaged_launch()
        self.autostart_check.setEnabled(packaged)
        self.autostart_check.setToolTip(
            'Available in installed FTHR builds.' if packaged else
            'Development runs never modify Windows startup registration.')
        self.autostart_check.setChecked(read_enabled())

    def _on_autostart_changed(self, state):
        from core.windows_autostart import read_enabled, set_enabled
        requested = state == Qt.CheckState.Checked.value
        if set_enabled(requested):
            print(f'[Lifecycle] AutostartChanged enabled={requested}')
            return
        # Registry write failures and an externally stale entry are reflected
        # immediately instead of leaving a checkbox that lies about Windows.
        self.autostart_check.blockSignals(True)
        self.autostart_check.setChecked(read_enabled())
        self.autostart_check.blockSignals(False)
        print('[Lifecycle] Autostart registration could not be changed')

    def _on_kofi_widget_toggled(self, enabled):
        self.sm.set('show_kofi_widget', bool(enabled))
        self.sm.save_settings()
        self.kofi_widget_changed.emit(bool(enabled))

    def _on_close_to_tray_toggled(self, enabled: bool) -> None:
        if self.sm is None or sys.platform != 'win32':
            return
        self.sm.set('close_to_tray', bool(enabled))
        self.sm.save_settings()
        self.close_to_tray_changed.emit(bool(enabled))
        print(f'[Lifecycle] CloseToTrayChanged enabled={bool(enabled)}')

    def _on_error_notifications_toggled(self, enabled: bool) -> None:
        if self.sm is None:
            return
        enabled = bool(enabled)
        self.sm.set('error_notifications_enabled', enabled)
        self.sm.save_settings()
        self.error_notifications_changed.emit(enabled)

    def __init__(self, settings_manager: SettingsManager = None, parent=None,
                 keyboard_capture: ThirdPartyKeyboardCapture | None = None):
        super().__init__(parent)
        self.sm = settings_manager
        from core.navigation_bindings import navigation_bindings_for
        self.navigation_bindings = navigation_bindings_for(settings_manager)
        self._keyboard_capture = keyboard_capture
        self._keyboard_windows: list[dict] = []
        self._background_ui_paused = False
        self._camera_start_generation = 0
        self._camera_pending_device_index: int | None = None
        self.setObjectName('settingsPage')
        self._loopback_stream = None
        self._mic_discovery_generation = 0
        self._mic_discovery_job: MicrophoneDiscoveryJob | None = None
        self._mic_discovery_context: dict[str, object] | None = None
        self._mic_discovery_poll_timer = QTimer(self)
        self._mic_discovery_poll_timer.setInterval(25)
        self._mic_discovery_poll_timer.timeout.connect(
            self._poll_mic_discovery)
        self._presets_mgr = PresetsManager()
        self._encoder_capabilities: tuple[EncoderCapability, ...] = ()
        self._encoder_probe_started = False
        self._encoder_probe_from_cache = False
        self._encoder_inventory_ready = False
        self._audio_preview_timer = QTimer(self)
        self._audio_preview_timer.setSingleShot(True)
        self._audio_preview_timer.setInterval(180)
        self._audio_preview_timer.timeout.connect(
            self._start_audio_preview_if_current)
        self._pending_category_index = 0
        self._category_switch_timer = QTimer(self)
        self._category_switch_timer.setSingleShot(True)
        self._category_switch_timer.setInterval(0)
        self._category_switch_timer.timeout.connect(
            self._apply_pending_category)
        self.encoder_capabilities_ready.connect(
            self._on_encoder_capabilities_ready)
        self._camera_start_result.connect(self._on_camera_start_result)
        self._camera_devices_result.connect(self._on_camera_devices_result)
        self._loaded_categories = set()
        self._building_categories = set()
        self._pending_encoder_preset = None
        self._encoder_dirty = False
        self._setup_ui()
        self._apply_styles()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Top category tab bar --
        tab_bar = QFrame()
        tab_bar.setObjectName('settingsTabBar')
        tab_layout = QHBoxLayout(tab_bar)
        tab_layout.setContentsMargins(24, 0, 24, 0)
        tab_layout.setSpacing(0)
        tab_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        self._cat_titles = [
            'General', 'Clip', 'Audio', 'Visuals', 'Customize', 'Performance', 'Shortcuts']
        _tab_icons = [
            'settings(general).png',
            'clip.png',
            'sound.png',
            'visuals.png',
            'personalize.png',
            'performance.png',
            'shortcuts.png',
        ]

        self._tab_buttons: list = []
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)

        # strict=False keeps the existing behaviour: if the icon list and the
        # title list ever fall out of step, the extra tabs are dropped rather
        # than raising in the middle of building the settings page.
        for i, (title, icon_file) in enumerate(
                zip(self._cat_titles, _tab_icons, strict=False)):
            btn = QToolButton()
            btn.setText(title)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            icon = _load_icon(icon_file, 36)
            btn.setIcon(icon)
            btn.setIconSize(QSize(36, 36))
            _register_icon_widget(btn, icon_file, 36)
            btn.setCheckable(True)
            btn.setObjectName('settingsTabBtn')
            btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self._tab_group.addButton(btn, i)
            tab_layout.addWidget(btn)
            self._tab_buttons.append(btn)

        self._tab_group.idClicked.connect(self._on_category_changed)
        main_layout.addWidget(tab_bar)

        # Thin separator line
        divider = QFrame()
        divider.setObjectName('settingsDivider')
        divider.setFrameShape(QFrame.Shape.HLine)
        divider.setFixedHeight(1)
        main_layout.addWidget(divider)

        # Content area --
        content = QWidget()
        content.setObjectName('settingsContent')
        self._settings_content = content
        content_layout = QVBoxLayout(content)
        # The viewport reaches the window edge; bottom padding belongs to the
        # scrollable page so controls do not vanish above a fixed empty strip.
        content_layout.setContentsMargins(Sizes.PAGE_INSET, Sizes.PAGE_INSET, Sizes.PAGE_INSET, 0)
        content_layout.setSpacing(0)

        self.stack = SlidingStackedWidget()
        self.stack._ensure_page = self._ensure_category
        for _ in self._cat_titles:
            self.stack.addWidget(QWidget())
        self._ensure_category(0)
        content_layout.addWidget(self.stack, stretch=1)

        main_layout.addWidget(content, stretch=1)

        # Select first tab
        self._tab_buttons[0].setChecked(True)

    def _on_category_changed(self, idx):
        """Coalesce rapid tab clicks so intermediate heavy pages are not painted."""
        if idx < 0 or idx >= self.stack.count():
            return
        self._pending_category_index = idx
        self._pending_category_direction = None
        self._audio_preview_timer.stop()
        if idx != 2 and 'mic_level_meter' in self.__dict__:
            self.mic_level_meter.stop()
            self._stop_loopback()
        if idx != 3 and '_keyboard_preview_timer' in self.__dict__:
            self._keyboard_preview_timer.stop()
        self._category_switch_timer.start()

    def navigate_category(self, step):
        idx = (self._pending_category_index + step) % self.stack.count()
        self._tab_buttons[idx].setChecked(True)
        self._on_category_changed(idx)
        self._pending_category_direction = 1 if step > 0 else -1

    def _apply_pending_category(self):
        if getattr(self, '_background_ui_paused', False):
            return
        idx = self._pending_category_index
        self._ensure_category(idx)
        self.stack.setCurrentIndex(
            idx, direction=getattr(self, '_pending_category_direction', None))
        if idx in (1, 5):
            self._start_encoder_probe()
        # Audio sub-page is index 2 — start the live meter only there
        if 'mic_level_meter' in self.__dict__:
            self._audio_preview_timer.stop()
            if (idx == 2 and self.isVisible()
                    and not getattr(self, '_background_ui_paused', False)):
                # Opening a native audio device is expensive. A short debounce
                # avoids start/stop churn while the user is skimming tabs.
                self._audio_preview_timer.start()
            else:
                self.mic_level_meter.stop()
                self._stop_loopback()
                if 'mic_loopback_check' in self.__dict__:
                    self.mic_loopback_check.blockSignals(True)
                    self.mic_loopback_check.setChecked(False)
                    self.mic_loopback_check.blockSignals(False)
        if '_camera_preview_timer' in self.__dict__:
            if idx == 3 and self.isVisible() and self.sm.get('camera_enabled', False):
                self._camera_preview_timer.start()
            else:
                self._camera_preview_timer.stop()
        if '_keyboard_preview_timer' in self.__dict__:
            if (idx == 3 and self.isVisible()
                    and not getattr(self, '_background_ui_paused', False)):
                self._keyboard_preview_timer.start()
            else:
                self._keyboard_preview_timer.stop()

    def _start_audio_preview_if_current(self) -> None:
        if (not self.isVisible() or 'mic_level_meter' not in self.__dict__
                or self.stack.currentIndex() != 2
                or getattr(self, '_background_ui_paused', False)):
            return
        self.mic_level_meter.set_gain(self.mic_vol_slider.value() / 100.0)
        self.mic_level_meter.start(self._selected_mic_index())
        if self.mic_loopback_check.isChecked() and self._loopback_stream is None:
            self._start_loopback(self._selected_mic_index())

    def _set_settings_content_surface(self) -> None:
        """Keep the settings canvas on the selected background token."""
        if not hasattr(self, '_settings_content'):
            return
        set_theme_style(self._settings_content,
            lambda: (f'QWidget#settingsContent {{ background-color: {Colors.BG}; }}'))

    def _make_general_page(self):
        from ui.upload_settings_widget import UploadSettingsWidget

        page = QWidget()
        page.setStyleSheet('background: transparent;')
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(0)

        # System --
        layout.addWidget(_flat_section_header('System'))
        layout.addSpacing(12)
        self.autostart_check = QCheckBox('Autostart with Windows')
        if sys.platform != 'win32':
            self.autostart_check.setVisible(False)
        layout.addWidget(self.autostart_check)
        self._init_autostart_checkbox()
        self.autostart_check.stateChanged.connect(self._on_autostart_changed)
        layout.addSpacing(8)

        self.close_to_tray_check = QCheckBox(
            'Minimize to system tray when closing FTHR')
        set_theme_style(self.close_to_tray_check, checkbox_qss)
        self.close_to_tray_check.setChecked(
            bool(self.sm.get('close_to_tray', True)))
        self.close_to_tray_check.setVisible(sys.platform == 'win32')
        self.close_to_tray_check.setToolTip(
            'X hides FTHR in the system tray. The power button fully shuts '
            'down capture; Minimize stays on the taskbar.')
        self.close_to_tray_check.toggled.connect(
            self._on_close_to_tray_toggled)
        layout.addWidget(self.close_to_tray_check)
        layout.addSpacing(6)

        self.kofi_widget_check = QCheckBox('Show Ko-fi donation widget')
        set_theme_style(self.kofi_widget_check, checkbox_qss)
        self.kofi_widget_check.setChecked(bool(self.sm.get('show_kofi_widget', True)))
        self.kofi_widget_check.toggled.connect(self._on_kofi_widget_toggled)
        layout.addWidget(self.kofi_widget_check)

        # Notifications --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Notifications'))
        layout.addSpacing(12)
        self.error_notifications_check = QCheckBox(
            'Show capture error messages')
        set_theme_style(self.error_notifications_check, checkbox_qss)
        self.error_notifications_check.setChecked(bool(self.sm.get(
            'error_notifications_enabled', True)))
        self.error_notifications_check.setToolTip(
            'Show messages at the bottom only when capture, clip saving, '
            'upload, or an upload connection fails.')
        self.error_notifications_check.toggled.connect(
            self._on_error_notifications_toggled)
        layout.addWidget(self.error_notifications_check)

        layout.addSpacing(12)
        layout.addWidget(QLabel('Capture card'))
        self.capture_card_mode_combo = QComboBox()
        set_theme_style(self.capture_card_mode_combo, combo_qss)
        for label, mode in (
                ('All notifications', 'all'),
                ('Clips, recordings and screenshots only', 'capture'),
                ('Game detection / window switching only', 'detection'),
                ('Off', 'off')):
            self.capture_card_mode_combo.addItem(label, mode)
        saved_mode = self.sm.get('capture_card_mode', None)
        if saved_mode is None:
            saved_mode = 'all' if self.sm.get('capture_card_enabled', True) else 'off'
        self.capture_card_mode_combo.setCurrentIndex(max(
            0, self.capture_card_mode_combo.findData(saved_mode)))
        self.capture_card_mode_combo.currentIndexChanged.connect(
            self._on_capture_card_mode_changed)
        layout.addWidget(self.capture_card_mode_combo)

        # Upload --
        layout.addSpacing(24)
        self._upload_settings_widget = UploadSettingsWidget(
            getattr(self.sm, '_upload_manager_ref', self.sm),
            no_scroll=True,
        )
        self._upload_settings_widget.connection_failed.connect(
            self.upload_connection_failed.emit)
        layout.addWidget(self._upload_settings_widget)

        # Export presets --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Export Presets'))
        layout.addSpacing(10)
        layout.addSpacing(10)
        from ui.export_presets_widget import ExportPresetsWidget
        self.export_presets_widget = ExportPresetsWidget(parent=page)
        layout.addWidget(self.export_presets_widget)

        # Troubleshooting --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Troubleshooting'))
        layout.addSpacing(10)
        diagnostic_hint = QLabel(
            'After reproducing an alpha bug, export a privacy-redacted report '
            'containing bounded hardware, capture, audio, playback, export, and '
            'library diagnostics. Nothing is uploaded automatically.')
        diagnostic_hint.setWordWrap(True)
        set_theme_style(diagnostic_hint, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)))
        layout.addWidget(diagnostic_hint)
        layout.addSpacing(8)
        self.export_diagnostic_btn = QPushButton('Export diagnostic report')
        set_theme_style(self.export_diagnostic_btn, button_outline_qss)
        self.export_diagnostic_btn.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor))
        self.export_diagnostic_btn.clicked.connect(
            self.diagnostic_export_requested.emit)
        layout.addWidget(self.export_diagnostic_btn, 0,
                         Qt.AlignmentFlag.AlignLeft)
        from ui.diagnostic_report import DiagnosticReportFile
        self.diagnostic_report_file = DiagnosticReportFile(page)
        layout.addWidget(self.diagnostic_report_file, 0,
                         Qt.AlignmentFlag.AlignLeft)

        # Focus pause has no Windows replay implementation. Do not expose a
        # control that would route through the unrelated legacy record toggle.
        if focus_pause_supported(sys.platform):
            layout.addSpacing(24)
            layout.addWidget(_flat_section_header('Anticheat Detection'))
            layout.addSpacing(12)

            self.anticheat_check = QCheckBox(
                'Pause recording when game is unfocused')
            set_theme_style(self.anticheat_check, checkbox_qss)
            self.anticheat_check.setChecked(
                self.sm.get('anticheat_detection_enabled', False))
            self.anticheat_check.toggled.connect(self._on_anticheat_toggled)
            layout.addWidget(self.anticheat_check)
            layout.addSpacing(4)

        # Settings Presets --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Settings Presets'))
        layout.addSpacing(12)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)

        self.preset_combo = _DropdownCombo()
        set_theme_style(self.preset_combo, combo_qss)
        self.preset_combo.setMinimumWidth(160)
        self._refresh_preset_combo()
        preset_row.addWidget(self.preset_combo, 1)

        load_btn = QPushButton('Load')
        set_theme_style(load_btn, button_primary_qss)
        load_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        load_btn.clicked.connect(self._on_preset_load)
        preset_row.addWidget(load_btn)

        save_btn = QPushButton('Save')
        set_theme_style(save_btn, button_outline_qss)
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.clicked.connect(self._on_preset_save)
        preset_row.addWidget(save_btn)

        del_btn = QPushButton('Delete')
        set_theme_style(del_btn, button_outline_qss)
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        del_btn.clicked.connect(self._on_preset_delete)
        preset_row.addWidget(del_btn)

        layout.addLayout(preset_row)

        # App Credits --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('App Credits'))
        layout.addSpacing(10)
        credits_hint = QLabel(
            'FTHRClips\n\n'
            'Haakon\n'
            'Founder & Head of Design\n\n'
            'Tom\n'
            'Co-founder & Head of Development\n\n'
            'Special thanks to our helpers\n\n'
            'benno0_ on Discord, originally suggested the upload feature\n'
            'assassin_gamer on Discord, suggested the stretch feature for the clip editor\n'
            'xc_ on Discord, suggested the import clips folder\n\n'
            'Also thanks to\n\n'
            'petiseba\n'
            'pokusan_\n\n'
            'For being labrats :D\n\n'
            'Much love to everyone testing our alpha :D')
        credits_hint.setWordWrap(True)
        set_theme_style(credits_hint, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        layout.addWidget(credits_hint)

        layout.addStretch()

        # Wrap everything in a scroll area so the combined content fits
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        set_theme_style(scroll, scrollbar_qss)
        scroll.setWidget(page)

        wrapper = QWidget()
        wrapper.setStyleSheet('background: transparent;')
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(scroll)

        return wrapper

    # Import Clips helpers --

    def _refresh_import_folders_list(self):
        while self._import_folders_layout.count():
            item = self._import_folders_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        folders = self.sm.get('imported_clip_folders', []) if self.sm else []

        if not folders:
            lbl = QLabel('No import folders added yet.')
            set_theme_style(lbl, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)))
            self._import_folders_layout.addWidget(lbl)
            return

        for folder in folders:
            self._import_folders_layout.addWidget(self._make_import_folder_row(folder))

    def _make_import_folder_row(self, path: str) -> QFrame:
        row = QFrame()
        set_theme_style(row, lambda: (f'QFrame {{ background: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER}; }}'))
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 6, 6, 6)
        rl.setSpacing(8)

        display_path = path
        if not os.path.isdir(path):
            display_path += '  —  MISSING (remove link or reconnect drive)'
        path_lbl = QLabel(display_path)
        set_theme_style(path_lbl, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        path_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        rl.addWidget(path_lbl, 1)

        remove_btn = IconButton('close.png', label='Close')
        remove_btn.setFixedSize(22, 22)
        remove_btn.setToolTip('Remove this folder')
        remove_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(remove_btn, lambda: (f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: 14px; font-weight: 600; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR}; color: {Colors.ERROR}; }}'))
        remove_btn.clicked.connect(lambda _, p=path: self._remove_import_folder(p))
        rl.addWidget(remove_btn)
        return row

    def _remove_import_folder(self, path: str):
        if self.sm is None:
            return
        folders = remove_import_root(
            self.sm.get('imported_clip_folders', []), path)
        self.sm.set('imported_clip_folders', folders)
        self.sm.save_settings()
        self._refresh_import_folders_list()
        self.imported_folders_changed.emit()

    def _on_import_folder_add(self):
        folder = QFileDialog.getExistingDirectory(
            self, 'Select Clips Folder',
            str(Path.home() / 'Videos'),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder or self.sm is None:
            return
        folders = add_import_root(
            self.sm.get('imported_clip_folders', []), folder)
        self.sm.set('imported_clip_folders', folders)
        self.sm.save_settings()
        self._refresh_import_folders_list()
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != folder]
        self._rebuild_scan_results()
        self.imported_folders_changed.emit()

    def _on_clips_folder_browse(self):
        if self.sm is None:
            return
        current_error = None
        try:
            current = str(clips_directory_from(self.sm))
        except StoragePathError as error:
            current_error = error
            # This is only the chooser's starting location. It is never saved
            # as a replacement storage root unless the user selects it.
            current = str(Path.home())
            self._report_storage_change_failure(error)
        folder = QFileDialog.getExistingDirectory(
            self, 'Select Clips Storage Folder', current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            if current_error is not None:
                print(f'[Storage] Clips folder selection cancelled: {current_error}')
            return

        self._commit_clips_directory(folder)

    def _on_clips_folder_use_default(self):
        if self.sm is None:
            return
        try:
            default = default_clips_directory()
        except StoragePathError as error:
            self._report_storage_change_failure(error)
            return
        self._commit_clips_directory(default)

    def _report_storage_change_failure(self, error) -> None:
        detail = str(error)
        emit_event('storage', 'directory_change_failed', state='FAILED',
                   detail=detail)
        FthrMessageDialog.warning(self, 'Storage location unavailable', detail)

    def _commit_clips_directory(self, directory) -> bool:
        """Prepare and persist a new root before changing the visible library."""
        if self.sm is None:
            return False
        old_settings = getattr(self.sm, 'settings', None)
        missing = object()
        old_recording_value = self.sm.get('recording_directory', missing)
        if old_recording_value is missing or old_recording_value is None:
            recording_is_derived = True
            preserved_recording_value = None
        else:
            recording_is_derived = False
            preserved_recording_value = old_recording_value
        try:
            try:
                old_root = clips_directory_from(self.sm)
            except StoragePathError:
                old_root = None
            if not recording_is_derived:
                try:
                    old_recording = recording_directory_from(self.sm)
                except StoragePathError:
                    old_recording = None
                if (old_root is not None and old_recording is not None
                        and old_recording == old_root / 'Recordings'):
                    recording_is_derived = True
            new_root = prepare_clips_storage(directory, probe_writable=True)
            new_recording = (new_root / 'Recordings' if recording_is_derived
                             else preserved_recording_value)

            previous = {}
            if isinstance(old_settings, dict):
                for key in ('clips_directory', 'recording_directory'):
                    previous[key] = (key in old_settings, old_settings.get(key))

            self.sm.set('clips_directory', str(new_root))
            recording_to_store = (str(new_recording) if recording_is_derived
                                  else preserved_recording_value)
            self.sm.set('recording_directory', recording_to_store)
            if not self.sm.save_settings():
                if isinstance(old_settings, dict):
                    for key, (was_present, old_value) in previous.items():
                        if was_present:
                            old_settings[key] = old_value
                        else:
                            old_settings.pop(key, None)
                error = StoragePathError(
                    'FTHR could not save the new storage location. Your current '
                    'library remains selected; choose a writable settings folder '
                    'and try again. No files were moved.')
                self._report_storage_change_failure(error)
                return False
        except (OSError, TypeError, ValueError) as error:
            self._report_storage_change_failure(
                error if isinstance(error, StoragePathError) else StoragePathError(
                    'FTHR could not use the selected storage folder. Choose a '
                    'valid writable folder in Settings. No files were moved.'))
            return False

        resolved = str(new_root)
        self.clips_directory_edit.setText(resolved)
        recording_edit = self.__dict__.get('recording_directory_edit', None)
        if recording_is_derived and recording_edit is not None:
            recording_edit.setText(str(new_recording))
        self.clips_directory_changed.emit(resolved)
        return True

    def _on_recording_folder_browse(self):
        if self.sm is None:
            return
        current_error = None
        try:
            current = str(recording_directory_from(self.sm))
        except StoragePathError as error:
            current_error = error
            current = str(Path.home())
            self._report_storage_change_failure(error)
        folder = QFileDialog.getExistingDirectory(
            self, 'Select Manual Recording Folder', current,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            if current_error is not None:
                print(f'[Storage] Recording folder selection cancelled: {current_error}')
            return
        try:
            resolved = str(ensure_storage_directory(folder, probe_writable=True))
        except StoragePathError as error:
            self._report_storage_change_failure(error)
            return
        try:
            owned_root = clips_directory_from(self.sm)
        except StoragePathError:
            # The recording folder is still repairable even when the library
            # root is unavailable; treat this choice as a separate import root.
            owned_root = None
        old_settings = getattr(self.sm, 'settings', None)
        previous = {}
        if isinstance(old_settings, dict):
            for key in ('recording_directory', 'imported_clip_folders'):
                previous[key] = (key in old_settings, old_settings.get(key))
        self.sm.set('recording_directory', resolved)
        external_roots = None
        try:
            if owned_root is None:
                raise ValueError('clips storage root is unavailable')
            Path(resolved).relative_to(owned_root)
        except ValueError:
            # External recording directories are linked into the library so
            # recordings remain editable after restarting FTHR.
            roots = add_import_root(
                self.sm.get('imported_clip_folders', []), resolved)
            external_roots = roots
            self.sm.set('imported_clip_folders', roots)
        if not self.sm.save_settings():
            if isinstance(old_settings, dict):
                for key, (was_present, old_value) in previous.items():
                    if was_present:
                        old_settings[key] = old_value
                    else:
                        old_settings.pop(key, None)
            self._report_storage_change_failure(StoragePathError(
                'FTHR could not save the manual recording folder. Your current '
                'folder remains selected. No files were moved.'))
            return
        if external_roots is not None:
            self._refresh_import_folders_list()
            self.imported_folders_changed.emit()
        self.recording_directory_edit.setText(resolved)

    def _on_import_folder_scan(self):
        _known = [
            ('Medal.tv',             Path.home() / 'Videos' / 'Medal'),
            ('Xbox Game Bar',        Path.home() / 'Videos' / 'Captures'),
            ('Outplayed',            Path.home() / 'Videos' / 'Outplayed'),
            ('Plays.tv',             Path.home() / 'Videos' / 'Plays.tv'),
            ('AMD ReLive',           Path.home() / 'Videos' / 'AMD' / 'ReLive'),
            ('Nvidia ShadowPlay',    Path.home() / 'Videos' / 'Shadowplay Clips'),
            ('GeForce Experience',   Path.home() / 'Videos' / 'NVIDIA'),
            ('Nvidia Highlights',    Path.home() / 'Videos' / 'Nvidia Highlights'),
        ]
        existing = {
            os.path.normcase(os.path.realpath(path))
            for path in (
                self.sm.get('imported_clip_folders', []) if self.sm else [])
        }
        found = [
            (name, str(path))
            for name, path in _known
            if (path.exists()
                and os.path.normcase(os.path.realpath(path)) not in existing)
        ]

        # Clear scan results area for fresh output
        while self._scan_results_layout.count():
            item = self._scan_results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not found:
            lbl = QLabel('No clip folders from other software were found on your system.')
            lbl.setWordWrap(True)
            set_theme_style(lbl, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY)))
            self._scan_results_layout.addWidget(lbl)
            self._scan_results_container.setVisible(True)
            self._scan_pending = []
            return

        self._scan_pending = found
        self._rebuild_scan_results()

    def _rebuild_scan_results(self):
        while self._scan_results_layout.count():
            item = self._scan_results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._scan_pending:
            self._scan_results_container.setVisible(False)
            return

        count = len(self._scan_pending)
        header = _flat_section_header(
            f'Found {count} folder{"s" if count != 1 else ""}'
        )
        self._scan_results_layout.addWidget(header)
        self._scan_results_layout.addSpacing(8)

        for name, path in self._scan_pending:
            self._scan_results_layout.addWidget(self._make_scan_result_row(name, path))

        self._scan_results_container.setVisible(True)

    def _make_scan_result_row(self, software_name: str, path: str) -> QFrame:
        row = QFrame()
        set_theme_style(row, lambda: (f'QFrame {{ background: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER}; }}'))
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 8, 8, 8)
        rl.setSpacing(Sizes.ROW_GAP)

        info = QVBoxLayout()
        info.setSpacing(1)
        name_lbl = QLabel(software_name)
        set_theme_style(name_lbl, lambda: (f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' background: transparent;'))
        path_lbl = QLabel(path)
        set_theme_style(path_lbl, lambda: (label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL)))
        info.addWidget(name_lbl)
        info.addWidget(path_lbl)
        rl.addLayout(info, 1)

        add_btn = QPushButton('Add')
        add_btn.setFixedWidth(60)
        add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(add_btn, button_primary_qss)
        add_btn.clicked.connect(lambda _, n=software_name, p=path: self._scan_add(n, p))
        rl.addWidget(add_btn)

        dismiss_btn = QPushButton('Dismiss')
        dismiss_btn.setFixedWidth(76)
        dismiss_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        set_theme_style(dismiss_btn, button_outline_qss)
        dismiss_btn.clicked.connect(lambda _, n=software_name, p=path: self._scan_dismiss(n, p))
        rl.addWidget(dismiss_btn)
        return row

    def _scan_add(self, software_name: str, path: str):
        if self.sm is not None:
            folders = add_import_root(
                self.sm.get('imported_clip_folders', []), path)
            self.sm.set('imported_clip_folders', folders)
            self.sm.save_settings()
            self._refresh_import_folders_list()
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != path]
        self._rebuild_scan_results()
        self.imported_folders_changed.emit()

    def _scan_dismiss(self, software_name: str, path: str):
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != path]
        self._rebuild_scan_results()

    def _storage_path_for_display(self, setting_key, resolver) -> str:
        """Display the resolved path, or the saved invalid value for repair."""
        try:
            return str(resolver(self.sm))
        except StoragePathError as error:
            emit_event('storage', 'settings_path_invalid', state='INVALID',
                       setting=setting_key, detail=str(error))
            raw_value = self.sm.get(setting_key, '')
            return '' if raw_value is None else str(raw_value)

    def _make_clip_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(0)

        layout.addWidget(_flat_section_header('Clip Settings'))
        layout.addSpacing(12)

        def _folder_row(label_text, current, browse_text, slot,
                        default_slot=None):
            row_widget = QWidget()
            row_widget.setStyleSheet('background: transparent;')
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            label = QLabel(label_text)
            label.setFixedWidth(180)
            set_theme_style(label,
                lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
            row.addWidget(label)
            edit = QLineEdit(str(current))
            edit.setReadOnly(True)
            set_theme_style(edit, lambda: (f'QLineEdit {{ background:{Colors.SURFACE_1}; '
                f'color:{Colors.TEXT_DIM}; border:1px solid {Colors.BORDER}; '
                f'padding:7px 9px; }}'))
            row.addWidget(edit, 1)
            browse = QPushButton('BROWSE')
            set_theme_style(browse, button_outline_qss)
            browse.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            browse.setToolTip(browse_text)
            browse.clicked.connect(slot)
            row.addWidget(browse)
            if default_slot is not None:
                use_default = QPushButton('USE DEFAULT')
                set_theme_style(use_default, button_outline_qss)
                use_default.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                use_default.setToolTip('Use the platform default clips folder.')
                use_default.clicked.connect(default_slot)
                row.addWidget(use_default)
            return row_widget, edit

        # Folders --
        layout.addWidget(_flat_section_header('Folders'))
        layout.addSpacing(12)
        storage_row, self.clips_directory_edit = _folder_row(
            'CLIPS STORAGE FOLDER', self._storage_path_for_display(
                'clips_directory', clips_directory_from),
            'Choose where FTHR stores clips, screenshots, exports, and shares.',
            self._on_clips_folder_browse,
            self._on_clips_folder_use_default)
        layout.addWidget(storage_row)
        layout.addSpacing(8)
        recording_row, self.recording_directory_edit = _folder_row(
            'MANUAL RECORDING FOLDER', self._storage_path_for_display(
                'recording_directory', recording_directory_from),
            'Choose where manual recordings are written.',
            self._on_recording_folder_browse)
        layout.addWidget(recording_row)

        # Import Clips --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Import Clips'))
        layout.addSpacing(10)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_btn = QPushButton('Add folder')
        set_theme_style(add_btn, button_outline_qss)
        add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        add_btn.clicked.connect(self._on_import_folder_add)
        btn_row.addWidget(add_btn)

        self._scan_btn = QPushButton('Scan for clips')
        set_theme_style(self._scan_btn, button_secondary_qss)
        self._scan_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._scan_btn.clicked.connect(self._on_import_folder_scan)
        btn_row.addWidget(self._scan_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addSpacing(12)

        self._import_folders_container = QWidget()
        self._import_folders_container.setStyleSheet('background: transparent;')
        self._import_folders_layout = QVBoxLayout(self._import_folders_container)
        self._import_folders_layout.setContentsMargins(0, 0, 0, 0)
        self._import_folders_layout.setSpacing(4)
        layout.addWidget(self._import_folders_container)

        layout.addSpacing(4)
        self._scan_results_container = QWidget()
        self._scan_results_container.setVisible(False)
        self._scan_results_container.setStyleSheet('background: transparent;')
        self._scan_results_layout = QVBoxLayout(self._scan_results_container)
        self._scan_results_layout.setContentsMargins(0, 0, 0, 0)
        self._scan_results_layout.setSpacing(4)
        layout.addWidget(self._scan_results_container)

        # Video Encoding --
        layout.addSpacing(24)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(18)
        layout.addWidget(_flat_section_header('Video Encoding'))
        layout.addSpacing(12)

        def _encoding_row(label_text, widget):
            row_widget = QWidget()
            row_widget.setStyleSheet('background: transparent;')
            row = QHBoxLayout(row_widget)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(Sizes.ROW_GAP)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(140)
            set_theme_style(lbl,
                lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            return row_widget

        self.encoder_combo = _DropdownCombo()
        self.encoder_combo.addItem('Detecting available encoders…', 'auto')
        self.encoder_combo.setEnabled(False)
        set_theme_style(self.encoder_combo, combo_qss)
        layout.addWidget(_encoding_row('Encoder', self.encoder_combo))
        layout.addSpacing(8)

        self.codec_combo = _DropdownCombo()
        self.codec_combo.addItem('Automatic', 'auto')
        self.codec_combo.setEnabled(False)
        set_theme_style(self.codec_combo, combo_qss)
        layout.addWidget(_encoding_row('Codec', self.codec_combo))
        layout.addSpacing(8)

        self.encoder_preset_combo = _DropdownCombo()
        for value, label in _NVENC_PRESET_OPTIONS:
            self.encoder_preset_combo.addItem(label, value)
        set_theme_style(self.encoder_preset_combo, combo_qss)
        saved_preset = int(self.sm.get('encoder_preset', 4))
        self.encoder_preset_combo.setCurrentIndex(
            max(0, min(6, saved_preset - 1)))
        self.encoder_preset_row = _encoding_row(
            'NVIDIA preset', self.encoder_preset_combo)
        self.encoder_preset_row.setVisible(False)
        layout.addWidget(self.encoder_preset_row)
        layout.addSpacing(8)

        self.active_encoder_lbl = QLabel('—')
        set_theme_style(self.active_encoder_lbl,
            lambda: (label_body(Colors.ACCENT, Fonts.SIZE_BODY)))
        layout.addWidget(_encoding_row('Active encoder', self.active_encoder_lbl))
        layout.addSpacing(10)

        encoder_actions = QHBoxLayout()
        encoder_actions.setSpacing(Sizes.ROW_GAP)
        self.encoder_apply_btn = QPushButton('Apply')
        set_theme_style(self.encoder_apply_btn, button_primary_qss)
        self.encoder_apply_btn.setVisible(False)
        self.encoder_apply_btn.clicked.connect(self._on_encoder_apply)
        encoder_actions.addWidget(self.encoder_apply_btn)
        self.encoder_refresh_btn = QPushButton('Redetect')
        set_theme_style(self.encoder_refresh_btn, button_outline_qss)
        self.encoder_refresh_btn.setToolTip(
            'Recheck the GPU drivers after a hardware or driver change.')
        self.encoder_refresh_btn.clicked.connect(
            lambda: self._start_encoder_probe(force=True))
        encoder_actions.addWidget(self.encoder_refresh_btn)
        self.encoder_detection_lbl = QLabel('Checking this device…')
        self.encoder_detection_lbl.setWordWrap(True)
        set_theme_style(self.encoder_detection_lbl,
            lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        encoder_actions.addWidget(self.encoder_detection_lbl, 1)
        layout.addLayout(encoder_actions)

        self.encoder_combo.currentIndexChanged.connect(
            self._on_encoder_setting_changed)
        self.codec_combo.currentIndexChanged.connect(
            self._on_codec_setting_changed)
        self.encoder_preset_combo.currentIndexChanged.connect(
            self._on_encoder_setting_changed)
        # Watermark --
        layout.addSpacing(24)
        layout.addWidget(_flat_section_header('Watermark'))
        layout.addSpacing(12)

        self.watermark_check = QCheckBox(
            'Add animated Capture Card to exports and shares')
        set_theme_style(self.watermark_check, checkbox_qss)
        self.watermark_check.setChecked(self.sm.get('watermark_enabled', False))
        self.watermark_check.toggled.connect(self._on_watermark_toggled)
        layout.addWidget(self.watermark_check)
        layout.addSpacing(10)

        layout.addSpacing(18)
        layout.addWidget(_flat_section_header('Clip Editor'))
        layout.addSpacing(12)
        for key, text, tip in (
                ('timeline_preview_strip_enabled', 'Seek bar preview',
                 'Off: the app will no longer generate preview images in the timeline.'),
                ('hide_deleted_timeline_segments', 'Hide deleted segments in the editor seek bar',
                 'Collapse deleted sections out of the timeline. Turn off to view and restore them.'),
                ('grid_show_edited_duration', 'Show clip length excluding deleted segments in the grid',
                 'Use saved trim boundaries and kept segments for the duration badge.')):
            check = QCheckBox(text)
            check.setToolTip(tip)
            set_theme_style(check, checkbox_qss)
            check.setChecked(bool(self.sm.get(key, key == 'timeline_preview_strip_enabled')))
            check.toggled.connect(lambda enabled, setting=key: self._save_clip_display_setting(setting, enabled))
            setattr(self, key + '_check', check)
            layout.addWidget(check)
            layout.addSpacing(10)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_theme_style(scroll, scrollbar_qss)
        scroll.setWidget(page)

        wrapper = QWidget()
        wrapper.setStyleSheet('background: transparent;')
        wrapper_layout = QVBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)
        wrapper_layout.addWidget(scroll)

        self._scan_pending: list[tuple[str, str]] = []
        self._refresh_import_folders_list()
        return wrapper

    def _save_clip_display_setting(self, key, enabled):
        self.sm.set(key, bool(enabled))
        self.sm.save_settings()
        if key == 'timeline_preview_strip_enabled':
            from ui.clip_viewer import ClipViewer, _TIMELINE_IMAGE_CACHE, _TIMELINE_CACHE_LOCK
            for viewer in QApplication.instance().allWidgets():
                if isinstance(viewer, ClipViewer) and viewer.sm is self.sm:
                    viewer.set_timeline_preview_enabled(enabled)
            if not enabled:
                with _TIMELINE_CACHE_LOCK:
                    _TIMELINE_IMAGE_CACHE.clear()
        self._sync_ui_performance_controls()

    def _make_audio_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)

        # Left: Microphone --
        left = QWidget()
        left.setStyleSheet('background: transparent;')
        left.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, Sizes.SECTION_GAP, 0)
        left_layout.setSpacing(0)
        left_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        left_layout.addWidget(_flat_section_header('Microphone'))
        left_layout.addSpacing(12)

        # Device row
        dev_row = QHBoxLayout()
        dev_row.setSpacing(8)
        dev_lbl = QLabel('Input device')
        dev_lbl.setFixedWidth(100)
        dev_row.addWidget(dev_lbl)
        self.mic_combo = _DropdownCombo()
        self._mic_combo_connected = False
        dev_row.addWidget(self.mic_combo, 1)
        refresh = QPushButton()
        refresh.setObjectName('micRefreshBtn')
        refresh.setFixedSize(26, 26)
        refresh.setToolTip('Re-scan input devices')
        _ref_ico2 = _load_icon('refresh.png', 13)
        if not _ref_ico2.isNull():
            refresh.setIcon(_ref_ico2)
            refresh.setIconSize(QSize(13, 13))
            _register_icon_widget(refresh, 'refresh.png', 13)
        else:
            refresh.setText('Refresh')
        refresh.clicked.connect(self._populate_mic_devices)
        dev_row.addWidget(refresh)
        left_layout.addLayout(dev_row)
        left_layout.addSpacing(8)

        self.mic_mono_check = QCheckBox('Downmix stereo microphone to mono')
        set_theme_style(self.mic_mono_check, checkbox_qss)
        self.mic_mono_check.setChecked(bool(self.sm.get('mic_downmix_mono', False)))
        self.mic_mono_check.setToolTip('Center your microphone in both speakers when recording.')
        self.mic_mono_check.toggled.connect(self._on_mic_mono_changed)
        left_layout.addWidget(self.mic_mono_check)
        left_layout.addSpacing(12)

        # Volume row
        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        self.mic_gain_label = QLabel('Microphone gain')
        self.mic_gain_label.setFixedWidth(100)
        vol_row.addWidget(self.mic_gain_label)
        self.mic_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_vol_slider.setRange(0, 200)
        self.mic_vol_slider.setValue(100)
        self.mic_vol_slider.setToolTip(
            'Microphone gain: 100% is unity (no digital boost). Values above '
            '100% apply a digital boost to newly captured microphone audio and '
            'can cause over-range or distortion on loud input. Windows audio '
            'enhancements are independent of this control. On Windows, restart '
            'capture for a changed engine gain to take effect.')
        vol_row.addWidget(self.mic_vol_slider, 1)
        self.mic_vol_value = QLabel('100%')
        self.mic_vol_value.setFixedWidth(42)
        self.mic_vol_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.mic_vol_slider.valueChanged.connect(self._on_mic_volume_changed)
        vol_row.addWidget(self.mic_vol_value)
        left_layout.addLayout(vol_row)
        self.mic_gain_warning = QLabel(
            '⚠ Above 100% applies a digital boost and may distort loud input. '
            'Restart capture for Windows engine gain changes to take effect.')
        self.mic_gain_warning.setObjectName('micGainWarning')
        self.mic_gain_warning.setWordWrap(True)
        set_theme_style(self.mic_gain_warning,
                        lambda: (label_body(Colors.WARNING, Fonts.SIZE_MICRO)))
        left_layout.addWidget(self.mic_gain_warning)
        self._update_mic_gain_warning(self.mic_vol_slider.value())
        left_layout.addSpacing(8)

        # Live level meter row
        meter_row = QHBoxLayout()
        meter_row.setSpacing(8)
        meter_lbl = QLabel('Live level')
        meter_lbl.setFixedWidth(100)
        meter_row.addWidget(meter_lbl)
        self.mic_level_meter = _MicLevelMeter()
        self.mic_level_meter.gary_thresholds_changed.connect(
            self._on_gary_thresholds_dragged)
        meter_row.addWidget(self.mic_level_meter, 1)
        left_layout.addLayout(meter_row)
        left_layout.addSpacing(10)

        # Loopback checkbox
        self.mic_loopback_check = QCheckBox(
            'Monitor (route mic to speakers so I can hear it)')
        self.mic_loopback_check.setToolTip(
            'Plays your microphone back through your speakers in real time so '
            'you can judge volume. Stops when you leave this page.')
        self.mic_loopback_check.toggled.connect(self._on_loopback_toggled)
        left_layout.addWidget(self.mic_loopback_check)

        left_layout.addSpacing(18)
        left_layout.addWidget(_flat_section_header('Gary Mode'))
        left_layout.addSpacing(8)

        gary_panel = QFrame()
        gary_panel.setObjectName('garyPanel')
        gary_layout = QHBoxLayout(gary_panel)
        gary_layout.setContentsMargins(10, 4, 10, 4)
        gary_layout.setSpacing(12)

        self.gary_preview = QLabel()
        self.gary_preview.setObjectName('garyPreview')
        self.gary_preview.setFixedSize(76, 58)
        self.gary_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gary_layout.addWidget(self.gary_preview)

        self.gary_enabled_check = QCheckBox('Enable Gary Mode')
        self.gary_enabled_check.setToolTip(
            'Show the selected image when the microphone crosses the start handle.')
        self.gary_enabled_check.toggled.connect(self._on_gary_enabled_changed)
        gary_layout.addWidget(
            self.gary_enabled_check, 0, Qt.AlignmentFlag.AlignVCenter)
        gary_layout.addStretch()

        choose_image = QPushButton('Choose')
        choose_image.setObjectName('garyImageButton')
        choose_image.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        choose_image.clicked.connect(self._choose_gary_image)
        gary_layout.addWidget(
            choose_image, 0, Qt.AlignmentFlag.AlignVCenter)
        reset_image = QPushButton('Default')
        reset_image.setObjectName('garyImageButton')
        reset_image.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        reset_image.clicked.connect(self._reset_gary_image)
        gary_layout.addWidget(
            reset_image, 0, Qt.AlignmentFlag.AlignVCenter)
        left_layout.addWidget(gary_panel)

        if not _SD_AVAILABLE:
            warn = QLabel(
                'Mic features need the "sounddevice" Python package.\n'
                '   Install it from your venv:  pip install sounddevice numpy')
            set_theme_style(warn, lambda: (label_body(Colors.ERROR, Fonts.SIZE_BODY)))
            left_layout.addSpacing(6)
            left_layout.addWidget(warn)

        cols.addWidget(left, 1)

        cols.addWidget(_settings_vsep())

        # Right: Notification placement and playback levels. Custom sound
        # files remain under Customize, but the everyday loudness controls
        # belong here beside the rest of the audio settings.
        right = QWidget()
        right.setStyleSheet('background: transparent;')
        right.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(Sizes.SECTION_GAP, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.sound_sliders = {}
        self.sound_value_labels = {}
        right_layout.addWidget(_flat_section_header('Notifications'))
        right_layout.addSpacing(12)

        self.notification_sounds_check = QCheckBox('Enable notification sounds')
        set_theme_style(self.notification_sounds_check, checkbox_qss)
        self.notification_sounds_check.setToolTip(
            'Toggle every FTHR notification sound on or off without changing '
            'the individual volume levels.')
        self.notification_sounds_check.toggled.connect(
            self._on_notification_sounds_toggled)
        right_layout.addWidget(self.notification_sounds_check)
        right_layout.addSpacing(12)

        mon_row = QHBoxLayout()
        mon_row.setSpacing(8)
        mon_lbl = QLabel('Monitor:')
        mon_lbl.setFixedWidth(150)
        mon_row.addWidget(mon_lbl)
        self.notif_monitor_combo = _DropdownCombo()
        self.notif_monitor_combo.addItem('Auto (highest Hz)', userData='auto')
        for s in QApplication.screens():
            g = s.availableGeometry()
            self.notif_monitor_combo.addItem(
                f'{s.name()}  ({g.width()}×{g.height()} @ {int(s.refreshRate())}Hz)',
                userData=s.name(),
            )
        self.notif_monitor_combo.currentIndexChanged.connect(self._on_notif_monitor_changed)
        mon_row.addWidget(self.notif_monitor_combo, 1)
        right_layout.addLayout(mon_row)

        right_layout.addSpacing(18)
        volume_heading = QLabel('Notification volume')
        set_theme_style(volume_heading,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        right_layout.addWidget(volume_heading)
        right_layout.addSpacing(8)

        notification_sounds = (
            ('clip', 'Clip captured'),
            ('screenshot', 'Screenshot saved'),
            ('error', 'Capture failed'),
            ('startup', 'Startup'),
            ('upload_successful', 'Upload successful'),
            ('upload_failed', 'Upload failed'),
        )
        notification_sounds = (('master', 'Master volume'),) + notification_sounds
        for key, label_text in notification_sounds:
            sound_row = QHBoxLayout()
            sound_row.setSpacing(8)
            sound_label = QLabel(label_text)
            sound_label.setFixedWidth(150)
            sound_row.addWidget(sound_label)

            slider = _SettingsSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setSingleStep(1)
            slider.setPageStep(10)
            set_theme_style(slider, slider_qss)
            slider.setAccessibleName(f'{label_text} notification volume')
            sound_row.addWidget(slider, 1)

            value_label = QLabel('100%')
            value_label.setFixedWidth(42)
            value_label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            sound_row.addWidget(value_label)

            self.sound_sliders[key] = slider
            self.sound_value_labels[key] = value_label
            slider.valueChanged.connect(
                lambda value, sound_key=key:
                self._on_sound_volume_changed(sound_key, value))
            right_layout.addLayout(sound_row)
            right_layout.addSpacing(7)

        cols.addWidget(right, 1)

        outer.addLayout(cols)

        # Show a placeholder and defer device inventory until after widget
        # construction; driver queries can block for hundreds of milliseconds.
        self.mic_combo.addItem('System Default', userData=None)
        QTimer.singleShot(0, self._populate_mic_devices)

        # Audio Capture --
        outer.addSpacing(24)
        outer.addWidget(_settings_hsep())
        outer.addSpacing(20)
        outer.addWidget(_flat_section_header('Audio Capture'))
        outer.addSpacing(12)

        self.audio_capture_check = QCheckBox('Enable audio capture')
        set_theme_style(self.audio_capture_check, checkbox_qss)
        self.audio_capture_check.setChecked(self.sm.get('audio_capture_enabled', True))
        self.audio_capture_check.toggled.connect(self._on_audio_capture_toggled)
        outer.addWidget(self.audio_capture_check)
        outer.addSpacing(12)

        playback_row = QHBoxLayout()
        playback_row.setContentsMargins(0, 0, 0, 0)
        playback_row.setSpacing(Sizes.ROW_GAP)
        playback_label = QLabel('Playback device to capture')
        playback_label.setObjectName('playbackDeviceLabel')
        playback_row.addWidget(playback_label)
        self.playback_combo = QComboBox()
        self.playback_combo.setMinimumWidth(0)
        self.playback_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        set_theme_style(self.playback_combo, combo_qss)
        self.playback_combo.addItem(_playback_device_labels()[0], '')
        self.playback_combo.currentIndexChanged.connect(self._on_playback_device_changed)
        playback_row.addWidget(self.playback_combo, 1)
        refresh_playback = QPushButton('Refresh')
        refresh_playback.setObjectName('playbackDeviceRefresh')
        set_theme_style(refresh_playback, button_outline_qss)
        refresh_playback.clicked.connect(self._populate_playback_devices)
        playback_row.addWidget(refresh_playback)
        outer.addLayout(playback_row)
        outer.addSpacing(Sizes.SPACE_3)
        hint = QLabel(_playback_device_labels()[2])
        hint.setWordWrap(True)
        outer.addWidget(hint)
        outer.addSpacing(12)
        self._playback_inventory_ready.connect(self._apply_playback_devices)
        QTimer.singleShot(0, self._populate_playback_devices)

        self.separate_audio_check = QCheckBox(
            'Keep system and microphone audio as separate tracks')
        self.separate_audio_check.setObjectName('separateAudioCheck')
        set_theme_style(self.separate_audio_check, checkbox_qss)
        self.separate_audio_check.setChecked(
            normalize_audio_capture_mode(self.sm.get(
                'audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED))
            == AUDIO_CAPTURE_MODE_SEPARATED)
        self.separate_audio_check.setToolTip(
            'Off (default): system and microphone audio are combined into one '
            'MP4 track, so the clip editor exposes only MASTER. On: keep the '
            'sources as separate audio tracks with individual editor controls.')
        self.separate_audio_check.toggled.connect(
            self._on_separate_audio_toggled)
        outer.addWidget(self.separate_audio_check)
        # Keep a descriptive alias for integrations that refer to the control
        # by its behavior rather than its visual label.
        self.audio_separation_check = self.separate_audio_check
        outer.addSpacing(4)
        mode_hint = QLabel(
            'Combined audio is the default. Enable separate tracks only when '
            'you need independent system and microphone mixing in the editor.')
        mode_hint.setWordWrap(True)
        set_theme_style(mode_hint, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        outer.addWidget(mode_hint)

        return page

    def _on_game_detection_toggled(self, checked: bool):
        self.sm.set('game_detection_enabled', checked)
        self.sm.save_settings()
        main_win = self.window()
        if hasattr(main_win, 'game_detection_popup'):
            main_win.game_detection_popup.set_enabled_state(checked)
        if hasattr(main_win, '_on_game_detection_configuration_changed'):
            main_win._on_game_detection_configuration_changed(
                checked,
                str(self.sm.get('game_detection_mode', 'auto')).lower(),
            )

    def _on_audio_capture_toggled(self, checked: bool):
        checked = bool(checked)
        self.sm.set('audio_capture_enabled', checked)
        self.sm.save_settings()
        self._sync_audio_controls()
        self.audio_capture_changed.emit(checked)

    def _on_separate_audio_toggled(self, checked: bool):
        mode = (AUDIO_CAPTURE_MODE_SEPARATED if checked
                else AUDIO_CAPTURE_MODE_COMBINED)
        self.sm.set('audio_capture_mode', mode)
        self.sm.save_settings()
        self.audio_capture_mode_changed.emit(mode)

    def _sync_audio_controls(self):
        """Keep Audio and Performance views backed by one audio setting."""
        audio_enabled = bool(self.sm.get('audio_capture_enabled', True))
        for name in ('audio_capture_check', 'performance_audio_check'):
            control = self.__dict__.get(name)
            if control is not None:
                control.blockSignals(True)
                control.setChecked(audio_enabled)
                control.blockSignals(False)
        separate = self.__dict__.get('separate_audio_check', None)
        if separate is not None:
            separate.setEnabled(audio_enabled)

    def _on_watermark_toggled(self, checked: bool):
        self.sm.set('watermark_enabled', checked)
        self.sm.save_settings()

    def _on_anticheat_toggled(self, checked: bool):
        self.sm.set('anticheat_detection_enabled', checked)
        self.sm.save_settings()
        main_win = self.window()
        if not hasattr(main_win, '_focus_monitor'):
            return
        if checked and self.sm.get('capture_mode', 'desktop') == 'window':
            target = self.sm.get('target_window_name', '')
            main_win._focus_monitor.set_target(target)
            main_win._focus_monitor.start()
        else:
            main_win._focus_monitor.stop()
            if hasattr(main_win, 'bridge') and main_win.bridge.is_connected():
                main_win.bridge.resume_recording()

    def _populate_camera_devices(self):
        # Device discovery can launch FFmpeg and open up to eight cameras.
        # Keep that work outside page construction and the Qt event loop.
        worker = self.__dict__.get('_camera_device_scan_thread')
        if worker is not None and worker.is_alive():
            return
        combo = self.camera_device_combo
        blocked = combo.blockSignals(True)
        combo.clear()
        combo.addItem('Scanning cameras…')
        combo.setEnabled(False)
        combo.blockSignals(blocked)

        def discover():
            try:
                from core.camera_recorder import CameraRecorder
                available = CameraRecorder.is_available()
                devices = CameraRecorder.list_devices() if available else []
                error = '' if available else 'Camera capture unavailable'
            except Exception:
                devices, error = [], 'Camera discovery failed; try Refresh'
            try:
                self._camera_devices_result.emit(devices, error)
            except RuntimeError:
                # The page may have been destroyed while a driver was scanning.
                pass

        self._camera_device_scan_thread = threading.Thread(
            target=discover, daemon=True, name='fthr-camera-device-scan')
        self._camera_device_scan_thread.start()

    @Slot(object, str)
    def _on_camera_devices_result(self, devices, error):
        combo = self.camera_device_combo
        blocked = combo.blockSignals(True)
        try:
            self._apply_camera_devices(devices, error)
        finally:
            combo.blockSignals(blocked)

    def _apply_camera_devices(self, devices, error):
        self.camera_device_combo.clear()
        if not devices:
            self.camera_device_combo.addItem(error or 'No cameras detected')
            self.camera_device_combo.setEnabled(False)
            return
        self.camera_device_combo.setEnabled(True)
        for device in devices:
            self.camera_device_combo.addItem(
                str(device['name']), int(device['index']))
        saved = int(self.sm.get('camera_device_index', 0) or 0)
        saved_index = self.camera_device_combo.findData(saved)
        self.camera_device_combo.setCurrentIndex(
            saved_index if saved_index >= 0 else 0)

    def _on_camera_toggled(self, checked: bool):
        from core.camera_recorder import CameraRecorder

        if checked:
            self.sm.set('camera_enabled', True)
            self.sm.save_settings()
            idx = int(self.sm.get('camera_device_index', 0) or 0)
            self._set_camera_overlay_enabled(False)
            self._start_camera_open(idx)
            return

        self._camera_start_generation += 1
        self._camera_pending_device_index = None
        self.sm.set('camera_enabled', False)
        self.sm.save_settings()
        CameraRecorder().stop()
        self._camera_preview_timer.stop()
        self._set_camera_overlay_enabled(False)
        self._clear_camera_preview()

    def _on_camera_device_changed(self, combo_index: int):
        if combo_index < 0:
            return
        device_index = self.camera_device_combo.itemData(combo_index)
        if device_index is None:
            return
        idx = int(device_index)
        self.sm.set('camera_device_index', int(device_index))
        self.sm.save_settings()
        if self.sm.get('camera_enabled', False):
            self._start_camera_open(idx)

    def _start_camera_open(self, device_index: int) -> None:
        """Open a camera asynchronously and marshal its result to this page."""
        from core.camera_recorder import CameraRecorder

        self._camera_start_generation += 1
        generation = self._camera_start_generation
        device_index = int(device_index)
        self._camera_pending_device_index = device_index
        self._camera_preview_timer.stop()
        self._clear_camera_preview()

        try:
            available = CameraRecorder.is_available()
        except Exception as exc:
            self._camera_start_result.emit(
                generation, device_index, False, str(exc)[:80])
            return
        if not available:
            self._camera_start_result.emit(
                generation, device_index, False,
                'Camera capture is unavailable in this installation.')
            return

        def on_result(ok: bool) -> None:
            # CameraRecorder invokes this on its worker thread. Emitting a Qt
            # signal is the only action here; all widget/settings work is
            # performed by _on_camera_start_result on the page's UI thread.
            self._camera_start_result.emit(
                generation, device_index, bool(ok), '')

        try:
            CameraRecorder().start_async(device_index, on_result=on_result)
        except Exception as exc:
            self._camera_start_result.emit(
                generation, device_index, False, str(exc)[:80])

    @Slot(int, int, bool, str)
    def _on_camera_start_result(self, generation: int, device_index: int,
                                ok: bool, diagnostic: str) -> None:
        current_device = int(self.sm.get('camera_device_index', 0) or 0)
        if (generation != self._camera_start_generation
                or device_index != self._camera_pending_device_index
                or device_index != current_device
                or not self.sm.get('camera_enabled', False)):
            return

        self._camera_pending_device_index = None
        if ok:
            self._set_camera_overlay_enabled(True)
            if not self._background_ui_paused:
                self._camera_preview_timer.start()
            return

        self.sm.set('camera_enabled', False)
        self.sm.save_settings()
        if 'camera_check' in self.__dict__:
            was_blocked = self.camera_check.blockSignals(True)
            self.camera_check.setChecked(False)
            self.camera_check.blockSignals(was_blocked)
        self._camera_preview_timer.stop()
        self._set_camera_overlay_enabled(False)
        self._clear_camera_preview()
        detail = (
            f'Could not open camera device {device_index}. Confirm it is '
            'connected and not in use by another app. The camera overlay '
            'was turned off; choose another camera or try again.'
        )
        diagnostic = ' '.join(str(diagnostic or '').split())[:80]
        if diagnostic:
            detail = f'{detail} ({diagnostic})'
        self.camera_unavailable.emit(detail[:240])

    def _set_camera_overlay_enabled(self, enabled: bool) -> None:
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_overlay_enabled('camera', enabled)
        elif hasattr(self, 'camera_overlay_editor'):
            self.camera_overlay_editor.set_enabled(enabled)

    def _clear_camera_preview(self) -> None:
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_camera_pixmap(QPixmap())
        elif hasattr(self, 'camera_overlay_editor'):
            self.camera_overlay_editor.set_frame(QPixmap())

    def _on_camera_overlay_rect_changed(self, rect: dict):
        self.sm.set('camera_overlay_rect', clamp_overlay_rect(rect))
        self.sm.save_settings()

    def _on_camera_pos_changed(self, idx: int):
        keys = ['bottom-right', 'bottom-left', 'top-right', 'top-left']
        self.sm.set('camera_position', keys[idx] if idx < len(keys) else 'bottom-right')
        self.sm.save_settings()

    def _on_camera_size_changed(self, idx: int):
        keys = ['small', 'medium', 'large']
        self.sm.set('camera_size', keys[idx] if idx < len(keys) else 'medium')
        self.sm.save_settings()

    def _update_camera_preview(self):
        sm = getattr(self, 'sm', None)
        if sm is not None and not sm.get('settings_visual_previews_enabled', True):
            timer = self.__dict__.get('_camera_preview_timer')
            if timer is not None:
                timer.stop()
            return
        preview = self.__dict__.get('unified_overlay_editor', None)
        if preview is None:
            preview = getattr(self, 'camera_overlay_editor', None)
        if preview is None or not preview.isVisible():
            return
        from core.camera_recorder import CameraRecorder
        frame = CameraRecorder().latest_frame
        if frame is None:
            return
        import cv2 as _cv2
        from PySide6.QtGui import QImage, QPixmap as _QPixmap
        source_h, source_w = frame.shape[:2]
        scale = min(1.0, 640 / max(1, source_w), 360 / max(1, source_h))
        if scale < 1.0:
            frame = _cv2.resize(
                frame,
                (max(1, int(source_w * scale)), max(1, int(source_h * scale))),
                interpolation=_cv2.INTER_AREA,
            )
        rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        # Use bytes() to copy the data — QImage constructed from a memoryview
        # holds a reference to the buffer, but rgb may be GC'd before Qt renders.
        qi = QImage(rgb.tobytes(), w, h, ch * w, QImage.Format.Format_RGB888)
        # UnifiedOverlayPreview scales directly in QPainter; retaining a
        # display-bounded pixmap avoids both a full-resolution GUI allocation
        # and another temporary scaled pixmap on every 100 ms preview tick.
        pix = _QPixmap.fromImage(qi)
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_camera_pixmap(pix)
        elif hasattr(self, 'camera_overlay_editor'):
            self.camera_overlay_editor.set_frame(pix)

    def _default_overlay_preview_background_path(self) -> Path:
        """Return the bundled desktop image used by the composite preview."""
        return Path(__file__).parent / 'assets' / 'preview_desktop.png'

    def _load_overlay_preview_background(self, preferred_path: str = '') -> QPixmap:
        """Load a chosen preview image, falling back to the bundled desktop."""
        candidates = []
        preferred = Path(str(preferred_path or '')).expanduser()
        if preferred.is_file():
            candidates.append(preferred)
        candidates.append(self._default_overlay_preview_background_path())
        for image_path in candidates:
            from ui.camera_overlay_editor import _cached_overlay_pixmap
            pixmap = _cached_overlay_pixmap(str(image_path), QSize(820, 500))
            if not pixmap.isNull():
                return pixmap
        return QPixmap()

    def _on_input_overlay_preview_background_changed(self, path: str) -> None:
        path = str(path or '')
        self.sm.set('input_overlay_preview_background', path)
        self.sm.save_settings()
        if 'input_overlay_preview_background_path' in self.__dict__:
            self.input_overlay_preview_background_path.setText(
                path or 'DEFAULT · DESKTOP SCREENSHOT')
        if 'input_overlay_preview_source' in self.__dict__:
            self.input_overlay_preview_source.setText(
                Path(path).name if path else 'DEFAULT · DESKTOP')
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_background_paths([
                path, str(self._default_overlay_preview_background_path())])

    def _choose_input_overlay_preview_background(self) -> bool:
        current = str(self.sm.get(
            'input_overlay_preview_background', '') or '')
        start = str(Path(current).parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, 'Choose preview background', start,
            'Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*)')
        if not path:
            return False
        if QPixmap(path).isNull():
            return False
        self._on_input_overlay_preview_background_changed(path)
        return True

    def _use_default_input_overlay_preview_background(self) -> None:
        self._on_input_overlay_preview_background_changed('')

    def _on_image_overlay_toggled(self, checked: bool):
        checked = bool(checked)
        layers = self._stored_image_layers()
        if checked and not layers:
            chosen = self._choose_image_overlay()
            if not chosen:
                self.image_overlay_check.blockSignals(True)
                self.image_overlay_check.setChecked(False)
                self.image_overlay_check.blockSignals(False)
                checked = False
        self.sm.set('image_overlay_enabled', checked)
        self.sm.save_settings()
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_overlay_enabled('image', checked)

    def _stored_image_layers(self) -> list[dict]:
        raw = self.sm.get('image_overlays', None)
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, dict)]
        return [dict(item) for item in image_overlay_layers(self.sm)]

    def _save_image_layers(self, layers: list[dict]) -> None:
        self.sm.set('image_overlays', layers)
        if layers:
            first = layers[0]
            self.sm.set('image_overlay_path', str(first.get('path', '')))
            self.sm.set('image_overlay_opacity', int(first.get('opacity', 100)))
            self.sm.set('image_overlay_fit', str(first.get('fit', 'fit')))
            self.sm.set('image_overlay_rect', clamp_overlay_rect(
                first.get('rect'), DEFAULT_IMAGE_OVERLAY_RECT))
        else:
            self.sm.set('image_overlay_path', '')
            self.sm.set('image_overlay_enabled', False)
        self.sm.save_settings()

    def _refresh_image_overlay_controls(self, selected: int | None = None) -> None:
        layers = self._stored_image_layers()
        if selected is None:
            selected = getattr(self, '_selected_image_index', 0)
        self._selected_image_index = (
            max(0, min(int(selected), len(layers) - 1)) if layers else -1)

        if 'image_overlay_selector' in self.__dict__:
            self.image_overlay_selector.blockSignals(True)
            self.image_overlay_selector.clear()
            for index, layer in enumerate(layers):
                name = Path(str(layer.get('path', ''))).name or f'Image {index + 1}'
                self.image_overlay_selector.addItem(
                    f'{index + 1:02d}  ·  {name}', index)
            if layers:
                self.image_overlay_selector.setCurrentIndex(
                    self._selected_image_index)
            else:
                self.image_overlay_selector.addItem('No image layers', -1)
            self.image_overlay_selector.setEnabled(bool(layers))
            self.image_overlay_selector.blockSignals(False)

        if 'remove_image_overlay_btn' in self.__dict__:
            self.remove_image_overlay_btn.setEnabled(bool(layers))
        if 'clear_image_overlays_btn' in self.__dict__:
            self.clear_image_overlays_btn.setEnabled(bool(layers))
        if 'image_overlay_count' in self.__dict__:
            count = len(layers)
            self.image_overlay_count.setText(
                f'{count} IMAGE LAYER{"S" if count != 1 else ""}')

        selected_layer = (
            layers[self._selected_image_index] if self._selected_image_index >= 0
            else None)
        if 'image_overlay_fit' in self.__dict__:
            self.image_overlay_fit.blockSignals(True)
            fit_index = self.image_overlay_fit.findData(
                selected_layer.get('fit', 'fit') if selected_layer else 'fit')
            self.image_overlay_fit.setCurrentIndex(max(0, fit_index))
            self.image_overlay_fit.setEnabled(selected_layer is not None)
            self.image_overlay_fit.blockSignals(False)
        if 'image_overlay_opacity' in self.__dict__:
            self.image_overlay_opacity.blockSignals(True)
            self.image_overlay_opacity.setValue(
                int(selected_layer.get('opacity', 100)) if selected_layer else 100)
            self.image_overlay_opacity.setEnabled(selected_layer is not None)
            self.image_overlay_opacity.blockSignals(False)
            self.image_overlay_opacity_value.setText(
                f'{self.image_overlay_opacity.value()}%')
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_image_layers(
                image_overlay_layers(self.sm))
            self.unified_overlay_editor.set_selected_image(
                self._selected_image_index)

    def _choose_image_overlay(self) -> bool:
        layers = self._stored_image_layers()
        current = str(layers[0].get('path', '') if layers else '')
        start = str(Path(current).parent) if current else str(Path.home())
        paths, _ = QFileDialog.getOpenFileNames(
            self, 'Add image overlays', start,
            'Images (*.png *.jpg *.jpeg *.webp *.bmp);;All files (*)')
        if not paths:
            return False
        valid_paths = [path for path in paths if not QPixmap(path).isNull()]
        if not valid_paths:
            return False
        for path in valid_paths:
            layers.append(new_image_overlay_layer(path, len(layers)))
        self._save_image_layers(layers)
        self.sm.set('image_overlay_enabled', True)
        self.sm.save_settings()
        self.image_overlay_check.blockSignals(True)
        self.image_overlay_check.setChecked(True)
        self.image_overlay_check.blockSignals(False)
        self._refresh_image_overlay_controls(len(layers) - 1)
        return True

    def _remove_selected_image_overlay(self):
        layers = self._stored_image_layers()
        index = getattr(self, '_selected_image_index', -1)
        if not (0 <= index < len(layers)):
            return
        layers.pop(index)
        self._save_image_layers(layers)
        self.image_overlay_check.blockSignals(True)
        self.image_overlay_check.setChecked(bool(layers))
        self.image_overlay_check.blockSignals(False)
        self._refresh_image_overlay_controls(min(index, len(layers) - 1))

    def _clear_image_overlay(self):
        self._save_image_layers([])
        self.image_overlay_check.blockSignals(True)
        self.image_overlay_check.setChecked(False)
        self.image_overlay_check.blockSignals(False)
        self._refresh_image_overlay_controls(-1)

    def _on_image_layer_selected(self, index: int):
        selected = self.image_overlay_selector.itemData(index)
        self._refresh_image_overlay_controls(
            int(selected) if selected is not None else -1)

    def _on_image_overlay_rect_changed(self, rect: dict):
        layers = self._stored_image_layers()
        if layers:
            layers[0]['rect'] = clamp_overlay_rect(
                rect, DEFAULT_IMAGE_OVERLAY_RECT)
            self._save_image_layers(layers)

    def _on_image_overlay_opacity_changed(self, value: int):
        layers = self._stored_image_layers()
        index = getattr(self, '_selected_image_index', -1)
        if 0 <= index < len(layers):
            layers[index]['opacity'] = int(value)
            self._save_image_layers(layers)
        self.image_overlay_opacity_value.setText(f'{int(value)}%')
        self._refresh_image_overlay_controls(index)

    def _on_image_overlay_fit_changed(self, index: int):
        value = self.image_overlay_fit.itemData(index) or 'fit'
        layers = self._stored_image_layers()
        selected = getattr(self, '_selected_image_index', -1)
        if 0 <= selected < len(layers):
            layers[selected]['fit'] = value
            self._save_image_layers(layers)
            self._refresh_image_overlay_controls(selected)

    def _on_input_overlay_rects_changed(self, rects: dict):
        rects = rects if isinstance(rects, dict) else {}
        if 'camera' in rects:
            self.sm.set('camera_overlay_rect', clamp_overlay_rect(
                rects.get('camera'), DEFAULT_OVERLAY_RECT))
        layers = self._stored_image_layers()
        for index, layer in enumerate(layers):
            key = f'image:{index}'
            if key in rects:
                layer['rect'] = clamp_overlay_rect(
                    rects[key], DEFAULT_IMAGE_OVERLAY_RECT)
        self._save_image_layers(layers)
        if 'keyboard' in rects:
            keyboard = third_party_keyboard_settings(self.sm)
            keyboard['rect'] = clamp_overlay_rect(
                rects.get('keyboard'), DEFAULT_KEYBOARD_OVERLAY_RECT)
            self.sm.set('third_party_keyboard', keyboard)
            self.sm.save_settings()

    def _keyboard_config_for_write(self) -> dict:
        return third_party_keyboard_settings(self.sm)

    def _save_keyboard_config(self, config: dict) -> None:
        self.sm.set('third_party_keyboard', {
            'enabled': bool(config.get('enabled', False)),
            'hwnd': max(0, int(config.get('hwnd', 0) or 0)),
            'window_name': str(config.get('window_name', '') or ''),
            'color': str(config.get('color', DEFAULT_KEYBOARD_COLOR)),
            'intensity': int(config.get('intensity', DEFAULT_KEYBOARD_INTENSITY)),
            'rect': clamp_overlay_rect(
                config.get('rect'), DEFAULT_KEYBOARD_OVERLAY_RECT),
        })
        self.sm.save_settings()
        if self._keyboard_capture is not None:
            self._keyboard_capture.configure(
                third_party_keyboard_settings(self.sm))

    def _refresh_keyboard_windows(self) -> None:
        if 'keyboard_window_combo' not in self.__dict__:
            return
        pass

        self._keyboard_windows = enumerate_keyboard_windows()
        config = third_party_keyboard_settings(self.sm)
        saved_hwnd = config['hwnd']
        if saved_hwnd and not any(
                int(item.get('hwnd', 0) or 0) == saved_hwnd
                for item in self._keyboard_windows):
            self._keyboard_windows.insert(0, {
                'hwnd': saved_hwnd,
                'display_name': (
                    f"{config['window_name'] or 'Saved keyboard'} · NOT VISIBLE"),
                'title': config['window_name'],
                'is_keyboard_candidate': True,
            })

        combo = self.keyboard_window_combo
        combo.blockSignals(True)
        combo.clear()
        for item in self._keyboard_windows:
            marker = '★ ' if item.get('is_keyboard_candidate') else ''
            combo.addItem(
                marker + str(item.get('display_name') or item.get('title')
                            or 'Window'),
                int(item.get('hwnd', 0) or 0))
        if self._keyboard_windows:
            selected = next((index for index, item in enumerate(
                self._keyboard_windows)
                if int(item.get('hwnd', 0) or 0) == saved_hwnd), 0)
            combo.setCurrentIndex(selected)
        else:
            combo.addItem('No titled windows detected')
        combo.setEnabled(bool(self._keyboard_windows))
        combo.blockSignals(False)
        self._sync_keyboard_overlay_controls()

    def _on_keyboard_window_changed(self, index: int) -> None:
        if index < 0 or 'keyboard_window_combo' not in self.__dict__:
            return
        value = self.keyboard_window_combo.itemData(index)
        if value is None:
            return
        try:
            hwnd = max(0, int(value))
        except (TypeError, ValueError):
            # User-editable window data is allowed to become stale between a
            # refresh and a selection; leave the current source unchanged.
            return
        if not hwnd:
            return
        config = self._keyboard_config_for_write()
        config['hwnd'] = hwnd
        config['window_name'] = self.keyboard_window_combo.itemText(index)
        config['enabled'] = True
        self.keyboard_overlay_check.blockSignals(True)
        self.keyboard_overlay_check.setChecked(True)
        self.keyboard_overlay_check.blockSignals(False)
        self._save_keyboard_config(config)
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_overlay_enabled('keyboard', True)
        self._sync_keyboard_overlay_controls()

    def _on_keyboard_overlay_toggled(self, checked: bool) -> None:
        config = self._keyboard_config_for_write()
        if checked and not config['hwnd']:
            self.keyboard_overlay_check.blockSignals(True)
            self.keyboard_overlay_check.setChecked(False)
            self.keyboard_overlay_check.blockSignals(False)
            self._sync_keyboard_overlay_controls()
            return
        config['enabled'] = bool(checked)
        self._save_keyboard_config(config)
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_overlay_enabled(
                'keyboard', bool(checked))
            if not checked:
                self.unified_overlay_editor.set_keyboard_pixmap(QPixmap())
        self._sync_keyboard_overlay_controls()

    def _sync_keyboard_color_swatch(self, color: str) -> None:
        if 'keyboard_color_swatch' not in self.__dict__:
            return
        color = str(color or DEFAULT_KEYBOARD_COLOR).lower()
        set_theme_style(self.keyboard_color_swatch,
            lambda color=color: (f'QPushButton {{ background: {color}; border: 1px solid '
            f'{Colors.BORDER_HI}; }} QPushButton:hover {{ border-color: '
            f'{Colors.ACCENT}; }}'))
        self.keyboard_color_value.setText(color.upper())

    def _start_keyboard_color_picker(self) -> None:
        if 'keyboard_source_preview' not in self.__dict__:
            return
        self.keyboard_source_preview.set_pick_mode(True)

    def _on_keyboard_color_picked(self, color) -> None:
        config = self._keyboard_config_for_write()
        config['color'] = color.name().lower()
        self._save_keyboard_config(config)
        self._sync_keyboard_color_swatch(config['color'])
        self._update_keyboard_preview()

    def _on_keyboard_intensity_changed(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        self.keyboard_intensity_value.setText(f'{value}%')
        config = self._keyboard_config_for_write()
        config['intensity'] = value
        self._save_keyboard_config(config)
        self._update_keyboard_preview()

    def _sync_keyboard_overlay_controls(self) -> None:
        if 'keyboard_overlay_check' not in self.__dict__:
            return
        config = third_party_keyboard_settings(self.sm)
        has_source = bool(config['hwnd'])
        windows = sys.platform == 'win32'
        self.keyboard_overlay_check.setEnabled(windows and has_source)
        self.keyboard_intensity.setEnabled(windows and has_source)
        self.keyboard_pick_color.setEnabled(windows and has_source)
        self.keyboard_color_swatch.setEnabled(windows and has_source)
        self.keyboard_source_preview.setEnabled(windows and has_source)
        self._sync_keyboard_color_swatch(config['color'])
        if 'unified_overlay_editor' in self.__dict__:
            self.unified_overlay_editor.set_overlay_enabled(
                'keyboard', config['enabled'])

    def _update_keyboard_preview(self) -> None:
        sm = getattr(self, 'sm', None)
        if sm is not None and not sm.get('settings_visual_previews_enabled', True):
            timer = self.__dict__.get('_keyboard_preview_timer')
            if timer is not None:
                timer.stop()
            return
        if self._keyboard_capture is None:
            return
        preview = self.__dict__.get('unified_overlay_editor', None)
        source_preview = self.__dict__.get('keyboard_source_preview', None)
        if preview is None or source_preview is None:
            return
        if not preview.isVisible() and not source_preview.isVisible():
            return
        frame, timestamp = self._keyboard_capture.latest_frame()
        config = third_party_keyboard_settings(self.sm)
        key = (timestamp, config['hwnd'], config['color'], config['intensity'])
        if (frame is not None and key == getattr(self, '_keyboard_preview_key', None)
                and not source_preview._image.isNull()
                and not preview._keyboard_pixmap.isNull()):
            return
        self._keyboard_preview_key = None
        if frame is None:
            error = self._keyboard_capture.last_error
            source_preview.set_empty_text(
                error or 'WAITING FOR KEYBOARD WINDOW')
            source_preview.set_image(QImage())
            preview.set_keyboard_pixmap(QPixmap())
            return

        import cv2 as _cv2
        source_h, source_w = frame.shape[:2]
        scale = min(1.0, 720 / max(1, source_w), 405 / max(1, source_h))
        if scale < 1.0:
            frame = _cv2.resize(
                frame,
                (max(2, int(source_w * scale)), max(2, int(source_h * scale))),
                interpolation=_cv2.INTER_AREA,
            )
        rgb = _cv2.cvtColor(frame, _cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        raw_image = QImage(
            rgb.tobytes(), w, h, ch * w,
            QImage.Format.Format_RGB888).copy()
        source_preview.set_image(raw_image, share_owned=True)

        rgba = chroma_key_rgba(
            frame, config['color'], config['intensity'])
        if rgba is None:
            preview.set_keyboard_pixmap(QPixmap())
            return
        keyed_image = QImage(
            rgba.tobytes(), w, h, 4 * w,
            QImage.Format.Format_RGBA8888).copy()
        preview.set_keyboard_pixmap(QPixmap.fromImage(keyed_image))
        self._keyboard_preview_key = key

    def _refresh_preset_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        names = self._presets_mgr.names()
        if names:
            self.preset_combo.addItems(names)
        else:
            self.preset_combo.addItem('— no presets —')
        self.preset_combo.blockSignals(False)

    def _on_preset_save(self):
        name, ok = FthrInputDialog.get_text(
            self,
            'Save Preset',
            'Name:',
            text=self.preset_combo.currentText() if self._presets_mgr.names() else '',
        )
        if not ok or not name.strip():
            return
        name = name.strip()
        data = {k: self.sm.get(k) for k in PRESET_KEYS}
        self._presets_mgr.save(name, data)
        self._refresh_preset_combo()
        idx = self.preset_combo.findText(name)
        if idx >= 0:
            self.preset_combo.setCurrentIndex(idx)

    def _on_preset_load(self):
        name = self.preset_combo.currentText()
        data = self._presets_mgr.load(name)
        if data is None:
            return
        filtered = filter_alpha_preset(data)
        previous = {key: self.sm.get(key) for key in filtered}
        for k, v in filtered.items():
            self.sm.set(k, v)
        main_win = self.window()
        if (hasattr(main_win, '_sync_requested_capture_settings')
                and not main_win._sync_requested_capture_settings()):
            for key, value in previous.items():
                self.sm.set(key, value)
            if hasattr(main_win, 'cap_settings_popup'):
                main_win.cap_settings_popup.reload_from_settings()
            return
        self.sm.save_settings()
        if hasattr(main_win, 'cap_settings_popup'):
            main_win.cap_settings_popup.reload_from_settings()
        if hasattr(main_win, '_restart_capture_engine'):
            main_win._restart_capture_engine()

    def _on_preset_delete(self):
        name = self.preset_combo.currentText()
        if not self._presets_mgr.names():
            return
        self._presets_mgr.delete(name)
        self._refresh_preset_combo()

    # Microphone helpers --

    def _on_mic_mono_changed(self, enabled):
        self.sm.set('mic_downmix_mono', bool(enabled))
        self.sm.save_settings()
        self.audio_capture_changed.emit(True)

    def _on_playback_device_changed(self, _index):
        self.sm.set('playback_device_id', self.playback_combo.currentData() or '')
        self.sm.set('playback_device_name', self.playback_combo.currentText())
        self.sm.save_settings()
        self.audio_capture_changed.emit(True)

    def _populate_playback_devices(self):
        if getattr(self, '_playback_scan_running', False):
            return
        self._playback_scan_running = True
        engine = getattr(self.window(), 'engine_path', None)
        def scan():
            result = []
            try:
                from core.windows_microphone_devices import parse_microphone_inventory
                if engine:
                    # The Linux engine lists PulseAudio sinks in the same schema;
                    # it needs the host library path, not the bundle's.
                    completed = subprocess.run(
                        [str(engine), '--list-playback-devices'], capture_output=True,
                        text=True, encoding='utf-8', timeout=5,
                        **({'creationflags': subprocess.CREATE_NO_WINDOW}
                           if sys.platform == 'win32'
                           else {'env': linux_runtime.child_process_environment()}))
                    if completed.returncode == 0:
                        result = [endpoint for endpoint in parse_microphone_inventory(completed.stdout)
                                  if endpoint.is_active]
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            self._playback_inventory_ready.emit(result)
        threading.Thread(target=scan, daemon=True, name='FTHRPlaybackInventory').start()

    def _apply_playback_devices(self, endpoints):
        self._playback_scan_running = False
        saved = self.sm.get('playback_device_id', '')
        self.playback_combo.blockSignals(True)
        try:
            self.playback_combo.clear()
            default_label, fallback_name, _hint = _playback_device_labels()
            self.playback_combo.addItem(default_label, '')
            for endpoint in endpoints:
                self.playback_combo.addItem(endpoint.display_name, endpoint.endpoint_id)
            index = self.playback_combo.findData(saved)
            if saved and index < 0:
                name = self.sm.get('playback_device_name', 'Selected device')
                self.playback_combo.addItem(
                    f'{name} (unavailable; using {fallback_name})', saved)
                index = self.playback_combo.count() - 1
            self.playback_combo.setCurrentIndex(max(0, index))
        finally:
            self.playback_combo.blockSignals(False)

    def _populate_mic_devices(self):
        """Discover microphones on a worker and apply results on the Qt thread.

        Native and PortAudio queries can block in drivers; generation checks discard
        stale results before updating widgets or settings.
        """
        if 'mic_combo' not in self.__dict__:
            return
        self._cancel_mic_discovery()
        self._mic_discovery_generation += 1
        generation = self._mic_discovery_generation
        saved_id = self.sm.get('mic_device_id') if self.sm is not None else None
        saved_name = self.sm.get('mic_device_name') if self.sm is not None else None
        previous_name = self.mic_combo.currentText() if self.mic_combo.count() else None

        engine_path = None
        main_window = self.window()
        candidate = getattr(main_window, 'engine_path', None)
        if candidate and Path(candidate).is_file():
            engine_path = candidate

        # Use native WASAPI inventory on Windows so IDs match the capture engine.
        # PortAudio queries can block in uncancellable host APIs; reserve them for
        # Linux, where Python owns microphone capture.
        legacy_query = (
            _sd.query_devices
            if _SD_AVAILABLE and sys.platform != 'win32' else None)
        self._mic_discovery_context = {
            'generation': generation,
            'saved_id': saved_id,
            'saved_name': saved_name,
            'previous_name': previous_name,
        }
        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem('Scanning microphones…', userData=None)
        self.mic_combo.blockSignals(False)

        job = MicrophoneDiscoveryJob(
            engine_path,
            legacy_query=legacy_query,
            timeout=5,
            generation=generation,
        )
        self._mic_discovery_job = job
        job.start(lambda _result, _error: None)
        self._mic_discovery_poll_timer.start()

    def _cancel_mic_discovery(self) -> None:
        job = self._mic_discovery_job
        if job is not None and not job.done:
            job.cancel()
        self._mic_discovery_job = None
        self._mic_discovery_context = None
        self._mic_discovery_poll_timer.stop()

    def _poll_mic_discovery(self) -> None:
        job = self._mic_discovery_job
        context = self._mic_discovery_context
        if job is None or context is None or not job.done:
            return
        self._mic_discovery_poll_timer.stop()
        self._mic_discovery_job = None
        self._mic_discovery_context = None
        if context['generation'] != self._mic_discovery_generation:
            return
        if isinstance(job.error, MicrophoneDiscoveryCancelled):
            return
        if job.error is not None:
            print(f'[Mic] Discovery failed: {job.error}')
            diagnostic_code = discovery_diagnostic_code(job.error)
            if diagnostic_code:
                emit_event(
                    'audio', 'endpoint_discovery_failed', state='FAILED',
                    error=diagnostic_code,
                    generation=int(context['generation']),
                    source='microphone',
                )
            result = MicrophoneDiscoveryResult(
                native_endpoints=(), legacy_indices={},
                native_error=str(job.error), legacy_error=None,
                generation=int(context['generation']),
            )
        else:
            result = job.result or MicrophoneDiscoveryResult(
                native_endpoints=(), legacy_indices={},
                native_error='microphone discovery returned no result',
                legacy_error=None, generation=int(context['generation']))
            emit_event(
                'audio', 'endpoint_discovery_completed', state='COMPLETED',
                source='microphone',
                **discovery_result_event_fields(result),
            )
        self._apply_mic_devices(
            result,
            saved_id=context['saved_id'],
            saved_name=context['saved_name'],
            previous_name=context['previous_name'],
        )

    def _apply_mic_devices(self, result: MicrophoneDiscoveryResult, *,
                           saved_id: str | None, saved_name: str | None,
                           previous_name: str | None) -> None:
        """Apply one current discovery result on the Qt thread only."""
        native_endpoints = result.native_endpoints
        legacy_indices = result.legacy_indices
        if result.native_error:
            print(f'[Mic] Native endpoint scan failed: {result.native_error}')
        if result.legacy_error:
            print(f'[Mic] Legacy endpoint scan failed: {result.legacy_error}')

        if not saved_id:
            migrated_id = migrate_legacy_microphone_name(saved_name, native_endpoints)
            if migrated_id and self.sm is not None:
                self.sm.set('mic_device_id', migrated_id)
                self.sm.save_settings()
                saved_id = migrated_id

        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        default_data = {'endpoint_id': None, 'legacy_index': None,
                        'display_name': 'System Default'}
        self.mic_combo.addItem('System Default', userData=default_data)
        active_ids = set()
        active_native_endpoints = [
            endpoint for endpoint in native_endpoints if endpoint.is_active]
        if active_native_endpoints:
            for endpoint in active_native_endpoints:
                active_ids.add(endpoint.endpoint_id)
                indices = legacy_indices.get(endpoint.display_name, [])
                legacy_index = indices[0] if len(indices) == 1 else None
                self.mic_combo.addItem(endpoint.display_name, userData={
                    'endpoint_id': endpoint.endpoint_id,
                    'legacy_index': legacy_index,
                    'display_name': endpoint.display_name,
                })
        elif _SD_AVAILABLE:
            # Linux remains on the legacy PortAudio path for this phase.
            for name, indices in legacy_indices.items():
                if len(indices) == 1:
                    self.mic_combo.addItem(name, userData={
                        'endpoint_id': None,
                        'legacy_index': indices[0],
                        'display_name': name,
                    })

        # Explicit selections remain explicit even after a USB/Bluetooth
        # device disappears or discovery fails. Passing this ID to the engine
        # produces a clear native failure instead of silently following
        # Default. This also prevents a stale worker result from selecting
        # the first newly discovered endpoint.
        if (saved_id
                and saved_id not in active_ids):
            self.mic_combo.addItem(
                f'{saved_name or "Selected microphone"} (unavailable)',
                userData={
                    'endpoint_id': saved_id,
                    'legacy_index': None,
                    'display_name': saved_name or 'Microphone',
                })

        selected = 0
        for index in range(self.mic_combo.count()):
            data = self.mic_combo.itemData(index)
            if isinstance(data, dict) and saved_id and data.get('endpoint_id') == saved_id:
                selected = index
                break
            if (not saved_id and saved_name and isinstance(data, dict)
                    and data.get('display_name') == saved_name):
                selected = index
                break
            if not saved_id and not saved_name and previous_name == self.mic_combo.itemText(index):
                selected = index
        self.mic_combo.setCurrentIndex(selected)
        self.mic_combo.blockSignals(False)
        if not self._mic_combo_connected:
            self.mic_combo.currentIndexChanged.connect(
                self._on_mic_device_changed)
            self._mic_combo_connected = True

    def _load_audio_settings(self):
        if self.sm is None:
            return
        # Block signals so loading values doesn't trigger saves mid-load.
        self.mic_combo.blockSignals(True)
        self.mic_vol_slider.blockSignals(True)
        self.mic_loopback_check.blockSignals(True)
        if 'separate_audio_check' in self.__dict__:
            self.separate_audio_check.blockSignals(True)
        self.notif_monitor_combo.blockSignals(True)
        for slider in self.sound_sliders.values():
            slider.blockSignals(True)
        self.notification_sounds_check.blockSignals(True)
        try:
            endpoint_id = self.sm.get('mic_device_id')
            name = self.sm.get('mic_device_name')
            for idx in range(self.mic_combo.count()):
                data = self.mic_combo.itemData(idx)
                if isinstance(data, dict) and endpoint_id and data.get('endpoint_id') == endpoint_id:
                    self.mic_combo.setCurrentIndex(idx)
                    break
                if (isinstance(data, dict) and not endpoint_id and name
                        and data.get('display_name') == name):
                    self.mic_combo.setCurrentIndex(idx)
                    break
            vol = int(self.sm.get('mic_volume', 100))
            self.mic_vol_slider.setValue(vol)
            self.mic_vol_value.setText(f'{vol}%')
            self.mic_level_meter.set_gain(vol / 100.0)
            self._update_mic_gain_warning(vol)
            self.mic_loopback_check.setChecked(bool(self.sm.get('mic_loopback', False)))
            self.separate_audio_check.setChecked(
                normalize_audio_capture_mode(self.sm.get(
                    'audio_capture_mode', AUDIO_CAPTURE_MODE_COMBINED))
                == AUDIO_CAPTURE_MODE_SEPARATED)
            saved_mon = self.sm.get('notification_monitor', 'auto')
            idx = self.notif_monitor_combo.findData(saved_mon)
            if idx >= 0:
                self.notif_monitor_combo.setCurrentIndex(idx)
            for key, slider in self.sound_sliders.items():
                sv = int(self.sm.get(f'sound_volume_{key}', 100))
                slider.setValue(sv)
                self.sound_value_labels[key].setText(f'{sv}%')
            self.notification_sounds_check.setChecked(bool(self.sm.get(
                'notification_sounds_enabled', True)))
            self._set_notification_sound_controls_enabled(
                self.notification_sounds_check.isChecked())
            self._sync_audio_controls()
        finally:
            self.mic_combo.blockSignals(False)
            self.mic_vol_slider.blockSignals(False)
            self.mic_loopback_check.blockSignals(False)
            if 'separate_audio_check' in self.__dict__:
                self.separate_audio_check.blockSignals(False)
            self.notif_monitor_combo.blockSignals(False)
            for slider in self.sound_sliders.values():
                slider.blockSignals(False)
            self.notification_sounds_check.blockSignals(False)
        self._load_gary_settings()

    def _load_gary_settings(self):
        if self.sm is None or 'gary_enabled_check' not in self.__dict__:
            return
        low, high = clamp_thresholds(
            self.sm.get('gary_min_level', DEFAULT_MIN_LEVEL),
            self.sm.get('gary_max_level', DEFAULT_MAX_LEVEL),
        )
        self.gary_enabled_check.blockSignals(True)
        try:
            self.gary_enabled_check.setChecked(
                bool(self.sm.get('gary_mode_enabled', False)))
            self._gary_min_level = low
            self._gary_max_level = high
            self.gary_image_path = self.sm.get('gary_image_path')
        finally:
            self.gary_enabled_check.blockSignals(False)
        self._refresh_gary_controls()
        self._refresh_gary_preview()

    def _save_gary_settings(self):
        if self.sm is None:
            return
        low, high = clamp_thresholds(
            self._gary_min_level, self._gary_max_level)
        self.sm.update({
            'gary_mode_enabled': self.gary_enabled_check.isChecked(),
            'gary_min_level': low,
            'gary_max_level': high,
            'gary_image_path': self.gary_image_path,
        })
        self.sm.save_settings()
        self.gary_settings_changed.emit()

    def _refresh_gary_controls(self):
        low, high = clamp_thresholds(
            self._gary_min_level, self._gary_max_level)
        self._gary_min_level, self._gary_max_level = low, high
        self.mic_level_meter.set_gary_thresholds(low, high)
        self.mic_level_meter.set_gary_enabled(
            self.gary_enabled_check.isChecked())

    def _on_gary_enabled_changed(self, _checked: bool):
        self._refresh_gary_controls()
        self._save_gary_settings()

    def _on_gary_thresholds_dragged(self, low: int, high: int):
        self._gary_min_level, self._gary_max_level = clamp_thresholds(low, high)
        self._refresh_gary_controls()
        self._save_gary_settings()

    def _choose_gary_image(self):
        start = self.gary_image_path or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self,
            'Choose Gary Mode image',
            start,
            'Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif);;All files (*.*)',
        )
        if not path:
            return
        if QPixmap(path).isNull():
            FthrMessageDialog.warning(
                self, 'Image not supported',
                'Choose a valid PNG, JPG, WEBP, BMP, or GIF image.')
            return
        self.gary_image_path = path
        self._refresh_gary_preview()
        self._save_gary_settings()

    def _reset_gary_image(self):
        self.gary_image_path = None
        self._refresh_gary_preview()
        self._save_gary_settings()

    def _effective_gary_image_path(self) -> Path:
        if self.gary_image_path:
            candidate = Path(self.gary_image_path).expanduser()
            if candidate.is_file():
                return candidate
        return Path(__file__).parent / 'assets' / 'gary.png'

    def _refresh_gary_preview(self):
        path = self._effective_gary_image_path()
        pix = QPixmap(str(path))
        if not pix.isNull():
            self.gary_preview.setPixmap(pix.scaled(
                self.gary_preview.size() - QSize(8, 8),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            self.gary_preview.clear()
            self.gary_preview.setText('No image')
        self.gary_preview.setToolTip(str(path))

    def _on_notif_monitor_changed(self, _idx: int):
        if self.sm is None:
            return
        val = self.notif_monitor_combo.currentData()
        self.sm.set('notification_monitor', val)
        self.sm.save_settings()
        self.notification_monitor_changed.emit()

    def _save_audio_settings(self):
        if self.sm is None:
            return
        data = self.mic_combo.currentData()
        endpoint_id = data.get('endpoint_id') if isinstance(data, dict) else None
        display_name = data.get('display_name') if isinstance(data, dict) else None
        self.sm.set('mic_device_id', endpoint_id)
        legacy_linux_selection = (
            sys.platform.startswith('linux') and isinstance(data, dict)
            and data.get('legacy_index') is not None
        )
        self.sm.set(
            'mic_device_name',
            display_name if endpoint_id or legacy_linux_selection else None)
        self.sm.set('mic_volume', self.mic_vol_slider.value())
        self.sm.set('mic_loopback', self.mic_loopback_check.isChecked())
        self.sm.save_settings()
        # Linux still owns its temporary legacy recorder. Windows applies the
        # requested endpoint/input gain only on the next engine generation.
        if sys.platform == 'win32':
            return
        try:
            from core.mic_recorder import MicRecorder
            if MicRecorder.is_available():
                rec = MicRecorder()
                rec.set_gain(self.mic_vol_slider.value() / 100.0)
                rec.start(self._selected_mic_index(),
                          gain=self.mic_vol_slider.value() / 100.0)
        except Exception as e:
            print(f'[Mic] settings push failed: {e}')

    def _selected_mic_index(self):
        data = self.mic_combo.currentData() if self.mic_combo.count() else None
        return data.get('legacy_index') if isinstance(data, dict) else data

    def _selected_mic_endpoint_id(self):
        data = self.mic_combo.currentData() if self.mic_combo.count() else None
        return data.get('endpoint_id') if isinstance(data, dict) else None

    def _on_mic_volume_changed(self, v: int):
        self.mic_vol_value.setText(f'{v}%')
        self.mic_level_meter.set_gain(v / 100.0)
        self._update_mic_gain_warning(v)
        if self._loopback_stream is not None:
            # Volume is read live by the loopback callback closure
            pass
        self._save_audio_settings()

    def _update_mic_gain_warning(self, value: int) -> None:
        """Show the distortion/restart warning only for digital boost values."""
        if 'mic_gain_warning' in self.__dict__:
            self.mic_gain_warning.setVisible(int(value) > 100)

    def _on_sound_volume_changed(self, key: str, v: int):
        if self.sm is None:
            return
        value_label = self.sound_value_labels.get(key)
        if value_label is not None:
            value_label.setText(f'{v}%')
        self.sm.set(f'sound_volume_{key}', v)
        self.sm.save_settings()

    def _set_notification_sound_controls_enabled(self, enabled: bool) -> None:
        for slider in self.sound_sliders.values():
            slider.setEnabled(bool(enabled))

    def _on_notification_sounds_toggled(self, enabled: bool) -> None:
        if self.sm is None:
            return
        enabled = bool(enabled)
        self._set_notification_sound_controls_enabled(enabled)
        self.sm.set('notification_sounds_enabled', enabled)
        self.sm.save_settings()

    def _on_mic_device_changed(self, _idx: int):
        idx = self._selected_mic_index()
        if sys.platform.startswith('linux'):
            # Tear down every old reader before MicRecorder replaces its stream.
            # The meter can share the recorder only after the new stream is live.
            self.mic_level_meter.stop()
            loopback_enabled = self.mic_loopback_check.isChecked()
            if loopback_enabled:
                self._stop_loopback()
            self._save_audio_settings()
            if (self.isVisible() and self.stack.currentIndex() == 2
                    and not getattr(self, '_background_ui_paused', False)):
                self.mic_level_meter.set_gain(
                    self.mic_vol_slider.value() / 100.0)
                self.mic_level_meter.start(idx)
            if loopback_enabled:
                self._start_loopback(idx)
            return

        # Restart meter on the new device
        self.mic_level_meter.stop()
        if (self.isVisible() and self.stack.currentIndex() == 2
                and not getattr(self, '_background_ui_paused', False)):
            self.mic_level_meter.set_gain(self.mic_vol_slider.value() / 100.0)
            self.mic_level_meter.start(idx)
        # Restart loopback if currently on
        if self.mic_loopback_check.isChecked():
            self._stop_loopback()
            self._start_loopback(idx)
        self._save_audio_settings()
        if sys.platform == 'win32':
            # Explicit/default microphone changes intentionally create a new
            # capture generation. A running native source is never silently
            # rebound to another endpoint or clock domain.
            self.audio_capture_changed.emit(True)

    def _start_loopback(self, device_index):
        self._stop_loopback()
        if (not _SD_AVAILABLE or not self.isVisible()
                or self.stack.currentIndex() != 2
                or self._background_ui_paused):
            return
        try:
            out_dev = None
            try:
                default_out = _sd.default.device[1]
                if default_out is not None and default_out >= 0:
                    out_dev = default_out
            except Exception:
                out_dev = None

            def _passthrough(indata, outdata, frames, time_info, status):
                vol = self.mic_vol_slider.value() / 100.0
                outdata[:] = indata * vol

            self._loopback_stream = _sd.Stream(
                device=(device_index, out_dev),
                channels=1,
                dtype='float32',
                samplerate=44100,
                blocksize=1024,
                callback=_passthrough,
            )
            self._loopback_stream.start()
        except Exception as e:
            print(f'Loopback start failed: {e}')
            self._loopback_stream = None
            self.mic_loopback_check.blockSignals(True)
            self.mic_loopback_check.setChecked(False)
            self.mic_loopback_check.blockSignals(False)

    def _stop_loopback(self):
        if self._loopback_stream is not None:
            try:
                self._loopback_stream.stop()
                self._loopback_stream.close()
            except Exception:
                # Loopback teardown is idempotent across device-loss callbacks.
                pass
            self._loopback_stream = None

    def _on_loopback_toggled(self, checked: bool):
        if checked:
            self._start_loopback(self._selected_mic_index())
        else:
            self._stop_loopback()
        self._save_audio_settings()

    # Page lifecycle — start/stop the live meter as the page comes/goes
    def showEvent(self, event):
        super().showEvent(event)
        if (hasattr(self, '_category_switch_timer')
                and self._pending_category_index != self.stack.currentIndex()
                and not getattr(self, '_background_ui_paused', False)):
            self._category_switch_timer.start()
            return
        # Only run the meter when the audio sub-page is selected
        if (hasattr(self, 'stack') and self.stack.currentIndex() == 2
                and not getattr(self, '_background_ui_paused', False)):
            self._audio_preview_timer.start()
        if ('_keyboard_preview_timer' in self.__dict__
                and hasattr(self, 'stack')
                and self.stack.currentIndex() == 3
                and not getattr(self, '_background_ui_paused', False)):
            self._keyboard_preview_timer.start()

    def hideEvent(self, event):
        self._camera_start_generation += 1
        self._camera_pending_device_index = None
        if '_camera_preview_timer' in self.__dict__:
            self._camera_preview_timer.stop()
        self._category_switch_timer.stop()
        self._audio_preview_timer.stop()
        self._cancel_mic_discovery()
        self._stop_loopback()
        if 'mic_level_meter' in self.__dict__:
            self.mic_level_meter.stop()
        if '_keyboard_preview_timer' in self.__dict__:
            self._keyboard_preview_timer.stop()
        super().hideEvent(event)

    def set_background_ui_paused(self, paused: bool) -> None:
        """Suspend settings-only previews without changing saved features."""
        paused = bool(paused)
        self._background_ui_paused = paused
        if paused:
            self._category_switch_timer.stop()
            self._audio_preview_timer.stop()
            self._cancel_mic_discovery()
            self._stop_loopback()
            if 'mic_level_meter' in self.__dict__:
                self.mic_level_meter.stop()
            for name in ('_camera_preview_timer', '_keyboard_preview_timer'):
                timer = self.__dict__.get(name)
                if timer is not None:
                    timer.stop()
            return

        if not self.isVisible() or not hasattr(self, 'stack'):
            return
        if self._pending_category_index != self.stack.currentIndex():
            self._category_switch_timer.start()
            return
        idx = self.stack.currentIndex()
        if idx == 2 and 'mic_level_meter' in self.__dict__:
            self._audio_preview_timer.start()
        if (idx == 3
                and self.sm.get('camera_enabled', False)
                and '_camera_preview_timer' in self.__dict__):
            self._camera_preview_timer.start()
        if idx == 3 and '_keyboard_preview_timer' in self.__dict__:
            self._keyboard_preview_timer.start()

    def _make_visuals_page_unified_legacy(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(0)

        preview_background_path = str(self.sm.get(
            'input_overlay_preview_background', '') or '')

        layout.addWidget(_flat_section_header('Visual Overlays'))
        layout.addSpacing(Sizes.ROW_GAP)

        columns = QHBoxLayout()
        columns.setSpacing(16)

        options = QFrame()
        options.setObjectName('overlayOptionsPanel')
        options.setFixedWidth(380)
        set_theme_style(options,
            lambda: ('QFrame#overlayOptionsPanel { background: transparent; border: none; }'))
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(0, 0, Sizes.ROW_GAP, 0)
        options_layout.setSpacing(0)

        def option_header(text: str) -> None:
            label = QLabel(text)
            set_theme_style(label, lambda: (label_uppercase(Colors.ACCENT, Fonts.SIZE_MICRO, 2)))
            options_layout.addWidget(label)
            options_layout.addSpacing(8)

        # Camera options
        option_header('Camera')
        self.camera_check = QCheckBox('Burn camera overlay into clips')
        set_theme_style(self.camera_check, checkbox_qss)
        self.camera_check.setChecked(bool(self.sm.get('camera_enabled', False)))
        self.camera_check.toggled.connect(self._on_camera_toggled)
        options_layout.addWidget(self.camera_check)
        options_layout.addSpacing(8)

        cam_dev_row = QHBoxLayout()
        cam_dev_row.setSpacing(Sizes.SPACE_3)
        cam_label = QLabel('Device')
        set_theme_style(cam_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        cam_label.setFixedWidth(58)
        cam_dev_row.addWidget(cam_label)
        self.camera_device_combo = _DropdownCombo()
        set_theme_style(self.camera_device_combo, combo_qss)
        self._populate_camera_devices()
        self.camera_device_combo.currentIndexChanged.connect(
            self._on_camera_device_changed)
        cam_dev_row.addWidget(self.camera_device_combo, 1)
        refresh = IconButton('refresh.png', label='Refresh')
        refresh.setObjectName('micRefreshBtn')
        refresh.setFixedSize(26, 26)
        refresh.setToolTip('Re-scan video devices')
        refresh.clicked.connect(self._populate_camera_devices)
        cam_dev_row.addWidget(refresh)
        options_layout.addLayout(cam_dev_row)
        options_layout.addSpacing(18)

        # Image options
        option_header('Image layers')
        image_layers = self._stored_image_layers()
        image_enabled = bool(
            image_layers and self.sm.get('image_overlay_enabled', False))
        self.image_overlay_check = QCheckBox('Burn image layers into clips')
        set_theme_style(self.image_overlay_check, checkbox_qss)
        self.image_overlay_check.setChecked(image_enabled)
        self.image_overlay_check.toggled.connect(self._on_image_overlay_toggled)
        options_layout.addWidget(self.image_overlay_check)
        options_layout.addSpacing(8)

        selector_row = QHBoxLayout()
        selector_row.setSpacing(8)
        self.image_overlay_selector = _DropdownCombo()
        set_theme_style(self.image_overlay_selector, combo_qss)
        self.image_overlay_selector.currentIndexChanged.connect(
            self._on_image_layer_selected)
        selector_row.addWidget(self.image_overlay_selector, 1)
        self.image_overlay_count = QLabel()
        self.image_overlay_count.setFixedWidth(92)
        self.image_overlay_count.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        set_theme_style(self.image_overlay_count,
            lambda: (label_uppercase(Colors.TEXT_MUTED, Fonts.SIZE_MICRO, 1)))
        selector_row.addWidget(self.image_overlay_count)
        options_layout.addLayout(selector_row)
        options_layout.addSpacing(8)

        image_actions = QHBoxLayout()
        image_actions.setSpacing(8)
        choose_image = QPushButton('Add images')
        set_theme_style(choose_image, button_primary_qss)
        choose_image.setMinimumHeight(36)
        choose_image.clicked.connect(self._choose_image_overlay)
        image_actions.addWidget(choose_image, 1)
        self.remove_image_overlay_btn = QPushButton('Remove')
        set_theme_style(self.remove_image_overlay_btn, button_outline_qss)
        self.remove_image_overlay_btn.setMinimumHeight(36)
        self.remove_image_overlay_btn.clicked.connect(
            self._remove_selected_image_overlay)
        image_actions.addWidget(self.remove_image_overlay_btn)
        self.clear_image_overlays_btn = QPushButton('Clear all')
        set_theme_style(self.clear_image_overlays_btn, button_secondary_qss)
        self.clear_image_overlays_btn.setMinimumHeight(36)
        self.clear_image_overlays_btn.clicked.connect(self._clear_image_overlay)
        image_actions.addWidget(self.clear_image_overlays_btn)
        options_layout.addLayout(image_actions)
        options_layout.addSpacing(12)

        image_controls = QHBoxLayout()
        image_controls.setSpacing(Sizes.SPACE_3)
        fit_label = QLabel('Fit')
        set_theme_style(fit_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        fit_label.setFixedWidth(30)
        image_controls.addWidget(fit_label)
        self.image_overlay_fit = _DropdownCombo()
        set_theme_style(self.image_overlay_fit, combo_qss)
        self.image_overlay_fit.addItem('Inside frame', 'fit')
        self.image_overlay_fit.addItem('Fill frame', 'fill')
        self.image_overlay_fit.currentIndexChanged.connect(
            self._on_image_overlay_fit_changed)
        image_controls.addWidget(self.image_overlay_fit, 1)
        opacity_label = QLabel('Opacity')
        set_theme_style(opacity_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        opacity_label.setFixedWidth(52)
        image_controls.addWidget(opacity_label)
        self.image_overlay_opacity = QSlider(Qt.Orientation.Horizontal)
        self.image_overlay_opacity.setRange(10, 100)
        self.image_overlay_opacity.setValue(100)
        set_theme_style(self.image_overlay_opacity, slider_qss)
        self.image_overlay_opacity.valueChanged.connect(
            self._on_image_overlay_opacity_changed)
        image_controls.addWidget(self.image_overlay_opacity, 1)
        self.image_overlay_opacity_value = QLabel(
            f"{self.image_overlay_opacity.value()}%")
        self.image_overlay_opacity_value.setFixedWidth(34)
        set_theme_style(self.image_overlay_opacity_value,
            lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        image_controls.addWidget(self.image_overlay_opacity_value)
        options_layout.addLayout(image_controls)
        options_layout.addSpacing(20)

        # Third-party keyboard options. The source is a separate top-level
        # Windows window (Noboard/NohBoard/Keyviz/etc.), just like an OBS
        # window-capture source. The capture worker stays independent of the
        # native replay engine so the eyedropper and intensity slider never
        # trigger an engine restart.
        option_header('Third-party keyboard')
        keyboard_config = third_party_keyboard_settings(self.sm)
        self.keyboard_overlay_check = QCheckBox(
            'Show external keyboard in clips')
        set_theme_style(self.keyboard_overlay_check, checkbox_qss)
        self.keyboard_overlay_check.setChecked(keyboard_config['enabled'])
        self.keyboard_overlay_check.toggled.connect(
            self._on_keyboard_overlay_toggled)
        options_layout.addWidget(self.keyboard_overlay_check)
        options_layout.addSpacing(8)

        keyboard_window_row = QHBoxLayout()
        keyboard_window_row.setSpacing(Sizes.SPACE_3)
        keyboard_window_label = QLabel('Window')
        set_theme_style(keyboard_window_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        keyboard_window_label.setFixedWidth(58)
        keyboard_window_row.addWidget(keyboard_window_label)
        self.keyboard_window_combo = _DropdownCombo()
        set_theme_style(self.keyboard_window_combo, combo_qss)
        self.keyboard_window_combo.currentIndexChanged.connect(
            self._on_keyboard_window_changed)
        keyboard_window_row.addWidget(self.keyboard_window_combo, 1)
        keyboard_refresh = IconButton('refresh.png', label='Refresh')
        keyboard_refresh.setObjectName('micRefreshBtn')
        keyboard_refresh.setFixedSize(26, 26)
        keyboard_refresh.setToolTip('Re-scan external keyboard windows')
        keyboard_refresh.clicked.connect(self._refresh_keyboard_windows)
        keyboard_window_row.addWidget(keyboard_refresh)
        options_layout.addLayout(keyboard_window_row)
        options_layout.addSpacing(8)

        self.keyboard_source_preview = KeyboardSourcePreview()
        self.keyboard_source_preview.set_empty_text(
            'SELECT A WINDOW TO PREVIEW')
        self.keyboard_source_preview.setFixedHeight(132)
        self.keyboard_source_preview.color_picked.connect(
            self._on_keyboard_color_picked)
        options_layout.addWidget(self.keyboard_source_preview)
        options_layout.addSpacing(8)

        keyboard_color_row = QHBoxLayout()
        keyboard_color_row.setSpacing(Sizes.SPACE_3)
        keyboard_color_label = QLabel('Key color')
        set_theme_style(keyboard_color_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        keyboard_color_label.setFixedWidth(58)
        keyboard_color_row.addWidget(keyboard_color_label)
        self.keyboard_color_swatch = QPushButton()
        self.keyboard_color_swatch.setFixedSize(36, 26)
        self.keyboard_color_swatch.setToolTip(
            'Pick the color to remove from the live keyboard preview')
        keyboard_color_row.addWidget(self.keyboard_color_swatch)
        self.keyboard_color_value = QLabel(keyboard_config['color'].upper())
        set_theme_style(self.keyboard_color_value,
            lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        keyboard_color_row.addWidget(self.keyboard_color_value)
        keyboard_color_row.addStretch()
        self.keyboard_pick_color = QPushButton('Pick from preview')
        set_theme_style(self.keyboard_pick_color, button_outline_qss)
        self.keyboard_pick_color.setMinimumHeight(30)
        self.keyboard_pick_color.clicked.connect(
            self._start_keyboard_color_picker)
        keyboard_color_row.addWidget(self.keyboard_pick_color)
        options_layout.addLayout(keyboard_color_row)
        options_layout.addSpacing(8)

        keyboard_intensity_row = QHBoxLayout()
        keyboard_intensity_row.setSpacing(Sizes.SPACE_3)
        keyboard_intensity_label = QLabel('Intensity')
        set_theme_style(keyboard_intensity_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        keyboard_intensity_label.setFixedWidth(58)
        keyboard_intensity_row.addWidget(keyboard_intensity_label)
        self.keyboard_intensity = _SettingsSlider(Qt.Orientation.Horizontal)
        self.keyboard_intensity.setRange(0, 100)
        self.keyboard_intensity.setValue(keyboard_config['intensity'])
        set_theme_style(self.keyboard_intensity, slider_qss)
        self.keyboard_intensity.valueChanged.connect(
            self._on_keyboard_intensity_changed)
        keyboard_intensity_row.addWidget(self.keyboard_intensity, 1)
        self.keyboard_intensity_value = QLabel(
            f"{keyboard_config['intensity']}%")
        self.keyboard_intensity_value.setFixedWidth(34)
        set_theme_style(self.keyboard_intensity_value,
            lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        keyboard_intensity_row.addWidget(self.keyboard_intensity_value)
        options_layout.addLayout(keyboard_intensity_row)
        options_layout.addSpacing(6)

        keyboard_hint = QLabel(
            'Capture a Noboard/NohBoard-style window, then click its solid '
            'background above. Changes are applied to the live preview '
            'immediately.')
        keyboard_hint.setWordWrap(True)
        set_theme_style(keyboard_hint, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        options_layout.addWidget(keyboard_hint)
        pass
        self._refresh_keyboard_windows()
        self._sync_keyboard_color_swatch(keyboard_config['color'])
        self._sync_keyboard_overlay_controls()
        options_layout.addSpacing(20)

        option_header('Preview background')
        self.input_overlay_preview_background_path = QLineEdit(
            preview_background_path or 'DEFAULT · DESKTOP SCREENSHOT')
        self.input_overlay_preview_background_path.setReadOnly(True)
        self.input_overlay_preview_background_path.setToolTip(
            str(self._default_overlay_preview_background_path())
            if not preview_background_path else preview_background_path)
        set_theme_style(self.input_overlay_preview_background_path, combo_qss)
        options_layout.addWidget(self.input_overlay_preview_background_path)
        options_layout.addSpacing(6)
        source_buttons = QHBoxLayout()
        source_buttons.setSpacing(Sizes.SPACE_3)
        choose_preview = QPushButton('Choose image')
        set_theme_style(choose_preview, button_outline_qss)
        choose_preview.setMinimumHeight(36)
        choose_preview.clicked.connect(
            self._choose_input_overlay_preview_background)
        source_buttons.addWidget(choose_preview)
        newest_preview = QPushButton('Use default')
        set_theme_style(newest_preview, button_secondary_qss)
        newest_preview.setMinimumHeight(36)
        newest_preview.clicked.connect(
            self._use_default_input_overlay_preview_background)
        source_buttons.addWidget(newest_preview)
        options_layout.addLayout(source_buttons)
        options_layout.addStretch()

        # Composite preview
        preview_panel = QFrame()
        preview_panel.setObjectName('overlayPreviewPanel')
        set_theme_style(preview_panel,
            lambda: ('QFrame#overlayPreviewPanel { background: transparent; border: none; }'))
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(Sizes.ROW_GAP, 0, 0, 0)
        preview_layout.setSpacing(8)
        preview_header = QHBoxLayout()
        preview_title = QLabel('Composite preview')
        set_theme_style(preview_title, lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_LABEL, 2)))
        preview_header.addWidget(preview_title)
        preview_header.addStretch()
        self.input_overlay_preview_source = QLabel(
            Path(preview_background_path).name
            if preview_background_path else 'DEFAULT · DESKTOP')
        set_theme_style(self.input_overlay_preview_source,
            lambda: (label_uppercase(Colors.TEXT_DIM, Fonts.SIZE_MICRO, 1)))
        self.input_overlay_preview_source.setToolTip(
            preview_background_path
            or str(self._default_overlay_preview_background_path()))
        preview_header.addWidget(self.input_overlay_preview_source)
        preview_layout.addLayout(preview_header)

        self.unified_overlay_editor = UnifiedOverlayPreview()
        self.unified_overlay_editor.setObjectName('unifiedOverlayPreview')
        self.unified_overlay_editor.set_background_paths([
            preview_background_path, str(self._default_overlay_preview_background_path())])
        self.unified_overlay_editor.set_rects({
            'camera': self.sm.get('camera_overlay_rect', DEFAULT_OVERLAY_RECT),
            'keyboard': keyboard_config['rect'],
        })
        self.unified_overlay_editor.set_camera_pixmap(QPixmap())
        self.unified_overlay_editor.set_keyboard_pixmap(QPixmap())
        self.unified_overlay_editor.set_image_layers(
            image_overlay_layers(self.sm))
        self.unified_overlay_editor.set_overlay_enabled(
            'camera', bool(self.sm.get('camera_enabled', False)))
        self.unified_overlay_editor.set_overlay_enabled(
            'keyboard', keyboard_config['enabled'])
        self.unified_overlay_editor.rects_changed.connect(
            self._on_input_overlay_rects_changed)
        self.unified_overlay_editor.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_layout.addWidget(self.unified_overlay_editor, 1)
        columns.addWidget(options, 0)
        columns.addWidget(preview_panel, 1)
        layout.addLayout(columns, 1)

        self._camera_preview_timer = QTimer(self)
        self._camera_preview_timer.setInterval(100)
        self._camera_preview_timer.timeout.connect(self._update_camera_preview)

        self._keyboard_preview_timer = QTimer(self)
        self._keyboard_preview_timer.setInterval(33)
        self._keyboard_preview_timer.timeout.connect(
            self._update_keyboard_preview)

        self._refresh_image_overlay_controls(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        set_theme_style(scroll, scrollbar_qss)
        scroll.setWidget(page)

        wrapper = QWidget()
        wrapper.setStyleSheet('background: transparent;')
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(scroll)
        return wrapper

    def _make_visuals_page(self):
        return self._make_visuals_page_unified_legacy()

    def _make_visuals_page_legacy(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, Sizes.SPACE_4, Sizes.SPACE_8)
        layout.setSpacing(0)
        preview_background = self._load_overlay_preview_background()

        # Camera Overlay --
        layout.addWidget(_flat_section_header('Camera Overlay'))
        layout.addSpacing(12)

        self.camera_check = QCheckBox('Burn camera overlay into clips')
        set_theme_style(self.camera_check, checkbox_qss)
        self.camera_check.setChecked(self.sm.get('camera_enabled', False))
        self.camera_check.toggled.connect(self._on_camera_toggled)
        layout.addWidget(self.camera_check)
        layout.addSpacing(8)

        cam_dev_row = QHBoxLayout()
        cam_dev_row.setSpacing(8)
        _dev_lbl = QLabel('Device')
        set_theme_style(_dev_lbl,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        _dev_lbl.setFixedWidth(80)
        cam_dev_row.addWidget(_dev_lbl)
        self.camera_device_combo = _DropdownCombo()
        set_theme_style(self.camera_device_combo, combo_qss)
        self._populate_camera_devices()
        self.camera_device_combo.currentIndexChanged.connect(self._on_camera_device_changed)
        cam_dev_row.addWidget(self.camera_device_combo, 1)
        refresh = IconButton('refresh.png', label='Refresh')
        refresh.setObjectName('micRefreshBtn')
        refresh.setFixedSize(26, 26)
        refresh.setToolTip('Re-scan video devices')
        refresh.clicked.connect(self._populate_camera_devices)
        cam_dev_row.addWidget(refresh)
        layout.addLayout(cam_dev_row)
        layout.addSpacing(6)

        self.camera_overlay_editor = CameraOverlayEditor()
        self.camera_overlay_editor.setObjectName('cameraOverlayEditor')
        self.camera_overlay_editor.set_rect(
            self.sm.get('camera_overlay_rect', DEFAULT_OVERLAY_RECT))
        self.camera_overlay_editor.set_enabled(
            bool(self.sm.get('camera_enabled', False)))
        self.camera_overlay_editor.set_background(preview_background)
        self.camera_overlay_editor.rect_changed.connect(
            self._on_camera_overlay_rect_changed)
        layout.addWidget(self.camera_overlay_editor)
        layout.addSpacing(6)
        self._camera_preview_timer = QTimer(self)
        self._camera_preview_timer.setInterval(100)
        self._camera_preview_timer.timeout.connect(self._update_camera_preview)

        # Image Overlay --
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Image Overlay'))
        layout.addSpacing(12)

        image_path = str(self.sm.get('image_overlay_path', '') or '')
        image_enabled = bool(self.sm.get('image_overlay_enabled', False))
        self.image_overlay_check = QCheckBox('Burn an image into clips')
        set_theme_style(self.image_overlay_check, checkbox_qss)
        self.image_overlay_check.setChecked(image_enabled)
        self.image_overlay_check.toggled.connect(self._on_image_overlay_toggled)
        layout.addWidget(self.image_overlay_check)
        layout.addSpacing(8)

        image_row = QHBoxLayout()
        image_row.setSpacing(8)
        image_label = QLabel('Image')
        set_theme_style(image_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        image_label.setFixedWidth(80)
        image_row.addWidget(image_label)
        self.image_overlay_path = QLineEdit(image_path)
        self.image_overlay_path.setReadOnly(True)
        self.image_overlay_path.setPlaceholderText('No image selected')
        set_theme_style(self.image_overlay_path, combo_qss)
        image_row.addWidget(self.image_overlay_path, 1)
        choose_image = QPushButton('Choose image')
        set_theme_style(choose_image, button_outline_qss)
        choose_image.clicked.connect(self._choose_image_overlay)
        image_row.addWidget(choose_image)
        clear_image = QPushButton('Clear')
        set_theme_style(clear_image, button_secondary_qss)
        clear_image.clicked.connect(self._clear_image_overlay)
        image_row.addWidget(clear_image)
        layout.addLayout(image_row)
        layout.addSpacing(8)

        image_controls = QHBoxLayout()
        image_controls.setSpacing(8)
        fit_label = QLabel('Placement')
        set_theme_style(fit_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        fit_label.setFixedWidth(80)
        image_controls.addWidget(fit_label)
        self.image_overlay_fit = _DropdownCombo()
        set_theme_style(self.image_overlay_fit, combo_qss)
        self.image_overlay_fit.addItem('Fit inside frame', 'fit')
        self.image_overlay_fit.addItem('Fill frame', 'fill')
        fit_index = self.image_overlay_fit.findData(
            self.sm.get('image_overlay_fit', 'fit'))
        self.image_overlay_fit.setCurrentIndex(max(0, fit_index))
        self.image_overlay_fit.currentIndexChanged.connect(
            self._on_image_overlay_fit_changed)
        image_controls.addWidget(self.image_overlay_fit, 1)
        opacity_label = QLabel('Opacity')
        set_theme_style(opacity_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        opacity_label.setFixedWidth(72)
        image_controls.addWidget(opacity_label)
        self.image_overlay_opacity = QSlider(Qt.Orientation.Horizontal)
        self.image_overlay_opacity.setRange(10, 100)
        self.image_overlay_opacity.setValue(
            int(self.sm.get('image_overlay_opacity', 100)))
        set_theme_style(self.image_overlay_opacity, slider_qss)
        self.image_overlay_opacity.valueChanged.connect(
            self._on_image_overlay_opacity_changed)
        image_controls.addWidget(self.image_overlay_opacity, 1)
        self.image_overlay_opacity_value = QLabel(
            f"{self.image_overlay_opacity.value()}%")
        self.image_overlay_opacity_value.setFixedWidth(38)
        set_theme_style(self.image_overlay_opacity_value,
            lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        image_controls.addWidget(self.image_overlay_opacity_value)
        layout.addLayout(image_controls)
        layout.addSpacing(6)

        self.image_overlay_editor = OverlayPlacementEditor(
            'IMAGE', DEFAULT_IMAGE_OVERLAY_RECT)
        self.image_overlay_editor.set_background(preview_background)
        self.image_overlay_editor.set_rect(self.sm.get(
            'image_overlay_rect', DEFAULT_IMAGE_OVERLAY_RECT))
        image_pixmap = QPixmap(image_path) if image_path else QPixmap()
        self.image_overlay_editor.set_overlay_pixmap(image_pixmap)
        self.image_overlay_editor.set_empty_text('CHOOSE AN IMAGE TO PREVIEW')
        self.image_overlay_editor.set_enabled(image_enabled)
        self.image_overlay_editor.rect_changed.connect(
            self._on_image_overlay_rect_changed)
        layout.addWidget(self.image_overlay_editor)
        layout.addSpacing(6)
        image_hint = QLabel('Transparent PNG and WebP files keep their alpha.')
        image_hint.setWordWrap(True)
        set_theme_style(image_hint, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        layout.addWidget(image_hint)

        # Keyboard visualization is intentionally shelved while mouse input
        # remains available in the unified editor.
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Input Overlays'))
        layout.addSpacing(10)
        coming_soon = QLabel(
            'Keyboard overlay coming soon.')
        coming_soon.setWordWrap(True)
        set_theme_style(coming_soon, lambda: (label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY)))
        layout.addWidget(coming_soon)

        layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        set_theme_style(scroll, scrollbar_qss)
        scroll.setWidget(page)

        wrapper = QWidget()
        wrapper.setStyleSheet('background: transparent;')
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(scroll)
        return wrapper

    def _on_theme_applied(self):
        """Re-apply the full app stylesheet using current theme colors."""
        global _COMBO_STYLE, _LABEL_STYLE
        theme = ThemeManager()
        colors = theme.get_all_colors()
        old_display, old_body = Fonts.DISPLAY, Fonts.BODY
        theme_fonts = theme.get_fonts()
        Fonts.configure(theme_fonts.get('display'), theme_fonts.get('body'))
        # Patch the Colors class at runtime so all future QSS references use
        # the custom values. This is a one-time operation per Apply click.
        from ui.style import Colors as C
        for token, value in colors.items():
            if hasattr(C, token):
                setattr(C, token, value)
        C.BAR_BG, C.BAR_FG = C.SHELL_BG, C.TEXT
        C.BAR_FG_DIM, C.BAR_HAIRLINE = C.TEXT_DIM, C.SHELL_DIVIDER
        _COMBO_STYLE = combo_qss()
        _LABEL_STYLE = label_uppercase(
            Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)
        app = QApplication.instance()
        if app is not None:
            apply_app_style(app)
        # Re-apply styles for this settings page
        self._apply_styles()
        self._set_settings_content_surface()
        # Detached editors, popups and dialogs are also part of the live UI.
        for surface in (app.topLevelWidgets() if app is not None else [self.window()]):
            retarget_widget_font_styles(
                surface, old_display=old_display, old_body=old_body)
            refresh_theme_styles(surface)
            for combo in surface.findChildren(_DropdownCombo):
                combo.refresh_theme_palette()
            surface.update()
        # Refresh samples in place, preserving controls, expansion and scroll.
        if '_customize_page' in self.__dict__:
            self._customize_page.refresh_theme()
        if 'keyboard_color_swatch' in self.__dict__:
            self._sync_keyboard_color_swatch(
                third_party_keyboard_settings(self.sm)['color'])
        # Signal the main window to rebuild its stylesheet
        top = self.window()
        if hasattr(top, '_apply_theme'):
            top._apply_theme()

    def _make_performance_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(_flat_section_header('Clip exports'))
        layout.addSpacing(12)
        self.parallel_exports_check = QCheckBox('Allow simultaneous exports')
        self.parallel_exports_check.setChecked(bool(self.sm.get('parallel_exports', False)))
        set_theme_style(self.parallel_exports_check, checkbox_qss)
        layout.addWidget(self.parallel_exports_check)
        self.parallel_export_limit_combo = _DropdownCombo()
        self.parallel_export_limit_combo.setAccessibleName('Maximum simultaneous exports')
        for count in (2, 3, 4):
            self.parallel_export_limit_combo.addItem(f'{count} exports at once', count)
        self.parallel_export_limit_combo.setCurrentIndex(max(0,
            self.parallel_export_limit_combo.findData(self.sm.get('parallel_export_limit', 2))))
        self.parallel_export_limit_combo.setEnabled(self.parallel_exports_check.isChecked())
        set_theme_style(self.parallel_export_limit_combo, combo_qss)
        layout.addWidget(self.parallel_export_limit_combo)
        export_hint = QLabel('Off by default: clips export one at a time. You can keep browsing and queue more clips. '
                            'Simultaneous exports use more CPU, GPU, memory and disk bandwidth and can affect recording or games. '
                            'Lowering the limit lets running jobs finish before starting more.')
        export_hint.setWordWrap(True)
        set_theme_style(export_hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(export_hint)
        from ui.export_jobs import show_export_jobs
        jobs_button = QPushButton('View export jobs')
        set_theme_style(jobs_button, button_primary_qss)
        jobs_button.clicked.connect(show_export_jobs)
        layout.addWidget(jobs_button)
        def save_export_performance(*_):
            enabled = self.parallel_exports_check.isChecked()
            self.parallel_export_limit_combo.setEnabled(enabled)
            self.sm.set('parallel_exports', enabled)
            self.sm.set('parallel_export_limit', self.parallel_export_limit_combo.currentData())
            self.sm.save_settings()
            from core.export_queue import configure
            configure(self.sm)
        self.parallel_exports_check.toggled.connect(save_export_performance)
        self.parallel_export_limit_combo.currentIndexChanged.connect(save_export_performance)
        layout.addSpacing(20)

        from core.ui_performance import PRESETS, UI_OPTIONS
        layout.addWidget(_flat_section_header('Interface performance'))
        layout.addSpacing(12)
        self.ui_performance_preset_combo = _DropdownCombo()
        self.ui_performance_preset_combo.setAccessibleName('Interface performance preset')
        self.ui_performance_preset_combo.addItems([*PRESETS, 'Custom'])
        set_theme_style(self.ui_performance_preset_combo, combo_qss)
        layout.addWidget(self.ui_performance_preset_combo)
        hint = QLabel(
            'Choose a preset, or adjust the options below to suit your PC. '
            'Impact shows how much each option may help your PC run more smoothly; '
            'results depend on how you use the app. These settings do not change '
            'recording or export quality.')
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(hint)
        layout.addSpacing(12)
        self._ui_performance_checks = {}
        aliases = {'pause_ui_in_background': 'performance_background_pause_check',
                   'reduce_ui_animations': 'reduce_ui_animations_check',
                   'clip_editor_live_preview': 'performance_clip_preview_check'}
        for key, default, title, impact, tip in UI_OPTIONS:
            check = QCheckBox(f'{title} · {impact} impact')
            check.setObjectName(key + '_performance_check')
            check.setToolTip(tip)
            set_theme_style(check, checkbox_qss)
            check.setChecked(bool(self.sm.get(key, default)))
            check.toggled.connect(
                lambda checked, setting=key: self._set_ui_performance_option(setting, checked))
            self._ui_performance_checks[key] = check
            if key in aliases:
                setattr(self, aliases[key], check)
            layout.addWidget(check)
            detail = QLabel(tip)
            detail.setWordWrap(True)
            set_theme_style(detail, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
            layout.addWidget(detail)
            layout.addSpacing(12)
        self._sync_ui_performance_controls()
        self.ui_performance_preset_combo.currentTextChanged.connect(self._apply_ui_performance_preset)
        layout.addSpacing(16)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addWidget(_flat_section_header('Audio Processing'))
        layout.addSpacing(12)

        self.performance_audio_check = QCheckBox(
            'Enable audio capture · Medium impact')
        set_theme_style(self.performance_audio_check, checkbox_qss)
        self.performance_audio_check.toggled.connect(
            self._on_audio_capture_toggled)
        layout.addWidget(self.performance_audio_check)
        layout.addSpacing(8)

        self._sync_audio_controls()

        # Keep the NVIDIA quality/performance choice available from the
        # performance-oriented settings tab as well as Clip. Both controls
        # write the same persisted encoder_preset value.
        self.performance_encoder_section = QWidget()
        self.performance_encoder_section.setStyleSheet(
            'background: transparent;')
        performance_encoder_layout = QVBoxLayout(
            self.performance_encoder_section)
        performance_encoder_layout.setContentsMargins(0, 0, 0, 0)
        performance_encoder_layout.setSpacing(0)
        performance_encoder_layout.addWidget(
            _flat_section_header('Video Encoding'))
        performance_encoder_layout.addSpacing(12)

        performance_encoder_row = QWidget()
        performance_encoder_row.setStyleSheet('background: transparent;')
        performance_encoder_row_layout = QHBoxLayout(performance_encoder_row)
        performance_encoder_row_layout.setContentsMargins(0, 0, 0, 0)
        performance_encoder_row_layout.setSpacing(Sizes.ROW_GAP)
        performance_encoder_label = QLabel('NVIDIA preset')
        performance_encoder_label.setFixedWidth(140)
        set_theme_style(performance_encoder_label,
            lambda: (label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)))
        performance_encoder_row_layout.addWidget(performance_encoder_label)

        self.performance_encoder_preset_combo = _DropdownCombo()
        for value, label in _NVENC_PRESET_OPTIONS:
            self.performance_encoder_preset_combo.addItem(label, value)
        set_theme_style(self.performance_encoder_preset_combo, combo_qss)
        saved_preset = int(self.sm.get('encoder_preset', 4))
        self.performance_encoder_preset_combo.setCurrentIndex(
            max(0, min(6, saved_preset - 1)))
        performance_encoder_row_layout.addWidget(
            self.performance_encoder_preset_combo, 1)
        performance_encoder_layout.addWidget(performance_encoder_row)
        self.performance_encoder_preset_row = performance_encoder_row
        self.performance_encoder_preset_row.setVisible(False)

        performance_encoder_actions = QHBoxLayout()
        performance_encoder_actions.setContentsMargins(0, 8, 0, 0)
        performance_encoder_actions.setSpacing(Sizes.ROW_GAP)
        self.performance_encoder_apply_btn = QPushButton('Apply')
        set_theme_style(self.performance_encoder_apply_btn, button_primary_qss)
        self.performance_encoder_apply_btn.setVisible(False)
        self.performance_encoder_apply_btn.clicked.connect(
            self._on_encoder_apply)
        performance_encoder_actions.addWidget(
            self.performance_encoder_apply_btn)
        performance_encoder_actions.addStretch()
        performance_encoder_layout.addLayout(performance_encoder_actions)
        self.performance_encoder_preset_combo.currentIndexChanged.connect(
            self._on_performance_encoder_preset_changed)
        self.performance_encoder_section.setVisible(False)
        layout.addWidget(self.performance_encoder_section)

        layout.addSpacing(28)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        set_theme_style(scroll, scrollbar_qss)
        layout.setContentsMargins(0, 0, 12, 20)
        scroll.setWidget(page)
        return scroll

    def _sync_ui_performance_controls(self):
        from core.ui_performance import DEFAULTS, matching_preset
        for key, check in self.__dict__.get('_ui_performance_checks', {}).items():
            check.blockSignals(True)
            check.setChecked(bool(self.sm.get(key, DEFAULTS[key])))
            check.blockSignals(False)
        combo = self.__dict__.get('ui_performance_preset_combo')
        if combo is not None:
            combo.blockSignals(True)
            combo.setCurrentText(matching_preset(self.sm))
            combo.blockSignals(False)
        check = self.__dict__.get('timeline_preview_strip_enabled_check')
        if check is not None:
            check.blockSignals(True)
            check.setChecked(bool(self.sm.get('timeline_preview_strip_enabled', True)))
            check.blockSignals(False)

    def _apply_ui_performance_preset(self, name):
        from core.ui_performance import PRESETS
        if name not in PRESETS:
            return
        for key, value in PRESETS[name].items():
            self.sm.set(key, value)
        self.sm.save_settings()
        self._apply_ui_performance_runtime()

    def _set_ui_performance_option(self, key, enabled):
        self.sm.set(key, bool(enabled))
        self.sm.save_settings()
        self._apply_ui_performance_runtime()

    def _apply_ui_performance_runtime(self):
        from ui.clip_viewer import ClipViewer, _TIMELINE_IMAGE_CACHE, _TIMELINE_CACHE_LOCK
        set_reduce_motion(self.sm.get('reduce_ui_animations', False))
        self.background_ui_pause_changed.emit(bool(self.sm.get('pause_ui_in_background', True)))
        timeline = bool(self.sm.get('timeline_preview_strip_enabled', True))
        for widget in QApplication.instance().allWidgets():
            if isinstance(widget, ClipViewer) and widget.sm is self.sm:
                widget._live_clip_preview = bool(self.sm.get('clip_editor_live_preview', True))
                widget.set_timeline_preview_enabled(timeline)
                widget._refresh_live_preview(immediate=True)
        if not timeline:
            with _TIMELINE_CACHE_LOCK:
                _TIMELINE_IMAGE_CACHE.clear()
        grid = getattr(self.window(), 'clip_grid', None)
        if grid is not None:
            grid.apply_thumbnail_preference()
        if not self.sm.get('settings_visual_previews_enabled', True):
            for name in ('_camera_preview_timer', '_keyboard_preview_timer'):
                timer = self.__dict__.get(name)
                if timer is not None:
                    timer.stop()
        self._sync_ui_performance_controls()

    def _on_reduce_ui_animations_toggled(self, checked: bool):
        self.sm.set('reduce_ui_animations', bool(checked))
        self.sm.save_settings()
        set_reduce_motion(checked)

    def _on_background_ui_pause_toggled(self, checked: bool):
        if self.sm is None:
            return
        checked = bool(checked)
        self.sm.set('pause_ui_in_background', checked)
        self.sm.save_settings()
        self.background_ui_pause_changed.emit(checked)

    def _on_clip_editor_preview_toggled(self, checked: bool):
        if self.sm is None:
            return
        self.sm.set('clip_editor_live_preview', bool(checked))
        self.sm.save_settings()

    def _on_capture_card_mode_changed(self, _index):
        mode = self.capture_card_mode_combo.currentData()
        self.sm.set('capture_card_mode', mode)
        self.sm.set('capture_card_enabled', mode != 'off')
        self.sm.save_settings()
        self.capture_card_changed.emit(mode != 'off')

    def _on_capture_card_toggled(self, checked: bool):
        if self.sm is None:
            return
        checked = bool(checked)
        self.sm.set('capture_card_enabled', checked)
        self.sm.save_settings()
        self.capture_card_changed.emit(checked)

    def _start_encoder_probe(self, *, force: bool = False):
        if self._encoder_probe_started and not force:
            return
        self._encoder_probe_started = True
        if not force:
            cached = load_cached_encoder_capabilities()
            if cached is not None:
                self._encoder_probe_from_cache = True
                self._on_encoder_capabilities_ready(cached)
                return

        self._encoder_probe_from_cache = False
        if 'encoder_combo' in self.__dict__:
            self.encoder_detection_lbl.setText('Checking this device…')
            self.encoder_refresh_btn.setEnabled(False)
            self.encoder_combo.setEnabled(False)
            self.codec_combo.setEnabled(False)

        def _probe():
            capabilities = probe_encoder_capabilities()
            save_encoder_capabilities_cache(capabilities)
            try:
                self.encoder_capabilities_ready.emit(capabilities)
            except RuntimeError:
                # Closing Settings while driver probing is in flight deletes
                # the Qt signal source; the result then has no consumer.
                return

        threading.Thread(
            target=_probe, name='fthr-encoder-probe', daemon=True).start()

    def _on_encoder_capabilities_ready(self, capabilities):
        self._encoder_capabilities = tuple(capabilities)
        self._encoder_inventory_ready = True
        if 'encoder_combo' not in self.__dict__:
            self._sync_encoder_controls(mark_dirty=False)
            return
        self.encoder_refresh_btn.setEnabled(True)
        saved_encoder = str(self.sm.get('encoder_pref', 'auto'))
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.clear()
        self.encoder_combo.addItem('Automatic (recommended)', 'auto')
        for capability in self._encoder_capabilities:
            self.encoder_combo.addItem(capability.label, capability.key)
        selected = self.encoder_combo.findData(saved_encoder)
        self.encoder_combo.setCurrentIndex(max(0, selected))
        self.encoder_combo.setEnabled(True)
        self.encoder_combo.blockSignals(False)
        if self._encoder_capabilities:
            prefix = ('Saved device profile · ' if self._encoder_probe_from_cache
                      else '')
            self.encoder_detection_lbl.setText(
                prefix + 'Available: ' + ', '.join(
                    capability.label for capability in self._encoder_capabilities))
        else:
            self.encoder_detection_lbl.setText(
                'No usable encoder was detected; Automatic remains available.')
        self._sync_encoder_controls(mark_dirty=False)

    def _sync_encoder_controls(self, *, mark_dirty: bool, refresh_clip=True):
        clip_loaded = 'encoder_combo' in self.__dict__
        encoder = (self.encoder_combo.currentData() or 'auto' if clip_loaded
                   else self.sm.get('encoder_pref', 'auto'))
        if mark_dirty:
            self._encoder_dirty = True
        if clip_loaded and refresh_clip:
            supported_codecs = available_codecs(self._encoder_capabilities, str(encoder))
            saved_codec = str(self.sm.get('codec_pref', 'auto'))
            previous_codec = (self.codec_combo.currentData() if mark_dirty else saved_codec)
            self.codec_combo.blockSignals(True)
            self.codec_combo.clear()
            self.codec_combo.addItem('Automatic', 'auto')
            codec_labels = {'h264': 'H.264', 'hevc': 'HEVC', 'av1': 'AV1'}
            for codec in supported_codecs:
                self.codec_combo.addItem(codec_labels[codec], codec)
            selected = self.codec_combo.findData(previous_codec or 'auto')
            self.codec_combo.setCurrentIndex(max(0, selected))
            self.codec_combo.setEnabled(True)
            self.codec_combo.blockSignals(False)

        supports_presets = str(encoder) == 'nvenc'
        preset = (self._pending_encoder_preset if self._pending_encoder_preset is not None
                  else int(self.sm.get('encoder_preset', 4)))
        if clip_loaded:
            self.encoder_preset_row.setVisible(supports_presets)
            self.encoder_preset_combo.setEnabled(supports_presets)
            if self._pending_encoder_preset is not None:
                self.encoder_preset_combo.blockSignals(True)
                self.encoder_preset_combo.setCurrentIndex(
                    max(0, self.encoder_preset_combo.findData(preset)))
                self.encoder_preset_combo.blockSignals(False)
            preset = int(self.encoder_preset_combo.currentData() or 4)
        if 'performance_encoder_section' in self.__dict__:
            self.performance_encoder_section.setVisible(supports_presets)
            self.performance_encoder_preset_row.setVisible(supports_presets)
            self.performance_encoder_preset_combo.blockSignals(True)
            self.performance_encoder_preset_combo.setCurrentIndex(
                max(0, self.performance_encoder_preset_combo.findData(preset)))
            self.performance_encoder_preset_combo.setEnabled(supports_presets)
            self.performance_encoder_preset_combo.blockSignals(False)
        for name in ('encoder_apply_btn', 'performance_encoder_apply_btn'):
            control = self.__dict__.get(name)
            if control is not None and (self._encoder_dirty or self._pending_encoder_preset is not None):
                control.setVisible(True)

    def _on_performance_encoder_preset_changed(self, index: int):
        self._pending_encoder_preset = int(
            self.performance_encoder_preset_combo.itemData(index) or 4)
        self._sync_encoder_controls(mark_dirty=True)

    def _on_encoder_setting_changed(self, _idx: int):
        self._pending_encoder_preset = int(self.encoder_preset_combo.currentData() or 4)
        self._sync_encoder_controls(mark_dirty=True)

    def _on_codec_setting_changed(self, _idx: int):
        self._encoder_dirty = True
        for name in ('encoder_apply_btn', 'performance_encoder_apply_btn'):
            control = self.__dict__.get(name)
            if control is not None:
                control.setVisible(True)

    def _on_encoder_apply(self):
        clip_loaded = 'encoder_combo' in self.__dict__
        encoder = str(self.encoder_combo.currentData() or 'auto' if clip_loaded
                      else self.sm.get('encoder_pref', 'auto'))
        codec = str(self.codec_combo.currentData() or 'auto' if clip_loaded
                    else self.sm.get('codec_pref', 'auto'))
        preset = (int(self.encoder_preset_combo.currentData() or 4) if clip_loaded
                  else self._pending_encoder_preset)
        if preset is None:
            preset = int(self.sm.get('encoder_preset', 4))
        self.sm.set('encoder_pref', encoder)
        self.sm.set('codec_pref', codec)
        self.sm.set('encoder_preset', preset)
        self.sm.save_settings()
        self._pending_encoder_preset = None
        self._encoder_dirty = False
        for name in ('encoder_apply_btn', 'performance_encoder_apply_btn'):
            control = self.__dict__.get(name)
            if control is not None:
                control.setVisible(False)
        self.encoder_config_changed.emit()

    def _apply_styles(self):
        set_theme_style(self, lambda: (f'''
            QWidget#settingsPage    {{ background-color: {Colors.BG}; }}
            QWidget#settingsContent {{ background-color: {Colors.BG}; }}
            QWidget#customizePage,
            QWidget#customizeContainer {{ background-color: {Colors.BG}; }}
            QScrollArea#customizeScroll {{ background-color: {Colors.BG}; }}

            QFrame#settingsTabBar {{
                background-color: {Colors.SHELL_BG_2};
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}

            QFrame#settingsDivider {{
                background-color: {Colors.SHELL_DIVIDER};
                border: none;
            }}

            QToolButton#settingsTabBtn {{
                background-color: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                color: {Colors.TEXT_DIM};
                font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px;
                font-weight: 400;
                padding: 14px 20px 10px 20px;
                min-width: 72px;
            }}
            QToolButton#settingsTabBtn:hover {{
                background-color: {Colors.SURFACE_3};
                color: {Colors.TEXT};
            }}
            QToolButton#settingsTabBtn:checked {{
                color: {Colors.TEXT};
                font-weight: 600;
                border-bottom: 2px solid {Colors.ACCENT};
                background-color: transparent;
            }}

            QPushButton#micRefreshBtn {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-size: 14px;
                font-weight: 600;
            }}
            QPushButton#micRefreshBtn:hover {{
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}

            QFrame#garyPanel {{
                background-color: transparent;
                border: none;
            }}
            QLabel#garyPreview {{
                background-color: {Colors.BG};
                border: 1px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_LABEL}px;
                letter-spacing: 0px;
            }}
            QPushButton#garyImageButton {{
                background-color: transparent;
                border: 1px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: 600;
                letter-spacing: 0px;
                padding: 5px 7px;
            }}
            QPushButton#garyImageButton:hover {{
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}

            {checkbox_qss()}
            {combo_qss()}
            {slider_qss()}

            QLabel {{
                color: {Colors.TEXT};
                font-size: {Fonts.SIZE_BODY_L}px;
                font-family: {Fonts.BODY};
                background: transparent;
            }}

            /* -- Accordion sections (Customize tab) -- */
            QFrame#accordionSection {{
                background-color: {Colors.SURFACE_3};
                border: 1px solid {Colors.BORDER};
            }}
            QPushButton#accordionHeader {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
                text-align: left;
            }}
            QPushButton#accordionHeader:hover {{
                background-color: {Colors.BORDER_HI};
            }}
            QWidget#accordionBody,
            QWidget#accordionBody > QWidget#accordionContent {{
                background-color: {Colors.SURFACE_3};
            }}
            QWidget#accordionBody QFrame#iconRow,
            QWidget#accordionBody QFrame#iconTintRow,
            QWidget#accordionBody QFrame#soundRow {{
                background-color: transparent;
                border: none;
            }}
            QFrame#colorPreview {{
                background-color: {Colors.BG};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
        '''))


# Entry point

def _load_fonts():
    """Register bundled Oswald and user-imported theme fonts with Qt."""
    fonts_dir = Path(__file__).parent / 'assets' / 'fonts'
    font_paths = list(fonts_dir.glob('*.ttf'))
    font_paths.extend(ThemeManager().get_custom_font_paths().values())
    for font_path in dict.fromkeys(font_paths):
        fid = QFontDatabase.addApplicationFont(str(font_path))
        if fid >= 0:
            families = QFontDatabase.applicationFontFamilies(fid)
            print(f"Font loaded: {font_path.name} -> {families}")
        else:
            print(f"Font failed to load: {font_path.name}")


def _prewarm_heavy_modules():
    """Warm OpenCV, QMediaPlayer, and FFmpeg resolution during the startup splash.

    This pays their one-time initialization cost before the first editor opens.
    """
    try:
        import cv2  # noqa: F401  — import alone is enough to load the .pyd
        # Touch a method so any lazy module-level init also runs.
        _ = cv2.__version__
    except Exception as e:
        print(f'[Prewarm] cv2 unavailable: {e}')

    try:
        from core.ffmpeg_tools import get_ffmpeg_exe as _warm_ffmpeg
        _warm_ffmpeg()
    except Exception as e:
        print(f'[Prewarm] ffmpeg unavailable: {e}')

    if sys.platform == 'win32':
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            # Pays the one-time cost of MediaFoundation init on Windows.
            _warm_player = QMediaPlayer()
            _warm_audio  = QAudioOutput()
            _warm_player.setAudioOutput(_warm_audio)
        except Exception as e:
            print(f'[Prewarm] Qt multimedia init failed: {e}')


def main():
    if sys.platform != 'win32':
        # Children (engine, ffmpeg, grim, hyprctl) must see the host's
        # library path, not PyInstaller's _internal; see linux_runtime.
        linux_runtime.restore_library_search_path_for_children()
    # When the frozen Windows exe is relaunched as the capture-card subprocess,
    # route into the card process instead of the main application.
    if '--card-process' in sys.argv:
        from ui.capture_card_process import main as _card_main
        _card_main()
        return

    background_start = '--background' in sys.argv
    print(f'Main.py successfully initiated background={background_start}')

    # Log resolved helper paths so missing tools and PATH overrides are diagnosable.
    if sys.platform != 'win32':
        print(linux_tools.report())

    # A second instance is destructive, not just redundant: two capture
    # engines fight over NVENC and over the single-writer shared-memory
    # command fields. Refuse before anything is started, but ask the owner to
    # restore its existing window so a normal second launch feels native.
    from core.single_instance import SingleInstance
    instance_guard = SingleInstance()
    if not instance_guard.acquire():
        print('[FTHR] Another instance is already running — exiting.')
        configure_qt_style()
        _app = QApplication(sys.argv)
        if sys.platform != 'win32':
            _app.setDesktopFileName('fthr-clips')
        from core.instance_activation import request_existing_instance_activation
        if request_existing_instance_activation():
            print('[Lifecycle] ExistingInstanceActivated')
            return 0
        # The owner may be starting up or its local activation endpoint may
        # have failed. Do not start a competing capture engine; retain a clear
        # fallback explanation instead.
        FthrMessageDialog.warning(
            None,
            'FTHR Clips is already running',
            'FTHR Clips is already open.\n\n'
            'Look for the window on your other monitors or in the system tray. '
            'If you believe this is wrong, end the running FTHRClips process '
            'and start it again.',
        )
        return 1

    diagnostic_session = start_diagnostic_session(APP_VERSION)
    diagnostic_session.collect_hardware_async()
    if HotkeyManager.uses_portal_shortcuts():
        # QApplication registers its app id with the host portal as it
        # starts, which xdg-desktop-portal refuses until fthr-clips.desktop
        # exists; install it first so the very first launch is identified too.
        linux_runtime.ensure_desktop_entry()
    configure_qt_style()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    if sys.platform != 'win32':
        # Match the installed desktop entry for Wayland taskbar/icon grouping.
        app.setDesktopFileName('fthr-clips')
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)
    app_icon = _load_icon('favicon.ico', 32)
    if not app_icon.isNull():
        app.setWindowIcon(app_icon)

    # Register bundled/theme fonts before Fusion snapshots the application
    # font. This keeps Oswald reliable for dialogs and native fallbacks too.
    _load_fonts()
    apply_app_style(app)
    _prewarm_heavy_modules()

    from core.instance_activation import InstanceActivationServer
    activation_server = InstanceActivationServer(parent=app)
    activation_server.start()

    try:
        window = MainWindow(background_start=background_start)
    except StoragePathError as error:
        settings_file = Path.home() / '.fthr' / 'settings.json'
        detail = (
            f'{error}\n\nTo continue, restore access to the configured clips '
            f'folder, or edit the clips_directory entry in:\n{settings_file}\n'
            'Then relaunch FTHR. The settings file will not be rewritten and '
            'media will not be moved automatically.')
        emit_event('storage', 'startup_storage_failed', state='FAILED',
                   detail=detail)
        print(f'[Storage] Startup stopped: {detail}')
        if not background_start:
            FthrMessageDialog('Storage location unavailable', detail).exec()
        activation_server.stop()
        instance_guard.release()
        end_diagnostic_session(clean=True)
        return 1
    activation_server.activation_requested.connect(window.restore_main_window)
    if not background_start:
        if window._desired_maximized:
            window.showMaximized()
        else:
            window.showNormal()
    else:
        print('[Lifecycle] BackgroundStartup')
    diagnostic_window = None if background_start else window
    emit_event(
        'ui', 'startup_display_snapshot',
        startup_mode=('background' if background_start else 'interactive'),
        displays=qt_display_snapshot(app, diagnostic_window),
        dpi_awareness=dpi_awareness_snapshot(diagnostic_window))

    try:
        return app.exec()
    finally:
        try:
            window._perform_full_shutdown()
            activation_server.stop()
            instance_guard.release()
        finally:
            end_diagnostic_session(clean=True)


if __name__ == '__main__':
    sys.exit(main())
