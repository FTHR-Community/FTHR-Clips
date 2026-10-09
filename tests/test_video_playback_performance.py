from __future__ import annotations

import time
from types import SimpleNamespace

import cv2
import numpy as np
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer

import core.camera_recorder as camera_recorder
import ui.camera_overlay_editor as overlay_editor
import ui.clip_viewer as clip_viewer
from core.playback_mix_model import PlaybackSource
from main import _SettingsPage
from ui.camera_overlay_editor import UnifiedOverlayPreview
from ui.clip_viewer import ClipViewer, LiveVideoPreview, TrimSlider


class _Settings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, _key, default=None):
        return self.values.get(_key, default)

    def set(self, *_args):
        return None

    def save_settings(self):
        return None


class _DraftManager:
    def __init__(self):
        self.draft = None
        self.writes = 0

    def get(self, _path):
        return {'tag': '', 'description': ''}

    def get_editor_draft(self, _path):
        return self.draft

    def set_editor_draft(self, _path, state):
        self.draft = state
        self.writes += 1
        return True


def _viewer(monkeypatch, qtbot, path: str, metadata_manager=None, settings=None) -> ClipViewer:
    monkeypatch.setattr(clip_viewer, 'discover_playback_sources', lambda _path: ())
    viewer = ClipViewer(
        path, None, settings_manager=settings or _Settings(),
        metadata_manager=metadata_manager)
    qtbot.addWidget(viewer)
    return viewer


def test_viewer_reads_autoplay_setting(monkeypatch, qtbot, tmp_path):
    viewer = _viewer(
        monkeypatch,
        qtbot,
        str(tmp_path / 'clip.mp4'),
        settings=_Settings({'clip_viewer_autoplay': False}),
    )

    assert viewer._play_when_ready is False
    viewer._teardown_player()


def test_playback_has_one_ui_loop_and_filmstrip_is_idle_only(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))
    timer_identity = id(viewer._ph_timer)

    viewer._schedule_timeline_thumbnails()
    assert viewer._timeline_prepare_timer.isActive()
    viewer._on_state_changed(QMediaPlayer.PlaybackState.PlayingState)
    viewer._on_state_changed(QMediaPlayer.PlaybackState.PlayingState)

    assert id(viewer._ph_timer) == timer_identity
    assert viewer._ph_timer.isActive()
    assert not viewer._timeline_prepare_timer.isActive()
    assert viewer.trim_slider._thumbnail_worker_starts == 0

    viewer._on_state_changed(QMediaPlayer.PlaybackState.PausedState)
    assert not viewer._ph_timer.isActive()
    assert viewer._timeline_prepare_timer.interval() == 1500
    viewer._teardown_player()


def test_video_frame_hotpath_keeps_owned_image_without_eager_normalization(
        monkeypatch):
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    updated = []
    frame = SimpleNamespace(
        isValid=lambda: True,
        toImage=lambda: image,
    )
    viewer = SimpleNamespace(
        _frame_image=QImage(),
        _frame_serial=0,
        _cached_key=('old',),
        _cached_frame=QImage(2, 2, QImage.Format.Format_ARGB32),
        update=lambda: updated.append(True),
    )
    viewer._invalidate_processed_frame = (
        LiveVideoPreview._invalidate_processed_frame.__get__(viewer))

    def _unexpected_normalization(*_args):
        raise AssertionError('neutral frames must bypass eager conversion')

    monkeypatch.setattr(
        LiveVideoPreview, '_normalize_decoded_video_range',
        staticmethod(_unexpected_normalization))

    LiveVideoPreview._on_video_frame(viewer, frame)

    assert viewer._frame_image is image
    assert viewer._frame_serial == 1
    assert viewer._cached_key is None
    assert updated == [True]


def test_playing_button_uses_contrasting_pause_icon(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))

    viewer._on_state_changed(QMediaPlayer.PlaybackState.PlayingState)

    assert viewer.play_btn.isChecked()
    assert viewer.play_btn.icon().cacheKey() == viewer._pause_active_icon.cacheKey()
    assert viewer.play_btn.icon().cacheKey() != viewer._pause_icon.cacheKey()
    viewer._teardown_player()


def test_resume_guard_hides_transient_backend_position_reset():
    viewer = SimpleNamespace(
        _resume_anchor_ms=8_000,
        _resume_guard_deadline=time.monotonic() + 1.0,
        _last_stable_position_ms=8_000,
    )

    assert ClipViewer._guarded_display_position(viewer, 0) == 8_000
    assert viewer._last_stable_position_ms == 8_000
    assert ClipViewer._guarded_display_position(viewer, 8_120) == 8_120


def test_quick_resume_does_not_prepare_deferred_audio_on_pause(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))
    deferred = (object(),)
    preparations = []
    viewer._deferred_audio_mixer_sources = deferred
    monkeypatch.setattr(viewer, '_prepare_audio_mixer', preparations.append)

    viewer._on_state_changed(QMediaPlayer.PlaybackState.PausedState)

    assert preparations == []
    assert viewer._timeline_prepare_timer.interval() == 1500
    viewer._load_timeline_thumbnails_if_idle()
    assert preparations == [deferred]
    viewer._teardown_player()


