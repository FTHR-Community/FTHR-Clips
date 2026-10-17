from __future__ import annotations

from types import SimpleNamespace

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPixmap
from PySide6.QtMultimedia import QMediaPlayer

from ui import clip_viewer
from ui.clip_viewer import (
    ClipViewer,
    LiveVideoPreview,
    _normalise_color_effects,
    _video_filter_chain,
)


class _Settings:
    def get(self, _key, default=None):
        return default

    def set(self, *_args):
        return None

    def save_settings(self):
        return None


def _viewer(monkeypatch, qtbot, tmp_path):
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', lambda _path, cancelled=None: ())
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'not a media file')
    viewer = ClipViewer(str(path), None, settings_manager=_Settings())
    qtbot.addWidget(viewer)
    return viewer


def _image(width=8, height=8):
    image = QImage(width, height, QImage.Format.Format_RGB888)
    image.fill(Qt.GlobalColor.black)
    return image


class _NativeFrame:
    """Small deterministic stand-in for a native sink frame."""

    def __init__(self, timestamp_us):
        self.timestamp_us = timestamp_us

    def isValid(self):
        return True

    def startTime(self):
        return self.timestamp_us

    def handleType(self):
        return 'RhiTextureHandle'

    def surfaceFormat(self):
        return SimpleNamespace(
            pixelFormat=lambda: 'Format_BGRA8888',
            frameSize=lambda: QSize(1920, 1080),
            handleType=lambda: 'RhiTextureHandle',
        )


def _native_timestamp_viewer():
    stats = {
        'frames_received': 0, 'frames_routed_to_software': 0,
        'neutral_native_bypass_frames': 0, 'repeated_timestamps': 0,
        'regressing_timestamps': 0, 'timestamp_discontinuities': 0,
        'invalid_frames': 0, 'source_timestamp_samples': 0,
        'sink_fps': 0.0, 'delivery_fps': 0.0, 'pixel_format': None,
        'dimensions': None, 'handle_type': None,
        'native_presentation_observable': False,
    }
    return SimpleNamespace(
        _native_stats=stats, _native_frame_count=0,
        _native_latest_frame=None, _native_has_real_frame=False,
        _native_last_timestamp_ms=None,
        _native_timestamp_samples=clip_viewer.deque(maxlen=512),
        _native_delivery_samples=clip_viewer.deque(maxlen=512),
        _video_renderer='native',
        _native_video_widget=SimpleNamespace(
            show=lambda: None, hide=lambda: None),
        _native_output_enabled=True,
        _requires_software_video=lambda: False,
        video_widget=SimpleNamespace(
            _on_video_frame=lambda _frame, **_kwargs: None),
        _set_surface_visibility=lambda **_kwargs: None,
    )


