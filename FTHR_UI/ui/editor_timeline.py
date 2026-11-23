"""Editor timeline with trim, playhead scrubbing, markers and filmstrip previews."""

from bisect import bisect_right
from collections import OrderedDict
import json
import threading
import time

from PySide6.QtCore import QLine, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import QFrame, QMenu, QScrollBar

from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.audio_timeline import MAX_WAVEFORM_MS, decode_waveform
from core.highlight_markers import normalize_highlight_markers
from core.media_identity import file_identity
from ui.editor_layout import AUDIO_LANE_HEIGHT
from ui.image_memory import ImageLRU, start_image_cache_maintenance
from ui.style import Colors, Fonts, set_theme_style
from ui.timeline_segments import TimelineSegment, TimelineSegments


_TIMELINE_CACHE_LIMIT = 8
_TIMELINE_IMAGE_CACHE = ImageLRU(20 * 1024 * 1024, max_entries=_TIMELINE_CACHE_LIMIT)
_TIMELINE_CACHE_LOCK = threading.Lock()
_WAVEFORM_CACHE = OrderedDict()


def clear_timeline_image_cache() -> None:
    with _TIMELINE_CACHE_LOCK:
        _TIMELINE_IMAGE_CACHE.clear()
        _WAVEFORM_CACHE.clear()


def _timeline_cache_key(clip_path: str, sampling: int | tuple[int, ...],
                        duration_ms: int) -> tuple | None:
    """Key previews by stable file identity so overwritten clips cannot reuse data."""

    try:
        identity = file_identity(clip_path)
    except OSError:
        # A missing/in-flight clip simply cannot contribute a reusable filmstrip.
        return None
    return (*identity, sampling, duration_ms)


def _cached_timeline_images(key, *, allow_partial=False) -> tuple[QImage, ...] | None:
    if key is None:
        return None
    with _TIMELINE_CACHE_LOCK:
        entry = _TIMELINE_IMAGE_CACHE.get(key)
        if entry is not None:
            _TIMELINE_IMAGE_CACHE.move_to_end(key)
            images, complete = entry
            if complete or allow_partial:
                return images
        return None


def _store_timeline_images(key, images: list[QImage], *, complete=True, cancelled=None) -> None:
    if key is None or not images:
        return
    with _TIMELINE_CACHE_LOCK:
        if cancelled is not None and cancelled.is_set():
            return
        previous = _TIMELINE_IMAGE_CACHE.get(key)
        if previous is not None and (previous[1] or len(previous[0]) > len(images)):
            return
        _TIMELINE_IMAGE_CACHE[key] = (tuple(images), complete)
        _TIMELINE_IMAGE_CACHE.move_to_end(key)
        while len(_TIMELINE_IMAGE_CACHE) > _TIMELINE_CACHE_LIMIT:
            _TIMELINE_IMAGE_CACHE.popitem(last=False)


def _timeline_seek_points(clip_path, spacing, start, count, cancelled):
    """Use indexed seeks only when they avoid enough GOP preroll to pay off."""

    if spacing < 5 or count - start < 2:
        return ()
    from core.media_process import run_media_process

    points = tuple((index + 0.5) * spacing for index in range(start, count))
    intervals = ','.join(f'{point:.6f}%+#1' for point in points)
    try:
        result = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-select_streams', 'V:0',
             '-read_intervals', intervals, '-show_entries',
             'packet=pts_time,flags:format=start_time', '-of', 'json', clip_path],
            cancelled, timeout_seconds=3.0, background='timeline')
        if result is None or result[0] != 0:
            return ()
        probe = json.loads(result[1])
        packets = probe['packets']
        if (len(packets) != len(points)
                or abs(float(probe['format']['start_time'])) > 0.001
                or any('K' not in packet['flags'] for packet in packets)):
            return ()
        preroll = [point - float(packet['pts_time'])
                   for point, packet in zip(points, packets, strict=True)]
    except (FFmpegUnavailable, KeyError, OSError, TypeError, ValueError):
        return ()

    # Include the cost of launching a child for each sample. Long GOPs may
    # make several seeks decode the same frames, so keep the sequential pass
    # if the index does not show a clear saving.
    if (not all(0 <= gap < spacing * 2 for gap in preroll)
            or sum(preroll) + 2 * len(points) >= 0.8 * spacing * len(points)):
        return ()
    return points


def _theme_color_with_alpha(value: str, alpha: int) -> QColor:
    color = QColor(value)
    color.setAlpha(alpha)
    return color


