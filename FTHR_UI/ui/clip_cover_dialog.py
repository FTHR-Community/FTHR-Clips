"""Asynchronous library-cover picker used by the clip grid."""

from __future__ import annotations

import math
import json
import threading
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import (
    QBuffer, QIODevice, QObject, QRunnable, Qt, QTimer, Signal,
    QMetaObject,
)
from PySide6.QtGui import QImage, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDoubleSpinBox, QFileDialog, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe
from core.library_covers import (
    MAX_COVER_INPUT_BYTES, MAX_COVER_PIXELS, MAX_NORMALIZED_COVER_HEIGHT,
    MAX_NORMALIZED_COVER_WIDTH, MAX_COVER_PNG_BYTES,
    CoverCommittedError, inspect_png,
)
from core.media_process import run_media_process
from ui.dialogs import FthrDialog, FthrMessageDialog
from ui.sliders import ClickToSeekSlider
from ui.style import (
    Colors, Fonts, button_outline_qss, button_primary_qss,
    button_secondary_qss, label_body, set_theme_style,
)


_cover_pool: object | None = None
_cover_runtime_app = None
_cover_owner_lock = threading.RLock()
_cover_owners = {}
_cover_owner_sequence = 0


class _CoverResultChannel(QObject):
    result = Signal(int, str, object, object)
    terminal = Signal(int)


class _CoverJobOwner:
    """Pure-Python lifetime owner independent of the short-lived dialog."""

    def __init__(self, channel):
        global _cover_owner_sequence
        self.channel = channel
        self._lock = threading.RLock()
        self._jobs = {}
        self._closed = False
        with _cover_owner_lock:
            _cover_owner_sequence += 1
            self._registry_key = _cover_owner_sequence
            _cover_owners[self._registry_key] = self

    def register(self, job_id, cancel_event):
        with self._lock:
            if self._closed:
                cancel_event.set()
                return False
            self._jobs[int(job_id)] = cancel_event
            return True

    def terminal(self, job_id):
        with self._lock:
            self._jobs.pop(int(job_id), None)
            retire = self._closed and not self._jobs
        if retire:
            self._retire()

    def close(self):
        with self._lock:
            self._closed = True
            for cancel_event in tuple(self._jobs.values()):
                cancel_event.set()
            retire = not self._jobs
        if retire:
            self._retire()

    def _retire(self):
        with self._lock:
            channel = self.channel
            self.channel = None
        if channel is not None:
            try:
                QMetaObject.invokeMethod(
                    channel, 'deleteLater', Qt.ConnectionType.QueuedConnection)
            except RuntimeError:
                # Qt may already have destroyed the app-owned channel at exit.
                pass
        with _cover_owner_lock:
            _cover_owners.pop(self._registry_key, None)


def _cancel_cover_owners():
    with _cover_owner_lock:
        owners = tuple(_cover_owners.values())
    for owner in owners:
        owner.close()


def _cover_runtime():
    """Create the app-owned result channel and bounded pool on the GUI thread."""
    global _cover_pool, _cover_runtime_app
    app = QApplication.instance()
    if app is None:
        raise RuntimeError('library cover UI requires a QApplication')
    if _cover_pool is None:
        from PySide6.QtCore import QThreadPool
        _cover_pool = QThreadPool(app)
        _cover_pool.setMaxThreadCount(2)
    if _cover_runtime_app is not app:
        app.aboutToQuit.connect(_cancel_cover_owners)
        _cover_runtime_app = app
    return _cover_pool


def _image_to_png(image: QImage) -> tuple[bytes, QImage]:
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        raise ValueError('image decoder returned an empty image')
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    image = image.scaled(
        MAX_NORMALIZED_COVER_WIDTH, MAX_NORMALIZED_COVER_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation)
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        raise OSError('could not create an in-memory cover image')
    try:
        if not image.save(buffer, 'PNG'):
            raise OSError('could not encode the selected image as PNG')
        payload = bytes(buffer.data())
    finally:
        buffer.close()
    if len(payload) > MAX_COVER_PNG_BYTES:
        raise ValueError('normalized cover exceeds the 8 MiB PNG limit')
    inspect_png(payload, require_normalized=True)
    return payload, image


