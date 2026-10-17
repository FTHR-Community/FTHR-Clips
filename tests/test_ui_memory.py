"""Behavior and resource bounds for optional filmstrips and idle UI cleanup."""

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from main import _SettingsPage
from core.settings_manager import SettingsManager
from core.screenshot_save import ScreenshotCropSaveWorker
from ui import clip_viewer, camera_overlay_editor
from ui.clip_viewer import TrimSlider, ClipViewer, TimelineSegment
from ui.image_memory import ImageLRU, image_bytes
from ui.screenshot_editor import CropCanvas, ScreenshotEditor
from ui.shell_transitions import SnapshotSlide, ShellPageStack
from ui.clip_editor_window import ClipEditorWindow


def _image(w=180, h=100):
    image = QImage(w, h, QImage.Format.Format_RGB888)
    image.fill(QColor('teal'))
    return image


def _editor(monkeypatch, qtbot, tmp_path, settings=None):
    from ui import clip_grid
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *_: (10, 1920, 1080, 30, 1000000, 1000000))
    for name in ('_set_media_source', '_discover_audio_sources_async',
                 '_prepare_playback_source'):
        monkeypatch.setattr(ClipViewer, name, lambda *_: None)
    window = ClipEditorWindow(str(tmp_path / 'first.mp4'), None,
                              settings_manager=settings)
    qtbot.addWidget(window)
    return window


def test_byte_cache_evicts_oldest_even_below_count_limit_and_expires(qtbot):
    cache = ImageLRU(2 * image_bytes(_image()), ttl=5)
    cache['a'] = _image()
    cache['b'] = _image()
    assert cache['a'] is not None  # keep the most recently used entry
    cache['c'] = _image()
    assert list(cache) == ['a', 'c']
    assert cache.bytes_used <= cache.max_bytes
    cache['oversized'] = _image(1920, 1080)
    assert 'oversized' not in cache
    cache.expire(now=max(entry[2] for entry in cache._entries.values()) + 6)
    assert len(cache) == 0 and cache.bytes_used == 0


