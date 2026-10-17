"""Decode and mix audio in a worker for one Qt QAudioSink.

QMediaPlayer owns video playback and the master clock. The native bridge
uses one demuxer and a decoder per selected stream; the audio callback
only drains a bounded PCM queue.
"""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from typing import Callable, Sequence

import numpy as np

from PySide6.QtCore import QIODevice, QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from core.audio_manifest import read_manifest_for_media, manifest_path_for
from core.media_identity import file_identity
from core.media_process import run_media_process
from core.ffmpeg_tools import FFmpegUnavailable, get_ffprobe_exe
from core.playback_mix_model import (
    CANONICAL_CHANNELS,
    CANONICAL_SAMPLE_RATE,
    PlaybackSource,
    ProbedAudioStream,
    SourceMixState,
    build_playback_sources,
    source_gain,
)


_NO_WINDOW = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
_FRAMES_PER_BLOCK = 1024
# Keep gain changes perceptually immediate. A half-second pre-render queue made
# every slider appear stale because it continued playing PCM mixed with the old
# values. 150 ms is still ample protection from ordinary decoder jitter.
_MAX_QUEUED_FRAMES = CANONICAL_SAMPLE_RATE * 3 // 20
_SYNC_RECOVERY_SECONDS = 0.75
_SYNC_DRIFT_LIMIT_MS = 120
_SYNC_RESUME_MARGIN_MS = 40
_OUTPUT_BUFFER_MS = 40


def _resample_pcm(pcm: bytes, playback_rate: float) -> tuple[bytes, int]:
    """Resample float32 stereo PCM for the fixed 48 kHz sink.

    Linear interpolation changes the number of source frames consumed per
    output block while retaining the same mix/gain path at each speed.
    """

    rate = max(0.25, min(2.0, float(playback_rate)))
    usable_bytes = len(pcm) - (len(pcm) % (CANONICAL_CHANNELS * 4))
    if usable_bytes <= 0:
        return b'', 0
    source = np.frombuffer(pcm[:usable_bytes], dtype='<f4')
    source_frames = source.size // CANONICAL_CHANNELS
    if source_frames <= 0:
        return b'', 0
    if abs(rate - 1.0) < 1e-6:
        return pcm[:usable_bytes], source_frames

    output_frames = max(1, round(source_frames / rate))
    if output_frames == source_frames:
        return pcm[:usable_bytes], source_frames
    source = source.reshape(source_frames, CANONICAL_CHANNELS)
    positions = np.linspace(0.0, source_frames - 1, output_frames)
    output = np.empty((output_frames, CANONICAL_CHANNELS), dtype=np.float32)
    source_x = np.arange(source_frames, dtype=np.float32)
    for channel in range(CANONICAL_CHANNELS):
        output[:, channel] = np.interp(
            positions, source_x, source[:, channel]).astype(np.float32)
    return output.astype('<f4', copy=False).tobytes(), output_frames


class PlaybackError(RuntimeError):
    """The clip can remain viewable, but editable audio is unavailable."""


class BoundedPCMQueue:
    """Small thread-safe audio queue; it never decodes in a Qt audio callback."""

    def __init__(self, bytes_per_frame: int):
        self._bytes_per_frame = bytes_per_frame
        self._data = bytearray()
        self._lock = threading.Lock()
        self.underruns = 0

    @property
    def frames(self) -> int:
        with self._lock:
            return len(self._data) // self._bytes_per_frame

    @property
    def byte_count(self) -> int:
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def append(self, pcm: bytes) -> bool:
        if not pcm or len(pcm) % self._bytes_per_frame:
            return False
        with self._lock:
            if len(self._data) + len(pcm) > _MAX_QUEUED_FRAMES * self._bytes_per_frame:
                return False
            self._data.extend(pcm)
            return True

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b''
        with self._lock:
            count = min(size, len(self._data))
            if count == 0:
                self.underruns += 1
                return b''
            data = bytes(self._data[:count])
            del self._data[:count]
            return data


class _AudioPullDevice(QIODevice):
    """Qt pull device backed only by BoundedPCMQueue.

    Invalidate each stopped sink generation so a late Windows callback returns
    silence instead of consuming PCM intended for the replacement sink.
    """

    def __init__(self, queue: BoundedPCMQueue, parent: QObject):
        super().__init__(parent)
        self._queue = queue
        self._active = True
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def invalidate(self) -> None:
        self._active = False

    def readData(self, maxlen: int) -> bytes:  # noqa: N802 - Qt virtual name
        if not self._active:
            return b''
        return self._queue.read(maxlen)

    def writeData(self, _data: bytes, _maxlen: int) -> int:  # noqa: N802
        return -1

    def isSequential(self) -> bool:  # noqa: N802
        return True

    def bytesAvailable(self) -> int:  # noqa: N802
        if not self._active:
            return super().bytesAvailable()
        return self._queue.byte_count + super().bytesAvailable()