def test_editor_draft_is_debounced_and_restored_on_reopen(
        monkeypatch, qtbot, tmp_path):
    clip = tmp_path / 'draft.mp4'
    clip.write_bytes(b'placeholder media')
    manager = _DraftManager()
    first = _viewer(monkeypatch, qtbot, str(clip), manager)
    before = first._capture_editor_state()
    first.trim_slider.start_pct = 0.1
    first.trim_slider.end_pct = 0.9
    first.trim_slider.segments = [
        clip_viewer.TimelineSegment(0.0, 0.4),
        clip_viewer.TimelineSegment(0.4, 1.0, True),
    ]
    first.trim_slider.selected_segment = 1
    first._crop_rect = (10, 20, 640, 360)
    first._stretch_ratio = 1.25
    first._effects['contrast'] = 30
    first._commit_editor_change(before)

    assert manager.writes == 0
    assert first._draft_save_timer.isActive()
    first._close()
    assert manager.writes == 1

    reopened = _viewer(monkeypatch, qtbot, str(clip), manager)
    assert reopened.trim_slider.start_pct == 0.1
    assert reopened.trim_slider.end_pct == 0.9
    assert len(reopened.trim_slider.segments) == 2
    assert reopened.trim_slider.segments[1].deleted is True
    assert reopened._crop_rect == (10, 20, 640, 360)
    assert reopened._stretch_ratio == 1.25
    assert reopened._effects['contrast'] == 30
    assert manager.writes == 1
    reopened._teardown_player()


def test_immediate_play_is_deferred_until_preparation_finishes(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(
        monkeypatch,
        qtbot,
        str(tmp_path / 'clip.mp4'),
        settings=_Settings({'clip_viewer_autoplay': False}),
    )

    viewer._toggle_play()

    assert viewer._play_when_ready is True
    assert viewer.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState
    assert 'PLAY QUEUED' in viewer._playback_status_lbl.text()

    viewer._media_ready = True
    viewer._audio_preparation_ready = True
    viewer._update_playback_readiness()
    assert viewer._playback_ready is True
    assert viewer._play_when_ready is False
    viewer._teardown_player()


def test_single_audio_track_reuses_qt_decoder(monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))
    source = PlaybackSource('track', 'Track 1', 'track', 1, 0)

    def _unexpected_mixer(*_args, **_kwargs):
        raise AssertionError('a single direct track must not create a second decoder')

    monkeypatch.setattr(clip_viewer, 'FFmpegPlaybackController', _unexpected_mixer)
    viewer._on_audio_sources_discovered((source,), '')

    assert viewer._native_audio_source_id == 'track'
    assert viewer._audio_mixer is None
    assert viewer._audio_preparation_ready is True
    viewer._teardown_player()


def test_changed_speed_with_preserved_pitch_promotes_direct_audio_to_mixer(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))
    source = PlaybackSource('track', 'Track 1', 'track', 1, 0)
    viewer._speed_rate = 2.0
    viewer._preserve_pitch = True
    prepared = []
    monkeypatch.setattr(viewer, '_prepare_audio_mixer', prepared.append)

    viewer._on_audio_sources_discovered((source,), '')

    assert prepared == [(source,)]
    assert viewer._native_audio_source_id is None
    viewer._teardown_player()


def test_teardown_detaches_native_resources_and_cancels_callbacks(
        monkeypatch, qtbot, tmp_path):
    viewer = _viewer(monkeypatch, qtbot, str(tmp_path / 'clip.mp4'))
    assert viewer.player.parent() is viewer
    assert viewer.audio_output.parent() is viewer

    viewer._teardown_player()
    viewer._teardown_player()  # idempotent close + closeEvent paths

    assert viewer._prepare_cancel.is_set()
    assert not viewer._ph_timer.isActive()
    assert not viewer._seek_timer.isActive()
    assert not viewer._timeline_prepare_timer.isActive()
    assert viewer.player.source().isEmpty()
    assert viewer.player.videoOutput() is None
    assert viewer.player.audioOutput() is None


def test_timeline_images_are_cached_across_clip_switches(qtbot, tmp_path):
    clip = tmp_path / 'timeline.mp4'
    writer = cv2.VideoWriter(
        str(clip), cv2.VideoWriter_fourcc(*'mp4v'), 20, (160, 90))
    assert writer.isOpened()
    for value in range(20):
        writer.write(np.full((90, 160, 3), value * 10, dtype=np.uint8))
    writer.release()

    first = TrimSlider(1000)
    qtbot.addWidget(first)
    first.load_thumbnails(str(clip), count=4)
    qtbot.waitUntil(lambda: len(first._thumbnails) == 4, timeout=4000)
    assert first._thumbnail_worker_starts == 1

    second = TrimSlider(1000)
    qtbot.addWidget(second)
    second.load_thumbnails(str(clip), count=4)
    assert len(second._thumbnails) == 4
    assert second._thumbnail_worker_starts == 0


