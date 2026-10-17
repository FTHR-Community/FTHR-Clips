# Responsive library grid grouped by date.
#
# Workers scan files and generate thumbnails; disk caches retain thumbnails
# and durations. Keep filesystem and decoding work off the Qt thread.

from ui.style import set_theme_style
from ui.motion import reduce_motion
from ui.image_memory import ImageLRU, start_image_cache_maintenance
import os
import subprocess
import threading
import hashlib
import math
import time
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QGridLayout,
    QMenu, QApplication,
    QPushButton, QSizePolicy, QLineEdit, QStackedWidget,
)
from PySide6.QtCore import (
    Qt, Signal, QTimer, QRunnable, QThreadPool, QObject, QEvent,
    QFileSystemWatcher, QRect, QPoint,
    QUrl, QSize, QRectF, QEasingCurve, QVariantAnimation,
)
from PySide6.QtGui import QDesktopServices, QPixmap, QPainter, QColor, QLinearGradient, QPainterPath, QPen
from PySide6.QtGui import QImage, QImageReader, QIcon, QFontMetrics, QPixmapCache

from core.clip_files import (
    IMAGE_SUFFIXES,
    VIDEO_SUFFIXES,
    is_completed_video_path,
    is_fthr_temporary_dir,
)
from core.library_index import (
    LibraryRecord, LibraryScanResult, canonical_media_path,
    scan_library,
)
from core.library_cache import maybe_prune_thumbnail_cache
from core.media_process import run_media_process as _run_owned_media_process, media_process_snapshot
from core.library_ownership import MediaOwnership, classify_media_path
from core.clip_mutations import renamed_clip_path, rename_clip
from core.audio_manifest import manifest_path_for
from core.theme_manager import ThemeManager
from core.media_metadata import parse_ffprobe_video_metadata
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.settings_manager import (
    StoragePathError, clips_directory_from, ensure_storage_directory,
)
from core.field_diagnostics import (
    emit_event, get_diagnostic_session, process_memory_bytes,
)
from ui.style import WheelSafeComboBox, paint_dropdown_arrow


class _DropdownCombo(WheelSafeComboBox):
    """QComboBox that shows icons/dropdown.png as its arrow, rotated when open."""

    _custom_arrow_managed = True

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
        p = QPainter(self)
        paint_dropdown_arrow(p, self.rect(), self._popup_open)
        p.end()

from ui.style import (
    Colors, Fonts, Sizes,
    label_display,
    context_menu_qss, combo_qss,
)
from ui.dialogs import FthrMessageDialog


THUMB_CACHE_DIR = os.path.join(os.path.expanduser('~'), '.fthr', 'thumbnails')

# These folders hold processed/shared clips — not shown in the main library grid
_GRID_EXCLUDED_DIRS = {'Exported', 'Shared'}
_VIDEO_EXTS = VIDEO_SUFFIXES
_IMAGE_EXTS = IMAGE_SUFFIXES


def _rgba(hex_color: str, alpha: int) -> str:
    """Turn a theme hex token into a QSS rgba color with the given alpha."""
    color = QColor(hex_color)
    return f'rgba({color.red()},{color.green()},{color.blue()},{alpha})'


def _is_grid_excluded_dir(path: str) -> bool:
    """Keep app-owned post-processing directories out of the library scan."""

    name = os.path.basename(os.fspath(path))
    return name in _GRID_EXCLUDED_DIRS or is_fthr_temporary_dir(name)


def _show_in_file_manager(file_path: str) -> None:
    """Open the containing folder and highlight ``file_path`` when possible."""

    path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
    subprocess.Popen(f'explorer.exe /select,"{path}"')
    return



# Card geometry
# Cards flex with the viewport. The thumbnail *surface* remains 16:9, while
# source pixels fill it with a centered crop, preserving their aspect ratio.
_CARD_W      = 360  # preferred / cache sizing reference
_CARD_MIN_W  = 300
_CARD_MAX_W  = 440
_THUMB_H     = 180
_CARD_BODY_H = 88
_CARD_H      = _THUMB_H + _CARD_BODY_H
_MAX_MATERIALIZED_CARDS = 96
_VIRTUAL_OVERSCAN_ROWS = 2
_MAX_THUMBNAIL_QUEUE = 128
_NEGATIVE_CACHE_SECONDS = 30.0
def _get_cached_thumb_path(file_path: str) -> str:
    """Return a cache path based on file path + mtime so stale caches auto-invalidate.

    Including mtime in the hash invalidates the cache automatically when a file
    is overwritten without requiring a separate cache index.
    """
    info = os.stat(file_path)
    # v4 caches preserve source aspect ratio through FFmpeg scaling. The format
    # here prevents an older, force-stretched 16:9 thumbnail from surviving an
    # application update.
    key = f'{canonical_media_path(file_path)}|{info.st_size}|{info.st_mtime_ns}|aspect-v4'.encode(
        'utf-8', errors='surrogateescape')
    return os.path.join(THUMB_CACHE_DIR, hashlib.md5(key).hexdigest() + '.jpg')


def _get_cached_duration_path(thumb_path: str) -> str:
    """Sidecar storing authoritative clip metadata for thumbnail/viewer reuse.

    The versioned filename invalidates old OpenCV-derived FPS caches.
    Format: ``duration width height fps video_bitrate total_bitrate``.
    """
    return thumb_path[:-4] + '.meta-v3'


def _get_negative_cache_path(thumb_path: str) -> str:
    """Fingerprint-bound marker for an unchanged media probe failure."""
    return thumb_path[:-4] + '.failed'


def _has_recent_probe_failure(path: str) -> bool:
    try:
        age = time.time() - os.path.getmtime(path)
        return 0 <= age < _NEGATIVE_CACHE_SECONDS
    except OSError:
        # A missing/unreadable optional marker must not suppress a fresh probe.
        return False


def _read_cached_duration(dur_path: str) -> int:
    """Backwards-compatible reader that returns just the duration."""
    meta = read_cached_metadata(dur_path)
    return int(meta[0]) if meta else 0


def read_cached_metadata(dur_path: str):
    """Return factual cached metadata, or ``None`` when unavailable."""
    try:
        with open(dur_path, 'r') as f:
            parts = f.read().strip().split()
        if not parts:
            return None
        duration = float(parts[0])
        width    = int(parts[1]) if len(parts) > 1 else 0
        height   = int(parts[2]) if len(parts) > 2 else 0
        fps      = float(parts[3]) if len(parts) > 3 else 0.0
        video_bitrate = int(parts[4]) if len(parts) > 4 else 0
        total_bitrate = int(parts[5]) if len(parts) > 5 else 0
        if (not math.isfinite(duration) or duration <= 0
                or width <= 0 or height <= 0 or not math.isfinite(fps)
                or fps < 0 or video_bitrate < 0 or total_bitrate < 0):
            return None
        return (duration, width, height, fps,
                video_bitrate, total_bitrate)
    except (OSError, ValueError):
        # A corrupt/missing sidecar is a cache miss and will be probed again.
        return None


def _write_cached_duration(dur_path: str, duration: float,
                            width: int = 0, height: int = 0, fps: float = 0.0,
                            video_bitrate: int = 0,
                            total_bitrate: int = 0):
    """Persist factual FFprobe metadata so ClipViewer can skip a second probe."""
    try:
        temp_path = dur_path + f'.tmp-{os.getpid()}-{threading.get_ident()}'
        with open(temp_path, 'w') as f:
            f.write(
                f'{float(duration):.6f} {int(width)} {int(height)} '
                f'{float(fps):.6f} {int(video_bitrate)} {int(total_bitrate)}')
        os.replace(temp_path, dur_path)
    except OSError:
        try:
            os.unlink(temp_path)
        except (OSError, UnboundLocalError):
            # A failed optional cache write may leave no temporary file to remove.
            pass


# Public lookup used by ClipViewer to avoid blocking a decoder on first
# open. Returns (duration_sec, width, height, fps) or None on any miss / error.
def get_cached_clip_metadata(file_path: str):
    try:
        cache_path = _get_cached_thumb_path(file_path)
    except OSError:
        # A removed source has no cache identity; callers can show the placeholder.
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
    """Readable date label for grouping cards under date headers."""
    return datetime.fromtimestamp(ts).strftime('%a, %b %d')


def _game_name_from_path(file_path: str, clips_root: str | None = None) -> str:
    """Best-effort game / source name from the parent folder."""
    parent = os.path.basename(os.path.dirname(file_path))
    root_name = os.path.basename(os.path.normpath(
        clips_root or os.path.expanduser('~/FTHR_Clips')))
    if parent in (root_name, ''):
        return 'Desktop'
    return parent


def _clip_title_from_filename(file_path: str) -> str:
    """Display the actual editable filename, without its media extension."""
    return Path(file_path).stem


class _ThumbnailSignals(QObject):
    finished = Signal(str, str, int)  # file_path, cache_path, duration_sec
    diagnostic_finished = Signal(int, int, bool)  # total_ms, metadata_ms, cache_hit


def _valid_cached_thumbnail(path: str) -> bool:
    """Validate a JPEG header/decode before treating a cache entry as a hit."""
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        return not image.isNull() and image.width() > 0 and image.height() > 0
    except (OSError, RuntimeError):
        # A damaged cached JPEG is a miss, never a reason to reject the clip.
        return False


def _probe_with_owned_process(
        file_path: str, cancel_event: threading.Event):
    try:
        probe = get_ffprobe_exe()
    except FFmpegUnavailable:
        # Missing FFprobe only disables enrichment; opening uses the original media.
        return None
    result = _run_owned_media_process(
        [probe, '-v', 'error', '-show_entries',
         'stream=index,codec_type,width,height,avg_frame_rate,'
         'r_frame_rate,duration,bit_rate:'
         'format_tags=fthr_frame_rate,fthr_video_bitrate_bps:'
         'format=duration,size,bit_rate', '-of', 'json', file_path],
        cancel_event)
    if result is None or result[0] != 0:
        return None
    try:
        size = Path(file_path).stat().st_size
    except OSError:
        size = None
    return parse_ffprobe_video_metadata(
        result[1].decode('utf-8', errors='replace'), file_size=size)