def _bridge_candidates() -> tuple[Path, ...]:
    suffix = 'FTHRPlaybackMixer.dll'
    candidates: list[Path] = []
    bundle = getattr(sys, '_MEIPASS', None)
    if bundle:
        candidates.extend((Path(bundle) / 'engine' / suffix, Path(bundle) / suffix))
    executable_dir = Path(sys.executable).resolve().parent
    candidates.extend((executable_dir / 'engine' / suffix, executable_dir / suffix))
    root = Path(__file__).resolve().parents[2]
    development_candidates = (
        # A locked DLL cannot be replaced while the editor is open, so builds
        # can land in any of these source-checkout outputs. Choose the newest
        # existing artifact on the next launch instead of permanently preferring
        # an older, still-locked Upgrade copy.
        root / 'FTHRcapture' / 'FTHRPlaybackMixer' / 'x64' / 'Upgrade' / suffix,
        root / 'FTHRcapture' / 'FTHRPlaybackMixer' / 'x64' / 'Release' / suffix,
        root / 'FTHRcapture' / 'x64' / 'Release' / suffix,
    )

    def _modified(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    candidates.extend(sorted(development_candidates, key=_modified, reverse=True))
    return tuple(candidates)


def _load_bridge() -> ctypes.CDLL:
    errors: list[str] = []
    source_root = Path(__file__).resolve().parents[2]
    for path in _bridge_candidates():
        if not path.is_file():
            continue
        try:
            if hasattr(os, 'add_dll_directory'):
                # The bridge and FFmpeg runtime are either together in the
                # frozen ``engine`` directory or beside the development engine.
                for directory in (path.parent,
                                  source_root / 'FTHRcapture' / 'x64' / 'Release',
                                  source_root / 'FTHRcapture' / 'FTHRclips'
                                  / 'third_party' / 'ffmpeg' / 'bin'):
                    if directory.is_dir():
                        os.add_dll_directory(str(directory))
            return ctypes.CDLL(str(path))
        except OSError as error:
            errors.append(f'{path.name}: {error}')
    detail = '; '.join(errors) or 'bridge binary was not built or bundled'
    raise PlaybackError(f'In-process audio mixer unavailable: {detail}')


class InProcessFFmpegMixer:
    """``ctypes`` ownership wrapper around the native in-process FFmpeg bridge."""

    def __init__(self, media_path: str, sources: Sequence[PlaybackSource]):
        selected = [source for source in sources
                    if source.available and source.container_index is not None]
        if not selected:
            raise PlaybackError('This clip has no decodable audio stream.')
        self.sources = tuple(selected)
        self._library = _load_bridge()
        self._configure_signatures()
        indexes = (ctypes.c_int * len(self.sources))(
            *(source.container_index for source in self.sources))
        error = ctypes.create_string_buffer(512)
        self._handle = self._library.fthr_playback_open(
            os.fsencode(media_path), indexes, len(self.sources), error, len(error))
        if not self._handle:
            raise PlaybackError(error.value.decode(errors='replace') or 'could not open clip audio')
        self._pitch_compensation_available = hasattr(
            self._library, 'fthr_playback_set_playback_rate')

    def _configure_signatures(self) -> None:
        library = self._library
        library.fthr_playback_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int),
                                               ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
        library.fthr_playback_open.restype = ctypes.c_void_p
        library.fthr_playback_pull.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float),
                                               ctypes.c_int, ctypes.POINTER(ctypes.c_float),
                                               ctypes.c_int, ctypes.c_float]
        library.fthr_playback_pull.restype = ctypes.c_int
        library.fthr_playback_seek.argtypes = [ctypes.c_void_p, ctypes.c_int64,
                                               ctypes.c_char_p, ctypes.c_size_t]
        library.fthr_playback_seek.restype = ctypes.c_int
        library.fthr_playback_source_failed.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.fthr_playback_source_failed.restype = ctypes.c_int
        library.fthr_playback_close.argtypes = [ctypes.c_void_p]
        library.fthr_playback_close.restype = None
        set_rate = getattr(library, 'fthr_playback_set_playback_rate', None)
        if set_rate is not None:
            set_rate.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_int,
                                 ctypes.c_char_p, ctypes.c_size_t]
            set_rate.restype = ctypes.c_int

    def set_playback_rate(self, rate: float, preserve_pitch: bool) -> bool:
        """Configure native pitch preservation and report whether it is live.

        Older bundled bridges do not expose this entry point.  They remain
        usable with the historical resampling fallback, but cannot claim to
        preserve pitch on a backend that lacks the capability.
        """

        active = bool(preserve_pitch and abs(float(rate) - 1.0) > 1e-6)
        if not self._pitch_compensation_available:
            return False
        error = ctypes.create_string_buffer(512)
        configured = self._library.fthr_playback_set_playback_rate(
            self._handle, ctypes.c_float(rate), int(bool(preserve_pitch)),
            error, len(error))
        if not configured:
            raise PlaybackError(
                error.value.decode(errors='replace')
                or 'could not configure pitch-preserving playback')
        return active

    def pull(self, frames: int, gains: Sequence[float], master_gain: float) -> bytes | None:
        output = (ctypes.c_float * (frames * CANONICAL_CHANNELS))()
        native_gains = (ctypes.c_float * len(gains))(*gains)
        result = self._library.fthr_playback_pull(
            self._handle, output, frames, native_gains, len(gains), master_gain)
        if result < 0:
            raise PlaybackError('FFmpeg decoder failed while mixing clip audio')
        if result == 0:
            return None
        return ctypes.string_at(output, result * CANONICAL_CHANNELS * ctypes.sizeof(ctypes.c_float))

    def seek(self, position_ms: int) -> None:
        error = ctypes.create_string_buffer(512)
        if not self._library.fthr_playback_seek(
                self._handle, max(0, position_ms), error, len(error)):
            raise PlaybackError(error.value.decode(errors='replace') or 'could not seek clip audio')

    def failed_source_indexes(self) -> tuple[int, ...]:
        return tuple(index for index in range(len(self.sources))
                     if self._library.fthr_playback_source_failed(self._handle, index))

    def close(self) -> None:
        if getattr(self, '_handle', None):
            self._library.fthr_playback_close(self._handle)
            self._handle = None