class _CoverWorker(QRunnable):
    def __init__(self, token, kind, channel, cancel_event, job_id=None,
                 owner=None):
        super().__init__()
        self.token = token
        self.kind = kind
        self.channel = channel
        self.cancel_event = cancel_event
        self.job_id = int(token if job_id is None else job_id)
        self.owner = owner
        self._result_emitted = False
        self._terminal_emitted = False

    def _emit(self, payload=None, error=None):
        self._result_emitted = True
        self.channel.result.emit(self.token, self.kind, payload, error)

    def run(self):
        try:
            self._run_body()
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError,
                AttributeError, KeyError, IndexError, UnicodeError) as error:
            # Worker boundaries convert decoder/process failures into one
            # queued result so the owner can always release its active slot.
            if not self._result_emitted:
                self._emit(error=str(error))
        finally:
            if not self._result_emitted:
                self._emit(error='library cover worker failed')
            if not self._terminal_emitted:
                self._terminal_emitted = True
                try:
                    self.channel.terminal.emit(self.job_id)
                finally:
                    if self.owner is not None:
                        self.owner.terminal(self.job_id)


class _FrameWorker(_CoverWorker):
    def __init__(self, token, source_path, seconds, channel, cancel_event, job_id=None,
                 owner=None):
        super().__init__(token, 'frame', channel, cancel_event, job_id, owner)
        self.source_path = source_path
        self.seconds = seconds

    def _run_body(self):
        if self.cancel_event.is_set():
            self._emit(error='cancelled')
            return
        ffmpeg = get_ffmpeg_exe()
        result = run_media_process(
            [ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss',
             f'{self.seconds:.3f}', '-i', self.source_path,
             '-frames:v', '1', '-vf',
             "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease",
             '-f', 'image2pipe', '-vcodec', 'png', '-'],
            self.cancel_event, timeout_seconds=8)
        if result is None or result[0] != 0 or not result[1]:
            raise OSError('video frame decoding failed')
        inspect_png(result[1], require_normalized=True)
        image = QImage.fromData(result[1], 'PNG')
        if image.isNull():
            raise ValueError('video frame image could not be decoded')
        self._emit({'png': result[1], 'image': image})


class _ImageWorker(_CoverWorker):
    def __init__(self, token, source_path, channel, cancel_event, job_id=None,
                 owner=None):
        super().__init__(token, 'image', channel, cancel_event, job_id, owner)
        self.source_path = source_path

    def _run_body(self):
        if self.cancel_event.is_set():
            self._emit(error='cancelled')
            return
        path = Path(self.source_path)
        before = path.stat()
        size = before.st_size
        if size <= 0 or size > MAX_COVER_INPUT_BYTES:
            raise ValueError('selected image exceeds the 32 MiB input limit')
        with path.open('rb') as source:
            source_bytes = source.read(MAX_COVER_INPUT_BYTES + 1)
        if len(source_bytes) > MAX_COVER_INPUT_BYTES:
            raise ValueError('selected image exceeds the 32 MiB input limit')
        after_read = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after_read.st_dev, after_read.st_ino,
                    after_read.st_size, after_read.st_mtime_ns)):
            raise OSError('selected image changed while it was read')
        reader = QImageReader()
        reader.setAutoTransform(True)
        reader.setDecideFormatFromContent(True)
        buffer = QBuffer()
        buffer.setData(source_bytes)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise OSError('selected image could not be opened')
        reader.setDevice(buffer)
        dimensions = reader.size()
        if (not dimensions.isValid()
                or dimensions.width() <= 0 or dimensions.height() <= 0
                or dimensions.width() * dimensions.height() > MAX_COVER_PIXELS):
            raise ValueError('selected image dimensions are unsupported')
        format_name = bytes(reader.format()).decode(
            'ascii', errors='ignore').casefold()
        suffix = path.suffix.casefold()
        if suffix in {'.gif', '.svg', '.apng', '.tif', '.tiff'}:
            raise ValueError('animated or vector images are not supported')
        image = reader.read()
        image_count = reader.imageCount()
        buffer.close()
        if self.cancel_event.is_set():
            self._emit(error='cancelled')
            return
        if format_name not in {'png', 'jpg', 'jpeg', 'webp'}:
            raise ValueError('selected image format is not supported')
        if format_name == 'png':
            # Inspect the bounded source bytes first so a renamed APNG is
            # never silently reduced to its first frame by QImageReader.
            inspect_png(source_bytes)
        if image_count > 1:
            raise ValueError('animated images are not supported')
        after_decode = path.stat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after_decode.st_dev, after_decode.st_ino,
                    after_decode.st_size, after_decode.st_mtime_ns)):
            raise OSError('selected image changed while it was decoded')
        png, decoded = _image_to_png(image)
        self._emit({'png': png, 'image': decoded})


