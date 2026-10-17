from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QMediaPlayer

from ui.clip_viewer import ClipViewer


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        if slot in self.slots:
            self.slots.remove(slot)


class _PoisonFrame:
    def __init__(self):
        self.validity_checks = 0

    def isValid(self):
        self.validity_checks += 1
        raise AssertionError('a stale source callback must be rejected first')


class _DelayedPlayer:
    def __init__(self, position=100):
        self._position = position
        self.seek_requests = []

    def playbackState(self):
        return QMediaPlayer.PlaybackState.PausedState

    def position(self):
        return self._position

    def setPosition(self, position):
        # Simulate a backend that acknowledges the seek asynchronously.
        self.seek_requests.append(position)


def test_old_native_signal_closure_is_ignored_after_source_generation_changes():
    signal = _Signal()
    viewer = SimpleNamespace(
        _native_output_enabled=True,
        _native_video_widget=SimpleNamespace(videoSink=lambda: SimpleNamespace(
            videoFrameChanged=signal)),
        video_widget=SimpleNamespace(_source_generation=1),
        _native_frame_callback=None,
    )

    ClipViewer._bind_native_video_frame_callback(viewer, 1)
    old_callback = signal.slots[-1]
    viewer.video_widget._source_generation = 2
    ClipViewer._bind_native_video_frame_callback(viewer, 2)

    frame = _PoisonFrame()
    old_callback(frame)

    assert frame.validity_checks == 0
    assert len(signal.slots) == 1


def test_flushed_seek_anchor_survives_async_source_swap(monkeypatch):
    player = _DelayedPlayer(position=1200)
    viewer = SimpleNamespace(
        _closing=False,
        player=player,
        _pending_seek_ms=9000,
        _audio_mixer=None,
        video_widget=SimpleNamespace(_source_generation=1),
        _seek_timer=SimpleNamespace(stop=lambda: None),
        _prepared_playback_path='prepared.mp4',
        _playback_path='original.mp4',
        _media_ready=True,
        _playback_ready=True,
        _renderer_resume_position_ms=None,
        _set_player_state=lambda *_args: None,
        _set_media_source=lambda: None,
    )
    monkeypatch.setattr('ui.clip_viewer.emit_event', lambda *_args, **_kwargs: None)

    ClipViewer._flush_pending_seek(viewer)
    assert player.seek_requests == [9000]
    assert viewer._pending_seek_ms is None

    ClipViewer._apply_prepared_playback_source(viewer)

    assert viewer._renderer_resume_position_ms == 9000
    assert viewer._playback_path == 'prepared.mp4'


def test_paused_obsolete_position_callback_does_not_move_the_seek_ui():
    displayed_positions = []
    clock_updates = []
    viewer = SimpleNamespace(
        _closing=False,
        player=_DelayedPlayer(position=300),
        _seek_anchor_ms=8000,
        _seek_anchor_source_generation=4,
        _seek_anchor_deadline=10**20,
        video_widget=SimpleNamespace(
            _source_generation=4,
            set_playback_clock=clock_updates.append),
        duration_ms=10000,
        trim_slider=SimpleNamespace(
            dragging=None,
            set_playhead=displayed_positions.append),
        time_label=SimpleNamespace(setText=lambda value: displayed_positions.append(value)),
        _fmt=lambda value: str(value),
        _resume_anchor_ms=None,
        _resume_guard_deadline=0,
        _last_stable_position_ms=8000,
    )

    ClipViewer._on_player_position_changed(viewer, 300)

    assert clock_updates == []
    assert displayed_positions == []
    assert viewer._last_stable_position_ms == 8000


def test_late_frame_zero_decode_is_rejected_after_a_newer_seek(monkeypatch):
    class _Worker:
        def __init__(self, *_args):
            self.signals = SimpleNamespace(ready=_Signal())

    started = []

    class _Pool:
        @staticmethod
        def globalInstance():
            return SimpleNamespace(start=started.append)

    monkeypatch.setattr('ui.clip_viewer._InitialFrameDecodeWorker', _Worker)
    monkeypatch.setattr('ui.clip_viewer._frame_decode_pool', _Pool.globalInstance)
    published = []
    video = SimpleNamespace(
        _source_generation=7,
        request_initial_frame=lambda: True,
        preview_performance_snapshot=lambda: {'has_real_frame': False},
        publish_initial_decoded_image=lambda generation, image:
            published.append((generation, image)),
    )
    viewer = SimpleNamespace(
        _connect_clip_result=lambda signal, callback: signal.connect(callback),
        _closing=False,
        _media_ready=True,
        _playback_path='clip.mp4',
        _prepare_cancel=object(),
        _seek_generation=3,
        _native_output_enabled=False,
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.StoppedState),
        video_widget=video,
    )
    viewer._on_initial_decoded_frame = (
        lambda result, seek_generation=None:
            ClipViewer._on_initial_decoded_frame(
                viewer, result, seek_generation=seek_generation))

    ClipViewer._request_initial_preview_frame(viewer)
    ready = started[0].signals.ready
    viewer._seek_generation = 4
    image = object()
    ready.slots[0]((7, image, None))

    assert published == []


