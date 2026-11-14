from dataclasses import replace

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QMediaPlayer

from core.playback_mix_model import PlaybackSource
from ui import clip_viewer


SYSTEM = PlaybackSource('system', 'System Audio', 'system', 2, 1, codec='pcm_f32le')


class _Mixer(QObject):
    ready_changed = Signal(bool, str)
    audio_failed = Signal(str)
    source_failed = Signal(str)
    reached_eof = Signal()

    def __init__(self, _path, sources, parent, *, limiter_enabled):
        super().__init__(parent)
        self.sources = sources
        self.limiter_enabled = limiter_enabled
        self.rate = None
        self.stopped = False

    def set_master_percent(self, _value):
        pass

    def set_source_state(self, *_args, **_kwargs):
        pass

    def set_playback_rate(self, rate, _position, *, preserve_pitch):
        self.rate = (rate, preserve_pitch)

    def start(self):
        pass

    def stop_async(self):
        self.stopped = True

    stop = stop_async


@pytest.fixture
def editor(monkeypatch, qtbot, tmp_path):
    monkeypatch.setattr(clip_viewer.ClipViewer, '_prepare_initial_media_source', lambda _: None)
    monkeypatch.setattr(clip_viewer.ClipViewer, '_discover_audio_sources_async', lambda _: None)
    monkeypatch.setattr(clip_viewer, 'FFmpegPlaybackController', _Mixer)
    viewer = clip_viewer.ClipViewer(str(tmp_path / 'clip.mp4'), None)
    qtbot.addWidget(viewer)
    selection = [0]
    monkeypatch.setattr(viewer.player, 'hasVideo', lambda: True)
    monkeypatch.setattr(viewer.player, 'audioTracks', lambda: [object(), object()])
    monkeypatch.setattr(viewer.player, 'activeAudioTrack', lambda: selection[0])
    monkeypatch.setattr(viewer.player, 'setActiveAudioTrack', lambda index: selection.__setitem__(0, index))
    yield viewer
    viewer._teardown_player()


def test_single_dry_track_selects_its_ordinal_and_keeps_gain_mute_and_history(editor):
    player = editor.player
    editor._on_audio_sources_discovered((SYSTEM,), '')
    assert not editor._media_ready
    assert player.activeAudioTrack() == 0

    editor._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)

    assert editor.player is player
    assert player.activeAudioTrack() == SYSTEM.audio_index
    assert editor._native_audio_source_id == SYSTEM.source_id
    assert editor._audio_mixer is None
    assert editor._playback_ready

    panel = editor._audio_mix_panel
    panel._sliders['master'].setValue(70)
    panel._sliders['system'].setValue(50)
    assert editor.audio_output.volume() == pytest.approx(0.35)
    panel._mute_buttons['system'].setChecked(True)
    assert editor.audio_output.isMuted()
    editor._undo()
    assert not editor.audio_output.isMuted()

    editor._on_audio_source_removed('system')
    assert editor.audio_output.isMuted()
    assert 'system' not in panel._sliders
    editor._undo()
    assert not editor.audio_output.isMuted()
    assert panel._sliders['system'].value() == 50
    editor._redo()
    assert editor.audio_output.isMuted()
    assert editor._audio_mixer is None


def test_pitch_preserving_speed_uses_mixer_until_returning_to_direct(editor, monkeypatch, qtbot):
    video_rates = []
    monkeypatch.setattr(editor.player, 'setPlaybackRate', video_rates.append)
    editor._on_audio_sources_discovered((SYSTEM,), '')
    editor._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
    editor.speed_combo.setCurrentIndex(editor.speed_combo.findData(2.0))

    mixer = editor._audio_mixer
    assert mixer.rate == (2.0, True)
    assert mixer.limiter_enabled
    mixer.ready_changed.emit(True, '')
    qtbot.waitUntil(editor.audio_output.isMuted)

    editor.preserve_pitch_toggle.setChecked(False)
    assert mixer.stopped
    assert editor._audio_mixer is None
    assert not editor.audio_output.isMuted()
    assert video_rates[-1] == 2.0

    editor._undo()
    assert editor._audio_mixer.rate == (2.0, True)
    editor._undo()
    assert editor._audio_mixer is None
    assert editor._native_audio_source_id == 'system'
    assert video_rates[-1] == 1.0


def test_undo_import_while_mixer_prepares_retires_it_and_restores_direct(editor, qtbot):
    editor._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
    editor._on_audio_sources_discovered((SYSTEM,), '')
    imported = PlaybackSource('import:one', 'music.wav', 'track', 0, 0, media_path='music.wav')
    editor._on_external_audio_discovered((imported,), '', editor._prepare_cancel)

    mixer = editor._audio_mixer
    assert mixer.sources == (SYSTEM, imported)
    assert not editor._audio_preparation_ready

    editor._undo()
    assert mixer.stopped
    assert editor._audio_mixer is None
    assert editor._native_audio_source_id == 'system'
    assert editor._playback_ready
    mixer.ready_changed.emit(True, '')
    qtbot.wait(1)
    assert not editor.audio_output.isMuted()

    editor._redo()
    assert editor._audio_mixer.sources == (SYSTEM, imported)
    editor._on_audio_source_removed(imported.source_id)
    assert editor._audio_mixer is None
    assert editor._native_audio_source_id == 'system'


def test_mixer_failure_preserves_audible_container_backup(editor, qtbot):
    backup = replace(SYSTEM, audio_index=0, mix_role='backup')
    app = PlaybackSource('app', 'App', 'application', 2, 1)
    editor._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
    editor._on_audio_sources_discovered((backup, app), '')

    editor._audio_mixer.ready_changed.emit(False, 'unavailable')
    qtbot.waitUntil(lambda: editor._audio_mixer is None)

    assert editor._playback_ready
    assert not editor.audio_output.isMuted()


@pytest.mark.parametrize('sources', [
    (replace(SYSTEM, input_gain_percent=0),),
    (replace(SYSTEM, input_gain_percent=110),),
    (replace(SYSTEM, mono=True),),
    (replace(SYSTEM, source_type='microphone', effect_filter_snapshot='highpass=f=80'),),
    (replace(SYSTEM, display_name='System Audio (backup)'),),
    (replace(SYSTEM, mix_role='base'),),
    (replace(SYSTEM, audio_index=2),),
    (SYSTEM, PlaybackSource('second', 'Second source', 'application', 1, 0)),
])
def test_processed_ambiguous_or_unselectable_tracks_keep_the_mixer(editor, sources):
    editor._on_media_status_changed(QMediaPlayer.MediaStatus.LoadedMedia)
    editor._on_audio_sources_discovered(sources, '')

    assert editor._audio_mixer.sources == sources
    assert editor._native_audio_source_id is None
