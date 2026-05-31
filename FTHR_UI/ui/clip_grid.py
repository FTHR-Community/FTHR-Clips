# clip_grid.py - the clip + screenshot library grid (yes, the layout is very
# "inspired by" Medal/Outplayed — imitation, flattery, etc.)
#
# Big picture: this scans a few folders for video/image files, groups them into
# "TUE, APR 28" style date sections, and lays each section out as a responsive
# grid of cards. The number of columns recalculates from the window width.
#
# The whole design goal here is DON'T BLOCK THE MAIN THREAD. Decoding a video
# frame for a thumbnail with cv2 is slow (20-100ms each), and statting hundreds
# of files isn't free either. So:
#   - file scanning + sorting happens on a worker thread (_FileCollectWorker)
#   - thumbnail generation happens on a worker thread (_ThumbnailWorker)
#   - thumbnails + duration are cached to disk so we only pay that cost once
# If the UI ever janks while scrolling this grid, something slow leaked back
# onto the main thread. go find it. it's always cv2.
import os
import sys
import subprocess
import hashlib
import cv2
from datetime import datetime, date

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout,
    QGraphicsOpacityEffect, QMenu, QMessageBox, QApplication,
    QPushButton, QComboBox, QSizePolicy,
)
from PyQt6.QtCore import (
    Qt, pyqtSignal, QTimer, QRunnable, QThreadPool, QObject,
    QFileSystemWatcher, QPropertyAnimation, QEasingCurve, QRect, QPoint,
)
from PyQt6.QtGui import QPixmap, QImage, QPainter, QColor, QPen


