"""Behavior and resource bounds for optional filmstrips and idle UI cleanup."""

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QMenu, QWidget

from main import _SettingsPage
from core.settings_manager import SettingsManager
from core.screenshot_save import ScreenshotCropSaveWorker
from ui import clip_viewer, camera_overlay_editor, editor_timeline
from ui.clip_viewer import TrimSlider, ClipViewer, TimelineSegment
from ui.image_memory import ImageLRU, image_bytes
from ui.screenshot_editor import CropCanvas, ScreenshotEditor
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
    monkeypatch.setattr(editor_timeline, 'get_ffmpeg_exe', lambda: 'ffmpeg')
    editor_timeline.clear_timeline_image_cache()
    slider.load_thumbnails(str(clip))
    qtbot.waitUntil(started.is_set)
    slider.set_preview_strip_enabled(False)
    qtbot.waitUntil(finished.is_set)
    qtbot.wait(10)
    slider.load_thumbnails(str(clip))
    assert slider._thumbnail_worker_starts == 1
    assert not slider._thumbnails and not slider._scaled_thumbnails
    assert editor_timeline._TIMELINE_IMAGE_CACHE.bytes_used == 0
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
    page_stub = SimpleNamespace(
        sm=settings,
        _sync_ui_performance_controls=lambda: None,
    )
    _SettingsPage._save_clip_display_setting(
        page_stub, 'timeline_preview_strip_enabled', False)
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
    from ui.clip_grid import ClipGrid, ClipThumbnail
    cards = [QWidget() for _ in range(12)]
    for card in cards:
        qtbot.addWidget(card)
    cache = ImageLRU(8 * 1024 * 1024)
    cache['recent'] = ('fingerprint', QPixmap.fromImage(_image()), 5, 'label', '')
    viewport = QWidget()
    qtbot.addWidget(viewport)
    visible_card = ClipThumbnail('visible.mp4', parent=viewport)
    visible = QPixmap.fromImage(_image())
    visible_card.set_custom_cover(visible, 'cover')
    covers = {'cover': visible}
    host = SimpleNamespace(_background_paused=True, _current_columns=4,
                           _recent_thumbnails=cache, _cover_pixmap_cache=covers,
                           _recycled_cards=cards,
                           _thumb_widgets={'visible.mp4': visible_card},
                           _scroll_viewport=lambda: viewport)
    ClipGrid._trim_hidden_memory(host)
    assert cache.bytes_used == 0 and len(cards) == 4
    assert not covers
    assert not visible_card._cover_pixmap.isNull()
    assert visible_card._cover_cache_key == 'cover'


@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('tool', ['crop', 'stretch'])
def test_editor_preview_dialog_disposes_after_result(
        qtbot, monkeypatch, tmp_path, accepted, tool):
    window = _editor(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    before = page._crop_rect, page._stretch_ratio
    crop = (10, 10, 160, 80)
    ratio = 1.5
    preview = _image()

    def load_crop(dialog):
        dialog._on_frame_loaded((dialog._frame_generation, preview, None))

    monkeypatch.setattr(clip_viewer.CropDialog, '_load_frame', load_crop)
    monkeypatch.setattr(clip_viewer.StretchDialog, '_extract_preview_frame',
                        lambda *_: QPixmap.fromImage(preview))
    dialog_type = (clip_viewer.CropDialog if tool == 'crop'
                   else clip_viewer.StretchDialog)
    destroyed = []

    def run_dialog(dialog):
        dialog.destroyed.connect(lambda: destroyed.append(True))
        if tool == 'crop':
            dialog.crop_rect = crop
        else:
            dialog.stretch_ratio = ratio
        QTimer.singleShot(0, dialog.accept if accepted else dialog.reject)
        return QDialog.exec(dialog)

    monkeypatch.setattr(dialog_type, 'exec', run_dialog)
    getattr(page, f'_open_{tool}_dialog')()
    expected = ((crop, before[1]) if tool == 'crop' else (before[0], ratio))
    assert (page._crop_rect, page._stretch_ratio) == (expected if accepted else before)
    qtbot.waitUntil(lambda: destroyed == [True])
    assert not page.findChildren(dialog_type)


def test_repeated_card_context_menu_close_releases_submenus(qtbot, monkeypatch):
    from ui.clip_grid import ClipThumbnail

    card = ClipThumbnail('preview.mp4')
    qtbot.addWidget(card)
    event = SimpleNamespace(globalPos=lambda: QPoint(0, 0))
    opened = []
    monkeypatch.setattr(card, '_emit_opened', lambda: opened.append(card.file_path))

    def choose_open():
        menu = QApplication.activePopupWidget()
        menu.setActiveAction(menu.actions()[0])
        QTest.keyClick(menu, Qt.Key.Key_Return)

    for close_menu in (lambda: QApplication.activePopupWidget().close(), choose_open):
        QTimer.singleShot(0, close_menu)
        card.contextMenuEvent(event)
        qtbot.waitUntil(lambda: not card.findChildren(QMenu))
    assert opened == ['preview.mp4']


def test_share_picker_disposes_on_cancel_and_after_selection(qtbot):
    parent = QWidget()
    qtbot.addWidget(parent)
    parent.sm = None
    opened = []
    parent._open_share_window = opened.append
    parent._open_share_preset = opened.append
    for choose_mode in (False, True):
        ClipViewer._show_share_overlay(parent)
        picker = parent.findChild(clip_viewer.ShareModeDialog)
        if choose_mode:
            picker._select(False)
        else:
            picker.reject()
        qtbot.waitUntil(lambda: not parent.findChildren(clip_viewer.ShareModeDialog))
    assert opened == [False]


@pytest.mark.parametrize('accepted', [False, True])
def test_game_crop_releases_screenshot_after_result(qtbot, monkeypatch, accepted):
    import main

    parent = QWidget()
    qtbot.addWidget(parent)
    rule = {'exe_name': 'game.exe'}
    saved = []
    host = SimpleNamespace(
        _custom_rules=[rule], window=lambda: parent,
        sm=SimpleNamespace(get=lambda _key, _default: 1),
        _save_rules=lambda: saved.append(True),
        _set_browser_status=lambda _status: None,
    )
    screen = SimpleNamespace(grabWindow=lambda _hwnd: QPixmap.fromImage(_image()))
    monkeypatch.setattr(main, 'QApplication', SimpleNamespace(primaryScreen=lambda: screen))

    def run_dialog(dialog):
        assert not dialog.canvas._preview.isNull()
        QTimer.singleShot(0, dialog.accept if accepted else dialog.reject)
        return QDialog.exec(dialog)

    monkeypatch.setattr(main.GameCropDialog, 'exec', run_dialog)
    main.GameDetectionPopup._edit_crop(host, 0)
    assert saved == ([True] if accepted else [])
    assert ('crop_profile' in rule) is accepted
    qtbot.waitUntil(lambda: not parent.findChildren(main.GameCropDialog))


@pytest.mark.parametrize('accepted', [False, True])
def test_common_modal_dialogs_release_after_return(qtbot, accepted):
    from ui.dialogs import FthrInputDialog, FthrMessageDialog

    parent = QWidget()
    qtbot.addWidget(parent)

    def finish():
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, FthrInputDialog):
            dialog.text_edit.setText('edited')
        (dialog.accept if accepted else dialog.reject)()

    QTimer.singleShot(0, finish)
    assert FthrInputDialog.get_text(parent, 'Input', 'Name') == ('edited', accepted)
    QTimer.singleShot(0, finish)
    assert FthrMessageDialog.question(parent, 'Confirm', 'Continue?') is accepted
    qtbot.waitUntil(lambda: not parent.findChildren(QDialog))