def test_effect_updates_keep_the_single_player_output_stable(monkeypatch, qtbot,
                                                              tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    player = viewer.player
    set_source_calls = []
    original_set_source = player.setSource
    monkeypatch.setattr(player, 'setSource',
                        lambda source: set_source_calls.append(source))
    monkeypatch.setattr(
        viewer, '_recreate_player_for_renderer',
        lambda *_args: (_ for _ in ()).throw(
            AssertionError('effect changes must not recreate the player')))

    viewer._effects['contrast'] = 25
    viewer._refresh_live_preview(immediate=True)
    viewer._stretch_ratio = 1.25
    viewer._refresh_live_preview(immediate=True)

    assert viewer._video_renderer == 'software'
    assert viewer.player is player
    assert set_source_calls == []
    monkeypatch.setattr(player, 'setSource', original_set_source)
    viewer._teardown_player()


def test_native_preview_output_is_windows_only():
    assert clip_viewer._use_native_preview_output('win32') is True
    assert clip_viewer._use_native_preview_output('linux') is False
    assert clip_viewer._use_native_preview_output('darwin') is False


def test_renderer_state_changes_do_not_reattach_player_output(monkeypatch, qtbot,
                                                               tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    output_calls = []
    source_calls = []
    monkeypatch.setattr(viewer.player, 'setVideoOutput', output_calls.append)
    monkeypatch.setattr(viewer.player, 'setSource', source_calls.append)
    viewer._effects['contrast'] = 20
    viewer._refresh_live_preview(immediate=True)
    viewer._effects['contrast'] = 0
    viewer._refresh_live_preview(immediate=True)
    assert output_calls == []
    assert source_calls == []
    assert viewer._video_renderer == 'native'
    viewer._teardown_player()


def test_teardown_emits_one_aggregate_render_summary_without_raw_samples(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    events = []
    monkeypatch.setattr(
        clip_viewer, 'emit_event',
        lambda subsystem, event, **fields: events.append(
            (subsystem, event, fields)))
    viewer._teardown_player()
    summaries = [entry for entry in events if entry[1] ==
                 'render_performance_summary']
    assert len(summaries) == 1
    fields = summaries[0][2]
    assert 'recent_sink_timestamps_ms' not in fields
    assert 'recent_delivery_times' not in fields


def test_latest_frame_pipeline_has_one_active_and_one_replaceable_pending(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview.resize(640, 360)
    preview._active_preview_request = {'generation': 1, 'serial': 1}

    first = object()
    second = object()
    preview._latest_video_frame = first
    preview._frame_serial = 1
    preview._enqueue_latest_frame()
    preview._latest_video_frame = second
    preview._frame_serial = 2
    preview._enqueue_latest_frame()
    preview._latest_video_frame = object()
    preview._frame_serial = 3
    preview._enqueue_latest_frame()

    assert preview._pending_preview_request['serial'] == 3
    snapshot = preview.preview_performance_snapshot()
    assert snapshot['active'] is True
    assert snapshot['pending'] is True
    assert snapshot['replaced'] == 2
    assert snapshot['max_queue_depth'] == 2
    preview.shutdown()


def test_neutral_preview_gate_bypasses_cpu_queue_and_keeps_bounded_state(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview._active_preview_request = {'generation': 1, 'serial': 1}
    preview._latest_video_frame = object()
    preview._frame_serial = 1
    preview.set_software_preview_enabled(False)
    preview._enqueue_latest_frame()

    snapshot = preview.preview_performance_snapshot()
    assert snapshot['software_preview_enabled'] is False
    assert snapshot['software_bypass_count'] == 1
    assert snapshot['submitted'] == 0
    assert snapshot['bounded_sample_limit'] == 512
    preview.shutdown()


def test_initial_decoded_image_forces_one_request_when_native_gate_is_disabled(
        qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.set_software_preview_enabled(False)
    started = []
    preview._start_preview_request = started.append

    assert preview.publish_initial_decoded_image(
        preview._source_generation, _image()) is True
    assert len(started) == 1
    assert started[0]['force'] is True
    assert isinstance(started[0]['frame'], QImage)
    preview.shutdown()


def test_native_sink_cadence_and_neutral_bypass_are_observable():
    routed = []
    visibility = []
    native = SimpleNamespace(show=lambda: None, hide=lambda: None)
    viewer = SimpleNamespace(
        _native_stats={
            'frames_received': 0, 'frames_routed_to_software': 0,
            'neutral_native_bypass_frames': 0, 'repeated_timestamps': 0,
            'regressing_timestamps': 0, 'timestamp_discontinuities': 0,
            'invalid_frames': 0,
            'source_timestamp_samples': 0, 'sink_fps': 0.0,
            'delivery_fps': 0.0, 'pixel_format': None, 'dimensions': None,
            'handle_type': None, 'native_presentation_observable': False,
        },
        _native_frame_count=0, _native_latest_frame=None,
        _native_has_real_frame=False, _native_last_timestamp_ms=None,
        _native_timestamp_samples=clip_viewer.deque(maxlen=512),
        _native_delivery_samples=clip_viewer.deque(maxlen=512),
        _video_renderer='native', _native_video_widget=native,
        _native_output_enabled=True,
        _requires_software_video=lambda: False,
        video_widget=SimpleNamespace(
            _on_video_frame=lambda frame, **_kwargs: routed.append(frame),
            hide=lambda: None,
        ),
        _set_surface_visibility=lambda **kwargs: visibility.append(kwargs),
    )
    for index in range(100):
        ClipViewer._on_native_video_frame(viewer, _NativeFrame(index * 8333))

    assert viewer._native_stats['frames_received'] == 100
    assert viewer._native_stats['neutral_native_bypass_frames'] == 100
    assert viewer._native_stats['frames_routed_to_software'] == 0
    assert len(visibility) == 1
    snapshot = ClipViewer.render_performance_snapshot(viewer)
    assert 119.0 <= snapshot['source_fps'] <= 121.0
    assert 119.0 <= snapshot['sink_fps'] <= 121.0
    assert viewer._native_stats['pixel_format'] == 'Format_BGRA8888'
    assert viewer._native_stats['dimensions'] == [1920, 1080]
    assert viewer._native_stats['handle_type'] == 'RhiTextureHandle'


def test_native_source_timestamp_cadences_are_estimated_without_frame_rate_assumptions():
    for expected_fps in (30, 60, 120, 144):
        stats = {
            'frames_received': 0, 'frames_routed_to_software': 0,
            'neutral_native_bypass_frames': 0, 'repeated_timestamps': 0,
            'regressing_timestamps': 0, 'timestamp_discontinuities': 0,
            'invalid_frames': 0,
            'source_timestamp_samples': 0, 'sink_fps': 0.0,
            'delivery_fps': 0.0, 'pixel_format': None, 'dimensions': None,
            'handle_type': None, 'native_presentation_observable': False,
        }
        viewer = SimpleNamespace(
            _native_stats=stats, _native_frame_count=0,
            _native_latest_frame=None, _native_has_real_frame=False,
            _native_last_timestamp_ms=None,
            _native_timestamp_samples=clip_viewer.deque(maxlen=512),
            _native_delivery_samples=clip_viewer.deque(maxlen=512),
            _video_renderer='native',
            _native_video_widget=SimpleNamespace(
                show=lambda: None, hide=lambda: None),
            _native_output_enabled=True,
            _requires_software_video=lambda: False,
            video_widget=SimpleNamespace(
                _on_video_frame=lambda _frame, **_kwargs: None,
                hide=lambda: None),
            _set_surface_visibility=lambda **_kwargs: None,
        )
        interval_us = round(1_000_000 / expected_fps)
        for index in range(24):
            ClipViewer._on_native_video_frame(
                viewer, _NativeFrame(index * interval_us))
        snapshot = ClipViewer.render_performance_snapshot(viewer)
        assert abs(snapshot['source_fps'] - expected_fps) < 1.0


def test_native_timestamp_backward_seek_reseeds_120fps_cadence_window():
    viewer = _native_timestamp_viewer()
    interval_us = round(1_000_000 / 120)
    for index in range(24):
        ClipViewer._on_native_video_frame(
            viewer, _NativeFrame(index * interval_us))

    ClipViewer._on_native_video_frame(viewer, _NativeFrame(50_000))
    assert viewer._native_stats['regressing_timestamps'] == 1
    assert viewer._native_stats['timestamp_discontinuities'] == 1
    assert list(viewer._native_timestamp_samples) == [50.0]

    for index in range(1, 24):
        ClipViewer._on_native_video_frame(
            viewer, _NativeFrame(50_000 + index * interval_us))

    snapshot = ClipViewer.render_performance_snapshot(viewer)
    assert abs(snapshot['source_fps'] - 120.0) < 1.0


def test_native_timestamp_large_forward_seek_reseeds_144fps_cadence_window():
    viewer = _native_timestamp_viewer()
    interval_us = round(1_000_000 / 144)
    for index in range(24):
        ClipViewer._on_native_video_frame(
            viewer, _NativeFrame(index * interval_us))

    seek_start_us = 5_000_000
    ClipViewer._on_native_video_frame(viewer, _NativeFrame(seek_start_us))
    assert viewer._native_stats['regressing_timestamps'] == 0
    assert viewer._native_stats['timestamp_discontinuities'] == 1
    assert list(viewer._native_timestamp_samples) == [5_000.0]

    for index in range(1, 24):
        ClipViewer._on_native_video_frame(
            viewer, _NativeFrame(seek_start_us + index * interval_us))

    snapshot = ClipViewer.render_performance_snapshot(viewer)
    assert abs(snapshot['source_fps'] - 144.0) < 1.0


def test_native_sink_routes_frames_when_edits_need_software_preview():
    routed = []
    native = SimpleNamespace(show=lambda: None, hide=lambda: None)
    viewer = SimpleNamespace(
        _native_stats={
            'frames_received': 0, 'frames_routed_to_software': 0,
            'neutral_native_bypass_frames': 0, 'repeated_timestamps': 0,
            'regressing_timestamps': 0, 'timestamp_discontinuities': 0,
            'invalid_frames': 0,
            'source_timestamp_samples': 0, 'sink_fps': 0.0,
            'delivery_fps': 0.0, 'pixel_format': None, 'dimensions': None,
            'handle_type': None, 'native_presentation_observable': False,
        },
        _native_frame_count=0, _native_latest_frame=None,
        _native_has_real_frame=False, _native_last_timestamp_ms=None,
        _native_timestamp_samples=clip_viewer.deque(maxlen=512),
        _native_delivery_samples=clip_viewer.deque(maxlen=512),
        _video_renderer='software', _native_video_widget=native,
        _native_output_enabled=True,
        _requires_software_video=lambda: True,
        video_widget=SimpleNamespace(
            _on_video_frame=lambda frame, **_kwargs: routed.append(frame)),
    )
    ClipViewer._on_native_video_frame(viewer, _NativeFrame(1000))
    assert viewer._native_stats['frames_routed_to_software'] == 1
    assert viewer._native_stats['neutral_native_bypass_frames'] == 0
    assert len(routed) == 1


def test_preview_conversion_and_processing_metrics_are_separate(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview._active_preview_request = {'generation': 0, 'serial': 1}
    preview._preview_generation = 0
    preview._on_preview_result(
        0, 1, _image(), 7.0, None,
        {'conversion_ms': 3.0, 'processing_ms': 4.0})
    snapshot = preview.preview_performance_snapshot()
    assert snapshot['conversion_count'] == 1
    assert snapshot['conversion_avg_ms'] == 3.0
    assert snapshot['processing_count'] == 1
    assert snapshot['processing_avg_ms'] == 4.0
    preview.set_playback_clock(1234)
    preview.set_latest_video_pts(1250)
    snapshot = preview.preview_performance_snapshot()
    assert snapshot['playback_clock_ms'] == 1234.0
    assert snapshot['video_pts_vs_playback_clock_ms'] == 16.0
    preview.shutdown()


def test_frame_age_starts_at_sink_receipt_not_worker_completion(monkeypatch, qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.resize(160, 90)
    preview._active_preview_request = {'generation': 0, 'serial': 1}
    preview._preview_generation = 0
    monkeypatch.setattr(clip_viewer.time, 'monotonic', lambda: 100.03)
    preview._on_preview_result(
        0, 1, _image(160, 90), 20.0, None,
        {
            'conversion_ms': 0.0,
            'processing_ms': 0.0,
            'conversion_complete': True,
            'processing_complete': True,
            'received_at': 100.0,
            'source_pts_ms': 90.0,
        })
    preview.render(QImage(160, 90, QImage.Format.Format_RGB888))
    snapshot = preview.preview_performance_snapshot()
    assert snapshot['frame_age_avg_ms'] == 30.0
    assert snapshot['conversion_count'] == 1
    assert snapshot['processing_count'] == 1
    preview.shutdown()


def test_native_timestamp_samples_are_bounded():
    viewer = SimpleNamespace(
        _native_stats={
            'frames_received': 0, 'frames_routed_to_software': 0,
            'neutral_native_bypass_frames': 0, 'repeated_timestamps': 0,
            'regressing_timestamps': 0, 'timestamp_discontinuities': 0,
            'invalid_frames': 0,
            'source_timestamp_samples': 0, 'sink_fps': 0.0,
            'delivery_fps': 0.0, 'pixel_format': None, 'dimensions': None,
            'handle_type': None, 'native_presentation_observable': False,
        },
        _native_frame_count=0, _native_latest_frame=None,
        _native_has_real_frame=False, _native_last_timestamp_ms=None,
        _native_timestamp_samples=clip_viewer.deque(maxlen=512),
        _native_delivery_samples=clip_viewer.deque(maxlen=512),
        _video_renderer='native',
        _native_video_widget=SimpleNamespace(show=lambda: None, hide=lambda: None),
        _native_output_enabled=True,
        _requires_software_video=lambda: False,
        video_widget=SimpleNamespace(
            _on_video_frame=lambda _frame, **_kwargs: None),
        _set_surface_visibility=lambda **_kwargs: None,
    )
    for index in range(700):
        ClipViewer._on_native_video_frame(viewer, _NativeFrame(index * 8333))
    assert len(viewer._native_timestamp_samples) <= 512
    assert len(viewer._native_delivery_samples) <= 512


def test_video_sink_callback_defers_to_image_conversion(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.show()
    submitted = []

    class _Frame:
        def isValid(self):
            return True

        def toImage(self):
            raise AssertionError('toImage must run in the worker, not callback')

    preview._start_preview_request = submitted.append
    preview._on_video_frame(_Frame())

    assert len(submitted) == 1
    assert submitted[0]['serial'] == 1
    preview.shutdown()


def test_paint_event_only_draws_prepared_image(monkeypatch, qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.resize(160, 90)
    preview._has_real_frame = True
    preview._prepared_frame = _image(160, 90)

    def fail_if_processed(*_args, **_kwargs):
        raise AssertionError('paintEvent must not run the color processor')

    monkeypatch.setattr(LiveVideoPreview, '_apply_color_effects',
                        staticmethod(fail_if_processed))
    rendered = QImage(160, 90, QImage.Format.Format_RGB888)
    preview.render(rendered)
    preview.shutdown()


def test_stale_preview_generation_cannot_replace_current_frame(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview._active_preview_request = {'generation': 4, 'serial': 7}
    preview._preview_generation = 5
    preview._frame_serial = 8
    preview._prepared_frame = _image(4, 4)
    old = preview._prepared_frame.cacheKey()

    preview._on_preview_result(4, 7, _image(8, 8), 2.0, None)

    assert preview._prepared_frame.cacheKey() == old
    assert preview.preview_performance_snapshot()['stale_results'] == 1
    preview.shutdown()


def test_current_generation_frame_is_displayed_while_newer_frame_waits(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview._active_preview_request = {'generation': 3, 'serial': 10}
    preview._preview_generation = 3
    preview._frame_serial = 11
    pending = {'generation': 3, 'serial': 11, 'frame': object()}
    preview._pending_preview_request = pending
    started = []
    preview._start_preview_request = started.append
    previous = _image(4, 4)
    preview._prepared_frame = previous

    current = _image(8, 8)
    preview._on_preview_result(3, 10, current, 2.0, None)

    assert preview._prepared_frame.cacheKey() == current.cacheKey()
    assert started == []
    assert preview._pending_preview_request is pending
    assert preview.preview_performance_snapshot()['stale_results'] == 0
    preview.shutdown()


def test_initial_frame_request_is_once_per_generation_and_shutdown_is_safe(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    assert preview.request_initial_frame() is True
    assert preview.request_initial_frame() is False
    preview.begin_source_generation()
    assert preview.request_initial_frame() is True
    preview.shutdown()
    assert preview.request_initial_frame() is False


def test_initial_decode_starts_once_without_touching_player(monkeypatch):
    started = []
    constructed = []
    requests = iter((True, False))

    class _Signal:
        def connect(self, slot):
            self.slot = slot

    class _Worker:
        def __init__(self, path, generation, cancelled):
            constructed.append((path, generation, cancelled))
            self.signals = SimpleNamespace(ready=_Signal())

    class _Pool:
        @staticmethod
        def globalInstance():
            return SimpleNamespace(start=started.append)

    monkeypatch.setattr(clip_viewer, '_InitialFrameDecodeWorker', _Worker)
    monkeypatch.setattr(clip_viewer, '_frame_decode_pool', _Pool.globalInstance)
    player_events = []
    player = SimpleNamespace(
        playbackState=lambda: QMediaPlayer.PlaybackState.StoppedState,
        pause=lambda: player_events.append('pause'),
        setPosition=lambda value: player_events.append(('seek', value)))
    cancel = object()
    viewer = SimpleNamespace(
        _connect_clip_result=lambda signal, callback: signal.connect(callback),
        _closing=False, _media_ready=True, _playback_path='clip.mp4',
        _prepare_cancel=cancel, player=player,
        _on_initial_decoded_frame=lambda _result: None,
        video_widget=SimpleNamespace(
            _source_generation=7,
            request_initial_frame=lambda: next(requests)))

    ClipViewer._request_initial_preview_frame(viewer)
    ClipViewer._request_initial_preview_frame(viewer)

    assert constructed == [('clip.mp4', 7, cancel)]
    assert started == [viewer._initial_frame_worker]
    assert player_events == []


def test_initial_frame_worker_decodes_one_owned_image_and_honours_cancel(
        monkeypatch, qtbot):
    released = []
    opened = []

    class _Capture:
        def __init__(self, path):
            opened.append(path)

        def read(self):
            return True, clip_viewer.np.zeros((9, 16, 3), dtype='uint8')

        def release(self):
            released.append(True)

    monkeypatch.setattr(clip_viewer.cv2, 'VideoCapture', _Capture)
    results = []
    worker = clip_viewer._InitialFrameDecodeWorker(
        'clip.mp4', 3, SimpleNamespace(is_set=lambda: False))
    worker.signals.ready.connect(results.append)
    worker.run()

    assert opened == ['clip.mp4']
    assert released == [True]
    assert len(results) == 1
    generation, image, error = results[0]
    assert generation == 3
    assert image.size() == QSize(16, 9)
    assert error is None

    cancelled_results = []
    cancelled = clip_viewer._InitialFrameDecodeWorker(
        'never-opened.mp4', 4, SimpleNamespace(is_set=lambda: True))
    cancelled.signals.ready.connect(cancelled_results.append)
    cancelled.run()
    assert opened == ['clip.mp4']
    assert cancelled_results == []


def test_initial_decode_is_not_requested_during_active_playback():
    requests = []
    viewer = SimpleNamespace(
        _closing=False, _media_ready=True,
        player=SimpleNamespace(
            playbackState=lambda: QMediaPlayer.PlaybackState.PlayingState),
        video_widget=SimpleNamespace(
            request_initial_frame=lambda: requests.append(True)))

    ClipViewer._request_initial_preview_frame(viewer)

    assert requests == []


def test_initial_decoded_image_is_source_matched_and_uses_bounded_pipeline(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.resize(160, 90)
    started = []
    preview._start_preview_request = started.append
    generation = preview._source_generation

    assert preview.publish_initial_decoded_image(generation + 1, _image()) is False
    assert preview.publish_initial_decoded_image(generation, _image()) is True
    assert len(started) == 1
    assert isinstance(started[0]['frame'], QImage)
    assert started[0]['generation'] == preview._preview_generation
    preview.shutdown()


def test_initial_decode_result_rejects_stale_playing_and_failed_results():
    published = []
    state = [QMediaPlayer.PlaybackState.StoppedState]
    video = SimpleNamespace(
        _source_generation=4,
        preview_performance_snapshot=lambda: {'has_real_frame': False},
        publish_initial_decoded_image=lambda generation, image:
            published.append((generation, image)))
    viewer = SimpleNamespace(
        _closing=False, video_widget=video,
        player=SimpleNamespace(playbackState=lambda: state[0]))

    ClipViewer._on_initial_decoded_frame(viewer, (3, _image(), None))
    state[0] = QMediaPlayer.PlaybackState.PlayingState
    ClipViewer._on_initial_decoded_frame(viewer, (4, _image(), None))
    state[0] = QMediaPlayer.PlaybackState.StoppedState
    ClipViewer._on_initial_decoded_frame(
        viewer, (4, QImage(), 'decode failed'))
    assert published == []

    image = _image()
    ClipViewer._on_initial_decoded_frame(viewer, (4, image, None))
    assert published == [(4, image)]


def test_invalid_media_keeps_poster_and_does_not_claim_a_real_frame(qtbot):
    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    poster = QPixmap(16, 9)
    poster.fill(Qt.GlobalColor.darkGray)
    preview.set_poster(poster)
    preview.begin_source_generation()

    assert preview.preview_performance_snapshot()['has_real_frame'] is False
    assert not preview._poster_image.isNull()
    preview.shutdown()


def test_fullscreen_button_escape_and_f11_preserve_editor_state(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    viewer._effects['contrast'] = 33
    viewer.audio_output.setVolume(0.42)
    viewer.audio_output.setMuted(True)
    viewer.player.setPosition(123)
    before = (
        viewer.player.playbackState(), viewer.player.position(),
        viewer.audio_output.volume(), viewer.audio_output.isMuted(),
        dict(viewer._effects), viewer.clip_path)
    viewer._toggle_fullscreen()
    assert viewer.isFullScreen()
    assert (
        viewer.player.playbackState(), viewer.player.position(),
        viewer.audio_output.volume(), viewer.audio_output.isMuted(),
        dict(viewer._effects), viewer.clip_path) == before

    viewer._handle_escape_shortcut()
    assert not viewer.isFullScreen()
    viewer._handle_f11_shortcut()
    assert viewer.isFullScreen()
    viewer._handle_f11_shortcut()
    assert not viewer.isFullScreen()
    viewer._teardown_player()


def test_fullscreen_restores_normal_geometry_and_controls_do_not_toggle(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    viewer.setGeometry(QRect(40, 50, 1000, 700))
    expected = QRect(viewer.geometry())
    viewer._toggle_fullscreen()
    viewer.play_btn.click()
    assert viewer.isFullScreen()
    viewer._exit_fullscreen()
    assert not viewer.isFullScreen()
    assert viewer.geometry() == expected
    viewer._teardown_player()


def test_fullscreen_restores_maximized_state(monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    viewer.showMaximized()
    qtbot.wait(20)
    if not viewer.isMaximized():
        viewer._teardown_player()
        return
    viewer._toggle_fullscreen()
    assert viewer.isFullScreen()
    viewer._exit_fullscreen()
    assert viewer.isMaximized()
    viewer._teardown_player()


def test_video_double_click_only_is_handled_by_video_surface(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    toggles = []
    viewer._toggle_fullscreen = lambda: toggles.append(True)
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick, viewer.video_widget.rect().center(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    assert viewer.eventFilter(viewer.video_widget, event) is True
    assert toggles == [True]
    assert viewer.eventFilter(viewer.play_btn,
                              QEvent(QEvent.Type.MouseButtonDblClick)) is False
    assert toggles == [True]
    native_event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick,
        viewer._native_video_widget.rect().center(),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    assert viewer.eventFilter(viewer._native_video_widget, native_event) is True
    assert toggles == [True, True]
    viewer._teardown_player()


def test_aspect_ratio_policy_and_stretch_remain_deterministic(qtbot):
    preview = LiveVideoPreview(1920, 1080)
    qtbot.addWidget(preview)
    preview.resize(400, 400)
    assert preview._video_display_rect().size() == QSize(400, 225)
    preview.set_edit_state(None, {}, 0.5)
    assert preview._video_display_rect().size() == QSize(355, 400)
    preview.shutdown()


def test_color_state_normalization_is_shared_by_preview_and_export(qtbot):
    raw = {
        'exposure': -999,
        'contrast': 999,
        'saturation': '40',
        'temperature': None,
        'sharpness': -4,
        'vignette': 999,
    }
    normalized = _normalise_color_effects(raw)
    assert normalized == {
        'exposure': -100, 'contrast': 100, 'saturation': 40,
        'temperature': 0, 'sharpness': 0, 'vignette': 100,
    }
    assert _normalise_color_effects(raw) == normalized
    assert _video_filter_chain(effects=raw) == _video_filter_chain(
        effects=normalized)

    preview = LiveVideoPreview(16, 9)
    qtbot.addWidget(preview)
    preview.set_edit_state(None, raw, 1.0)
    assert preview._effects == normalized
    preview.set_edit_state(None, {}, 1.0)
    assert preview._effects == _normalise_color_effects({})
    preview.shutdown()


def test_rapid_effect_changes_never_reload_media_or_construct_a_player(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, tmp_path)
    source_calls = []
    monkeypatch.setattr(viewer.player, 'setSource', source_calls.append)

    def unexpected_player(*_args, **_kwargs):
        raise AssertionError('effect changes must not construct a player')

    monkeypatch.setattr(clip_viewer, 'QMediaPlayer', unexpected_player)
    preview = viewer.video_widget
    preview._active_preview_request = {'generation': 1, 'serial': 1}
    preview._latest_video_frame = object()
    for value in range(-100, 101, 10):
        preview.set_edit_state(None, {'contrast': value}, 1.0)

    snapshot = preview.preview_performance_snapshot()
    assert source_calls == []
    assert snapshot['pending'] is True
    assert snapshot['max_queue_depth'] <= 2
    viewer._teardown_player()
