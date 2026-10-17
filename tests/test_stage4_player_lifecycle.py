from __future__ import annotations

from types import SimpleNamespace
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtMultimedia import QMediaPlayer

from ui.clip_viewer import ClipViewer, PlayerLifecycleState


def test_player_lifecycle_states_are_explicit_and_terminal_failure_exists():
    assert PlayerLifecycleState.PREPARING.value == 'PREPARING'
    assert PlayerLifecycleState.PLAY_QUEUED.value == 'PLAY_QUEUED'
    assert PlayerLifecycleState.READY.value == 'READY'
    assert PlayerLifecycleState.FAILED.value == 'FAILED'
    assert PlayerLifecycleState.CLOSING.value == 'CLOSING'

    viewer = SimpleNamespace(
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _player_failure_detail='',
    )
    ClipViewer._set_player_state(
        viewer, PlayerLifecycleState.FAILED, 'decoder did not become ready')
    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED
    assert viewer._player_failure_detail == 'decoder did not become ready'


def test_escape_key_converges_on_viewer_close():
    called = []
    viewer = SimpleNamespace(_close=lambda: called.append(True))
    event = SimpleNamespace(key=lambda: Qt.Key.Key_Escape,
                            accept=lambda: called.append('accepted'))

    ClipViewer.keyPressEvent(viewer, event)

    assert called == [True, 'accepted']


def test_failed_player_ignores_late_ready_and_playing_callbacks(qtbot):
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.FAILED,
        _playback_ready=False,
        _media_ready=False,
        _play_when_ready=True,
        _playback_requested_at=0.0,
        _audio_preparation_ready=False,
    )
    viewer._playback_stall_timer = QTimer()
    viewer._playback_stall_timer.setSingleShot(True)
    viewer._playback_status_lbl = SimpleNamespace(setText=lambda *_: None)
    viewer.play_btn = SimpleNamespace(
        blockSignals=lambda *_: None, setChecked=lambda *_: None)
    viewer._set_playback_button_visual = lambda *_: None
    viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
    viewer._fail_playback = ClipViewer._fail_playback.__get__(viewer)
    ClipViewer._on_media_status_changed(
        viewer, QMediaPlayer.MediaStatus.LoadedMedia)
    ClipViewer._on_state_changed(viewer, QMediaPlayer.PlaybackState.PlayingState)

    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED
    assert viewer._playback_ready is False


def test_player_preparation_timeout_is_terminal(qtbot):
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _playback_ready=False,
        _media_ready=False,
        _playback_preparing_at=0.0,
        _play_when_ready=True,
        _playback_requested_at=0.0,
        _audio_preparation_ready=False,
    )
    viewer._playback_stall_timer = QTimer()
    viewer._playback_status_lbl = SimpleNamespace(setText=lambda *_: None)
    viewer.play_btn = SimpleNamespace(
        blockSignals=lambda *_: None, setChecked=lambda *_: None)
    viewer._set_playback_button_visual = lambda *_: None
    viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
    viewer._fail_playback = ClipViewer._fail_playback.__get__(viewer)

    ClipViewer._on_playback_diagnostic_stall(viewer)
    ClipViewer._on_media_status_changed(
        viewer, QMediaPlayer.MediaStatus.LoadedMedia)

    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED
    assert viewer._play_when_ready is False


def test_invalid_media_backend_result_is_terminal(qtbot):
    viewer = SimpleNamespace(
        _closing=False,
        clip_path="clip.mp4", _playback_path="clip.mp4",
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _player_failure_detail='',
        _playback_ready=False,
        _media_ready=False,
        _play_when_ready=True,
    )
    viewer._playback_stall_timer = QTimer()
    viewer._playback_status_lbl = SimpleNamespace(setText=lambda *_: None)
    viewer.play_btn = SimpleNamespace(
        blockSignals=lambda *_: None, setChecked=lambda *_: None)
    viewer._set_playback_button_visual = lambda *_: None
    viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
    viewer._fail_playback = ClipViewer._fail_playback.__get__(viewer)

    ClipViewer._on_media_status_changed(
        viewer, QMediaPlayer.MediaStatus.InvalidMedia)

    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED
    assert viewer._play_when_ready is False
    assert 'InvalidMedia' in viewer._player_failure_detail


def test_loaded_media_without_video_is_terminal(qtbot):
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _player_failure_detail='',
        _playback_ready=False,
        _media_ready=False,
        _play_when_ready=True,
        player=SimpleNamespace(hasVideo=lambda: False),
    )
    viewer._playback_stall_timer = QTimer()
    viewer._playback_status_lbl = SimpleNamespace(setText=lambda *_: None)
    viewer.play_btn = SimpleNamespace(
        blockSignals=lambda *_: None, setChecked=lambda *_: None)
    viewer._set_playback_button_visual = lambda *_: None
    viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
    viewer._fail_playback = ClipViewer._fail_playback.__get__(viewer)

    ClipViewer._on_media_status_changed(
        viewer, QMediaPlayer.MediaStatus.LoadedMedia)

    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED
    assert viewer._media_ready is False
    assert viewer._player_failure_detail == 'Loaded media has no video stream'


