"""Windows scene graph checks for single-image slides and idle resource release."""

import pytest
from PySide6.QtGui import QColor, QImage
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QWidget

from ui.quick_video_preview import QuickVideoPreview, quick_preview_supported


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