def test_initial_frame_helper_is_skipped_when_native_sink_already_has_frame():
    requests = []
    viewer = SimpleNamespace(
        _closing=False,
        _media_ready=True,
        _native_output_enabled=True,
        _native_has_real_frame=True,
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.StoppedState),
        video_widget=SimpleNamespace(
            request_initial_frame=lambda: requests.append(True)),
    )

    ClipViewer._request_initial_preview_frame(viewer)

    assert requests == []


def test_initial_frame_helper_is_skipped_for_durable_paused_seek_after_guard():
    requests = []
    viewer = SimpleNamespace(
        _closing=False,
        _media_ready=True,
        _native_output_enabled=True,
        _native_has_real_frame=False,
        _latest_paused_seek_ms=8000,
        _seek_anchor_ms=None,
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.StoppedState),
        video_widget=SimpleNamespace(
            request_initial_frame=lambda: requests.append(True),
            _source_generation=1),
    )

    ClipViewer._request_initial_preview_frame(viewer)

    assert requests == []


def test_expired_guard_does_not_replace_durable_seek_during_source_preparation(
        monkeypatch):
    player = _DelayedPlayer(position=1200)
    viewer = SimpleNamespace(
        _closing=False,
        player=player,
        _pending_seek_ms=None,
        _latest_paused_seek_ms=9000,
        _seek_anchor_ms=9000,
        _seek_anchor_source_generation=1,
        _seek_anchor_deadline=0.0,
        video_widget=SimpleNamespace(_source_generation=1),
        _seek_timer=SimpleNamespace(stop=lambda: None),
        _prepared_playback_path='prepared.mp4',
        _playback_path='original.mp4',
        _media_ready=True,
        _playback_ready=True,
        _renderer_resume_position_ms=None,
        _set_player_state=lambda *_args: None,
        _set_media_source=lambda: None,
    )

    assert ClipViewer._seek_anchor_is_active(viewer) is False
    ClipViewer._apply_prepared_playback_source(viewer)

    assert viewer._latest_paused_seek_ms == 9000
    assert viewer._renderer_resume_position_ms == 9000
    assert viewer._playback_path == 'prepared.mp4'


def test_matching_native_frame_before_position_ack_keeps_durable_seek():
    class _Frame:
        @staticmethod
        def isValid():
            return True

        @staticmethod
        def startTime():
            return 9_000_000

    player = _DelayedPlayer(position=1200)
    player.playbackState = lambda: QMediaPlayer.PlaybackState.PausedState
    viewer = SimpleNamespace(
        _closing=False,
        _native_output_enabled=True,
        _native_stats={
            'invalid_frames': 0,
            'frames_received': 0,
            'frames_routed_to_software': 0,
            'neutral_native_bypass_frames': 0,
            'repeated_timestamps': 0,
            'regressing_timestamps': 0,
            'timestamp_discontinuities': 0,
            'source_timestamp_samples': 0,
        },
        _native_frame_count=0,
        _native_has_real_frame=False,
        _native_latest_received_at=None,
        _native_delivery_samples=[],
        _native_latest_frame=None,
        _native_last_timestamp_ms=None,
        _native_timestamp_samples=[],
        _native_metadata_sampled=True,
        _video_renderer='native',
        _native_surface_visible=False,
        _native_frame_callback=None,
        _seek_generation=4,
        _seek_anchor_ms=9000,
        _seek_anchor_source_generation=1,
        _seek_anchor_deadline=10**20,
        _seek_position_acknowledged=False,
        _seek_frame_acknowledged=False,
        _latest_paused_seek_ms=9000,
        _paused_seek_fallback_visible=False,
        _paused_seek_fallback_timer=SimpleNamespace(stop=lambda: None),
        _pending_paused_seek_decode=None,
        _set_surface_visibility=lambda **_kwargs: None,
        _requires_software_video=lambda: False,
        player=player,
        video_widget=SimpleNamespace(
            _source_generation=1,
            set_latest_video_pts=lambda _pts: None),
    )
    ClipViewer._on_native_video_frame(
        viewer, _Frame(), source_generation=1)

    assert viewer._seek_frame_acknowledged is True
    assert viewer._seek_position_acknowledged is False
    assert viewer._latest_paused_seek_ms == 9000
    assert viewer._native_stats['frames_received'] == 1


def test_seek_invalidates_inflight_preview_conversion_but_keeps_poster(qtbot):
    from ui.clip_viewer import LiveVideoPreview

    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    old_poster = QImage(4, 4, QImage.Format.Format_RGB32)
    preview._prepared_frame = old_poster
    preview._active_preview_request = {'generation': 0, 'serial': 1}
    preview._preview_generation = 0

    preview.begin_seek_generation(1)
    preview._on_preview_result(0, 1, QImage(8, 8, QImage.Format.Format_RGB32),
                               1.0, None)

    assert preview._prepared_frame.cacheKey() == old_poster.cacheKey()
    assert preview.preview_performance_snapshot()['stale_results'] == 1
    preview.shutdown()