def test_nonmodal_message_releases_reference_on_parent_destruction(qtbot):
    from ui.dialogs import FthrMessageDialog

    parent = QWidget()
    before = len(FthrMessageDialog._live_non_modal)
    FthrMessageDialog.warning(parent, 'Warning', 'Example')
    assert len(FthrMessageDialog._live_non_modal) == before + 1
    parent.deleteLater()
    qtbot.waitUntil(lambda: len(FthrMessageDialog._live_non_modal) == before)


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




def test_disabled_strip_never_starts_work_during_playback_or_scrubbing(
        qtbot, monkeypatch, tmp_path):
    import core.media_process
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    settings.set('timeline_preview_strip_enabled', False)
    settings.save_settings()
    window = _editor(monkeypatch, qtbot, tmp_path, settings)
    page = window.clip_page
    page._playback_ready = True
    monkeypatch.setattr(page.player, 'playbackState',
                        lambda: QMediaPlayer.PlaybackState.PlayingState)
    calls = []
    monkeypatch.setattr(core.media_process, 'run_media_process',
                        lambda *args, **kwargs: calls.append((args, kwargs)))
    for _ in range(3):
        page._on_state_changed(QMediaPlayer.PlaybackState.PlayingState)
        page._on_seek_requested(.5)
        page._schedule_timeline_thumbnails()
        page._load_timeline_thumbnails_if_idle()
        page.trim_slider.load_thumbnails(page.clip_path)
    assert not page._timeline_prepare_timer.isActive()
    assert page.trim_slider._thumbnail_worker_starts == 0
    assert not calls
    assert not page.trim_slider._thumbnails
    window._teardown_player()


def test_disabling_filmstrip_reaps_a_real_decoder_and_stays_off(
        qtbot, monkeypatch, tmp_path):
    import subprocess
    import time
    from core import media_process
    from core.ffmpeg_tools import get_ffmpeg_exe

    clip = tmp_path / 'slow-filmstrip.mp4'
    subprocess.run([
        get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
        'color=c=blue:s=160x90:r=24:d=24', '-c:v', 'mpeg4', str(clip),
    ], check=True, capture_output=True, timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    original_runner = media_process.run_media_process
    children = []
    original_started = media_process._media_process_started

    def record_child(process):
        children.append(process)
        original_started(process)

    def paced_runner(command, *args, **kwargs):
        # Force a real long-running FFmpeg decode so the toggle must kill it,
        # rather than racing a tiny fixture which has already completed.
        command = list(command)
        command.insert(command.index('-i'), '-re')
        return original_runner(command, *args, **kwargs)

    monkeypatch.setattr(media_process, '_media_process_started', record_child)
    monkeypatch.setattr(media_process, 'run_media_process', paced_runner)
    slider = TrimSlider(24_000)
    qtbot.addWidget(slider)
    slider.load_thumbnails(str(clip))
    qtbot.waitUntil(lambda: bool(slider._thumbnails), timeout=5000)
    assert children and children[0].poll() is None
    started = time.monotonic()
    slider.set_preview_strip_enabled(False)
    qtbot.waitUntil(lambda: media_process.media_process_snapshot()[0] == 0, timeout=3000)
    assert time.monotonic() - started < 3
    assert all(child.poll() is not None for child in children)
    assert not slider._thumbnails and not slider._scaled_thumbnails
    assert editor_timeline._TIMELINE_IMAGE_CACHE.bytes_used == 0
    starts = slider._thumbnail_worker_starts
    child_count = len(children)
    for _ in range(3):
        slider.load_thumbnails(str(clip))
    qtbot.wait(200)
    assert len(children) == child_count
    assert slider._thumbnail_worker_starts == starts
    assert not slider._thumbnails