def test_disable_filmstrip_cancels_inflight_decode_and_rejects_late_cache(
        qtbot, monkeypatch, tmp_path):
    import core.media_process
    clip = tmp_path / 'clip.mp4'
    clip.touch()
    slider = TrimSlider(10_000)
    qtbot.addWidget(slider)
    started, finished = threading.Event(), threading.Event()

    def runner(_command, cancelled, **kwargs):
        started.set()
        cancelled.wait(2)
        # Simulate stdout already buffered when cancellation was requested.
        kwargs['on_stdout'](bytes(180 * 100 * 3))
        finished.set()
        return None

    monkeypatch.setattr(core.media_process, 'run_media_process', runner)
    monkeypatch.setattr(clip_viewer, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    clip_viewer._TIMELINE_IMAGE_CACHE.clear()
    slider.load_thumbnails(str(clip))
    qtbot.waitUntil(started.is_set)
    slider.set_preview_strip_enabled(False)
    qtbot.waitUntil(finished.is_set)
    qtbot.wait(10)
    slider.load_thumbnails(str(clip))
    assert slider._thumbnail_worker_starts == 1
    assert not slider._thumbnails and not slider._scaled_thumbnails
    assert clip_viewer._TIMELINE_IMAGE_CACHE.bytes_used == 0
    slider.resize(800, 116)
    slider.show()
    seeks = []
    slider.seek_requested.connect(seeks.append)
    QTest.mouseClick(slider, Qt.MouseButton.LeftButton, pos=QPoint(480, 56))
    assert .55 < seeks[-1] < .65
    assert slider.split_at_playhead()
    assert len(slider.segments) == 2
    slider.set_preview_strip_enabled(True)
    assert slider.preview_strip_enabled


def test_preference_persists_and_disabled_strip_does_not_block_audio(
        qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    assert settings.get('timeline_preview_strip_enabled') is True
    window = _editor(monkeypatch, qtbot, tmp_path, settings)
    page = window.clip_page
    _SettingsPage._save_clip_display_setting(
        SimpleNamespace(sm=settings, _sync_ui_performance_controls=lambda: None),
        'timeline_preview_strip_enabled', False)
    assert not page.trim_slider.preview_strip_enabled
    assert SettingsManager().get('timeline_preview_strip_enabled') is False
    page._schedule_timeline_thumbnails()
    assert not page._timeline_prepare_timer.isActive()
    pending = (object(),)
    prepared = []
    page._deferred_audio_mixer_sources = pending
    monkeypatch.setattr(page, '_prepare_audio_mixer', prepared.append)
    monkeypatch.setattr(page.player, 'playbackState',
                        lambda: QMediaPlayer.PlaybackState.PausedState)
    page._schedule_timeline_thumbnails()
    page._load_timeline_thumbnails_if_idle()
    assert prepared == [pending]
    for index in range(4):
        window.replace_clip(str(tmp_path / f'{index}.mp4'), autoplay=False)
        assert not window.trim_slider.preview_strip_enabled
        window._load_timeline_thumbnails_if_idle()
    assert window.trim_slider._thumbnail_worker_starts == 0


def test_idle_editor_releases_images_preserves_state_and_reuses_controls(
        qtbot, monkeypatch, tmp_path):
    window = _editor(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    page.trim_slider.start_pct = .1
    page.trim_slider.segments = [TimelineSegment(0, .5), TimelineSegment(.5, 1, True)]
    page._effects['contrast'] = 24
    state = page.capture_session_state()
    page.trim_slider._thumbnails = [QPixmap.fromImage(_image())]
    page.trim_slider._timeline_background = QPixmap(800, 116)
    controls = page.play_btn, page.trim_slider, page.video_widget
    assert page.release_idle_ui_resources() is False  # never trim active/exporting UI
    page._teardown_player(wait_for_workers=False)
    assert page.release_idle_ui_resources()
    assert not page.trim_slider._thumbnails
    assert page.trim_slider._timeline_background.isNull()
    window.replace_clip(page.clip_path, session_state=state, autoplay=False)
    assert controls == (page.play_btn, page.trim_slider, page.video_widget)
    assert page._effects['contrast'] == 24
    assert page.trim_slider.start_pct == .1
    assert page.trim_slider.segments[1].deleted


def test_hidden_library_discards_recent_images_and_excess_spares(qtbot):
    from ui.clip_grid import ClipGrid
    cards = [QWidget() for _ in range(12)]
    for card in cards:
        qtbot.addWidget(card)
    cache = ImageLRU(8 * 1024 * 1024)
    cache['recent'] = ('fingerprint', QPixmap.fromImage(_image()), 5, 'label', '')
    visible = QPixmap.fromImage(_image())
    host = SimpleNamespace(_background_paused=True, _current_columns=4,
                           _recent_thumbnails=cache, _recycled_cards=cards,
                           _scroll_viewport=lambda: None)
    ClipGrid._trim_hidden_memory(host)
    assert cache.bytes_used == 0 and len(cards) == 4
    assert not visible.isNull()


def test_overlay_preview_follows_display_size_and_reloads_after_idle(qtbot, tmp_path):
    image = _image(2400, 1800)
    path = tmp_path / 'overlay.png'
    assert image.save(str(path))
    preview = camera_overlay_editor.UnifiedOverlayPreview()
    qtbot.addWidget(preview)
    preview.resize(820, 500)
    layer = {'path': str(path), 'rect': {'x': .05, 'y': .05, 'w': .15, 'h': .2}}
    preview.set_image_layers([layer])
    small = preview._image_layers[0]['pixmap'].size()
    assert small.width() < 512 and small.height() < 512
    preview.set_rects({'image:0': {'x': .05, 'y': .05, 'w': .8, 'h': .8}})
    assert preview._image_layers[0]['pixmap'].width() > small.width()
    rects = preview.rects_data()
    preview._release_hidden_previews()
    assert preview._image_layers[0]['pixmap'].isNull()
    assert preview.rects_data() == rects
    preview.show()
    assert not preview._image_layers[0]['pixmap'].isNull()
    assert QImage(str(path)).size() == image.size()


@pytest.mark.parametrize('size', [QSize(1000, 1000), QSize(1800, 700)])
def test_crop_coordinates_account_for_letterbox_and_dpi(qtbot, size):
    canvas = CropCanvas(QPixmap.fromImage(_image()), source_size=QSize(7680, 4320))
    qtbot.addWidget(canvas)
    canvas.resize(size)
    canvas.show()
    shown = canvas._image_rect()
    canvas.crop_rect = QRect(shown.x(), shown.y(), shown.width(), shown.height())
    assert canvas.get_crop_rect() == QRect(0, 0, 7680, 4320)
    canvas.crop_rect = QRect(shown.x() - 100, shown.y() - 100,
                             shown.width() + 200, shown.height() + 200)
    assert canvas.get_crop_rect() == QRect(0, 0, 7680, 4320)


def test_large_screenshot_uses_small_preview_and_original_crop_pixels(qtbot, tmp_path):
    path, out = tmp_path / 'source.png', tmp_path / 'crop.partial'
    image = _image(7680, 4320)
    image.setPixelColor(321, 123, QColor('red'))
    assert image.save(str(path))
    editor = ScreenshotEditor(str(path), str(tmp_path / 'full.png'))
    qtbot.addWidget(editor)
    editor.resize(1000, 740)
    editor.show()
    qtbot.wait(150)
    assert image_bytes(editor.canvas._preview_pixmap) < 8 * 1024 * 1024
    assert editor.canvas.source_size == image.size()
    worker = ScreenshotCropSaveWorker(path, QRect(321, 123, 27, 19), out)
    failures = []
    worker.failed.connect(lambda *args: failures.append(args))
    worker.start()
    assert worker.wait(8000)
    assert failures == []
    crop = QImage(str(out))
    assert crop.size() == QSize(27, 19)
    assert crop.pixelColor(0, 0) == QColor('red')
    assert crop.pixelColor(26, 18) == image.pixelColor(347, 141)


def test_partial_crop_keeps_exact_source_coordinates_through_repeated_resizes(qtbot):
    canvas = CropCanvas(QPixmap.fromImage(_image()), source_size=QSize(7680, 4320))
    qtbot.addWidget(canvas)
    canvas.resize(937, 601)
    canvas.show()
    rect = canvas._image_rect()
    canvas.crop_rect = QRect(rect.x() + 123, rect.y() + 41, 417, 209)
    wanted = canvas.get_crop_rect()
    for size in (QSize(1001, 703), QSize(1200, 613), QSize(1900, 1300), QSize(937, 601)):
        canvas.resize(size)
        assert canvas.get_crop_rect() == wanted


def test_rotated_screenshot_preview_and_save_use_same_orientation(qtbot, tmp_path):
    pil = pytest.importorskip('PIL.Image')
    from PySide6.QtGui import QImageReader
    path = tmp_path / 'rotated.jpg'
    source = pil.new('RGB', (120, 80), 'red')
    exif = source.getexif()
    exif[274] = 6
    source.save(path, exif=exif)
    editor = ScreenshotEditor(str(path), str(tmp_path / 'full.png'))
    qtbot.addWidget(editor)
    assert editor.canvas.source_size == QSize(80, 120)
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    expected = reader.read().copy(QRect(12, 34, 25, 30))
    out = tmp_path / 'crop.png'
    worker = ScreenshotCropSaveWorker(path, QRect(12, 34, 25, 30), out)
    worker.start()
    assert worker.wait(2000)
    assert QImage(str(out)) == expected


@pytest.mark.parametrize('horizontal', [False, True])
@pytest.mark.parametrize('direction', [-1, 1])
def test_live_slide_keeps_one_image_and_restores_geometry(qtbot, horizontal, direction):
    stack = QWidget()
    qtbot.addWidget(stack)
    stack.resize(400, 300)
    page = QWidget(stack)
    page.setGeometry(stack.rect())
    page.setStyleSheet('background: blue')
    page.show()
    stack.show()
    outgoing = QPixmap(400, 300)
    outgoing.fill(QColor('red'))
    slide = SnapshotSlide(stack, outgoing, page, direction, horizontal=horizontal)
    slide.show()
    slide.raise_()
    assert slide._incoming.isNull()
    slide._advance(.5)
    shown = stack.grab().toImage()
    before = shown.pixelColor(50, 150) if horizontal else shown.pixelColor(200, 30)
    after = shown.pixelColor(350, 150) if horizontal else shown.pixelColor(200, 270)
    assert before == QColor('red' if direction > 0 else 'blue')
    assert after == QColor('blue' if direction > 0 else 'red')
    slide.hide()
    assert page.geometry() == stack.rect()
    assert slide._outgoing.isNull() and slide._incoming.isNull()


def test_snapshot_stack_interrupt_and_resize_keep_current_page_layout(qtbot):
    stack = ShellPageStack()
    qtbot.addWidget(stack)
    for color in ('red', 'green', 'blue'):
        page = QWidget()
        page.setStyleSheet(f'background: {color}')
        stack.addWidget(page)
    stack.resize(400, 300)
    stack.show()
    stack.setCurrentIndex(1)
    assert not stack._slide._incoming.isNull()
    assert stack._slide._live_incoming is None
    stack._slide._advance(.5)
    stack.setCurrentIndex(2)
    stack.resize(500, 400)
    assert stack._slide is None
    assert stack.currentWidget().geometry() == stack.contentsRect()
