"""
FTHR Clips - Main Application Entry Point

Layout:
    Top bar  — white, frameless drag region:
               Logo | ■ status | [CAPTURE▼] [SOURCE▼] [HOTKEYS▼] | – ×
    Body     — black, clip grid fills all remaining space
"""

import sys
import subprocess
import time
import math
import os
import tempfile
import threading
from pathlib import Path
from datetime import datetime
_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QScrollArea, QFrame, QMessageBox, QComboBox,
    QGraphicsOpacityEffect, QSizePolicy, QStackedWidget,
    QListWidget, QListWidgetItem, QGroupBox, QCheckBox, QSlider,
    QToolButton, QButtonGroup, QFileDialog, QLineEdit,
)
from PyQt6.QtCore import (
    QTimer, pyqtSignal, Qt, QPoint, QPointF, QSize, QRect,
    QPropertyAnimation, QAbstractAnimation, QEasingCurve,
    QParallelAnimationGroup,
)
from PyQt6.QtGui import QPixmap, QFontDatabase, QFont, QCursor, QPainter, QPen, QColor, QIcon, QBrush, QPolygonF, QPalette

from core.capture_bridge import CaptureBridge
from core.hotkey_manager import HotkeyManager, AVAILABLE_KEYS
from core.game_detector import GameDetector
from core.presets_manager import PresetsManager, PRESET_KEYS
from core.settings_manager import SettingsManager
from core.theme_manager import ThemeManager
from core.mic_recorder import MicRecorder, write_wav
from ui.capture_card_client import CaptureCardClient
from ui.clip_grid import ClipGrid
from ui.customize_page import CustomizePage
from ui.capture_settings_widget import (
    CaptureSettingsWidget, _enumerate_capturable_windows,
)
from ui.style import (
    Colors, Fonts, Sizes,
    label_display, label_uppercase, label_body,
    STATUS_ACTIVE, STATUS_IDLE, STATUS_WARNING,
    COMBO_QSS, BUTTON_PRIMARY_QSS, BUTTON_OUTLINE_QSS,
    BUTTON_SECONDARY_QSS, BUTTON_PILL_QSS, BUTTON_PILL_GHOST_QSS,
    SLIDER_QSS, CHECKBOX_QSS, GROUPBOX_QSS,
    SCROLLBAR_QSS, TOOLTIP_QSS,
    scrollbar_qss, tooltip_qss,
)

try:
    import numpy as _np
    import sounddevice as _sd
    _SD_AVAILABLE = True
except Exception:
    _SD_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

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


def _load_logo_inverted(path: Path) -> QPixmap:
    """Load the logo PNG and invert RGB so the original black-on-white art
    becomes white-on-transparent — matches the dark top bar."""
    from PyQt6.QtGui import QImage
    img = QImage(str(path))
    if img.isNull():
        return QPixmap(str(path))
    img = img.convertToFormat(QImage.Format.Format_ARGB32)
    img.invertPixels(QImage.InvertMode.InvertRgb)
    return QPixmap.fromImage(img)


def _make_settings_icon(size: int = 18, color: str = Colors.TEXT) -> QIcon:
    """Draw a minimal 3-line sliders icon using QPainter."""
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    # Three horizontal lines (decreasing length → "filter/sliders" icon)
    p.drawLine(1, 4,  size - 1, 4)
    p.drawLine(1, 9,  size - 4, 9)
    p.drawLine(1, 14, size - 7, 14)
    p.end()
    return QIcon(pix)


def _icon_canvas(size: int):
    """Return (QPixmap, QPainter) ready for stroke drawing."""
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    return pix, p


def _make_general_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Gear / sun-burst — 8 pegs around a hollow ring."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = cy = size / 2
    # Centre ring
    r1 = size * 0.20
    p.drawEllipse(int(cx - r1), int(cy - r1), int(r1 * 2), int(r1 * 2))
    # 8 pegs
    for i in range(8):
        a = i * math.pi / 4
        x1 = cx + math.cos(a) * (r1 + 1.5)
        y1 = cy + math.sin(a) * (r1 + 1.5)
        x2 = cx + math.cos(a) * (size * 0.46)
        y2 = cy + math.sin(a) * (size * 0.46)
        p.drawLine(int(x1), int(y1), int(x2), int(y2))
    p.end()
    return QIcon(pix)


def _make_clip_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Filmstrip — rounded rectangle with sprocket holes top + bottom."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    pad = 2
    p.drawRect(pad, pad + 1, size - pad * 2, size - pad * 2 - 2)
    p.setBrush(QBrush(QColor(color)))
    p.setPen(Qt.PenStyle.NoPen)
    hole_w = (size - pad * 2 - 8) / 3
    for i in range(3):
        x = pad + 4 + int(i * (hole_w + 2))
        p.drawRect(x, pad + 4, max(2, int(hole_w)), 2)
        p.drawRect(x, size - pad - 6, max(2, int(hole_w)), 2)
    p.end()
    return QIcon(pix)


def _make_audio_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Speaker with two emanation arcs."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(QBrush(QColor(color)))
    # Speaker box
    p.drawRect(3, int(size * 0.38), 4, int(size * 0.24))
    # Speaker cone (triangle)
    cone = QPolygonF([
        QPointF(7, size * 0.38),
        QPointF(size * 0.55, size * 0.18),
        QPointF(size * 0.55, size * 0.82),
        QPointF(7, size * 0.62),
    ])
    p.drawPolygon(cone)
    # Sound arcs
    p.setBrush(Qt.BrushStyle.NoBrush)
    for i, r in enumerate([3.5, 6.5]):
        rect_x = int(size * 0.55 + i * 1)
        rect_y = int(size / 2 - r)
        p.drawArc(rect_x, rect_y, int(r * 2), int(r * 2),
                  -60 * 16, 120 * 16)
    p.end()
    return QIcon(pix)


def _make_upload_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Upward arrow rising from a tray — upload icon."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    cx = size / 2
    # Tray (horizontal base line)
    p.drawLine(3, int(size * 0.78), size - 3, int(size * 0.78))
    # Shaft of arrow
    p.drawLine(int(cx), int(size * 0.62), int(cx), int(size * 0.22))
    # Arrow head
    p.drawLine(int(cx), int(size * 0.22), int(cx - 4), int(size * 0.40))
    p.drawLine(int(cx), int(size * 0.22), int(cx + 4), int(size * 0.40))
    p.end()
    return QIcon(pix)


def _make_visuals_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Monitor — rectangle on a stand."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(2, 4, size - 4, int(size * 0.55))
    # Stand
    p.drawLine(int(size / 2) - 3, int(size * 0.78), int(size / 2) + 3, int(size * 0.78))
    p.drawLine(int(size / 2),     int(size * 0.59),  int(size / 2),     int(size * 0.78))
    p.end()
    return QIcon(pix)


