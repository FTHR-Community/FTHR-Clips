"""Windows scene graph checks for single-image slides and idle resource release."""

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QWidget

from ui.quick_video_preview import QuickVideoPreview, quick_preview_supported
from ui.shell_transitions import SnapshotSlide


def _live_surface(qtbot):
    if not quick_preview_supported():
        pytest.skip('Native Windows hardware scene graph required')
    owner = QWidget()
    qtbot.addWidget(owner)
    owner.resize(400, 225)
    preview = QuickVideoPreview(owner)
    preview.setGeometry(owner.rect())
    owner.show()
    preview.show()
    image = QImage(400, 225, QImage.Format.Format_RGB32)
    image.fill(QColor('blue'))
    preview.videoSink().setVideoFrame(QVideoFrame(image))
    qtbot.wait(100)
    return owner, preview


def test_reduced_motion_retires_transition_surface_and_reenables_cleanly(
        qtbot, monkeypatch, tmp_path):
    from shiboken6 import isValid
    from ui.motion import set_reduce_motion
    from ui import clip_grid, clip_viewer
    from ui.clip_editor_window import ClipEditorWindow
    if not quick_preview_supported():
        pytest.skip('Native Windows hardware scene graph required')
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *_: (10, 1920, 1080, 30, 1000000, 1000000))
    for method in ('_set_media_source', '_discover_audio_sources_async',
                   '_prepare_playback_source', '_load_timeline_thumbnails_if_idle'):
        monkeypatch.setattr(clip_viewer.ClipViewer, method, lambda *_: None)
    window = ClipEditorWindow(str(tmp_path / 'first.mp4'), None)
    qtbot.addWidget(window)
    set_reduce_motion(False)
    window.show()
    window.replace_clip(str(tmp_path / 'second.mp4'), navigation_step=1, autoplay=False)
    surface = window._snapshot_slide_surface
    assert surface is not None and surface._keys
    try:
        set_reduce_motion(True)
        assert window._clip_slide is None
        assert window._snapshot_slide_surface is None
        assert window.clip_page.geometry() == window.contentsRect()
        qtbot.waitUntil(lambda: not isValid(surface))
        window.replace_clip(str(tmp_path / 'first.mp4'), navigation_step=-1, autoplay=False)
        assert window._clip_slide is None
        set_reduce_motion(False)
        window._last_navigation_at = None
        window.replace_clip(str(tmp_path / 'second.mp4'), navigation_step=1, autoplay=False)
        assert window._clip_slide is not None
        assert window._snapshot_slide_surface is not surface
        assert window._snapshot_slide_surface._keys
    finally:
        window._teardown_player(wait_for_workers=False)
        set_reduce_motion(False)


def test_live_gpu_slide_preserves_video_and_clipping_with_one_provider_image(qtbot):
    owner, preview = _live_surface(qtbot)
    outgoing = QPixmap(owner.size())
    outgoing.fill(QColor('red'))
    for direction in (1, -1):
        slide = SnapshotSlide(owner, outgoing, preview, direction, horizontal=True)
        slide.show()
        slide.raise_()
        assert len(slide._surface._keys) == 1
        slide._advance(.5)
        qtbot.wait(100)
        image = owner.grab().toImage()
        dpr = image.devicePixelRatio()
        assert image.pixelColor(int(50 * dpr), int(110 * dpr)) == QColor(
            'red' if direction > 0 else 'blue')
        assert image.pixelColor(int(350 * dpr), int(110 * dpr)) == QColor(
            'blue' if direction > 0 else 'red')
        slide.hide()
        assert preview.geometry() == QRect(0, 0, 400, 225)
        assert not owner._snapshot_slide_surface._keys
        slide.deleteLater()
    assert preview.gpu_ready


def test_idle_gpu_scene_graph_invalidates_and_recreates_without_losing_sink(qtbot):
    owner, preview = _live_surface(qtbot)
    sink = preview.videoSink()
    invalidated = []
    preview.quickWindow().sceneGraphInvalidated.connect(lambda: invalidated.append(True))
    # Real hardware event, rather than assuming hide releases GPU allocations.
    owner.hide()
    preview.release_idle_resources()
    qtbot.waitUntil(lambda: bool(invalidated), timeout=3000)
    assert not preview.quickWindow().isPersistentSceneGraph()
    preview.resume_resources()
    owner.show()
    image = QImage(400, 225, QImage.Format.Format_RGB32)
    image.fill(QColor('green'))
    sink.setVideoFrame(QVideoFrame(image))
    qtbot.wait(150)
    assert preview.videoSink() is sink and preview.gpu_ready
    frame = preview.grabFramebuffer()
    assert frame.pixelColor(frame.width() // 2, frame.height() // 2) == QColor('green')


def test_parked_editor_retires_render_target_and_recreates_after_grace(
        qtbot, monkeypatch, tmp_path):
    from test_ui_memory import _editor
    from shiboken6 import isValid
    if not quick_preview_supported():
        pytest.skip('Native Windows hardware scene graph required')
    window = _editor(monkeypatch, qtbot, tmp_path)
    window.resize(1100, 700)
    window.show()
    page = window.clip_page
    original = page._native_video_widget
    assert isinstance(original, QuickVideoPreview)
    page._effects['contrast'] = 24
    state = page.capture_session_state()
    controls = page.play_btn, page.trim_slider, page.video_widget
    window._teardown_player(wait_for_workers=False)
    window._idle_memory_timer.setInterval(10)
    window.hide()
    qtbot.waitUntil(lambda: not isValid(original))
    assert page._idle_gpu_rebuild
    window.replace_clip(page.clip_path, session_state=state, autoplay=False)
    window.show()
    surface = page._native_video_widget
    assert isinstance(surface, QuickVideoPreview) and surface is not original
    assert controls == (page.play_btn, page.trim_slider, page.video_widget)
    assert page._effects['contrast'] == 24
    image = QImage(400, 225, QImage.Format.Format_RGB32)
    image.fill(QColor('blue'))
    surface.show()
    surface.videoSink().setVideoFrame(QVideoFrame(image))
    qtbot.wait(100)
    assert surface.gpu_ready and not surface.grabFramebuffer().isNull()
