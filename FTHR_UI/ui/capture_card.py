"""
capture_card.py — that slick little "CLIP CAPTURED" toast that slides in from
the corner when you grab a clip. The dopamine hit. The whole vibe.

Architecture, because it's not obvious:
- The card is ONE frameless translucent window and EVERYTHING in it is hand-drawn
  in paintEvent() with QPainter — the camera icon, the text, the shimmer, the
  draining progress bar. No child widgets. Drawing it ourselves means it's pixel
  perfect, themeable, and cheap. Trying to do this shimmer/progress thing with
  real QWidgets and stylesheets would be a nightmare and look worse.
- The slide in → hold → slide out motion is a QSequentialAnimationGroup: three
  animations played back to back (slide in, pause, slide out). Qt does the
  tweening, we just describe the keyframes.
- The progress bar drain and the shimmer sweep are driven by their own ~60fps
  QTimers, kicked off at staggered delays so it all feels choreographed.

Heads up: in the real app this whole thing runs in a SEPARATE PROCESS (see
capture_card_client.py for the why). This file is the actual card; the client is
the remote control.

Public API:
    card = CaptureCard()
    card.show_clip(duration_s, fps, resolution_label)
    card.show_screenshot()
    card.show_error(detail='')
"""

import ctypes
import sys
import subprocess as _subprocess
from pathlib import Path

from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QSequentialAnimationGroup,
    QPauseAnimation, QEasingCurve, QPoint, QRect, QElapsedTimer,
)
from PyQt6.QtGui import (
    QColor, QPainter, QPen, QFont, QLinearGradient, QPainterPath,
)
from PyQt6.QtWidgets import QApplication, QWidget

from core.theme_manager import ThemeManager


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_W, _H     = 340, 116   # card size in pixels
_BORDER    = 3           # left accent border width
_MARGIN    = 16          # distance from screen edges

# Animation timing (ms)
_SLIDE_IN  = 450
_HOLD      = 2400
_SLIDE_OUT = 350

# Sound files live in assets/sounds/ alongside the UI source.
_SND_DIR        = Path(__file__).parent.parent / 'assets' / 'sounds'
_SND_CLIP       = _SND_DIR / 'clip_captured.mp3'
_SND_SCREENSHOT = _SND_DIR / 'screenshot_saved.mp3'
_SND_ERROR      = _SND_DIR / 'error.mp3'

_MCI_ALIAS = 'fthr_card'


# ---------------------------------------------------------------------------
# Sound helper
# ---------------------------------------------------------------------------

def _play_mp3(path: Path) -> None:
    """Fire-and-forget MP3 playback. And yes, this is THE sound playback hack.

    You'd think "just use QMediaPlayer" — and I did, for a while. But spinning up
    a QMediaPlayer for a 200ms blip is heavy, leaks if you don't babysit it, and
    on Linux it would straight up HANG the prewarm on some setups. So instead we
    go full caveman and shell out to whatever the OS already has:
      - Windows: the ancient MCI API via winmm. Close any old handle, open, play.
        It's from like Windows 3.1 and it just works. don't fix what ain't broke.
      - Linux: shell out to ffplay (ships with ffmpeg, which we already depend on)
        with no display, auto-exit, dead silent logging.
    Fire and forget. We never wait for it. This is fine. 🔥
    """
    if not path.exists():
        return
    if sys.platform == 'win32':
        try:
            mci = ctypes.windll.winmm.mciSendStringW
            # Close any previous playback first or MCI gets cranky about the alias
            # already being in use when you clip twice in a row.
            mci(f'close {_MCI_ALIAS}', None, 0, None)
            mci(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}', None, 0, None)
            mci(f'play {_MCI_ALIAS}', None, 0, None)
        except Exception:
            pass
    else:
        try:
            _subprocess.Popen(
                ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'quiet', str(path)],
                stdout=_subprocess.DEVNULL, stderr=_subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass  # ffplay not installed — silent fallback
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CaptureCard widget
# ---------------------------------------------------------------------------

