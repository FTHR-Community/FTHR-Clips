"""Studio layout stays usable at the supported minimum window size."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap, QIcon
from PySide6.QtTest import QSignalSpy
from core.playback_mix_model import PlaybackSource
from ui.clip_viewer import ClipViewer, AudioMixPanel
from ui.clip_editor_window import ClipEditorWindow
from ui.style import ThemedDropdownButton

@pytest.fixture
def studio(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('ui.clip_grid.get_cached_clip_metadata', lambda *a, **k: (30,1920,1080,60,25000000,25000000))
    for method in ('_set_media_source','_prepare_playback_source','_discover_audio_sources_async','_load_timeline_thumbnails_if_idle'):
        monkeypatch.setattr(ClipViewer, method, lambda *a, **k: None)
    editor = ClipEditorWindow(str(tmp_path / 'clip.mp4'), None, thumb_pixmap=QPixmap())
    qtbot.addWidget(editor)
    yield editor
    editor._teardown_player(wait_for_workers=False)

@pytest.mark.parametrize('width,height', [(1440,900),(1920,1080),(2560,1440),(3840,2160),(1100,760),(960,600)])
def test_studio_preserves_preview_and_full_width_timeline(studio, qtbot, width, height):
    studio.resize(width,height)
    studio.show()
    qtbot.wait(20)
    page = studio.clip_page
    assert studio.width() == width
    assert page._video_clip_frame.width() >= 280
    assert page._video_clip_frame.height() >= 160
    assert page._studio_timeline.width() == page.width()
    assert page._studio_timeline.geometry().bottom() <= page.height()
    assert page._video_tools_scroll.horizontalScrollBar().maximum() == 0
    assert page._sidebar_scroll.horizontalScrollBar().maximum() == 0
    assert page.export_btn.isVisible()


def test_expanded_panels_scroll_without_displacing_timeline(studio, qtbot):
    studio.resize(960,600)
    studio.show()
    page = studio.clip_page
    for panel in page._studio_panels:
        header = panel.findChild(ThemedDropdownButton)
        if not header.isChecked(): header.click()
    qtbot.wait(20)
    assert page._video_tools_scroll.verticalScrollBar().maximum() > 0
    assert page._studio_timeline.geometry().bottom() <= page.height()
    assert page._video_clip_frame.height() >= 160
    panel = page._studio_panels[0]
    header = panel.findChild(ThemedDropdownButton)
    header.click()
    assert not panel.findChild(__import__('PySide6.QtWidgets',fromlist=['QFrame']).QFrame, 'panelBody').isVisible()
    header.click()
    assert header.isChecked()


def test_audio_app_icons_preserve_named_controls_and_mute(qtbot, monkeypatch):
    pixmap=QPixmap(24,24);pixmap.fill(Qt.GlobalColor.green)
    monkeypatch.setattr('ui.clip_viewer._running_windows_app_icon',lambda identity: QIcon(pixmap))
    source=PlaybackSource('chat','Discord','application',1,0,persistent_identity='discord')
    panel=AudioMixPanel(80,{}, {},(source,),True)
    qtbot.addWidget(panel)
    spy=QSignalSpy(panel.source_muted)
    assert not panel._icons['chat'].pixmap().isNull()
    assert panel._labels['chat'].toolTip() == 'Discord'
    panel._mute_buttons['chat'].click()
    assert spy.count() == 1
    assert spy.at(0) == ['chat',True]


def test_history_controls_live_in_timeline_and_editor_extras_are_removed(studio):
    page = studio.clip_page
    assert page._studio_timeline.isAncestorOf(page.undo_btn)
    assert page._studio_timeline.isAncestorOf(page.redo_btn)
    for name in ('_limiter_toggle', '_output_peak_label', '_audio_exclusions_panel'):
        assert not hasattr(page, name)


def test_layout_returns_to_small_size_after_large_portrait(studio, qtbot):
    studio.show()
    page = studio.clip_page
    studio.resize(3840, 2160)
    page._src_w, page._src_h = 608, 1080
    page._update_studio_layout()
    qtbot.wait(20)
    wide = page._studio_video_sidebar.width()
    page._src_w, page._src_h = 1920, 1080
    studio.resize(960, 600)
    qtbot.wait(20)
    assert page._studio_video_sidebar.width() < wide
    assert page._video_clip_frame.width() >= 280
    assert page._studio_timeline.geometry().bottom() <= page.height()


@pytest.mark.parametrize('width,height,source_width,source_height', [
    (1920, 1032, 1280, 1024),
    (2560, 1440, 1440, 1080),
    (3840, 2160, 1280, 1024),
    (1920, 1032, 608, 1080),
])
def test_narrow_clips_do_not_leave_large_gutters(studio, qtbot, width, height, source_width, source_height):
    page = studio.clip_page
    page._src_w, page._src_h = source_width, source_height
    page.video_widget.set_source_size(source_width, source_height)
    studio.resize(width, height)
    studio.show()
    page._update_studio_layout()
    qtbot.wait(30)
    fitted = page.video_widget._video_display_rect()
    assert page._video_clip_frame.width() - fitted.width() <= 48
    assert page._video_tools_scroll.horizontalScrollBar().maximum() == 0
    assert page._sidebar_scroll.horizontalScrollBar().maximum() == 0
    assert page.trim_slider.height() <= 145


def test_broad_tools_reflow_and_restore_without_losing_panels(studio, qtbot):
    page = studio.clip_page
    page._src_w, page._src_h = 608, 1080
    studio.resize(3840, 2160)
    studio.show()
    qtbot.wait(20)
    assert page._studio_video_columns == 2
    assert page._studio_video_grid.getItemPosition(1)[1] == 1
    studio.resize(960, 600)
    qtbot.wait(20)
    assert page._studio_video_columns == 1
    assert page._studio_video_grid.count() == 3
    assert all(page._studio_video_grid.getItemPosition(i)[1] == 0 for i in range(3))


def test_clip_and_file_details_follow_audio_in_the_right_sidebar(studio, qtbot):
    studio.resize(960, 600)
    studio.show()
    page = studio.clip_page
    right = page._sidebar_scroll.widget()
    panels = [panel for panel in page._studio_panels if right.isAncestorOf(panel)]
    assert [panel.findChild(ThemedDropdownButton).text() for panel in panels] == [
        'Audio mix', 'Clip details', 'File details']
    assert [right.layout().indexOf(panel) for panel in panels] == sorted(
        right.layout().indexOf(panel) for panel in panels)
    assert right.isAncestorOf(page._tag_edit)
    assert right.isAncestorOf(page._desc_edit)
    for panel in panels:
        header = panel.findChild(ThemedDropdownButton)
        if not header.isChecked():
            header.click()
    qtbot.wait(20)
    assert page._sidebar_scroll.verticalScrollBar().maximum() > 0
    assert page._sidebar_scroll.horizontalScrollBar().maximum() == 0
    assert page._studio_timeline.geometry().bottom() <= page.height()


def test_crop_overlay_does_not_shrink_the_full_source_preview(studio, qtbot):
    page = studio.clip_page
    studio.resize(1920, 1032)
    studio.show()
    qtbot.wait(20)
    before = page._studio_video_sidebar.width()
    page._crop_rect = (656, 0, 608, 1080)
    page._refresh_live_preview(immediate=True)
    qtbot.wait(20)
    assert page._studio_video_sidebar.width() == before