def probe_audio_streams(media_path: str, cancelled: threading.Event | None = None
                        ) -> tuple[ProbedAudioStream, ...]:
    """Read only container metadata once; FFmpeg decode remains in-process."""

    try:
        probe = get_ffprobe_exe()
    except FFmpegUnavailable as error:
        raise PlaybackError(str(error)) from error
    try:
        command = [probe, '-v', 'error', '-show_entries',
             'format_tags=comment,fthr_audio_mode:stream=index,codec_type:stream_tags=title,handler_name',
             '-of', 'json', media_path]
        if cancelled is None:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=4, **_NO_WINDOW)
            returncode, output = result.returncode, result.stdout
        else:
            result = run_media_process(command, cancelled, timeout_seconds=4)
            if result is None:
                raise PlaybackError('audio stream inspection cancelled or timed out')
            returncode, output, _stderr = result
    except (OSError, subprocess.SubprocessError) as error:
        raise PlaybackError(f'could not inspect clip audio streams: {error}') from error
    if returncode != 0:
        raise PlaybackError('could not inspect clip audio streams')
    try:
        raw = json.loads(output)
        raw_streams = raw.get('streams', [])
    except (TypeError, json.JSONDecodeError) as error:
        raise PlaybackError('clip stream metadata was not valid JSON') from error
    format_section = raw.get('format') if isinstance(raw, dict) else None
    format_tags = (format_section.get('tags', {})
                   if isinstance(format_section, dict) else {})
    raw_audio_mode = (format_tags.get('fthr_audio_mode')
                      if isinstance(format_tags, dict) else None)
    if not isinstance(raw_audio_mode, str) and isinstance(format_tags, dict):
        comment = format_tags.get('comment')
        if isinstance(comment, str) and comment.casefold().startswith(
                'fthr-audio-mode='):
            raw_audio_mode = comment.split('=', 1)[1]
    audio_mode = (raw_audio_mode.strip().lower()
                  if isinstance(raw_audio_mode, str)
                  and raw_audio_mode.strip().lower() in {'combined', 'separated'}
                  else None)
    streams: list[ProbedAudioStream] = []
    for stream in raw_streams:
        if stream.get('codec_type') != 'audio' or not isinstance(stream.get('index'), int):
            continue
        tags = stream.get('tags') if isinstance(stream.get('tags'), dict) else {}
        title = tags.get('title') or tags.get('handler_name')
        streams.append(ProbedAudioStream(
            container_index=stream['index'], audio_index=len(streams),
            title=title if isinstance(title, str) else None,
            audio_mode=audio_mode))
    if audio_mode is None:
        labels = {
            str((stream.get('tags') or {}).get('title')
                or (stream.get('tags') or {}).get('handler_name')
                or '').strip().casefold()
            for stream in raw_streams
            if isinstance(stream, dict) and stream.get('codec_type') == 'audio'
        }
        if 'combined audio' in labels:
            audio_mode = 'combined'
        elif {'system audio', 'microphone'} <= labels:
            audio_mode = 'separated'
        if audio_mode is not None:
            streams = [ProbedAudioStream(
                container_index=stream.container_index,
                audio_index=stream.audio_index,
                title=stream.title,
                audio_mode=audio_mode)
                       for stream in streams]
    return tuple(streams)