class CaptureCard(QWidget):
    """
    Frameless, always-on-top notification card drawn entirely in paintEvent.
    Slide in → hold → slide out, with a draining progress bar and shimmer.
    """

    def __init__(self):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(_W, _H)

        # Display state
        self._headline: str = 'CLIP CAPTURED'
        self._stats: list[tuple[str, str]] = []   # (value, label) pairs
        self._progress: float = 1.0               # 1.0 → 0.0 during hold
        self._shimmer: float = -1.0               # -1 = off, 0→3 = x position
        self._scale: float = 1.0                  # set per-show from screen height

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

        # The animation group has NO Qt parent on purpose. That means plain old
        # Python refcounting owns it: the moment we reassign self._seq, the old
        # group's refcount hits zero and Qt tears it down cleanly. If we gave it
        # a Qt parent instead, old animation groups would pile up as children and
        # fight the new one for control of self.pos. Spamming the clip hotkey
        # would then make the card teleport around like it's possessed. ask me
        # how I know.
        self._seq: QSequentialAnimationGroup | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_clip(self, duration_s: int, fps: int, resolution: str) -> None:
        self._headline = 'CLIP CAPTURED'
        self._stats = [
            (f'{duration_s}s',  'Duration'),
            (f'{fps} FPS',      'Framerate'),
            (resolution,        'Resolution'),
        ]
        self._show(_SND_CLIP)

    def show_screenshot(self) -> None:
        self._headline = 'SCREENSHOT SAVED'
        self._stats = []
        self._show(_SND_SCREENSHOT)

    def show_error(self, detail: str = '') -> None:
        self._headline = 'CAPTURE FAILED'
        self._stats = [(detail, '')] if detail else []
        self._show(_SND_ERROR)

    def show_upload(self, filename: str = '') -> None:
        self._headline = 'CLIP UPLOADED'
        self._stats = [(filename, '')] if filename else []
        self._show(_SND_CLIP)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _show(self, sound: Path) -> None:
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

        # Load theme colors once per show
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

        _play_mp3(sound)

        # Determine positions and scale for this screen
        screen = QApplication.primaryScreen().availableGeometry()

        # Scale the card relative to a 1080p baseline so it doesn't look like a
        # billboard on a 768p laptop or a postage stamp on 4K. Clamp to [0.65, 1.0]
        # — never bigger than the design size, never microscopic.
        self._scale = min(1.0, max(0.65, screen.height() / 1080))
        w = int(_W * self._scale)
        h = int(_H * self._scale)
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
        sin.setDuration(_SLIDE_IN)
        sin.setStartValue(off_pt)
        sin.setEndValue(on_pt)
        sin.setEasingCurve(QEasingCurve.Type.OutBack)

        hold = QPauseAnimation(_HOLD)

        sout = QPropertyAnimation(self, b'pos')
        sout.setDuration(_SLIDE_OUT)
        sout.setStartValue(on_pt)
        sout.setEndValue(off_pt)
        sout.setEasingCurve(QEasingCurve.Type.InCubic)

        self._seq = QSequentialAnimationGroup()
        self._seq.addAnimation(sin)
        self._seq.addAnimation(hold)
        self._seq.addAnimation(sout)
        self._seq.finished.connect(self.hide)
        self._seq.start()

        # The choreography. Shimmer fires 150ms into the slide so it sweeps as the
        # card arrives (feels reactive, not pre-baked). The progress bar only
        # starts draining once the card has fully landed — draining mid-slide
        # looks broken. These two magic numbers are pure feel, tuned by eyeball.
        self._shimmer_delay.start(150)
        self._progress_delay.start(_SLIDE_IN + 30)

    def _start_progress(self) -> None:
        self._prog_clock.start()
        self._prog_timer.start()

    def _tick_progress(self) -> None:
        elapsed = self._prog_clock.elapsed()
        self._progress = max(0.0, 1.0 - elapsed / _HOLD)
        if elapsed >= _HOLD:
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

    # ------------------------------------------------------------------
    # Painting
    # ------------------------------------------------------------------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Scale all painting to the base 340×116 coordinate space
        if self._scale != 1.0:
            p.scale(self._scale, self._scale)

        # Background (use base dimensions, not widget size)
        p.fillRect(0, 0, _W, _H, self._c_bg)

        # Left accent border
        p.fillRect(0, 0, _BORDER, _H, self._c_accent)

        # Clip shimmer to card interior
        inner = QRect(_BORDER, 0, _W - _BORDER, _H)
        p.setClipRect(inner)

        # Shimmer overlay sweep
        if self._shimmer >= 0.0:
            cx = _BORDER + int((_W - _BORDER) * (self._shimmer / 3.0 - 0.3))
            grad = QLinearGradient(cx - 70, 0, cx + 70, 0)
            grad.setColorAt(0.0, QColor(255, 255, 255, 0))
            grad.setColorAt(0.5, QColor(255, 255, 255, 14))
            grad.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillRect(inner, grad)

        p.setClipping(False)

        # ── Top row: camera icon + headline ──
        icon_x, icon_y, icon_size = _BORDER + 17, 17, 26
        self._draw_camera(p, icon_x, icon_y, icon_size)

        hl_font = QFont('Bahnschrift', 15, QFont.Weight.Bold)
        hl_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        p.setFont(hl_font)
        p.setPen(self._c_text)
        p.drawText(
            _BORDER + 52, 12, _W - _BORDER - 58, 28,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._headline,
        )

        # ── Divider ──
        p.setPen(QPen(self._c_divider, 1))
        p.drawLine(_BORDER + 10, 47, _W - 10, 47)

        # ── Stats ──
        if self._stats:
            self._draw_stats(p, 54)

        # ── Progress track ──
        p.setPen(Qt.PenStyle.NoPen)
        p.fillRect(0, _H - 2, _W, 2, self._c_prog_track)

        # ── Progress fill ──
        fill_w = int(_W * self._progress)
        if fill_w > 0:
            p.fillRect(0, _H - 2, fill_w, 2, self._c_prog_fill)

        p.end()

    def _draw_camera(self, p: QPainter, x: int, y: int, size: int) -> None:
        """Render a video-camera icon matching the SVG in capturecard.html."""
        scale = size / 24.0
        pen = QPen(self._c_text)
        pen.setWidthF(1.6 * scale)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        # Body rect: SVG x=2,y=6,w=14,h=12,rx=1.5
        p.drawRoundedRect(
            int(x + 2*scale), int(y + 6*scale),
            int(14*scale),    int(12*scale),
            1.5*scale, 1.5*scale,
        )

        # Viewfinder triangle: M16 10 l5 -3 v10 l-5 -3 V10
        path = QPainterPath()
        path.moveTo(x + 16*scale, y + 10*scale)
        path.lineTo(x + 21*scale, y +  7*scale)
        path.lineTo(x + 21*scale, y + 17*scale)
        path.lineTo(x + 16*scale, y + 13*scale)
        path.closeSubpath()
        p.drawPath(path)

    def _draw_stats(self, p: QPainter, top: int) -> None:
        """Three-column stats with vertical separators."""
        n = len(self._stats)
        if n == 0:
            return

        usable = _W - _BORDER - 20
        col_w  = usable // n
        base_x = _BORDER + 10

        val_font = QFont('Bahnschrift', 12, QFont.Weight.DemiBold)
        val_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)

        lbl_font = QFont('Bahnschrift', 8)
        lbl_font.setWeight(QFont.Weight.Light)
        lbl_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.2)

        for i, (val, lbl) in enumerate(self._stats):
            cx = base_x + i * col_w

            p.setFont(val_font)
            p.setPen(self._c_text)
            p.drawText(cx, top, col_w - 6, 22,
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       val)

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