class TrimSlider(QFrame):
    """Thumbnail timeline with trim, zoom, split, delete and click-to-seek."""

    range_changed  = Signal(float, float)
    seek_requested = Signal(float)
    playhead_scrub_finished = Signal()
    segments_changed = Signal()
    selection_changed = Signal(int, bool)
    zoom_changed = Signal(float)
    thumbnails_ready = Signal(object)
    range_edit_started = Signal()
    range_edit_finished = Signal()
    context_split_requested = Signal(float)
    context_toggle_requested = Signal(int)
    hide_deleted_changed = Signal(bool)
    highlight_activated = Signal(object)
    audio_selection_changed = Signal()
    audio_segments_changed = Signal()
    audio_context_split_requested = Signal(float)
    audio_context_toggle_requested = Signal(int)
    restore_inherited_audio_requested = Signal(int)
    waveform_ready = Signal(object)
    waveform_visibility_changed = Signal(bool)
    audio_waveform_finished = Signal()
    audio_geometry_changed = Signal()

    def __init__(self, duration_ms: int, parent=None, *, maintain_cache=True,
                 audio_lane=False, source_lanes=False):
        super().__init__(parent)
        self.duration_ms  = max(duration_ms, 1)
        self.start_pct    = 0.0
        self.end_pct      = 1.0
        self.playhead_pct = 0.0
        self.dragging     = None
        self._trim_drag_origin = None
        self.segments = [TimelineSegment(0.0, 1.0)]
        self.hide_deleted_segments = False
        self.selected_segment = 0
        self.audio_lane = audio_lane
        self.source_lanes = source_lanes
        self.editing_audio = False
        self.active_audio_source: str | None = None
        self.audio_source_ids: tuple[str, ...] = ()
        self.audio_muted_sources: frozenset[str] = frozenset()
        self._audio_source_segments: dict[str, tuple] = {}
        self._lane_capacity = 1
        self._lane_offset = 0
        self.selected_audio_position = 0.0
        self.audio_segments = (TimelineSegment(0.0, 1.0),)
        self._audio_lane_key = None
        self._audio_lane_intervals = {}
        self.waveform_enabled = audio_lane
        self._waveform_job = 0
        self._waveform_cancel = threading.Event()
        self._waveform_loading = False
        self._waveform_complete = False
        self._waveform_peaks = ()
        self._source_waveform_peaks = {}
        self.zoom_factor = 1.0
        # Keep roughly a five-second source window reachable even for
        # multi-hour recordings, while preserving the familiar 8× cap for
        # ordinary replay clips.
        self.max_zoom_factor = max(
            8.0, min(2048.0, self.duration_ms / 5000.0))
        self.view_start_pct = 0.0
        self._thumbnails: list[QPixmap] = []
        self._scaled_thumbnails = ImageLRU(4 * 1024 * 1024)
        self.preview_strip_enabled = True
        self._thumbnail_source = None
        self._thumbnail_count = 0
        self._thumbnail_loading = False
        self._thumbnail_complete = False
        if maintain_cache:
            start_image_cache_maintenance()
        self._thumbnail_job = 0
        self._thumbnail_cancel = threading.Event()
        self._thumbnail_worker_starts = 0
        self._timeline_background = QPixmap()
        self._timeline_background_key = None
        self._range_mapping_key = None
        self._highlight_markers: tuple = ()
        self._highlight_revision = 0
        self._highlight_geometry_key = None
        self._highlight_hit_groups: tuple = ()
        self._highlight_interval_key = None
        self._highlight_intervals: tuple[tuple[int, int], ...] = ()
        self._highlight_interval_starts: tuple[int, ...] = ()
        self._painting_timeline = False
        self.setFixedHeight(116 + (AUDIO_LANE_HEIGHT if audio_lane else 0))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.setStyleSheet('QFrame { background-color: transparent; border: none; }')
        if source_lanes:
            self._audio_scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
            self._audio_scrollbar.setObjectName('audioLaneScroll')
            self._audio_scrollbar.setSingleStep(1)
            self._audio_scrollbar.valueChanged.connect(self._on_lane_scroll)
            self._audio_scrollbar.hide()
        self.thumbnails_ready.connect(self._set_thumbnail_images)
        self.waveform_ready.connect(self._set_waveform_result)

    @property
    def view_span_pct(self) -> float:
        return 1.0 / max(self.zoom_factor, 1.0)

    @property
    def segments(self):
        return self._segments

    @segments.setter
    def segments(self, values):
        old = self.__dict__.get('_segments')
        if values is old:  # augmented assignment already advanced the version
            return
        replacement = TimelineSegments(values)
        if old is not None:
            old.detach()
        self._segments = replacement
        self._segments_generation = self.__dict__.get('_segments_generation', 0) + 1
        self._range_mapping_key = None
        self._timeline_background_key = None
        self._highlight_geometry_key = None
        self._highlight_interval_key = None

    def _ensure_range_mapping(self):
        if self._painting_timeline:
            return
        key = (self._segments_generation, self._segments.version)
        if key == self._range_mapping_key:
            return
        ranges = [(s.start_pct, s.end_pct) for s in self.segments if not s.deleted]
        self._mapped_ranges = ranges or [(0.0, 1.0)]
        self._range_starts = [start for start, _ in self._mapped_ranges]
        self._range_ends = [end for _, end in self._mapped_ranges]
        self._range_offsets = [0.0]
        for start, end in self._mapped_ranges:
            self._range_offsets.append(self._range_offsets[-1] + (end - start))
        self._range_cumulative_ends = self._range_offsets[1:]
        self._range_mapping_key = key

    def _source_to_display(self, pct):
        if not self.hide_deleted_segments:
            return pct
        self._ensure_range_mapping()
        index = bisect_right(self._range_ends, pct)
        offset = self._range_offsets[index]
        if index < len(self._mapped_ranges):
            start, end = self._mapped_ranges[index]
            offset += max(0.0, min(pct, end) - start)
        return offset / self._range_offsets[-1]

    def _display_to_source(self, pct):
        if not self.hide_deleted_segments:
            return pct
        self._ensure_range_mapping()
        offset = max(0.0, min(1.0, pct)) * self._range_offsets[-1]
        index = bisect_right(self._range_cumulative_ends, offset + 1e-9)
        if index >= len(self._mapped_ranges):
            return self._range_ends[-1]
        return self._range_starts[index] + offset - self._range_offsets[index]

    def set_hide_deleted_segments(self, enabled):
        enabled = bool(enabled)
        if enabled == self.hide_deleted_segments:
            return
        self.hide_deleted_segments = enabled
        self.view_start_pct = 0.0
        self._timeline_background_key = None
        self._highlight_geometry_key = None
        self._highlight_interval_key = None
        self.ensure_visible(self.playhead_pct)
        self.hide_deleted_changed.emit(enabled)
        self.update()

    def set_zoom(self, factor: float, anchor_pct: float | None = None):
        old_span = self.view_span_pct
        factor = max(1.0, min(self.max_zoom_factor, float(factor)))
        anchor = self._source_to_display(self.playhead_pct if anchor_pct is None else anchor_pct)
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
        pct = self._source_to_display(pct)
        span = self.view_span_pct
        if self.view_start_pct <= pct <= self.view_start_pct + span:
            return
        offset = span * (0.08 if align_start else 0.5)
        self.view_start_pct = max(0.0, min(1.0 - span, pct - offset))
        self.update()

    def set_preview_strip_enabled(self, enabled):
        self.preview_strip_enabled = bool(enabled)
        if not self.preview_strip_enabled:
            self.release_thumbnail_resources()
            clear_timeline_image_cache()
        self._timeline_background_key = None
        self.update()

    def set_highlight_markers(self, markers):
        """Replace the bounded source-time marker cache used by the timeline."""

        self._highlight_markers = normalize_highlight_markers(list(markers))
        self._highlight_revision += 1
        self._highlight_geometry_key = None
        self._timeline_background_key = None
        self.update()

    def highlight_position_pct(self, marker) -> float | None:
        """Convert a marker's retained presentation position to source pct."""

        try:
            position_100ns = marker.position_100ns
        except AttributeError:
            try:
                position_100ns = marker['position_100ns']
            except (KeyError, TypeError):
                # A malformed read-only marker cannot map to the timeline;
                # leave it unavailable instead of interrupting clip opening.
                return None
        if type(position_100ns) is not int or self.duration_ms <= 0:
            return None
        duration_100ns = self.duration_ms * 10_000
        return max(0.0, min(1.0, position_100ns / duration_100ns))

    def _highlight_kept_intervals(self):
        """Return retained source intervals in exact 100 ns coordinates."""

        duration_100ns = self.duration_ms * 10_000
        key = (self.duration_ms, self._segments_generation, self._segments.version,
               self.start_pct, self.end_pct)
        if key == self._highlight_interval_key:
            return self._highlight_intervals
        trim_start = max(0, min(duration_100ns,
                                int(self.start_pct * duration_100ns)))
        trim_end = max(trim_start, min(duration_100ns,
                                      int(self.end_pct * duration_100ns)))
        intervals = []
        for segment in self.segments:
            if segment.deleted:
                continue
            start = max(trim_start, min(duration_100ns,
                                        int(segment.start_pct * duration_100ns)))
            end = max(start, min(trim_end, int(segment.end_pct * duration_100ns)))
            if start < end:
                intervals.append((start, end))
        self._highlight_interval_key = key
        self._highlight_intervals = tuple(intervals)
        self._highlight_interval_starts = tuple(start for start, _ in intervals)
        return self._highlight_intervals

    def highlight_available(self, marker) -> bool:
        """Whether a source-time marker remains inside the retained edit."""

        try:
            position_100ns = (marker.position_100ns
                              if hasattr(marker, 'position_100ns')
                              else marker['position_100ns'])
        except (KeyError, TypeError):
            return False
        duration_100ns = self.duration_ms * 10_000
        if position_100ns < 0 or position_100ns >= duration_100ns:
            return False
        intervals = self._highlight_kept_intervals()
        index = bisect_right(self._highlight_interval_starts, position_100ns) - 1
        return (index >= 0 and position_100ns < intervals[index][1])

    def _highlight_geometry(self):
        self._ensure_range_mapping()
        key = (
            self.width(), self.height(), self.devicePixelRatioF(), self.duration_ms,
            self._highlight_revision,
            self._segments_generation, self._segments.version,
            self.hide_deleted_segments, self.view_start_pct,
            self.view_span_pct, self.start_pct, self.end_pct,
        )
        if key == self._highlight_geometry_key:
            return self._highlight_hit_groups
        pad, tw = self._time_axis()
        view_end = self.view_start_pct + self.view_span_pct
        grouped = {}
        for marker in self._highlight_markers:
            pct = self.highlight_position_pct(marker)
            if pct is None:
                continue
            # Deleted/out-of-trim markers remain visible as dim source-time
            # context, but never map through a collapsed deleted partition.
            # That keeps their paint location from becoming a false hit at a
            # retained boundary; hit testing filters them below as well.
            available = self.highlight_available(marker)
            if self.hide_deleted_segments and not available:
                # Deleted source partitions are removed from the display axis;
                # drawing their raw source coordinate would create a false hit
                # at a retained boundary.  The sidebar still reports them as
                # unavailable.
                continue
            display = self._source_to_display(pct) if available else pct
            if not self.view_start_pct <= display <= view_end:
                continue
            x = pad + int((display - self.view_start_pct)
                          / self.view_span_pct * tw)
            grouped.setdefault(x, []).append((marker, available))
        self._highlight_hit_groups = tuple(
            (x, tuple(marker for marker, _ in values),
             any(available for _, available in values),
             tuple(marker for marker, available in values if available))
            for x, values in sorted(grouped.items()))
        self._highlight_geometry_key = key
        return self._highlight_hit_groups

    def release_thumbnail_resources(self):
        self.cancel_thumbnail_loading()
        self.clear_waveform()
        self._thumbnail_count = 0
        self._thumbnail_complete = False
        self._thumbnails.clear()
        self._scaled_thumbnails.clear()
        self._timeline_background = QPixmap()
        self._timeline_background_key = None
        if self._thumbnail_source is not None:
            with _TIMELINE_CACHE_LOCK:
                _TIMELINE_IMAGE_CACHE.pop(self._thumbnail_source, None)
            self._thumbnail_source = None

    def load_thumbnails(self, clip_path: str, count: int = 24):
        """Decode an evenly-spaced filmstrip off the UI thread.

        The viewer debounces this around seeks. A clip change or scrub
        cancels between decoder operations, and an LRU reuses the
        decoded images when a clip is reopened.
        """

        if not self.preview_strip_enabled:
            return
        count = max(1, min(120, int(count)))
        duration_ms = self.duration_ms
        cache_key = _timeline_cache_key(clip_path, count, duration_ms)
        if (cache_key is not None and cache_key == self._thumbnail_source
                and self._thumbnail_loading and not self._thumbnail_cancel.is_set()):
            return
        self.cancel_thumbnail_loading()
        job = self._thumbnail_job
        cancelled = threading.Event()
        self._thumbnail_cancel = cancelled
        if cache_key is None or cache_key != self._thumbnail_source:
            self._thumbnails.clear()
            self._scaled_thumbnails.clear()
            self.update()
        self._thumbnail_source = cache_key
        self._thumbnail_count = count
        self._thumbnail_complete = False
        cached = _cached_timeline_images(cache_key)
        if cached is not None:
            self._set_thumbnail_images((job, cached, True))
            return
        self._thumbnail_loading = True
        self._thumbnail_worker_starts += 1

        def _worker():
            images = list(_cached_timeline_images(cache_key, allow_partial=True) or ())
            last_update = 0.0
            complete = False

            def publish(status):
                if cancelled.is_set():
                    return
                try:
                    self.thumbnails_ready.emit((job, tuple(images), status))
                except RuntimeError:
                    # The editor was deleted before the queued update.
                    cancelled.set()

            try:
                from core.media_process import run_media_process
                duration_seconds = max(0.001, duration_ms / 1000.0)
                spacing = duration_seconds / count
                frame_bytes = 180 * 100 * 3
                pending_pixels = bytearray()

                def receive_pixels(chunk):
                    nonlocal last_update
                    pending_pixels.extend(chunk)
                    while (len(pending_pixels) >= frame_bytes and len(images) < count
                           and not cancelled.is_set()):
                        pixels = bytes(pending_pixels[:frame_bytes])
                        del pending_pixels[:frame_bytes]
                        images.append(QImage(
                            pixels, 180, 100, 180 * 3,
                            QImage.Format.Format_RGB888).copy())
                        _store_timeline_images(cache_key, images, complete=False, cancelled=cancelled)
                        now = time.monotonic()
                        if len(images) < count and now - last_update >= 0.1:
                            last_update = now
                            publish(None)

                ffmpeg = get_ffmpeg_exe()
                scale = ('scale=180:100:force_original_aspect_ratio=decrease,'
                         'pad=180:100:(ow-iw)/2:(oh-ih)/2')

                def decode(offset, frames, filters, timeout):
                    pending_pixels.clear()
                    seek_args = ['-ss', f'{offset:.9f}'] if offset else []
                    result = run_media_process(
                        [ffmpeg, '-v', 'error', '-threads', '4', '-filter_threads', '1',
                         *seek_args, '-i', clip_path, '-map', '0:V:0', '-an', '-sn',
                         '-vf', filters, '-frames:v', str(frames), '-pix_fmt', 'rgb24',
                         '-f', 'rawvideo', '-threads:v', '1', '-'],
                        cancelled, timeout_seconds=timeout, background='timeline',
                        on_stdout=receive_pixels)
                    return result is not None and result[0] == 0

                for point in _timeline_seek_points(clip_path, spacing, len(images), count,
                                                    cancelled):
                    loaded = len(images)
                    if not decode(point, 1, scale, 8.0) or len(images) == loaded:
                        break

                loaded = len(images)
                if loaded == count:
                    complete = True
                elif not cancelled.is_set():
                    # Keep one sequential decoder for densely spaced samples,
                    # unindexed files, or the remainder after a failed seek.
                    complete = decode(loaded * spacing, count - loaded,
                                      f'fps={count / duration_seconds:.9f},{scale}', 30.0)
            except (FFmpegUnavailable, OSError, RuntimeError, TypeError, ValueError) as error:
                print(f'[ClipTimeline] thumbnail filmstrip unavailable: {error}')
            complete = bool(images) and complete
            if complete:
                _store_timeline_images(cache_key, images, cancelled=cancelled)
            publish(complete)

        threading.Thread(target=_worker, daemon=True).start()

    def cancel_thumbnail_loading(self):
        """Cooperatively stop a filmstrip job without blocking the GUI thread."""

        self._thumbnail_cancel.set()
        self._thumbnail_job += 1
        self._thumbnail_loading = False

    @property
    def waveform_available(self):
        return self.audio_lane and 0 < self.duration_ms <= MAX_WAVEFORM_MS

    @property
    def waveform_pending(self):
        return any(self.waveform_pending_for(source_id)
                   for source_id in self.visible_audio_sources())

    def waveform_pending_for(self, source_id):
        complete = (source_id in self._source_waveform_peaks if source_id is not None
                    else self._waveform_complete)
        return (self.waveform_enabled and self.waveform_available
                and not self._waveform_loading and not complete)

    def set_waveform_enabled(self, enabled):
        self.waveform_enabled = bool(enabled)
        if not enabled:
            self.cancel_waveform_loading()
        self.waveform_visibility_changed.emit(self.waveform_enabled)
        self.update()

    def cancel_waveform_loading(self):
        if not self._waveform_loading:
            return
        self._waveform_cancel.set()
        self._waveform_job += 1
        self._waveform_loading = False

    def clear_waveform(self):
        self.cancel_waveform_loading()
        self._waveform_complete = False
        self._waveform_peaks = ()
        self._source_waveform_peaks.clear()
        self._timeline_background_key = None
        self.update()

    def load_waveform(self, clip_path, streams, *, cache_only=False, source_id=None):
        """Decode once per immutable source; the Qt thread only gets the peaks."""
        if not streams or not self.waveform_pending_for(source_id):
            return
        duration_ms = self.duration_ms
        cache_key = _timeline_cache_key(clip_path, streams, duration_ms)
        if cache_key is None:
            return
        with _TIMELINE_CACHE_LOCK:
            peaks = _WAVEFORM_CACHE.get(cache_key)
            if peaks is not None:
                _WAVEFORM_CACHE.move_to_end(cache_key)
        if peaks is not None:
            self._set_waveform_result((self._waveform_job, source_id, peaks))
            return
        if cache_only:
            return

        self._waveform_loading = True
        self._waveform_job += 1
        job = self._waveform_job
        cancelled = self._waveform_cancel = threading.Event()

        def worker():
            peaks = decode_waveform(clip_path, streams, duration_ms, cancelled)
            if cancelled.is_set():
                return
            if peaks:
                with _TIMELINE_CACHE_LOCK:
                    _WAVEFORM_CACHE[cache_key] = peaks
                    _WAVEFORM_CACHE.move_to_end(cache_key)
                    while len(_WAVEFORM_CACHE) > _TIMELINE_CACHE_LIMIT:
                        _WAVEFORM_CACHE.popitem(last=False)
            try:
                self.waveform_ready.emit((job, source_id, peaks))
            except RuntimeError:
                cancelled.set()

        threading.Thread(target=worker, name='FTHR-timeline-waveform', daemon=True).start()

    def _set_waveform_result(self, result):
        job, source_id, peaks = result
        if job != self._waveform_job:
            return
        self._waveform_loading = False
        if source_id is None:
            self._waveform_complete = True
            self._waveform_peaks = peaks
        else:
            self._source_waveform_peaks[source_id] = peaks
        self._timeline_background_key = None
        self.update()
        self.audio_waveform_finished.emit()

    def _set_thumbnail_images(self, payload):
        job, images, complete = payload
        if job != self._thumbnail_job or not self.preview_strip_enabled:
            return
        if complete is not None:
            self._thumbnail_loading = False
            self._thumbnail_complete = complete
        self._thumbnails = [QPixmap.fromImage(image) for image in images]
        self._scaled_thumbnails.clear()
        self.update()

    def _scaled_thumbnail(self, source: QPixmap, size: QSize) -> QPixmap:
        # cacheKey changes if a pixmap is modified. Dimensions cover resizing;
        # zoom/pan can reuse the same frame at the same display size.
        key = (source.cacheKey(), size.width(), size.height())
        cached = self._scaled_thumbnails.get(key)
        if cached is not None:
            self._scaled_thumbnails.move_to_end(key)
            return cached
        scaled = source.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        self._scaled_thumbnails[key] = scaled
        while len(self._scaled_thumbnails) > 64:
            self._scaled_thumbnails.popitem(last=False)
        return scaled

    def set_playhead(self, pct: float):
        old_rect = self._playhead_rect()
        self.playhead_pct = max(0.0, min(1.0, pct))
        new_rect = self._playhead_rect()
        if old_rect != new_rect:
            # A long seek should not dirty the entire strip between the markers.
            if not old_rect.isEmpty():
                self.update(old_rect)
            if not new_rect.isEmpty():
                self.update(new_rect)

    def _playhead_rect(self):
        pct = self._source_to_display(self.playhead_pct)
        if not self.view_start_pct <= pct <= self.view_start_pct + self.view_span_pct:
            return QRect()
        left, width = self._time_axis()
        x = left + int((pct - self.view_start_pct) / self.view_span_pct * width)
        return QRect(x - 7, 14, 15, self.height() - 31)

    def set_audio_sources(self, ids, muted=()):
        ids = tuple(ids)
        muted = frozenset(muted)
        if ids == self.audio_source_ids and muted == self.audio_muted_sources:
            return
        self.audio_source_ids = ids
        self.audio_muted_sources = muted
        if self.editing_audio and self.active_audio_source not in ids:
            self._select_video_lane()
        self._update_audio_scrollbar()
        self.update()

    def configure_lane_height(self, height, capacity):
        if self._lane_capacity != capacity:
            self._lane_capacity = capacity
        self.setFixedHeight(height)
        self._update_audio_scrollbar()

    @property
    def visible_audio_row_count(self):
        return min(self._lane_capacity, max(1, len(self.audio_source_ids)))

    def visible_audio_sources(self):
        if not self.source_lanes:
            return (None,)
        return self.audio_source_ids[self._lane_offset:self._lane_offset + self.visible_audio_row_count]

    def audio_controls_width(self):
        return max(150, min(260, self.width() // 4)) if self.source_lanes else 0

    def _time_axis(self):
        left = 14 + self.audio_controls_width()
        right_margin = 14 + (12 if self.source_lanes and self._audio_scrollbar.maximum() > 0 else 0)
        return left, max(1, self.width() - left - right_margin)

    def _update_audio_scrollbar(self):
        if not self.source_lanes:
            return
        scrollbar = self._audio_scrollbar
        scrollbar.setRange(0, max(0, len(self.audio_source_ids) - self.visible_audio_row_count))
        scrollbar.setPageStep(self.visible_audio_row_count)
        scrollbar.setGeometry(self.width() - 20, self._audio_lane_top(),
                              14, self.visible_audio_row_count * AUDIO_LANE_HEIGHT - 8)
        scrollbar.setVisible(scrollbar.maximum() > 0)
        self._timeline_background_key = None
        self._highlight_geometry_key = None
        self.audio_geometry_changed.emit()

    def _on_lane_scroll(self, offset):
        self._lane_offset = offset
        self._timeline_background_key = None
        self.audio_geometry_changed.emit()
        self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_audio_scrollbar()

    def _audio_source_at_y(self, y):
        row = (y - self._audio_lane_top()) // AUDIO_LANE_HEIGHT
        sources = self.visible_audio_sources()
        if not 0 <= row < len(sources):
            return False, None
        return True, sources[row]

    def set_audio_segments(self, segments):
        self.audio_segments = tuple(segments)
        self._audio_lane_key = None
        self.update()

    def set_source_audio_segments(self, edits):
        self._audio_source_segments = {key: tuple(lane) for key, lane in edits.items()}
        self._audio_lane_key = None
        self.update()

    def _segments_for_source(self, source_id):
        if source_id is None:
            return self.audio_segments
        return self._audio_source_segments.get(source_id) or (TimelineSegment(0.0, 1.0),)

    def audio_cut_ranges(self, source_id=None):
        """Source fractions where the whole audio mix is silent."""
        ranges = []
        for segment in self._segments_for_source(source_id):
            if not segment.deleted:
                continue
            if ranges and ranges[-1][1] == segment.start_pct:
                ranges[-1] = (ranges[-1][0], segment.end_pct)
            else:
                ranges.append((segment.start_pct, segment.end_pct))
        return tuple(ranges)

    def _audio_intervals(self, source_id=None):
        """Partition the lane at audio splits, video cuts and trim handles."""
        self._ensure_range_mapping()
        key = (self._range_mapping_key, self.start_pct, self.end_pct)
        if key != self._audio_lane_key:
            self._audio_lane_intervals.clear()
            self._audio_lane_key = key
        if source_id not in self._audio_lane_intervals:
            source_segments = self._segments_for_source(source_id)
            video_cuts = (segment.end_pct for segment in self.segments)
            audio_cuts = (segment.end_pct for segment in source_segments)
            legacy_cuts = (segment.end_pct for segment in self.audio_segments
                           if source_id is not None)
            boundaries = sorted({0.0, self.start_pct, self.end_pct,
                                 *video_cuts, *audio_cuts, *legacy_cuts})
            video = audio = legacy = 0
            intervals = []
            for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
                while self.segments[video].end_pct <= start:
                    video += 1
                while source_segments[audio].end_pct <= start:
                    audio += 1
                while self.audio_segments[legacy].end_pct <= start:
                    legacy += 1
                excluded = (self.segments[video].deleted or start < self.start_pct
                            or end > self.end_pct
                            or (source_id is not None and self.audio_segments[legacy].deleted))
                intervals.append((start, end, audio, source_segments[audio].deleted,
                                  excluded))
            self._audio_lane_intervals[source_id] = tuple(intervals)
        return self._audio_lane_intervals[source_id]

    def _audio_index_at(self, pct, source_id=None):
        intervals = self._audio_intervals(source_id)
        for index, (start, end, _, _, excluded) in enumerate(intervals):
            if start <= pct < end or end == pct == 1.0:
                if pct == start and excluded and index > 0 and not intervals[index - 1][4]:
                    return index - 1
                return index
        return -1

    def selected_audio_segment(self):
        index = self.selected_audio_index()
        return self._audio_intervals(self.active_audio_source)[index] if index >= 0 else None

    def selected_audio_index(self):
        return self._audio_index_at(self.selected_audio_position, self.active_audio_source)

    def select_audio_at(self, pct, source_id=None):
        self.selected_audio_position = pct
        self.active_audio_source = source_id
        self.editing_audio = True
        self.audio_selection_changed.emit()
        self.update()

    def _select_video_lane(self):
        if self.editing_audio:
            self.editing_audio = False
            self.audio_selection_changed.emit()
            self.update()

    def can_split_audio_at(self, pct, source_id=None):
        index = self._audio_index_at(pct, source_id)
        if index < 0 or len(self._segments_for_source(source_id)) >= 500:
            return False
        start, end, _, deleted, excluded = self._audio_intervals(source_id)[index]
        minimum = max(0.001, 250 / self.duration_ms)
        return not (deleted or excluded) and pct - start >= minimum and end - pct >= minimum

    def _replace_audio_segments(self, source_id, segments):
        if source_id is None:
            self.set_audio_segments(segments)
        else:
            if segments == (TimelineSegment(0.0, 1.0),):
                self._audio_source_segments.pop(source_id, None)
            else:
                self._audio_source_segments[source_id] = tuple(segments)
            self._audio_lane_key = None
            self.update()

    def split_audio_at(self, pct, source_id=None):
        if not self.can_split_audio_at(pct, source_id):
            return False
        segments = self._segments_for_source(source_id)
        _, _, index, _, _ = self._audio_intervals(source_id)[self._audio_index_at(pct, source_id)]
        segment = segments[index]
        self._replace_audio_segments(source_id, (
            *segments[:index], TimelineSegment(segment.start_pct, pct),
            TimelineSegment(pct, segment.end_pct), *segments[index + 1:]))
        self.select_audio_at(pct, source_id)
        self.audio_segments_changed.emit()
        return True

    def toggle_audio_segment(self, index, source_id=None):
        intervals = self._audio_intervals(source_id)
        if not 0 <= index < len(intervals):
            return False
        start, end, audio, deleted, excluded = intervals[index]
        if excluded:
            return False
        segments = self._segments_for_source(source_id)
        segment = segments[audio]
        replacement = [TimelineSegment(start, end, not deleted)]
        if segment.start_pct < start:
            replacement.insert(0, TimelineSegment(segment.start_pct, start, deleted))
        if end < segment.end_pct:
            replacement.append(TimelineSegment(end, segment.end_pct, deleted))
        if len(segments) + len(replacement) - 1 > 500:
            return False
        self._replace_audio_segments(source_id, (
            *segments[:audio], *replacement, *segments[audio + 1:]))
        self.select_audio_at((start + end) / 2, source_id)
        self.audio_segments_changed.emit()
        return True

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
            if self.hide_deleted_segments and (segment.deleted or pct == segment.end_pct):
                continue
            if segment.start_pct <= pct <= segment.end_pct:
                return index
        if self.hide_deleted_segments:
            for index in range(len(self.segments) - 1, -1, -1):
                segment = self.segments[index]
                if not segment.deleted and abs(pct - segment.end_pct) < 1e-9:
                    return index
        return -1

    def paintEvent(self, event):
        self._ensure_range_mapping()
        if self.audio_lane:
            for source_id in self.visible_audio_sources():
                self._audio_intervals(source_id)
        key = (
            self.width(), self.height(), self.devicePixelRatioF(), self.duration_ms,
            self._range_mapping_key, self.hide_deleted_segments, self.selected_segment,
            self.preview_strip_enabled, self._thumbnail_count,
            self.start_pct, self.end_pct, self.view_start_pct, self.zoom_factor,
            self._highlight_revision,
            self._audio_lane_key, self.editing_audio,
            self.selected_audio_position if self.editing_audio else None,
            self.audio_source_ids, self.audio_muted_sources, self.active_audio_source,
            self._lane_offset,
            self.waveform_enabled,
            tuple(frame.cacheKey() for frame in self._thumbnails), Fonts.BODY_FAMILY,
            tuple(getattr(Colors, name) for name in
                  ('TEXT_DIM', 'BORDER_HI', 'SURFACE_2', 'TEXT', 'BG', 'DELETE', 'ACCENT')),
        )
        self._painting_timeline = True
        try:
            if key != self._timeline_background_key:
                ratio = self.devicePixelRatioF()
                background = QPixmap(self.size() * ratio)
                background.setDevicePixelRatio(ratio)
                background.fill(Qt.GlobalColor.transparent)
                background_painter = QPainter(background)
                try:
                    self._paint_timeline_background(background_painter)
                    if self.audio_lane:
                        for row, source_id in enumerate(self.visible_audio_sources()):
                            self._paint_audio_lane(background_painter, source_id, row)
                        if self.source_lanes and not self.audio_source_ids:
                            self._paint_empty_audio_lane(background_painter)
                    self._paint_highlight_markers(background_painter)
                finally:
                    background_painter.end()
                self._timeline_background = background
                self._timeline_background_key = key
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._timeline_background)
            self._paint_playhead(painter)
        finally:
            self._painting_timeline = False

    def _paint_timeline_background(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad, tw = self._time_axis()
        ty, th = 22, self._video_strip_height()
        view_end = self.view_start_pct + self.view_span_pct

        # The collapsed timeline labels retained time; seeking stays in source time.
        font = QFont(Fonts.BODY_FAMILY, 7)
        painter.setFont(font)
        painter.setPen(QPen(QColor(Colors.TEXT_DIM)))
        for tick in range(5):
            pct = self.view_start_pct + self.view_span_pct * tick / 4
            x = pad + int(tw * tick / 4)
            align = Qt.AlignmentFlag.AlignLeft if tick == 0 else (
                Qt.AlignmentFlag.AlignRight if tick == 4 else Qt.AlignmentFlag.AlignHCenter)
            retained = self._range_offsets[-1] if self.hide_deleted_segments else 1.0
            painter.drawText(x - 34, 2, 68, 16, align, self._fmt(pct * self.duration_ms * retained))

        painter.setPen(QPen(QColor(Colors.BORDER_HI), 1))
        painter.setBrush(QBrush(QColor(Colors.SURFACE_2)))
        painter.drawRect(pad, ty, tw, th)

        # Filmstrip frames are sampled in source time, including while zoomed.
        slots = max(4, min(14, tw // 92)) if self.preview_strip_enabled else 0
        for slot in range(slots):
            left = pad + int(slot * tw / slots)
            right = pad + int((slot + 1) * tw / slots)
            slot_start = self.view_start_pct + self.view_span_pct * slot / slots
            pct = self._display_to_source(slot_start + self.view_span_pct / (2 * slots))
            if self._thumbnails:
                count = self._thumbnail_count or len(self._thumbnails)
                thumb_index = min(count - 1, int(pct * count))
                last_index = len(self._thumbnails) - 1
                # A partial strip keeps its source-time bins. The newest frame
                # can paint sooner when it already falls within this tile.
                if (thumb_index > last_index and not self.hide_deleted_segments
                        and (last_index + 0.5) / count >= slot_start):
                    thumb_index = last_index
            else:
                thumb_index = 0
            if thumb_index < len(self._thumbnails):
                source = self._thumbnails[thumb_index]
                target = QRect(left, ty, max(1, right - left), th)
                scaled = self._scaled_thumbnail(source, target.size())
                sx = max(0, (scaled.width() - target.width()) // 2)
                sy = max(0, (scaled.height() - target.height()) // 2)
                painter.drawPixmap(target, scaled, QRect(sx, sy, target.width(), target.height()))
            painter.setPen(QPen(_theme_color_with_alpha(Colors.TEXT, 24), 1))
            painter.drawLine(right, ty, right, ty + th)

        def x_for_pct(pct: float) -> int:
            pct = self._source_to_display(pct)
            return pad + int((pct - self.view_start_pct) / self.view_span_pct * tw)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(_theme_color_with_alpha(Colors.BG, 176)))
        if self._source_to_display(self.start_pct) > self.view_start_pct:
            painter.drawRect(pad, ty, max(0, min(tw, x_for_pct(self.start_pct) - pad)), th)
        if self._source_to_display(self.end_pct) < view_end:
            ex = max(pad, min(pad + tw, x_for_pct(self.end_pct)))
            painter.drawRect(ex, ty, pad + tw - ex, th)

        for index, segment in enumerate(self.segments):
            if self.hide_deleted_segments and segment.deleted:
                continue
            left_pct = max(self._source_to_display(segment.start_pct), self.view_start_pct)
            right_pct = min(self._source_to_display(segment.end_pct), view_end)
            if right_pct <= left_pct:
                continue
            left = max(pad, pad + int((left_pct - self.view_start_pct) / self.view_span_pct * tw))
            right = min(pad + tw, pad + int((right_pct - self.view_start_pct) / self.view_span_pct * tw))
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
            if index == self.selected_segment and not self.editing_audio:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                color = Colors.DELETE if segment.deleted else Colors.ACCENT
                painter.setPen(QPen(QColor(color), 2))
                painter.drawRect(left + 1, ty + 1, max(1, right - left - 2), th - 2)
            if self._source_to_display(segment.start_pct) > self.view_start_pct:
                cut_x = x_for_pct(segment.start_pct)
                painter.setPen(QPen(QColor(Colors.TEXT), 1))
                painter.drawLine(cut_x, ty + 8, cut_x, ty + th - 8)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(Colors.ACCENT)))
        for _handle, rect in self._trim_grip_rects():
            painter.drawRect(rect)

        overview_y, overview_h = self.height() - 13, 7
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(Colors.BORDER_HI)))
        painter.drawRect(pad, overview_y, tw, overview_h)
        painter.setBrush(QBrush(QColor(Colors.DELETE)))
        for segment in self.segments:
            if segment.deleted and not self.hide_deleted_segments:
                painter.drawRect(pad + int(segment.start_pct * tw), overview_y,
                                 max(1, int((segment.end_pct - segment.start_pct) * tw)),
                                 overview_h)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(Colors.ACCENT), 1))
        painter.drawRect(pad + int(self.view_start_pct * tw), overview_y,
                         max(3, int(self.view_span_pct * tw)), overview_h)

    def _video_strip_height(self):
        if self.audio_lane:
            return max(24, self.height() - 46 - self.visible_audio_row_count * AUDIO_LANE_HEIGHT)
        return max(70, self.height() - 46)

    def _trim_grip_rects(self):
        pad, width = self._time_axis()
        span = self.view_span_pct
        for handle, pct in (('start', self.start_pct), ('end', self.end_pct)):
            local_pct = (self._source_to_display(pct) - self.view_start_pct) / span
            if 0.0 <= local_pct <= 1.0:
                x = pad + int(local_pct * width)
                yield handle, QRect(x - 4, 19, 8, self._video_strip_height() + 6)

    def _trim_handle_at(self, position):
        if not 19 <= position.y() < 25 + self._video_strip_height():
            return None
        hits = [(abs(position.x() - (rect.x() + 4)), handle)
                for handle, rect in self._trim_grip_rects()
                if rect.adjusted(-5, 0, 5, 0).contains(position)]
        return min(hits, key=lambda hit: hit[0])[1] if hits else None

    def _update_trim_drag(self, x):
        if self._trim_drag_origin is None:
            return
        origin_x, source_pct, origin_pct, step, view_start, view_end = self._trim_drag_origin
        if x != origin_x:
            display_pct = max(view_start, min(view_end, origin_pct + (x - origin_x) * step))
            source_pct = self._display_to_source(display_pct)
        minimum = min(1.0, max(0.001, 250 / self.duration_ms))
        start, end = self.start_pct, self.end_pct
        if self.dragging == 'start':
            start = max(0.0, min(source_pct, end - minimum))
        else:
            end = min(1.0, max(source_pct, start + minimum))
        if (start, end) != (self.start_pct, self.end_pct):
            self.start_pct, self.end_pct = start, end
            self.range_changed.emit(start, end)
            self.update()

    def _audio_lane_top(self):
        return 22 + self._video_strip_height() + 8

    def _paint_empty_audio_lane(self, painter):
        pad, width = self._time_axis()
        rect = QRect(pad, self._audio_lane_top(), width, AUDIO_LANE_HEIGHT - 8)
        painter.fillRect(rect, QColor(Colors.SURFACE_2))
        painter.setPen(QColor(Colors.TEXT_DIM))
        painter.setFont(QFont(Fonts.BODY_FAMILY, 8))
        painter.drawText(rect.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, 'No audio tracks')

    def _paint_audio_lane(self, painter, source_id=None, row=0):
        pad, width = self._time_axis()
        top = self._audio_lane_top() + row * AUDIO_LANE_HEIGHT
        height = AUDIO_LANE_HEIGHT - 8
        painter.fillRect(QRect(pad, top, width, height), QColor(Colors.SURFACE_2))
        selected = (self.selected_audio_index() if self.editing_audio
                    and source_id == self.active_audio_source else -1)
        label_drawn = self.source_lanes
        peaks = ()
        if self.waveform_enabled:
            peaks = (self._source_waveform_peaks.get(source_id, ()) if source_id is not None
                     else self._waveform_peaks)
        muted = source_id in self.audio_muted_sources
        peak_max = max(peaks, default=0)
        painter.setFont(QFont(Fonts.BODY_FAMILY, 8))
        for index, (start, end, _, deleted, excluded) in enumerate(self._audio_intervals(source_id)):
            if self.hide_deleted_segments and self._source_to_display(start) == self._source_to_display(end):
                continue
            left = (self._source_to_display(start) - self.view_start_pct) / self.view_span_pct
            right = (self._source_to_display(end) - self.view_start_pct) / self.view_span_pct
            if right <= 0.0 or left >= 1.0:
                continue
            x = pad + int(max(0.0, left) * width)
            segment_width = pad + int(min(1.0, right) * width) - x
            if segment_width <= 1:
                continue
            rect = QRect(x, top, segment_width - 1, height)
            color = (Colors.TEXT_MUTED if excluded or muted else
                     Colors.DELETE if deleted else Colors.ACCENT)
            painter.fillRect(rect, _theme_color_with_alpha(
                color, 24 if excluded else 44 if deleted else 65))
            painter.setPen(QPen(QColor(color), 2 if index == selected else 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            if deleted or excluded:
                painter.save()
                painter.setClipRect(rect.adjusted(1, 1, -1, -1))
                painter.setPen(QPen(_theme_color_with_alpha(color, 95), 1))
                for offset in range(-height, segment_width, 9):
                    painter.drawLine(x + offset, top + height, x + offset + height, top)
                painter.restore()
            elif peak_max:
                lines = []
                center = top + height // 2
                count = len(peaks)
                for peak_index in range(int(start * count), min(count, int(end * count + 1))):
                    pct = (peak_index + .5) / count
                    if not start <= pct < end:
                        continue
                    position = (self._source_to_display(pct) - self.view_start_pct) / self.view_span_pct
                    peak_x = pad + round(position * width)
                    if x < peak_x < x + segment_width - 2:
                        amplitude = max(1, round((height // 2 - 4) * peaks[peak_index] / peak_max))
                        lines.append(QLine(peak_x, center - amplitude, peak_x, center + amplitude))
                if lines:
                    painter.setPen(QPen(_theme_color_with_alpha(color, 190), 1))
                    painter.drawLines(lines)
            if segment_width >= 66 and not excluded and (deleted or not label_drawn):
                if not deleted and peak_max:
                    painter.fillRect(rect.adjusted(3, 3, 45 - segment_width, -3),
                                     QColor(Colors.SURFACE_2))
                painter.setPen(QColor(Colors.TEXT))
                painter.drawText(rect.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter,
                                 'Silence' if deleted else 'Audio')
                label_drawn |= not deleted

    def _paint_playhead(self, painter):
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pad, tw = self._time_axis()
        ty = 22
        bottom = self.height() - 19 if self.audio_lane else ty + self._video_strip_height() + 5
        view_end = self.view_start_pct + self.view_span_pct

        def x_for_pct(pct):
            return pad + int((self._source_to_display(pct) - self.view_start_pct)
                             / self.view_span_pct * tw)

        if self.view_start_pct <= self._source_to_display(self.playhead_pct) <= view_end:
            ph_x = x_for_pct(self.playhead_pct)
            painter.setPen(QPen(QColor(Colors.TEXT), 2))
            painter.drawLine(ph_x, ty - 7, ph_x, bottom)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(Colors.TEXT)))
            painter.drawPolygon(QPolygon([
                QPoint(ph_x - 5, ty - 7), QPoint(ph_x + 5, ty - 7),
                QPoint(ph_x, ty - 1),
            ]))

    def _paint_highlight_markers(self, painter):
        """Paint one cached geometry item per occupied pixel column."""

        if not self._highlight_markers:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ty, th = 22, self._video_strip_height()
        for x, markers, available, _available_markers in self._highlight_geometry():
            color = Colors.ACCENT if available else Colors.TEXT_MUTED
            painter.setPen(QPen(QColor(color), 1 if available else 1))
            painter.drawLine(x, ty - 7, x, ty + th + 5)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(color)))
            painter.drawPolygon(QPolygon([
                QPoint(x - 4, ty - 7), QPoint(x + 4, ty - 7),
                QPoint(x, ty - 1),
            ]))
            if len(markers) > 1:
                painter.setPen(QPen(QColor(color), 1))
                painter.setFont(QFont(Fonts.BODY_FAMILY, 7))
                painter.drawText(x + 4, ty + 11, f'+{len(markers) - 1}')

    def _pct_from_x(self, x: int) -> float:
        pad, tw = self._time_axis()
        local = max(0.0, min(1.0, (x - pad) / tw))
        return self._display_to_source(self.view_start_pct + local * self.view_span_pct)

    def _overview_pct_from_x(self, x: int) -> float:
        pad, tw = self._time_axis()
        return self._display_to_source(max(0.0, min(1.0, (x - pad) / tw)))

    def _highlight_at(self, x: int, y: int):
        if y < 12 or y > self.height() - 13:
            return None
        nearest = None
        distance = 9
        for marker_x, _markers, group_available, available_markers in self._highlight_geometry():
            if abs(marker_x - x) <= distance:
                if not group_available:
                    continue
                nearest = available_markers[0]
                distance = abs(marker_x - x)
        return nearest

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        event_pos = event.position().toPoint()
        handle = self._trim_handle_at(event_pos)
        if handle is not None:
            self._select_video_lane()
            self.dragging = handle
            start = self.view_start_pct
            span = self.view_span_pct
            pct = self.start_pct if handle == 'start' else self.end_pct
            self._trim_drag_origin = (
                event_pos.x(), pct, self._source_to_display(pct),
                span / self._time_axis()[1], start, start + span)
            self.setCursor(Qt.CursorShape.SizeHorCursor)
            self.range_edit_started.emit()
            return
        if self.source_lanes and event_pos.x() < self._time_axis()[0]:
            return
        if self.audio_lane and self._audio_lane_top() <= event_pos.y() < self.height() - 18:
            hit, source_id = self._audio_source_at_y(event_pos.y())
            if not hit:
                return
            pct = self._pct_from_x(event_pos.x())
            self.dragging = 'playhead'
            self.set_playhead(pct)
            self.select_audio_at(pct, source_id)
            self.seek_requested.emit(pct)
            return
        self._select_video_lane()
        if event_pos.y() >= self.height() - 18:
            pct = self._overview_pct_from_x(event_pos.x())
            span = self.view_span_pct
            self.view_start_pct = max(0.0, min(1.0 - span, self._source_to_display(pct) - span / 2))
            self.playhead_pct = pct
            self.selected_segment = max(0, self._segment_index_at(pct))
            self.selection_changed.emit(
                self.selected_segment, self.segments[self.selected_segment].deleted)
            self.seek_requested.emit(pct)
            self.update()
            return
        pct = self._pct_from_x(event_pos.x())
        marker = self._highlight_at(event_pos.x(), event_pos.y())
        if marker is not None:
            if self.highlight_available(marker):
                marker_pct = self.highlight_position_pct(marker)
                self.playhead_pct = marker_pct
                self.highlight_activated.emit(marker.as_dict())
            self.update()
            return
        # A click jumps immediately and can continue as a playhead scrub.
        self.dragging = 'playhead'
        self.playhead_pct = pct
        index = self._segment_index_at(pct)
        if index >= 0:
            self.selected_segment = index
            self.selection_changed.emit(index, self.segments[index].deleted)
        self.seek_requested.emit(pct)
        self.update()

    def mouseMoveEvent(self, event):
        position = event.position().toPoint()
        if not self.dragging:
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._trim_handle_at(position)
                           else Qt.CursorShape.PointingHandCursor)
            return
        if self.dragging == 'playhead':
            pct = self._pct_from_x(position.x())
            self.playhead_pct = pct
            self.seek_requested.emit(pct)
            self.update()
        else:
            self._update_trim_drag(position.x())

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        was_playhead = self.dragging == 'playhead'
        was_range_edit = self.dragging in ('start', 'end')
        position = event.position().toPoint()
        if was_playhead:
            pct = self._pct_from_x(position.x())
            if pct != self.playhead_pct:
                self.seek_requested.emit(pct)
        elif was_range_edit:
            self._update_trim_drag(position.x())
            self._trim_drag_origin = None
            self.setCursor(Qt.CursorShape.SizeHorCursor if self._trim_handle_at(position)
                           else Qt.CursorShape.PointingHandCursor)
        self.dragging = None
        if was_playhead:
            self.playhead_scrub_finished.emit()
        elif was_range_edit:
            self.range_edit_finished.emit()

    def wheelEvent(self, event):
        if (self.source_lanes and self._audio_scrollbar.maximum() > 0
                and self._audio_lane_top() <= event.position().y() < self.height() - 18):
            step = event.angleDelta().y() or event.pixelDelta().y()
            if step:
                self._audio_scrollbar.setValue(self._lane_offset - (1 if step > 0 else -1))
                event.accept()
                return
        event.ignore()

    def _context_menu_for_pct(self, pct: float, *, audio=False, source_id=None) -> QMenu:
        """Build a segment menu targeted at source time, not stale selection."""

        pct = max(0.0, min(1.0, float(pct)))
        if audio:
            index = self._audio_index_at(pct, source_id)
            self.select_audio_at(pct, source_id)
        else:
            self._select_video_lane()
            index = self._segment_index_at(pct)
            if index < 0:
                index = max(0, min(self.selected_segment, len(self.segments) - 1))
                pct = self.segments[index].start_pct
            self.selected_segment = index
            self.selection_changed.emit(index, self.segments[index].deleted)
        self.set_playhead(pct)
        self.seek_requested.emit(pct)
        self.update()

        menu = QMenu(self)
        menu.setObjectName('timelineContextMenu')
        set_theme_style(menu, lambda: (f'''
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
        '''))

        split_action = menu.addAction('Split here')
        if audio:
            _, _, _, deleted, excluded = self._audio_intervals(source_id)[index]
            can_split = self.can_split_audio_at(pct, source_id)
            can_toggle = not excluded
            split_requested = self.audio_context_split_requested
            toggle_requested = self.audio_context_toggle_requested
        else:
            segment = self.segments[index]
            deleted = segment.deleted
            minimum = max(0.001, 250 / self.duration_ms)
            can_split = (not deleted and pct - segment.start_pct >= minimum
                         and segment.end_pct - pct >= minimum)
            active = sum(not item.deleted for item in self.segments)
            can_toggle = deleted or (len(self.segments) > 1 and active > 1)
            split_requested = self.context_split_requested
            toggle_requested = self.context_toggle_requested
        split_action.setEnabled(can_split)
        split_action.triggered.connect(
            lambda _checked=False, at=pct: split_requested.emit(at))

        toggle_text = 'Restore segment' if deleted else 'Delete segment'
        toggle_action = menu.addAction(toggle_text)
        toggle_action.setEnabled(can_toggle)
        toggle_action.triggered.connect(
            lambda _checked=False, at=index: toggle_requested.emit(at))
        if audio and source_id is not None:
            inherited_index = self._audio_index_at(pct)
            inherited = self._audio_intervals()[inherited_index]
            if inherited[3] and not inherited[4]:
                restore = menu.addAction('Restore audio across all tracks')
                restore.triggered.connect(
                    lambda _checked=False, at=inherited_index:
                    self.restore_inherited_audio_requested.emit(at))
        menu.addSeparator()
        hide_action = menu.addAction('Hide deleted segments')
        hide_action.setCheckable(True)
        hide_action.setChecked(self.hide_deleted_segments)
        hide_action.toggled.connect(self.set_hide_deleted_segments)
        if audio:
            waveform_action = menu.addAction('Show waveform')
            waveform_action.setCheckable(True)
            waveform_action.setChecked(self.waveform_enabled and self.waveform_available)
            waveform_action.setEnabled(self.waveform_available)
            waveform_action.toggled.connect(self.set_waveform_enabled)
        return menu

    def contextMenuEvent(self, event):
        pos = event.pos()
        overview = pos.y() >= self.height() - 18
        pct = (self._overview_pct_from_x(pos.x())
               if overview else self._pct_from_x(pos.x()))
        audio = self.audio_lane and not overview and pos.y() >= self._audio_lane_top()
        hit, source_id = self._audio_source_at_y(pos.y()) if audio else (True, None)
        if not hit:
            return
        self._context_menu = self._context_menu_for_pct(pct, audio=audio, source_id=source_id)
        self._context_menu.popup(event.globalPos())
        event.accept()

    def _fmt(self, ms: float) -> str:
        s = int(ms / 1000)
        return f'{s // 60}:{s % 60:02d}'