_SOURCE_CACHE: OrderedDict[tuple, tuple[PlaybackSource, ...]] = OrderedDict()
_SOURCE_CACHE_LOCK = threading.Lock()
_RETIRING_AUDIO_CONTROLLERS = set()


def finish_audio_cleanup(media_path: str, timeout: float = 0.5) -> bool:
    """File mutations must wait for any recently retired decoder of this clip."""
    identity = os.path.normcase(os.path.abspath(media_path))
    for controller in tuple(_RETIRING_AUDIO_CONTROLLERS):
        worker = controller._worker
        if os.path.normcase(os.path.abspath(worker._media_path)) == identity:
            worker.join(timeout=timeout)
            if worker.is_alive():
                return False
    return True


def _source_identity(media_path):
    try:
        media = file_identity(media_path)
        try:
            manifest = file_identity(manifest_path_for(media_path))
        except FileNotFoundError:
            manifest = None
        return media, manifest
    except OSError:
        return None


def discover_playback_sources(media_path: str, cancelled: threading.Event | None = None
                              ) -> tuple[PlaybackSource, ...]:
    """Combine a valid FTHR sidecar, if any, with actual container streams."""

    if cancelled is not None and cancelled.is_set():
        raise PlaybackError('audio stream inspection cancelled')
    key = _source_identity(media_path)
    if key is not None:
        with _SOURCE_CACHE_LOCK:
            cached = _SOURCE_CACHE.get(key)
            if cached is not None:
                _SOURCE_CACHE.move_to_end(key)
                return cached
    streams = (probe_audio_streams(media_path) if cancelled is None
               else probe_audio_streams(media_path, cancelled))
    if cancelled is not None and cancelled.is_set():
        return ()
    try:
        manifest = (read_manifest_for_media(media_path) if cancelled is None
                    else read_manifest_for_media(media_path, cancelled))
    except InterruptedError as error:
        raise PlaybackError('audio source validation cancelled') from error
    sources = build_playback_sources(manifest, streams)
    if (key is not None and key == _source_identity(media_path)
            and (cancelled is None or not cancelled.is_set())):
        with _SOURCE_CACHE_LOCK:
            _SOURCE_CACHE[key] = sources
            _SOURCE_CACHE.move_to_end(key)
            while len(_SOURCE_CACHE) > 128:
                _SOURCE_CACHE.popitem(last=False)
    return sources