def _make_version_icon(size: int = 22, color: str = Colors.TEXT) -> QIcon:
    """Up-arrow inside a downloading-style ring — version & updates."""
    pix, p = _icon_canvas(size)
    pen = QPen(QColor(color), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    # Three-quarter ring
    p.drawArc(3, 3, size - 6, size - 6, 30 * 16, 300 * 16)
    # Down arrow (download)
    cx = size / 2
    p.drawLine(int(cx), int(size * 0.30), int(cx), int(size * 0.66))
    p.drawLine(int(cx), int(size * 0.66), int(cx - 3), int(size * 0.55))
    p.drawLine(int(cx), int(size * 0.66), int(cx + 3), int(size * 0.55))
    p.end()
    return QIcon(pix)


_icon_registry: list[tuple[object, str, int]] = []

# QPainter fallbacks for icons that don't have a PNG asset on disk.
# Maps icon filename → maker(size, color) → QIcon.
_PAINTER_ICON_FALLBACKS: dict[str, object] = {
    'upload.png': _make_upload_icon,
}


def _tint_pixmap(pixmap: QPixmap, color: QColor) -> QPixmap:
    """Recolor all opaque pixels to *color*, preserving alpha."""
    tinted = QPixmap(pixmap.size())
    tinted.fill(Qt.GlobalColor.transparent)
    p = QPainter(tinted)
    p.drawPixmap(0, 0, pixmap)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(tinted.rect(), color)
    p.end()
    return tinted


def _load_icon(name: str, size: int = 20) -> QIcon:
    """Load icon, preferring custom theme override over default asset.

    Custom (imported) icons are used as-is.
    Default icons are tinted with the icon tint color from the theme.
    """
    theme = ThemeManager()

    # Custom imported icon — use as-is, no tinting
    try:
        custom = theme.get_custom_icon_path(name)
        if custom and custom.exists():
            pix = QPixmap(str(custom)).scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            return QIcon(pix)
    except Exception:
        pass

    # Performance icon = updates.png flipped vertically (arrow points up instead of down)
    if name == 'performance.png':
        from PyQt6.QtGui import QTransform
        src = Path(__file__).parent / 'assets' / 'icons' / 'updates.png'
        if src.exists():
            pix = QPixmap(str(src)).scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pix = pix.transformed(QTransform().scale(1, -1))
            tint_hex = theme.get_icon_tint('updates.png')
            pix = _tint_pixmap(pix, QColor(tint_hex))
            return QIcon(pix)

    # Default asset — apply icon tint color
    path = Path(__file__).parent / 'assets' / 'icons' / name
    if path.exists():
        pix = QPixmap(str(path)).scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        tint_hex = theme.get_icon_tint(name)
        pix = _tint_pixmap(pix, QColor(tint_hex))
        return QIcon(pix)
    # QPainter fallback (registered by feature modules for icons with no PNG)
    maker = _PAINTER_ICON_FALLBACKS.get(name)
    if maker:
        return maker(size)
    return QIcon()


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
            pass
    _icon_registry.clear()
    _icon_registry.extend(alive)


def _dims_to_label(w: int, h: int) -> str:
    return _DIMS_TO_LABEL.get((w, h), f'{w}×{h}' if w else 'SOURCE')


def _sanitize_foldername(name: str) -> str:
    """Strip Windows-invalid chars from a window title to make a safe folder name."""
    invalid = r'\/:*?"<>|'
    cleaned = ''.join(c for c in name if c not in invalid).strip('. ')
    return cleaned[:32] or 'Unknown'

# Brand accent kept as a local alias for inline f-strings sprinkled below.
# Single source of truth lives in style.Colors.
FTHR_TEAL     = Colors.ACCENT
FTHR_TEAL_DIM = Colors.ACCENT_DIM

# Animation timing — kept module-level so popups, the settings page, and the
# clip viewer all converge on the same fade duration. 180 ms is brisk enough
# that the user perceives the panel as "snappy" but slow enough that opening
# / closing doesn't read as a jump-cut.
PANEL_FADE_MS = 180


def _check_linux_input_group() -> bool:
    """Return True if this process has /dev/input access for global hotkeys."""
    if sys.platform == 'win32':
        return True
    import grp
    try:
        input_gid = grp.getgrnam('input').gr_gid
        return input_gid in os.getgroups()
    except Exception:
        return False


# Canonical QSS / label fragments come from style.py; aliased so call-sites stay short.
_COMBO_STYLE = COMBO_QSS
_LABEL_STYLE = label_uppercase(Colors.TEXT, Fonts.SIZE_MICRO, Fonts.TRACK_LABEL)


# ---------------------------------------------------------------------------
# Custom QComboBox — shows icons/dropdown.png as the arrow indicator and
# rotates it 180° while the popup is open, back to 0° when it closes.
# ---------------------------------------------------------------------------

class _DropdownCombo(QComboBox):
    _arrow_pix: 'QPixmap | None' = None  # class-level cache

    @classmethod
    def _get_arrow(cls) -> 'QPixmap | None':
        if cls._arrow_pix is None:
            path = Path(__file__).parent / 'assets' / 'icons' / 'dropdown.png'
            if path.exists():
                cls._arrow_pix = QPixmap(str(path))
        return cls._arrow_pix

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False
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
        pix = self._get_arrow()
        if pix is None or pix.isNull():
            return
        sz = 14
        scaled = pix.scaled(sz, sz,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        x = self.width() - sz - 8
        y = (self.height() - sz) // 2
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._popup_open:
            p.translate(x + sz / 2.0, y + sz / 2.0)
            p.rotate(180)
            p.drawPixmap(QRect(-sz // 2, -sz // 2, sz, sz), scaled)
        else:
            p.drawPixmap(x, y, scaled)
        p.end()


# ---------------------------------------------------------------------------
# Mic level meter — paints a horizontal RMS bar driven by a sounddevice stream
# ---------------------------------------------------------------------------

class _MicLevelMeter(QWidget):
    """Live mic-loudness bar. Updates at ~30Hz from an InputStream callback."""

    _level_changed = pyqtSignal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)
        self.setMinimumWidth(120)
        self._level    = 0.0
        self._peak     = 0.0
        self._stream   = None
        self._gain     = 1.0
        self._shared_recorder = None
        self._level_changed.connect(self._on_level)

        self._peak_decay = QTimer(self)
        self._peak_decay.setInterval(60)
        self._peak_decay.timeout.connect(self._decay_peak)

    def set_gain(self, g: float):
        self._gain = max(0.0, g)

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

        p.fillRect(0, 0, w, h, QColor(Colors.BG))
        p.setPen(QPen(QColor(Colors.HAIRLINE), 1))
        p.drawRect(0, 0, w - 1, h - 1)

        fill_w = max(int((w - 2) * self._level), 0)
        if fill_w > 0:
            p.fillRect(1, 1, fill_w, h - 2, QColor(Colors.ACCENT))

        peak_x = int((w - 2) * self._peak)
        if peak_x > 0:
            p.fillRect(1 + peak_x, 1, 2, h - 2, QColor(Colors.TEXT))


# ---------------------------------------------------------------------------
# RecordingDot — painted circle indicator; pulse driven externally via opacity
# ---------------------------------------------------------------------------

class RecordingDot(QWidget):
    """A small filled circle. Drawn with QPainter so it scales crisply at any
    DPI. Pulse is handled by the parent via QGraphicsOpacityEffect."""

    def __init__(self, diameter: int = 8, height: int = 52, parent=None):
        super().__init__(parent)
        self._diameter = diameter
        self._color = QColor(Colors.ACCENT)
        self.setFixedSize(diameter + 6, height)

    def set_color(self, hex_color: str) -> None:
        new_color = QColor(hex_color)
        if new_color != self._color:
            self._color = new_color
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(self._color))
        cx = self.width() // 2
        cy = self.height() // 2
        r = self._diameter // 2
        p.drawEllipse(cx - r, cy - r, self._diameter, self._diameter)
        p.end()


# ---------------------------------------------------------------------------
# Popup panel base — shared look for all dropdown panels
# ---------------------------------------------------------------------------

class _PopupPanel(QFrame):
    """Base class for top-bar dropdown panels (capture settings, source, hotkeys)."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName('popupPanel')
        self.setMinimumWidth(300)
        self.setStyleSheet(f'''
            QFrame#popupPanel {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER_HI};
                border-radius: {Sizes.RADIUS_MD}px;
            }}
            QFrame#popupPanel QLabel {{
                color: {Colors.TEXT};
                background-color: transparent;
            }}
        ''')
        self._show_anim: QPropertyAnimation | None = None

    def show_below(self, button: QWidget):
        pos = button.mapToGlobal(QPoint(0, button.height()))
        self.move(pos)
        self.show()
        self.raise_()
        # setWindowOpacity on Popup windows is not supported on Wayland —
        # the compositor just ignores it and spams a warning every frame.
        # Skip the fade on Wayland; just show instantly. Looks fine.
        app = QApplication.instance()
        if app and app.platformName() != 'wayland':
            self.setWindowOpacity(0.0)
            anim = QPropertyAnimation(self, b'windowOpacity', self)
            anim.setDuration(PANEL_FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._show_anim = anim
            anim.start()


# ---------------------------------------------------------------------------
# Capture Settings popup
# ---------------------------------------------------------------------------

class CaptureSettingsPopup(_PopupPanel):
    """Dropdown panel: clip length, fps, resolution, quality."""

    clip_length_changed   = pyqtSignal(int)
    extended_clip_changed = pyqtSignal(int)
    framerate_changed     = pyqtSignal(int)
    resolution_changed  = pyqtSignal(int, int)
    bitrate_changed     = pyqtSignal(int)
    restart_needed      = pyqtSignal()
    summary_changed     = pyqtSignal(str)   # emitted whenever any value changes

    _CLIP_VALUES  = [5, 10, 15, 30, 45, 60, 120, 180, 300, 600, 900]
    _CLIP_LABELS  = ['5s','10s','15s','30s','45s','1m','2m','3m','5m','10m','15m']
    _EXT_VALUES   = [30, 45, 60, 120, 180, 300, 600, 900]
    _EXT_LABELS   = ['30s','45s','1m','2m','3m','5m','10m','15m']
    _FPS_VALUES   = [30, 60, 120, 144, 165, 240, 360]
    _RES_LABELS   = ['480p','720p','1080p','1440p','Source']
    _RES_KEYS     = ['480p','720p','1080p','1440p','source']
    _QUAL_LABELS  = ['Low','Medium','High']
    _QUAL_KEYS    = ['low','medium','high']

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self._restart_pending = False

        self.cur_clip   = self.sm.get('clip_length',    30)
        self.cur_ext    = self.sm.get('extended_clip_length',  60)
        self.cur_fps    = self.sm.get('framerate',       60)
        self.cur_res    = self.sm.get('resolution',   'source')
        self.cur_qual   = self.sm.get('bitrate_level', 'high')

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        def _row(label_text, combo):
            row = QHBoxLayout()
            row.setSpacing(16)
            lbl = QLabel(label_text)
            lbl.setStyleSheet(_LABEL_STYLE)
            lbl.setFixedWidth(92)
            row.addWidget(lbl)
            row.addWidget(combo, stretch=1)
            return row

        # Clip length
        clip_idx = self._CLIP_VALUES.index(self.cur_clip) \
            if self.cur_clip in self._CLIP_VALUES else 3
        self.clip_combo = self._make_combo(self._CLIP_LABELS, clip_idx,
                                           self._on_clip_changed)
        layout.addLayout(_row('CLIP LENGTH', self.clip_combo))

        # Extended clip length
        ext_idx = self._EXT_VALUES.index(self.cur_ext) \
            if self.cur_ext in self._EXT_VALUES else 2  # default index for '1m'
        self.ext_combo = self._make_combo(self._EXT_LABELS, ext_idx,
                                          self._on_ext_clip_changed)
        layout.addLayout(_row('EXT. CLIP', self.ext_combo))

        # FPS
        fps_idx = self._FPS_VALUES.index(self.cur_fps) \
            if self.cur_fps in self._FPS_VALUES else 1
        self.fps_combo = self._make_combo(
            [str(v) for v in self._FPS_VALUES], fps_idx, self._on_fps_changed)
        layout.addLayout(_row('FRAMERATE', self.fps_combo))

        # Resolution
        res_idx = self._RES_KEYS.index(self.cur_res.lower()) \
            if self.cur_res.lower() in self._RES_KEYS else 4
        self.res_combo = self._make_combo(self._RES_LABELS, res_idx,
                                          self._on_res_changed)
        layout.addLayout(_row('RESOLUTION', self.res_combo))

        # Quality
        qual_idx = self._QUAL_KEYS.index(self.cur_qual) \
            if self.cur_qual in self._QUAL_KEYS else 2
        self.qual_combo = self._make_combo(self._QUAL_LABELS, qual_idx,
                                           self._on_qual_changed)
        layout.addLayout(_row('QUALITY', self.qual_combo))

        # Apply + Restart button (hidden until needed)
        self.restart_btn = QPushButton('APPLY + RESTART')
        self.restart_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.restart_btn.setVisible(False)
        self.restart_btn.clicked.connect(self._on_restart)
        layout.addWidget(self.restart_btn)

    def _make_combo(self, items, idx, callback):
        c = _DropdownCombo()
        c.addItems(items)
        c.setCurrentIndex(idx)
        c.setStyleSheet(_COMBO_STYLE)
        c.currentIndexChanged.connect(callback)
        return c

    def _on_clip_changed(self, idx):
        self.cur_clip = self._CLIP_VALUES[idx]
        self.sm.set('clip_length', self.cur_clip)
        self.sm.save_settings()
        self.clip_length_changed.emit(self.cur_clip)
        self._update_summary()

    def _on_ext_clip_changed(self, idx):
        self.cur_ext = self._EXT_VALUES[idx]
        self.sm.set('extended_clip_length', self.cur_ext)
        self.sm.save_settings()
        self.extended_clip_changed.emit(self.cur_ext)
        self._update_summary()

    def reload_from_settings(self):
        self.cur_clip = self.sm.get('clip_length', 30)
        self.cur_ext  = self.sm.get('extended_clip_length', 60)
        self.cur_fps  = self.sm.get('framerate', 60)
        self.cur_res  = self.sm.get('resolution', 'source')
        self.cur_qual = self.sm.get('bitrate_level', 'high')

        for combo, values, val in [
            (self.clip_combo, self._CLIP_VALUES, self.cur_clip),
            (self.ext_combo,  self._EXT_VALUES,  self.cur_ext),
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
        kbps = BITRATE_PRESETS[self.cur_res][self.cur_qual]
        self.bitrate_changed.emit(kbps)
        self._update_summary()

    def _on_qual_changed(self, idx):
        self.cur_qual = self._QUAL_KEYS[idx]
        self.sm.set('bitrate_level', self.cur_qual)
        self.sm.save_settings()
        kbps = BITRATE_PRESETS[self.cur_res][self.cur_qual]
        self.bitrate_changed.emit(kbps)
        self._update_summary()

    def _mark_restart(self):
        self._restart_pending = True
        self.restart_btn.setVisible(True)

    def _on_restart(self):
        self._restart_pending = False
        self.restart_btn.setVisible(False)
        self.restart_needed.emit()
        self.hide()

    def _update_summary(self):
        clip_label = self._CLIP_LABELS[self._CLIP_VALUES.index(self.cur_clip)] \
            if self.cur_clip in self._CLIP_VALUES else f'{self.cur_clip}s'
        res_label  = self._RES_LABELS[self._RES_KEYS.index(self.cur_res.lower())] \
            if self.cur_res.lower() in self._RES_KEYS else self.cur_res.upper()
        summary = f'{clip_label}  ·  {self.cur_fps}fps  ·  {res_label}  ·  {self.cur_qual.upper()}'
        self.summary_changed.emit(summary)

    def get_summary(self) -> str:
        clip_label = self._CLIP_LABELS[self._CLIP_VALUES.index(self.cur_clip)] \
            if self.cur_clip in self._CLIP_VALUES else f'{self.cur_clip}s'
        res_label  = self._RES_LABELS[self._RES_KEYS.index(self.cur_res.lower())] \
            if self.cur_res.lower() in self._RES_KEYS else self.cur_res.upper()
        return f'{clip_label}  ·  {self.cur_fps}fps  ·  {res_label}  ·  {self.cur_qual.upper()}'

    def get_bitrate(self) -> int:
        return BITRATE_PRESETS[self.cur_res][self.cur_qual]


# ---------------------------------------------------------------------------
# Source popup
# ---------------------------------------------------------------------------

class SourcePopup(_PopupPanel):
    """Dropdown panel: desktop / window selector."""

    source_changed = pyqtSignal(str, int)   # (mode, hwnd)
    restart_needed = pyqtSignal()
    summary_changed = pyqtSignal(str)

    def __init__(self, settings_manager: SettingsManager, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.cur_mode    = self.sm.get('capture_mode',    'desktop')
        self.cur_hwnd    = self.sm.get('target_hwnd',     0)
        self.cur_monitor = self.sm.get('capture_monitor', '')
        self._window_list: list = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        lbl = QLabel('CAPTURE SOURCE')
        lbl.setStyleSheet(_LABEL_STYLE)
        layout.addWidget(lbl)

        self.mode_combo = _DropdownCombo()
        self.mode_combo.addItems(['Desktop', 'Window / Game'])
        self.mode_combo.setStyleSheet(_COMBO_STYLE)
        if self.cur_mode == 'window':
            self.mode_combo.setCurrentIndex(1)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_combo)

        # Window list row
        win_row = QHBoxLayout()
        win_row.setSpacing(6)
        self.window_combo = _DropdownCombo()
        self.window_combo.setStyleSheet(_COMBO_STYLE)
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
            self.refresh_btn.setText('↺')
        self.refresh_btn.setFixedSize(28, 28)
        self.refresh_btn.setStyleSheet(f'''
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
        ''')
        self.refresh_btn.clicked.connect(self._refresh_windows)
        win_row.addWidget(self.refresh_btn)
        layout.addLayout(win_row)

        # Monitor selector (desktop mode only)
        self.monitor_combo = _DropdownCombo()
        self.monitor_combo.setStyleSheet(_COMBO_STYLE)
        self.monitor_combo.addItem('Erster Bildschirm (Standard)', userData='')
        for s in QApplication.screens():
            g = s.availableGeometry()
            self.monitor_combo.addItem(
                f'{s.name()}  ({g.width()}×{g.height()} @ {int(s.refreshRate())}Hz)',
                userData=s.name(),
            )
        saved_mon = self.cur_monitor
        idx = self.monitor_combo.findData(saved_mon)
        if idx >= 0:
            self.monitor_combo.setCurrentIndex(idx)
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)
        layout.addWidget(self.monitor_combo)

        self.restart_btn = QPushButton('APPLY + RESTART')
        self.restart_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.restart_btn.setVisible(False)
        self.restart_btn.clicked.connect(self._on_restart)
        layout.addWidget(self.restart_btn)

        self._update_window_visibility()
        if self.cur_mode == 'window':
            self._refresh_windows()

    def _update_window_visibility(self):
        show = (self.cur_mode == 'window')
        self.window_combo.setVisible(show)
        self.refresh_btn.setVisible(show)
        self.monitor_combo.setVisible(not show)

    def _on_monitor_changed(self, _idx: int):
        self.cur_monitor = self.monitor_combo.currentData()
        self.sm.set('capture_monitor', self.cur_monitor)
        self.sm.save_settings()
        self.restart_btn.setVisible(True)
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
        self.restart_btn.setVisible(True)
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
            self.sm.set('target_hwnd', self.cur_hwnd)
            self.sm.save_settings()
            self.restart_btn.setVisible(True)
            self._emit_summary()

    def _on_restart(self):
        self.restart_btn.setVisible(False)
        self.restart_needed.emit()
        self.hide()

    def _emit_summary(self):
        if self.cur_mode == 'desktop':
            label = 'DESKTOP'
        elif self._window_list:
            idx = self.window_combo.currentIndex()
            if 0 <= idx < len(self._window_list):
                label = self._window_list[idx]['display_name'].upper()
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
            return self._window_list[idx]['display_name'].upper()
        return 'WINDOW'


# ---------------------------------------------------------------------------
# Hotkey popup (compact version of existing HotkeyPanel)
# ---------------------------------------------------------------------------

class HotkeyPopup(_PopupPanel):

    def __init__(self, hotkey_manager: HotkeyManager, parent=None):
        super().__init__(parent)
        self.hotkey_manager = hotkey_manager
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        lbl = QLabel('HOTKEYS')
        lbl.setStyleSheet(_LABEL_STYLE)
        layout.addWidget(lbl)

        actions = [
            ('CAPTURE CLIP',          'save_clip'),
            ('EXTENDED CLIP',         'save_extended_clip'),
            ('SCREENSHOT',            'save_screenshot'),
        ]
        self._combos = {}
        for label_text, action_key in actions:
            row = QHBoxLayout()
            row.setSpacing(12)
            lbl2 = QLabel(label_text)
            lbl2.setStyleSheet(
                label_uppercase(Colors.TEXT, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL))
            lbl2.setFixedWidth(140)
            row.addWidget(lbl2)
            combo = _DropdownCombo()
            combo.addItems(AVAILABLE_KEYS)
            current = self.hotkey_manager.get_hotkey(action_key)
            if current in AVAILABLE_KEYS:
                combo.setCurrentText(current)
            combo.setStyleSheet(_COMBO_STYLE)
            combo.currentTextChanged.connect(
                lambda key, a=action_key: self.hotkey_manager.set_hotkey(a, key))
            self._combos[action_key] = combo
            row.addWidget(combo)
            layout.addLayout(row)


# ---------------------------------------------------------------------------
# TopBarButton — a styled button for the top bar dropdowns
# ---------------------------------------------------------------------------

class TopBarButton(QPushButton):
    """Compact rounded-rect button for the dark status row that shows a summary + dropdown arrow."""

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setStyleSheet(f'''
            QPushButton {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px;
                font-weight: bold;
                padding: 0px 14px;
                min-width: 60px;
            }}
            QPushButton:hover {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QPushButton:pressed {{
                color: {Colors.BG};
                background-color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
        ''')
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._base_text = text
        super().setText(f'{text}  ▾')

    def setText(self, text: str):
        self._base_text = text
        super().setText(f'{text}  ▾')


# ---------------------------------------------------------------------------
# Stats strip
# ---------------------------------------------------------------------------

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

        buf_text = QLabel('BUFFER')
        buf_text.setObjectName('statsBufText')
        layout.addWidget(buf_text)

        layout.addSpacing(12)

        # Thin progress bar for buffer fill — track is a quiet hairline,
        # fill is the brand accent so capture pressure reads at a glance.
        bar_wrap = QFrame()
        bar_wrap.setFixedSize(120, 3)
        bar_wrap.setStyleSheet(
            f'QFrame {{ background-color: {Colors.BORDER}; border: none; }}')
        self._buf_fill = QFrame(bar_wrap)
        self._buf_fill.setGeometry(0, 0, 0, 3)
        self._buf_fill.setStyleSheet(
            f'QFrame {{ background-color: {Colors.ACCENT}; border: none; }}')
        layout.addWidget(bar_wrap)

        layout.addSpacing(12)

        self._buf_time_lbl = QLabel('—')
        self._buf_time_lbl.setObjectName('statsBufTime')
        layout.addWidget(self._buf_time_lbl)

        self.setStyleSheet(f'''
            QFrame#statsStrip {{
                background-color: {Colors.BG};
                border-bottom: 1px solid {Colors.TEXT};
            }}
            QLabel#statsEncoder {{
                color: {Colors.ACCENT};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: bold;
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
                font-weight: bold;
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
        ''')

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



# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):

    clip_saved = pyqtSignal(str)

    def __init__(self):
        super().__init__()

        # ── Frameless window ──────────────────────────────────────────────
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.settings_manager = SettingsManager()

        from core.upload_manager import UploadManager
        self.upload_manager = UploadManager(self.settings_manager)
        # Give the settings widget a back-reference so its Save button can call
        # refresh_settings() without needing a direct signal connection.
        self.settings_manager._upload_manager_ref = self.upload_manager

        # Load custom theme colors before any UI is built so QSS uses them
        self._theme_mgr = ThemeManager()
        _theme_colors = self._theme_mgr.get_all_colors()
        for _tk, _val in _theme_colors.items():
            if hasattr(Colors, _tk):
                setattr(Colors, _tk, _val)

        self.clip_duration          = self.settings_manager.get('clip_length',          30)
        self.extended_clip_duration = self.settings_manager.get('extended_clip_length',  60)
        self.capture_fps            = self.settings_manager.get('framerate',             60)
        self.capture_bitrate        = self.settings_manager.get('bitrate_kbps',       16000)

        saved_res = self.settings_manager.get('resolution', 'source')
        self.capture_width, self.capture_height = _resolution_to_dims(saved_res)
        self.buffer_seconds = max(self.clip_duration, self.extended_clip_duration) + 2

        self.engine_process = None
        self.bridge         = CaptureBridge()

        if sys.platform == 'win32':
            project_root   = Path(__file__).parent.parent / 'FTHRcapture'
            possible_paths = [
                project_root / 'x64' / 'Release' / 'FTHRClips.exe',
                project_root / 'x64' / 'Debug'   / 'FTHRClips.exe',
                project_root / 'Release'          / 'FTHRClips.exe',
                project_root / 'Debug'            / 'FTHRClips.exe',
            ]
        else:
            _env_engine = os.environ.get('FTHR_ENGINE', '')
            if getattr(sys, 'frozen', False):
                # In frozen PyInstaller build the engine is in _internal/ (_MEIPASS)
                possible_paths = [Path(sys._MEIPASS) / 'FTHRclips']
            else:
                linux_root = Path(__file__).parent.parent / 'FTHRcapture_linux'
                possible_paths = [
                    *([ Path(_env_engine) ] if _env_engine else []),
                    linux_root / 'build' / 'FTHRclips',
                ]
        self.engine_path = None
        for p in possible_paths:
            if p.exists():
                self.engine_path = p
                print(f"Engine found: {p.parent.name}/{p.name}")
                break
        if not self.engine_path:
            print("Engine not found.")

        # Pre-create the special folders so users can find them right away
        for _folder in ('Desktop', 'Exported', 'Shared'):
            (Path.home() / 'FTHR_Clips' / _folder).mkdir(parents=True, exist_ok=True)

        self.hotkey_manager  = HotkeyManager()

        self._pending_game_window: dict | None = None
        self._active_game_hwnd:    int  | None = None
        self._game_dismiss_timer = QTimer(self)
        self._game_dismiss_timer.setSingleShot(True)
        self._game_dismiss_timer.timeout.connect(self._on_game_prompt_timeout)

        self._game_detector = GameDetector()
        self._game_detector.game_appeared.connect(self._on_game_appeared)
        self._game_detector.game_closed.connect(self._on_game_closed)

        if self.settings_manager.get('game_detection_enabled', False):
            self._game_detector.start()

        self.is_capturing    = True
        self.capture_card    = CaptureCardClient(self.settings_manager)
        self._encoder_type   = 'DETECTING'

        # Window drag state
        self._drag_pos: QPoint | None = None

        self.setWindowTitle('FTHR Clips 1.0.0-alpha')
        self.setMinimumSize(1100, 720)

        self._setup_ui()
        self._load_saved_theme()
        self._apply_styles()
        self._setup_hotkeys()
        if not _check_linux_input_group():
            QTimer.singleShot(1500, self._warn_input_group)

        # Start the always-on microphone recorder so saved clips can include
        # the user's voice. The C++ engine doesn't capture mic — we record
        # in Python and ffmpeg-mux it into each clip after save.
        self._start_mic_recorder()

        # Launch the capture engine once the event loop is running. Deferring
        # past __init__ keeps the window responsive while the engine boots and
        # the bridge polls for its shared memory. Once running, the status timer
        # below handles transparent reconnection if the engine ever dies.
        QTimer.singleShot(0, self.start_engine)

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(500)

    # =======================================================================
    # UI layout
    # =======================================================================

    def _setup_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ────────────────────────────────────────────────────────────────────
        # UNIFIED TOP BAR
        # logo  |  capture / source / hotkeys / gear  |  close-settings  |  — ✕
        # The mode-specific clusters (main vs settings) swap visibility when
        # _toggle_settings_page is called.
        # ────────────────────────────────────────────────────────────────────
        top_bar = QFrame()
        top_bar.setObjectName('topBar')
        top_bar.setFixedHeight(Sizes.UNIFIED_BAR_H)
        tb = QHBoxLayout(top_bar)
        tb.setContentsMargins(16, 0, 0, 0)
        tb.setSpacing(10)

        # ── Logo — check theme override first, then fall back to default asset.
        #    Default asset is black-on-white so we invert RGB for the dark bar.
        #    Custom logos are used as-is (user provides the final look). ──
        self._logo_label = QLabel()
        self._load_logo()
        self._logo_label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tb.addWidget(self._logo_label)

        tb.addStretch(1)

        # Hidden objects kept so internal status/dot methods don't crash
        self.rec_dot = RecordingDot(diameter=8, height=Sizes.UNIFIED_BAR_H)
        self.status_label = QLabel('CONNECTING')
        self.status_label.setStyleSheet(STATUS_IDLE)
        self.status_label.setObjectName('statusLabel')

        # ── Main-mode cluster: capture-settings dropdowns + gear ──
        self.main_mode_cluster = QFrame()
        self.main_mode_cluster.setObjectName('topClusterMain')
        mc = QHBoxLayout(self.main_mode_cluster)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(10)

        self.cap_settings_popup = CaptureSettingsPopup(self.settings_manager, self)
        self.cap_settings_popup.clip_length_changed.connect(self._on_clip_length_changed)
        self.cap_settings_popup.extended_clip_changed.connect(self._on_extended_clip_length_changed)
        self.cap_settings_popup.framerate_changed.connect(self._on_framerate_changed)
        self.cap_settings_popup.resolution_changed.connect(self._on_resolution_changed)
        self.cap_settings_popup.bitrate_changed.connect(self._on_bitrate_changed)
        self.cap_settings_popup.restart_needed.connect(self._restart_capture_engine)
        self.cap_settings_popup.summary_changed.connect(self._on_cap_summary_changed)

        self.cap_btn = TopBarButton(self.cap_settings_popup.get_summary())
        self.cap_btn.clicked.connect(self._toggle_cap_settings)
        mc.addWidget(self.cap_btn)

        self.source_popup = SourcePopup(self.settings_manager, self)
        self.source_popup.restart_needed.connect(self._restart_capture_engine)
        self.source_btn = TopBarButton('SOURCE')
        self.source_btn.clicked.connect(self._toggle_source)
        mc.addWidget(self.source_btn)

        self.hotkey_popup = HotkeyPopup(self.hotkey_manager, self)
        self.hotkey_btn = TopBarButton('HOTKEYS')
        self.hotkey_btn.clicked.connect(self._toggle_hotkeys)
        mc.addWidget(self.hotkey_btn)

        # NVENC / HW status label (hidden by default)
        self.hw_label = QLabel()
        self.hw_label.setObjectName('hwLabel')
        self.hw_label.setVisible(False)
        mc.addWidget(self.hw_label)

        self.settings_gear_btn = QPushButton()
        _gear_ico = _load_icon('settings(general).png', 18)
        self.settings_gear_btn.setIcon(_gear_ico if not _gear_ico.isNull() else _make_settings_icon(18, Colors.TEXT))
        self.settings_gear_btn.setIconSize(QSize(18, 18))
        _register_icon_widget(self.settings_gear_btn, 'settings(general).png', 18)
        self.settings_gear_btn.setObjectName('settingsGearBtn')
        self.settings_gear_btn.setFixedSize(36, 32)
        self.settings_gear_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.settings_gear_btn.clicked.connect(self._toggle_settings_page)
        mc.addWidget(self.settings_gear_btn)

        tb.addWidget(self.main_mode_cluster)

        # ── Settings-mode cluster: just the close-settings button ──
        self.settings_mode_cluster = QFrame()
        self.settings_mode_cluster.setObjectName('topClusterSettings')
        sc = QHBoxLayout(self.settings_mode_cluster)
        sc.setContentsMargins(0, 0, 0, 0)
        sc.setSpacing(10)

        self.close_settings_btn = QPushButton()
        self.close_settings_btn.setObjectName('homeBtn')
        self.close_settings_btn.setFixedSize(36, 32)
        _home_ico = _load_icon('home.png', 18)
        if not _home_ico.isNull():
            self.close_settings_btn.setIcon(_home_ico)
            self.close_settings_btn.setIconSize(QSize(18, 18))
            _register_icon_widget(self.close_settings_btn, 'home.png', 18)
        else:
            self.close_settings_btn.setText('⌂')
        self.close_settings_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.close_settings_btn.setToolTip('Back to clips')
        self.close_settings_btn.clicked.connect(self._toggle_settings_page)
        sc.addWidget(self.close_settings_btn)

        tb.addWidget(self.settings_mode_cluster)
        self.settings_mode_cluster.setVisible(False)

        # ── Window controls (always visible) ──
        self.min_btn = QPushButton()
        self.min_btn.setObjectName('winBtn')
        self.min_btn.setFixedSize(46, Sizes.UNIFIED_BAR_H)
        _min_ico = _load_icon('minimize.png', 14)
        if not _min_ico.isNull():
            self.min_btn.setIcon(_min_ico)
            self.min_btn.setIconSize(QSize(14, 14))
            _register_icon_widget(self.min_btn, 'minimize.png', 14)
        else:
            self.min_btn.setText('—')
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
            self.max_btn.setText('□')
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
            self.close_btn.setText('✕')
        self.close_btn.clicked.connect(self.close)
        tb.addWidget(self.close_btn)

        root.addWidget(top_bar)

        # Drag/double-click on the bar background (the buttons absorb their own clicks)
        top_bar.mousePressEvent       = self._bar_mouse_press
        top_bar.mouseMoveEvent        = self._bar_mouse_move
        top_bar.mouseReleaseEvent     = self._bar_mouse_release
        top_bar.mouseDoubleClickEvent = self._bar_double_click
        self._top_bar = top_bar

        # ── Main content stack ────────────────────────────────────────────
        self.main_stack = QStackedWidget()
        self.main_stack.setObjectName('mainStack')

        # Page 0: clip grid
        body_page = QWidget()
        body_page.setObjectName('body')
        body_layout = QVBoxLayout(body_page)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.clip_grid = ClipGrid(settings_manager=self.settings_manager)
        self.clip_grid.clip_opened.connect(self._on_clip_opened)
        scroll.setWidget(self.clip_grid)
        body_layout.addWidget(scroll, stretch=1)

        self.main_stack.addWidget(body_page)

        # Page 1: full-screen settings
        self._settings_page_widget = _SettingsPage(self.settings_manager)
        self._settings_page_widget.close_requested.connect(
            self._toggle_settings_page)
        self._settings_page_widget.imported_folders_changed.connect(
            self.clip_grid.force_refresh)
        self._settings_page_widget.notification_monitor_changed.connect(
            self.capture_card.restart)
        self._settings_page_widget.encoder_config_changed.connect(
            self._on_encoder_config_changed)
        self.main_stack.addWidget(self._settings_page_widget)

        # Wire upload manager → clip grid + start
        self.upload_manager.upload_finished.connect(self._on_upload_finished)
        self.clip_grid.clip_upload_requested.connect(self.upload_manager.enqueue_upload)
        self.clip_grid.set_upload_checker(self.upload_manager.is_uploaded)
        self.clip_grid.set_upload_enabled_checker(
            lambda: self.settings_manager.get('upload_enabled', False))
        self.upload_manager.start()

        root.addWidget(self.main_stack, stretch=1)

        # Hardware error bar (shown at bottom of app if NVENC fails)
        self.hw_error_bar = _HWErrorBar()
        self.hw_error_bar.retry_clicked.connect(self._on_retry_hardware_encoding)
        self.hw_error_bar.setVisible(False)
        root.addWidget(self.hw_error_bar)

        # Start recording dot animation
        self._setup_rec_dot()

    # ── Drag support ─────────────────────────────────────────────────────

    def _bar_mouse_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
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

    # ── Native Windows resize + Aero snap ────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_native_style_applied', False):
            self._native_style_applied = True
            # Defer SetWindowPos(SWP_FRAMECHANGED) to after the event loop starts.
            # Calling it synchronously inside showEvent sends WM_NCCALCSIZE back
            # into nativeEvent while Qt is mid-show, causing a crash.
            QTimer.singleShot(0, self._apply_native_style)
            # Pre-realize the settings page so the first time the user clicks
            # the gear button it doesn't pay for layout, font resolution, and
            # stylesheet compilation. The page is already constructed; we just
            # need Qt to do its first-show work for it.
            QTimer.singleShot(0, self._prerealize_settings_page)

    def _prerealize_settings_page(self):
        """Force Qt to do the deferred first-show work for the settings page.

        Adding a widget to a QStackedWidget doesn't trigger a full layout +
        style pass — that happens lazily on the first show. We pay that cost
        upfront here so the visible click-to-show transition is instant.

        ensurePolished() runs the QSS pass; adjustSize() forces a layout. We
        call them on the page itself and on every descendant widget so nested
        pages (the Audio sub-tab is the slowest) aren't deferred.
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
        # NOTE: do NOT call super().nativeEvent() — sip 6.15.1 crashes passing
        # the voidptr back to C++.  Returning (False, 0) lets Qt's WndProc
        # continue with default processing, which is identical in effect.
        if eventType == b'windows_generic_MSG':
            try:
                import ctypes, ctypes.wintypes
                ptr = int(message)
                if ptr:
                    # Safe peek: read only the UINT message field.
                    # MSG layout on 64-bit Windows:
                    #   HWND   hwnd    (8 bytes)
                    #   UINT   message (4 bytes)  ← uint32 index [2]
                    #   ...
                    msg_type = ctypes.cast(
                        ptr, ctypes.POINTER(ctypes.c_uint32))[2]
                    if msg_type == 0x0084:  # WM_NCHITTEST — safe to read full MSG
                        msg = ctypes.wintypes.MSG.from_address(ptr)
                        lp  = msg.lParam
                        cx  = ctypes.c_short(lp & 0xFFFF).value
                        cy  = ctypes.c_short((lp >> 16) & 0xFFFF).value
                        g   = self.frameGeometry()
                        bw  = 6  # resize border width in pixels

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

                        # Caption area: enables Aero snap & native drag
                        if cy < g.top() + Sizes.UNIFIED_BAR_H:
                            local = self.mapFromGlobal(QPoint(cx, cy))
                            w = self.childAt(local)
                            if w is None or not isinstance(w, (QPushButton, QComboBox)):
                                return True, 2  # HTCAPTION
            except Exception:
                pass
        return False, 0  # not handled — Qt WndProc continues normally

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
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

    # ── Helpers ──────────────────────────────────────────────────────────

    def _vsep(self):
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet(
            f'QFrame {{ color: {Colors.BORDER_HI};'
            f' max-width: 1px; margin: 12px 4px; }}'
        )
        return sep

    # ── Popup toggles ────────────────────────────────────────────────────

    def _toggle_cap_settings(self):
        if self.cap_settings_popup.isVisible():
            self.cap_settings_popup.hide()
        else:
            self.source_popup.hide()
            self.hotkey_popup.hide()
            self.cap_settings_popup.show_below(self.cap_btn)

    def _toggle_source(self):
        if self.source_popup.isVisible():
            self.source_popup.hide()
        else:
            self.cap_settings_popup.hide()
            self.hotkey_popup.hide()
            self.source_popup.show_below(self.source_btn)

    def _toggle_hotkeys(self):
        if self.hotkey_popup.isVisible():
            self.hotkey_popup.hide()
        else:
            self.cap_settings_popup.hide()
            self.source_popup.hide()
            self.hotkey_popup.show_below(self.hotkey_btn)

    def _on_cap_summary_changed(self, text: str):
        self.cap_btn.setText(text)

    def _toggle_settings_page(self):
        if self.main_stack.currentIndex() == 1:
            # Fade out settings, then switch back to clip grid
            effect = self._settings_page_widget.graphicsEffect()
            if effect is None:
                effect = QGraphicsOpacityEffect(self._settings_page_widget)
                self._settings_page_widget.setGraphicsEffect(effect)
            anim = QPropertyAnimation(effect, b'opacity', self)
            anim.setDuration(PANEL_FADE_MS)
            anim.setStartValue(1.0)
            anim.setEndValue(0.0)
            anim.setEasingCurve(QEasingCurve.Type.InCubic)
            anim.finished.connect(lambda: self.main_stack.setCurrentIndex(0))
            self._settings_fade_out = anim
            try:
                self._settings_page_widget._mappings_timer.stop()
            except AttributeError:
                pass
            anim.start()
            self.main_mode_cluster.setVisible(True)
            self.settings_mode_cluster.setVisible(False)
        else:
            self.main_stack.setCurrentIndex(1)
            if self.settings_manager.get('multiband_audio_enabled', False):
                try:
                    self._settings_page_widget._mappings_timer.start()
                except AttributeError:
                    pass
            self.main_mode_cluster.setVisible(False)
            self.settings_mode_cluster.setVisible(True)
            # Close any open dropdowns from main mode
            self.cap_settings_popup.hide()
            self.source_popup.hide()
            self.hotkey_popup.hide()
            # Fade in settings page
            effect = QGraphicsOpacityEffect(self._settings_page_widget)
            self._settings_page_widget.setGraphicsEffect(effect)
            effect.setOpacity(0.0)
            anim = QPropertyAnimation(effect, b'opacity', self)
            anim.setDuration(PANEL_FADE_MS)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            self._settings_fade_in = anim
            anim.start()

    # ── Recording dot ────────────────────────────────────────────────────

    def _setup_rec_dot(self):
        self._dot_effect = QGraphicsOpacityEffect(self.rec_dot)
        self.rec_dot.setGraphicsEffect(self._dot_effect)
        self._dot_anim = QPropertyAnimation(self._dot_effect, b'opacity', self)
        self._dot_anim.setDuration(900)
        self._dot_anim.setStartValue(1.0)
        self._dot_anim.setEndValue(0.15)
        self._dot_anim.setEasingCurve(QEasingCurve.Type.SineCurve)
        self._dot_anim.setLoopCount(-1)
        self._dot_anim.start()

    def _set_rec_dot_state(self, state: str):
        # Top bar is white, so the muted-state color must be a dark tone — using
        # white here was the source of the "no dot visible while disconnected" bug.
        colors = {
            'capturing':    Colors.ACCENT,
            'stopped':      Colors.TEXT_MUTED,
            'disconnected': Colors.TEXT_MUTED,
        }
        self.rec_dot.set_color(colors.get(state, Colors.TEXT_MUTED))
        if state == 'capturing':
            if self._dot_anim.state() != QAbstractAnimation.State.Running:
                self._dot_anim.start()
        else:
            self._dot_anim.stop()
            self._dot_effect.setOpacity(1.0)

    # =======================================================================
    # Hotkeys
    # =======================================================================

    def _warn_input_group(self):
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle('Hotkeys Disabled')
        msg.setText(
            'Global hotkeys are disabled because your user is not in the <b>input</b> group.<br><br>'
            'Run this command, then log out and back in:<br>'
            '<code>sudo usermod -aG input $USER</code>'
        )
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.exec()

    def _setup_hotkeys(self):
        self.hotkey_manager.save_clip_triggered.connect(self._on_hotkey_save_clip)
        self.hotkey_manager.save_extended_clip_triggered.connect(
            self._on_hotkey_save_extended_clip)
        self.hotkey_manager.save_screenshot_triggered.connect(
            self._on_hotkey_save_screenshot)
        self.hotkey_manager.confirm_game_detection_triggered.connect(
            self._on_confirm_game_detection)
        self.hotkey_manager.dismiss_game_detection_triggered.connect(
            self._on_dismiss_game_detection)
        self.hotkey_manager.register_all()
        print("Hotkeys registered.")

    def _on_hotkey_save_clip(self):          self._save_clip(self.clip_duration)
    def _on_hotkey_save_extended_clip(self): self._save_clip(self.extended_clip_duration)
    def _on_hotkey_save_screenshot(self):
        self.capture_card.show_screenshot()
        QMessageBox.information(self, 'Coming Soon', 'Screenshot feature coming soon!')

    def _on_game_appeared(self, window: dict):
        self._pending_game_window = window
        game_name = window.get('display_name', 'Game')
        hotkey = self.hotkey_manager.hotkeys.get('confirm_game_detection', 'F8')
        self.capture_card.show_prompt(
            f'{game_name} erkannt — [{hotkey}] Aufnehmen  [Esc] Ablehnen')
        self._game_dismiss_timer.start(15000)

    def _on_game_closed(self, hwnd: int):
        if hwnd != self._active_game_hwnd:
            return
        self._active_game_hwnd = None
        self.settings_manager.set('capture_mode', 'desktop')
        self.settings_manager.set('target_hwnd', 0)
        self.settings_manager.save_settings()
        self._restart_capture_engine()
        self.capture_card.show_prompt('Game geschlossen — zurück auf Desktop')

    def _on_confirm_game_detection(self):
        if self._pending_game_window is None:
            return
        hwnd = self._pending_game_window['hwnd']
        self._active_game_hwnd = hwnd
        self.settings_manager.set('capture_mode', 'window')
        self.settings_manager.set('target_hwnd', hwnd)
        self.settings_manager.save_settings()
        self._pending_game_window = None
        self._game_dismiss_timer.stop()
        self._restart_capture_engine()

    def _on_dismiss_game_detection(self):
        self._pending_game_window = None
        self._game_dismiss_timer.stop()

    def _on_game_prompt_timeout(self):
        self._pending_game_window = None

    # =======================================================================
    # Settings handlers
    # =======================================================================

    def _on_clip_length_changed(self, duration: int):
        self.clip_duration  = duration
        self.buffer_seconds = max(duration, self.extended_clip_duration) + 2

    def _on_extended_clip_length_changed(self, duration: int):
        self.extended_clip_duration = duration
        self.buffer_seconds = max(self.clip_duration, duration) + 2

    def _on_framerate_changed(self, fps: int):        self.capture_fps = fps
    def _on_resolution_changed(self, w: int, h: int): self.capture_width, self.capture_height = w, h
    def _on_bitrate_changed(self, kbps: int):         self.capture_bitrate = kbps

    # =======================================================================
    # Engine lifecycle
    # =======================================================================

    def connect_to_engine(self) -> bool:
        max_retries = 10
        for attempt in range(1, max_retries + 1):
            if self.bridge.initialize():
                print(f"Connected to capture engine (attempt {attempt}).")
                self._set_status('CAPTURING', STATUS_ACTIVE)
                self._set_rec_dot_state('capturing')
                QTimer.singleShot(2000, self._check_hardware_encoding_status)
                return True
            if attempt < max_retries:
                print(f"Connection attempt {attempt}/{max_retries} failed, retrying...")
                self._set_status(f'CONNECTING {attempt}/{max_retries}', STATUS_IDLE)
                self.bridge.shutdown()
                self.bridge = CaptureBridge()
                time.sleep(0.5)

        print("Bridge connection failed after all retries.")
        self._set_status('DISCONNECTED', STATUS_WARNING)
        return False

    def start_engine(self) -> bool:
        if not self.engine_path or not self.engine_path.exists():
            print("Engine executable not found.")
            self._set_status('NO ENGINE', STATUS_WARNING)
            return False

        # Calculate x264 fallback FramePool budget from ACTUAL capture resolution,
        # not worst-case 4K. Cap at 2 GB — above that the user is better served
        # by buying an NVIDIA card than by allocating half their RAM to a ring buffer.
        actual_w = self.capture_width  if self.capture_width  else 1920
        actual_h = self.capture_height if self.capture_height else 1080
        bytes_per_frame = actual_w * actual_h * 4
        frames_needed   = self.buffer_seconds * self.capture_fps
        mb_needed       = max(64, (frames_needed * bytes_per_frame + (1024*1024-1)) // (1024*1024))
        max_buffer_mb   = min(int(mb_needed) + 64, 2048)   # hard 2 GB ceiling

        capture_mode    = self.settings_manager.get('capture_mode',    'desktop')
        target_hwnd     = self.settings_manager.get('target_hwnd',     0)
        capture_monitor = self.settings_manager.get('capture_monitor', '')
        mode_arg        = '1' if (capture_mode == 'window' and target_hwnd) else '0'
        hwnd_arg        = str(int(target_hwnd))

        # 0 = stretch (default), 1 = fit (letterbox/pillarbox)
        scaling_mode = self.settings_manager.get('scaling_mode', 'stretch')
        scale_arg    = '1' if scaling_mode == 'fit' else '0'

        print(f"Starting engine  |  FPS={self.capture_fps}  "
              f"Buffer={self.buffer_seconds}s  "
              f"Bitrate={self.capture_bitrate}kbps  Pool={max_buffer_mb}MB  "
              f"Scale={scaling_mode}"
              + (f"  Monitor={capture_monitor}" if capture_monitor else ""))
        codec_pref_int = {
            'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3
        }.get(self.settings_manager.get('codec_pref', 'auto'), 0)
        encoder_preset = self.settings_manager.get('encoder_preset', 4)
        multiband_enabled = self.settings_manager.get('multiband_audio_enabled', False)
        if multiband_enabled:
            self._write_audio_categories_json()
        multiband_arg = '1' if multiband_enabled else '0'
        audio_enabled = self.settings_manager.get('audio_capture_enabled', True)
        audio_arg = '1' if audio_enabled else '0'
        try:
            self.engine_process = subprocess.Popen(
                [str(self.engine_path),
                 str(self.capture_fps), str(self.buffer_seconds),
                 str(self.capture_width), str(self.capture_height),
                 str(self.capture_bitrate), str(max_buffer_mb),
                 mode_arg, hwnd_arg, scale_arg, capture_monitor,
                 str(codec_pref_int), str(encoder_preset),
                 multiband_arg, audio_arg],
                **_NO_WINDOW
            )
            for _ in range(20):
                time.sleep(0.15)
                if self.bridge.initialize():
                    print("Connected to capture engine.")
                    self._set_status('CAPTURING', STATUS_ACTIVE)
                    self._set_rec_dot_state('capturing')
                    QTimer.singleShot(2000, self._check_hardware_encoding_status)
                    return True
            self._set_status('DISCONNECTED', STATUS_WARNING)
            return False
        except Exception as e:
            print(f"Engine start error: {e}")
            self._set_status('ERROR', STATUS_WARNING)
            return False

    def stop_engine(self):
        if self.bridge:
            self.bridge.shutdown()
        if self.engine_process:
            self.engine_process.terminate()
            try:
                self.engine_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.engine_process.kill()
            self.engine_process = None
        else:
            pass  # no known PID — do not kill by name to avoid affecting other instances

    def _restart_capture_engine(self):
        self._set_status('RESTARTING', STATUS_IDLE)
        self.stop_engine()
        QTimer.singleShot(1000, self._finish_restart)

    def _on_encoder_config_changed(self):
        codec  = self.settings_manager.get('codec_pref',     'auto')
        preset = self.settings_manager.get('encoder_preset', 4)
        if self.bridge.is_connected():
            self.bridge.set_encoder_config(codec, preset)
            self._set_status('APPLYING…', STATUS_IDLE)
        else:
            self._set_status('GESPEICHERT', STATUS_IDLE)

    def _write_audio_categories_json(self):
        """Write ~/.fthr/audio_categories.json for the C++ engine to read at startup."""
        import json as _json
        cats = self.settings_manager.get('audio_categories', [])
        path = Path.home() / '.fthr' / 'audio_categories.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            for cat in cats:
                sink_name = 'fthr_' + ''.join(
                    c if c.isalnum() else '_' for c in cat['name'].lower())
                obj = {
                    'name':     cat['name'],
                    'sink':     sink_name,
                    'patterns': cat.get('patterns', []),
                }
                f.write(_json.dumps(obj, ensure_ascii=True) + '\n')

    def _finish_restart(self):
        self.bridge = CaptureBridge()
        self.start_engine()

    # =======================================================================
    # Microphone recorder
    # =======================================================================

    def _start_mic_recorder(self):
        if not MicRecorder.is_available():
            print('[Mic] sounddevice not installed — mic-in-clips disabled')
            return
        device_index = self._resolved_mic_device_index()
        gain = float(self.settings_manager.get('mic_volume', 100)) / 100.0
        if MicRecorder().start(device_index, gain=gain):
            print(f'[Mic] Recorder started (device={device_index}, gain={gain:.2f})')
        else:
            print('[Mic] Recorder failed to start — clips will have no mic audio')

    def _resolved_mic_device_index(self):
        """Translate the saved mic_device_name into a sounddevice index, or None."""
        if not MicRecorder.is_available():
            return None
        name = self.settings_manager.get('mic_device_name')
        if not name:
            return None
        try:
            import sounddevice as _sd
            for i, dev in enumerate(_sd.query_devices()):
                if dev.get('max_input_channels', 0) > 0 and dev.get('name') == name:
                    return i
        except Exception:
            pass
        return None

    # =======================================================================
    # Save clip
    # =======================================================================

    def _save_clip(self, duration_seconds: int = 30):
        if not self.bridge or not self.bridge.is_connected():
            QMessageBox.warning(self, 'Not Connected',
                                'Capture engine is not running.')
            return

        timestamp    = datetime.now().strftime('%d%b%Y_%H-%M')
        clips_root   = Path.home() / 'FTHR_Clips'
        capture_mode = self.settings_manager.get('capture_mode', 'desktop')

        if capture_mode == 'window':
            idx = self.source_popup.window_combo.currentIndex()
            if 0 <= idx < len(self.source_popup._window_list):
                raw_name  = self.source_popup._window_list[idx]['display_name']
            else:
                raw_name  = 'Unknown'
            game_name    = _sanitize_foldername(raw_name)
            clips_folder = clips_root / game_name
            filename     = f'{game_name}_clip_from_{timestamp}.mp4'
        else:
            clips_folder = clips_root / 'Desktop'
            filename     = f'desktop_clip_from_{timestamp}.mp4'

        clips_folder.mkdir(parents=True, exist_ok=True)
        output_path  = clips_folder / filename
        print(f"Saving clip: {output_path.name}  ({duration_seconds}s)")

        # Capture the mic-window timestamp before requesting the save so the
        # post-mux thread can pull the matching mic segment from the ring.
        #
        # The C++ engine's audio ring excludes the newest 0.5 s (safety margin)
        # from every snapshot, so the system audio in the saved clip ends ~0.5 s
        # before the hotkey. Shift mic_end_time back by the same amount so the
        # mic WAV covers the identical wall-clock window as the system audio.
        # Without this correction the mic audio drifts ~0.5 s late relative to
        # the game audio and the last 0.5 s of mic before the hotkey ends up
        # spilling into the next clip's mix window.
        _AUDIO_SAFETY_S = 0.5
        mic_end_time = time.monotonic() - _AUDIO_SAFETY_S

        try:
            if self.bridge.save_clip(str(output_path), duration_seconds):
                print(f"Clip saved: {output_path.name}")
                self.clip_saved.emit(str(output_path))
                self.clip_grid._load_clips()
                self._set_status('SAVED', STATUS_ACTIVE)
                QTimer.singleShot(2000,
                    lambda: self._set_status('CAPTURING', STATUS_ACTIVE))
                res_label = _dims_to_label(self.capture_width, self.capture_height)
                self.capture_card.show_clip(duration_seconds, self.capture_fps, res_label)
                # Notify the upload manager and get the clip-ready event.
                # The event is set immediately if there's no mic mux pending;
                # otherwise the mux worker sets it after os.replace() completes.
                audio_on = self.settings_manager.get('audio_capture_enabled', True)
                multiband_on = audio_on and self.settings_manager.get('multiband_audio_enabled', False)
                mic_active = (audio_on and not multiband_on and
                              MicRecorder.is_available() and MicRecorder().is_running())
                clip_ready = self.upload_manager.notify_clip_saved(
                    str(output_path), has_mic_mux=mic_active)
                self._mux_mic_into_clip(
                    str(output_path), duration_seconds, mic_end_time, clip_ready)
                if multiband_on:
                    self._mux_multiband_into_clip(
                        str(output_path), duration_seconds, mic_end_time)
                if not mic_active and not multiband_on:
                    self._finalize_clip(str(output_path), duration_seconds)
            else:
                self.capture_card.show_error()
                QMessageBox.warning(self, 'Save Failed', 'Could not save clip.')
        except Exception as e:
            print(f"Save error: {e}")
            self.capture_card.show_error()
            QMessageBox.critical(self, 'Error', str(e))

    # ── Upload finished callback ──────────────────────────────────────────

    def _on_upload_finished(self, path: str, success: bool, msg: str):
        if success:
            self._set_status('UPLOADED', STATUS_ACTIVE)
            QTimer.singleShot(2000, lambda: self._set_status('CAPTURING', STATUS_ACTIVE))
            self.capture_card.show_upload(os.path.basename(path))
            # Refresh the badge on the matching clip card if it's visible
            widget = self.clip_grid._thumb_widgets.get(path)
            if widget:
                widget.set_uploaded(True)
        else:
            print(f'[Upload] Failed — {msg}  ({path})')

    # ── Mic post-mux ─────────────────────────────────────────────────────

    def _mux_mic_into_clip(self, clip_path: str,
                           duration_seconds: int,
                           mic_end_time: float,
                           clip_ready=None):
        """
        Wait for the engine to finish writing the clip, then mix the matching
        mic-recording segment into the clip's audio track. Runs in a daemon
        thread so the UI stays responsive.

        clip_ready is a threading.Event returned by UploadManager.notify_clip_saved().
        The mux worker sets it after os.replace() so the upload can start.
        If there is no mic to mux, the event is set here before returning.
        """
        if not MicRecorder.is_available() or not MicRecorder().is_running():
            if clip_ready is not None:
                clip_ready.set()
            return

        threading.Thread(
            target=self._mic_mux_worker,
            args=(clip_path, duration_seconds, mic_end_time, clip_ready),
            daemon=True,
        ).start()

    def _mic_mux_worker(self, clip_path: str,
                        duration_seconds: int,
                        mic_end_time: float,
                        clip_ready=None):
        try:
            import imageio_ffmpeg
        except ImportError:
            print('[Mic] imageio-ffmpeg missing — skipping mic mux')
            return

        try:
            # Wait for the clip file to actually appear and stabilize. The engine
            # runs SaveClipThread in the background; for short clips this is
            # usually <1s, but worst-case x264 path can take ~clip-duration.
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
                print(f'[Mic] Clip {clip_path} did not stabilize — skipping mux')
                return

            # Pull the mic samples covering the same window the clip captured.
            # The clip ends at mic_end_time and started duration_seconds earlier.
            samples = MicRecorder().extract_segment(mic_end_time, duration_seconds)
            if samples is None or samples.size == 0:
                print('[Mic] No mic samples for this clip window')
                return

            try:
                ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except RuntimeError:
                print('[Mic] ffmpeg not found — skipping mic mux')
                return

            # Write mic to a temp wav, then run ffmpeg to mix wav + clip's audio
            # back into a new mp4. Replace the original on success.
            with tempfile.TemporaryDirectory() as td:
                mic_wav = os.path.join(td, 'mic.wav')
                if not write_wav(mic_wav, samples):
                    return
                mixed_mp4 = os.path.join(td, 'mixed.mp4')

                # -filter_complex: take the first input's audio (system) and the
                # second input's audio (mic), mix them with amix=inputs=2.
                # duration=first keeps the clip length; dropout_transition=0
                # avoids amix attenuating when one input drops.
                cmd = [
                    ffmpeg, '-y',
                    '-i', clip_path,
                    '-i', mic_wav,
                    '-filter_complex',
                    '[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=0[aout]',
                    '-map', '0:v',
                    '-map', '[aout]',
                    '-c:v', 'copy',
                    '-c:a', 'aac', '-b:a', '192k',
                    '-shortest',
                    mixed_mp4,
                ]

                try:
                    result = subprocess.run(
                        cmd, capture_output=True,
                        **_NO_WINDOW,
                    )
                except Exception as e:
                    print(f'[Mic] ffmpeg mux error: {e}')
                    return

                if result.returncode != 0:
                    err = result.stderr.decode(errors='replace').strip().splitlines()
                    last = err[-1] if err else '(no stderr)'
                    print(f'[Mic] ffmpeg failed: {last}')
                    return

                # Replace the original clip with the mixed version.
                try:
                    os.replace(mixed_mp4, clip_path)
                    print(f'[Mic] Mixed mic into {os.path.basename(clip_path)}')
                    self._apply_watermark(clip_path, ffmpeg)
                except OSError as e:
                    print(f'[Mic] Could not replace clip: {e}')
        finally:
            # Always unblock the upload worker, regardless of success or failure.
            if clip_ready is not None:
                clip_ready.set()

    def _mux_multiband_into_clip(self, clip_path: str, duration_seconds: int,
                                  audio_end_time: float):
        """Start a background thread to mix per-category WAVs into the clip."""
        threading.Thread(
            target=self._multiband_mux_worker,
            args=(clip_path, duration_seconds, audio_end_time),
            daemon=True,
        ).start()

    def _multiband_mux_worker(self, clip_path: str, duration_seconds: int,
                               audio_end_time: float):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
            print('[MultiAudio] imageio-ffmpeg missing — skipping multiband mix')
            return

        from core.audio_mixer import mix_multiband_clip

        # Wait for clip file to stabilize (same pattern as mic mux)
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
            print(f'[MultiAudio] Clip {clip_path} did not stabilize — skipping mix')
            return

        # Find per-category WAV files written by the C++ engine next to the clip
        base = os.path.splitext(clip_path)[0]
        cats = self.settings_manager.get('audio_categories', [])
        category_wavs = {}
        volumes = {}
        for cat in cats:
            sink_name = 'fthr_' + ''.join(
                c if c.isalnum() else '_' for c in cat['name'].lower())
            wav_path = f'{base}_{sink_name}.wav'
            if os.path.exists(wav_path):
                category_wavs[cat['name']] = wav_path
                volumes[cat['name']] = cat.get('volume', 100) / 100.0

        if not category_wavs:
            print('[MultiAudio] No category WAVs found — skipping mix')
            return

        # Include mic audio in the multiband mix if mic recorder is active
        mic_wav_path = None
        try:
            from core.mic_recorder import MicRecorder, write_wav
            if MicRecorder.is_available() and MicRecorder().is_running():
                samples = MicRecorder().extract_segment(audio_end_time, duration_seconds)
                if samples is not None and samples.size > 0:
                    import tempfile as _tf
                    mic_tmp = _tf.NamedTemporaryFile(suffix='_mic.wav', delete=False)
                    mic_wav_path = mic_tmp.name
                    mic_tmp.close()
                    if write_wav(mic_wav_path, samples):
                        category_wavs['Mikrofon'] = mic_wav_path
                        volumes['Mikrofon'] = self.settings_manager.get('mic_volume', 100) / 100.0
        except Exception as e:
            print(f'[MultiAudio] Mic include error: {e}')

        ok = mix_multiband_clip(clip_path, category_wavs, volumes, ffmpeg)

        # Clean up WAV files regardless of mix result
        for wav in category_wavs.values():
            try:
                os.remove(wav)
            except OSError:
                pass

        # Clean up mic temp file
        if mic_wav_path and os.path.exists(mic_wav_path):
            try:
                os.remove(mic_wav_path)
            except OSError:
                pass

        if not ok:
            print('[MultiAudio] Mix failed')
        if ok:
            self._apply_watermark(clip_path, ffmpeg)

    def _apply_watermark(self, clip_path: str, ffmpeg: str) -> None:
        if not self.settings_manager.get('watermark_enabled', False):
            return
        text = self.settings_manager.get('watermark_text', 'FTHR') or 'FTHR'
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(
            suffix='.mp4',
            dir=os.path.dirname(clip_path),
            delete=False,
        )
        tmp_path = tmp.name
        tmp.close()
        try:
            result = subprocess.run(
                [
                    ffmpeg, '-y',
                    '-i', clip_path,
                    '-vf', (
                        f"drawtext=text='{text}'"
                        ":fontsize=28:fontcolor=white@0.5"
                        ":x=w-tw-16:y=h-th-16"
                    ),
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
                    '-c:a', 'copy',
                    tmp_path,
                ],
                capture_output=True,
                **_NO_WINDOW,
            )
            if result.returncode == 0:
                os.replace(tmp_path, clip_path)
                print(f'[Watermark] Applied to {os.path.basename(clip_path)}')
            else:
                err = result.stderr.decode(errors='replace').strip().splitlines()
                print(f'[Watermark] ffmpeg failed: {err[-1] if err else "(no stderr)"}')
        except Exception as e:
            print(f'[Watermark] Error: {e}')
        finally:
            try:
                os.remove(tmp_path)
            except FileNotFoundError:
                pass

    def _finalize_clip(self, clip_path: str, duration_seconds: int):
        if not self.settings_manager.get('watermark_enabled', False):
            return
        threading.Thread(
            target=self._finalize_clip_worker,
            args=(clip_path, duration_seconds),
            daemon=True,
        ).start()

    def _finalize_clip_worker(self, clip_path: str, duration_seconds: int):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (ImportError, RuntimeError):
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
            print(f'[Finalize] Clip did not stabilize — skipping watermark')
            return
        self._apply_watermark(clip_path, ffmpeg)

    # =======================================================================
    # UI state
    # =======================================================================

    def _update_status(self):
        if not self.bridge or not self.bridge.is_connected():
            self._reconnect_counter = getattr(self, '_reconnect_counter', 0) + 1
            if self._reconnect_counter % 3 == 1:
                if self.bridge:
                    self.bridge.shutdown()
                self.bridge = CaptureBridge()
                if self.bridge.initialize():
                    print("Reconnected to capture engine.")
                    self._set_status('CAPTURING', STATUS_ACTIVE)
                    self._set_rec_dot_state('capturing')
                    self._reconnect_counter = 0
                    QTimer.singleShot(2000, self._check_hardware_encoding_status)
                    return
            self._set_status('CONNECTING', STATUS_IDLE)
            self._set_rec_dot_state('disconnected')
            return

        status = self.bridge.get_status()
        if not status.get('connected'):
            self._set_status('DISCONNECTED', STATUS_WARNING)
            self._set_rec_dot_state('disconnected')
            return
        codec = self.bridge.get_active_codec()
        if codec:
            preset = self.bridge.get_active_preset()
            new_enc_text = f'{codec} — P{preset}'
            lbl = self._settings_page_widget.active_encoder_lbl
            if lbl.text() != new_enc_text:
                lbl.setText(new_enc_text)
        if self.is_capturing:
            frames   = status.get('frames_captured', 0)
            new_text = f'CAPTURING  {frames:,}f'
            if self.status_label.text() != new_text:
                self.status_label.setText(new_text)
                # setStyleSheet triggers a full re-style of the label and is
                # expensive (~1ms). Only call it on actual style transitions —
                # the frame count text updates twice/sec but the style stays
                # STATUS_ACTIVE the whole time we're capturing.
                if getattr(self, '_last_status_style', None) is not STATUS_ACTIVE:
                    self.status_label.setStyleSheet(STATUS_ACTIVE)
                    self._last_status_style = STATUS_ACTIVE
            self._set_rec_dot_state('capturing')

    def _set_status(self, text: str, style: str):
        self.status_label.setText(text)
        # Skip the QSS reapply when the style didn't change (CONNECTING ticks
        # every 500ms during reconnect would otherwise re-style on every tick).
        if getattr(self, '_last_status_style', None) is not style:
            self.status_label.setStyleSheet(style)
            self._last_status_style = style

    def _on_clip_opened(self, clip_path: str, thumb_pixmap: QPixmap, card_global_rect: QRect):
        from ui.clip_viewer import ClipViewer
        upload_on = self.settings_manager.get('upload_enabled', False)
        viewer = ClipViewer(clip_path, self.bridge, self, thumb_pixmap=thumb_pixmap,
                            settings_manager=self.settings_manager,
                            upload_enabled=upload_on)
        viewer.upload_requested.connect(self.upload_manager.enqueue_upload)
        viewer.showMaximized()
        viewer.exec()

    # =======================================================================
    # Hardware encoding detection
    # =======================================================================

    def _check_hardware_encoding_status(self):
        if not self.bridge or not self.bridge.is_connected():
            return
        try:
            nvenc_active = self.bridge._layout.nvenc_active
        except Exception:
            nvenc_active = False

        self._encoder_type = 'NVENC' if nvenc_active else 'x264'

        if not nvenc_active:
            self.hw_error_bar.set_message(
                'Hardware encoding unavailable — using software encoder (slower)')
            self.hw_error_bar.setVisible(True)
            print("[UI] Hardware encoding not available")
        else:
            self.hw_error_bar.setVisible(False)
            print("[UI] Hardware encoding active (NVENC)")

    def _on_retry_hardware_encoding(self):
        print("[UI] Retry hardware encoding requested")
        self.hw_error_bar.setVisible(False)
        self._restart_capture_engine()
        QTimer.singleShot(3000, self._check_hardware_encoding_status)

    # =======================================================================
    # Styles
    # =======================================================================

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

    def _apply_styles(self):
        self.setStyleSheet(f'''
            /* ── Window canvas ── */
            QMainWindow {{ background-color: {Colors.BG}; }}
            QWidget {{
                background-color: {Colors.BG};
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
            }}

            /* ── Unified top bar ── */
            QFrame#topBar {{
                background-color: {Colors.SHELL_BG};
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QFrame#topBar QLabel {{
                background-color: transparent;
                color: {Colors.TEXT};
            }}
            QFrame#topClusterMain,
            QFrame#topClusterSettings {{
                background: transparent;
                border: none;
            }}
            QLabel#statusLabel {{ background: transparent; }}

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

            /* Min / close — flat against the dark bar */
            QPushButton#winBtn {{
                background-color: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton#winBtn:hover {{
                background-color: {Colors.SURFACE_3};
                color: {Colors.TEXT};
            }}
            QPushButton#closeBtn {{
                background-color: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: 12px;
                font-weight: bold;
            }}
            QPushButton#closeBtn:hover {{
                background-color: {Colors.ERROR};
                color: {Colors.TEXT};
            }}

            {scrollbar_qss()}

            {tooltip_qss()}
        ''')

    def _load_logo(self):
        """Load the top-bar logo, preferring a custom theme override."""
        theme = ThemeManager()
        custom = theme.get_custom_icon_path('fthr_logo.png')
        if custom and custom.exists():
            pix = QPixmap(str(custom))
            self._logo_label.setPixmap(
                pix.scaledToHeight(28, Qt.TransformationMode.SmoothTransformation))
            return
        logo_path = Path(__file__).parent / 'assets' / 'fthr_logo.png'
        if logo_path.exists():
            pix = _load_logo_inverted(logo_path)
            self._logo_label.setPixmap(
                pix.scaledToHeight(28, Qt.TransformationMode.SmoothTransformation))
        else:
            self._logo_label.setText('FTHR')
            self._logo_label.setStyleSheet(
                label_display(Colors.TEXT, Fonts.SIZE_H2, Fonts.TRACK_HEADING))

    def _apply_theme(self):
        """Rebuild the main window stylesheet from current Colors class values.
        Called by the Customize page after the user clicks Apply Theme."""
        self._apply_styles()
        _refresh_all_icons()
        self._load_logo()
        if hasattr(self, 'clip_grid'):
            self.clip_grid.refresh_theme()
        self.update()
        QApplication.processEvents()

    # =======================================================================
    # Shutdown
    # =======================================================================

    def closeEvent(self, event):
        self.status_timer.stop()
        self.hotkey_manager.cleanup()
        try:
            if MicRecorder.is_available():
                MicRecorder().stop()
        except Exception:
            pass
        self.upload_manager.stop()
        self.capture_card.close()
        self.stop_engine()
        event.accept()


# ---------------------------------------------------------------------------
# Settings page — shared layout helpers
# ---------------------------------------------------------------------------

def _flat_section_header(title: str) -> QWidget:
    """Accent uppercase label with a thin extending line to the right."""
    row = QWidget()
    row.setStyleSheet('background: transparent;')
    hl = QHBoxLayout(row)
    hl.setContentsMargins(0, 0, 0, 0)
    hl.setSpacing(10)
    lbl = QLabel(title.upper())
    lbl.setStyleSheet(
        f'color: {Colors.ACCENT}; font-size: {Fonts.SIZE_BODY_L}px; font-weight: 700;'
        f' letter-spacing: 2px; background: transparent; border: none;'
        f' font-family: {Fonts.DISPLAY};'
    )
    hl.addWidget(lbl)
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f'background: {Colors.SHELL_DIVIDER}; border: none;')
    hl.addWidget(line, 1)
    return row


def _settings_hsep() -> QFrame:
    """Thin horizontal divider between settings sections."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f'background: {Colors.SHELL_DIVIDER}; border: none;')
    return line


def _settings_vsep() -> QFrame:
    """Thin vertical divider between settings columns."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedWidth(1)
    line.setStyleSheet(f'background: {Colors.SHELL_DIVIDER}; border: none;')
    return line


# ---------------------------------------------------------------------------
# Sliding stacked widget — gives the settings page its swipe animation
# ---------------------------------------------------------------------------

class SlidingStackedWidget(QWidget):
    """
    Drop-in replacement for QStackedWidget that slides pages horizontally
    when the index changes. Forward = slides left, back = slides right.
    Rapid clicks queue to the most recent target so the UI never desyncs.
    """

    _DURATION = 260   # ms

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._pages = []
        self._current = -1
        self._animating = False
        self._pending = None
        self._anim_group = None

    # API mirrors QStackedWidget ------------------------------------------------

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

    def setCurrentIndex(self, new_idx):
        if new_idx < 0 or new_idx >= len(self._pages):
            return
        if new_idx == self._current and not self._animating:
            return
        if self._animating:
            self._pending = new_idx
            return
        self._slide(new_idx)

    def currentIndex(self):
        return self._current

    def currentWidget(self):
        if 0 <= self._current < len(self._pages):
            return self._pages[self._current]
        return None

    def widget(self, idx):
        if 0 <= idx < len(self._pages):
            return self._pages[idx]
        return None

    def count(self):
        return len(self._pages)

    # Sizing --------------------------------------------------------------------

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
        super().resizeEvent(event)
        if not self._animating and self._current >= 0:
            w, h = self.width(), self.height()
            self._pages[self._current].setGeometry(0, 0, w, h)

    # Animation -----------------------------------------------------------------

    def _slide(self, new_idx):
        old_idx = self._current
        direction = 1 if new_idx > old_idx else -1   # 1 = new from right
        w, h = self.width(), self.height()

        old_page = self._pages[old_idx]
        new_page = self._pages[new_idx]

        new_page.setGeometry(direction * w, 0, w, h)
        new_page.show()
        new_page.raise_()

        self._current = new_idx
        self._animating = True

        anim_out = QPropertyAnimation(old_page, b'geometry', self)
        anim_out.setDuration(self._DURATION)
        anim_out.setStartValue(QRect(0, 0, w, h))
        anim_out.setEndValue(QRect(-direction * w, 0, w, h))
        anim_out.setEasingCurve(QEasingCurve.Type.OutCubic)

        anim_in = QPropertyAnimation(new_page, b'geometry', self)
        anim_in.setDuration(self._DURATION)
        anim_in.setStartValue(QRect(direction * w, 0, w, h))
        anim_in.setEndValue(QRect(0, 0, w, h))
        anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        group = QParallelAnimationGroup(self)
        group.addAnimation(anim_out)
        group.addAnimation(anim_in)
        group.finished.connect(self._finish)
        self._anim_group = group
        group.start()

    def _finish(self):
        w, h = self.width(), self.height()
        for i, page in enumerate(self._pages):
            if i != self._current:
                page.hide()
                page.setGeometry(0, 0, w, h)
        self._anim_group = None
        self._animating = False
        if self._pending is not None:
            nxt = self._pending
            self._pending = None
            if nxt == self._current:
                self._pages[self._current].show()
            else:
                self._slide(nxt)


# ---------------------------------------------------------------------------
# Full-screen settings page (embedded in main content stack)
# ---------------------------------------------------------------------------

class _SettingsPage(QWidget):
    close_requested           = pyqtSignal()
    imported_folders_changed  = pyqtSignal()
    notification_monitor_changed = pyqtSignal()
    encoder_config_changed    = pyqtSignal()

    _AUTOSTART_KEY  = r'Software\Microsoft\Windows\CurrentVersion\Run'
    _AUTOSTART_NAME = 'FTHRClips'

    def _init_autostart_checkbox(self):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._AUTOSTART_KEY)
            winreg.QueryValueEx(key, self._AUTOSTART_NAME)
            winreg.CloseKey(key)
            self.autostart_check.setChecked(True)
        except (FileNotFoundError, OSError):
            self.autostart_check.setChecked(False)

    def _on_autostart_changed(self, state):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._AUTOSTART_KEY,
                                 0, winreg.KEY_SET_VALUE)
            if state == 2:  # Qt.CheckState.Checked
                exe = sys.executable
                winreg.SetValueEx(key, self._AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(key, self._AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
            winreg.CloseKey(key)
        except OSError:
            pass

    def __init__(self, settings_manager: SettingsManager = None, parent=None):
        super().__init__(parent)
        self.sm = settings_manager
        self.setObjectName('settingsPage')
        self._loopback_stream = None
        self._presets_mgr = PresetsManager()
        self._setup_ui()
        self._apply_styles()
        self._load_audio_settings()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Top category tab bar ──────────────────────────────────────────
        tab_bar = QFrame()
        tab_bar.setObjectName('settingsTabBar')
        tab_layout = QHBoxLayout(tab_bar)
        tab_layout.setContentsMargins(24, 0, 24, 0)
        tab_layout.setSpacing(0)
        tab_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        self._cat_titles = ['General', 'Clip', 'Audio', 'Visuals', 'Customize', 'Performance', 'Version & Updates']
        _tab_icons = [
            'settings(general).png',
            'clip.png',
            'sound.png',
            'visuals.png',
            'personalize.png',
            'performance.png',
            'updates.png',
        ]

        self._tab_buttons: list = []
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)

        for i, (title, icon_file) in enumerate(zip(self._cat_titles, _tab_icons)):
            btn = QToolButton()
            btn.setText(title.upper())
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

        # ── Content area ─────────────────────────────────────────────────
        content = QWidget()
        content.setObjectName('settingsContent')
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(36, 22, 36, 22)
        content_layout.setSpacing(0)

        self.stack = SlidingStackedWidget()
        self.stack.addWidget(self._make_general_page())     # 0: General (includes upload)
        self.stack.addWidget(self._make_clip_page())        # 1: Clip
        self.stack.addWidget(self._make_audio_page())       # 2: Audio
        self.stack.addWidget(self._make_visuals_page())     # 3: Visuals
        self._customize_page = CustomizePage()
        self._customize_page.theme_applied.connect(self._on_theme_applied)
        self.stack.addWidget(self._customize_page)          # 4: Customize
        self.stack.addWidget(self._make_performance_page()) # 5: Performance
        self.stack.addWidget(self._make_version_page())     # 6: Version & Updates
        content_layout.addWidget(self.stack, stretch=1)

        main_layout.addWidget(content, stretch=1)

        # Select first tab
        self._tab_buttons[0].setChecked(True)

    def _on_category_changed(self, idx):
        self.stack.setCurrentIndex(idx)
        # Audio sub-page is index 2 — start the live meter only there
        if hasattr(self, 'mic_level_meter'):
            if idx == 2 and self.isVisible():
                self.mic_level_meter.set_gain(self.mic_vol_slider.value() / 100.0)
                self.mic_level_meter.start(self._selected_mic_index())
            else:
                self.mic_level_meter.stop()
                self._stop_loopback()
                if hasattr(self, 'mic_loopback_check'):
                    self.mic_loopback_check.blockSignals(True)
                    self.mic_loopback_check.setChecked(False)
                    self.mic_loopback_check.blockSignals(False)

    def _make_general_page(self):
        from ui.upload_settings_widget import UploadSettingsWidget

        page = QWidget()
        page.setStyleSheet('background: transparent;')
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, 16, 32)
        layout.setSpacing(0)

        # ── System ────────────────────────────────────────────────────────
        layout.addWidget(_flat_section_header('System'))
        layout.addSpacing(12)
        self.autostart_check = QCheckBox('Autostart with Windows')
        if sys.platform != 'win32':
            self.autostart_check.setVisible(False)
        layout.addWidget(self.autostart_check)
        if sys.platform == 'win32':
            self._init_autostart_checkbox()
        if sys.platform == 'win32':
            self.autostart_check.stateChanged.connect(self._on_autostart_changed)

        # ── Game Detection ────────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Game Detection'))
        layout.addSpacing(12)

        self.game_detection_check = QCheckBox('Games erkennen und zum Aufnehmen vorschlagen')
        self.game_detection_check.setStyleSheet(CHECKBOX_QSS)
        self.game_detection_check.setChecked(self.sm.get('game_detection_enabled', False))
        self.game_detection_check.toggled.connect(self._on_game_detection_toggled)
        layout.addWidget(self.game_detection_check)
        layout.addSpacing(4)

        _gd_hint = QLabel('Drücke F8 um aufzunehmen wenn ein Game erkannt wird.')
        _gd_hint.setWordWrap(True)
        _gd_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_gd_hint)

        # ── Import Clips ──────────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Import Clips'))
        layout.addSpacing(10)

        desc = QLabel(
            'Add folders from other clipping software so their clips '
            'appear alongside your FTHR recordings.'
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(desc)
        layout.addSpacing(12)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        add_btn = QPushButton('ADD FOLDER')
        add_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        add_btn.clicked.connect(self._on_import_folder_add)
        btn_row.addWidget(add_btn)

        self._scan_btn = QPushButton('SCAN FOR CLIPS')
        self._scan_btn.setStyleSheet(BUTTON_SECONDARY_QSS)
        self._scan_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._scan_btn.clicked.connect(self._on_import_folder_scan)
        btn_row.addWidget(self._scan_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)
        layout.addSpacing(12)

        # Added-folders list
        self._import_folders_container = QWidget()
        self._import_folders_container.setStyleSheet('background: transparent;')
        self._import_folders_layout = QVBoxLayout(self._import_folders_container)
        self._import_folders_layout.setContentsMargins(0, 0, 0, 0)
        self._import_folders_layout.setSpacing(4)
        layout.addWidget(self._import_folders_container)

        # Scan results (hidden until scan runs)
        layout.addSpacing(4)
        self._scan_results_container = QWidget()
        self._scan_results_container.setVisible(False)
        self._scan_results_container.setStyleSheet('background: transparent;')
        self._scan_results_layout = QVBoxLayout(self._scan_results_container)
        self._scan_results_layout.setContentsMargins(0, 0, 0, 0)
        self._scan_results_layout.setSpacing(4)
        layout.addWidget(self._scan_results_container)

        # ── Upload ────────────────────────────────────────────────────────
        layout.addSpacing(28)
        self._upload_settings_widget = UploadSettingsWidget(self.sm, no_scroll=True)
        layout.addWidget(self._upload_settings_widget)

        # ── Settings Presets ──────────────────────────────────────────────
        layout.addSpacing(28)
        layout.addWidget(_flat_section_header('Settings-Presets'))
        layout.addSpacing(12)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)

        self.preset_combo = _DropdownCombo()
        self.preset_combo.setStyleSheet(_COMBO_STYLE)
        self.preset_combo.setMinimumWidth(160)
        self._refresh_preset_combo()
        preset_row.addWidget(self.preset_combo, 1)

        load_btn = QPushButton('LADEN')
        load_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        load_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        load_btn.clicked.connect(self._on_preset_load)
        preset_row.addWidget(load_btn)

        save_btn = QPushButton('SPEICHERN')
        save_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        save_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        save_btn.clicked.connect(self._on_preset_save)
        preset_row.addWidget(save_btn)

        del_btn = QPushButton('LÖSCHEN')
        del_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        del_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        del_btn.clicked.connect(self._on_preset_delete)
        preset_row.addWidget(del_btn)

        layout.addLayout(preset_row)

        layout.addStretch()

        # Wrap everything in a scroll area so the combined content fits
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(scrollbar_qss())
        scroll.setWidget(page)

        wrapper = QWidget()
        wrapper.setStyleSheet('background: transparent;')
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        wl.addWidget(scroll)

        self._scan_pending: list[tuple[str, str]] = []
        self._refresh_import_folders_list()
        return wrapper

    # ── Import Clips helpers ──────────────────────────────────────────────

    def _refresh_import_folders_list(self):
        while self._import_folders_layout.count():
            item = self._import_folders_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        folders = self.sm.get('imported_clip_folders', []) if self.sm else []

        if not folders:
            lbl = QLabel('No import folders added yet.')
            lbl.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
            self._import_folders_layout.addWidget(lbl)
            return

        for folder in folders:
            self._import_folders_layout.addWidget(self._make_import_folder_row(folder))

    def _make_import_folder_row(self, path: str) -> QFrame:
        row = QFrame()
        row.setStyleSheet(
            f'QFrame {{ background: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER}; }}'
        )
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 6, 6, 6)
        rl.setSpacing(8)

        path_lbl = QLabel(path)
        path_lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        path_lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        rl.addWidget(path_lbl, 1)

        remove_btn = QPushButton('×')
        remove_btn.setFixedSize(22, 22)
        remove_btn.setToolTip('Remove this folder')
        remove_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        remove_btn.setStyleSheet(
            f'QPushButton {{ background: transparent;'
            f' border: 1px solid {Colors.BORDER}; color: {Colors.TEXT_DIM};'
            f' font-size: 14px; font-weight: bold; }}'
            f' QPushButton:hover {{ border-color: {Colors.ERROR}; color: {Colors.ERROR}; }}'
        )
        remove_btn.clicked.connect(lambda _, p=path: self._remove_import_folder(p))
        rl.addWidget(remove_btn)
        return row

    def _remove_import_folder(self, path: str):
        if self.sm is None:
            return
        folders = list(self.sm.get('imported_clip_folders', []))
        if path in folders:
            folders.remove(path)
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
        folders = list(self.sm.get('imported_clip_folders', []))
        if folder not in folders:
            folders.append(folder)
            self.sm.set('imported_clip_folders', folders)
            self.sm.save_settings()
        self._refresh_import_folders_list()
        # Remove from scan results if it was pending there
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != folder]
        self._rebuild_scan_results()
        self.imported_folders_changed.emit()

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
        existing = set(self.sm.get('imported_clip_folders', []) if self.sm else [])
        found = [
            (name, str(path))
            for name, path in _known
            if path.exists() and str(path) not in existing
        ]

        # Clear scan results area for fresh output
        while self._scan_results_layout.count():
            item = self._scan_results_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not found:
            lbl = QLabel('No clip folders from other software were found on your system.')
            lbl.setWordWrap(True)
            lbl.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY))
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
        row.setStyleSheet(
            f'QFrame {{ background: {Colors.SURFACE_1};'
            f' border: 1px solid {Colors.BORDER}; }}'
        )
        rl = QHBoxLayout(row)
        rl.setContentsMargins(10, 8, 8, 8)
        rl.setSpacing(10)

        info = QVBoxLayout()
        info.setSpacing(1)
        name_lbl = QLabel(software_name)
        name_lbl.setStyleSheet(
            f'color: {Colors.TEXT}; font-size: {Fonts.SIZE_BODY}px;'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' background: transparent;'
        )
        path_lbl = QLabel(path)
        path_lbl.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_LABEL))
        info.addWidget(name_lbl)
        info.addWidget(path_lbl)
        rl.addLayout(info, 1)

        add_btn = QPushButton('ADD')
        add_btn.setFixedWidth(60)
        add_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        add_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        add_btn.clicked.connect(lambda _, n=software_name, p=path: self._scan_add(n, p))
        rl.addWidget(add_btn)

        dismiss_btn = QPushButton('DISMISS')
        dismiss_btn.setFixedWidth(76)
        dismiss_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        dismiss_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        dismiss_btn.clicked.connect(lambda _, n=software_name, p=path: self._scan_dismiss(n, p))
        rl.addWidget(dismiss_btn)
        return row

    def _scan_add(self, software_name: str, path: str):
        if self.sm is not None:
            folders = list(self.sm.get('imported_clip_folders', []))
            if path not in folders:
                folders.append(path)
                self.sm.set('imported_clip_folders', folders)
                self.sm.save_settings()
            self._refresh_import_folders_list()
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != path]
        self._rebuild_scan_results()
        self.imported_folders_changed.emit()

    def _scan_dismiss(self, software_name: str, path: str):
        self._scan_pending = [(n, p) for n, p in self._scan_pending if p != path]
        self._rebuild_scan_results()

    def _make_clip_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_flat_section_header('Clip Settings'))
        layout.addSpacing(12)
        hint = QLabel(
            'Clip length, frame rate, resolution and bitrate are\n'
            'configured via the top bar capture button (▶ CAP).'
        )
        hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(hint)
        layout.addStretch()
        return page

    def _make_audio_page(self):
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        cols = QHBoxLayout()
        cols.setContentsMargins(0, 0, 0, 0)
        cols.setSpacing(0)

        # ── Left: Microphone ──────────────────────────────────────────────
        left = QWidget()
        left.setStyleSheet('background: transparent;')
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 28, 0)
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
            refresh.setText('↻')
        refresh.clicked.connect(self._populate_mic_devices)
        dev_row.addWidget(refresh)
        left_layout.addLayout(dev_row)
        left_layout.addSpacing(8)

        # Volume row
        vol_row = QHBoxLayout()
        vol_row.setSpacing(8)
        vol_lbl = QLabel('Loudness')
        vol_lbl.setFixedWidth(100)
        vol_row.addWidget(vol_lbl)
        self.mic_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.mic_vol_slider.setRange(0, 200)
        self.mic_vol_slider.setValue(100)
        vol_row.addWidget(self.mic_vol_slider, 1)
        self.mic_vol_value = QLabel('100%')
        self.mic_vol_value.setFixedWidth(42)
        self.mic_vol_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.mic_vol_slider.valueChanged.connect(self._on_mic_volume_changed)
        vol_row.addWidget(self.mic_vol_value)
        left_layout.addLayout(vol_row)
        left_layout.addSpacing(8)

        # Live level meter row
        meter_row = QHBoxLayout()
        meter_row.setSpacing(8)
        meter_lbl = QLabel('Live level')
        meter_lbl.setFixedWidth(100)
        meter_row.addWidget(meter_lbl)
        self.mic_level_meter = _MicLevelMeter()
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

        if not _SD_AVAILABLE:
            warn = QLabel(
                '⚠  Mic features need the "sounddevice" Python package.\n'
                '   Install it from your venv:  pip install sounddevice numpy')
            warn.setStyleSheet(label_body(Colors.ERROR, Fonts.SIZE_BODY))
            left_layout.addSpacing(6)
            left_layout.addWidget(warn)

        left_layout.addStretch()
        cols.addWidget(left, 1)

        cols.addWidget(_settings_vsep())

        # ── Right: FTHR Sounds ────────────────────────────────────────────
        right = QWidget()
        right.setStyleSheet('background: transparent;')
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(28, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        right_layout.addWidget(_flat_section_header('FTHR Sounds'))
        right_layout.addSpacing(12)

        self.sound_sliders = {}
        for label_text, key in [
            ('Clip Captured',       'clip'),
            ('Screenshot Captured', 'screenshot'),
            ('Error Sound',         'error'),
            ('Startup Sound',       'startup'),
        ]:
            row = QHBoxLayout()
            row.setSpacing(8)
            lbl = QLabel(f'{label_text}:')
            lbl.setFixedWidth(150)
            row.addWidget(lbl)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(100)
            self.sound_sliders[key] = slider
            row.addWidget(slider, 1)
            val_lbl = QLabel('100%')
            val_lbl.setFixedWidth(38)
            val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            slider.valueChanged.connect(lambda v, l=val_lbl: l.setText(f'{v}%'))
            row.addWidget(val_lbl)
            right_layout.addLayout(row)
            right_layout.addSpacing(8)

        right_layout.addSpacing(20)
        right_layout.addWidget(_flat_section_header('Notifications'))
        right_layout.addSpacing(12)

        mon_row = QHBoxLayout()
        mon_row.setSpacing(8)
        mon_lbl = QLabel('Monitor:')
        mon_lbl.setFixedWidth(150)
        mon_row.addWidget(mon_lbl)
        self.notif_monitor_combo = _DropdownCombo()
        self.notif_monitor_combo.addItem('Auto (höchste Hz)', userData='auto')
        for s in QApplication.screens():
            g = s.availableGeometry()
            self.notif_monitor_combo.addItem(
                f'{s.name()}  ({g.width()}×{g.height()} @ {int(s.refreshRate())}Hz)',
                userData=s.name(),
            )
        self.notif_monitor_combo.currentIndexChanged.connect(self._on_notif_monitor_changed)
        mon_row.addWidget(self.notif_monitor_combo, 1)
        right_layout.addLayout(mon_row)

        right_layout.addStretch()
        cols.addWidget(right, 1)

        outer.addLayout(cols)

        # Mic enumeration was synchronous here, which made the entire
        # settings page wait on sounddevice.query_devices(). On systems with
        # many audio devices this added 100–400 ms to the first open. We
        # populate a placeholder now and run the real scan on the next event
        # loop tick so widget construction stays I/O-free.
        self.mic_combo.addItem('System Default', userData=None)
        QTimer.singleShot(0, self._populate_mic_devices)

        # ── Multiband Audio ───────────────────────────────────────────────
        outer.addSpacing(24)
        outer.addWidget(_settings_hsep())
        outer.addSpacing(20)
        outer.addWidget(_flat_section_header('Multiband Audio'))
        outer.addSpacing(8)

        mb_desc = QLabel(
            'Nimmt jede App-Kategorie separat auf und brennt Lautstärke-Presets '
            'beim Clip-Save ein. Deaktiviere für maximale Performance.')
        mb_desc.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        mb_desc.setWordWrap(True)
        outer.addWidget(mb_desc)
        outer.addSpacing(12)

        self.multiband_check = QCheckBox('Multiband Audio aktivieren')
        self.multiband_check.setStyleSheet(CHECKBOX_QSS)
        self.multiband_check.setChecked(self.sm.get('multiband_audio_enabled', False))
        self.multiband_check.toggled.connect(self._on_multiband_toggled)
        outer.addWidget(self.multiband_check)
        outer.addSpacing(12)

        # Category list container — shown only when multiband is enabled
        self.multiband_container = QWidget()
        mb_inner = QVBoxLayout(self.multiband_container)
        mb_inner.setContentsMargins(0, 0, 0, 0)
        mb_inner.setSpacing(6)
        self._cat_rows = []
        self._rebuild_category_rows(mb_inner)
        outer.addWidget(self.multiband_container)
        self.multiband_container.setVisible(self.sm.get('multiband_audio_enabled', False))

        # Recognised app→category label (updated by timer when page is visible)
        self.mappings_lbl = QLabel('Erkannte Apps: —')
        self.mappings_lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.mappings_lbl.setWordWrap(True)
        outer.addWidget(self.mappings_lbl)
        outer.addSpacing(8)

        # + Add category row
        add_row = QHBoxLayout()
        self.new_cat_name = QLineEdit()
        self.new_cat_name.setPlaceholderText('Name (z.B. "Musik")')
        self.new_cat_name.setStyleSheet(COMBO_QSS)
        self.new_cat_patterns = QLineEdit()
        self.new_cat_patterns.setPlaceholderText('Patterns: spotify,Spotify,vlc')
        self.new_cat_patterns.setStyleSheet(COMBO_QSS)
        add_btn = QPushButton('+ Hinzufügen')
        add_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
        add_btn.clicked.connect(self._on_add_category)
        add_row.addWidget(self.new_cat_name, 1)
        add_row.addWidget(self.new_cat_patterns, 2)
        add_row.addWidget(add_btn)
        outer.addLayout(add_row)

        # Timer to refresh mappings label every 2s while page is visible
        self._mappings_timer = QTimer(self)
        self._mappings_timer.setInterval(2000)
        self._mappings_timer.timeout.connect(self._update_mappings_label)

        return page

    # ── Multiband Audio helpers ──────────────────────────────────────────

    def _rebuild_category_rows(self, layout: QVBoxLayout):
        """Clear and repopulate the category volume rows."""
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._cat_rows = []

        for i, cat in enumerate(self.sm.get('audio_categories', [])):
            row_w = QWidget()
            row_h = QHBoxLayout(row_w)
            row_h.setContentsMargins(0, 0, 0, 0)
            row_h.setSpacing(8)

            name_lbl = QLabel(cat['name'])
            name_lbl.setFixedWidth(100)
            name_lbl.setStyleSheet(label_body(Colors.TEXT, Fonts.SIZE_BODY))

            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setStyleSheet(SLIDER_QSS)
            slider.setRange(0, 100)
            slider.setValue(cat.get('volume', 100))

            val_lbl = QLabel(f"{cat.get('volume', 100)}%")
            val_lbl.setFixedWidth(38)
            val_lbl.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            slider.valueChanged.connect(
                lambda v, idx=i, lbl=val_lbl: self._on_cat_volume(idx, v, lbl))

            del_btn = QPushButton('✕')
            del_btn.setFixedSize(24, 24)
            del_btn.setStyleSheet(BUTTON_OUTLINE_QSS)
            del_btn.clicked.connect(lambda _, idx=i: self._on_delete_category(idx))

            row_h.addWidget(name_lbl)
            row_h.addWidget(slider, 1)
            row_h.addWidget(val_lbl)
            row_h.addWidget(del_btn)
            layout.addWidget(row_w)
            self._cat_rows.append((name_lbl, slider, val_lbl, del_btn))

    def _on_multiband_toggled(self, checked: bool):
        self.sm.set('multiband_audio_enabled', checked)
        self.sm.save_settings()
        self.multiband_container.setVisible(checked)
        if checked:
            self._mappings_timer.start()
        else:
            self._mappings_timer.stop()

    def _on_game_detection_toggled(self, checked: bool):
        self.sm.set('game_detection_enabled', checked)
        self.sm.save_settings()
        main_win = self.window()
        if hasattr(main_win, '_game_detector'):
            if checked:
                main_win._game_detector.start()
            else:
                main_win._game_detector.stop()

    def _on_audio_capture_toggled(self, checked: bool):
        self.sm.set('audio_capture_enabled', checked)
        self.sm.save_settings()

    def _refresh_preset_combo(self):
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        names = self._presets_mgr.names()
        if names:
            self.preset_combo.addItems(names)
        else:
            self.preset_combo.addItem('— kein Preset —')
        self.preset_combo.blockSignals(False)

    def _on_preset_save(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, 'Preset speichern', 'Name:',
            text=self.preset_combo.currentText() if self._presets_mgr.names() else '')
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
        for k, v in data.items():
            self.sm.set(k, v)
        self.sm.save_settings()
        main_win = self.window()
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

    def _on_cat_volume(self, idx: int, vol: int, lbl: QLabel):
        lbl.setText(f'{vol}%')
        cats = self.sm.get('audio_categories', [])
        if 0 <= idx < len(cats):
            cats[idx]['volume'] = vol
            self.sm.set('audio_categories', cats)
            self.sm.save_settings()

    def _on_delete_category(self, idx: int):
        cats = self.sm.get('audio_categories', [])
        if 0 <= idx < len(cats):
            cats.pop(idx)
            self.sm.set('audio_categories', cats)
            self.sm.save_settings()
            mb_inner = self.multiband_container.layout()
            self._rebuild_category_rows(mb_inner)

    def _on_add_category(self):
        name = self.new_cat_name.text().strip()
        if not name:
            return
        patterns = [p.strip() for p in self.new_cat_patterns.text().split(',')
                    if p.strip()]
        cats = self.sm.get('audio_categories', [])
        cats.append({'name': name, 'volume': 100, 'patterns': patterns})
        self.sm.set('audio_categories', cats)
        self.sm.save_settings()
        self.new_cat_name.clear()
        self.new_cat_patterns.clear()
        self._rebuild_category_rows(self.multiband_container.layout())

    def _update_mappings_label(self):
        """Refresh the recognised-apps label from the bridge."""
        try:
            # Walk up Qt parent hierarchy: SettingsPage → QStackedWidget → MainWindow
            main_win = self.parent()
            while main_win is not None and not hasattr(main_win, 'bridge'):
                main_win = main_win.parent()
            if main_win is None:
                return
            mappings = main_win.bridge.get_audio_mappings()
            if not mappings:
                self.mappings_lbl.setText('Erkannte Apps: (keine)')
            else:
                parts = [f'{app} → {cat} ✓' for app, cat in mappings.items()]
                self.mappings_lbl.setText('Erkannte Apps: ' + '   '.join(parts))
        except Exception:
            pass

    # ── Microphone helpers ───────────────────────────────────────────────

    def _populate_mic_devices(self):
        """Re-scan input devices and refill the combo."""
        # Capture the *intended* selection: prefer the saved settings name
        # over the combo's current text, so a deferred first run still ends
        # up on the user's previously-chosen device. Manual refresh (button)
        # falls through to the current selection in the combo.
        intended = None
        if self.sm is not None:
            saved = self.sm.get('mic_device_name')
            if saved:
                intended = saved
        if intended is None and self.mic_combo.count():
            intended = self.mic_combo.currentText()

        self.mic_combo.blockSignals(True)
        self.mic_combo.clear()
        self.mic_combo.addItem('System Default', userData=None)
        if _SD_AVAILABLE:
            try:
                for i, dev in enumerate(_sd.query_devices()):
                    if dev.get('max_input_channels', 0) > 0:
                        self.mic_combo.addItem(dev['name'], userData=i)
            except Exception as e:
                print(f'Mic scan failed: {e}')
        if intended:
            idx = self.mic_combo.findText(intended)
            if idx >= 0:
                self.mic_combo.setCurrentIndex(idx)
        self.mic_combo.blockSignals(False)
        try:
            self.mic_combo.currentIndexChanged.disconnect(self._on_mic_device_changed)
        except (TypeError, RuntimeError):
            pass
        self.mic_combo.currentIndexChanged.connect(self._on_mic_device_changed)

    def _load_audio_settings(self):
        if self.sm is None:
            return
        # Block signals so loading values doesn't trigger _save_audio_settings
        # mid-load (which would overwrite later fields with their defaults).
        self.mic_combo.blockSignals(True)
        self.mic_vol_slider.blockSignals(True)
        self.mic_loopback_check.blockSignals(True)
        self.notif_monitor_combo.blockSignals(True)
        try:
            name = self.sm.get('mic_device_name')
            if name:
                idx = self.mic_combo.findText(name)
                if idx >= 0:
                    self.mic_combo.setCurrentIndex(idx)
            vol = int(self.sm.get('mic_volume', 100))
            self.mic_vol_slider.setValue(vol)
            self.mic_vol_value.setText(f'{vol}%')
            self.mic_level_meter.set_gain(vol / 100.0)
            self.mic_loopback_check.setChecked(bool(self.sm.get('mic_loopback', False)))
            saved_mon = self.sm.get('notification_monitor', 'auto')
            idx = self.notif_monitor_combo.findData(saved_mon)
            if idx >= 0:
                self.notif_monitor_combo.setCurrentIndex(idx)
        finally:
            self.mic_combo.blockSignals(False)
            self.mic_vol_slider.blockSignals(False)
            self.mic_loopback_check.blockSignals(False)
            self.notif_monitor_combo.blockSignals(False)

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
        name = self.mic_combo.currentText()
        self.sm.set('mic_device_name', None if name == 'System Default' else name)
        self.sm.set('mic_volume', self.mic_vol_slider.value())
        self.sm.set('mic_loopback', self.mic_loopback_check.isChecked())
        self.sm.save_settings()
        # Push updates to the always-on background recorder so future clips
        # use the newly-selected device and gain.
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
        return self.mic_combo.currentData() if self.mic_combo.count() else None

    def _on_mic_volume_changed(self, v: int):
        self.mic_vol_value.setText(f'{v}%')
        self.mic_level_meter.set_gain(v / 100.0)
        if self._loopback_stream is not None:
            # Volume is read live by the loopback callback closure
            pass
        self._save_audio_settings()

    def _on_mic_device_changed(self, _idx: int):
        idx = self._selected_mic_index()
        # Restart meter on the new device
        self.mic_level_meter.stop()
        if self.isVisible() and self.stack.currentIndex() == 2:
            self.mic_level_meter.set_gain(self.mic_vol_slider.value() / 100.0)
            self.mic_level_meter.start(idx)
        # Restart loopback if currently on
        if self.mic_loopback_check.isChecked():
            self._stop_loopback()
            self._start_loopback(idx)
        self._save_audio_settings()

    def _start_loopback(self, device_index):
        self._stop_loopback()
        if not _SD_AVAILABLE:
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
        # Only run the meter when the audio sub-page is selected
        if hasattr(self, 'stack') and self.stack.currentIndex() == 2:
            self.mic_level_meter.set_gain(self.mic_vol_slider.value() / 100.0)
            self.mic_level_meter.start(self._selected_mic_index())

    def hideEvent(self, event):
        self._stop_loopback()
        if hasattr(self, 'mic_level_meter'):
            self.mic_level_meter.stop()
        super().hideEvent(event)

    def _make_visuals_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(_flat_section_header('Startup'))
        layout.addSpacing(12)

        self.splash_check = QCheckBox('Enable startup splash screen')
        self.splash_check.setChecked(True)
        layout.addWidget(self.splash_check)

        layout.addStretch()
        return page

    def _on_theme_applied(self):
        """Re-apply the full app stylesheet using current theme colors."""
        theme = ThemeManager()
        colors = theme.get_all_colors()
        # Patch the Colors class at runtime so all future QSS references use
        # the custom values. This is a one-time operation per Apply click.
        from ui.style import Colors as C
        for token, value in colors.items():
            if hasattr(C, token):
                setattr(C, token, value)
        # Re-apply styles for this settings page
        self._apply_styles()
        # Refresh the customize page swatches and preview
        self._customize_page._refresh_all()
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

        layout.addWidget(_flat_section_header('Video Encoding'))
        layout.addSpacing(12)

        def _row(label_text, widget):
            row = QHBoxLayout()
            row.setSpacing(10)
            lbl = QLabel(label_text)
            lbl.setFixedWidth(140)
            lbl.setStyleSheet(_LABEL_STYLE)
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            return row

        # Codec
        self.codec_combo = _DropdownCombo()
        self.codec_combo.addItems(['Auto', 'H.264', 'HEVC', 'AV1'])
        self.codec_combo.setStyleSheet(_COMBO_STYLE)
        saved_codec = self.sm.get('codec_pref', 'auto')
        self.codec_combo.setCurrentIndex(
            {'auto': 0, 'h264': 1, 'hevc': 2, 'av1': 3}.get(saved_codec, 0))
        layout.addLayout(_row('CODEC', self.codec_combo))
        layout.addSpacing(8)

        # Preset
        self.preset_combo = _DropdownCombo()
        self.preset_combo.addItems([
            'P1 — Schnellst', 'P2', 'P3', 'P4 — Ausgeglichen',
            'P5', 'P6', 'P7 — Beste Qualität',
        ])
        self.preset_combo.setStyleSheet(_COMBO_STYLE)
        saved_preset = self.sm.get('encoder_preset', 4)
        self.preset_combo.setCurrentIndex(max(0, min(6, saved_preset - 1)))
        layout.addLayout(_row('PRESET', self.preset_combo))
        layout.addSpacing(8)

        # Active encoder (read-only label)
        self.active_encoder_lbl = QLabel('—')
        self.active_encoder_lbl.setStyleSheet(
            label_body(Colors.ACCENT, Fonts.SIZE_BODY))
        layout.addLayout(_row('AKTIVER ENCODER', self.active_encoder_lbl))
        layout.addSpacing(12)

        # Apply button (hidden until user changes something)
        self.encoder_apply_btn = QPushButton('ÜBERNEHMEN')
        self.encoder_apply_btn.setStyleSheet(BUTTON_PRIMARY_QSS)
        self.encoder_apply_btn.setVisible(False)
        self.encoder_apply_btn.clicked.connect(self._on_encoder_apply)
        layout.addWidget(self.encoder_apply_btn)

        self.codec_combo.currentIndexChanged.connect(self._on_encoder_setting_changed)
        self.preset_combo.currentIndexChanged.connect(self._on_encoder_setting_changed)

        enc_note = QLabel('Anwenden unterbricht die Aufnahme kurz und löscht den aktuellen Puffer.')
        enc_note.setStyleSheet(label_body(Colors.TEXT_DIM,
            Fonts.SIZE_SMALL if hasattr(Fonts, 'SIZE_SMALL') else Fonts.SIZE_BODY))
        layout.addWidget(enc_note)

        layout.addSpacing(28)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addWidget(_flat_section_header('Capture Buffer'))
        layout.addSpacing(12)

        buf_note = QLabel(
            'Buffer length is set via the top bar capture button.\n'
            'A larger buffer uses more RAM but lets you save longer clips.'
        )
        buf_note.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(buf_note)

        layout.addSpacing(28)
        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addWidget(_flat_section_header('Audio Capture'))
        layout.addSpacing(12)

        self.audio_capture_check = QCheckBox('Audio aufnehmen')
        self.audio_capture_check.setStyleSheet(CHECKBOX_QSS)
        self.audio_capture_check.setChecked(self.sm.get('audio_capture_enabled', True))
        self.audio_capture_check.toggled.connect(self._on_audio_capture_toggled)
        layout.addWidget(self.audio_capture_check)
        layout.addSpacing(4)

        _ac_hint = QLabel('Deaktivieren spart CPU. Änderung gilt beim nächsten Engine-Neustart.')
        _ac_hint.setWordWrap(True)
        _ac_hint.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(_ac_hint)

        layout.addStretch()
        return page

    def _on_encoder_setting_changed(self, _idx: int):
        self.encoder_apply_btn.setVisible(True)

    def _on_encoder_apply(self):
        codec_map = {0: 'auto', 1: 'h264', 2: 'hevc', 3: 'av1'}
        codec  = codec_map.get(self.codec_combo.currentIndex(), 'auto')
        preset = self.preset_combo.currentIndex() + 1   # 0-indexed combo → 1-7
        self.sm.set('codec_pref',     codec)
        self.sm.set('encoder_preset', preset)
        self.sm.save_settings()
        self.encoder_apply_btn.setVisible(False)
        self.encoder_config_changed.emit()

    def _make_version_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(_flat_section_header('Current Installation'))
        layout.addSpacing(12)

        ver_lbl = QLabel('FTHR Clips v1.0.0')
        ver_lbl.setStyleSheet(label_display(Colors.TEXT, Fonts.SIZE_H3, 3))
        layout.addWidget(ver_lbl)
        layout.addSpacing(4)

        build_lbl = QLabel('Build: 2026.04.28')
        build_lbl.setStyleSheet(label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(build_lbl)
        layout.addSpacing(24)

        layout.addWidget(_settings_hsep())
        layout.addSpacing(20)

        layout.addWidget(_flat_section_header('Available Versions'))
        layout.addSpacing(12)

        latest_lbl = QLabel('You are on the latest version.')
        latest_lbl.setStyleSheet(label_body(Colors.ACCENT, Fonts.SIZE_BODY_L))
        layout.addWidget(latest_lbl)

        layout.addStretch()
        return page

    def _apply_styles(self):
        self.setStyleSheet(f'''
            QWidget#settingsPage    {{ background-color: {Colors.BG}; }}
            QWidget#settingsContent {{ background-color: {Colors.BG}; }}

            QFrame#settingsTabBar {{
                background-color: {Colors.SHELL_BG};
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
                font-weight: bold;
                padding: 14px 20px 10px 20px;
                min-width: 72px;
            }}
            QToolButton#settingsTabBtn:hover {{
                background-color: {Colors.SURFACE_3};
                color: {Colors.TEXT};
            }}
            QToolButton#settingsTabBtn:checked {{
                color: {Colors.ACCENT};
                border-bottom: 2px solid {Colors.ACCENT};
                background-color: transparent;
            }}

            QPushButton#micRefreshBtn {{
                background-color: {Colors.SURFACE_2};
                border: {Sizes.BORDER_W}px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT};
                font-size: 14px;
                font-weight: bold;
            }}
            QPushButton#micRefreshBtn:hover {{
                border-color: {Colors.ACCENT};
                color: {Colors.ACCENT};
            }}

            {CHECKBOX_QSS}
            {COMBO_QSS}
            {SLIDER_QSS}

            QLabel {{
                color: {Colors.TEXT};
                font-size: {Fonts.SIZE_BODY_L}px;
                font-family: {Fonts.BODY};
                background: transparent;
            }}

            /* ── Accordion sections (Customize tab) ── */
            QFrame#accordionSection {{
                background-color: {Colors.SURFACE_1};
                border: 1px solid {Colors.BORDER};
            }}
            QPushButton#accordionHeader {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
                text-align: left;
            }}
            QPushButton#accordionHeader:hover {{
                background-color: {Colors.SURFACE_3};
            }}
            QWidget#accordionBody {{
                background-color: {Colors.SURFACE_1};
            }}
            QFrame#colorPreview {{
                background-color: {Colors.BG};
                border: 1px solid {Colors.BORDER};
            }}
        ''')


# ---------------------------------------------------------------------------
# Hardware error bar (bottom of window)
# ---------------------------------------------------------------------------

class _HWErrorBar(QFrame):
    retry_clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(Sizes.HW_BAR_H)
        self.setStyleSheet(f'''
            QFrame {{
                background-color: {Colors.SURFACE_1};
                border-top: 1px solid {Colors.ERROR};
            }}
        ''')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(Sizes.SPACE_6, 0, Sizes.SPACE_6, 0)
        layout.setSpacing(12)

        self._msg = QLabel()
        self._msg.setStyleSheet(
            f'color: {Colors.ERROR}; font-size: {Fonts.SIZE_BODY}px;'
            f' letter-spacing: 1px;'
            f' font-family: {Fonts.BODY}; background: transparent;'
        )
        layout.addWidget(self._msg)
        layout.addStretch()

        retry = QPushButton('RETRY')
        retry.setStyleSheet(f'''
            QPushButton {{
                background-color: transparent;
                border: {Sizes.BORDER_W}px solid {Colors.ERROR};
                color: {Colors.ERROR};
                font-size: {Fonts.SIZE_LABEL}px;
                font-family: {Fonts.DISPLAY};
                letter-spacing: {Fonts.TRACK_LABEL}px;
                font-weight: bold;
                padding: 4px 14px;
            }}
            QPushButton:hover {{
                background-color: {Colors.ERROR};
                color: {Colors.TEXT};
            }}
        ''')
        retry.clicked.connect(self.retry_clicked)
        layout.addWidget(retry)

    def set_message(self, text: str):
        self._msg.setText(text)


# ===========================================================================
# Entry point
# ===========================================================================

def _load_fonts():
    """Register bundled fonts with Qt. Falls back silently if files missing."""
    fonts_dir = Path(__file__).parent / 'assets' / 'fonts'
    for ttf in fonts_dir.glob('*.ttf'):
        fid = QFontDatabase.addApplicationFont(str(ttf))
        if fid >= 0:
            families = QFontDatabase.applicationFontFamilies(fid)
            print(f"Font loaded: {ttf.name} -> {families}")
        else:
            print(f"Font failed to load: {ttf.name}")


def _prewarm_heavy_modules():
    """Eagerly initialize heavy libs that the editor would otherwise pay for
    on the first clip open.

    On first use, each of these triggers expensive one-time work:
      - cv2: loads the OpenCV C++ extension and FFmpeg demux backend.
      - QMediaPlayer: spins up Windows MediaFoundation and the H.264 codec.
      - imageio_ffmpeg: locates the bundled ffmpeg binary on disk.

    Doing this at app startup (where the splash hides the latency) makes the
    first ClipViewer open feel instantaneous. None of these are user-visible
    in the warming step — the QMediaPlayer is constructed and immediately
    deleted, just to pay the MediaFoundation initialization cost once.
    """
    try:
        import cv2  # noqa: F401  — import alone is enough to load the .pyd
        # Touch a method so any lazy module-level init also runs.
        _ = cv2.__version__
    except Exception as e:
        print(f'[Prewarm] cv2 unavailable: {e}')

    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        print(f'[Prewarm] imageio-ffmpeg unavailable: {e}')

    if sys.platform == 'win32':
        try:
            from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
            # Pays the one-time cost of MediaFoundation init on Windows.
            _warm_player = QMediaPlayer()
            _warm_audio  = QAudioOutput()
            _warm_player.setAudioOutput(_warm_audio)
        except Exception as e:
            print(f'[Prewarm] Qt multimedia init failed: {e}')


def main():
    # When the frozen Windows exe is relaunched as the capture-card subprocess,
    # route into the card process instead of the main application.
    if '--card-process' in sys.argv:
        from ui.capture_card_process import main as _card_main
        _card_main()
        return

    print("Main.py successfully initiated")
    app = QApplication(sys.argv)
    app.setApplicationName('FTHR Clips')
    app.setApplicationVersion('1.0.0-alpha')

    _load_fonts()
    _prewarm_heavy_modules()

    window = MainWindow()
    window.showMaximized()

    try:
        return app.exec()
    finally:
        window.stop_engine()


if __name__ == '__main__':
    sys.exit(main())
