from PySide6.QtGui import QPixmap

from ui.clip_editor_window import ClipEditorWindow


class _Prepared:
    def __init__(self):
        self.disposals = 0

    def dispose(self):
        self.disposals += 1


def test_deferred_editor_paints_skeleton_without_thumbnail_before_constructing_page(monkeypatch, qtbot):
    prepared = _Prepared()
    thumb = QPixmap(128, 72)
    thumb.fill()
    window = ClipEditorWindow('first.mp4', None, defer_initial_clip=True, autoplay=False,
                              thumb_pixmap=thumb, preloaded_media=prepared)
    qtbot.addWidget(window)
    calls = []

    def open_clip(path, **options):
        assert window.isVisible()
        assert window._poster._painted
        assert not hasattr(window._poster, '_thumbnail')
        assert window._poster.close_button.isVisible()
        assert not options['autoplay']
        assert options['preloaded_media'] is prepared
        assert prepared.disposals == 0
        calls.append(path)
        return True

    monkeypatch.setattr(window, 'replace_clip', open_clip)
    assert window.clip_page is None
    assert window.clip_path == 'first.mp4'
    assert window.clip_pending
    assert calls == []
    with qtbot.waitSignal(window.clip_page_ready):
        window.show()
    assert calls == ['first.mp4']
    assert not window.clip_pending
    assert window._poster is None
    assert prepared.disposals == 1


def test_closing_after_show_cancels_queued_setup(monkeypatch, qtbot):
    prepared = _Prepared()
    window = ClipEditorWindow('first.mp4', None, defer_initial_clip=True,
                              preloaded_media=prepared)
    qtbot.addWidget(window)
    calls = []
    monkeypatch.setattr(window, 'replace_clip', lambda *args, **kw: calls.append(args))
    window.show()
    with qtbot.waitSignal(window.finished):
        window._close()
    qtbot.wait(20)
    assert calls == []
    assert window.clip_page is None
    assert not window.clip_pending
    assert prepared.disposals == 1


def test_destroying_queued_shell_disposes_preload(qtbot):
    prepared = _Prepared()
    window = ClipEditorWindow('first.mp4', None, defer_initial_clip=True,
                              preloaded_media=prepared)
    window.deleteLater()
    qtbot.waitUntil(lambda: prepared.disposals == 1)


def test_parked_editor_rebinds_after_paint_and_preserves_page(monkeypatch, qtbot, tmp_path):
    from ui.clip_viewer import ClipViewer

    monkeypatch.setattr(ClipViewer, '_prepare_initial_media_source', lambda _self: None)
    monkeypatch.setattr(ClipViewer, '_discover_audio_sources_async', lambda _self: None)
    first, next_clip = tmp_path / 'first.mp4', tmp_path / 'next.mp4'
    first.touch()
    next_clip.touch()
    window = ClipEditorWindow(str(first), None)
    qtbot.addWidget(window)
    page = window.clip_page
    state = window.capture_session_state()
    window._teardown_player(wait_for_workers=False)

    window.queue_clip_after_paint(str(next_clip), session_state=state, autoplay=False)
    assert window.clip_path == str(next_clip)
    assert window.capture_session_state() is state
    assert page.clip_path == str(first)
    with qtbot.waitSignal(window.clip_page_ready):
        window.show()
    assert window.clip_page is page
    assert page.clip_path == str(next_clip)
    assert page.isVisible()
    assert window._poster is not None
    page._playback_ready = True
    page._audio_preparation_ready = True
    page._native_has_real_frame = True
    window._finish_loading_if_ready()
    assert window._poster is None


def test_ready_controls_and_video_reveal_while_audio_is_loading(monkeypatch, qtbot, tmp_path):
    from ui.clip_viewer import ClipViewer, PlayerLifecycleState

    monkeypatch.setattr(ClipViewer, '_prepare_initial_media_source', lambda _self: None)
    monkeypatch.setattr(ClipViewer, '_discover_audio_sources_async', lambda _self: None)
    path = tmp_path / 'pending.mp4'
    path.touch()
    window = ClipEditorWindow(str(path), None, defer_initial_clip=True)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.clip_page_ready):
        window.show()
    assert window.isVisible() and window._poster.isVisible()
    assert window._content.currentWidget() is window._poster
    page = window.clip_page
    assert set(window._poster._regions) == {'video', 'audio'}
    assert not window._poster.mask().contains(page.play_btn.mapTo(window, page.play_btn.rect().center()))
    assert not window._poster.mask().contains(page.trim_slider.mapTo(window, page.trim_slider.rect().center()))
    page._native_has_real_frame = True
    window._finish_loading_if_ready()
    assert set(window._poster._regions) == {'audio'}
    assert not page._playback_ready
    page._player_lifecycle_state = PlayerLifecycleState.FAILED
    window._finish_loading_if_ready()
    assert window._poster is None
    assert window.clip_page.isVisible()


def test_audio_reveals_before_video_and_before_mixer_readiness(monkeypatch, qtbot, tmp_path):
    from ui.clip_viewer import ClipViewer
    monkeypatch.setattr(ClipViewer, '_prepare_initial_media_source', lambda _self: None)
    monkeypatch.setattr(ClipViewer, '_discover_audio_sources_async', lambda _self: None)
    path = tmp_path / 'pending.mp4'
    path.touch()
    window = ClipEditorWindow(str(path), None, defer_initial_clip=True)
    qtbot.addWidget(window)
    with qtbot.waitSignal(window.clip_page_ready):
        window.show()
    page = window.clip_page
    page._playback_sources = (object(),)
    window._finish_loading_if_ready()
    assert set(window._poster._regions) == {'video'}
    assert not page._audio_preparation_ready
    page._native_has_real_frame = True
    window._finish_loading_if_ready()
    assert window._poster is None
    assert not window._loading_timer.isActive()
    page._playback_sources = ()