class _DecodeWorker(threading.Thread):
    """The only Python thread allowed to call native FFmpeg playback APIs."""

    def __init__(self, media_path: str, sources: Sequence[PlaybackSource],
                 queue: BoundedPCMQueue,
                 encode_for_device: Callable[[bytes], bytes],
                 mix_values: Callable[[], tuple[list[float], float]],
                 ready: Callable[[str | None], None],
                 failed: Callable[[str], None], source_failed: Callable[[str], None],
                  eof: Callable[[], None], pcm_ready: Callable[[], None] = lambda: None,
                  finished: Callable[[], None] = lambda: None):
        super().__init__(name='FTHR-audio-decode', daemon=True)
        self._media_path = media_path
        self._sources = tuple(sources)
        self._queue = queue
        self._encode_for_device = encode_for_device
        self._mix_values = mix_values
        self._ready = ready
        self._failed = failed
        self._source_failed = source_failed
        self._eof = eof
        self._pcm_ready = pcm_ready
        self._finished = finished
        self._condition = threading.Condition()
        self._playing = False
        self._stopping = False
        self._seek_ms: int | None = 0
        self._decoded_frames = 0
        self._playback_rate = 1.0
        self._preserve_pitch = True
        # Invalidates a pull that was already inside native FFmpeg when a new
        # seek/pause arrived.  Without this token, one old 1024-frame block
        # could be appended after the UI cleared the queue and become the first
        # audible audio at the new playhead position.
        self._command_generation = 0
        self._reported_failed_sources: set[int] = set()

    def play(self, position_ms: int) -> None:
        with self._condition:
            self._command_generation += 1
            self._seek_ms = max(0, position_ms)
            self._playing = True
            self._queue.clear()
            self._condition.notify_all()

    def pause(self) -> None:
        with self._condition:
            self._command_generation += 1
            self._playing = False
            self._queue.clear()
            self._condition.notify_all()

    def seek(self, position_ms: int) -> None:
        with self._condition:
            self._command_generation += 1
            self._seek_ms = max(0, position_ms)
            self._queue.clear()
            self._condition.notify_all()

    def set_playback_rate(self, rate: float, position_ms: int | None = None,
                          preserve_pitch: bool = True) -> None:
        rate = max(0.25, min(2.0, float(rate)))
        with self._condition:
            self._playback_rate = rate
            self._preserve_pitch = bool(preserve_pitch)
            if position_ms is not None:
                self._command_generation += 1
                self._seek_ms = max(0, int(position_ms))
                self._queue.clear()
                self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._command_generation += 1
            self._stopping = True
            self._playing = False
            self._queue.clear()
            self._condition.notify_all()

    @property
    def estimated_position_ms(self) -> int:
        with self._condition:
            queued = self._queue.frames
            queued_source_frames = round(queued * self._playback_rate)
            return max(0, round(
                (self._decoded_frames - queued_source_frames)
                * 1000 / CANONICAL_SAMPLE_RATE))

    def run(self) -> None:
        mixer: InProcessFFmpegMixer | None = None
        try:
            mixer = InProcessFFmpegMixer(self._media_path, self._sources)
            self._ready(None)
            configured_rate: float | None = None
            configured_preserve_pitch: bool | None = None
            native_pitch_compensation = False
            while True:
                with self._condition:
                    while not self._stopping and not self._playing:
                        self._condition.wait()
                    if self._stopping:
                        return
                    seek_ms = self._seek_ms
                    self._seek_ms = None
                    generation = self._command_generation
                    playback_rate = self._playback_rate
                    preserve_pitch = self._preserve_pitch
                if (configured_rate != playback_rate
                        or configured_preserve_pitch != preserve_pitch):
                    configure = getattr(mixer, 'set_playback_rate', None)
                    native_pitch_compensation = bool(
                        configure(playback_rate, preserve_pitch)
                        if callable(configure) else False)
                    configured_rate = playback_rate
                    configured_preserve_pitch = preserve_pitch
                if seek_ms is not None:
                    mixer.seek(seek_ms)
                    with self._condition:
                        if generation != self._command_generation:
                            continue
                        self._decoded_frames = round(seek_ms * CANONICAL_SAMPLE_RATE / 1000)
                if self._queue.frames >= _MAX_QUEUED_FRAMES - _FRAMES_PER_BLOCK:
                    time.sleep(0.005)
                    continue
                gains, master = self._mix_values()
                # Pitch-preserving atempo returns sink-rate PCM. Resampling without pitch
                # preservation consumes source-rate PCM and changes pitch with speed.
                requested_frames = (_FRAMES_PER_BLOCK if native_pitch_compensation
                                    else max(1, round(
                                        _FRAMES_PER_BLOCK * playback_rate)))
                block = mixer.pull(requested_frames, gains, master)
                with self._condition:
                    stale = (generation != self._command_generation
                             or not self._playing or self._stopping)
                if stale:
                    continue
                for index in mixer.failed_source_indexes():
                    if index not in self._reported_failed_sources:
                        self._reported_failed_sources.add(index)
                        self._source_failed(self._sources[index].source_id)
                if block is None:
                    with self._condition:
                        if generation != self._command_generation:
                            continue
                        self._playing = False
                    self._eof()
                    continue
                output_frames = len(block) // (CANONICAL_CHANNELS * 4)
                source_frames = (round(output_frames * playback_rate)
                                 if native_pitch_compensation else output_frames)
                encoded = (block if native_pitch_compensation
                           else _resample_pcm(block, playback_rate)[0])
                encoded = self._encode_for_device(encoded)
                wake_output = False
                with self._condition:
                    # Re-check under the command lock and append while holding
                    # it. seek()/pause() clear the queue under this same lock,
                    # so an obsolete block can never race in just afterwards.
                    if (generation != self._command_generation
                            or not self._playing or self._stopping):
                        continue
                    was_empty = self._queue.frames == 0
                    if self._queue.append(encoded):
                        self._decoded_frames += source_frames
                        wake_output = was_empty
                if wake_output:
                    # Cross-thread Qt signal: starts/restarts the endpoint on
                    # the GUI thread as soon as replacement PCM exists instead
                    # of waiting up to one 20 ms maintenance-timer interval.
                    self._pcm_ready()
        except PlaybackError as error:
            self._ready(str(error))
            self._failed(str(error))
        except Exception as error:  # Defensive: never take down the Qt event loop.
            self._ready(str(error))
            self._failed(f'audio playback stopped: {error}')
        finally:
            try:
                if mixer is not None:
                    mixer.close()
            finally:
                self._finished()


