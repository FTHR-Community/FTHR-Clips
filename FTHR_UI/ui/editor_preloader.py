"""One optional, silent media preparation slot for the next editor open."""

from dataclasses import dataclass, field
import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer, QVideoSink

from core.audio_manifest import manifest_path_for
from core.ffmpeg_playback import FFmpegPlaybackController, PlaybackError, discover_playback_sources
from core.field_diagnostics import emit_event
from core.media_identity import file_identity
from core.playback_mix_model import PlaybackSource
from core.playback_proxy import cached_playback_path, prepare_compatibility_path


_PRELOAD_LIFETIME_SECONDS = 30
_LOADED_MEDIA = (QMediaPlayer.MediaStatus.LoadedMedia,
                 QMediaPlayer.MediaStatus.BufferedMedia)
_PENDING = object()
_LOG = logging.getLogger(__name__)


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def _clip_identity(path):
    media = file_identity(path)
    try:
        manifest = file_identity(manifest_path_for(path))
    except FileNotFoundError:
        manifest = None
    return media, manifest


@dataclass(eq=False)
class _Request:
    path: str
    quality: str
    deadline: float
    cancelled: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class _Inspection:
    clip_identity: tuple
    playback_path: str
    playback_identity: tuple
    audio_sources: tuple[PlaybackSource, ...] | None

    def matches_files(self, path):
        try:
            return (self.clip_identity == _clip_identity(path)
                    and self.playback_identity == file_identity(self.playback_path))
        except OSError:
            return False


def _inspect(request):
    path, cancelled = request.path, request.cancelled
    if cancelled.is_set():
        return None
    clip_identity = _clip_identity(path)
    audio_sources = None

    def inspect_audio():
        nonlocal audio_sources
        try:
            audio_sources = discover_playback_sources(path, cancelled)
        except PlaybackError:
            # Let the foreground audio probe retry/report the error normally.
            pass
        except Exception:
            _LOG.debug('Optional editor audio inspection failed', exc_info=True)

    audio_worker = threading.Thread(target=inspect_audio, name='FTHR-editor-preload-audio',
                                    daemon=True)
    audio_worker.start()
    try:
        playback_path = cached_playback_path(path, request.quality)
        if playback_path == path:
            playback_path = prepare_compatibility_path(path, cancelled)
        if cancelled.is_set():
            return None
        playback_identity = file_identity(playback_path)
    finally:
        # Do not release the slot while its audio helper is still reading.
        audio_worker.join()
    result = _Inspection(clip_identity, playback_path, playback_identity, audio_sources)
    if not cancelled.is_set() and result.matches_files(path):
        return result
    return None


def _run_inspection(request, completed):
    inspection = None
    try:
        inspection = _inspect(request)
    except OSError:
        # A clip may be renamed, replaced, or deleted while hovering.
        pass
    except Exception:
        _LOG.debug('Optional editor preload failed', exc_info=True)
    try:
        completed.emit(request, inspection)
    except RuntimeError:
        # Qt can destroy the owning window while the probe is unwinding.
        pass


class PreparedEditorMedia:
    """Own the loaded media until the editor adopts it or the caller disposes it."""

    def __init__(self, parent, request, inspection):
        self._request = request
        self._inspection = inspection
        self._owner = parent
        self._mixer = None
        self._player = QMediaPlayer(parent)
        self._audio = QAudioOutput(self._player)
        self._sink = QVideoSink(self._player)
        self._audio.setMuted(True)
        self._player.setAudioOutput(self._audio)
        self._player.setVideoSink(self._sink)
        # Leave the playback clock stopped. Opening the source primes media
        # loading without starting an offscreen decode or writing audio.
        self._player.setSource(QUrl.fromLocalFile(os.path.abspath(inspection.playback_path)))
        sources = tuple(source for source in (inspection.audio_sources or ())
                        if source.available and source.container_index is not None)
        needs_mixer = (len(sources) > 1 or any(
            source.mix_role != 'direct' or source.input_gain_percent != 100
            or source.mono or source.effect_filter_snapshot for source in sources))
        retiring = getattr(parent, '_retiring_mixer', None)
        retiring_alive = retiring is not None and retiring._worker.is_alive()
        if needs_mixer and not retiring_alive:
            try:
                # start() opens decoders/DSP but never advances the clock or
                # starts an audio endpoint. Only the editor can call play().
                self._mixer = FFmpegPlaybackController(request.path, sources, parent)
                self._mixer.start()
            except (PlaybackError, RuntimeError):
                self._dispose_mixer()

    def take_audio_mixer(self, parent):
        mixer, self._mixer = self._mixer, None
        if mixer is None:
            return None
        result = mixer.preparation_result
        if (not self._inspection.matches_files(self._request.path)
                or (result is not None and not result[0])
                or mixer._output_device.id() != QMediaDevices.defaultAudioOutput().id()):
            self._mixer = mixer
            self._dispose_mixer()
            return None
        mixer.setParent(parent)
        return mixer

    def _dispose_mixer(self):
        mixer, self._mixer = self._mixer, None
        if mixer is not None:
            self._owner._retiring_mixer = mixer
            mixer.stop_async()

    @property
    def audio_sources(self):
        return self._inspection.audio_sources

    def _is_usable(self, path):
        player = self._player
        if (player is None or _path_key(path) != _path_key(self._request.path)
                or time.monotonic() >= self._request.deadline
                or not self._inspection.matches_files(path)):
            return False
        try:
            loading = player.mediaStatus() == QMediaPlayer.MediaStatus.LoadingMedia
            return ((loading or player.mediaStatus() in _LOADED_MEDIA)
                    and player.error() == QMediaPlayer.Error.NoError
                    and (loading or player.hasVideo())
                    and player.playbackState() == QMediaPlayer.PlaybackState.StoppedState)
        except RuntimeError:
            return False

    def adopt(self, parent, *, clip_path, playback_path):
        """Transfer the idle player, its audio output, and the stable decoder sink."""
        if (_path_key(playback_path) != _path_key(self._inspection.playback_path)
                or not self._is_usable(clip_path)):
            self.dispose()
            return None
        player, audio, sink = self._player, self._audio, self._sink
        player.setParent(parent)
        self._player = self._audio = self._sink = None
        return player, audio, sink

    def dispose(self):
        self._dispose_mixer()
        player = self._player
        self._player = self._audio = self._sink = None
        if player is None:
            return
        try:
            player.setSource(QUrl())
            player.setVideoSink(None)
            player.setAudioOutput(None)
            player.deleteLater()
        except RuntimeError:
            # The Qt parent may have already released the player and its outputs.
            pass


