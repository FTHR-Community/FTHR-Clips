"""User performance choices must suppress work without changing captured media."""
from pathlib import Path
from types import SimpleNamespace
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QScrollArea

from core.settings_manager import SettingsManager
from core.ui_performance import DEFAULTS, PRESETS, matching_preset
from main import _SettingsPage
from ui.clip_grid import ClipGrid, ClipThumbnail
from ui.clip_viewer import ClipViewer
from ui.motion import set_reduce_motion


def test_export_concurrency_is_opt_in_and_persisted(qtbot, monkeypatch, tmp_path):
    from core import export_queue
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    sm = SettingsManager()
    page = _SettingsPage(sm)
    qtbot.addWidget(page)
    page._ensure_category(5)
    assert not page.parallel_exports_check.isChecked()
    assert not page.parallel_export_limit_combo.isEnabled()
    page.parallel_exports_check.setChecked(True)
    page.parallel_export_limit_combo.setCurrentIndex(2)
    assert export_queue._limit == 4
    assert SettingsManager().get('parallel_exports') is True
    assert SettingsManager().get('parallel_export_limit') == 4
    page.parallel_exports_check.setChecked(False)
    assert export_queue._limit == 1
    assert SettingsManager().get('parallel_exports') is False


def test_presets_persist_and_manual_changes_sync_clip_tab(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    sm = SettingsManager()
    sm.set('watermark_enabled', True)
    sm.set('encoder_preset', 6)
    page = _SettingsPage(sm)
    qtbot.addWidget(page)
    page._ensure_category(5)
    page._ensure_category(1)
    try:
        for name, values in PRESETS.items():
            page._apply_ui_performance_preset(name)
            assert matching_preset(sm) == name
            assert page.ui_performance_preset_combo.currentText() == name
            reloaded = SettingsManager()
            assert all(reloaded.get(key) == value for key, value in values.items())
            assert reloaded.get('watermark_enabled') is True
            assert reloaded.get('encoder_preset') == 6
            assert page.timeline_preview_strip_enabled_check.isChecked() == values['timeline_preview_strip_enabled']
        page.timeline_preview_strip_enabled_check.setChecked(True)
        assert page.ui_performance_preset_combo.currentText() == 'Custom'
        assert page._ui_performance_checks['timeline_preview_strip_enabled'].isChecked()
        page._ui_performance_checks['clip_editor_enabled'].setChecked(True)
        assert sm.get('clip_editor_enabled') is True
        assert isinstance(page.stack.widget(5), QScrollArea)
        page.stack.setCurrentIndex(5)
        page.resize(1050, 700)
        page.show()
        qtbot.wait(50)
        scroll = page.stack.widget(5)
        assert scroll.verticalScrollBar().maximum() > 0
    finally:
        set_reduce_motion(False)


def test_thumbnail_switch_cancels_and_releases_and_requeues(qtbot):
    sm = SimpleNamespace(get=lambda key, default=None: False)
    card = ClipThumbnail('sample.mp4')
    qtbot.addWidget(card)
    card._thumb_pixmap = QPixmap(32, 32)
    card._thumbnail_ready = True
    worker = SimpleNamespace(cancel_event=threading.Event())
    grid = SimpleNamespace(
        _sm=sm, _thumbnail_workers={'sample.mp4': worker},
        _recent_thumbnails={'old': object()}, _thumb_widgets={'sample.mp4': card},
        _recycled_cards=[], _deferred_thumbnail_paths={'sample.mp4'})
    grid._thumbnails_enabled = lambda: ClipGrid._thumbnails_enabled(grid)
    ClipGrid.apply_thumbnail_preference(grid)
    assert worker.cancel_event.is_set()
    assert card._thumb_pixmap.isNull() and not card._thumbnail_ready
    assert not grid._recent_thumbnails and not grid._deferred_thumbnail_paths
    # Disabled paths return before looking up a card, image, or worker.
    ClipGrid._start_thumbnail_worker(grid, 'sample.mp4')
    ClipGrid._on_thumb_ready(grid, 'sample.mp4', 'missing.png', 10)
    ClipGrid._on_image_thumb_ready(grid, 'sample.mp4', None)
    queued = []
    sm.get = lambda key, default=None: True
    grid._requeue_visible_thumbnail_jobs = lambda: queued.append(True)
    ClipGrid.apply_thumbnail_preference(grid)
    assert queued == [True]


def test_color_preview_does_not_modify_export_effects():
    effects = {'contrast': 35, 'vignette': 20}
    state = SimpleNamespace(sm={'editor_color_preview_enabled': False}, _effects=effects)
    assert ClipViewer._preview_color_effects(state) == {}
    assert state._effects == {'contrast': 35, 'vignette': 20}
    state.sm['editor_color_preview_enabled'] = True
    assert ClipViewer._preview_color_effects(state) == effects


def test_disabled_settings_previews_stop_before_reading_frames(qtbot):
    state = SimpleNamespace(sm={'settings_visual_previews_enabled': False})
    state._camera_preview_timer = QTimer()
    state._keyboard_preview_timer = QTimer()
    for timer in (state._camera_preview_timer, state._keyboard_preview_timer):
        timer.start(100)
    _SettingsPage._update_camera_preview(state)
    _SettingsPage._update_keyboard_preview(state)
    assert not state._camera_preview_timer.isActive()
    assert not state._keyboard_preview_timer.isActive()


def test_basic_viewer_has_no_editor_and_releases_source(qtbot, tmp_path):
    from ui.basic_clip_viewer import BasicClipViewer
    viewer = BasicClipViewer(str(tmp_path / 'missing.mp4'))
    qtbot.addWidget(viewer)
    assert not viewer.findChildren(ClipViewer)
    assert not hasattr(viewer, 'trim_slider')
    viewer.seek.setRange(0, 1000)
    viewer._position_changed(500)
    assert viewer.seek.value() == 500
    viewer.done(0)
    assert viewer.player.source().isEmpty()


def test_presets_only_contain_ui_preferences():
    assert matching_preset({}) == 'Full experience'
    for preset in PRESETS.values():
        assert set(preset) == set(DEFAULTS)
        assert preset['pause_ui_in_background'] is True


def test_library_viewer_mode_bypasses_editor_session_creation(qtbot, tmp_path):
    from main import MainWindow
    from PySide6.QtWidgets import QWidget

    class Host(QWidget):
        _forget_basic_viewer = MainWindow._forget_basic_viewer

    host = Host()
    qtbot.addWidget(host)
    host.settings_manager = {'clip_editor_enabled': False}
    host._clip_readiness = SimpleNamespace(can_access=lambda _: True)
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'')
    # No editor session, settings page, bridge or metadata service exists.
    MainWindow._on_clip_opened(host, str(clip), None, None)
    assert host._basic_clip_viewer is not None
    assert not hasattr(host, '_editor_sessions')
    host._basic_clip_viewer.close()
    qtbot.waitUntil(lambda: host._basic_clip_viewer is None)


def test_basic_viewer_plays_seeks_and_pauses_real_media(qtbot, tmp_path):
    import subprocess
    from core.ffmpeg_tools import get_ffmpeg_exe
    from PySide6.QtMultimedia import QMediaPlayer
    from ui.basic_clip_viewer import BasicClipViewer

    clip = tmp_path / 'sample.mp4'
    subprocess.run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                    'color=c=blue:s=160x90:r=25:d=3', '-c:v', 'mpeg4', str(clip)],
                   check=True, capture_output=True, timeout=20)
    viewer = BasicClipViewer(str(clip))
    qtbot.addWidget(viewer)
    viewer.show()
    qtbot.waitUntil(lambda: viewer.player.duration() > 0, timeout=10000)
    viewer._toggle_playback()
    qtbot.waitUntil(lambda: viewer.player.position() > 100, timeout=10000)
    viewer._toggle_playback()
    assert viewer.player.playbackState() == QMediaPlayer.PlaybackState.PausedState
    viewer.seek.setValue(1500)
    qtbot.waitUntil(lambda: abs(viewer.player.position() - 1500) < 150)
    assert viewer.player.error() == QMediaPlayer.Error.NoError
    viewer.close()
