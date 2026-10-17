"""Animated capture-result window painted with QPainter.

A separate process keeps its Qt event loop responsive during finalization.
CaptureCardClient forwards commands; animations control slide-in, hold,
and slide-out while timers update shimmer and progress.
"""

import os
from pathlib import Path

from PySide6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QSequentialAnimationGroup,
    QPauseAnimation, QEasingCurve, QPoint, QRect, QElapsedTimer,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QFont, QFontDatabase, QFontMetrics,
    QLinearGradient,
)
from PySide6.QtWidgets import QApplication, QWidget

from core.settings_manager import SettingsManager
from core.theme_manager import ThemeManager
from ui.sound_playback import SoundPlayback


# Layout constants

_FULL_W, _FULL_H       = 340, 116
_COMPACT_W, _COMPACT_H = 320, 76
_BORDER    = 3           # left accent border width
_MARGIN    = 16          # distance from screen edges

# Animation timing. Every value is milliseconds; ``hold_duration_ms`` is the
# fully-arrived visible hold. Total lifetime also includes slide-in/out.
_SLIDE_IN_DURATION_MS = 450
_DEFAULT_HOLD_DURATION_MS = 2400
_SLIDE_OUT_DURATION_MS = 350

# Sound files live in assets/sounds/ alongside the UI source.
_SND_DIR = Path(__file__).parent.parent / 'assets' / 'sounds'
_SOUND_FILES = {
    'clip_captured': _SND_DIR / 'clip_captured.wav',
    'screenshot_captured': _SND_DIR / 'screenshot_saved.wav',
    'error': _SND_DIR / 'error.wav',
    'startup': _SND_DIR / 'startup.wav',
    'upload_successful': _SND_DIR / 'upload_successful.wav',
    'upload_failed': _SND_DIR / 'upload_failed.wav',
}
_SOUND_VOLUME_KEYS = {
    'clip_captured': 'sound_volume_clip',
    'screenshot_captured': 'sound_volume_screenshot',
    'error': 'sound_volume_error',
    'startup': 'sound_volume_startup',
    'upload_successful': 'sound_volume_upload_successful',
    'upload_failed': 'sound_volume_upload_failed',
}

_FONT_ASSET = Path(__file__).parent.parent / 'assets' / 'fonts' / 'Oswald-Bold.ttf'
_NOTIFICATION_FONTS_LOADED = False

_SOUND_PLAYBACK: SoundPlayback | None = None


def _load_notification_fonts() -> None:
    global _NOTIFICATION_FONTS_LOADED
    if _NOTIFICATION_FONTS_LOADED:
        return
    paths = [*_FONT_ASSET.parent.glob('*.ttf'),
             *ThemeManager().get_custom_font_paths().values()]
    for path in dict.fromkeys(paths):
        QFontDatabase.addApplicationFont(str(path))
    _NOTIFICATION_FONTS_LOADED = True


def _body_font(size: int, weight=QFont.Weight.Normal) -> QFont:
    family = ThemeManager().get_font('body') or 'Oswald'
    font = QFont(family, size, weight)
    font.setFamilies([family, 'Segoe UI', 'Noto Sans', 'DejaVu Sans'])
    return font


def _resolve_sound(key: str) -> Path:
    try:
        custom = ThemeManager().get_custom_sound_path(key)
        if custom is not None and custom.exists():
            return custom
    except (AttributeError, OSError, TypeError, ValueError):
        # A missing/corrupt custom theme sound must fall back to the bundled cue.
        return _SOUND_FILES[key]
    return _SOUND_FILES[key]


# Sound helper

def _get_sound_playback() -> SoundPlayback:
    global _SOUND_PLAYBACK
    if _SOUND_PLAYBACK is None:
        # The card process is long-lived, so loading the short cues once avoids
        # a decoder startup race every time a clip is saved or the app starts.
        preload = [_resolve_sound(key) for key in _SOUND_FILES]
        _SOUND_PLAYBACK = SoundPlayback(preload_paths=preload)
    return _SOUND_PLAYBACK


def _play_sound(path: Path, volume: int = 100) -> None:
    """Play a cue after its local source is ready."""
    if path.exists():
        try:
            _get_sound_playback().play(path, volume)
        except Exception:
            # Audio must never take down the detached notification process.
            global _SOUND_PLAYBACK
            _SOUND_PLAYBACK = None