class PendingPreparedMedia:
    """Transfer an inspection to the editor without interrupting its worker."""

    def __init__(self, request):
        self._request = request
        self._claimed = False
        self._callback = None
        self._result = _PENDING

    @property
    def quality(self):
        return self._request.quality

    def bind(self, path, quality, callback):
        if (self._claimed or self._request.cancelled.is_set()
                or _path_key(path) != _path_key(self._request.path)
                or quality != self.quality):
            return False
        self._claimed = True
        if self._result is _PENDING:
            self._callback = callback
        else:
            callback(self._result)
        return True

    def _finish(self, inspection):
        if self._request.cancelled.is_set():
            return
        self._result = inspection
        callback, self._callback = self._callback, None
        if callback is not None:
            callback(inspection)

    def cancel(self):
        self._callback = None
        self._request.cancelled.set()

    def dispose(self):
        if not self._claimed:
            self.cancel()


class EditorPreloader(QObject):
    """Keep one worker in flight and coalesce subsequent requests to the latest."""

    _completed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._desired = None
        self._active = None
        self._ready = None
        self._pending = None
        self._retiring_mixer = None
        self._expires = QTimer(self)
        self._expires.setSingleShot(True)
        self._expires.timeout.connect(self.clear)
        self._completed.connect(self._on_inspected, Qt.ConnectionType.QueuedConnection)

    def request(self, path, *, preview_quality='auto'):
        path = os.fspath(path)
        current = self._desired
        if (current is not None and _path_key(current.path) == _path_key(path)
                and current.quality == preview_quality and self.keep_alive(path)):
            return
        self.clear()
        self._desired = _Request(path, preview_quality,
                                 time.monotonic() + _PRELOAD_LIFETIME_SECONDS)
        emit_event('playback', 'preload_requested')
        self._expires.start(_PRELOAD_LIFETIME_SECONDS * 1000)
        self._start_latest()

    def keep_alive(self, path):
        request = self._desired
        if (request is None or request.cancelled.is_set()
                or time.monotonic() >= request.deadline
                or _path_key(path) != _path_key(request.path)
                or (self._ready is not None and not self._ready._is_usable(path))):
            return False
        request.deadline = time.monotonic() + _PRELOAD_LIFETIME_SECONDS
        self._expires.start(_PRELOAD_LIFETIME_SECONDS * 1000)
        return True

    def take(self, path):
        """Transfer a matching player or its still-pending inspection."""
        prepared, self._ready = self._ready, None
        request = self._desired
        if prepared is not None:
            if prepared._is_usable(path):
                self.clear()
                emit_event('playback', 'preload_taken', hit=True,
                           audio_prepared=prepared._mixer is not None)
                return prepared
            prepared.dispose()
        elif (request is not None and not request.cancelled.is_set()
              and time.monotonic() < request.deadline
              and _path_key(path) == _path_key(request.path)
              and (self._pending is None or self._pending._request.cancelled.is_set())):
            self._desired = None
            self._expires.stop()
            pending = self._pending = PendingPreparedMedia(request)
            self._start_latest()
            emit_event('playback', 'preload_taken', hit=True, pending=True)
            return pending
        self.clear()
        emit_event('playback', 'preload_taken', hit=False)
        return None

    @Slot()
    def clear(self):
        self._expires.stop()
        if self._desired is not None:
            self._desired.cancelled.set()
            self._desired = None
        if self._ready is not None:
            self._ready.dispose()
            self._ready = None

    def _start_latest(self):
        pending = self._pending
        if pending is not None and pending._request.cancelled.is_set():
            self._pending = pending = None
        request = pending._request if pending is not None else self._desired
        if self._active is not None or request is None:
            return
        self._active = request
        self.destroyed.connect(request.cancelled.set)
        worker = threading.Thread(target=_run_inspection, name='FTHR-editor-preload',
                                  args=(request, self._completed), daemon=True)
        try:
            worker.start()
        except RuntimeError:
            self._on_inspected(request, None)

    @Slot(object, object)
    def _on_inspected(self, request, inspection):
        self.destroyed.disconnect(request.cancelled.set)
        self._active = None
        pending = self._pending
        if pending is not None and request is pending._request:
            self._pending = None
            if inspection is not None and not inspection.matches_files(request.path):
                inspection = None
            pending._finish(inspection)
            self._start_latest()
            return
        if request is not self._desired:
            self._start_latest()
            return
        if (inspection is None or request.cancelled.is_set()
                or time.monotonic() >= request.deadline
                or not inspection.matches_files(request.path)):
            self.clear()
            return
        self._ready = PreparedEditorMedia(self, request, inspection)