def test_overlay_images_decode_once_and_paint_without_replacement(
        qtbot, tmp_path):
    overlay_editor._IMAGE_PREVIEW_CACHE.clear()
    image_path = tmp_path / 'overlay.png'
    image = QPixmap(320, 180)
    image.fill(QColor('#28c4b8'))
    assert image.save(str(image_path), 'PNG')

    preview = UnifiedOverlayPreview()
    qtbot.addWidget(preview)
    preview.resize(640, 380)
    layers = [
        {
            'path': str(image_path), 'enabled': True, 'opacity': 100,
            'fit': 'fit',
            'rect': {'x': .03 + index * .16, 'y': .08, 'w': .15, 'h': .28},
        }
        for index in range(5)
    ]
    preview.set_image_layers(layers)
    first_key = preview._image_layers[0]['pixmap'].cacheKey()
    assert all(layer['pixmap'].cacheKey() == first_key
               for layer in preview._image_layers)
    assert len(overlay_editor._IMAGE_PREVIEW_CACHE) == 1

    preview.set_image_layers(layers)
    assert all(layer['pixmap'].cacheKey() == first_key
               for layer in preview._image_layers)
    assert len(overlay_editor._IMAGE_PREVIEW_CACHE) == 1
    for _ in range(20):
        target = QPixmap(preview.size())
        preview.render(target)
    assert preview._image_layers[0]['pixmap'].cacheKey() == first_key


def test_large_overlay_is_bounded_for_preview_without_touching_source(tmp_path):
    overlay_editor._IMAGE_PREVIEW_CACHE.clear()
    image_path = tmp_path / 'large.png'
    source = QImage(2400, 1800, QImage.Format.Format_RGB888)
    source.fill(QColor('#183b55'))
    assert source.save(str(image_path), 'PNG')

    preview = overlay_editor._cached_overlay_pixmap(str(image_path))

    assert not preview.isNull()
    assert preview.width() * preview.height() <= overlay_editor._IMAGE_PREVIEW_MAX_PIXELS
    # Preview decoding is non-destructive; export still receives the original.
    reader = QImage(str(image_path))
    assert reader.size() == source.size()


def test_camera_start_is_idempotent_for_active_device(monkeypatch):
    class _Capture:
        opens = 0
        releases = 0

        def __init__(self, _index):
            type(self).opens += 1
            self.open = True

        def isOpened(self):
            return self.open

        def release(self):
            if self.open:
                type(self).releases += 1
                self.open = False

    class _Thread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(camera_recorder, '_AVAILABLE', True)
    monkeypatch.setattr(camera_recorder._cv2, 'VideoCapture', _Capture)
    monkeypatch.setattr(camera_recorder.threading, 'Thread', _Thread)
    camera_recorder.CameraRecorder._instance = None
    recorder = camera_recorder.CameraRecorder()

    assert recorder.start(2) is True
    assert recorder.start(2) is True
    assert _Capture.opens == 1
    assert _Capture.releases == 0

    assert recorder.start(3) is True
    assert _Capture.opens == 2
    assert _Capture.releases == 1
    recorder.stop()


def test_hidden_camera_preview_does_not_touch_camera_frames(monkeypatch):
    class _UnexpectedRecorder:
        def __init__(self):
            raise AssertionError('hidden preview must not read or convert a camera frame')

    monkeypatch.setattr(camera_recorder, 'CameraRecorder', _UnexpectedRecorder)
    hidden_preview = SimpleNamespace(isVisible=lambda: False)
    host = SimpleNamespace(unified_overlay_editor=hidden_preview)

    _SettingsPage._update_camera_preview(host)


def test_full_preview_effects_fit_inside_smoke_budget(qtbot):
    preview = LiveVideoPreview(1280, 720)
    qtbot.addWidget(preview)
    image = QImage(1280, 720, QImage.Format.Format_RGB888)
    image.fill(QColor(80, 100, 120))
    effects = {
        'exposure': 20, 'contrast': 10, 'saturation': 25,
        'temperature': -10, 'sharpness': 40,
    }

    # Warm OpenCV's dispatch once, then use a median to avoid scheduler noise.
    LiveVideoPreview._apply_color_effects(image, effects)
    samples = []
    for _ in range(5):
        started = time.perf_counter()
        result = LiveVideoPreview._apply_color_effects(image, effects)
        samples.append((time.perf_counter() - started) * 1000.0)
    assert not result.isNull()
    assert sorted(samples)[len(samples) // 2] < 50.0


def test_neutral_editor_preview_uses_smooth_frame_scaling(qtbot):
    preview = LiveVideoPreview(2, 2)
    qtbot.addWidget(preview)
    preview.resize(9, 9)
    source = QImage(2, 2, QImage.Format.Format_RGB888)
    for y in range(2):
        source.setPixelColor(0, y, QColor('#000000'))
        source.setPixelColor(1, y, QColor('#ffffff'))
    preview._frame_image = source

    rendered = QImage(9, 9, QImage.Format.Format_RGB888)
    rendered.fill(QColor('#000000'))
    preview.render(rendered)

    boundary = rendered.pixelColor(4, 4).red()
    assert 0 < boundary < 255
