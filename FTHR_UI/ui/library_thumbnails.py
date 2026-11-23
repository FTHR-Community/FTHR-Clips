"""Background library thumbnail decoding and versioned metadata caching."""

import hashlib
import math
import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, Signal
from PySide6.QtGui import QImage, QImageReader

from core.clip_files import is_completed_video_path
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.field_diagnostics import emit_event
from core.library_cache import maybe_prune_thumbnail_cache
from core.library_index import canonical_media_path
from core.media_metadata import parse_ffprobe_video_metadata
from core.media_process import run_media_process as _run_owned_media_process


THUMB_CACHE_DIR = os.path.join(os.path.expanduser('~'), '.fthr', 'thumbnails')
_NEGATIVE_CACHE_SECONDS = 30.0


def _get_cached_thumb_path(file_path: str, *, indexed=None, source_stat=None) -> str:
    """Return a cache path based on file path + mtime so stale caches auto-invalidate.

    Including mtime in the hash invalidates the cache automatically when a file
    is overwritten without requiring a separate cache index.
    """
    info = os.stat(file_path) if source_stat is None else source_stat
    identity = None
    if indexed is not None and indexed.st_ino and indexed.st_dev is not None:
        if ((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                == (indexed.st_dev, indexed.st_ino, indexed.size, indexed.mtime_ns)):
            identity = indexed.identity
    if identity is None:
        identity = canonical_media_path(file_path)
    # v4 caches preserve source aspect ratio through FFmpeg scaling. The format
    # here prevents an older, force-stretched 16:9 thumbnail from surviving an
    # application update.
    key = f'{identity}|{info.st_size}|{info.st_mtime_ns}|aspect-v4'.encode(
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


def get_cached_clip_metadata(file_path: str):
    """Read cached clip metadata without opening a decoder."""
    try:
        cache_path = _get_cached_thumb_path(file_path)
    except OSError:
        # A removed source has no cache identity; callers can show the placeholder.
        return None
    return read_cached_metadata(_get_cached_duration_path(cache_path))


class _ThumbnailSignals(QObject):
    finished = Signal(str, str, int)  # file_path, cache_path, duration_sec
    cached_ready = Signal(str, object, object)  # file_path, QImage, video metadata
    diagnostic_finished = Signal(int, int, bool)  # total_ms, metadata_ms, cache_hit


def _worker_thumb_path(worker, source_stat=None):
    indexed = getattr(worker, '_indexed_record_snapshot', None)
    if indexed is None and source_stat is None:
        return _get_cached_thumb_path(worker.file_path)
    return _get_cached_thumb_path(worker.file_path, indexed=indexed, source_stat=source_stat)


def _same_thumbnail_source(path, snapshot):
    try:
        current = os.stat(path)
    except OSError:
        # A removed or unavailable clip cannot receive a published thumbnail.
        return False
    return ((current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns)
            == (snapshot.st_dev, snapshot.st_ino, snapshot.st_size, snapshot.st_mtime_ns,
                snapshot.st_ctime_ns))


def _read_cached_thumbnail(path: str) -> QImage | None:
    """Load one validated cache image without opening the source media."""
    try:
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        image = reader.read()
        return image if not image.isNull() else None
    except (OSError, RuntimeError):
        # A damaged cached JPEG is a miss, never a reason to reject the clip.
        return None


def _valid_cached_thumbnail(path: str) -> bool:
    return _read_cached_thumbnail(path) is not None


class _CachedThumbnailSignals(QObject):
    finished = Signal(str, object, object)  # file_path, image, optional video metadata
    diagnostic_finished = Signal(int, int, bool)


class _CachedThumbnailWorker(QRunnable):
    """Read an existing disk thumbnail while cards are moving through the grid."""

    def __init__(self, file_path, is_video, cancel_event):
        super().__init__()
        self.file_path = file_path
        self.is_video = is_video
        self.cancel_event = cancel_event
        self.signals = _CachedThumbnailSignals()

    def run(self):
        started = time.monotonic()
        cache_hit = False
        try:
            if self.cancel_event.is_set():
                return
            cache_path = _worker_thumb_path(self)
            if not os.path.isfile(cache_path) or self.cancel_event.is_set():
                return
            metadata = None
            if self.is_video:
                metadata = read_cached_metadata(_get_cached_duration_path(cache_path))
            image = _read_cached_thumbnail(cache_path)
            if image is not None and not self.cancel_event.is_set():
                cache_hit = True
                self.signals.finished.emit(self.file_path, image, metadata)
        except OSError:
            # Removal or an unavailable cache is a miss; the settled viewport can retry.
            pass
        finally:
            self.signals.diagnostic_finished.emit(
                round((time.monotonic() - started) * 1000), 0, cache_hit)


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
         '-threads', '2', '-filter_threads', '1', '-i', file_path, '-frames:v', '1', '-vf',
         "scale=w='min(640,iw)':h='min(360,ih)':force_original_aspect_ratio=decrease",
         '-threads:v', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
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
            if self.cancel_event.is_set():
                return
            if not is_completed_video_path(self.file_path):
                self.signals.finished.emit(self.file_path, '', 0)
                return
            source_stat = os.stat(self.file_path)
            cache_path = _worker_thumb_path(self, source_stat)
            dur_path   = _get_cached_duration_path(cache_path)
            failed_path = _get_negative_cache_path(cache_path)
            preview_only = getattr(self, '_thumbnail_motion_preview', False)

            # Fingerprinting in _get_cached_thumb_path means mutations select a
            # new generation; corrupt entries never poison the next run.
            exists = os.path.isfile(cache_path)
            image = _read_cached_thumbnail(cache_path) if exists else None
            metadata = read_cached_metadata(dur_path)
            duration = metadata[0] if metadata is not None else 0.0
            if image is not None and (metadata is not None or preview_only):
                if self.cancel_event.is_set():
                    return
                cache_hit = True
                if getattr(self, '_deliver_cached_image', False):
                    self.signals.cached_ready.emit(self.file_path, image, metadata)
                else:
                    self.signals.finished.emit(self.file_path, cache_path, int(duration))
                return
            if exists and image is None:
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
                self.signals.finished.emit(self.file_path, '', int(duration))
                return
            if metadata is None and not preview_only:
                metadata_started = time.monotonic()
                probed = _probe_with_owned_process(self.file_path, self.cancel_event)
                metadata_ms = round((time.monotonic() - metadata_started) * 1000)
                if self.cancel_event.is_set() or not _same_thumbnail_source(self.file_path, source_stat):
                    return
                if probed is not None:
                    duration = probed.duration_seconds or 0.0
                    os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
                    _write_cached_duration(
                        dur_path, duration, probed.width or 0, probed.height or 0,
                        probed.average_fps or 0.0, probed.video_bitrate_bps or 0,
                        probed.total_bitrate_bps or 0)
            if self.cancel_event.is_set():
                return
            cached_pixels = image is not None
            if not cached_pixels:
                image = _decode_thumbnail_with_owned_process(self.file_path, self.cancel_event)
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

            if self.cancel_event.is_set() or not _same_thumbnail_source(self.file_path, source_stat):
                return
            os.makedirs(THUMB_CACHE_DIR, exist_ok=True)
            if not os.path.exists(cache_path):
                temp_path = cache_path + f'.tmp-{os.getpid()}-{threading.get_ident()}.jpg'
                if image.save(temp_path, 'JPG', 85):
                    os.replace(temp_path, cache_path)
                else:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
            if cached_pixels and getattr(self, '_deliver_cached_image', False):
                self.signals.cached_ready.emit(self.file_path, image, read_cached_metadata(dur_path))
            else:
                self.signals.finished.emit(self.file_path, cache_path, int(duration))
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
                if cache_path and not self.cancel_event.is_set():
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
            if self.cancel_event.is_set():
                return
            cache_path = _worker_thumb_path(self)
            failed_path = _get_negative_cache_path(cache_path)
            image = _read_cached_thumbnail(cache_path) if os.path.exists(cache_path) else None
            if image is not None:
                if self.cancel_event.is_set():
                    return
                cache_hit = True
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
                if cache_path and not self.cancel_event.is_set():
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
