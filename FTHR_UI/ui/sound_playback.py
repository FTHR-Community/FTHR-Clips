"""Play cached notification and customization sounds.

Preload WAV cues with QSoundEffect. Other formats use QMediaPlayer and
start after their local file has loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import logging

from PySide6.QtCore import QUrl

_LOG = logging.getLogger(__name__)
_MULTIMEDIA_ERROR: str | None = None
try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QSoundEffect
except (ImportError, OSError) as error:
    # QtMultimedia is optional for the rest of the application. Keep this
    # boundary narrow: required QtCore/Widgets imports remain fail-fast.
    QAudioOutput = QMediaPlayer = QSoundEffect = None
    _MULTIMEDIA_ERROR = f'{type(error).__name__}: {error}'[:240]
    _LOG.warning('Optional Qt multimedia unavailable: %s', _MULTIMEDIA_ERROR)


if QMediaPlayer is None:
    _MEDIA_READY = set()
    _MEDIA_FAILED = set()
else:
    _MEDIA_READY = {
        QMediaPlayer.MediaStatus.LoadedMedia,
        QMediaPlayer.MediaStatus.BufferedMedia,
    }
    _MEDIA_FAILED = {
        QMediaPlayer.MediaStatus.InvalidMedia,
        QMediaPlayer.MediaStatus.NoMedia,
    }


@dataclass
class _Entry:
    path: Path
    signature: tuple[int, int]
    kind: str
    player: Any
    output: Any = None
    ready: bool = False
    # ``None`` means this source was only prepared.  Generation 0 is a valid
    # initial controller state, so using 0 as the old sentinel made every
    # preloaded cue play as soon as QSoundEffect reported it loaded.
    pending_generation: int | None = None


class SoundPlayback:
    """Cache sound sources and start them only after they are ready."""

    def __init__(self, parent=None, preload_paths=()):
        self._parent = parent
        self._entries: dict[str, _Entry] = {}
        self._generation = 0
        self._active: _Entry | None = None
        for path in preload_paths:
            self.prepare(path)

    @staticmethod
    def _signature(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _key(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)

    @staticmethod
    def _is_wav(path: Path) -> bool:
        return path.suffix.lower() == '.wav'

    def prepare(self, path: Path | str) -> bool:
        """Begin loading ``path`` without playing it."""
        if QSoundEffect is None or QMediaPlayer is None or QAudioOutput is None:
            return False
        path = Path(path)
        signature = self._signature(path)
        if signature is None:
            return False

        key = self._key(path)
        current = self._entries.get(key)
        if current is not None and current.signature == signature:
            return True
        if current is not None:
            self._stop_entry(current)

        if self._is_wav(path):
            effect = QSoundEffect(self._parent)
            entry = _Entry(path, signature, 'effect', effect)
            status_signal = getattr(effect, 'statusChanged', None)
            if status_signal is not None:
                status_signal.connect(
                    lambda entry=entry: self._effect_status_changed(entry))
            else:
                entry.ready = True
            self._entries[key] = entry
            effect.setSource(QUrl.fromLocalFile(key))
            if getattr(effect, 'isLoaded', lambda: False)():
                entry.ready = True
            return True

        output = QAudioOutput(self._parent)
        player = QMediaPlayer(self._parent)
        player.setAudioOutput(output)
        entry = _Entry(path, signature, 'media', player, output)
        status_signal = getattr(player, 'mediaStatusChanged', None)
        if status_signal is not None:
            status_signal.connect(
                lambda status, entry=entry: self._media_status_changed(
                    entry, status))
        else:
            entry.ready = True
        self._entries[key] = entry
        player.setSource(QUrl.fromLocalFile(key))
        if getattr(player, 'mediaStatus', lambda: None)() in _MEDIA_READY:
            entry.ready = True
        return True

    def _effect_status_changed(self, entry: _Entry) -> None:
        try:
            ready = bool(entry.player.isLoaded())
        except Exception:
            # A deleted Qt effect is equivalent to a failed asynchronous load.
            ready = False
        entry.ready = ready
        if ready:
            self._play_if_current(entry)

    def _media_status_changed(self, entry: _Entry, status) -> None:
        if status in _MEDIA_READY:
            entry.ready = True
            self._play_if_current(entry)
        elif status in _MEDIA_FAILED:
            entry.ready = False
            entry.pending_generation = None

    def _play_if_current(self, entry: _Entry) -> None:
        if (entry.pending_generation is None
                or entry.pending_generation != self._generation):
            return
        entry.pending_generation = None
        try:
            entry.player.play()
            self._active = entry
        except Exception:
            entry.pending_generation = None

    @staticmethod
    def _stop_entry(entry: _Entry) -> None:
        entry.pending_generation = None
        try:
            entry.player.stop()
        except Exception:
            # Qt may destroy an audio object while its status callback is queued.
            pass

    def stop(self) -> None:
        """Stop all cues and invalidate any delayed start callbacks."""
        self._generation += 1
        for entry in self._entries.values():
            self._stop_entry(entry)
        self._active = None

    def set_volume(self, volume: int) -> None:
        """Update the currently playing cue without restarting it."""
        if self._active is None:
            return
        try:
            if self._active.kind == 'effect':
                self._active.player.setVolume(max(0, min(100, volume)) / 100.0)
            else:
                self._active.output.setVolume(max(0, min(100, volume)) / 100.0)
        except Exception:
            # Volume updates are optional after an endpoint disappears.
            pass

    def play(self, path: Path | str, volume: int = 100) -> bool:
        """Play a cached or newly loaded cue at ``volume`` percent."""
        volume = max(0, min(100, int(volume)))
        self.stop()
        if volume == 0 or not self.prepare(path):
            return False

        entry = self._entries[self._key(Path(path))]
        self._generation += 1
        entry.pending_generation = self._generation
        try:
            if entry.kind == 'effect':
                entry.player.setVolume(volume / 100.0)
            else:
                entry.output.setVolume(volume / 100.0)
        except Exception:
            entry.pending_generation = None
            return False
        self._play_if_current(entry)
        return True