def test_paused_seek_fallback_decodes_only_latest_and_never_plays(
        monkeypatch):
    started = []

    class _Worker:
        def __init__(self, path, source_generation, seek_generation,
                     target_ms, cancelled):
            self.args = (path, source_generation, seek_generation,
                         target_ms, cancelled)
            self.signals = SimpleNamespace(ready=_Signal())

    class _Pool:
        @staticmethod
        def globalInstance():
            return SimpleNamespace(start=started.append)

    monkeypatch.setattr('ui.clip_viewer._PausedSeekFrameDecodeWorker', _Worker)
    monkeypatch.setattr('ui.clip_viewer._frame_decode_pool', _Pool.globalInstance)
    player = _DelayedPlayer(position=0)
    player.play = lambda: (_ for _ in ()).throw(
        AssertionError('paused seek fallback must not start playback'))
    viewer = SimpleNamespace(
        _connect_clip_result=lambda signal, callback: signal.connect(callback),
        _closing=False,
        _media_ready=True,
        _native_output_enabled=True,
        _video_renderer='native',
        _latest_paused_seek_ms=9000,
        _seek_frame_acknowledged=False,
        _seek_generation=2,
        _playback_path='clip.mp4',
        _prepare_cancel=object(),
        player=player,
        video_widget=SimpleNamespace(_source_generation=5),
        _paused_seek_decode_worker=None,
        _pending_paused_seek_decode=None,
    )
    viewer._start_paused_seek_decode = (
        lambda request: ClipViewer._start_paused_seek_decode(viewer, request))
    viewer._on_paused_seek_frame_decoded = (
        lambda result: ClipViewer._on_paused_seek_frame_decoded(viewer, result))

    ClipViewer._on_paused_seek_fallback_timeout(viewer)

    assert len(started) == 1
    assert started[0].args[:4] == ('clip.mp4', 5, 2, 9000)
    assert viewer._paused_seek_decode_worker is started[0]

    viewer._seek_generation = 3
    viewer._latest_paused_seek_ms = 12000
    ClipViewer._on_paused_seek_fallback_timeout(viewer)
    assert viewer._pending_paused_seek_decode == (5, 3, 12000, 'clip.mp4')

    player.playbackState = lambda: QMediaPlayer.PlaybackState.PlayingState
    ClipViewer._on_paused_seek_fallback_timeout(viewer)
    assert len(started) == 1
    assert viewer._pending_paused_seek_decode == (5, 3, 12000, 'clip.mp4')


def test_paused_seek_decode_result_publishes_only_matching_epoch(qtbot):
    published = []
    visibility = []
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    video = SimpleNamespace(
        _source_generation=3,
        publish_seek_decoded_image=lambda *args: published.append(args) or True,
    )
    player = _DelayedPlayer(position=0)
    viewer = SimpleNamespace(
        _closing=False,
        _seek_generation=7,
        _latest_paused_seek_ms=4000,
        _seek_frame_acknowledged=False,
        _seek_position_acknowledged=False,
        _seek_anchor_ms=4000,
        _seek_anchor_source_generation=3,
        _seek_anchor_deadline=10**20,
        _paused_seek_decode_worker=object(),
        _pending_paused_seek_decode=None,
        _paused_seek_fallback_visible=False,
        _set_surface_visibility=lambda **kwargs: visibility.append(kwargs),
        player=player,
        video_widget=video,
    )

    ClipViewer._on_paused_seek_frame_decoded(
        viewer, (2, 7, 4000, image, None))
    assert published == []

    viewer._paused_seek_decode_worker = object()
    ClipViewer._on_paused_seek_frame_decoded(
        viewer, (3, 7, 4000, image, None))

    assert published == [(3, 7, 4000, image)]
    assert visibility == [{'native_visible': False}]
    assert viewer._paused_seek_fallback_visible is True
    assert viewer._seek_frame_acknowledged is True
    assert viewer._seek_position_acknowledged is False


def test_playback_position_resumption_clears_paused_seek_state():
    clock_updates = []
    viewer = SimpleNamespace(
        _closing=False,
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.PlayingState),
        _seek_anchor_ms=7000,
        _seek_anchor_source_generation=2,
        _seek_anchor_deadline=10**20,
        _latest_paused_seek_ms=7000,
        _paused_seek_fallback_visible=True,
        _paused_seek_fallback_timer=SimpleNamespace(stop=lambda: None),
        video_widget=SimpleNamespace(set_playback_clock=clock_updates.append),
    )

    ClipViewer._on_player_position_changed(viewer, 7050)

    assert viewer._seek_anchor_ms is None
    assert viewer._latest_paused_seek_ms is None
    assert viewer._paused_seek_fallback_visible is False
    assert clock_updates == [7050]
