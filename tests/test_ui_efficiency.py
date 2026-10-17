"""UI resource reductions must preserve navigation, pixels and edit state."""

from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QEvent, QRect
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from core.encoder_capabilities import EncoderCapability
from core.settings_manager import SettingsManager
from main import _SettingsPage
from ui.camera_overlay_editor import UnifiedOverlayPreview, _IMAGE_PREVIEW_CACHE
from ui.clip_viewer import TrimSlider, TimelineSegment
from ui.hover_transition import HoverTransitions
from ui.keyboard_overlay_preview import KeyboardSourcePreview
from ui.motion import set_reduce_motion
from ui.shell_transitions import ShellPageStack
from ui.timeline_segments import TimelineSegments


@pytest.fixture
def settings_page(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for name in ('_populate_mic_devices', '_populate_playback_devices',
                 '_refresh_keyboard_windows', '_start_encoder_probe'):
        monkeypatch.setattr(_SettingsPage, name, lambda *args, **kwargs: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    yield page
    set_reduce_motion(False)


def _capabilities():
    return (EncoderCapability('nvenc', 'NVIDIA NVENC', ('h264', 'hevc'),
                              (('h264', 'h264_nvenc'), ('hevc', 'hevc_nvenc')), True),)


def test_settings_construct_only_general_and_pause_does_not_materialize_pages(settings_page):
    page = settings_page
    assert page._loaded_categories == {0}
    assert 'mic_combo' not in page.__dict__
    assert 'unified_overlay_editor' not in page.__dict__
    page.set_background_ui_paused(True)
    page.set_background_ui_paused(False)
    assert page._loaded_categories == {0}


def test_rapid_tab_navigation_builds_only_final_category(settings_page, qtbot):
    page = settings_page
    page.show()
    for index in (3, 4, 2, 1, 5):
        page._on_category_changed(index)
    assert page._loaded_categories == {0}
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 5)
    assert page._loaded_categories == {0, 5}
    assert 'mic_level_meter' not in page.__dict__
    assert 'unified_overlay_editor' not in page.__dict__


def test_settings_navigation_creates_one_category_and_preserves_its_controls(settings_page):
    page = settings_page
    page.stack.setCurrentIndex(1)
    assert page._loaded_categories == {0, 1}
    control = page.timeline_preview_strip_enabled_check
    control.setChecked(False)
    for index in (5, 3, 4, 6, 2, 0, 1):
        page.stack.setCurrentIndex(index)
        assert page.stack.currentWidget().parentWidget() is page.stack
    assert page._loaded_categories == set(range(7))
    assert page.timeline_preview_strip_enabled_check is control
    assert not control.isChecked()
    assert not page.sm.get('timeline_preview_strip_enabled')


def test_performance_first_encoder_apply_does_not_construct_clip(settings_page):
    page = settings_page
    page.sm.set('encoder_pref', 'nvenc')
    page._on_encoder_capabilities_ready(_capabilities())
    page.stack.setCurrentIndex(5)
    assert page._loaded_categories == {0, 5}
    page.performance_encoder_preset_combo.setCurrentIndex(5)
    page._on_encoder_apply()
    assert page.sm.get('encoder_preset') == 6
    assert page._loaded_categories == {0, 5}
    page.stack.setCurrentIndex(1)
    assert page.encoder_combo.currentData() == 'nvenc'
    assert page.encoder_preset_combo.currentData() == 6


def test_pending_encoder_and_codec_changes_survive_lazy_performance_page(settings_page):
    page = settings_page
    page.sm.set('encoder_pref', 'nvenc')
    page._on_encoder_capabilities_ready(_capabilities())
    page.stack.setCurrentIndex(1)
    page.codec_combo.setCurrentIndex(page.codec_combo.findData('hevc'))
    page.encoder_preset_combo.setCurrentIndex(5)
    page.stack.setCurrentIndex(5)
    assert page.codec_combo.currentData() == 'hevc'
    assert page.performance_encoder_preset_combo.currentData() == 6
    assert not page.performance_encoder_apply_btn.isHidden()
    page._on_encoder_apply()
    assert page.sm.get('codec_pref') == 'hevc'
    assert page.sm.get('encoder_preset') == 6


def test_audio_toggle_in_performance_is_loaded_by_audio_later(settings_page):
    page = settings_page
    page.stack.setCurrentIndex(5)
    page.performance_audio_check.setChecked(False)
    assert page._loaded_categories == {0, 5}
    page.stack.setCurrentIndex(2)
    assert not page.audio_capture_check.isChecked()
    assert not page.separate_audio_check.isEnabled()


def test_keyboard_duplicate_frames_skip_conversion_but_setting_changes_refresh(
        settings_page, monkeypatch):
    page = settings_page
    page.stack.setCurrentIndex(3)
    page.show()
    frame = np.zeros((120, 220, 3), dtype=np.uint8)
    page._keyboard_capture = SimpleNamespace(latest_frame=lambda: (frame, 1.0), last_error='')
    import cv2
    calls = []
    convert = cv2.cvtColor
    monkeypatch.setattr(cv2, 'cvtColor', lambda *args, **kwargs:
                        (calls.append(True), convert(*args, **kwargs))[1])
    page._update_keyboard_preview()
    key = page.unified_overlay_editor._keyboard_pixmap.cacheKey()
    count = len(calls)
    assert count > 0
    for _ in range(8):
        page._update_keyboard_preview()
    assert len(calls) == count
    assert page.unified_overlay_editor._keyboard_pixmap.cacheKey() == key
    page.sm.set('third_party_keyboard', {'color': '#ff00ff', 'intensity': 12})
    page._update_keyboard_preview()
    assert len(calls) > count
    page.hide()
    page.unified_overlay_editor._release_hidden_previews()
    page.keyboard_source_preview._release_hidden_image()
    page.show()
    before = len(calls)
    page._update_keyboard_preview()
    assert len(calls) > before
    assert not page.keyboard_source_preview._image.isNull()


def test_keyboard_image_sharing_retains_owned_pixels_and_detaches_on_mutation(qtbot):
    preview = KeyboardSourcePreview()
    qtbot.addWidget(preview)
    image = QImage(20, 10, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    preview.set_image(image, share_owned=True)
    assert preview._image.cacheKey() == image.cacheKey()
    image.fill(QColor('blue'))
    assert preview._image.pixelColor(2, 2) == QColor('red')
    del image
    assert preview._image.pixelColor(2, 2) == QColor('red')


def test_keyboard_default_image_api_keeps_external_buffer_independent(qtbot):
    preview = KeyboardSourcePreview()
    qtbot.addWidget(preview)
    pixels = np.zeros((10, 20, 3), dtype=np.uint8)
    pixels[:, :, 0] = 255
    image = QImage(pixels.data, 20, 10, pixels.strides[0], QImage.Format.Format_RGB888)
    preview.set_image(image)
    pixels[:, :, :] = 0
    assert preview._image.pixelColor(2, 2) == QColor('red')


def test_background_is_bounded_released_restored_and_resized(qtbot, tmp_path):
    path = tmp_path / 'large.png'
    image = QImage(7680, 4320, QImage.Format.Format_RGB32)
    image.fill(QColor('red'))
    assert image.save(str(path))
    del image
    preview = UnifiedOverlayPreview()
    qtbot.addWidget(preview)
    preview.resize(820, 500)
    preview.set_background_paths([str(tmp_path / 'missing.png'), str(path)])
    assert preview._background.width() < 2000
    initial = preview._background.width()
    preview.show()
    preview.hide()
    preview._release_hidden_previews()
    assert preview._background.isNull()
    assert not any(key[0].endswith('large.png') for key in _IMAGE_PREVIEW_CACHE)
    preview.resize(1600, 900)
    preview.show()
    preview._refresh_image_previews()
    assert preview._background.width() > initial
    assert preview._background.width() * preview._background.height() <= 4_000_000
    assert preview._background.toImage().pixelColor(10, 10) == QColor('red')


def test_live_overlay_update_skips_nonintersecting_layers(qtbot, tmp_path, monkeypatch):
    path = tmp_path / 'image.png'
    image = QPixmap(30, 30)
    image.fill(QColor('red'))
    image.save(str(path))
    preview = UnifiedOverlayPreview()
    qtbot.addWidget(preview)
    preview.resize(820, 500)
    preview.set_rects({'camera': {'x': .7, 'y': .7, 'w': .2, 'h': .2},
                       'keyboard': {'x': .05, 'y': .05, 'w': .2, 'h': .2}})
    preview.set_image_layers([{'path': str(path), 'enabled': True,
                               'rect': {'x': .4, 'y': .05, 'w': .2, 'h': .2}}])
    preview.show()
    qtbot.wait(30)
    calls = []
    draw_image = preview._draw_image_layer
    monkeypatch.setattr(preview, '_draw_image_layer', lambda *args:
                        (calls.append(True), draw_image(*args))[1])
    preview.set_camera_pixmap(image)
    qtbot.wait(30)
    assert calls == []
    # Full renders still contain the untouched image layer.
    rendered = preview.grab().toImage()
    point = preview._pixel_rect_for('image:0').center().toPoint()
    assert rendered.pixelColor(point).red() > 150


def test_reduce_motion_persists_stops_active_slides_and_skips_new_snapshots(
        settings_page, qtbot, monkeypatch):
    page = settings_page
    page.resize(1100, 720)
    page.show()
    page.stack.setCurrentIndex(5)
    assert page.stack._slide is not None
    page.reduce_ui_animations_check.setChecked(True)
    assert page.stack._slide is None
    assert page.stack.currentWidget().geometry() == page.stack.rect()
    restored = SettingsManager()
    assert restored.get('reduce_ui_animations') is True
    monkeypatch.setattr(page.stack, 'grab', lambda *args: pytest.fail('snapshot in reduced motion'))
    page.stack.setCurrentIndex(1)
    assert page.stack._slide is None
    shell = ShellPageStack(page)
    qtbot.addWidget(shell)
    shell.addWidget(QWidget())
    shell.addWidget(QWidget())
    shell.show()
    monkeypatch.setattr(shell, 'grab', lambda *args: pytest.fail('snapshot in reduced motion'))
    shell.setCurrentIndex(1)
    assert shell._slide is None


def test_reduce_motion_hover_is_immediate_and_cleans_existing_fade(settings_page, qtbot):
    page = settings_page
    page.show()
    button = QPushButton('Hover', page)
    button.show()
    transitions = HoverTransitions(QApplication.instance())
    try:
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        assert button._hover_fade is not None
        page.reduce_ui_animations_check.setChecked(True)
        assert button._hover_fade is None
        QApplication.sendEvent(button, QEvent(QEvent.Type.Leave))
        QApplication.sendEvent(button, QEvent(QEvent.Type.Enter))
        assert button._hover_fade is None
    finally:
        QApplication.instance().removeEventFilter(transitions)
        transitions.deleteLater()


def test_segment_mapping_reuses_lists_until_field_or_structure_edit(qtbot):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.segments = [TimelineSegment(0, .5), TimelineSegment(.5, 1)]
    timeline.set_hide_deleted_segments(True)
    timeline._ensure_range_mapping()
    mapped = timeline._mapped_ranges
    for index in range(100):
        timeline.set_playhead(index / 100)
        assert timeline._source_to_display(.75) == .75
        assert timeline._mapped_ranges is mapped
    timeline.segments[0].deleted = True
    assert timeline._source_to_display(.75) == .5
    assert timeline._mapped_ranges is not mapped
    timeline.segments[1].start_pct = .6
    assert timeline._source_to_display(.8) == pytest.approx(.5)
    old = timeline.segments
    timeline.segments = [TimelineSegment(0, 1)]
    assert timeline._source_to_display(.8) == .8
    old[0].deleted = False
    assert timeline._source_to_display(.8) == .8


@pytest.mark.parametrize('operation', [
    lambda s: s.append(TimelineSegment(1, 2)),
    lambda s: s.extend([TimelineSegment(1, 2)]),
    lambda s: s.insert(0, TimelineSegment(-1, 0)),
    lambda s: s.pop(), lambda s: s.remove(s[0]), lambda s: s.clear(),
    lambda s: s.reverse(), lambda s: s.sort(key=lambda item: -item.start_pct),
    lambda s: s.__setitem__(0, TimelineSegment(0, .1)),
    lambda s: s.__setitem__(slice(None), [TimelineSegment(0, 1)]),
    lambda s: s.__delitem__(0), lambda s: s.__iadd__([TimelineSegment(1, 2)]),
    lambda s: s.__imul__(2),
])
def test_segment_list_mutations_invalidate_and_attach_field_observers(operation):
    segments = TimelineSegments([TimelineSegment(0, .5), TimelineSegment(.5, 1)])
    version = segments.version
    operation(segments)
    assert segments.version > version
    if segments:
        version = segments.version
        segments[0].deleted = not segments[0].deleted
        assert segments.version > version


def test_segment_observer_supports_two_timelines_and_detaching():
    segment = TimelineSegment(0, 1)
    first, second = TimelineSegments([segment]), TimelineSegments([segment])
    before = first.version, second.version
    segment.deleted = True
    assert first.version > before[0] and second.version > before[1]
    first.detach()
    before = first.version, second.version
    segment.deleted = False
    assert first.version == before[0] and second.version > before[1]


def test_playhead_seek_requests_two_small_regions_instead_of_whole_strip(qtbot, monkeypatch):
    timeline = TrimSlider(10_000)
    qtbot.addWidget(timeline)
    timeline.resize(1000, 116)
    timeline.set_playhead(.1)
    regions = []
    monkeypatch.setattr(timeline, 'update', lambda rect: regions.append(QRect(rect)))
    timeline.set_playhead(.9)
    assert len(regions) == 2
    assert all(rect.width() == 15 for rect in regions)