class FFmpegPlaybackController(QObject):
    """Qt-facing controller for one clip's in-process editable audio."""

    ready_changed = Signal(bool, str)
    audio_failed = Signal(str)
    source_failed = Signal(str)
    reached_eof = Signal()
    pcm_available = Signal()
    decode_finished = Signal()

    def __init__(self, media_path: str, sources: Sequence[PlaybackSource], parent: QObject):
        super().__init__(parent)
        self.sources = tuple(source for source in sources if source.available)
        self._states = {source.source_id: SourceMixState() for source in self.sources}
        self._master_gain = 1.0
        self._mix_lock = threading.Lock()
        self._sync_guard_until = 0.0
        # A clock correction may briefly hold an early audio endpoint while
        # the video catches up.  Automatic sync must never seek audio
        # backwards: doing so replays the just-heard syllable/sample and is
        # perceived as a deterministic double hit after a timeline seek.
        self._sync_holding = False
        self._stopping = False
        self._async_stop = False
        self.decode_finished.connect(self._finish_async_stop)
        self._output_device, self._output_format, self._encode_for_device = self._select_output_format()
        self._queue = BoundedPCMQueue(self._output_format.bytesPerFrame())
        self._device: _AudioPullDevice | None = None
        self._sink: QAudioSink | None = None
        self._sink_timer = QTimer(self)
        self._sink_timer.setInterval(20)
        self._sink_timer.timeout.connect(self._keep_sink_running)
        self.pcm_available.connect(self._keep_sink_running)
        self._worker = _DecodeWorker(
            media_path, self.sources, self._queue, self._encode_for_device, self._mix_values,
            self._worker_ready, self._worker_failed, self.source_failed.emit,
            self.reached_eof.emit, self.pcm_available.emit,
            self.decode_finished.emit)
        # The owner must connect ready/error signals before this thread starts;
        # otherwise a fast bridge open can emit readiness before Qt has any
        # receiver and leave the fallback output permanently muted.
        self._worker_started = False

    def start(self) -> None:
        if self._worker_started:
            return
        self._worker_started = True
        self._worker.start()

    def _select_output_format(
            self) -> tuple[object, QAudioFormat, Callable[[bytes], bytes]]:
        device = QMediaDevices.defaultAudioOutput()
        float_format = QAudioFormat()
        float_format.setSampleRate(CANONICAL_SAMPLE_RATE)
        float_format.setChannelCount(CANONICAL_CHANNELS)
        float_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        if device.isFormatSupported(float_format):
            return device, float_format, lambda pcm: pcm

        int16_format = QAudioFormat()
        int16_format.setSampleRate(CANONICAL_SAMPLE_RATE)
        int16_format.setChannelCount(CANONICAL_CHANNELS)
        int16_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if device.isFormatSupported(int16_format):
            def _to_int16(pcm: bytes) -> bytes:
                samples = np.frombuffer(pcm, dtype='<f4')
                return (np.clip(samples, -1.0, 1.0) * 32767.0).astype('<i2').tobytes()
            return device, int16_format, _to_int16
        raise PlaybackError('Default output supports neither 48 kHz float nor 16-bit stereo audio.')

    def _mix_values(self) -> tuple[list[float], float]:
        with self._mix_lock:
            # Application stems are reversible deltas against Default Mix.
            # Feeding their raw 0..1 UI gain here duplicated every app at 100%
            # and made 0% merely remove that duplicate. source_gain() converts
            # them to the required -1..0 delta while leaving mic/import tracks
            # as ordinary direct gains and the hidden base fixed at 1.
            return ([source_gain(source, self._states) for source in self.sources],
                    self._master_gain)

    def _worker_ready(self, error: str | None) -> None:
        if not self._stopping:
            self.ready_changed.emit(error is None, error or '')

    def _worker_failed(self, error: str) -> None:
        if not self._stopping:
            self.audio_failed.emit(error)

    def set_master_percent(self, value: int) -> None:
        with self._mix_lock:
            self._master_gain = max(0, min(100, value)) / 100.0

    def set_source_state(self, source_id: str, gain_percent: int | None = None,
                         muted: bool | None = None) -> None:
        with self._mix_lock:
            current = self._states.get(source_id, SourceMixState())
            self._states[source_id] = SourceMixState(
                gain_percent=current.gain_percent if gain_percent is None else gain_percent,
                muted=current.muted if muted is None else muted)

    def set_playback_rate(self, rate: float, position_ms: int | None = None,
                          preserve_pitch: bool = True) -> None:
        """Match live mixed audio to speed, with optional pitch preservation."""

        normalized = max(0.25, min(2.0, float(rate)))
        self._worker.set_playback_rate(
            normalized, position_ms, preserve_pitch=preserve_pitch)
        if position_ms is not None:
            self._discard_output_buffer()
            self._sync_guard_until = time.monotonic() + _SYNC_RECOVERY_SECONDS
            self._keep_sink_running()

    def refresh_mix(self, position_ms: int) -> None:
        """Apply new gains at the video clock after discarding queued old-mix PCM.

        The editor coalesces changes to at most one call per 40 ms to limit seeks.
        """

        if not self._worker_started:
            return
        self._worker.seek(max(0, position_ms))
        self._discard_output_buffer()
        self._sync_guard_until = time.monotonic() + _SYNC_RECOVERY_SECONDS
        self._keep_sink_running()

    def source_state(self, source_id: str) -> SourceMixState:
        with self._mix_lock:
            return self._states.get(source_id, SourceMixState())

    def play(self, position_ms: int) -> None:
        self.start()
        self._worker.play(position_ms)
        self._discard_output_buffer()
        self._sync_guard_until = time.monotonic() + _SYNC_RECOVERY_SECONDS
        self._sink_timer.start()

    def pause(self) -> None:
        self._worker.pause()
        self._discard_output_buffer()

    def seek(self, position_ms: int) -> None:
        self._worker.seek(position_ms)
        self._discard_output_buffer()
        self._sync_guard_until = time.monotonic() + _SYNC_RECOVERY_SECONDS

    def sync_to_video_position(self, position_ms: int) -> None:
        # Subtract both the Python queue and QAudioSink device buffer from the
        # worker position before comparing it with QMediaPlayer. After a hard seek,
        # allow one prebuffer interval before correcting again to avoid seek loops.
        if time.monotonic() < getattr(self, '_sync_guard_until', 0.0):
            return
        drift_ms = self._estimated_output_position_ms() - max(0, position_ms)

        # If audio is early, freeze the endpoint and let the monotonic video
        # clock catch up.  Seeking it backwards would replay PCM that has
        # already reached the listener, which was the short double-audio glitch
        # seen at repeatable timeline positions.  Hysteresis avoids rapidly
        # toggling suspend/resume around the correction threshold.
        if getattr(self, '_sync_holding', False):
            if drift_ms > _SYNC_RESUME_MARGIN_MS:
                return
            self._sync_holding = False
            if drift_ms < -_SYNC_DRIFT_LIMIT_MS:
                # Video passed the held endpoint: skip forward, never rewind.
                self.seek(position_ms)
                return
            sink = self._sink
            if sink is not None:
                try:
                    if sink.state() == QAudio.State.SuspendedState:
                        sink.resume()
                except (AttributeError, RuntimeError, TypeError):
                    # The endpoint may be deleted asynchronously during stop.
                    pass
            return

        if drift_ms > _SYNC_DRIFT_LIMIT_MS:
            sink = self._sink
            if sink is not None:
                try:
                    sink.suspend()
                    self._sync_holding = True
                except (AttributeError, RuntimeError, TypeError):
                    # A disappearing output endpoint is handled by state polling.
                    pass
            return
        if drift_ms < -_SYNC_DRIFT_LIMIT_MS:
            # Skipping late audio forward cannot replay anything already heard.
            self.seek(position_ms)

    def _estimated_output_position_ms(self) -> int:
        buffered_frames = 0
        if self._sink is not None:
            try:
                capacity = max(0, int(self._sink.bufferSize()))
                free = max(0, min(capacity, int(self._sink.bytesFree())))
                bytes_per_frame = max(1, self._output_format.bytesPerFrame())
                buffered_frames = (capacity - free) // bytes_per_frame
            except (AttributeError, RuntimeError, TypeError, ValueError):
                # A device can disappear between the state check and these Qt
                # calls.  Error reporting remains owned by _on_sink_state().
                buffered_frames = 0
        buffered_ms = round(buffered_frames * 1000 / CANONICAL_SAMPLE_RATE)
        return max(0, self._worker.estimated_position_ms - buffered_ms)

    def _discard_output_buffer(self) -> None:
        # Recreate the sink after a seek: the Windows Qt backend may not resume
        # pulls after reset(). Replacing it also discards device-buffered PCM.
        # _keep_sink_running() opens the new endpoint once fresh PCM is available.
        self._release_sink()

    def _release_sink(self) -> None:
        self._sync_holding = False
        device = getattr(self, '_device', None)
        if device is not None:
            try:
                device.invalidate()
            except (AttributeError, RuntimeError, TypeError):
                # An already-destroyed Qt device needs no further cleanup.
                pass
        sink = self._sink
        if sink is None:
            return
        # Clear the field first. stop() emits stateChanged synchronously on
        # some backends and the old sink must not be mistaken for the active
        # endpoint by _on_sink_state().
        self._sink = None
        try:
            sink.stop()
        except (AttributeError, RuntimeError, TypeError):
            # Stop is best-effort after detaching this sink generation.
            pass
        try:
            sink.deleteLater()
        except (AttributeError, RuntimeError, TypeError):
            # Qt may already have destroyed the endpoint during shutdown.
            pass

    def _new_pull_device(self) -> _AudioPullDevice:
        """Return a fresh, active device for one sink generation."""

        old_device = getattr(self, '_device', None)
        if old_device is not None:
            try:
                old_device.invalidate()
                old_device.close()
                old_device.deleteLater()
            except (AttributeError, RuntimeError, TypeError):
                # Replacing an already-invalidated device is safe and expected.
                pass
        self._device = _AudioPullDevice(self._queue, self)
        return self._device

    def _keep_sink_running(self) -> None:
        if getattr(self, '_stopping', False):
            return
        if self._queue.frames == 0:
            return
        if self._sink is None:
            device = self._new_pull_device()
            self._sink = QAudioSink(self._output_device, self._output_format, self)
            try:
                self._sink.setBufferSize(
                    self._output_format.bytesPerFrame() * CANONICAL_SAMPLE_RATE
                    * _OUTPUT_BUFFER_MS // 1000)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                # Some Qt audio backends reject advisory buffer sizing; their
                # safe backend default remains usable.
                pass
            self._sink.stateChanged.connect(self._on_sink_state)
            self._sink.start(device)
        elif self._sink.state() == QAudio.State.SuspendedState:
            if not getattr(self, '_sync_holding', False):
                self._sink.resume()
        elif self._sink.state() == QAudio.State.IdleState:
            # An empty pull queue is an underrun, not a device-loss error.
            # Resume only after a worker-filled block exists; no decode runs in
            # the audio callback and no timing sleep is used for recovery.
            if self._device is not None:
                self._sink.start(self._device)
        elif (self._sink.state() == QAudio.State.StoppedState
              and self._sink.error() == QAudio.Error.NoError):
            # A stopped Windows endpoint cannot be trusted to restart in place.
            # Recreate it while queued PCM is available so unexpected benign
            # stops recover through the same proven path as a corrective seek.
            self._release_sink()
            self._keep_sink_running()

    def _on_sink_state(self, state: QAudio.State) -> None:
        if state != QAudio.State.StoppedState or self._sink is None:
            return
        if self._sink.error() in (QAudio.Error.OpenError, QAudio.Error.IOError,
                                  QAudio.Error.FatalError):
            self.audio_failed.emit('Audio output device stopped unexpectedly.')

    def stop_async(self) -> None:
        """Keep the QObject alive until native decode has released its files."""
        if self._async_stop:
            return
        self._async_stop = True
        self.setParent(None)
        _RETIRING_AUDIO_CONTROLLERS.add(self)
        self.stop(wait_for_worker=False)
        if not self._worker_started or not self._worker.is_alive():
            self._finish_async_stop()

    def _finish_async_stop(self) -> None:
        if self._async_stop:
            self._async_stop = False
            _RETIRING_AUDIO_CONTROLLERS.discard(self)
            self.deleteLater()

    def stop(self, *, wait_for_worker=True) -> None:
        self._stopping = True
        self._sink_timer.stop()
        if self._worker_started:
            self._worker.stop()
            if wait_for_worker:
                self._worker.join(timeout=0.5)
                if self._worker.is_alive():
                    self.stop_async()
        else:
            self._queue.clear()
        self._release_sink()
        device = getattr(self, '_device', None)
        if device is not None:
            try:
                device.close()
            except (AttributeError, RuntimeError, TypeError):
                # Process teardown may destroy the Qt device before stop().
                pass