def test_media_without_audio_sources_can_finish_audio_preparation():
    ready_updates = []
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _playback_sources=(),
        _audio_tracks=(),
        _multitrack_audio=False,
        _source_volumes={},
        _source_mutes={},
        _native_audio_source_id=None,
        _audio_preparation_ready=False,
        _speed_rate=1.0,
        _preserve_pitch=True,
        _apply_container_audio_volume=lambda: None,
        _update_playback_readiness=lambda: ready_updates.append(True),
        _refresh_audio_mix_panel=lambda: None,
    )

    ClipViewer._on_audio_sources_discovered(viewer, (), '')

    assert viewer._audio_preparation_ready is True
    assert ready_updates == [True]


def test_failed_player_rejects_new_play_command():
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.FAILED,
    )

    ClipViewer._toggle_play(viewer)

    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED


def test_preparation_stall_uses_preparation_start_without_play_request(monkeypatch):
    events = []
    viewer = SimpleNamespace(
        _closing=False,
        _player_lifecycle_state=PlayerLifecycleState.PREPARING,
        _playback_ready=False,
        _media_ready=False,
        _playback_requested_at=0.0,
        _playback_preparing_at=1.0,
        _audio_preparation_ready=False,
    )
    viewer._playback_stall_timer = QTimer()
    viewer._playback_status_lbl = SimpleNamespace(setText=lambda *_: None)
    viewer.play_btn = SimpleNamespace(
        blockSignals=lambda *_: None, setChecked=lambda *_: None)
    viewer._set_playback_button_visual = lambda *_: None
    viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
    viewer._fail_playback = ClipViewer._fail_playback.__get__(viewer)

    monkeypatch.setattr('ui.clip_viewer.emit_event',
                        lambda *_args, **kwargs: events.append(kwargs))
    monkeypatch.setattr('ui.clip_viewer.time.monotonic', lambda: 2.0)
    ClipViewer._on_playback_diagnostic_stall(viewer)

    assert events[0]['elapsed_ms'] == 1000
    assert viewer._player_lifecycle_state is PlayerLifecycleState.FAILED


def test_fifty_teardown_cycles_are_idempotent_and_close_state(qtbot):
    class _Timer:
        def stop(self):
            return None

    class _Player:
        def stop(self):
            return None

        def setVideoOutput(self, *_):
            return None

        def setAudioOutput(self, *_):
            return None

        def setSource(self, *_):
            return None

    class _Device:
        def close(self):
            return None

    for _ in range(50):
        viewer = SimpleNamespace(
            _teardown_complete=False,
            _closing=False,
            _prepare_cancel=threading.Event(),
            _play_when_ready=True,
            _export_cancel=threading.Event(),
            _export_job=None,
            _export_thread=None,
            _timeline_prepare_timer=_Timer(),
            _audio_prepare_deadline=_Timer(),
            _playback_stall_timer=_Timer(),
            trim_slider=SimpleNamespace(cancel_thumbnail_loading=lambda: None),
            _seek_timer=_Timer(), _mix_refresh_timer=_Timer(),
            _preview_update_timer=_Timer(), _draft_save_timer=_Timer(),
            _preview_update_pending=True, _pending_seek_ms=10,
            _ph_timer=_Timer(), player=_Player(),
            _audio_mixer=None, video_widget=SimpleNamespace(shutdown=lambda: None),
            audio_output=SimpleNamespace(setMuted=lambda *_: None),
            _diagnostic_player_counted=False,
            _player_lifecycle_state=PlayerLifecycleState.READY,
            _player_failure_detail='',
        )
        viewer._set_player_state = ClipViewer._set_player_state.__get__(viewer)
        viewer._flush_clip_details = lambda: None
        viewer._teardown_player = ClipViewer._teardown_player.__get__(viewer)
        viewer._teardown_player()
        viewer._teardown_player()

        assert viewer._teardown_complete is True
        assert viewer._player_lifecycle_state is PlayerLifecycleState.CLOSED


def test_renderer_change_keeps_player_identity_and_source():
    calls = []
    player = object()
    viewer = SimpleNamespace(
        _closing=False,
        player=player,
        _update_video_renderer=lambda: calls.append(True),
    )

    assert ClipViewer._recreate_player_for_renderer(
        viewer, 'software', 12_345, True) is True
    assert viewer.player is player
    assert calls == [True]