def _notification_sounds_enabled() -> bool:
    try:
        return bool(SettingsManager().get('notification_sounds_enabled', True))
    except Exception:
        return True


# CaptureCard widget

class CaptureCard(QWidget):
    """
    Frameless, always-on-top notification card drawn entirely in paintEvent.
    Slide in → hold → slide out, with a draining progress bar and shimmer.
    """

    def __init__(self, *, visuals_enabled: bool = True):
        _load_notification_fonts()
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_FULL_W, _FULL_H)

        # Display state
        self._headline: str = 'CLIP CAPTURED'
        self._stats: list[tuple[str, str]] = []   # (value, label) pairs
        self._detail: str = ''
        self._progress: float = 1.0               # 1.0 → 0.0 during hold
        self._shimmer: float = -1.0               # -1 = off, 0→3 = x position
        self._scale: float = 1.0                  # set per-show from screen height
        self._base_w = _FULL_W
        self._base_h = _FULL_H
        self._compact = False
        self._display_kind = 'normal'
        self._visuals_enabled = bool(visuals_enabled)
        self._hold_duration_ms = _DEFAULT_HOLD_DURATION_MS

        # Theme colors — loaded once per show to avoid per-paint overhead
        self._c_bg = QColor('#000000')
        self._c_accent = QColor('#ffffff')
        self._c_text = QColor('#ffffff')
        self._c_divider = QColor('#222222')
        self._c_stats_dim = QColor('#666666')
        self._c_prog_track = QColor('#111111')
        self._c_prog_fill = QColor('#ffffff')

        # Progress drain
        self._prog_timer = QTimer(self)
        self._prog_timer.setInterval(16)
        self._prog_timer.timeout.connect(self._tick_progress)
        self._prog_clock = QElapsedTimer()

        # Shimmer sweep
        self._shim_timer = QTimer(self)
        self._shim_timer.setInterval(16)
        self._shim_timer.timeout.connect(self._tick_shimmer)
        self._shim_clock = QElapsedTimer()

        # Delay timers (stored so they can be cancelled on re-trigger)
        self._shimmer_delay = QTimer(self)
        self._shimmer_delay.setSingleShot(True)
        self._shimmer_delay.timeout.connect(self._start_shimmer)

        self._progress_delay = QTimer(self)
        self._progress_delay.setSingleShot(True)
        self._progress_delay.timeout.connect(self._start_progress)

        # The animation group deliberately has no Qt parent. Python reference
        # ownership disposes the previous group when self._seq is replaced. With
        # a Qt parent, old animation groups would remain as children and
        # compete with the new group for control of self.pos during rapid saves.
        self._seq: QSequentialAnimationGroup | None = None

        # Warm the short cues while the detached card is starting. The first
        # startup/clip event can then play immediately instead of racing a
        # decoder initialization on the notification path.
        try:
            _get_sound_playback()
        except Exception:
            # Notification audio is optional; a later event retries initialization.
            pass

    # Public API

    def show_clip(self, duration_s: int, fps: int, resolution: str,
                  hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'CLIP CAPTURED'
        self._detail = ''
        self._stats = [
            (f'{duration_s}s',  'Duration'),
            (f'{fps} FPS',      'Framerate'),
            (resolution,        'Resolution'),
        ]
        self._show('clip_captured', hold_duration_ms=hold_duration_ms)

    def show_screenshot(
            self, hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'SCREENSHOT SAVED'
        self._detail = ''
        self._stats = []
        self._show(
            'screenshot_captured', compact=True,
            hold_duration_ms=hold_duration_ms)

    def show_error(self, detail: str = '',
                   hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'CAPTURE FAILED'
        self._detail = detail
        self._stats = []
        self._show(
            'error', compact=True, hold_duration_ms=hold_duration_ms)

    def show_upload(self, filename: str = '',
                    hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'CLIP UPLOADED'
        self._detail = filename
        self._stats = []
        self._show(
            'upload_successful', compact=True,
            hold_duration_ms=hold_duration_ms)

    def show_upload_failed(
            self, detail: str = '',
            hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'UPLOAD FAILED'
        self._detail = detail
        self._stats = []
        self._show(
            'upload_failed', compact=True,
            hold_duration_ms=hold_duration_ms)

    def play_startup(self) -> None:
        if not _notification_sounds_enabled():
            return
        key = 'startup'
        try:
            volume = int(SettingsManager().get(
                _SOUND_VOLUME_KEYS[key], 100))
        except Exception:
            volume = 100
        _play_sound(_resolve_sound(key), _master_volume(volume))

    def show_prompt(self, text: str,
                    hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'CAPTURE UPDATE'
        self._detail = text
        self._stats = []
        self._show(
            None, compact=True, hold_duration_ms=hold_duration_ms)

    def show_recording_saved(
            self, filename: str = '',
            hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'MANUAL RECORDING SAVED'
        self._detail = (
            f'{filename}  ·  Ready to trim and export.'
            if filename else 'Ready to trim and export.')
        self._stats = []
        self._show(
            None, compact=True, hold_duration_ms=hold_duration_ms)

    def show_capturing(self, source: str,
                       hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'NOW CAPTURING'
        self._detail = source or 'Desktop'
        self._stats = []
        self._show(
            None, compact=True, hold_duration_ms=hold_duration_ms)

    def show_background_capture(
            self, source: str,
            hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._headline = 'NOW CAPTURING'
        self._detail = f'{source or "Desktop"}  ·  Running in the background'
        self._stats = []
        self._show(
            None, compact=True, hold_duration_ms=hold_duration_ms)
        self._display_kind = 'background'

    def hide_background_capture(self) -> None:
        if self._display_kind == 'background':
            self._stop_display()

    # Internal

    def set_visuals_enabled(self, enabled: bool) -> None:
        """Toggle the card window while leaving notification sounds enabled."""
        self._visuals_enabled = bool(enabled)
        if not self._visuals_enabled:
            self._stop_display()

    def _stop_display(self) -> None:
        # Cancel any running animation/timers and destroy the old group so it
        # doesn't linger as a Qt child competing with the new animation.
        if self._seq is not None:
            self._seq.stop()
            self._seq = None
        self._prog_timer.stop()
        self._shim_timer.stop()
        self._shimmer_delay.stop()
        self._progress_delay.stop()
        self.hide()
        self._display_kind = 'normal'

    def _show(self, sound_key: str | None, *, compact: bool = False,
              hold_duration_ms: int = _DEFAULT_HOLD_DURATION_MS) -> None:
        self._stop_display()
        try:
            self._hold_duration_ms = max(1, int(hold_duration_ms))
        except (TypeError, ValueError, OverflowError):
            self._hold_duration_ms = _DEFAULT_HOLD_DURATION_MS
        self._compact = compact
        self._base_w = _COMPACT_W if compact else _FULL_W
        self._base_h = _COMPACT_H if compact else _FULL_H

        try:
            tm = ThemeManager()
            self._c_bg         = QColor(tm.get_capture_card_color('CAPTURE_CARD_BG'))
            self._c_accent     = QColor(tm.get_capture_card_color('CAPTURE_CARD_ACCENT'))
            self._c_text       = QColor(tm.get_capture_card_color('CAPTURE_CARD_TEXT'))
            self._c_divider    = QColor(tm.get_capture_card_color('CAPTURE_CARD_DIVIDER'))
            self._c_stats_dim  = QColor(tm.get_capture_card_color('CAPTURE_CARD_STATS_DIM'))
            self._c_prog_track = QColor(tm.get_capture_card_color('CAPTURE_CARD_PROGRESS_TRACK'))
            self._c_prog_fill  = QColor(tm.get_capture_card_color('CAPTURE_CARD_PROGRESS_FILL'))
        except Exception:
            pass

        if sound_key is not None and _notification_sounds_enabled():
            try:
                volume = int(SettingsManager().get(
                    _SOUND_VOLUME_KEYS[sound_key], 100))
            except Exception:
                volume = 100
            _play_sound(_resolve_sound(sound_key), _master_volume(volume))

        # The helper process remains alive as a lightweight sound host when
        # card visuals are disabled. Keep this after sound dispatch so all
        # notification cues retain their existing behaviour.
        if not self._visuals_enabled:
            return

        # Determine positions and scale for this screen.
        # FTHR_CARD_SCREEN_NAME is set by CaptureCardClient based on the user's
        # notification_monitor setting. 'auto' falls back to highest refresh rate.
        screens = QApplication.screens()
        target = os.environ.get('FTHR_CARD_SCREEN_NAME', 'auto')
        if target != 'auto':
            active_screen = next((s for s in screens if s.name() == target), None)
        else:
            active_screen = None
        if active_screen is None:
            active_screen = max(screens, key=lambda s: s.refreshRate()) if screens else QApplication.primaryScreen()
        screen = active_screen.availableGeometry()

        # Scale the card relative to a 1080p baseline so it doesn't look like a
        # billboard on a 768p laptop or a postage stamp on 4K. Clamp to [0.65, 1.0]
        # — never bigger than the design size, never microscopic.
        self._scale = min(1.0, max(0.65, screen.height() / 1080))
        w = int(self._base_w * self._scale)
        h = int(self._base_h * self._scale)
        self.setFixedSize(w, h)

        on_x   = screen.right() - w - _MARGIN
        on_y   = screen.top()   +     _MARGIN
        off_x  = screen.right() + w + 10  # off-screen right

        # Reset paint state
        self._progress = 1.0
        self._shimmer  = -1.0

        # Place off-screen, then show
        self.move(off_x, on_y)
        self.show()

        on_pt  = QPoint(on_x,  on_y)
        off_pt = QPoint(off_x, on_y)

        # Animations have no Qt parent — the group owns them via addAnimation(),
        # and the group itself has no Qt parent so Python refcount controls its
        # lifetime. Replacing self._seq destroys everything cleanly.
        sin = QPropertyAnimation(self, b'pos')
        sin.setDuration(_SLIDE_IN_DURATION_MS)
        sin.setStartValue(off_pt)
        sin.setEndValue(on_pt)
        sin.setEasingCurve(QEasingCurve.Type.OutBack)

        hold = QPauseAnimation(self._hold_duration_ms)

        sout = QPropertyAnimation(self, b'pos')
        sout.setDuration(_SLIDE_OUT_DURATION_MS)
        sout.setStartValue(on_pt)
        sout.setEndValue(off_pt)
        sout.setEasingCurve(QEasingCurve.Type.InCubic)

        self._seq = QSequentialAnimationGroup()
        self._seq.addAnimation(sin)
        self._seq.addAnimation(hold)
        self._seq.addAnimation(sout)
        self._seq.finished.connect(self.hide)
        self._seq.start()

        # Start the shimmer during arrival, then drain progress only after the
        # card has landed. These delays are visual timing constants.
        self._shimmer_delay.start(150)
        self._progress_delay.start(_SLIDE_IN_DURATION_MS + 30)

    def _start_progress(self) -> None:
        self._prog_clock.start()
        self._prog_timer.start()

    def _tick_progress(self) -> None:
        elapsed = self._prog_clock.elapsed()
        self._progress = max(
            0.0, 1.0 - elapsed / self._hold_duration_ms)
        if elapsed >= self._hold_duration_ms:
            self._prog_timer.stop()
        self.update()

    def _start_shimmer(self) -> None:
        self._shimmer = 0.0
        self._shim_clock.start()
        self._shim_timer.start()

    def _tick_shimmer(self) -> None:
        elapsed = self._shim_clock.elapsed()
        # Full sweep in 550ms across 3× the card width
        self._shimmer = elapsed / 550.0 * 3.0
        if elapsed >= 550:
            self._shimmer = -1.0
            self._shim_timer.stop()
        self.update()

    # Painting

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Scale all painting to the selected full/compact coordinate space.
        if self._scale != 1.0:
            p.scale(self._scale, self._scale)

        w = self._base_w
        h = self._base_h

        # Background (use base dimensions, not widget size)
        p.fillRect(0, 0, w, h, self._c_bg)

        # Left accent border
        p.fillRect(0, 0, _BORDER, h, self._c_accent)

        # Clip shimmer to card interior
        inner = QRect(_BORDER, 0, w - _BORDER, h)
        p.setClipRect(inner)

        # Shimmer overlay sweep
        if self._shimmer >= 0.0:
            cx = _BORDER + int((w - _BORDER) * (self._shimmer / 3.0 - 0.3))
            grad = QLinearGradient(cx - 70, 0, cx + 70, 0)
            shimmer_clear = QColor(self._c_accent)
            shimmer_clear.setAlpha(0)
            shimmer_soft = QColor(self._c_accent)
            shimmer_soft.setAlpha(14)
            grad.setColorAt(0.0, shimmer_clear)
            grad.setColorAt(0.5, shimmer_soft)
            grad.setColorAt(1.0, shimmer_clear)
            p.fillRect(inner, grad)

        p.setClipping(False)

        # Compact cards keep punctuation-heavy text in the native body face;
        # headline tracking is reserved for short all-caps status text.
        if self._compact:
            icon_x, icon_y, icon_size = _BORDER + 12, 13, 20
            headline_x, headline_y, headline_h = _BORDER + 42, 8, 25
            headline_size = 11
        else:
            icon_x, icon_y, icon_size = _BORDER + 17, 17, 26
            headline_x, headline_y, headline_h = _BORDER + 52, 12, 28
            headline_size = 15
        self._draw_camera(p, icon_x, icon_y, icon_size)

        hl_font = QFont(
            ThemeManager().get_font('display') or 'Oswald',
            headline_size,
            QFont.Weight.DemiBold,
        )
        hl_font.setFamilies([ThemeManager().get_font('display'),
                             'Segoe UI', 'Noto Sans', 'DejaVu Sans'])
        hl_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0)
        p.setFont(hl_font)
        p.setPen(self._c_text)
        headline = QFontMetrics(hl_font).elidedText(
            self._headline.capitalize(),
            Qt.TextElideMode.ElideRight,
            w - headline_x - 12,
        )
        p.drawText(
            headline_x, headline_y, w - headline_x - 12, headline_h,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            headline,
        )

        if self._compact:
            if self._detail:
                detail_font = _body_font(9, QFont.Weight.Normal)
                p.setFont(detail_font)
                detail_color = QColor(self._c_text)
                detail_color.setAlpha(180)
                p.setPen(detail_color)
                detail = QFontMetrics(detail_font).elidedText(
                    self._detail,
                    Qt.TextElideMode.ElideRight,
                    w - headline_x - 12,
                )
                p.drawText(
                    headline_x, 33, w - headline_x - 12, 27,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    detail,
                )
        else:
            p.setPen(QPen(self._c_divider, 1))
            p.drawLine(_BORDER + 10, 47, w - 10, 47)
            if self._stats:
                self._draw_stats(p, 54)

        # Progress track ──
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(0, h - 2, w, 2, self._c_prog_track)

        # Progress fill ──
        fill_w = int(w * self._progress)
        if fill_w > 0:
            p.fillRect(0, h - 2, fill_w, 2, self._c_prog_fill)

        p.end()

    def _draw_camera(self, p: QPainter, x: int, y: int, size: int) -> None:
        """Render the supplied camera SVG with capture-card text tint."""
        from ui.icons import icon_pixmap
        pixmap = icon_pixmap('camera.png', size, self._c_text.name())
        p.drawPixmap(x, y, pixmap)

    def _draw_stats(self, p: QPainter, top: int) -> None:
        """Three-column stats with vertical separators."""
        n = len(self._stats)
        if n == 0:
            return

        usable = self._base_w - _BORDER - 20
        col_w  = usable // n
        base_x = _BORDER + 10

        val_font = _body_font(11, QFont.Weight.DemiBold)

        lbl_font = _body_font(8)
        lbl_font.setWeight(QFont.Weight.Light)

        for i, (val, lbl) in enumerate(self._stats):
            cx = base_x + i * col_w

            p.setFont(val_font)
            p.setPen(self._c_text)
            value = QFontMetrics(val_font).elidedText(
                str(val), Qt.TextElideMode.ElideRight, col_w - 6)
            p.drawText(cx, top, col_w - 6, 22,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       value)

            if lbl:
                p.setFont(lbl_font)
                p.setPen(self._c_stats_dim)
                p.drawText(cx, top + 22, col_w - 6, 16,
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           lbl.upper())

            # Vertical separator (not after last column)
            if i < n - 1:
                sep_x = cx + col_w - 2
                p.setPen(QPen(self._c_divider, 1))
                p.drawLine(sep_x, top + 2, sep_x, top + 34)


def _master_volume(volume):
    try:
        master = max(0, min(100, int(SettingsManager().get('sound_volume_master', 100))))
    except (TypeError, ValueError):
        master = 100
    return round(max(0, min(100, volume)) * master / 100)