class _ProbeWorker(_CoverWorker):
    def __init__(self, token, source_path, channel, cancel_event, job_id=None,
                 owner=None):
        super().__init__(token, 'probe', channel, cancel_event, job_id, owner)
        self.source_path = source_path

    def _run_body(self):
        if self.cancel_event.is_set():
            self._emit(error='cancelled')
            return
        ffprobe = get_ffprobe_exe()
        result = run_media_process(
            [ffprobe, '-v', 'error', '-show_entries',
             'format=duration:stream=duration,avg_frame_rate,r_frame_rate,nb_frames',
             '-select_streams', 'v:0', '-of', 'json', self.source_path],
            self.cancel_event, timeout_seconds=8)
        if result is None or result[0] != 0:
            raise OSError('video duration probe failed')
        try:
            probe = json.loads(result[1].decode('utf-8', errors='strict'))
            format_data = probe.get('format', {})
            stream_data = (probe.get('streams') or [{}])[0]
            duration = float(stream_data.get('duration')
                             or format_data.get('duration'))
        except (AttributeError, IndexError, KeyError, TypeError, ValueError,
                UnicodeError, json.JSONDecodeError):
            raise ValueError(
                'video duration probe returned invalid data') from None
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError('video duration is unavailable')
        interval = 0.0
        for value in (stream_data.get('avg_frame_rate'),
                      stream_data.get('r_frame_rate')):
            if not isinstance(value, str) or value in {'0/0', '0'}:
                continue
            try:
                rate = float(Fraction(value))
            except (ValueError, ZeroDivisionError, TypeError):
                # A malformed optional rate falls back to the next rate/count.
                continue
            if math.isfinite(rate) and rate > 0:
                interval = 1.0 / rate
                break
        if interval <= 0:
            try:
                frame_count = int(stream_data.get('nb_frames', 0) or 0)
            except (TypeError, ValueError, OverflowError):
                frame_count = 0
            if frame_count > 0:
                interval = duration / frame_count
        # A missing rate is unusual for the supported CFR sources.  Keep a
        # bounded millisecond guard rather than exposing the container EOF.
        interval = min(max(interval, 0.001), duration)
        last_frame = max(0.0, math.floor(
            max(0.0, duration - interval) * 1000.0) / 1000.0)
        self._emit({'duration': duration, 'last_frame': last_frame})


class _SaveWorker(_CoverWorker):
    def __init__(self, token, manager, source_path, png_bytes, expected_identity,
                 expected_clip_id, expected_revision, channel, cancel_event,
                 job_id=None, owner=None):
        super().__init__(token, 'save', channel, cancel_event, job_id, owner)
        self.manager = manager
        self.source_path = source_path
        self.png_bytes = png_bytes
        self.expected_identity = dict(expected_identity)
        self.expected_clip_id = expected_clip_id
        self.expected_revision = expected_revision

    def _run_body(self):
        if self.cancel_event.is_set():
            self._emit(error='cancelled')
            return
        try:
            if self.png_bytes is None:
                self.manager.clear_library_cover(
                    self.source_path,
                    expected_identity=self.expected_identity,
                    expected_clip_id=self.expected_clip_id,
                    expected_cover_revision=self.expected_revision)
                self._emit({'reference': None, 'png': None})
            else:
                reference = self.manager.set_library_cover(
                    self.source_path, self.png_bytes,
                    expected_identity=self.expected_identity,
                    expected_clip_id=self.expected_clip_id,
                    expected_cover_revision=self.expected_revision)
                self._emit({'reference': reference, 'png': self.png_bytes})
        except CoverCommittedError as error:
            self._emit({
                'reference': error.committed_reference,
                'png': self.png_bytes,
                'committed': True,
                'reload_needed': True,
                'cleared': error.cleared,
                'message': str(error),
            })