class _DropdownCombo(QComboBox):
    """QComboBox that shows icons/dropdown.png as its arrow, rotated when open."""
    _arrow_pix: 'QPixmap | None' = None

    @classmethod
    def _get_arrow(cls) -> 'QPixmap | None':
        if cls._arrow_pix is None:
            path = os.path.join(os.path.dirname(__file__), '..', 'assets', 'icons', 'dropdown.png')
            if os.path.exists(path):
                cls._arrow_pix = QPixmap(path)
        return cls._arrow_pix

    def __init__(self, parent=None):
        super().__init__(parent)
        self._popup_open = False

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
        scaled = pix.scaled(sz, sz, Qt.AspectRatioMode.KeepAspectRatio,
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

from ui.style import (
    Colors, Fonts, Sizes,
    label_display, label_uppercase, label_body,
    CONTEXT_MENU_QSS, COMBO_QSS, CARD_QSS,
)


THUMB_CACHE_DIR = os.path.join(os.path.expanduser('~'), '.fthr', 'thumbnails')

# These folders hold processed/shared clips — not shown in the main library grid
_GRID_EXCLUDED_DIRS = {'Exported', 'Shared'}
_VIDEO_EXTS = ('.mp4', '.mkv', '.avi')
_IMAGE_EXTS = ('.png', '.jpg', '.jpeg')


# ─── Card geometry ──────────────────────────────────────────────────
# Thumbnail aspect 16:9 → 320×180. Card body adds 64px below for title row.
_CARD_W      = 320
_THUMB_H     = 180
_CARD_BODY_H = 72
_CARD_H      = _THUMB_H + _CARD_BODY_H


def _get_cached_thumb_path(file_path: str) -> str:
    """Return a cache path based on file path + mtime so stale caches auto-invalidate.

    Baking the mtime into the hash means if a file is overwritten (same name,
    new content) the cache key changes and we regenerate. Free invalidation,
    no bookkeeping. galaxy brain moment, smaller scale.
    """
    mtime = os.path.getmtime(file_path)
    key = f'{file_path}|{mtime}'.encode()
    return os.path.join(THUMB_CACHE_DIR, hashlib.md5(key).hexdigest() + '.jpg')


def _get_cached_duration_path(thumb_path: str) -> str:
    """Sidecar file storing clip metadata so we don't reopen the video on cache hits.

    Format: a single line of "duration_sec width height fps" — duration alone is
    kept first for backwards compatibility with caches written before the
    width/height/fps fields were added; readers tolerate the old single-int form.
    The same .dur path is shared by ClipViewer so it can skip cv2.VideoCapture
    entirely on cache hits.
    """
    return thumb_path[:-4] + '.dur'  # replace '.jpg' with '.dur'


def _read_cached_duration(dur_path: str) -> int:
    """Backwards-compatible reader that returns just the duration."""
    meta = read_cached_metadata(dur_path)
    return meta[0] if meta else 0


def read_cached_metadata(dur_path: str):
    """
    Return (duration_sec, width, height, fps) from the .dur sidecar, or None.

    Older caches contained only the duration; in that case width/height are 0
    and fps is 0.0 — callers should treat zeros as "unknown" and fall back to
    cv2.VideoCapture themselves.
    """
    try:
        with open(dur_path, 'r') as f:
            parts = f.read().strip().split()
        if not parts:
            return None
        duration = int(parts[0])
        width    = int(parts[1]) if len(parts) > 1 else 0
        height   = int(parts[2]) if len(parts) > 2 else 0
        fps      = float(parts[3]) if len(parts) > 3 else 0.0
        return (duration, width, height, fps)
    except (OSError, ValueError):
        return None


def _write_cached_duration(dur_path: str, duration: int,
                            width: int = 0, height: int = 0, fps: float = 0.0):
    """Persist duration plus optional width/height/fps so ClipViewer can skip cv2."""
    try:
        with open(dur_path, 'w') as f:
            f.write(f'{int(duration)} {int(width)} {int(height)} {fps:.3f}')
    except OSError:
        pass


# Public lookup used by ClipViewer to avoid blocking cv2.VideoCapture on first
# open. Returns (duration_sec, width, height, fps) or None on any miss / error.
def get_cached_clip_metadata(file_path: str):
    try:
        cache_path = _get_cached_thumb_path(file_path)
    except OSError:
        return None
    return read_cached_metadata(_get_cached_duration_path(cache_path))


def _humanize_relative(ts: float) -> str:
    """Turn a file mtime into a 'X days ago' / 'just now' style string."""
    now = datetime.now()
    when = datetime.fromtimestamp(ts)
    delta = now - when
    secs = int(delta.total_seconds())
    if secs < 60:
        return 'just now'
    mins = secs // 60
    if mins < 60:
        return f'{mins} min ago'
    hours = mins // 60
    if hours < 24:
        return f'{hours} hr ago'
    days = hours // 24
    if days == 1:
        return 'yesterday'
    if days < 7:
        return f'{days} days ago'
    weeks = days // 7
    if weeks < 5:
        return f'{weeks} wk ago'
    months = days // 30
    if months < 12:
        return f'{months} mo ago'
    return f'{days // 365} yr ago'


def _section_label_for(ts: float) -> str:
    """Group label like 'TUE, APR 28' for grouping cards under date headers."""
    return datetime.fromtimestamp(ts).strftime('%a, %b %d').upper()


def _game_name_from_path(file_path: str) -> str:
    """Best-effort game / source name from the parent folder."""
    parent = os.path.basename(os.path.dirname(file_path))
    if parent in ('FTHR_Clips', '') or parent == os.path.basename(
            os.path.expanduser('~/FTHR_Clips')):
        return 'DESKTOP'
    return parent.upper()


def _clip_title_from_filename(file_path: str) -> str:
    """Friendly title — strip the timestamp tail from the filename."""
    base = os.path.splitext(os.path.basename(file_path))[0]
    # Drop trailing pattern like  *_clip_from_28Apr2026_22-30
    for suffix_marker in ('_clip_from_', '_screenshot_from_', '_from_'):
        idx = base.find(suffix_marker)
        if idx > 0:
            base = base[:idx]
            break
    base = base.replace('_', ' ').replace('-', ' ').strip()
    return base[:1].upper() + base[1:] if base else 'Clip'


class _ThumbnailSignals(QObject):
    finished = pyqtSignal(str, str, int)  # file_path, cache_path, duration_sec


class _ThumbnailWorker(QRunnable):
    """Generate and cache a video thumbnail off the main thread."""

    def __init__(self, file_path: str):
        super().__init__()
        self.file_path = file_path
        self.signals = _ThumbnailSignals()

    def run(self):
        cache_path = _get_cached_thumb_path(self.file_path)
        dur_path   = _get_cached_duration_path(cache_path)

        # Cache hit: image AND duration sidecar both exist.
        # This skips opening the video file entirely (cv2.VideoCapture +
        # FFmpeg demux is the expensive part — typically 20-100ms per file).
        if os.path.exists(cache_path) and os.path.exists(dur_path):
            duration = _read_cached_duration(dur_path)
            self.signals.finished.emit(self.file_path, cache_path, duration)
            return

        cap = cv2.VideoCapture(self.file_path)
        if not cap.isOpened():
            self.signals.finished.emit(self.file_path, '', 0)
            return

        ret, frame = cap.read()
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        duration = int(frames / fps) if fps > 0 else 0
        cap.release()

        if not ret or frame is None:
            self.signals.finished.emit(self.file_path, '', duration)
            return

        os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
        if not os.path.exists(cache_path):
            thumb = cv2.resize(frame, (_CARD_W, _THUMB_H))
            cv2.imwrite(cache_path, thumb, [cv2.IMWRITE_JPEG_QUALITY, 85])
        # Persist full metadata so ClipViewer can skip its own cv2.VideoCapture
        # on subsequent opens — this is what removes the first-launch lag for
        # clips that already appear on the grid.
        _write_cached_duration(dur_path, duration, width, height, fps)
        self.signals.finished.emit(self.file_path, cache_path, duration)


class _FileCollectSignals(QObject):
    # raw_files: set[str], sorted_pairs: list[(mtime, path)], imported: set[str], subdirs: list[str]
    finished = pyqtSignal(object, object, object, object)


class _FileCollectWorker(QRunnable):
    """Scans folders, applies filter, sorts by mtime — all off the main thread."""

    def __init__(self, clips_dir: str, import_dirs: list,
                 filter_: str, sort_: str):
        super().__init__()
        self.clips_dir   = clips_dir
        self.import_dirs = import_dirs
        self.filter_     = filter_
        self.sort_       = sort_
        self.signals     = _FileCollectSignals()

    def run(self):
        found:    set  = set()
        imported: set  = set()
        subdirs:  list = []

        try:
            entries = os.listdir(self.clips_dir)
        except OSError:
            entries = []
        for name in entries:
            full = os.path.join(self.clips_dir, name)
            if os.path.isfile(full) and name.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                found.add(full)
            elif os.path.isdir(full) and name not in _GRID_EXCLUDED_DIRS:
                subdirs.append(full)
                try:
                    for sub in os.listdir(full):
                        sf = os.path.join(full, sub)
                        if os.path.isfile(sf) and sub.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                            found.add(sf)
                except OSError:
                    pass

        for folder in self.import_dirs:
            if not os.path.isdir(folder):
                continue
            subdirs.append(folder)
            try:
                for name in os.listdir(folder):
                    full = os.path.join(folder, name)
                    if os.path.isfile(full) and name.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                        found.add(full)
                        imported.add(full)
                    elif os.path.isdir(full):
                        subdirs.append(full)
                        try:
                            for sub in os.listdir(full):
                                sf = os.path.join(full, sub)
                                if os.path.isfile(sf) and sub.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                                    found.add(sf)
                                    imported.add(sf)
                        except OSError:
                            pass
            except OSError:
                pass

        files: list = list(found)
        if self.filter_ == 'clips':
            files = [f for f in files if f.lower().endswith(_VIDEO_EXTS)]
        elif self.filter_ == 'screenshots':
            files = [f for f in files if f.lower().endswith(_IMAGE_EXTS)]
        elif self.filter_ == 'imported':
            files = [f for f in files if f in imported]

        # Bulk-stat here (off main thread) so the main thread never calls getmtime
        try:
            mtimes = {f: os.path.getmtime(f) for f in files}
        except OSError:
            mtimes = {}
        files.sort(key=lambda f: mtimes.get(f, 0.0), reverse=(self.sort_ != 'oldest'))
        pairs = [(mtimes.get(f, 0.0), f) for f in files]

        self.signals.finished.emit(found, pairs, imported, subdirs)


# ──────────────────────────────────────────────────────────────────────
# ClipThumbnail — Medal-style card: thumb on top, info row below
# ──────────────────────────────────────────────────────────────────────

class ClipThumbnail(QFrame):
    clicked          = pyqtSignal(str)
    opened           = pyqtSignal(str, QPixmap, QRect)   # video-only: path, thumb, global card rect
    deleted          = pyqtSignal(str)
    upload_requested = pyqtSignal(str)

    def __init__(self, file_path: str, is_video: bool = True, imported: bool = False,
                 upload_enabled: bool = False, uploaded: bool = False, parent=None):
        super().__init__(parent)
        self.file_path      = file_path
        self.is_video       = is_video
        self.imported       = imported
        self.upload_enabled = upload_enabled
        self.uploaded       = uploaded
        self.setObjectName('clipCard')
        self.setFixedSize(_CARD_W, _CARD_H)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._fade_anim: QPropertyAnimation | None = None
        self._setup_ui()
        if not is_video:
            self._load_image_thumbnail()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Thumbnail container ───────────────────────────────────────────
        thumb = QFrame(self)
        thumb.setObjectName('cardThumb')
        thumb.setFixedSize(_CARD_W, _THUMB_H)

        self.thumb_label = QLabel(thumb)
        self.thumb_label.setGeometry(0, 0, _CARD_W, _THUMB_H)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setScaledContents(True)
        self.thumb_label.setStyleSheet('background: transparent; border: none;')

        # Center play glyph (video only)
        if self.is_video:
            self._play_icon = QLabel('▶', thumb)
            self._play_icon.setGeometry(0, 0, _CARD_W, _THUMB_H)
            self._play_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._play_icon.setStyleSheet(
                'color: rgba(255,255,255,210); font-size: 36px;'
                ' background: transparent; border: none;'
            )
            self._play_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._play_icon.setVisible(False)

        # Top-right duration badge
        if self.is_video:
            self.duration_label = QLabel('0:00', thumb)
            self.duration_label.setObjectName('cardDurationBadge')
            self.duration_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.duration_label.setStyleSheet(
                f'QLabel#cardDurationBadge {{'
                f' color: {Colors.TEXT};'
                f' background: rgba(0,0,0,170);'
                f' border-radius: 0px; padding: 2px 9px;'
                f' font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_LABEL}px;'
                f' font-weight: bold; letter-spacing: 1px;'
                f'}}'
            )
            self.duration_label.adjustSize()
            self.duration_label.move(_CARD_W - self.duration_label.width() - 10, 10)

        # Bottom-left imported badge (only for clips from imported folders)
        self._imported_badge_h = 0
        if self.imported:
            imp = QLabel('IMPORTED', thumb)
            imp.setObjectName('cardImportedBadge')
            imp.setStyleSheet(
                f'QLabel#cardImportedBadge {{'
                f' color: {Colors.ACCENT};'
                f' background: rgba(0,0,0,180);'
                f' border-radius: 0px; padding: 2px 7px;'
                f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
                f' font-weight: bold; letter-spacing: 2px;'
                f'}}'
            )
            imp.adjustSize()
            imp.move(8, _THUMB_H - imp.height() - 8)
            imp.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._imported_badge_h = imp.height() + 4   # used to stack UPLOADED above it

        # "UPLOADED" badge — pre-created, shown/hidden dynamically
        self._upload_badge = QLabel('UPLOADED', thumb)
        self._upload_badge.setObjectName('cardUploadedBadge')
        self._upload_badge.setStyleSheet(
            f'QLabel#cardUploadedBadge {{'
            f' color: {Colors.BG};'
            f' background: rgba(0,170,0,210);'
            f' border-radius: 0px; padding: 2px 7px;'
            f' font-family: {Fonts.DISPLAY}; font-size: {Fonts.SIZE_MICRO}px;'
            f' font-weight: bold; letter-spacing: 2px;'
            f'}}'
        )
        self._upload_badge.adjustSize()
        _badge_bottom = _THUMB_H - 8 - self._imported_badge_h
        self._upload_badge.move(8, _badge_bottom - self._upload_badge.height())
        self._upload_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._upload_badge.setVisible(self.uploaded)

        layout.addWidget(thumb)

        # ── Card body (game / title / share+menu / time-ago) ─────────────
        body = QFrame(self)
        body.setObjectName('cardBody')
        body.setFixedSize(_CARD_W, _CARD_BODY_H)

        bl = QVBoxLayout(body)
        bl.setContentsMargins(12, 8, 8, 8)
        bl.setSpacing(2)

        # Top row: game name (small dim) + share + menu
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(6)

        self.game_label = QLabel(_game_name_from_path(self.file_path))
        self.game_label.setObjectName('cardGame')
        top_row.addWidget(self.game_label)
        top_row.addStretch(1)

        self.share_btn = QPushButton('⤴')
        self.share_btn.setObjectName('cardIconBtn')
        self.share_btn.setFixedSize(22, 22)
        self.share_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.share_btn.setToolTip('Share')
        self.share_btn.clicked.connect(self._on_share_click)
        top_row.addWidget(self.share_btn)

        self.menu_btn = QPushButton('⋯')
        self.menu_btn.setObjectName('cardIconBtn')
        self.menu_btn.setFixedSize(22, 22)
        self.menu_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_btn.clicked.connect(self._show_menu)
        top_row.addWidget(self.menu_btn)
        bl.addLayout(top_row)

        # Title row
        self.title_label = QLabel(_clip_title_from_filename(self.file_path))
        self.title_label.setObjectName('cardTitle')
        self.title_label.setWordWrap(False)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        bl.addWidget(self.title_label)

        # Time-ago row
        try:
            mtime = os.path.getmtime(self.file_path)
        except OSError:
            mtime = 0.0
        self.time_label = QLabel(_humanize_relative(mtime))
        self.time_label.setObjectName('cardTime')
        bl.addWidget(self.time_label)

        layout.addWidget(body)

        self.setStyleSheet(self._card_qss())

    @staticmethod
    def _card_qss() -> str:
        return f'''
            QFrame#clipCard {{
                background-color: {Colors.CARD_BG};
                border: 1px solid {Colors.CARD_BORDER};
                border-radius: {Sizes.RADIUS_CARD}px;
            }}
            QFrame#clipCard:hover {{
                background-color: {Colors.CARD_BG_HI};
                border-color: {Colors.BORDER_HI};
            }}
            QFrame#cardThumb {{
                background-color: {Colors.SURFACE_3};
                border: none;
                border-top-left-radius: {Sizes.RADIUS_CARD}px;
                border-top-right-radius: {Sizes.RADIUS_CARD}px;
            }}
            QFrame#cardBody {{
                background-color: transparent;
                border: none;
            }}
            QLabel#cardGame {{
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.DISPLAY};
                font-size: {Fonts.SIZE_MICRO}px;
                font-weight: bold;
                letter-spacing: 2px;
                background: transparent;
            }}
            QLabel#cardTitle {{
                color: {Colors.TEXT};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY_L}px;
                font-weight: bold;
                background: transparent;
            }}
            QLabel#cardTime {{
                color: {Colors.TEXT_MUTED};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_LABEL}px;
                background: transparent;
            }}
            QPushButton#cardIconBtn {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_DIM};
                font-size: 16px;
                font-weight: bold;
            }}
            QPushButton#cardIconBtn:hover {{
                color: {Colors.ACCENT};
            }}
        '''

    def set_uploaded(self, val: bool):
        self.uploaded = val
        self._upload_badge.setVisible(val)

    def refresh_theme(self):
        self.setStyleSheet(self._card_qss())

    def _on_share_click(self):
        # Share is wired through ClipViewer for now — open the viewer
        if self.is_video:
            px = self.thumb_label.pixmap() or QPixmap()
            global_rect = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
            self.opened.emit(self.file_path, px, global_rect)

    def _show_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(CONTEXT_MENU_QSS)
        open_act = menu.addAction('Open in viewer')
        explorer_act = menu.addAction('Show in Explorer')
        copy_act = menu.addAction('Copy path')
        if self.upload_enabled:
            menu.addSeparator()
            upload_act = menu.addAction('Upload')
        else:
            upload_act = None
        menu.addSeparator()
        del_act = menu.addAction('Delete')
        action = menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height())))
        if action == open_act:
            self._on_share_click()
        elif action == explorer_act:
            if sys.platform == 'win32':
                subprocess.Popen(f'explorer /select,"{self.file_path}"', shell=True)
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(self.file_path)])
        elif action == copy_act:
            QApplication.clipboard().setText(self.file_path)
        elif upload_act and action == upload_act:
            self.upload_requested.emit(self.file_path)
        elif action == del_act:
            self._confirm_delete()

    def _confirm_delete(self):
        reply = QMessageBox.question(
            self, 'Delete',
            f'Delete {os.path.basename(self.file_path)}?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            try:
                os.remove(self.file_path)
                self.deleted.emit(self.file_path)
            except OSError as e:
                QMessageBox.warning(self, 'Delete Failed', str(e))

    # ── Hover (large play glyph + slight border highlight) ───────────────

    def enterEvent(self, event):
        if self.is_video and hasattr(self, '_play_icon'):
            self._play_icon.setVisible(True)

    def leaveEvent(self, event):
        if self.is_video and hasattr(self, '_play_icon'):
            self._play_icon.setVisible(False)

    def contextMenuEvent(self, event):
        # Right-click anywhere on card — same options as the ⋯ button
        menu = QMenu(self)
        menu.setStyleSheet(CONTEXT_MENU_QSS)
        open_act = menu.addAction('Open in viewer')
        explorer_act = menu.addAction('Show in Explorer')
        copy_act = menu.addAction('Copy path')
        if self.upload_enabled:
            menu.addSeparator()
            upload_act = menu.addAction('Upload')
        else:
            upload_act = None
        menu.addSeparator()
        del_act = menu.addAction('Delete')
        action = menu.exec(event.globalPos())
        if action == open_act:
            self._on_share_click()
        elif action == explorer_act:
            if sys.platform == 'win32':
                subprocess.Popen(f'explorer /select,"{self.file_path}"', shell=True)
            else:
                subprocess.Popen(['xdg-open', os.path.dirname(self.file_path)])
        elif action == copy_act:
            QApplication.clipboard().setText(self.file_path)
        elif upload_act and action == upload_act:
            self.upload_requested.emit(self.file_path)
        elif action == del_act:
            self._confirm_delete()

    def fade_in(self, delay_ms: int = 0):
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)

        def _start():
            anim = QPropertyAnimation(effect, b'opacity', self)
            anim.setDuration(450)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda: self.setGraphicsEffect(None))
            self._fade_anim = anim
            anim.start()

        if delay_ms > 0:
            QTimer.singleShot(delay_ms, _start)
        else:
            _start()

    def _load_image_thumbnail(self):
        pixmap = QPixmap(self.file_path)
        self.thumb_label.setPixmap(
            pixmap.scaled(_CARD_W, _THUMB_H,
                          Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                          Qt.TransformationMode.SmoothTransformation))

    def set_video_thumbnail(self, cache_path: str, duration: int):
        if cache_path and os.path.exists(cache_path):
            pixmap = QPixmap(cache_path)
            self.thumb_label.setPixmap(
                pixmap.scaled(_CARD_W, _THUMB_H,
                              Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation))
        if self.is_video and hasattr(self, 'duration_label'):
            mins, secs = divmod(duration, 60)
            self.duration_label.setText(f'{mins}:{secs:02d}')
            self.duration_label.adjustSize()
            self.duration_label.move(_CARD_W - self.duration_label.width() - 10, 10)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            # Don't trigger card-open if the click was on a child button —
            # those handle their own actions. We can detect by checking the
            # local target widget.
            local = event.position().toPoint()
            child = self.childAt(local)
            if isinstance(child, QPushButton):
                return
            self.clicked.emit(self.file_path)
            if self.is_video:
                px = self.thumb_label.pixmap() or QPixmap()
                global_rect = QRect(self.mapToGlobal(QPoint(0, 0)), self.size())
                self.opened.emit(self.file_path, px, global_rect)