def _decode_thumbnail_with_owned_process(
        file_path: str, cancel_event: threading.Event) -> QImage | None:
    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable:
        # Missing FFmpeg only disables the thumbnail, not access to the clip.
        return None
    result = _run_owned_media_process(
        [ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', '0',
         '-i', file_path, '-frames:v', '1', '-vf',
         "scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease",
         '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
        cancel_event)
    if result is None or result[0] != 0 or not result[1]:
        return None
    image = QImage.fromData(result[1], 'JPEG')
    if image.isNull():
        return None
    return image


class _ThumbnailWorker(QRunnable):
    """Generate and cache a video thumbnail off the main thread."""

    def __init__(self, file_path: str, cancel_event=None):
        super().__init__()
        self.file_path = file_path
        self.cancel_event = cancel_event or threading.Event()
        self.signals = _ThumbnailSignals()

    def run(self):
        started = time.monotonic()
        metadata_ms = 0
        cache_hit = False
        cache_path = ''
        temp_path = ''
        try:
            if not is_completed_video_path(self.file_path):
                self.signals.finished.emit(self.file_path, '', 0)
                return
            if self.cancel_event.is_set():
                return
            cache_path = _get_cached_thumb_path(self.file_path)
            dur_path   = _get_cached_duration_path(cache_path)
            failed_path = _get_negative_cache_path(cache_path)

            # Cache hit: a valid JPEG AND factual metadata sidecar both exist.
            # Fingerprinting in _get_cached_thumb_path means mutations select a
            # new generation; corrupt entries never poison the next run.
            if (os.path.exists(cache_path) and os.path.exists(dur_path)
                    and read_cached_metadata(dur_path) is not None
                    and _valid_cached_thumbnail(cache_path)):
                cache_hit = True
                duration = _read_cached_duration(dur_path)
                self.signals.finished.emit(self.file_path, cache_path, duration)
                return
            if os.path.exists(cache_path) and not _valid_cached_thumbnail(cache_path):
                # Never retain a corrupt generation just because its filename
                # still matches the current source fingerprint.
                try:
                    os.unlink(cache_path)
                except OSError:
                    pass
            if _has_recent_probe_failure(failed_path):
                # A decoder timeout or temporarily unavailable tool says
                # nothing permanent about the media. Retry unchanged files
                # after a short cooldown, including markers from older builds.
                self.signals.finished.emit(
                    self.file_path, '', _read_cached_duration(dur_path))
                return
            metadata_started = time.monotonic()
            probed = _probe_with_owned_process(
                self.file_path, self.cancel_event)
            metadata_ms = round((time.monotonic() - metadata_started) * 1000)
            if self.cancel_event.is_set():
                return
            duration = (probed.duration_seconds
                        if probed and probed.duration_seconds is not None else 0.0)
            width = probed.width if probed and probed.width else 0
            height = probed.height if probed and probed.height else 0
            fps = probed.average_fps if probed and probed.average_fps else 0.0
            video_bitrate = (
                probed.video_bitrate_bps if probed and probed.video_bitrate_bps else 0)
            total_bitrate = (
                probed.total_bitrate_bps if probed and probed.total_bitrate_bps else 0)

            # Metadata is useful even if thumbnail decoding later fails.
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            if probed is not None:
                _write_cached_duration(
                    dur_path, duration, width, height, fps,
                    video_bitrate, total_bitrate)

            image = _decode_thumbnail_with_owned_process(
                self.file_path, self.cancel_event)
            if image is None:
                if self.cancel_event.is_set():
                    return
                try:
                    Path(failed_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(failed_path).touch()
                except OSError:
                    pass
                self.signals.finished.emit(self.file_path, '', int(duration))
                return

            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            if not os.path.exists(cache_path):
                # QImage decode/scaling stays on this QRunnable. Only the
                # thumbnail-sized JPEG reaches the UI and cache.
                temp_path = cache_path + f'.tmp-{os.getpid()}-{threading.get_ident()}.jpg'
                if image.save(temp_path, 'JPG', 85):
                    os.replace(temp_path, cache_path)
                else:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            # Persist full metadata so ClipViewer can skip its own cv2.VideoCapture
            # on subsequent opens — this is what removes the first-launch lag for
            # clips that already appear on the grid.
            self.signals.finished.emit(
                self.file_path, cache_path, int(duration))
        except Exception as error:
            # Thumbnail/metadata enrichment must never invalidate a clip that
            # was already transactionally published by the engine.
            if self.cancel_event.is_set():
                return
            emit_event(
                'library', 'thumbnail_generation_failed', state='FAILED',
                detail=f'{type(error).__name__}: {error}')
            try:
                if cache_path:
                    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
                    Path(_get_negative_cache_path(cache_path)).touch()
            except (OSError, UnboundLocalError):
                pass
            self.signals.finished.emit(self.file_path, '', 0)
        finally:
            try:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                maybe_prune_thumbnail_cache(THUMB_CACHE_DIR)
            except Exception as error:
                # Cache maintenance is optional; it must never prevent the
                # terminal diagnostic signal that releases the job owner.
                emit_event(
                    'library', 'thumbnail_cache_maintenance_failed',
                    state='FAILED',
                    detail=f'{type(error).__name__}: {error}')
            finally:
                self.signals.diagnostic_finished.emit(
                    round((time.monotonic() - started) * 1000),
                    metadata_ms, cache_hit)


class _ImageThumbnailSignals(QObject):
    finished = Signal(str, object)  # file_path, thumbnail-sized QImage
    diagnostic_finished = Signal(int, int, bool)


class _ImageThumbnailWorker(QRunnable):
    """Decode screenshots with QImageReader away from the Qt GUI thread."""

    def __init__(self, file_path: str, cancel_event=None):
        super().__init__()
        self.file_path = file_path
        self.cancel_event = cancel_event or threading.Event()
        self.signals = _ImageThumbnailSignals()

    def run(self):
        started = time.monotonic()
        cache_hit = False
        cache_path = ''
        temp_path = ''
        try:
            cache_path = _get_cached_thumb_path(self.file_path)
            failed_path = _get_negative_cache_path(cache_path)
            if self.cancel_event.is_set():
                return
            if os.path.exists(cache_path) and _valid_cached_thumbnail(cache_path):
                cache_hit = True
                reader = QImageReader(cache_path)
                reader.setAutoTransform(True)
                image = reader.read()
                self.signals.finished.emit(self.file_path, image)
                return
            if os.path.exists(cache_path):
                try:
                    os.unlink(cache_path)
                except OSError:
                    pass
            if _has_recent_probe_failure(failed_path):
                self.signals.finished.emit(self.file_path, QImage())
                return
            reader = QImageReader(self.file_path)
            reader.setAutoTransform(True)
            size = reader.size()
            if size.isValid() and (size.width() > 640 or size.height() > 360):
                size.scale(QSize(640, 360), Qt.AspectRatioMode.KeepAspectRatio)
                reader.setScaledSize(size)
            image = reader.read()
            if self.cancel_event.is_set():
                return
            if image.isNull():
                raise ValueError('image decoder returned an empty image')
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path + f'.tmp-{os.getpid()}-{threading.get_ident()}.jpg'
            if not image.save(temp_path, 'JPG', 85):
                raise OSError('thumbnail cache write failed')
            os.replace(temp_path, cache_path)
            self.signals.finished.emit(self.file_path, image)
        except Exception as error:
            if self.cancel_event.is_set():
                return
            emit_event(
                'library', 'thumbnail_generation_failed', state='FAILED',
                detail=f'{type(error).__name__}: {error}')
            try:
                if cache_path:
                    Path(_get_negative_cache_path(cache_path)).parent.mkdir(
                        parents=True, exist_ok=True)
                    Path(_get_negative_cache_path(cache_path)).touch()
            except (OSError, UnboundLocalError):
                pass
            self.signals.finished.emit(self.file_path, QImage())
        finally:
            try:
                if temp_path:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
                if cache_path:
                    maybe_prune_thumbnail_cache(THUMB_CACHE_DIR)
            except Exception as error:
                # Cache maintenance is optional; it must not strand the
                # in-flight owner by suppressing the terminal signal.
                emit_event(
                    'library', 'thumbnail_cache_maintenance_failed',
                    state='FAILED',
                    detail=f'{type(error).__name__}: {error}')
            finally:
                self.signals.diagnostic_finished.emit(
                    round((time.monotonic() - started) * 1000), 0, cache_hit)


class _FileCollectSignals(QObject):
    # raw_files: set[str], sorted_pairs: list[(mtime, path)], imported: set[str], subdirs: list[str]
    finished = Signal(object, object, object, object)
    # generation, bounded discovery snapshot.  The final ``finished`` result
    # remains authoritative for global ordering and overlap deduplication.
    batch = Signal(int, object)


class _FileCollectWorker(QRunnable):
    """Scans folders, applies filter, sorts by mtime — all off the main thread."""

    def __init__(self, clips_dir: str, import_dirs: list,
                 filter_: str, sort_: str, cancel_event=None):
        super().__init__()
        self.clips_dir   = clips_dir
        self.import_dirs = import_dirs
        self.filter_     = filter_
        self.sort_       = sort_
        self.cancel_event = cancel_event or threading.Event()
        self.result: LibraryScanResult | None = None
        self.signals     = _FileCollectSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self):
        generation = int(getattr(self, 'generation', 0))
        self.result = scan_library(
            self.clips_dir,
            self.import_dirs,
            filter_=self.filter_,
            sort_=self.sort_,
            cancel_event=self.cancel_event,
            on_batch=lambda records: self.signals.batch.emit(
                generation, records),
        )
        records = self.result.records
        found = {record.path for record in self.result.all_records}
        imported = {record.path for record in self.result.all_records
                    if record.imported}
        pairs = [(record.mtime, record.path) for record in records]
        self.signals.finished.emit(
            found, pairs, imported, list(self.result.watched_directories))


# ClipThumbnail — Medal-style card: thumb on top, info row below

class _RenameLabel(QLabel):
    rename_requested = Signal()

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self.setMinimumWidth(0)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_full_text(self, text):
        self._full_text = text
        self.setToolTip(text + '\nDouble-click to rename; F2 also works')
        self._elide()

    def _elide(self):
        super().setText(QFontMetrics(self.font()).elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(1, self.width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.rename_requested.emit()
            event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F2:
            self.rename_requested.emit()
        else:
            super().keyPressEvent(event)


class _RenameEdit(QLineEdit):
    cancelled = Signal()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class _CardShareButton(QPushButton):
    """Animate the action's colors directly, independent of snapshot overlays."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty('nativeHoverAnimation', True)
        self.action_name = 'upload'
        self._hover_progress = 0.0
        self._hover_animation = QVariantAnimation(self)
        self._hover_animation.setDuration(120)
        self._hover_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._hover_animation.valueChanged.connect(self._advance_hover)

    def _advance_hover(self, progress):
        self._hover_progress = progress
        self.update()

    def _animate_hover(self, target):
        self._hover_animation.stop()
        if reduce_motion(self):
            self._advance_hover(target)
            return
        self._hover_animation.setStartValue(self._hover_progress)
        self._hover_animation.setEndValue(target)
        self._hover_animation.start()

    def enterEvent(self, event):
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def hideEvent(self, event):
        self._hover_animation.stop()
        self._hover_progress = 0.0
        self.setProperty('actionHovered', False)
        super().hideEvent(event)

    def paintEvent(self, event):
        def blend(start, end):
            first, last = QColor(start), QColor(end)
            return QColor.fromRgbF(*[
                a + (b - a) * self._hover_progress
                for a, b in zip(first.getRgbF(), last.getRgbF(), strict=True)])

        background = blend(Colors.ACCENT, Colors.ACCENT_SOFT)
        tint = blend(Colors.BG, Colors.ACCENT)
        border = QColor(Colors.ACCENT)
        if not self.isEnabled():
            background, tint, border = map(QColor, (Colors.SURFACE_3, Colors.TEXT_MUTED, Colors.BORDER))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(background)
        painter.setPen(QPen(QColor(Colors.TEXT) if self.hasFocus() else border,
                            2 if self.hasFocus() else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 4, 4)
        size = self.iconSize()
        icon_rect = QRect(QPoint((self.width() - size.width()) // 2,
                                (self.height() - size.height()) // 2), size)
        _card_icon(self.action_name, tint.name()).paint(painter, icon_rect)


class _WorkerCancellation:
    """Cancel one decode without cancelling the other visible thumbnails."""
    def __init__(self, parent_event):
        self.parent_event = parent_event
        self.local_event = threading.Event()

    def set(self):
        self.local_event.set()

    def is_set(self):
        return self.local_event.is_set() or self.parent_event.is_set()

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while not self.is_set():
            remaining = deadline - time.monotonic() if deadline is not None else 0.05
            if remaining <= 0:
                return False
            self.local_event.wait(min(0.05, remaining))
        return True


def _card_icon(name: str, color: str, size: int = 28) -> QIcon:
    theme = ThemeManager()
    custom = theme.get_custom_icon_path(name + '.png')
    return _render_card_icon(name, color, size, str(custom) if custom else '')


@lru_cache(maxsize=128)
def _render_card_icon(name: str, color: str, size: int, custom: str) -> QIcon:
    from ui.icons import icon_pixmap
    return QIcon(icon_pixmap(name + '.png', size, color, custom_path=custom))


class ClipThumbnail(QFrame):
    clicked = Signal(str)
    opened = Signal(str, QPixmap, QRect)
    deleted = Signal(str)
    renamed = Signal(str, str)
    upload_requested = Signal(str)

    def __init__(self, file_path: str, is_video: bool = True, imported: bool = False,
                 upload_enabled: bool = False, uploaded: bool = False,
                 upload_info: dict | None = None, ready: bool = True,
                 card_width: int = _CARD_W, parent=None,
                 clips_root: str | None = None, defer_image_load: bool = False):
        super().__init__(parent)
        self.file_path, self.is_video, self.imported = file_path, is_video, imported
        self.upload_enabled, self.uploaded = upload_enabled, uploaded
        self.upload_info = dict(upload_info or {})
        self.upload_link = ''
        self.ready = ready
        self._clips_root = clips_root or os.path.expanduser('~/FTHR_Clips')
        self._thumbnail_ready = False
        self._source_duration = 0.0
        self._duration_provider = None
        self._thumbnail_fingerprint = ''
        self._thumb_pixmap = QPixmap()
        self._card_width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(card_width)))
        self._thumb_height = round(self._card_width * 9 / 16)
        self._mutation_busy = False
        self._editing_name = False
        self._prepare_mutation = None
        self._finish_mutation = None
        self._wait_for_readers = None
        self._mutation_timer = QTimer(self)
        self._mutation_timer.setSingleShot(True)
        self._mutation_timer.timeout.connect(self._try_mutation)
        self._upload_status = ''
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(self._clear_action_feedback)
        self.setObjectName('clipCard')
        self.setProperty('nativeHoverAnimation', True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._setup_ui()
        self.resize_card(self._card_width, force=True)
        self.set_ready(ready)
        if not is_video and not defer_image_load:
            self._load_image_thumbnail()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._thumb_frame = QFrame(self)
        self._thumb_frame.setObjectName('cardThumb')
        self._thumb_frame.setCursor(Qt.CursorShape.PointingHandCursor)
        self._thumb_frame.mousePressEvent = self._on_thumbnail_click
        self.thumb_label = QLabel(self._thumb_frame)
        self.thumb_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.thumb_label.setStyleSheet('background: transparent; border: none;')
        if self.is_video:
            self._play_icon = QLabel(self._thumb_frame)
            self._play_icon.setPixmap(_card_icon('play', Colors.TEXT, 36).pixmap(36, 36))
            self._play_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._play_icon.setStyleSheet('background: transparent; border: none;')
            self._play_icon.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._play_icon.hide()
            self.duration_label = QLabel('0:00', self._thumb_frame)
            self.duration_label.setObjectName('cardDurationBadge')
            self.duration_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._imported_badge = QLabel('Imported', self._thumb_frame)
        self._imported_badge.setObjectName('cardImportedBadge')
        self._imported_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._imported_badge.setVisible(self.imported)
        self._upload_badge = QLabel('Uploaded', self._thumb_frame)
        self._upload_badge.setObjectName('cardUploadedBadge')
        self._upload_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._upload_badge.setVisible(self.uploaded)
        self._action_feedback = QLabel(self._thumb_frame)
        self._action_feedback.setObjectName('cardActionFeedback')
        self._action_feedback.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._action_feedback.hide()
        self._finalizing_badge = None
        layout.addWidget(self._thumb_frame)

        self._body_frame = QFrame(self)
        self._body_frame.setObjectName('cardBody')
        body = QHBoxLayout(self._body_frame)
        body.setContentsMargins(10, 10, 10, 10)
        body.setSpacing(8)
        info = QVBoxLayout()
        info.setSpacing(3)
        self.title_label = _RenameLabel(_clip_title_from_filename(self.file_path))
        self.title_label.setObjectName('cardTitle')
        self.title_label.set_full_text(_clip_title_from_filename(self.file_path))
        self.title_label.rename_requested.connect(self._begin_rename)
        self.title_edit = _RenameEdit()
        self.title_edit.setObjectName('cardRename')
        self.title_edit.setToolTip('Enter to save; Escape to cancel')
        self.title_edit.setAccessibleName('Clip filename')
        self.title_edit.editingFinished.connect(self._commit_rename)
        self.title_edit.cancelled.connect(self._cancel_rename)
        self._title_stack = QStackedWidget()
        self._title_stack.setMinimumWidth(0)
        self._title_stack.setFixedHeight(32)
        self._title_stack.addWidget(self.title_label)
        self._title_stack.addWidget(self.title_edit)
        info.addWidget(self._title_stack)
        self.metadata_label = QLabel('Reading clip details…' if self.is_video else 'Screenshot')
        self.metadata_label.setObjectName('cardMetadata')
        self.metadata_label.setMinimumWidth(0)
        self.metadata_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        info.addWidget(self.metadata_label)
        body.addLayout(info, 1)
        self._link_actions = QWidget(self._body_frame)
        actions = QHBoxLayout(self._link_actions)
        actions.setContentsMargins(0, 0, 0, 0)
        self.share_btn = _CardShareButton()
        self.share_btn.setObjectName('cardShareBtn')
        self.share_btn.setFixedSize(48, 52)
        self.share_btn.setIconSize(QSize(28, 28))
        self.share_btn.clicked.connect(self._on_share_click)
        self.share_btn.installEventFilter(self)
        actions.addWidget(self.share_btn)
        # The single visible action becomes Copy link after upload.
        self.copy_link_btn = self.share_btn
        self.open_link_btn = QPushButton(self)
        self.open_link_btn.hide()
        self.open_link_btn.clicked.connect(self._open_upload_link)
        body.addWidget(self._link_actions)
        self.delete_btn = QPushButton()
        self.delete_btn.setObjectName('cardDeleteBtn')
        self.delete_btn.setFixedSize(48, 52)
        self.delete_btn.setIconSize(QSize(28, 28))
        self.delete_btn.setToolTip('Delete clip' if not self.imported else 'Delete the linked original file')
        self.delete_btn.setAccessibleName('Delete clip')
        self.delete_btn.clicked.connect(lambda: self._confirm_delete(allow_linked=True))
        self.menu_btn = self.delete_btn  # legacy accessor
        body.addWidget(self.delete_btn)
        layout.addWidget(self._body_frame)
        self.refresh_theme()
        self.set_upload_info(self.upload_info)
        for button in (self.share_btn, self.delete_btn):
            button.setCursor(Qt.CursorShape.PointingHandCursor)

    @staticmethod
    def _card_qss():
        return f'''
            QFrame#clipCard {{ background: {Colors.CARD_BG}; border: none; border-radius: {Sizes.RADIUS_CARD}px; }}
            QFrame#cardThumb {{ background: {Colors.CARD_BG}; border: none; border-top-left-radius: {Sizes.RADIUS_CARD}px; border-top-right-radius: {Sizes.RADIUS_CARD}px; }}
            QFrame#cardBody, QStackedWidget {{ background: transparent; border: none; }}
            QLabel {{ background: transparent; border: none; }}
            QLabel#cardTitle {{ color: {Colors.TEXT}; font-family: {Fonts.BODY}; font-size: 15px; font-weight: 600; letter-spacing: 0px; }}
            QLabel#cardTitle:focus {{ color: {Colors.ACCENT}; }}
            QLabel#cardMetadata {{ color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY}; font-size: 12px; font-weight: 400; }}
            QLineEdit#cardRename {{ background: {Colors.SURFACE_1}; color: {Colors.TEXT}; border: 1px solid {Colors.ACCENT}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 17px; padding: 0 3px; }}
            QPushButton#cardShareBtn {{ background: {Colors.ACCENT}; color: {Colors.BG}; border: 1px solid {Colors.ACCENT}; border-radius: 4px; }}
            QPushButton#cardShareBtn:hover, QPushButton#cardShareBtn:pressed, QPushButton#cardShareBtn[actionHovered="true"] {{ color: {Colors.ACCENT}; border: 1px solid {Colors.ACCENT}; background: {Colors.ACCENT_SOFT}; }}
            QPushButton#cardDeleteBtn {{ background: {Colors.DELETE}; color: {Colors.TEXT}; border: 1px solid {Colors.DELETE}; border-radius: 4px; }}
            QPushButton#cardDeleteBtn:hover {{ border-color: {Colors.ERROR}; background: {Colors.ERROR_SOFT}; }}
            QPushButton:focus {{ border: 2px solid {Colors.TEXT}; }}
            QPushButton:disabled {{ background: {Colors.SURFACE_3}; border-color: {Colors.BORDER}; }}
            QLabel#cardDurationBadge {{ color: {Colors.TEXT}; background: {_rgba(Colors.SURFACE_1, 230)}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 14px; padding: 2px 9px; }}
            QLabel#cardImportedBadge, QLabel#cardUploadedBadge {{ color: {Colors.BG}; background: {Colors.ACCENT}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.DISPLAY}; font-size: 12px; padding: 2px 7px; letter-spacing: 0px; }}
            QLabel#cardFinalizingBadge {{ color: {Colors.BG}; background: {Colors.WARNING}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 12px; padding: 3px 7px; }}
            QLabel#cardActionFeedback {{ color: {Colors.TEXT}; background: {Colors.SURFACE_3}; border: 1px solid {Colors.ACCENT_DIM}; border-radius: {Sizes.RADIUS_SM}px; font-family: {Fonts.BODY}; font-size: 14px; padding: 4px 8px; }}
        '''

    def refresh_theme(self):
        set_theme_style(self, lambda: (self._card_qss()))
        self.delete_btn.setIcon(_card_icon('delete', Colors.TEXT))
        self._share_icon_key = None
        self._update_share_action()
        if hasattr(self, '_play_icon'):
            self._play_icon.setPixmap(_card_icon('play', Colors.TEXT, 36).pixmap(36, 36))
        self.resize_card(self._card_width, force=True)

    def rebind(self, file_path, *, imported, upload_enabled, uploaded,
               upload_info, ready, card_width):
        """Reuse a card of the same media type without rebuilding its widget tree."""
        self._feedback_timer.stop()
        self._mutation_timer.stop()
        self._upload_status = ''
        self._action_feedback.hide()
        self.file_path = file_path
        self._source_duration = 0.0
        self.imported = imported
        self.upload_enabled = upload_enabled
        self.uploaded = uploaded
        self._thumbnail_ready = False
        self._thumbnail_fingerprint = ''
        self._thumb_pixmap = QPixmap()
        self.thumb_label.clear()
        self._title_stack.setCurrentIndex(0)
        self.title_label.set_full_text(_clip_title_from_filename(file_path))
        self.title_edit.clear()
        self.metadata_label.setText('Reading clip details…' if self.is_video else 'Screenshot')
        self.metadata_label.setToolTip('')
        self._imported_badge.setVisible(imported)
        self._upload_badge.setVisible(uploaded)
        if self.is_video:
            self.duration_label.setText('0:00')
            self._play_icon.hide()
        self.delete_btn.setToolTip('Delete clip' if not imported else 'Delete the linked original file')
        self.set_upload_info(upload_info)
        self.set_ready(ready)
        self.resize_card(card_width, force=True)

    def _update_share_action(self):
        has_link = bool(self.upload_link)
        self.share_btn.action_name = 'link' if has_link else 'upload'
        icon_key = ('link' if has_link else 'upload',
                    Colors.ACCENT if self.share_btn.property('actionHovered') else Colors.BG)
        if getattr(self, '_share_icon_key', None) != icon_key:
            self.share_btn.setIcon(_card_icon(*icon_key))
            self._share_icon_key = icon_key
        sftp = self.upload_link.startswith('sftp://')
        label = 'Copy SFTP location' if sftp else 'Copy link' if has_link else 'Upload clip'
        self.share_btn.setAccessibleName(label)
        self.share_btn.setToolTip(
            'Copy SFTP location (requires an SFTP client and login)' if sftp else
            label if has_link or self.upload_enabled else
            'Enable the uploader in Settings to upload this clip')
        self.share_btn.setEnabled(self.ready and not self._mutation_busy and not self._upload_status and (has_link or (self.is_video and self.upload_enabled)))

    def eventFilter(self, obj, event):
        if obj is self.share_btn and event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            hovered = event.type() == QEvent.Type.Enter
            self.share_btn.setProperty('actionHovered', hovered)
            self.share_btn.update()
            tint = Colors.ACCENT if hovered else Colors.BG
            self._share_icon_key = ('link' if self.upload_link else 'upload', tint)
            self.share_btn.setIcon(_card_icon(*self._share_icon_key))
        return super().eventFilter(obj, event)

    def _show_action_feedback(self, message, *, duration_ms=2000):
        self._feedback_timer.stop()
        self._action_feedback.setText(message)
        self._action_feedback.adjustSize()
        self._action_feedback.move(8, 8)
        self._action_feedback.setVisible(bool(message))
        self._action_feedback.raise_()
        if message and duration_ms:
            self._feedback_timer.start(duration_ms)

    def _clear_action_feedback(self):
        self._show_action_feedback(self._upload_status, duration_ms=0)

    def set_upload_status(self, message, *, feedback=''):
        self._upload_status = message
        self._show_action_feedback(message or feedback, duration_ms=0 if message else 2500)
        self._update_share_action()

    def set_uploaded(self, val):
        self.uploaded = val
        self._upload_badge.setVisible(val)
        self._update_share_action()

    def set_ready(self, ready):
        self.ready = bool(ready)
        if self.ready and self._finalizing_badge is not None:
            self._finalizing_badge.deleteLater()
            self._finalizing_badge = None
        elif not self.ready and self._finalizing_badge is None:
            self._finalizing_badge = QLabel('Finalizing', self._thumb_frame)
            self._finalizing_badge.setObjectName('cardFinalizingBadge')
            self._finalizing_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._finalizing_badge.adjustSize()
            self._finalizing_badge.move(8, 8)
            self._finalizing_badge.show()
        self.delete_btn.setEnabled(self.ready and not self._mutation_busy)
        self.title_label.setEnabled(self.ready and not self._mutation_busy)
        self._update_share_action()

    @staticmethod
    def _upload_url(info):
        if isinstance(info, dict):
            for key in ('url', 'raw_url'):
                value = str(info.get(key, '') or '').strip()
                if value.startswith(('https://', 'http://')):
                    return value
                if value.startswith('sftp://'):
                    url = QUrl(value)
                    if (url.isValid() and url.host() and not url.userInfo()
                            and url.path().startswith('/') and len(url.path()) > 1
                            and (url.port() == -1 or 1 <= url.port() <= 65535)):
                        return value
        return ''

    def set_upload_info(self, info):
        self.upload_info = dict(info or {})
        self.upload_link = self._upload_url(self.upload_info)
        self.open_link_btn.setEnabled(bool(self.upload_link))
        self._link_actions.setVisible(self.is_video or bool(self.upload_link))
        self._update_share_action()

    def _copy_upload_link(self):
        if self.upload_link:
            QApplication.clipboard().setText(self.upload_link)
            self._show_action_feedback(
                'SFTP location copied' if self.upload_link.startswith('sftp://') else 'Link copied')

    def _open_upload_link(self):
        if self.upload_link:
            QDesktopServices.openUrl(QUrl(self.upload_link))

    def resize_card(self, width, force=False):
        width = max(_CARD_MIN_W, min(_CARD_MAX_W, int(width)))
        if width == self._card_width and not force:
            return
        self._card_width = width
        self._thumb_height = round(width * 9 / 16)
        self.setFixedSize(width, self._thumb_height + _CARD_BODY_H)
        self._thumb_frame.setFixedSize(width, self._thumb_height)
        self.thumb_label.setGeometry(0, 0, width, self._thumb_height)
        self._body_frame.setFixedSize(width, _CARD_BODY_H)
        if hasattr(self, '_play_icon'):
            self._play_icon.setGeometry(0, 0, width, self._thumb_height)
            self.duration_label.adjustSize()
            self.duration_label.move(width - self.duration_label.width() - 10, self._thumb_height - self.duration_label.height() - 8)
        badge_y = self._thumb_height - 8
        if self.imported:
            self._imported_badge.adjustSize()
            badge_y -= self._imported_badge.height()
            self._imported_badge.move(8, badge_y)
            badge_y -= 4
        self._upload_badge.adjustSize()
        self._upload_badge.move(8, badge_y - self._upload_badge.height())
        if self._finalizing_badge is not None:
            self._finalizing_badge.adjustSize()
            self._finalizing_badge.move(8, 8)
        self._render_thumbnail()

    def _render_thumbnail(self):
        if self._thumb_pixmap.isNull():
            return
        # Compose once when the image, card size or theme changes. No effect
        # widgets or animation timers run while the library is idle.
        width, height = self._card_width, self._thumb_height
        ratio = self.devicePixelRatioF()
        # Qt's shared LRU has a fixed memory budget. Preserve composed images
        # across card recycling without retaining a second unbounded library.
        # Source identity, geometry, DPR and theme all invalidate the key.
        cache_key = f'fthr/card/{self._thumb_pixmap.cacheKey()}/{width}/{height}/{ratio}/{Sizes.RADIUS_CARD}/{Colors.CARD_BG}'
        cached = QPixmapCache.find(cache_key)
        if cached is not None:
            self.thumb_label.setPixmap(cached)
            return
        source = self._thumb_pixmap.scaled(
            round(width * ratio), round(height * ratio),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        rendered = QPixmap(round(width * ratio), round(height * ratio))
        rendered.setDevicePixelRatio(ratio)
        rendered.fill(Qt.GlobalColor.transparent)
        painter = QPainter(rendered)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rounded = QPainterPath()
        rounded.addRoundedRect(QRectF(0, 0, width, height), Sizes.RADIUS_CARD, Sizes.RADIUS_CARD)
        rounded.addRect(QRectF(0, Sizes.RADIUS_CARD, width, height - Sizes.RADIUS_CARD))
        rounded.setFillRule(Qt.FillRule.WindingFill)
        painter.setClipPath(rounded)
        crop = QRectF((source.width() - rendered.width()) / 2,
                      (source.height() - rendered.height()) / 2,
                      rendered.width(), rendered.height())
        painter.drawPixmap(QRectF(0, 0, width, height), source, crop)
        fade = QLinearGradient(0, height * .64, 0, height)
        transparent = QColor(Colors.CARD_BG)
        transparent.setAlpha(0)
        fade.setColorAt(0, transparent)
        fade.setColorAt(1, QColor(Colors.CARD_BG))
        painter.fillRect(QRectF(0, height * .64, width, height * .36), fade)
        painter.end()
        QPixmapCache.insert(cache_key, rendered)
        self.thumb_label.setPixmap(rendered)

    def release_thumbnail_image(self):
        """Drop only this card's images; keep indexed metadata and controls."""
        if not self._thumb_pixmap.isNull():
            cache_key = f'fthr/card/{self._thumb_pixmap.cacheKey()}/{self._card_width}/{self._thumb_height}/{self.devicePixelRatioF()}/{Sizes.RADIUS_CARD}/{Colors.CARD_BG}'
            QPixmapCache.remove(cache_key)
        self._thumb_pixmap = QPixmap()
        self.thumb_label.clear()
        self._thumbnail_ready = False

    def _on_share_click(self):
        if not self.ready or self._mutation_busy:
            return
        if self.upload_link:
            self._copy_upload_link()
        elif self.is_video and self.upload_enabled:
            self._show_action_feedback('Upload requested')
            self.upload_requested.emit(self.file_path)

    def _on_thumbnail_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.ready and not self._mutation_busy:
            self.clicked.emit(self.file_path)
            self._emit_opened()
            event.accept()

    def _emit_opened(self):
        if self.is_video and self.ready and not self._mutation_busy:
            self.opened.emit(self.file_path, self._thumb_pixmap,
                             QRect(self.mapToGlobal(QPoint(0, 0)), self.size()))

    def _begin_rename(self):
        if not self.ready or self._mutation_busy:
            return
        self._editing_name = True
        self.title_edit.setText(Path(self.file_path).stem)
        self._title_stack.setCurrentIndex(1)
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _cancel_rename(self):
        self._editing_name = False
        self._title_stack.setCurrentIndex(0)

    def _commit_rename(self):
        if not self._editing_name:
            return
        name = self.title_edit.text()
        self._cancel_rename()
        try:
            destination = renamed_clip_path(self.file_path, name)
        except (OSError, ValueError) as error:
            FthrMessageDialog.warning(self, 'Rename failed', str(error))
            return
        if destination == self.file_path:
            return
        if self.imported and not FthrMessageDialog.question(
                self, 'Rename linked original', f'Rename the original file in its source folder?\n\n{self.file_path}'):
            return
        self._begin_mutation('rename', destination)

    def _confirm_delete(self, *, allow_linked=False):
        if not self.ready or self._mutation_busy:
            return
        ownership = classify_media_path(self.file_path, self._clips_root, [])
        if ((self.imported and not allow_linked)
                or (ownership is not MediaOwnership.FTHR_OWNED and not (self.imported and allow_linked))):
            FthrMessageDialog.information(self, 'Linked Original Protected',
                                         'Use the dedicated Delete button to review deletion of this linked original.')
            return
        detail = f'Delete {Path(self.file_path).name}?\nThis cannot be undone.'
        if self.imported:
            detail += f'\n\nThis deletes the original from its source folder:\n{self.file_path}'
        if FthrMessageDialog.question(self, 'Delete clip', detail):
            self._begin_mutation('delete')

    def _begin_mutation(self, kind, destination=None):
        reason = self._prepare_mutation(self.file_path) if self._prepare_mutation else ''
        if reason:
            FthrMessageDialog.warning(self, 'Clip is busy', reason)
            return
        self._mutation_busy = True
        self._mutation_kind, self._mutation_destination = kind, destination
        self._mutation_attempts = 0
        self._mutation_old_path = self.file_path
        try:
            cache = _get_cached_thumb_path(self.file_path)
            self._mutation_caches = (cache, _get_cached_duration_path(cache), _get_negative_cache_path(cache))
        except OSError:
            self._mutation_caches = ()
        self.set_ready(self.ready)
        self._try_mutation()

    def _try_mutation(self):
        self._mutation_attempts += 1
        if self._wait_for_readers and not self._wait_for_readers(self.file_path):
            if self._mutation_attempts < 30:
                self._mutation_timer.start(100)
                return
            self._mutation_failed('The thumbnail reader is still closing. Try again in a moment.')
            return
        try:
            old_path = self.file_path
            if self._mutation_kind == 'rename':
                rename_clip(old_path, self._mutation_destination)
                self.file_path = self._mutation_destination
            else:
                os.remove(old_path)
                try:
                    manifest_path_for(old_path).unlink(missing_ok=True)
                except OSError:
                    pass  # Media deletion succeeded; optional sidecar cleanup.
        except OSError as error:
            if getattr(error, 'winerror', 0) in (32, 33) and self._mutation_attempts < 30:
                self._mutation_timer.start(100)
                return
            self._mutation_failed(str(error))
            return
        except (ValueError, TypeError) as error:
            self._mutation_failed(str(error))
            return
        for cache in self._mutation_caches:
            try:
                os.remove(cache)
            except OSError:
                pass
        self._release_mutation()
        if self._mutation_kind == 'rename':
            self.title_label.set_full_text(Path(self.file_path).stem)
            self.renamed.emit(old_path, self.file_path)
        else:
            self.deleted.emit(old_path)

    def _release_mutation(self):
        self._mutation_busy = False
        if self._finish_mutation:
            self._finish_mutation(self._mutation_old_path)
        self.set_ready(self.ready)

    def _mutation_failed(self, reason):
        self._release_mutation()
        FthrMessageDialog.warning(self, 'File change failed', reason)

    def contextMenuEvent(self, event):
        if not self.ready or self._mutation_busy:
            return
        menu = QMenu(self)
        set_theme_style(menu, lambda: (context_menu_qss()))
        open_action = menu.addAction('Open in editor')
        rename_action = menu.addAction('Rename')
        explorer_action = menu.addAction('Show in file manager')
        copy_action = menu.addAction('Copy path')
        link_action = menu.addAction(
            'Open SFTP location' if self.upload_link.startswith('sftp://')
            else 'Open uploaded link') if self.upload_link else None
        delete_action = menu.addAction('Delete clip')
        action = menu.exec(event.globalPos())
        if action == open_action:
            self._emit_opened()
        elif action == rename_action:
            self._begin_rename()
        elif action == explorer_action:
            _show_in_file_manager(self.file_path)
        elif action == copy_action:
            QApplication.clipboard().setText(self.file_path)
        elif link_action and action == link_action:
            self._open_upload_link()
        elif action == delete_action:
            self._confirm_delete(allow_linked=True)

    def enterEvent(self, event):
        if hasattr(self, '_play_icon'):
            self._play_icon.setVisible(self.ready and not self._mutation_busy)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if hasattr(self, '_play_icon'):
            self._play_icon.hide()
        super().leaveEvent(event)

    def fade_in(self, delay_ms=0):
        pass  # Cards paint immediately without compositing effects.

    def _load_image_thumbnail(self):
        reader = QImageReader(self.file_path)
        reader.setAutoTransform(True)
        source_size = reader.size()
        if source_size.isValid():
            reader.setScaledSize(source_size.scaled(640, 360, Qt.AspectRatioMode.KeepAspectRatio))
        self.set_image_thumbnail(reader.read())

    def set_image_thumbnail(self, image):
        if image is not None and not image.isNull():
            self._thumb_pixmap = QPixmap.fromImage(image)
            self._thumbnail_ready = True
            self.metadata_label.setText('Screenshot')
            self._render_thumbnail()

    def set_video_thumbnail(self, cache_path, duration):
        pixmap = QPixmap(cache_path)
        if not pixmap.isNull():
            self._thumb_pixmap = pixmap
            self._thumbnail_ready = True
            self._render_thumbnail()
        metadata = read_cached_metadata(_get_cached_duration_path(cache_path))
        self.set_source_duration(metadata[0] if metadata else duration)
        if metadata:
            _, width, height, fps, bitrate, _total = metadata
            details = [f'{height}p']
            if fps:
                details.append(f'{fps:g} FPS')
            if bitrate:
                details.append(f'{bitrate / 1_000_000:g} Mbps')
            self.metadata_label.setText(' · '.join(details))
            self.metadata_label.setToolTip(f'{width} × {height}  /  ' + ' / '.join(details[1:]))
        else:
            self.metadata_label.setText('Video clip')
        self.resize_card(self._card_width, force=True)


    def set_source_duration(self, duration):
        self._source_duration = duration
        self.refresh_duration_label()

    def refresh_duration_label(self):
        if not self.is_video:
            return
        duration = (self._duration_provider(self.file_path, self._source_duration)
                    if self._duration_provider else self._source_duration)
        seconds = int(duration + 1e-6)
        self.duration_label.setText(f'{seconds // 60}:{seconds % 60:02d}' if duration > 0 else '--:--')
        self.duration_label.adjustSize()
        self.duration_label.move(self._card_width - self.duration_label.width() - 10,
                                 self._thumb_height - self.duration_label.height() - 8)


# ClipGrid — filter bar + date sections + grid of cards

class ClipGrid(QWidget):
    clip_clicked          = Signal(str)
    clip_opened           = Signal(str, QPixmap, QRect)
    screenshot_clicked    = Signal(str)
    clip_upload_requested = Signal(str)
    clip_renamed = Signal(str, str)

    # Compact gutters keep the media surface visually dominant while still
    # separating cards at desktop widths.
    _H_MARGIN = 48
    _GRID_SPACING = 12

    def __init__(self, settings_manager=None, parent=None, *, metadata_manager=None):
        super().__init__(parent)
        self._sm              = settings_manager
        self._metadata_manager = metadata_manager
        self._upload_checker  = None   # callable(path) -> bool
        self._upload_info_checker = None  # callable(path) -> dict | None
        self._upload_enabled  = None   # callable() -> bool
        self._readiness_checker = None  # callable(path) -> bool
        self._mutation_prepare = None
        self._mutation_finish = None
        self._mutation_paths = set()
        self._mutation_refresh_pending = False
        self.clips_dir      = str(clips_directory_from(self._sm))
        self.thumbnails     = []
        self._thumb_widgets = {}
        self._recycled_cards: list[ClipThumbnail] = []
        # Retain decoded images and labels, not widgets or running workers.
        # Returning to a recent row avoids JPEG reads, metadata reads, and jobs.
        self._recent_thumbnails = ImageLRU(8 * 1024 * 1024, max_entries=96)
        start_image_cache_maintenance()
        self._idle_memory_timer = QTimer(self)
        self._idle_memory_timer.setSingleShot(True)
        self._idle_memory_timer.setInterval(15_000)
        self._idle_memory_timer.timeout.connect(self._trim_hidden_memory)
        self._upload_statuses = {}
        self._known_files   = set()
        self._imported_files: set = set()
        # Save completion updates one card in place.  Keep the date-section
        # layouts indexed so adding a clip never has to tear down and recreate
        # every card in a large library.
        self._section_grids: dict[str, QGridLayout] = {}
        self._section_files: dict[str, list[str]] = {}
        self._section_widgets: dict[str, QWidget] = {}
        self._virtual_ready: dict[str, bool] = {}
        self._virtual_sections: dict[str, QWidget] = {}
        self._card_virtual_sections: dict[str, str] = {}
        self._virtual_initialized = False
        self._records: dict[str, LibraryRecord] = {}
        self._scan_generation = 0
        self._active_scan_worker: _FileCollectWorker | None = None
        self._pending_scan_request: tuple[str, str] | None = None
        self._shutdown_started = False
        self._storage_error_reported = False
        self._thumbnail_jobs_inflight: set[str] = set()
        self._deferred_thumbnail_paths: set[str] = set()
        self._thumbnail_job_fingerprints: dict[str, str] = {}
        # The set above is kept for bounded queue metrics and compatibility,
        # while this token map is the ownership authority.  A late terminal
        # signal from an older generation must never remove a replacement job
        # for the same path.
        self._thumbnail_job_owners: dict[str, tuple[int, str]] = {}
        # Keep signal senders alive until their queued terminal delivery. A
        # QRunnable may finish before the GUI gets back to its event queue.
        self._thumbnail_workers: dict[str, QRunnable] = {}
        self._thumbnail_cancel_event = threading.Event()
        self._thumbnail_generation = 0
        self._pending_saved_clips: dict[str, bool] = {}
        # CLIP_SAVED can arrive while discovery is already in flight.  Keep
        # the published record until that generation is merged so the final
        # scan cannot erase a valid just-saved clip it raced with.
        self._scan_upserts: dict[str, LibraryRecord] = {}
        self._thread_pool   = QThreadPool()
        # Cap at 2 workers. FFmpeg thumbnail decode is already heavy on disk
        # and CPU; throwing 16 threads at it just thrashes and makes everything
        # slower. 400ms of profiling led me here. don't touch this.
        self._thread_pool.setMaxThreadCount(2)
        # File scans must not sit behind a long queue of video decoders.  A
        # separate single-worker pool makes filter/sort changes respond as soon
        # as the current scan (if any) completes.
        self._scan_thread_pool = QThreadPool()
        self._scan_thread_pool.setMaxThreadCount(1)
        # Pre-compute the column count from the primary screen's available
        # width so the first paint already matches the maximized window.
        # Without this, the grid renders at 3 cols then snaps to N cols once
        # the resize event fires after showMaximized() — visible lag.
        initial_width = self._initial_viewport_width()
        (self._current_columns,
         self._current_card_widths) = self._layout_metrics(initial_width)

        self._filter = 'all'
        self._sort = 'newest'
        self._background_paused = False
        self._background_refresh_pending = False
        self._in_transition = False
        self._transition_anim = None
        # Keep transition workers (and, critically, their signal objects) alive
        # until the queued completion callback has run on the GUI thread.
        self._transition_workers: dict[int, _FileCollectWorker] = {}
        self._diagnostic_scan_started: dict[int, float] = {}
        self._thumbnail_jobs_submitted = 0
        self._thumbnail_jobs_completed = 0
        self._thumbnail_diagnostics_completed = 0
        self._thumbnail_total_elapsed_ms = 0
        self._metadata_total_elapsed_ms = 0
        self._thumbnail_cache_hits = 0
        self._thumbnail_queue_peak = 0

        # Coalesce scroll/layout requests into one bounded update. This timer
        # runs after a change or to finish a bounded creation burst, never idle.
        self._virtual_update_timer = QTimer(self)
        self._virtual_update_timer.setSingleShot(True)
        self._virtual_update_timer.setInterval(16)
        self._virtual_update_timer.timeout.connect(self._update_virtualized_cards)
        # Rebind cards immediately, but do not start decoders for rows flying
        # past the viewport. Cached images remain available during scrolling.
        self._scrolling = False
        self._scroll_settle_timer = QTimer(self)
        self._scroll_settle_timer.setSingleShot(True)
        self._scroll_settle_timer.setInterval(90)
        self._scroll_settle_timer.timeout.connect(self._resume_scroll_thumbnails)

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
        self.refresh_timer.setInterval(30000)
        self.refresh_timer.timeout.connect(self._load_clips)
        self.refresh_timer.start()

        # A scroll event changes which small subset of cards is materialized.
        # Connecting to the nearest scroll area's bar keeps the public ClipGrid
        # API unchanged and avoids a per-card event filter.
        QTimer.singleShot(0, self._connect_scroll_updates)
        # Declare the painted library surface opaque so Qt can reuse scrolled
        # pixels instead of repainting the shell through transparent ancestors.
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, QColor(Colors.BG).alpha() == 255)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), QColor(Colors.BG))
        painter.end()

    def _connect_scroll_updates(self) -> None:
        parent = self.parentWidget()
        for _ in range(4):
            if parent is None:
                break
            bar = getattr(parent, 'verticalScrollBar', None)
            if callable(bar):
                try:
                    bar().valueChanged.connect(self._on_library_scrolled)
                    bar().rangeChanged.connect(self._request_virtual_update)
                except (AttributeError, RuntimeError):
                    pass
                break
            parent = parent.parentWidget()

    def _on_library_scrolled(self, *_args):
        if self._background_paused or self._shutdown_started:
            return
        self._scrolling = True
        self._scroll_settle_timer.start()
        self._request_virtual_update()

    def _resume_scroll_thumbnails(self):
        self._scrolling = False
        self._requeue_visible_thumbnail_jobs()

    def is_linked_import(self, path: str) -> bool:
        target = os.path.normcase(os.path.realpath(path))
        return any(
            os.path.normcase(os.path.realpath(candidate)) == target
            for candidate in self._imported_files
        )

    def set_upload_checker(self, checker):
        """checker(path: str) -> bool  — True if the clip has been uploaded."""
        self._upload_checker = checker
        self._refresh_upload_states()

    def set_upload_info_checker(self, checker):
        """checker(path: str) -> dict | None — persisted provider result."""
        self._upload_info_checker = checker
        self._refresh_upload_states()

    def set_upload_enabled_checker(self, checker):
        """checker() -> bool  — True if uploads are enabled (shows Upload menu item)."""
        self._upload_enabled = checker
        enabled = bool(checker and checker())
        for card in self._thumb_widgets.values():
            card.upload_enabled = enabled
            card._update_share_action()

    def set_mutation_callbacks(self, prepare, finish):
        self._mutation_prepare, self._mutation_finish = prepare, finish
        for card in self.thumbnails:
            card._prepare_mutation = self._prepare_card_mutation
            card._finish_mutation = self._finish_card_mutation

    def _prepare_card_mutation(self, path):
        reason = self._mutation_prepare(path) if self._mutation_prepare else ''
        if not reason:
            self._mutation_paths.add(path)
        return reason

    def _finish_card_mutation(self, path):
        self._mutation_paths.discard(path)
        if self._mutation_finish:
            self._mutation_finish(path)
        if self._mutation_refresh_pending:
            self._mutation_refresh_pending = False
            QTimer.singleShot(0, self.force_refresh)

    def _release_thumbnail_reader(self, path):
        worker = self._thumbnail_workers.get(path)
        if worker is not None:
            worker.cancel_event.set()
        return path not in self._thumbnail_jobs_inflight

    def _on_clip_renamed(self, old_path, new_path):
        # Migrate history before the newly named card is scanned.
        self.clip_renamed.emit(old_path, new_path)
        self.force_refresh()

    def set_readiness_checker(self, checker):
        self._readiness_checker = checker

    def _refresh_upload_states(self):
        """Refresh badges and returned-link actions on cards already in the grid."""
        for path, card in self._thumb_widgets.items():
            uploaded = bool(self._upload_checker and self._upload_checker(path))
            info = (
                self._upload_info_checker(path)
                if uploaded and self._upload_info_checker else None)
            card.set_upload_info(info)
            card.set_uploaded(uploaded)

    def refresh_theme(self):
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, QColor(Colors.BG).alpha() == 255)
        self.update()
        _render_card_icon.cache_clear()
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            card.refresh_theme()

    def _viewport_width(self) -> int:
        """The scroll-area viewport is our parent — read its width so column
        calculation works even when the grid layout is wider than the viewport."""
        vp = self.parentWidget()
        return vp.width() if vp else self.width()

    def _initial_viewport_width(self) -> int:
        """Best-guess viewport width before the parent has been laid out.

        This lets the first build match the maximized window and avoids a
        visible reflow during startup.
        """
        screen = QApplication.primaryScreen()
        if screen is None:
            return 1100
        return screen.availableGeometry().width()

    @classmethod
    def _layout_metrics(cls, viewport_width: int) -> tuple[int, list[int]]:
        """Return column count and exact per-column widths for a viewport."""
        available = max(_CARD_MIN_W, int(viewport_width) - cls._H_MARGIN)
        cols = max(
            1,
            int((available + cls._GRID_SPACING)
                // (_CARD_W + cls._GRID_SPACING)),
        )
        while cols > 1:
            card_width = (
                available - cls._GRID_SPACING * (cols - 1)) // cols
            if card_width >= _CARD_MIN_W:
                break
            cols -= 1
        while True:
            card_width = (
                available - cls._GRID_SPACING * (cols - 1)) // cols
            if card_width <= _CARD_MAX_W:
                break
            if (available - cls._GRID_SPACING * cols) // (cols + 1) < _CARD_MIN_W:
                break
            cols += 1

        usable = min(available - cls._GRID_SPACING * (cols - 1), cols * _CARD_MAX_W)
        base, remainder = divmod(usable, cols)
        widths = [base + (1 if col < remainder else 0)
                  for col in range(cols)]
        return cols, widths

    def _initial_columns_from_screen(self) -> int:
        """Compatibility helper used by a few downstream integrations."""
        return self._layout_metrics(self._initial_viewport_width())[0]

    def _compute_columns(self) -> int:
        return self._layout_metrics(self._viewport_width())[0]

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_vp_filter', False):
            vp = self.parentWidget()
            if vp:
                vp.installEventFilter(self)
                self._vp_filter = True
        self._resize_timer.start(0)

    def eventFilter(self, obj, event):
        # Resize events fire like a machine gun while you drag a window edge.
        # Debounce so we only rebuild the grid 200ms after you STOP dragging,
        # not 60 times a second mid-drag. Your CPU thanks you.
        if (event.type() == event.Type.Resize
                and not self._background_paused and not self._shutdown_started):
            self._resize_timer.start(200)
        return super().eventFilter(obj, event)

    def _on_resize_settled(self):
        new_cols, new_widths = self._layout_metrics(self._viewport_width())
        if (new_cols != self._current_columns
                or new_widths != self._current_card_widths):
            self._current_columns = new_cols
            self._current_card_widths = new_widths
            self._relayout_grids()
        self._request_virtual_update()

    def _on_dir_changed(self, path: str):
        if self._background_paused:
            self._background_refresh_pending = True
            return
        self._debounce_timer.start(500)

    def _trim_hidden_memory(self):
        if not self._background_paused:
            return
        self._recent_thumbnails.clear()
        viewport = self._scroll_viewport()
        if viewport is not None:
            for card in self._thumb_widgets.values():
                bounds = QRect(card.mapTo(viewport, QPoint()), card.size())
                if (not bounds.intersects(viewport.rect())
                        and not card._editing_name and not card._mutation_busy):
                    card.release_thumbnail_image()
        # Preserve materialized cards, scroll position and active interactions.
        # A spare row is enough to avoid rebuilding controls on the next scroll.
        limit = max(1, self._current_columns)
        while len(self._recycled_cards) > limit:
            self._recycled_cards.pop().deleteLater()

    def set_background_paused(self, paused: bool) -> None:
        """Suspend library scans while the containing app is in background.

        Capture/save completion can still invalidate the library through
        ``force_refresh``. While paused that work is coalesced into one refresh
        when the UI becomes active again.
        """
        paused = bool(paused)
        if paused == self._background_paused:
            return
        self._background_paused = paused
        if paused:
            self._idle_memory_timer.start()
            settle_timer = getattr(self, '_scroll_settle_timer', None)
            if settle_timer is not None:
                settle_timer.stop()
            self._scrolling = False
            self._virtual_update_timer.stop()
            self._resize_timer.stop()
            # Cancel queued and active decodes cooperatively. Do not clear the
            # pool or release ownership here: a queued/active QRunnable still
            # owns its token and must reach its terminal diagnostic callback.
            # Releasing it globally would let a replacement job run alongside
            # the old worker and would make a late result able to strand or
            # remove the replacement's ownership.
            self._thumbnail_cancel_event.set()
            self._thumbnail_generation += 1
            self._scan_generation += 1
            if self._active_scan_worker is not None:
                self._active_scan_worker.cancel()
            self.refresh_timer.stop()
            self._debounce_timer.stop()
            return

        # A cancelled QRunnable that was already in the pool can finish after
        # this method returns.  Start a fresh generation before re-enriching
        # visible cards so its late signal cannot own or clear a replacement
        # job.
        self._idle_memory_timer.stop()
        self._thumbnail_cancel_event = threading.Event()
        self._resize_timer.start(0)
        self._request_virtual_update()
        self._requeue_visible_thumbnail_jobs()
        self.refresh_timer.start()
        if self._background_refresh_pending:
            self._background_refresh_pending = False
            self._known_files = None
            self._load_clips()
        pending = self._pending_saved_clips
        self._pending_saved_clips = {}
        for path, ready in pending.items():
            self.upsert_saved_clip(path, ready=ready)

    # UI

    def _setup_ui(self):
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(24, 18, 24, 24)
        self.layout.setSpacing(0)

        # Filter bar
        filt = QFrame()
        filt.setObjectName('filterBar')
        filt.setFixedHeight(Sizes.FILTER_H)
        fl = QHBoxLayout(filt)
        fl.setContentsMargins(12, 0, 12, 0)
        fl.setSpacing(12)

        self._all_count_label = QLabel('All clips')
        self._all_count_label.setObjectName('allCountLabel')
        fl.addWidget(self._all_count_label)

        fl.addStretch(1)

        self.filter_combo = _DropdownCombo()
        self.filter_combo.addItems(['All clips', 'Clips only',
                                    'Screenshots', 'Imported clips'])
        set_theme_style(self.filter_combo, lambda: (combo_qss()))
        self.filter_combo.setMinimumWidth(120)
        self.filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        fl.addWidget(self.filter_combo)

        self.sort_combo = _DropdownCombo()
        self.sort_combo.addItems(['Newest', 'Oldest'])
        set_theme_style(self.sort_combo, lambda: (combo_qss()))
        self.sort_combo.setMinimumWidth(110)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        fl.addWidget(self.sort_combo)

        self.layout.addWidget(filt)
        self.layout.addSpacing(8)

        # Section host (date-grouped grids live here)
        self._sections_host = QWidget()
        self._sections_host.setStyleSheet('background: transparent;')
        self._sections_layout = QVBoxLayout(self._sections_host)
        self._sections_layout.setContentsMargins(0, 0, 0, 0)
        self._sections_layout.setSpacing(20)
        self.layout.addWidget(self._sections_host)

        # Empty state
        self._empty_widget = QWidget()
        self._empty_widget.setStyleSheet('background: transparent;')
        ev_layout = QVBoxLayout(self._empty_widget)
        ev_layout.setContentsMargins(0, 96, 0, 0)
        ev_layout.setSpacing(0)
        ev_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self._no_clips_lbl = QLabel('No clips yet')
        set_theme_style(self._no_clips_lbl, lambda: (label_display(Colors.TEXT, Fonts.SIZE_H2, 0)))
        self._no_clips_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ev_layout.addWidget(self._no_clips_lbl)

        self._empty_detail_lbl = QLabel()
        set_theme_style(self._empty_detail_lbl, lambda: (label_display(Colors.TEXT_DIM, Fonts.SIZE_LABEL, Fonts.TRACK_LABEL)))
        self._empty_detail_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_detail_lbl.setVisible(False)
        ev_layout.addSpacing(Sizes.SPACE_2)
        ev_layout.addWidget(self._empty_detail_lbl)

        ev_layout.addSpacing(Sizes.SPACE_4)

        mark_row = QHBoxLayout()
        mark_row.setContentsMargins(0, 0, 0, 0)
        mark_row.addStretch()
        mark = QFrame()
        mark.setFixedSize(56, 2)
        set_theme_style(mark, lambda: (f'background: {Colors.ACCENT}; border: none;'))
        mark_row.addWidget(mark)
        mark_row.addStretch()
        ev_layout.addLayout(mark_row)

        self.layout.addWidget(self._empty_widget)
        self.layout.addStretch()

        set_theme_style(self, lambda: (self._grid_qss()))

    @staticmethod
    def _grid_qss() -> str:
        return f'''
            QFrame#filterBar {{
                background: {Colors.SURFACE_1};
                border-radius: {Sizes.RADIUS_MD}px;
                border-bottom: 1px solid {Colors.SHELL_DIVIDER};
            }}
            QLabel#allCountLabel {{
                color: {Colors.TEXT};
                font-family: {Fonts.DISPLAY};
                font-weight: 600;
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
                font-weight: 600;
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

    # Filter / sort handlers

    def _on_filter_changed(self, idx: int):
        mapping = ['all', 'clips', 'screenshots', 'imported']
        self._filter = mapping[idx] if idx < len(mapping) else 'all'
        if self._records:
            self._apply_current_index()
        else:
            # Compatibility path for a filter changed before the first scan
            # has published an index. It is still cancellable and bounded.
            self._fade_out_then_reload()

    def _on_sort_changed(self, idx: int):
        self._sort = ['newest', 'oldest'][idx]
        if self._records:
            self._apply_current_index()
        else:
            self._fade_out_then_reload()

    def _filtered_records(self, *, ordered: bool = True) -> list[LibraryRecord]:
        records = list(self._records.values())
        if self._filter == 'clips':
            records = [record for record in records if record.kind == 'video']
        elif self._filter == 'screenshots':
            records = [record for record in records if record.kind == 'image']
        elif self._filter == 'imported':
            records = [record for record in records if record.imported]
        if not ordered:
            return records
        if self._sort == 'oldest':
            records.sort(key=lambda record: record.mtime_ns)
        elif self._sort == 'longest':
            records.sort(key=lambda record: record.size, reverse=True)
        else:
            records.sort(key=lambda record: record.mtime_ns, reverse=True)
        return records

    def _records_from_paths(self, paths: set[str]) -> dict[str, LibraryRecord]:
        result: dict[str, LibraryRecord] = {}
        for raw in paths:
            try:
                info = os.stat(raw)
            except OSError:
                continue
            identity = canonical_media_path(raw)
            suffix = Path(raw).suffix.casefold()
            kind = 'video' if suffix in _VIDEO_EXTS else 'image'
            result[identity] = LibraryRecord(
                os.path.abspath(os.path.normpath(raw)), identity, (),
                raw in self._imported_files, kind,
                int(info.st_size), int(info.st_mtime_ns))
        return result

    def _apply_current_index(self) -> None:
        if self._mutation_paths:
            self._mutation_refresh_pending = True
            return
        # Discovery can finish before the first debounced Resize. Use the
        # real viewport now, otherwise scrolling initially targets a range
        # calculated for the primary monitor's much wider column count.
        self._current_columns, self._current_card_widths = self._layout_metrics(
            self._viewport_width())
        records = self._filtered_records()
        self._deferred_thumbnail_paths.clear()
        self._thumbnail_cancel_event.set()
        self._thumbnail_generation += 1
        self._thumbnail_cancel_event = threading.Event()
        self._clear_sections()
        self.thumbnails.clear()
        self._thumb_widgets.clear()
        self._virtual_initialized = False
        self._virtual_ready.clear()
        if not records:
            self._show_empty(True)
            self._all_count_label.setText(self._filter_heading())
            return
        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(records)})')
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for record in records:
            key = _section_label_for(record.mtime)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(record.path)
        global_idx = 0
        for section_key in order:
            files = groups[section_key]
            self._add_section(section_key, files, global_idx)
            global_idx += len(files)
        self._request_virtual_update()

    def _fade_out_then_reload(self):
        """Scan a category in the background, keeping existing cards visible.

        Avoid QGraphicsOpacityEffect: it can leave the host transparent on Windows.
        """
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
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
        for previous in list(self._transition_workers.values()):
            previous.cancel()
        # Remove queued obsolete transitions; a running worker observes its
        # event and exits at the next directory boundary.
        self._scan_thread_pool.clear()
        self._transition_workers.clear()

        def _launch_worker():
            import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
            cancel_event = threading.Event()
            worker = _FileCollectWorker(
                self.clips_dir, import_dirs, self._filter, self._sort,
                cancel_event=cancel_event)
            self._transition_workers[seq] = worker
            self._diagnostic_scan_started[seq] = time.monotonic()
            emit_event(
                'library', 'scan_started', state='SCANNING',
                scan_kind='filter_transition', filter=self._filter,
                active_workers=len(self._transition_workers))

            def _on_done(raw, pairs, imported, subdirs):
                self._transition_workers.pop(seq, None)
                started = self._diagnostic_scan_started.pop(seq, None)
                emit_event(
                    'library', 'scan_completed', state='COMPLETED',
                    scan_kind='filter_transition',
                    candidate_video_count=sum(
                        1 for path in raw if is_completed_video_path(path)),
                    candidate_media_count=len(raw),
                    imported_media_count=len(imported),
                    elapsed_ms=(round((time.monotonic() - started) * 1000)
                                if started is not None else None),
                    active_workers=len(self._transition_workers),
                    process_memory_bytes=process_memory_bytes())
                if (self._transition_seq == seq
                        and (worker.result is None
                             or not worker.result.stats.cancelled)):
                    self._on_files_collected_for_transition(raw, pairs, imported, subdirs)

            worker.signals.finished.connect(_on_done)
            self._scan_thread_pool.start(worker)

        # Do not use a graphics effect here. Besides being unnecessary for the
        # scan, it can leave Qt's backing store transparent on some Windows
        # GPU/driver combinations.
        self._sections_host.setGraphicsEffect(None)
        _launch_worker()

    def _on_files_collected_for_transition(self, raw_files, sorted_pairs, imported_files, subdirs):
        """Runs on the main thread once the background worker finishes."""
        self._sync_watched_directories(subdirs)

        self._imported_files = imported_files
        self._records = self._records_from_paths(set(raw_files))
        self._known_files = set(raw_files)
        self._apply_current_index()

        self._sections_host.setGraphicsEffect(None)
        self._in_transition = False
        self._transition_anim = None
        pending = self._pending_saved_clips
        self._pending_saved_clips = {}
        for path, ready in pending.items():
            self.upsert_saved_clip(path, ready=ready)

    def _filter_heading(self) -> str:
        if self._filter == 'clips':
            return 'CLIPS'
        if self._filter == 'screenshots':
            return 'SCREENSHOTS'
        if self._filter == 'imported':
            return 'IMPORTED CLIPS'
        return 'ALL CLIPS'

    # Filesystem walking

    def _collect_media_files(self) -> set:
        """Compatibility helper; production refreshes use ``_FileCollectWorker``."""
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        result = scan_library(self.clips_dir, import_dirs, filter_='all', sort_=self._sort)
        self._imported_files = set(result.imported_paths)
        for path in result.watched_directories:
            self._watch_subdir(path)
        self._records = {record.identity: record for record in result.all_records}
        return {record.path for record in result.all_records}

    def _watch_subdir(self, path: str):
        if (not self._shutdown_started and os.path.isdir(path)
                and path not in self._watcher.directories()):
            self._watcher.addPath(path)

    def _sync_watched_directories(self, paths) -> None:
        """Keep QFileSystemWatcher bounded to the latest scanner result."""
        if self._shutdown_started:
            return
        wanted = {os.path.abspath(os.path.normpath(path)) for path in paths}
        current = set(self._watcher.directories())
        for path in current - wanted:
            self._watcher.removePath(path)
        for path in wanted - current:
            if os.path.isdir(path):
                self._watcher.addPath(path)

    # Build the date-grouped layout

    def _load_clips(self):
        if self._mutation_paths:
            self._mutation_refresh_pending = True
            return
        if self._background_paused:
            self._background_refresh_pending = True
            return
        if self._in_transition or self._shutdown_started:
            return
        if not os.path.isdir(self.clips_dir):
            try:
                ensure_storage_directory(self.clips_dir)
            except StoragePathError as error:
                self._report_storage_unavailable(error)
                return
            if self.clips_dir not in self._watcher.directories():
                self._watcher.addPath(self.clips_dir)
            self._storage_error_reported = False
            self._show_empty(True)
            return
        self._begin_scan('foreground_refresh')

    def _report_storage_unavailable(self, error: StoragePathError) -> None:
        emit_event('storage', 'library_root_unavailable', state='FAILED',
                   path=self.clips_dir, detail=str(error))
        if self._storage_error_reported:
            return
        self._storage_error_reported = True
        FthrMessageDialog.warning(
            self, 'Clips Folder Unavailable',
            f'{error}\n\nFTHR will not switch to another folder. Restore access '
            'or choose a writable clips folder in Settings.')

    def _begin_scan(self, scan_kind: str) -> None:
        """Start one cancellable scan; at most one follow-up is retained."""
        import_dirs = self._sm.get('imported_clip_folders', []) if self._sm else []
        self._scan_generation += 1
        generation = self._scan_generation
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
            self._pending_scan_request = (scan_kind, self._filter)
            return
        cancel_event = threading.Event()
        worker = _FileCollectWorker(
            self.clips_dir, import_dirs, 'all', self._sort,
            cancel_event=cancel_event)
        worker.generation = generation
        worker.scan_kind = scan_kind
        self._active_scan_worker = worker
        started = time.monotonic()
        self._diagnostic_scan_started[generation] = started
        emit_event(
            'library', 'scan_started', state='SCANNING', scan_kind=scan_kind,
            generation=generation, root_count=1 + len(import_dirs),
            active_workers=self._scan_thread_pool.activeThreadCount())

        def _on_done(raw, pairs, imported, subdirs):
            if self._active_scan_worker is worker:
                self._active_scan_worker = None
            elapsed = round((time.monotonic() - started) * 1000)
            result = worker.result
            if result is None or result.stats.cancelled:
                emit_event('library', 'scan_cancelled', state='CANCELLED',
                           generation=generation, elapsed_ms=elapsed)
            elif generation == self._scan_generation and not self._shutdown_started:
                self._sync_watched_directories(subdirs)
                next_records = {record.identity: record
                                for record in result.all_records}
                for identity, upserted in list(self._scan_upserts.items()):
                    try:
                        if os.path.isfile(upserted.path):
                            next_records.setdefault(identity, upserted)
                    except OSError:
                        # A deleted path must not be resurrected by a late
                        # CLIP_SAVED notification.
                        continue
                self._scan_upserts.clear()
                self._imported_files = set(result.imported_paths)
                self._known_files = {record.path for record in result.all_records}
                summary = result.stats.as_dict() | {
                    'scan_kind': scan_kind,
                    'generation': generation,
                    'candidate_media_count': len(result.all_records),
                    'candidate_video_count': sum(
                        record.kind == 'video' for record in result.all_records),
                    'imported_media_count': len(self._imported_files),
                    'elapsed_ms': elapsed,
                    'thumbnail_widget_cache_entries': len(self._thumb_widgets),
                    'metadata_cache_entries': len(self._records),
                    'active_thumbnail_workers': self._thread_pool.activeThreadCount(),
                    'active_scan_workers': self._scan_thread_pool.activeThreadCount(),
                    'process_memory_bytes': process_memory_bytes(),
                }
                emit_event('library', 'scan_completed', state='COMPLETED', **summary)
                session = get_diagnostic_session()
                if session is not None:
                    session.update_summary('library', summary)
                if next_records != self._records:
                    self._records = next_records
                    self._apply_current_index()
                else:
                    self._requeue_visible_thumbnail_jobs()
            self._diagnostic_scan_started.pop(generation, None)
            pending = self._pending_scan_request
            self._pending_scan_request = None
            if pending and not self._shutdown_started:
                QTimer.singleShot(0, lambda: self._begin_scan(pending[0]))

        worker.signals.finished.connect(_on_done)
        worker.signals.batch.connect(self._on_scan_batch)
        self._scan_thread_pool.start(worker)
        # Give a tiny initial scan a chance to publish before callers inspect
        # a freshly constructed ClipGrid (and before the first paint).  The
        # wait is hard-bounded; large libraries continue asynchronously and
        # never occupy the main thread for the duration of their scan.
        if (scan_kind == 'foreground_refresh' and not self._records
                and not self.isVisible()):
            self._scan_thread_pool.waitForDone(100)
            QApplication.processEvents()

    def _on_scan_batch(self, generation: int, records) -> None:
        """Publish the first discovery batch before the scan finishes.

        Keep later batches on the worker to avoid repeated Qt model rebuilds;
        publish the sorted result once the generation completes.
        """
        if (self._shutdown_started or generation != self._scan_generation
                or self._active_scan_worker is None
                or getattr(self._active_scan_worker, 'generation', None)
                != generation or self._records):
            if generation != self._scan_generation:
                emit_event('library', 'stale_scan_batch_discarded',
                           state='DISCARDED', generation=generation)
            return
        partial = {record.identity: record for record in records}
        if not partial:
            return
        self._records = partial
        self._known_files = {record.path for record in partial.values()}
        self._imported_files = {
            record.path for record in partial.values() if record.imported}
        emit_event('library', 'scan_batch_published', state='PARTIAL',
                   generation=generation, record_count=len(partial))
        self._apply_current_index()

    def _finish_thumbnail_job(
            self, file_path: str, generation: int, fingerprint: str) -> bool:
        """Release one job only when the callback still owns its token."""
        token = (generation, fingerprint)
        if self._thumbnail_job_owners.get(file_path) != token:
            return False
        self._thumbnail_job_owners.pop(file_path, None)
        self._thumbnail_workers.pop(file_path, None)
        self._thumbnail_jobs_inflight.discard(file_path)
        if self._thumbnail_job_fingerprints.get(file_path) == fingerprint:
            self._thumbnail_job_fingerprints.pop(file_path, None)
        return True

    def _requeue_visible_thumbnail_jobs(self) -> None:
        """Re-enrich visible cards without rebuilding the library index."""
        if self._background_paused or self._shutdown_started:
            return
        for file_path, card in tuple(self._thumb_widgets.items()):
            if card.ready and not card._thumbnail_ready:
                self._start_thumbnail_worker(file_path)

    def _indexed_record(self, file_path: str):
        # Scanned paths are already absolute/canonical. Avoid resolving them
        # through Windows filesystem handles again for each scrolling card.
        return (self._records.get(os.path.normcase(os.path.normpath(file_path)))
                or self._records.get(canonical_media_path(file_path)))

    def _thumbnails_enabled(self):
        return self._sm is None or bool(self._sm.get('library_thumbnails_enabled', True))

    def apply_thumbnail_preference(self):
        if self._thumbnails_enabled():
            self._requeue_visible_thumbnail_jobs()
            return
        for worker in tuple(self._thumbnail_workers.values()):
            worker.cancel_event.set()
        self._recent_thumbnails.clear()
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            card.release_thumbnail_image()
        self._deferred_thumbnail_paths.clear()

    def _start_thumbnail_worker(self, file_path: str) -> None:
        if not self._thumbnails_enabled():
            return
        if self._background_paused or self._shutdown_started:
            return
        card = self._thumb_widgets.get(file_path)
        if (card is None or not card.ready
                or card._mutation_busy
                or card._thumbnail_ready
                or file_path in self._thumbnail_jobs_inflight):
            return
        getattr(self, '_deferred_thumbnail_paths', set()).discard(file_path)
        try:
            fingerprint = self._indexed_record(file_path).fingerprint
        except AttributeError:
            fingerprint = ''
        cached = self._recent_thumbnails.get(file_path)
        if cached is not None:
            if cached[0] == fingerprint:
                self._recent_thumbnails.move_to_end(file_path)
                _, card._thumb_pixmap, duration, text, tooltip = cached
                card._thumbnail_ready = True
                card._thumbnail_fingerprint = fingerprint
                if card.is_video:
                    card.set_source_duration(duration)
                card.metadata_label.setText(text)
                card.metadata_label.setToolTip(tooltip)
                card._render_thumbnail()
                return
            self._recent_thumbnails.pop(file_path, None)
        if getattr(self, '_scrolling', False):
            self._deferred_thumbnail_paths.add(file_path)
            return
        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
            self._deferred_thumbnail_paths.add(file_path)
            return
        generation = self._thumbnail_generation
        if card.is_video:
            worker = _ThumbnailWorker(file_path, _WorkerCancellation(self._thumbnail_cancel_event))
            worker.signals.finished.connect(
                lambda path, cache, duration, expected=fingerprint, generation=generation:
                    self._on_thumb_ready(
                        path, cache, duration, expected, generation))
        else:
            worker = _ImageThumbnailWorker(
                file_path, _WorkerCancellation(self._thumbnail_cancel_event))
            worker.signals.finished.connect(
                lambda path, image, expected=fingerprint, generation=generation:
                    self._on_image_thumb_ready(
                        path, image, expected, generation))
        worker.signals.diagnostic_finished.connect(
            lambda elapsed_ms, metadata_ms, cache_hit,
                   path=file_path, generation=generation, expected=fingerprint:
                self._on_thumb_diagnostic(
                    elapsed_ms, metadata_ms, cache_hit,
                    path, generation, expected))
        self._thumbnail_jobs_inflight.add(file_path)
        self._thumbnail_workers[file_path] = worker
        self._thumbnail_job_owners[file_path] = (generation, fingerprint)
        self._thumbnail_queue_peak = max(
            self._thumbnail_queue_peak, len(self._thumbnail_jobs_inflight))
        self._thumbnail_job_fingerprints[file_path] = fingerprint
        self._thumbnail_jobs_submitted += 1
        self._thread_pool.start(worker)

    def _create_thumbnail(
            self, file_path: str, card_width: int,
            *, ready_override: bool | None = None, parent=None) -> ClipThumbnail:
        is_video = is_completed_video_path(file_path)
        uploaded = bool(
            self._upload_checker and self._upload_checker(file_path))
        upload_info = (
            self._upload_info_checker(file_path)
            if uploaded and self._upload_info_checker else None)
        ready = bool(
            ready_override
            if ready_override is not None
            else (self._readiness_checker(file_path)
                  if self._readiness_checker else True))
        options = dict(
            imported=file_path in self._imported_files,
            upload_enabled=bool(self._upload_enabled and self._upload_enabled()),
            uploaded=uploaded, upload_info=upload_info, ready=ready,
            card_width=card_width)
        pool = self._recycled_cards
        match = next((index for index, card in enumerate(pool)
                      if card.is_video == is_video), None)
        if match is not None:
            thumb = pool.pop(match)
            if thumb.parentWidget() is not (parent or self._sections_host):
                thumb.setParent(parent or self._sections_host)
            thumb.rebind(file_path, **options)
        else:
            if pool and len(self._thumb_widgets) + len(pool) >= _MAX_MATERIALIZED_CARDS:
                pool.pop().deleteLater()
            thumb = ClipThumbnail(
                file_path, is_video=is_video, **options,
                clips_root=self.clips_dir, defer_image_load=not is_video,
                parent=parent or self._sections_host)
            thumb.opened.connect(self.clip_opened.emit)
            thumb.clicked.connect(self.clip_clicked.emit if is_video else self.screenshot_clicked.emit)
            thumb._prepare_mutation = self._prepare_card_mutation
            thumb._finish_mutation = self._finish_card_mutation
            thumb._wait_for_readers = self._release_thumbnail_reader
            thumb.renamed.connect(self._on_clip_renamed)
            thumb.deleted.connect(self._on_clip_deleted)
            thumb.upload_requested.connect(self.clip_upload_requested.emit)
        thumb.set_upload_status(self._upload_statuses.get(file_path, ''))
        thumb._duration_provider = self._display_duration
        self.thumbnails.append(thumb)
        self._thumb_widgets[file_path] = thumb
        return thumb

    def set_upload_status(self, path, message, *, feedback=''):
        if message:
            self._upload_statuses[path] = message
        else:
            self._upload_statuses.pop(path, None)
        card = self._thumb_widgets.get(path)
        if card is not None:
            card.set_upload_status(message, feedback=feedback)

    def _add_section(
            self, header_text: str, files: list[str], starting_idx: int,
            *, insert_at: int | None = None,
            ready_overrides: dict[str, bool] | None = None):
        """Build a date header with a small grid or a virtual canvas.

        Above the card budget, store geometry and position only visible cards
        through _layout_virtual_section instead of allocating every layout item.
        """
        section = QFrame()
        section.setStyleSheet('background: transparent;')
        sl = QVBoxLayout(section)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(8)

        # Header row: date + dropdown caret (visual)
        hdr_row = QHBoxLayout()
        hdr_row.setContentsMargins(0, 0, 0, 0)
        hdr_row.setSpacing(10)

        date_lbl = QLabel(header_text)
        date_lbl.setProperty('class', 'sectionHeader')
        set_theme_style(date_lbl, lambda: (f'color: {Colors.TEXT};'
            f' font-family: {Fonts.DISPLAY}; font-weight: 600;'
            f' font-size: {Fonts.SIZE_BODY_L}px;'
            f' letter-spacing: {Fonts.TRACK_LABEL}px;'
            f' background: transparent;'))
        hdr_row.addWidget(date_lbl)

        # Game/source name comes from the most-recent file's parent folder
        sample_game = (
            _game_name_from_path(files[0], self.clips_dir) if files else '')
        if sample_game:
            sub_lbl = QLabel(f'·  {sample_game}')
            set_theme_style(sub_lbl, lambda: (f'color: {Colors.TEXT_DIM}; font-family: {Fonts.BODY};'
                f' font-size: {Fonts.SIZE_BODY}px; background: transparent;'))
            hdr_row.addWidget(sub_lbl)

        hdr_row.addStretch(1)
        sl.addLayout(hdr_row)

        self._section_files[header_text] = list(files)
        self._section_widgets[header_text] = section
        for fp, value in (ready_overrides or {}).items():
            self._virtual_ready[fp] = bool(value)

        # Do not create a partially materialized grid with one spacer per
        # undisplayed file.  That old placeholder model was still O(total
        # files) in both QObject and QLayoutItem count.
        use_virtual = (
            len(self._records) > _MAX_MATERIALIZED_CARDS
            or len(files) > _MAX_MATERIALIZED_CARDS
            or len(self._thumb_widgets) + len(files)
                > _MAX_MATERIALIZED_CARDS)
        if use_virtual:
            canvas = QWidget(section)
            canvas.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            canvas.setMinimumWidth(1)
            self._virtual_sections[header_text] = canvas
            sl.addWidget(canvas)
            self._set_virtual_canvas_height(header_text)
        else:
            cols = self._current_columns
            widths = self._current_card_widths
            grid = QGridLayout()
            grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            grid.setHorizontalSpacing(self._GRID_SPACING)
            grid.setVerticalSpacing(self._GRID_SPACING)
            grid.setContentsMargins(0, 0, 0, 0)
            for i, fp in enumerate(files):
                ready_override = self._virtual_ready.get(fp)
                row, col = divmod(i, cols)
                thumb = self._create_thumbnail(
                    fp, widths[col], ready_override=ready_override)
                grid.addWidget(
                    thumb, row, col,
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                )
                if not self._in_transition:
                    thumb.fade_in(delay_ms=min((starting_idx + i) * 35, 600))
                self._start_thumbnail_worker(fp)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            sl.addLayout(grid)
            self._section_grids[header_text] = grid
        if insert_at is None:
            self._sections_layout.addWidget(section)
        else:
            self._sections_layout.insertWidget(insert_at, section)

    def _scroll_viewport(self):
        parent = self.parentWidget()
        for _ in range(5):
            if parent is None:
                return None
            if (hasattr(parent, 'verticalScrollBar')
                    and callable(parent.verticalScrollBar)):
                return parent.viewport()
            parent = parent.parentWidget()
        return None

    def navigation_paths(self, *, videos_only=False):
        """Return the displayed order, including unmaterialized library cards."""
        paths = []
        keys = {widget: key for key, widget in self._section_widgets.items()}
        for index in range(self._sections_layout.count()):
            key = keys.get(self._sections_layout.itemAt(index).widget())
            paths.extend(self._section_files.get(key, ()))
        if videos_only:
            paths = [path for path in paths
                     if Path(path).suffix.casefold() in _VIDEO_EXTS
                     and (self._readiness_checker(path)
                          if self._readiness_checker else self._virtual_ready.get(path, True))
                     and path not in self._mutation_paths]
        return paths

    def adjacent_clip(self, path, step):
        paths = self.navigation_paths()
        if path not in paths:
            return None
        index = paths.index(path) + step
        # Check only candidate files. Stating every file in a large or linked
        # library on each arrow press would block the GUI on filesystem I/O.
        while 0 <= index < len(paths):
            candidate = paths[index]
            ready = (self._readiness_checker(candidate) if self._readiness_checker
                     else self._virtual_ready.get(candidate, True))
            if (Path(candidate).suffix.casefold() in _VIDEO_EXTS
                    and ready and candidate not in self._mutation_paths
                    and os.path.isfile(candidate)):
                return candidate
            index += step
        return None

    def _set_virtual_canvas_height(self, section_key: str) -> None:
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return
        cols = max(1, self._current_columns)
        rows = (len(files) + cols - 1) // cols
        height = max(0, rows * self._virtual_row_height()
                     - (self._GRID_SPACING if rows else 0))
        if canvas.height() != height or canvas.minimumHeight() != height:
            canvas.setFixedHeight(height)

    def _virtual_row_height(self) -> int:
        width = max(self._current_card_widths, default=_CARD_W)
        return round(width * 9 / 16) + _CARD_BODY_H + self._GRID_SPACING

    def _release_virtual_card(self, path: str) -> None:
        card = self._thumb_widgets.get(path)
        if card is not None and (card._mutation_busy or card._editing_name):
            return
        for popup in (QApplication.activeModalWidget(), QApplication.activePopupWidget()):
            if card is not None and popup is not None and card.isAncestorOf(popup):
                return
        self._deferred_thumbnail_paths.discard(path)
        card = self._thumb_widgets.pop(path, None)
        self._card_virtual_sections.pop(path, None)
        if card is None:
            return
        worker = self._thumbnail_workers.get(path)
        if worker is not None:
            # Cancel this file only. Its terminal callback still owns cleanup;
            # clearing the thread pool would strand queued workers' tokens.
            worker.cancel_event.set()
        if card._thumbnail_ready:
            self._recent_thumbnails[path] = (
                card._thumbnail_fingerprint,
                card._thumb_pixmap,
                card._source_duration if card.is_video else 0.0,
                card.metadata_label.text(), card.metadata_label.toolTip())
            self._recent_thumbnails.move_to_end(path)
            while len(self._recent_thumbnails) > _MAX_MATERIALIZED_CARDS:
                self._recent_thumbnails.popitem(last=False)
        try:
            self.thumbnails.remove(card)
        except ValueError:
            pass
        card.hide()
        card._feedback_timer.stop()
        card._thumb_pixmap = QPixmap()
        card.thumb_label.clear()
        # Discard excess spare controls even while scrolling a large library.
        if len(self._recycled_cards) < max(4, self._current_columns * 2):
            self._recycled_cards.append(card)
        else:
            card.deleteLater()

    def _convert_section_to_virtual(self, section_key: str) -> None:
        """Replace a small grid before it can grow an unbounded placeholder list."""
        grid = self._section_grids.pop(section_key, None)
        section = self._section_widgets.get(section_key)
        if grid is None or section is None:
            return
        section_layout = section.layout()
        if section_layout is None:
            return
        grid_index = next(
            (index for index in range(section_layout.count())
             if section_layout.itemAt(index).layout() is grid), None)
        if grid_index is None:
            return
        section_layout.takeAt(grid_index)
        for path in list(self._section_files.get(section_key, ())):
            card = self._thumb_widgets.pop(path, None)
            if card is None:
                continue
            try:
                self.thumbnails.remove(card)
            except ValueError:
                pass
            card.setParent(None)
            card.deleteLater()
        canvas = QWidget(section)
        canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._virtual_sections[section_key] = canvas
        section_layout.insertWidget(grid_index, canvas)
        self._set_virtual_canvas_height(section_key)
        self._layout_virtual_section(section_key)

    def _virtual_section_plan(self, section_key: str, viewport):
        """Calculate visibility once; card coordinates live in the stable host."""
        canvas = self._virtual_sections.get(section_key)
        files = self._section_files.get(section_key, ())
        if canvas is None:
            return []
        self._set_virtual_canvas_height(section_key)
        cols = max(1, self._current_columns)
        row_height = self._virtual_row_height()
        rows = (len(files) + cols - 1) // cols
        if viewport is None:
            row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        else:
            try:
                top = canvas.mapTo(viewport, QPoint(0, 0)).y()
                row_start = max(
                    0, int((-top) // row_height) - _VIRTUAL_OVERSCAN_ROWS)
                row_end = min(
                    rows,
                    int((viewport.height() - top) // row_height)
                    + _VIRTUAL_OVERSCAN_ROWS + 1,
                )
            except (RuntimeError, AttributeError):
                row_start, row_end = 0, min(rows, _MAX_MATERIALIZED_CARDS // cols + 1)
        wanted: list[tuple[str, int, int]] = []
        for row in range(row_start, row_end):
            for col in range(cols):
                index = row * cols + col
                if index >= len(files):
                    break
                wanted.append((files[index], row, col))
        # A very tall viewport must still respect the global live-card bound.
        return wanted[:_MAX_MATERIALIZED_CARDS]

    def _layout_virtual_section(self, section_key: str, *, release_only=False) -> None:
        """Refresh one changed section outside the batched scrolling path."""
        wanted = self._virtual_section_plan(section_key, self._scroll_viewport())
        wanted_paths = {path for path, _, _ in wanted}
        for path, owner in list(self._card_virtual_sections.items()):
            if owner == section_key and path not in wanted_paths:
                self._release_virtual_card(path)
        if release_only:
            return
        self._materialize_virtual_section(section_key, wanted)

    def _materialize_virtual_section(self, section_key: str, wanted, *, deadline=None) -> bool:
        if not wanted:
            return True
        canvas = self._virtual_sections[section_key]
        widths = self._current_card_widths
        row_height = self._virtual_row_height()
        origin = canvas.mapTo(self._sections_host, QPoint(0, 0))
        column_offsets = []
        offset = origin.x()
        for width in widths:
            column_offsets.append(offset)
            offset += width + self._GRID_SPACING
        for path, row, col in wanted:
            card = self._thumb_widgets.get(path)
            if card is None:
                if len(self._thumb_widgets) >= _MAX_MATERIALIZED_CARDS:
                    break
                if deadline is not None and time.perf_counter() >= deadline:
                    return False
                card = self._create_thumbnail(
                    path, widths[col],
                    ready_override=self._virtual_ready.get(path),
                    parent=self._sections_host)
                self._card_virtual_sections[path] = section_key
                card.fade_in(delay_ms=0)
                self._start_thumbnail_worker(path)
            elif self._card_virtual_sections.get(path) != section_key:
                self._card_virtual_sections[path] = section_key
            # Keep the widget tree under one parent across date boundaries.
            # Reparenting every child causes stylesheet/layout work on Windows.
            card.resize_card(widths[col])
            geometry = QRect(
                column_offsets[col],
                origin.y() + row * row_height,
                widths[col], card.height())
            if card.geometry() != geometry:
                card.setGeometry(geometry)
            if card.isHidden():
                card.show()
                card.raise_()
        return True

    def _request_virtual_update(self, *_args) -> None:
        if (not self._shutdown_started and not self._background_paused
                and not self._virtual_update_timer.isActive()):
            self._virtual_update_timer.start()

    def _update_virtualized_cards(self, *_args) -> None:
        """Materialize only visible/overscan records and release distant cards."""
        if self._shutdown_started or self._background_paused:
            return
        viewport = self._scroll_viewport()
        plans = {
            key: self._virtual_section_plan(key, viewport)
            for key in tuple(self._virtual_sections)
        }
        wanted_paths = {path for wanted in plans.values() for path, _, _ in wanted}
        # Release globally before allocating, including when returning to newer
        # dates. Do not scan the full card registry once for every date section.
        for path in tuple(self._card_virtual_sections):
            if path not in wanted_paths:
                self._release_virtual_card(path)
        # Creating a Qt card polishes its whole child tree. During a fast wheel
        # burst, yield after a small budget rather than creating a row at once.
        # Recalculate visibility on the next frame, so obsolete work is skipped.
        deadline = time.perf_counter() + .004 if self._scrolling else None
        for section_key, wanted in plans.items():
            if not self._materialize_virtual_section(section_key, wanted, deadline=deadline):
                self._request_virtual_update()
                break
        self._virtual_initialized = True

    def _insert_saved_path(self, files: list[str], file_path: str) -> None:
        """Insert after equal keys using scanner metadata, without filesystem I/O."""
        if file_path in files:
            return
        longest = self._sort == 'longest'
        direction = 1 if self._sort == 'oldest' else -1
        # Use raw paths: canonicalizing each path can itself touch the disk.
        values = {
            record.path: direction * (record.size if longest else record.mtime_ns)
            for record in self._records.values()
        }
        # Match the previous sort's zero fallback for unavailable metadata.
        key = lambda path: values.get(path, 0)
        files.insert(bisect_right(files, key(file_path), key=key), file_path)

    def upsert_saved_clip(self, file_path: str, *, ready: bool) -> None:
        """Insert or finalize a known saved path without rescanning the library.

        Update only its date section so save work stays independent of library size.
        """
        update_started = time.monotonic()
        file_path = os.path.abspath(os.path.normpath(os.fspath(file_path)))
        if not is_completed_video_path(file_path):
            return

        if self._background_paused or self._in_transition:
            self._pending_saved_clips[file_path] = (
                bool(ready)
                or self._pending_saved_clips.get(file_path, False))
            if self._background_paused:
                self._background_refresh_pending = True
            return

        if self._known_files is None:
            self._known_files = set(self._thumb_widgets)
        self._known_files.add(file_path)

        existing = self._thumb_widgets.get(file_path)
        if existing is not None:
            self._virtual_ready[file_path] = bool(ready)
            existing.set_ready(ready)
            existing_record = self._records.get(canonical_media_path(file_path))
            if self._active_scan_worker is not None and existing_record is not None:
                self._scan_upserts[existing_record.identity] = existing_record
            if ready:
                self._start_thumbnail_worker(file_path)
            emit_event(
                'library', 'saved_clip_upserted', state='UPDATED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        # A virtualized record may already be indexed without having a live
        # card. Do not create a second path entry for a CLIP_SAVED race.
        identity = canonical_media_path(file_path)
        if identity in self._records:
            self._virtual_ready[file_path] = bool(ready)
            if self._active_scan_worker is not None:
                self._scan_upserts[identity] = self._records[identity]
            emit_event(
                'library', 'saved_clip_upserted', state='INDEXED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round((time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            self._update_virtualized_cards()
            return

        # A locally saved video is not visible in screenshot/imported-only
        # views.  It is still added to _known_files so the debounced filesystem
        # watcher sees no unexplained change and does not trigger a rebuild.
        if self._filter not in {'all', 'clips'}:
            emit_event(
                'library', 'saved_clip_upserted', state='FILTERED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return

        section_key = 'OTHER'
        try:
            info = os.stat(file_path)
            record = LibraryRecord(
                file_path, identity, (canonical_media_path(self.clips_dir),),
                False, 'video', int(info.st_size), int(info.st_mtime_ns))
            self._records[identity] = record
            section_key = _section_label_for(record.mtime)
        # CLIP_SAVED can race with a file that is still being finalized.
        except OSError:
            pass

        if self._active_scan_worker is not None:
            upserted = self._records.get(identity)
            if upserted is not None:
                self._scan_upserts[identity] = upserted

        if len(self._records) > _MAX_MATERIALIZED_CARDS:
            for key in tuple(self._section_grids):
                self._convert_section_to_virtual(key)

        grid = self._section_grids.get(section_key)
        virtual_canvas = self._virtual_sections.get(section_key)
        if grid is None and virtual_canvas is not None:
            files = self._section_files.setdefault(section_key, [])
            self._insert_saved_path(files, file_path)
            self._virtual_ready[file_path] = bool(ready)
            self._set_virtual_canvas_height(section_key)
            self._layout_virtual_section(section_key)
            self._show_empty(False)
            self._all_count_label.setText(
                f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
            emit_event(
                'library', 'saved_clip_upserted', state='ADDED',
                ready=bool(ready), full_scan=False,
                elapsed_ms=round(
                    (time.monotonic() - update_started) * 1000),
                visible_card_count=len(self._thumb_widgets))
            return
        if grid is None:
            insert_at = 0 if self._sort != 'oldest' else None
            self._add_section(
                section_key, [file_path], 0, insert_at=insert_at,
                ready_overrides={file_path: bool(ready)})
        else:
            files = self._section_files.setdefault(section_key, [])
            self._insert_saved_path(files, file_path)
            self._virtual_ready[file_path] = bool(ready)
            # Convert before the section can acquire placeholder items beyond
            # the global live-card budget. The virtual canvas has O(visible)
            # children regardless of how many records the section contains.
            if (len(files) > _MAX_MATERIALIZED_CARDS
                    or len(self._thumb_widgets) > _MAX_MATERIALIZED_CARDS):
                self._convert_section_to_virtual(section_key)
                self._set_virtual_canvas_height(section_key)
                self._layout_virtual_section(section_key)
                self._show_empty(False)
                self._all_count_label.setText(
                    f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
                emit_event(
                    'library', 'saved_clip_upserted', state='ADDED',
                    ready=bool(ready), full_scan=False,
                    elapsed_ms=round(
                        (time.monotonic() - update_started) * 1000),
                    visible_card_count=len(self._thumb_widgets))
                return
            # Reflow this small section only; no spacer is needed because this
            # branch is guaranteed to remain within the live-card bound.
            while grid.count():
                grid.takeAt(0)
            cols = self._current_columns
            widths = self._current_card_widths
            for index, path in enumerate(files):
                row, col = divmod(index, cols)
                card = self._thumb_widgets.get(path)
                if card is None and len(self._thumb_widgets) < _MAX_MATERIALIZED_CARDS:
                    card = self._create_thumbnail(
                        path, widths[col],
                        ready_override=self._virtual_ready.get(path))
                if card is not None and index < _MAX_MATERIALIZED_CARDS:
                    card.resize_card(widths[col])
                    grid.addWidget(
                        card, row, col,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            for col, width in enumerate(widths):
                grid.setColumnMinimumWidth(col, width)
            if file_path in self._thumb_widgets:
                self._thumb_widgets[file_path].fade_in()
                self._start_thumbnail_worker(file_path)
            self._request_virtual_update()

        self._show_empty(False)
        self._all_count_label.setText(
            f'{self._filter_heading()}  ({len(self._filtered_records(ordered=False))})')
        emit_event(
            'library', 'saved_clip_upserted', state='ADDED',
            ready=bool(ready), full_scan=False,
            elapsed_ms=round((time.monotonic() - update_started) * 1000),
            visible_card_count=len(self._thumb_widgets))

    def _relayout_grids(self):
        """Reposition existing cards for the new column count.

        Reusing widgets avoids flicker and restarting thumbnail workers on resize.
        """
        cols = self._current_columns
        widths = self._current_card_widths
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
                for col in range(grid.columnCount()):
                    grid.setColumnMinimumWidth(col, 0)
                    grid.setColumnStretch(col, 0)
                grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                while grid.count():
                    child = grid.takeAt(0)
                    w = child.widget()
                    if w:
                        widgets.append(w)
                for i, w in enumerate(widgets):
                    if isinstance(w, ClipThumbnail):
                        w.resize_card(widths[i % cols])
                    grid.addWidget(
                        w, i // cols, i % cols,
                        Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                    )
                for col, width in enumerate(widths):
                    grid.setColumnMinimumWidth(col, width)
        for section_key in tuple(self._virtual_sections):
            self._layout_virtual_section(section_key)

    def set_clips_directory(self, path: str, *, prepared: bool = False) -> None:
        """Switch the primary library root and refresh the visible media."""
        if prepared:
            try:
                raw_path = os.fspath(path)
                if (not isinstance(raw_path, str) or not raw_path.strip()
                        or '\x00' in raw_path or not Path(raw_path).is_absolute()):
                    raise StoragePathError(
                        'The prepared clips storage folder must be a non-empty '
                        'absolute path without NUL characters.')
                # Settings already created, write-probed, and persisted this
                # exact path. Do not probe it again before adopting the saved
                # root; _load_clips reports if it became unavailable meanwhile.
                resolved = raw_path
            except (OSError, TypeError, ValueError) as error:
                storage_error = (error if isinstance(error, StoragePathError)
                                 else StoragePathError(
                                     'The prepared clips storage folder is invalid.'))
                self._report_storage_unavailable(storage_error)
                return
        else:
            try:
                resolved = str(ensure_storage_directory(
                    path, probe_writable=True))
            except StoragePathError as error:
                self._report_storage_unavailable(error)
                return
        if resolved == self.clips_dir:
            self.force_refresh()
            return

        self._transition_seq = getattr(self, '_transition_seq', 0) + 1
        if self._transition_anim is not None:
            self._transition_anim.stop()
            self._transition_anim = None
        self._in_transition = False
        self._sections_host.setGraphicsEffect(None)

        # The prepared settings handoff must not perform a second filesystem
        # resolution before adopting its already-persisted absolute root.
        old_root = (Path(self.clips_dir) if prepared else
                    Path(self.clips_dir).resolve(strict=False))
        for watched in list(self._watcher.directories()):
            watched_path = Path(watched)
            if not prepared:
                watched_path = watched_path.resolve(strict=False)
            try:
                watched_path.relative_to(old_root)
            except ValueError:
                continue
            self._watcher.removePath(watched)

        self.clips_dir = resolved
        self._storage_error_reported = False
        self._known_files = set()
        self._imported_files = set()
        if resolved not in self._watcher.directories():
            self._watcher.addPath(resolved)
        self._load_clips()

    def _display_duration(self, path, source_duration):
        if (self._sm and self._sm.get('grid_show_edited_duration', False)
                and self._metadata_manager is not None):
            return self._metadata_manager.edited_duration(path, source_duration)
        return source_duration

    def refresh_duration_labels(self):
        for card in self._thumb_widgets.values():
            card.refresh_duration_label()

    def force_refresh(self):
        """Clear the file cache and immediately reload — called when import folders change."""
        if self._background_paused:
            self._background_refresh_pending = True
            return
        self._known_files = set()
        self._load_clips()

    def shutdown(self) -> None:
        """Stop library timers/workers within a bounded grace period."""
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._recent_thumbnails.clear()
        for timer_name in ('refresh_timer', '_debounce_timer', '_resize_timer', '_idle_memory_timer',
                           '_virtual_update_timer', '_scroll_settle_timer'):
            timer = getattr(self, timer_name, None)
            if timer is not None:
                timer.stop()
        if self._active_scan_worker is not None:
            self._active_scan_worker.cancel()
        self._pending_scan_request = None
        self._thumbnail_cancel_event.set()
        self._thread_pool.clear()
        self._scan_thread_pool.clear()
        # Third-party decoders are bounded to a short grace period; shutdown
        # must remain responsive if one ignores cancellation.
        self._thread_pool.waitForDone(1000)
        self._scan_thread_pool.waitForDone(1000)
        for file_path, (generation, fingerprint) in tuple(
                self._thumbnail_job_owners.items()):
            self._finish_thumbnail_job(file_path, generation, fingerprint)
        self._active_scan_worker = None
        for path in list(self._watcher.directories()):
            self._watcher.removePath(path)

    def _clear_sections(self):
        # Virtual cards are direct host children so crossing dates never needs
        # reparenting. Dispose those explicitly when replacing the index.
        for card in (*self._thumb_widgets.values(), *self._recycled_cards):
            if card.parentWidget() is self._sections_host:
                card.hide()
                card.deleteLater()
        self._recycled_cards.clear()
        self._section_grids.clear()
        self._section_files.clear()
        self._section_widgets.clear()
        self._virtual_sections.clear()
        self._card_virtual_sections.clear()
        while self._sections_layout.count():
            item = self._sections_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _show_empty(self, show: bool):
        if show:
            detail = ''
            if self._filter == 'screenshots':
                self._no_clips_lbl.setText('No screenshots yet')
                hotkeys = self._sm.get('hotkeys', {}) if self._sm else {}
                screenshot_key = hotkeys.get('save_screenshot', 'F12')
                detail = f'PRESS {screenshot_key} TO TAKE A SCREENSHOT'
            elif self._filter == 'clips':
                self._no_clips_lbl.setText('No clips yet')
            elif self._filter == 'imported':
                self._no_clips_lbl.setText('No imported clips')
            else:
                self._no_clips_lbl.setText('No clips yet')
            self._empty_detail_lbl.setText(detail)
            self._empty_detail_lbl.setVisible(bool(detail))
        self._empty_widget.setVisible(show)
        self._sections_host.setVisible(not show)

    def _on_clip_deleted(self, file_path: str):
        identity = canonical_media_path(file_path)
        self._records.pop(identity, None)
        if isinstance(self._known_files, set):
            self._known_files.discard(file_path)
        self._imported_files.discard(file_path)
        self._virtual_ready.pop(file_path, None)
        for section_key, files in list(self._section_files.items()):
            if file_path not in files:
                continue
            self._section_files[section_key] = [path for path in files
                                                if path != file_path]
        # This rebuild uses the in-memory index only; no filesystem walk is
        # scheduled for an external delete.
        self._apply_current_index()

    def _on_thumb_ready(self, file_path: str, cache_path: str, duration: int,
                        expected_fingerprint: str = '', generation: int | None = None):
        if not self._thumbnails_enabled():
            return
        current = self._indexed_record(file_path)
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget:
            widget.set_video_thumbnail(cache_path, duration)
            widget._thumbnail_fingerprint = expected_fingerprint or (current.fingerprint if current else '')

    def _on_image_thumb_ready(
            self, file_path: str, image: QImage,
            expected_fingerprint: str = '', generation: int | None = None):
        if not self._thumbnails_enabled():
            return
        current = self._indexed_record(file_path)
        if (generation is not None and generation != self._thumbnail_generation):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        if (expected_fingerprint and current is not None
                and current.fingerprint != expected_fingerprint):
            emit_event('library', 'stale_thumbnail_result_discarded',
                       state='DISCARDED')
            return
        widget = self._thumb_widgets.get(file_path)
        if widget is not None:
            widget.set_image_thumbnail(image)
            widget._thumbnail_fingerprint = expected_fingerprint or (current.fingerprint if current else '')

    def _on_thumb_diagnostic(
            self, elapsed_ms: int, metadata_ms: int, cache_hit: bool,
            file_path: str | None = None, generation: int | None = None,
            fingerprint: str = ''):
        # This is the single terminal cleanup path. Both successful and failed
        # workers emit diagnostic_finished from finally; cancelled workers do
        # too. Pause/rebuild leaves ownership intact until this callback so a
        # replacement can never run concurrently with its predecessor.
        if file_path is not None and generation is not None:
            worker = self._thumbnail_workers.get(file_path)
            locally_cancelled = worker is not None and worker.cancel_event.is_set()
            current = self._indexed_record(file_path)
            obsolete_owner = generation != self._thumbnail_generation
            fingerprint_changed = (
                bool(fingerprint) and current is not None
                and current.fingerprint != fingerprint)
            if self._finish_thumbnail_job(file_path, generation, fingerprint):
                self._thumbnail_jobs_completed += 1
                # Queue pressure must not strand a visible card until another
                # capture, scan or scroll happens. Only drain deferred work;
                # failed current jobs retain the normal scan retry cooldown.
                deferred = getattr(self, '_deferred_thumbnail_paths', set())
                if (deferred and not getattr(self, '_scrolling', False) and not self._background_paused
                        and not self._shutdown_started):
                    for path in tuple(deferred):
                        if len(self._thumbnail_jobs_inflight) >= _MAX_THUMBNAIL_QUEUE:
                            break
                        deferred.discard(path)
                        self._start_thumbnail_worker(path)
                # A cancelled old generation may have left a visible card
                # non-ready. Once its owner has actually reached the terminal
                # signal, enqueue exactly one replacement for the current
                # foreground generation.
                if obsolete_owner or fingerprint_changed:
                    self._requeue_visible_thumbnail_jobs()
                elif locally_cancelled:
                    self._start_thumbnail_worker(file_path)
        self._thumbnail_diagnostics_completed += 1
        self._thumbnail_total_elapsed_ms += max(0, int(elapsed_ms))
        self._metadata_total_elapsed_ms += max(0, int(metadata_ms))
        self._thumbnail_cache_hits += int(bool(cache_hit))
        active_processes, peak_processes = media_process_snapshot()
        if (self._thumbnail_diagnostics_completed == self._thumbnail_jobs_submitted
                or self._thumbnail_diagnostics_completed % 25 == 0):
            emit_event(
                'library', 'thumbnail_health_snapshot',
                metadata_jobs=self._thumbnail_jobs_submitted,
                thumbnail_jobs=self._thumbnail_jobs_submitted,
                completed_thumbnail_jobs=self._thumbnail_jobs_completed,
                completed_diagnostic_jobs=self._thumbnail_diagnostics_completed,
                total_thumbnail_elapsed_ms=self._thumbnail_total_elapsed_ms,
                total_metadata_probe_elapsed_ms=self._metadata_total_elapsed_ms,
                thumbnail_cache_hits=self._thumbnail_cache_hits,
                active_workers=self._thread_pool.activeThreadCount(),
                thumbnail_queue_depth=len(self._thumbnail_jobs_inflight),
                thumbnail_queue_peak=self._thumbnail_queue_peak,
                media_process_active=active_processes,
                media_process_peak=peak_processes,
                materialized_card_count=len(self._thumb_widgets),
                cache_entry_count=self._thumbnail_diagnostics_completed,
                process_memory_bytes=process_memory_bytes())