class ClipCoverDialog(FthrDialog):
    """Pick a video frame or image and persist it as a library cover."""

    cover_saved = Signal(object)

    def __init__(self, manager, source_path, *, expected_identity,
                 expected_clip_id=None, existing_reference=None,
                 duration_seconds=0.0, is_video=True,
                 initial_pixmap=None, save_guard=None, parent=None):
        super().__init__('Clip cover', parent, width=470)
        self._manager = manager
        self._source_path = str(source_path)
        self._expected_identity = dict(expected_identity)
        self._expected_clip_id = expected_clip_id
        self._existing_reference = existing_reference
        try:
            hint_duration = float(duration_seconds or 0.0)
        except (TypeError, ValueError, OverflowError):
            hint_duration = 0.0
        self._duration_seconds = (
            hint_duration if math.isfinite(hint_duration) and hint_duration > 0
            else 0.0)
        self._is_video = bool(is_video)
        self._save_guard = save_guard
        self._candidate_png = None
        self._candidate_pixmap = QPixmap()
        self._reset_requested = False
        self._active_cancel = None
        self._active_kind = None
        self._active_job_id = None
        self._active_token = 0
        self._pending_request = None
        self._saving = False
        self._closed = False
        self._probe_cancel = None
        self._probe_token = 0
        self._probe_job_id = None
        self._probe_active = False
        self._next_job_id = 0
        self._pool = _cover_runtime()
        # The sender outlives a rejected dialog; Qt removes the receiver
        # connection when this dialog is destroyed, so late worker results
        # cannot call into a dead QObject wrapper.
        self._channel = _CoverResultChannel(QApplication.instance())
        self._owner = _CoverJobOwner(self._channel)
        self._channel.result.connect(
            self._on_worker_result, Qt.ConnectionType.QueuedConnection)
        self._channel.terminal.connect(
            self._on_job_terminal, Qt.ConnectionType.QueuedConnection)
        # Keep destruction cleanup independent of the dialog receiver itself;
        # the pure-Python owner remains alive until every worker is terminal.
        self.destroyed.connect(self._owner.close)

        self.preview = QLabel()
        self.preview.setMinimumSize(400, 225)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet(
            f'background: {Colors.SURFACE_1}; color: {Colors.TEXT_DIM};')
        self.preview.setText('No cover selected')
        self.body_layout.addWidget(self.preview)

        self.status = QLabel()
        set_theme_style(self.status, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.status.setWordWrap(True)
        self.body_layout.addWidget(self.status)

        self._frame_controls = QWidget(self)
        frame_layout = QVBoxLayout(self._frame_controls)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self.frame_slider = ClickToSeekSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(0, 0)
        self.frame_slider.valueChanged.connect(self._frame_slider_changed)
        frame_layout.addWidget(self.frame_slider)
        self.seconds_spin = QDoubleSpinBox()
        self.seconds_spin.setDecimals(3)
        self.seconds_spin.setRange(0.0, 0.0)
        self.seconds_spin.setSingleStep(0.1)
        self.seconds_spin.valueChanged.connect(self._seconds_changed)
        frame_layout.addWidget(self.seconds_spin)
        self.body_layout.addWidget(self._frame_controls)
        if not self._is_video:
            self._frame_controls.hide()
        else:
            self.frame_slider.setEnabled(False)
            self.seconds_spin.setEnabled(False)
            self.status.setText('Checking video duration; you can still choose an image.')

        choose = QPushButton('Choose image...')
        set_theme_style(choose, lambda: button_secondary_qss())
        choose.clicked.connect(self._choose_image)
        self.body_layout.addWidget(choose)
        self._choose_button = choose

        reset = QPushButton('Reset to generated thumbnail')
        set_theme_style(reset, lambda: button_outline_qss())
        reset.clicked.connect(self._reset_candidate)
        self.body_layout.addWidget(reset)
        self._reset_button = reset
        # Without a persisted cover there is nothing to restore.  The button
        # becomes useful after a preview candidate is ready, so a user can
        # discard that candidate before applying it.
        reset.setEnabled(existing_reference is not None)

        self.action_layout.addStretch(1)
        cancel = QPushButton('Cancel')
        set_theme_style(cancel, lambda: button_outline_qss())
        cancel.clicked.connect(self.reject)
        self.action_layout.addWidget(cancel)
        apply_button = QPushButton('Apply')
        set_theme_style(apply_button, lambda: button_primary_qss())
        apply_button.clicked.connect(self._apply)
        self.action_layout.addWidget(apply_button)
        self._apply_button = apply_button

        if isinstance(initial_pixmap, QPixmap) and not initial_pixmap.isNull():
            self._set_preview(initial_pixmap)
        self._frame_debounce = QTimer(self)
        self._frame_debounce.setSingleShot(True)
        self._frame_debounce.setInterval(150)
        self._frame_debounce.timeout.connect(self._launch_pending_frame)
        if self._is_video:
            self._start_duration_probe()
        self._apply_button.setEnabled(False)

    def _new_job_id(self):
        self._next_job_id += 1
        return self._next_job_id

    def _start_cover_worker(self, worker):
        job_id = worker.job_id
        worker.owner = self._owner
        if not self._owner.register(job_id, worker.cancel_event):
            return
        self._pool.start(worker)

    def _start_duration_probe(self):
        if self._probe_cancel is not None:
            self._probe_cancel.set()
        self._probe_token += 1
        cancel = threading.Event()
        job_id = self._new_job_id()
        self._probe_cancel = cancel
        self._probe_job_id = job_id
        self._probe_active = True
        self._start_cover_worker(_ProbeWorker(
            self._probe_token, self._source_path, self._channel, cancel, job_id))

    def _last_frame_seconds(self, duration):
        if not math.isfinite(duration) or duration <= 0:
            return 0.0
        # FFmpeg receives millisecond precision; stay strictly before EOF
        # even when the source duration is an exact millisecond boundary.
        return max(0.0, (math.ceil(duration * 1000.0) - 1) / 1000.0)

    def _set_verified_duration(self, duration, last_frame=None):
        duration = float(duration)
        try:
            last = float(last_frame) if last_frame is not None else (
                self._last_frame_seconds(duration))
        except (TypeError, ValueError, OverflowError):
            last = self._last_frame_seconds(duration)
        if not math.isfinite(last) or last < 0 or last >= duration:
            last = self._last_frame_seconds(duration)
        maximum_ms = min(2_147_483_647, max(0, math.floor(last * 1000.0)))
        self._duration_seconds = duration
        self.frame_slider.blockSignals(True)
        self.seconds_spin.blockSignals(True)
        self.frame_slider.setRange(0, maximum_ms)
        self.seconds_spin.setRange(0.0, last)
        self.frame_slider.setValue(min(self.frame_slider.value(), maximum_ms))
        self.seconds_spin.setValue(min(self.seconds_spin.value(), last))
        self.frame_slider.blockSignals(False)
        self.seconds_spin.blockSignals(False)
        self.frame_slider.setEnabled(True)
        self.seconds_spin.setEnabled(True)

    def _cancel_active_preview(self):
        if self._active_cancel is not None:
            self._active_cancel.set()

    def _cancel_owned_jobs(self):
        self._frame_debounce.stop()
        self._pending_request = None
        self._cancel_active_preview()
        if self._probe_cancel is not None:
            self._probe_cancel.set()
        self._owner.close()

    def _retire_channel_if_idle(self):
        if self._closed:
            self._owner.close()

    def _on_job_terminal(self, job_id):
        if job_id == self._probe_job_id:
            self._probe_cancel = None
            self._probe_job_id = None
            self._probe_active = False
        if job_id == self._active_job_id:
            self._active_job_id = None
            self._active_cancel = None
        self._retire_channel_if_idle()

    def _set_preview(self, pixmap):
        if pixmap.isNull():
            self.preview.setPixmap(QPixmap())
            self.preview.setText('No cover selected')
            return
        self.preview.setText('')
        target = self.preview.size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = pixmap.scaled(
            target, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation)
        left = max(0, (scaled.width() - target.width()) // 2)
        top = max(0, (scaled.height() - target.height()) // 2)
        scaled = scaled.copy(left, top, target.width(), target.height())
        self.preview.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self._candidate_pixmap.isNull():
            self._set_preview(self._candidate_pixmap)

    def _frame_slider_changed(self, value):
        if self.seconds_spin.value() != value / 1000.0:
            self.seconds_spin.blockSignals(True)
            self.seconds_spin.setValue(value / 1000.0)
            self.seconds_spin.blockSignals(False)
        self._schedule_frame(value / 1000.0)

    def _seconds_changed(self, value):
        seconds = min(max(float(value), 0.0), self._last_frame_seconds(
            self._duration_seconds))
        slider_value = min(2_147_483_647, max(0, round(seconds * 1000)))
        if self.frame_slider.value() != slider_value:
            self.frame_slider.blockSignals(True)
            self.frame_slider.setValue(slider_value)
            self.frame_slider.blockSignals(False)
        self._schedule_frame(seconds)

    def _schedule_frame(self, seconds):
        if self._saving or not self._is_video or self._duration_seconds <= 0:
            return
        seconds = min(max(float(seconds), 0.0),
                      self._last_frame_seconds(self._duration_seconds))
        self._active_token += 1
        self._pending_request = ('frame', self._active_token, float(seconds))
        if self._active_cancel is not None:
            self._active_cancel.set()
        self.status.setText('Loading frame...')
        self._frame_debounce.start()

    def _launch_pending_frame(self):
        if self._saving or self._active_kind is not None:
            return
        request = self._pending_request
        if request is None or request[0] != 'frame':
            return
        self._pending_request = None
        _, token, seconds = request
        cancel = threading.Event()
        self._active_cancel = cancel
        self._active_kind = 'frame'
        self._active_job_id = self._new_job_id()
        self._start_cover_worker(_FrameWorker(
            token, self._source_path, seconds, self._channel, cancel,
            self._active_job_id))

    def _launch_pending_image(self):
        if self._saving or self._active_kind is not None:
            return
        request = self._pending_request
        if request is None or request[0] != 'image':
            return
        self._pending_request = None
        _, token, path = request
        cancel = threading.Event()
        self._active_cancel = cancel
        self._active_kind = 'image'
        self._active_job_id = self._new_job_id()
        self.status.setText('Loading image...')
        self._start_cover_worker(_ImageWorker(
            token, path, self._channel, cancel, self._active_job_id))

    def _launch_pending_request(self):
        """Start the one replaceable request once the active worker ended."""
        request = self._pending_request
        if request is None or self._active_kind is not None:
            return
        if request[0] == 'image':
            self._launch_pending_image()
        elif request[0] == 'frame':
            self._launch_pending_frame()

    def _choose_image(self):
        if self._saving:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, 'Choose cover image', '',
            'Images (*.png *.jpg *.jpeg *.webp)')
        if not path:
            return
        self._active_token += 1
        self._cancel_active_preview()
        self._pending_request = ('image', self._active_token, path)
        self._frame_debounce.stop()
        if self._active_kind is None:
            self._launch_pending_image()

    def _reset_candidate(self):
        if self._saving:
            return
        self._active_token += 1
        self._candidate_png = None
        self._candidate_pixmap = QPixmap()
        self._reset_requested = True
        self._pending_request = None
        self._cancel_active_preview()
        self._set_preview(QPixmap())
        self._apply_button.setEnabled(True)
        self._reset_button.setEnabled(self._existing_reference is not None)
        self.status.setText('The generated thumbnail will be restored.')

    def _apply(self):
        if self._saving or self._active_kind in {'frame', 'image', 'probe'}:
            return
        if self._pending_request is not None:
            return
        if self._candidate_png is None and not self._reset_requested:
            self.accept()
            return
        if callable(self._save_guard):
            try:
                allowed = bool(self._save_guard())
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                FthrMessageDialog.warning(
                    self, 'Cover not saved', str(error), plain_text=True)
                return
            if not allowed:
                FthrMessageDialog.warning(
                    self, 'Cover not saved',
                    'The clip changed or is currently busy. Try again.',
                    plain_text=True)
                return
        self._saving = True
        self._apply_button.setEnabled(False)
        self._choose_button.setEnabled(False)
        self._reset_button.setEnabled(False)
        self.status.setText('Saving cover...')
        cancel = threading.Event()
        self._active_cancel = cancel
        self._active_kind = 'save'
        self._active_token += 1
        self._active_job_id = self._new_job_id()
        self._start_cover_worker(_SaveWorker(
            self._active_token, self._manager, self._source_path,
            self._candidate_png, self._expected_identity,
            self._expected_clip_id,
            (self._existing_reference.get('revision')
             if isinstance(self._existing_reference, dict) else None),
            self._channel, cancel, self._active_job_id))

    def _on_worker_result(self, token, kind, payload, error):
        if self._closed:
            return
        if kind == 'probe':
            if token != self._probe_token:
                return
            self._probe_active = False
            self._probe_cancel = None
            self._probe_job_id = None
            if error is not None:
                self.frame_slider.setEnabled(False)
                self.seconds_spin.setEnabled(False)
                if self._active_kind is None and self._candidate_png is None:
                    self.status.setText(
                        'Frame selection is unavailable because the video duration '
                        'could not be read. You can still choose an image.')
                self._launch_pending_request()
                return
            if isinstance(payload, dict):
                duration_value = payload.get('duration')
                last_frame = payload.get('last_frame')
            else:
                duration_value = payload
                last_frame = None
            try:
                duration = float(duration_value)
            except (TypeError, ValueError, OverflowError):
                duration = 0.0
            if not math.isfinite(duration) or duration <= 0:
                self.frame_slider.setEnabled(False)
                self.seconds_spin.setEnabled(False)
                if self._active_kind is None and self._candidate_png is None:
                    self.status.setText(
                        'Frame selection is unavailable because the video duration '
                        'could not be read. You can still choose an image.')
                return
            self._set_verified_duration(duration, last_frame)
            if (self._pending_request is None and self._candidate_png is None
                    and not self._reset_requested):
                # Requesting zero explicitly makes the first frame an actual
                # selectable candidate instead of relying on a slider change.
                self._schedule_frame(0.0)
            self._launch_pending_request()
            return
        if kind != self._active_kind:
            return
        if token != self._active_token:
            # A cancelled preview still owns the single active slot until its
            # terminal result arrives.  Hand the replaceable pending request
            # to the pool only at that boundary.
            if kind in {'frame', 'image', 'probe'}:
                self._active_cancel = None
                self._active_kind = None
                self._active_job_id = None
                self._launch_pending_request()
            return
        self._active_cancel = None
        self._active_kind = None
        self._active_job_id = None
        if error is not None:
            if kind == 'save':
                self._saving = False
                self._apply_button.setEnabled(True)
                self._choose_button.setEnabled(True)
                self._reset_button.setEnabled(True)
                FthrMessageDialog.warning(
                    self, 'Cover not saved', str(error), plain_text=True)
                self.status.setText('Cover was not changed.')
            elif error != 'cancelled':
                self.status.setText(str(error))
            self._launch_pending_request()
            return
        if kind in {'frame', 'image'}:
            if not isinstance(payload, dict):
                self.status.setText('Cover preview failed.')
                self._launch_pending_request()
                return
            self._candidate_png = bytes(payload.get('png', b''))
            self._reset_requested = False
            image = payload.get('image')
            if not isinstance(image, QImage) or image.isNull():
                self.status.setText('Cover preview failed.')
                self._launch_pending_request()
                return
            self._candidate_pixmap = QPixmap.fromImage(image)
            self._set_preview(self._candidate_pixmap)
            self._apply_button.setEnabled(True)
            self._reset_button.setEnabled(True)
            self.status.setText('Preview ready.')
            self._launch_pending_request()
            return
        if kind == 'save':
            self._saving = False
            if isinstance(payload, dict):
                payload = dict(payload)
                payload['pixmap'] = self._candidate_pixmap
            if isinstance(payload, dict) and payload.get('reload_needed'):
                self.status.setText(
                    'Cover saved; metadata reload is needed before further changes.')
                FthrMessageDialog.warning(
                    self, 'Cover saved',
                    'The cover was saved successfully. Metadata reload is needed '
                    'before further cover changes.', plain_text=True)
            self._cancel_owned_jobs()
            self._closed = True
            self.cover_saved.emit(payload)
            self.accept()

    def reject(self):
        if self._saving:
            return
        if self._closed:
            return
        self._closed = True
        self._cancel_owned_jobs()
        super().reject()

    def closeEvent(self, event):  # noqa: N802 - Qt API name
        if self._saving:
            event.ignore()
            return
        if self._closed:
            event.accept()
            return
        # QDialog closes through reject(), which owns cancellation and the
        # closed flag. Setting it here would veto that first rejection.
        event.accept()
        super().closeEvent(event)