# ──────────────────────────────────────────────────────────────────────
# ClipGrid — filter bar + date sections + grid of cards
# ──────────────────────────────────────────────────────────────────────

class ClipGrid(QWidget):
    clip_clicked          = pyqtSignal(str)
    clip_opened           = pyqtSignal(str, QPixmap, QRect)
    screenshot_clicked    = pyqtSignal(str)
    clip_upload_requested = pyqtSignal(str)

    # Left+right margins from _setup_ui (28+28) — used for column calculation.
    _H_MARGIN = 56
    _GRID_SPACING = 18

    def __init__(self, settings_manager=None, parent=None):
        super().__init__(parent)
        self._sm              = settings_manager
        self._upload_checker  = None   # callable(path) -> bool
        self._upload_enabled  = None   # callable() -> bool
        self.clips_dir      = os.path.expanduser('~/FTHR_Clips')
        self.thumbnails     = []
        self._thumb_widgets = {}
        self._known_files   = set()
        self._imported_files: set = set()
        self._thread_pool   = QThreadPool()
        # Cap at 2 workers. cv2/ffmpeg thumbnail decode is already heavy on disk
        # and CPU; throwing 16 threads at it just thrashes and makes everything
        # slower. 400ms of profiling led me here. don't touch this.
        self._thread_pool.setMaxThreadCount(2)
        # Pre-compute the column count from the primary screen's available
        # width so the first paint already matches the maximized window.
        # Without this, the grid renders at 3 cols then snaps to N cols once
        # the resize event fires after showMaximized() — visible lag.
        self._current_columns = self._initial_columns_from_screen()

        self._filter = 'all'
        self._sort = 'newest'
        self._in_transition = False
        self._transition_anim = None

        # Watcher must exist before _load_clips() runs
        self._watcher = QFileSystemWatcher()
        if os.path.exists(self.clips_dir):
            self._watcher.addPath(self.clips_dir)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        self._setup_ui()
        self._load_clips()

        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._load_clips)

        # Debounced resize — only rebuilds the grid when columns would change.
        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self.refresh_timer = QTimer()
        self.refresh_timer.timeout.connect(self._load_clips)
        self.refresh_timer.start(30000)

    def set_upload_checker(self, checker):
        """checker(path: str) -> bool  — True if the clip has been uploaded."""
        self._upload_checker = checker

    def set_upload_enabled_checker(self, checker):
        """checker() -> bool  — True if uploads are enabled (shows Upload menu item)."""
        self._upload_enabled = checker

    def refresh_theme(self):
        for card in self._thumb_widgets.values():
            card.refresh_theme()

    def _viewport_width(self) -> int:
        """The scroll-area viewport is our parent — read its width so column
        calculation works even when the grid layout is wider than the viewport."""
        vp = self.parentWidget()
        return vp.width() if vp else self.width()

    def _initial_columns_from_screen(self) -> int:
        """Best-guess column count before the parent viewport has been laid out.

        Used at __init__ time so the first grid build matches the maximized
        window — avoids the ugly visible 3→N-column reflow on app startup where
        you watch the grid snap wider half a second after launch. small detail,
        but it's the kind of jank that makes an app feel cheap. narrator: it
        was not, in fact, fine without this."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return 3
        screen_w = screen.availableGeometry().width()
        available = screen_w - self._H_MARGIN
        cols = (available + self._GRID_SPACING) // (_CARD_W + self._GRID_SPACING)
        return max(3, int(cols))

    def _compute_columns(self) -> int:
        available = self._viewport_width() - self._H_MARGIN
        cols = (available + self._GRID_SPACING) // (_CARD_W + self._GRID_SPACING)
        return max(3, cols)

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_vp_filter', False):
            vp = self.parentWidget()
            if vp:
                vp.installEventFilter(self)
                self._vp_filter = True

    def eventFilter(self, obj, event):
        # Resize events fire like a machine gun while you drag a window edge.
        # Debounce so we only rebuild the grid 200ms after you STOP dragging,
        # not 60 times a second mid-drag. Your CPU thanks you.
        if event.type() == event.Type.Resize:
            self._resize_timer.start(200)
        return super().eventFilter(obj, event)

    def _on_resize_settled(self):
        new_cols = self._compute_columns()
        if new_cols != self._current_columns:
            self._current_columns = new_cols
            self._relayout_grids()

    def _on_dir_changed(self, path: str):
        self._debounce_timer.start(500)

    # ── UI ───────────────────────────────────────────────────────────────

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(28, 18, 28, 28)
        self.layout.setSpacing(0)

        # ── Filter bar ────────────────────────────────────────────────────
        filt = QFrame()
        filt.setObjectName('filterBar')
        filt.setFixedHeight(Sizes.FILTER_H)
        fl = QHBoxLayout(filt)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(12)

        self._all_count_label = QLabel('ALL CLIPS')
        self._all_count_label.setObjectName('allCountLabel')
        fl.addWidget(self._all_count_label)

        fl.addStretch(1)

        self.filter_combo = _DropdownCombo()
        self.filter_combo.addItems(['All clips', 'Clips only',
                                    'Screenshots', 'Imported clips'])
        self.filter_combo.setStyleSheet(COMBO_QSS)
        self.filter_combo.setMinimumWidth(120)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_combo)

        self.sort_combo = _DropdownCombo()
        self.sort_combo.addItems(['Newest', 'Oldest'])
        self.sort_combo.setStyleSheet(COMBO_QSS)
        self.sort_combo.setMinimumWidth(110)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        fl.addWidget(self.sort_combo)

        self.layout.addWidget(filt)
        self.layout.addSpacing(8)

        # ── Section host (date-grouped grids live here) ───────────────────
        self._sections_host = QWidget()
        self._sections_host.setStyleSheet('background: transparent;')
        self._sections_layout = QVBoxLayout(self._sections_host)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(20)
        self.layout.addWidget(self._sections_host)

        # ── Empty state ───────────────────────────────────────────────────
        self._empty_widget = QWidget()
        self._empty_widget.setStyleSheet('background: transparent;')
        ev_layout = QVBoxLayout(self._empty_widget)
        ev_layout.setContentsMargins(0, 96, 0, 0)
        ev_layout.setSpacing(0)
        ev_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._no_clips_lbl = QLabel('NO CLIPS YET')
        self._no_clips_lbl.setStyleSheet(label_display(Colors.TEXT_GHOST, Fonts.SIZE_H1, 8))
        self._no_clips_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev_layout.addWidget(self._no_clips_lbl)

        ev_layout.addSpacing(Sizes.SPACE_5)

        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(0, 0, 0, 0)
        mark_row.addStretch()
        mark = QFrame()
        mark.setFixedSize(56, 2)
        mark.setStyleSheet(f'background: {Colors.ACCENT}; border: none;')
        mark_row.addWidget(mark)
        mark_row.addStretch()
        ev_layout.addLayout(mark_row)

        ev_layout.addSpacing(Sizes.SPACE_5)

        hint_row = QHBoxLayout()
        hint_row.setContentsMargins(0, 0, 0, 0)
        hint_row.setSpacing(8)
        hint_row.addStretch()
        prefix = QLabel('press')
        prefix.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY_L))
        hint_row.addWidget(prefix)
        key_pill = QLabel('F9')
        key_pill.setStyleSheet(
            f'color: {Colors.TEXT}; background: {Colors.SURFACE_2};'
            f' border: 1px solid {Colors.BORDER_HI}; border-radius: 0px;'
            f' padding: 3px 10px;'
            f' font-family: {Fonts.BODY}; font-size: {Fonts.SIZE_BODY}px;'
            f' font-weight: bold; letter-spacing: 1px;'
        )
        hint_row.addWidget(key_pill)
        suffix = QLabel('to capture the last few seconds')
        suffix.setStyleSheet(label_body(Colors.TEXT_MUTED, Fonts.SIZE_BODY_L))
        hint_row.addWidget(suffix)
        hint_row.addStretch()
        ev_layout.addLayout(hint_row)

        self.layout.addWidget(self._empty_widget)
        self.layout.addStretch()

        self.setStyleSheet(self._grid_qss())

    @staticmethod
    def _grid_qss() -> str:
        return f'''
            QFrame#filterBar {{
                background: transparent;
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#allCountLabel {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: bold;
                font-size: {Fonts.SIZE_LABEL}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QPushButton#viewToggle, QPushButton#viewToggleActive {{
                background-color: {Colors.SURFACE_2};
                border: 1px solid {Colors.BORDER};
                border-radius: {Sizes.RADIUS_MD}px;
                color: {Colors.TEXT_DIM};
                font-size: 14px;
            }}
            QPushButton#viewToggle:hover, QPushButton#viewToggleActive:hover {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QPushButton#viewToggleActive {{
                color: {Colors.ACCENT};
                border-color: {Colors.ACCENT};
            }}
            QLabel.sectionHeader {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: bold;
                font-size: {Fonts.SIZE_BODY_L}px;
                letter-spacing: {Fonts.TRACK_LABEL}px;
                background: transparent;
            }}
            QLabel.sectionSub {{
                color: {Colors.TEXT_DIM};
                font-family: {Fonts.BODY};
                font-size: {Fonts.SIZE_BODY}px;
                background: transparent;
            }}
        '''

    # ── Filter / sort handlers ──────────────────────────────────────────

    def _on_filter_changed(self, idx: int):
        mapping = ['all', 'clips', 'screenshots', 'imported']
        self._filter = mapping[idx] if idx < len(mapping) else 'all'
        self._known_files = set()
        self._fade_out_then_reload()

    def _on_sort_changed(self, idx: int):
        self._sort = ['newest', 'oldest'][idx]
        self._known_files = set()
        self._fade_out_then_reload()

    def _fade_out_then_reload(self):
        """Fade out → background I/O → fade in. Never blocks the main thread."""
        if self._transition_anim is not None:
            self._transition_anim.stop()
            self._transition_anim = None

        self._in_transition = True
        # Sequence guard: if you spam the filter dropdown, multiple background
        # workers race. Each gets a seq number and only the latest one's result
        # is allowed to touch the UI — older ones finish and get thrown away.
        # Otherwise a slow earlier scan could overwrite a newer filter's results.
        self._transition_seq = getattr(self, '_transition_seq', 0) + 1
        seq = self._transition_seq

        def _launch_worker():
            import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
            worker = _FileCollectWorker(self.clips_dir, import_dirs, self._filter, self._sort)

            def _on_done(raw, pairs, imported, subdirs):
                if self._transition_seq == seq:
                    self._on_files_collected_for_transition(raw, pairs, imported, subdirs)

            worker.signals.finished.connect(_on_done)
            self._thread_pool.start(worker)

        if not self._sections_host.isVisible():
            _launch_worker()
            return

        out_effect = QGraphicsOpacityEffect(self._sections_host)
        self._sections_host.setGraphicsEffect(out_effect)

        out = QPropertyAnimation(out_effect, b'opacity', self)
        out.setDuration(140)
        out.setStartValue(1.0)
        out.setEndValue(0.0)
        out.setEasingCurve(QEasingCurve.Type.OutCubic)
        out.finished.connect(_launch_worker)
        out.start()
        self._transition_anim = out

    def _on_files_collected_for_transition(self, raw_files, sorted_pairs, imported_files, subdirs):
        """Runs on the main thread once the background worker finishes."""
        for path in subdirs:
            self._watch_subdir(path)

        self._known_files    = raw_files
        self._imported_files = imported_files

        self._clear_sections()
        self.thumbnails.clear()
        self._thumb_widgets.clear()

        if not sorted_pairs:
            self._show_empty(True)
            self._all_count_label.setText(self._filter_heading())
        else:
            self._show_empty(False)
            self._all_count_label.setText(f'{self._filter_heading()}  ({len(sorted_pairs)})')
            groups: dict = {}
            order:  list = []
            for mtime, f in sorted_pairs:
                key = _section_label_for(mtime)
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                groups[key].append(f)
            global_idx = 0
            for section_key in order:
                section_files = groups[section_key]
                self._add_section(section_key, section_files, global_idx)
                global_idx += len(section_files)

        # Fade back in
        in_effect = QGraphicsOpacityEffect(self._sections_host)
        in_effect.setOpacity(0.0)
        self._sections_host.setGraphicsEffect(in_effect)

        fade_in = QPropertyAnimation(in_effect, b'opacity', self)
        fade_in.setDuration(220)
        fade_in.setStartValue(0.0)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        def _done():
            self._sections_host.setGraphicsEffect(None)
            self._in_transition = False
            self._transition_anim = None

        fade_in.finished.connect(_done)
        fade_in.start()
        self._transition_anim = fade_in

    def _filter_heading(self) -> str:
        if self._filter == 'clips':
            return 'CLIPS'
        if self._filter == 'screenshots':
            return 'SCREENSHOTS'
        if self._filter == 'imported':
            return 'IMPORTED CLIPS'
        return 'ALL CLIPS'

    # ── Filesystem walking ──────────────────────────────────────────────

    def _collect_media_files(self) -> set:
        found = set()
        self._imported_files = set()

        # Primary FTHR_Clips folder
        try:
            entries = os.listdir(self.clips_dir)
        except OSError:
            entries = []
        for name in entries:
            full = os.path.join(self.clips_dir, name)
            if os.path.isfile(full):
                if name.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                    found.add(full)
            elif os.path.isdir(full) and name not in _GRID_EXCLUDED_DIRS:
                self._watch_subdir(full)
                try:
                    for sub in os.listdir(full):
                        sub_full = os.path.join(full, sub)
                        if os.path.isfile(sub_full) and \
                                sub.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                            found.add(sub_full)
                except OSError:
                    pass

        # Imported folders from settings
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        for folder in import_dirs:
            if not os.path.isdir(folder):
                continue
            self._watch_subdir(folder)
            try:
                for name in os.listdir(folder):
                    full = os.path.join(folder, name)
                    if os.path.isfile(full) and name.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                        found.add(full)
                        self._imported_files.add(full)
                    elif os.path.isdir(full):
                        self._watch_subdir(full)
                        try:
                            for sub in os.listdir(full):
                                sub_full = os.path.join(full, sub)
                                if os.path.isfile(sub_full) and \
                                        sub.lower().endswith(_VIDEO_EXTS + _IMAGE_EXTS):
                                    found.add(sub_full)
                                    self._imported_files.add(sub_full)
                        except OSError:
                            pass
            except OSError:
                pass

        return found

    def _watch_subdir(self, path: str):
        if path not in self._watcher.directories():
            self._watcher.addPath(path)

    # ── Build the date-grouped layout ───────────────────────────────────

    def _load_clips(self):
        if self._in_transition:
            return
        if not os.path.exists(self.clips_dir):
            os.makedirs(self.clips_dir)
            if self.clips_dir not in self._watcher.directories():
                self._watcher.addPath(self.clips_dir)
            self._show_empty(True)
            return

        current_files = self._collect_media_files()

        if current_files == self._known_files:
            return
        self._known_files = current_files

        self._clear_sections()
        self.thumbnails.clear()
        self._thumb_widgets.clear()

        # Apply filter
        files = list(current_files)
        if self._filter == 'clips':
            files = [f for f in files if f.lower().endswith(_VIDEO_EXTS)]
        elif self._filter == 'screenshots':
            files = [f for f in files if f.lower().endswith(_IMAGE_EXTS)]
        elif self._filter == 'imported':
            files = [f for f in files if f in self._imported_files]
        # 'all' shows everything

        # Sort
        if self._sort == 'oldest':
            files.sort(key=os.path.getmtime)
        elif self._sort == 'longest':
            # Without per-clip duration here we approximate by file size
            # (longer clips ≈ bigger files). Real duration comes via worker.
            files.sort(key=lambda f: os.path.getsize(f), reverse=True)
        else:
            files.sort(key=os.path.getmtime, reverse=True)

        if not files:
            self._show_empty(True)
            self._all_count_label.setText(self._filter_heading())
            return

        self._show_empty(False)
        self._all_count_label.setText(f'{self._filter_heading()}  ({len(files)})')

        # Group by date
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for f in files:
            try:
                key = _section_label_for(os.path.getmtime(f))
            except OSError:
                key = 'OTHER'
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(f)

        global_idx = 0
        for section_key in order:
            section_files = groups[section_key]
            self._add_section(section_key, section_files, global_idx)
            global_idx += len(section_files)

    def _add_section(self, header_text: str, files: list[str], starting_idx: int):
        """One block: [date header] + [game subtitle] + [grid of cards]."""
        section = QFrame()
        section.setStyleSheet('background: transparent;')
        sl = QVBoxLayout(section)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)

        # Header row: date + dropdown caret (visual)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(10)

        date_lbl = QLabel(f'{header_text}  ▾')
        date_lbl.setProperty('class', 'sectionHeader')
        date_lbl.setStyleSheet(
            f'color: {Colors.TEXT};'
            f' font-family: {Fonts.DISPLAY}; font-weight: bold;'
            f' font-size: {Fonts.SIZE_BODY_L}px;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'
        )
        hdr_row.addWidget(date_lbl)

        # Game/source name comes from the most-recent file's parent folder
        sample_game = _game_name_from_path(files[0]) if files else ''
        if sample_game:
            sub_lbl = QLabel(f'·  {sample_game}')
            sub_lbl.setStyleSheet(
                f'color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY};'
                f' font-size: {Fonts.SIZE_BODY}px; background: transparent;'
            )
            hdr_row.addWidget(sub_lbl)

        hdr_row.addStretch(1)
        sl.addLayout(hdr_row)

        cols = self._current_columns
        grid = QGridLayout()
        grid.setSpacing(self._GRID_SPACING)
        grid.setContentsMargins(0, 0, 0, 0)

        upload_enabled = bool(self._upload_enabled and self._upload_enabled())
        for i, fp in enumerate(files):
            is_video = fp.lower().endswith(_VIDEO_EXTS)
            uploaded = bool(self._upload_checker and self._upload_checker(fp))
            thumb = ClipThumbnail(
                fp, is_video=is_video,
                imported=fp in self._imported_files,
                upload_enabled=upload_enabled,
                uploaded=uploaded,
            )
            thumb.opened.connect(self.clip_opened.emit)
            thumb.clicked.connect(
                self.clip_clicked.emit if is_video else self.screenshot_clicked.emit)
            thumb.deleted.connect(self._on_clip_deleted)
            thumb.upload_requested.connect(self.clip_upload_requested.emit)
            grid.addWidget(thumb, i // cols, i % cols)
            self.thumbnails.append(thumb)
            self._thumb_widgets[fp] = thumb
            if not self._in_transition:
                thumb.fade_in(delay_ms=min((starting_idx + i) * 35, 600))

            if is_video:
                worker = _ThumbnailWorker(fp)
                worker.signals.finished.connect(self._on_thumb_ready)
                self._thread_pool.start(worker)

        sl.addLayout(grid)
        self._sections_layout.addWidget(section)

    def _relayout_grids(self):
        """Re-position existing cards into the new column count without
        destroying and recreating widgets.

        Key word: WITHOUT recreating. We could just nuke everything and rebuild
        on every resize, but that re-runs all the thumbnail workers and flickers
        the whole grid. Instead we yank each card out of the grid and drop it
        back at its new (row, col). Same widgets, new positions. works on my
        machine ✓ (and yours, hopefully)."""
        cols = self._current_columns
        for si in range(self._sections_layout.count()):
            section_widget = self._sections_layout.itemAt(si).widget()
            if section_widget is None:
                continue
            section_layout = section_widget.layout()
            if section_layout is None:
                continue
            for li in range(section_layout.count()):
                item = section_layout.itemAt(li)
                if item is None:
                    continue
                grid = item.layout()
                if not isinstance(grid, QGridLayout):
                    continue
                widgets = []
                while grid.count():
                    child = grid.takeAt(0)
                    w = child.widget()
                    if w:
                        widgets.append(w)
                for i, w in enumerate(widgets):
                    grid.addWidget(w, i // cols, i % cols)

    def force_refresh(self):
        """Clear the file cache and immediately reload — called when import folders change."""
        self._known_files = set()
        self._load_clips()

    def _clear_sections(self):
        while self._sections_layout.count():
            item = self._sections_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _show_empty(self, show: bool):
        if show:
            if self._filter == 'screenshots':
                self._no_clips_lbl.setText('NO SCREENSHOTS YET')
            elif self._filter == 'clips':
                self._no_clips_lbl.setText('NO CLIPS YET')
            elif self._filter == 'imported':
                self._no_clips_lbl.setText('NO IMPORTED CLIPS')
            else:
                self._no_clips_lbl.setText('NO CLIPS YET')
        self._empty_widget.setVisible(show)
        self._sections_host.setVisible(not show)

    def _on_clip_deleted(self, file_path: str):
        self._known_files = set()
        self._load_clips()

    def _on_thumb_ready(self, file_path: str, cache_path: str, duration: int):
        widget = self._thumb_widgets.get(file_path)
        if widget:
            widget.set_video_thumbnail(cache_path, duration)
