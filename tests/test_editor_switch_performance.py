"""Resource lifetime and behavior regressions for reusable editor surfaces."""

import threading
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QWidget

from ui import clip_grid, clip_viewer
from ui.clip_editor_window import ClipEditorWindow
from core import audio_manifest, media_metadata
import pytest


def shell(monkeypatch, qtbot, tmp_path, *, metadata_manager=None, settings_manager=None):
    monkeypatch.setattr(clip_grid, 'get_cached_clip_metadata',
                        lambda *_: (10, 1920, 1080, 30, 1000000, 1000000))
    for name in ('_set_media_source', '_discover_audio_sources_async',
                 '_prepare_playback_source', '_load_timeline_thumbnails_if_idle'):
        monkeypatch.setattr(clip_viewer.ClipViewer, name, lambda *_: None)
    window = ClipEditorWindow(str(tmp_path / 'first.mp4'), None,
                              metadata_manager=metadata_manager,
                              settings_manager=settings_manager)
    qtbot.addWidget(window)
    return window


def test_discord_picker_opens_visible_share_and_exports(monkeypatch, qtbot, tmp_path):
    import subprocess
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton
    from core import export_queue, export_encoding
    from core.ffmpeg_tools import get_ffmpeg_exe
    window = shell(monkeypatch, qtbot, tmp_path)
    ffmpeg = get_ffmpeg_exe()
    source = Path(window.clip_path)
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x240:rate=12:duration=1', '-c:v', 'ffv1',
                    str(source.with_suffix('.mkv'))], check=True)
    window.replace_clip(str(source.with_suffix('.mkv')), autoplay=False)
    monkeypatch.setattr(clip_viewer, 'clips_directory_from', lambda *_: tmp_path)
    monkeypatch.setattr(clip_viewer.ShareWindow, '_make_thumbnail', lambda *_: None)
    monkeypatch.setattr(export_queue, 'choose_video_args',
                        lambda ff, quality, bitrate, **kw: export_encoding.software_args(quality, ff, bitrate))
    window.show()
    page = window.clip_page
    page._show_share_overlay()
    picker = page.findChild(clip_viewer.ShareModeDialog)
    picker._quality_combo.setCurrentIndex(picker._quality_combo.findData('quality'))
    opened_after_close = []
    original = page._create_share_window
    def open_share(*args):
        opened_after_close.append(not picker.isVisible())
        original(*args)
    page._create_share_window = open_share
    discord = next(button for button in picker.findChildren(QPushButton) if 'Discord' in button.text())
    qtbot.mouseClick(discord, Qt.MouseButton.LeftButton)
    assert opened_after_close == [True]
    share = next(iter(clip_viewer._share_windows))
    try:
        assert share.isVisible()
        assert share.windowType() == Qt.WindowType.Dialog
        assert share._discord_mode
        qtbot.waitUntil(lambda: share._export_thread is not None and not share._export_thread.is_alive(), timeout=10000)
        qtbot.wait(50)
        assert share._export_path is not None, (share._fname_lbl.text(), share._fname_lbl.toolTip())
        assert Path(share._export_path).stat().st_size < 20 * 1024 * 1024
        window.close()
        assert share.isVisible()
    finally:
        share.close()


def test_editor_controls_order_and_maximize_behavior(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    header = page._editor_header.layout()
    close = page._editor_header.findChild(clip_viewer.IconButton, 'editorClose')
    assert header.indexOf(page.minimize_btn) < header.indexOf(page.maximize_btn) < header.indexOf(close)
    window.show()
    page.maximize_btn.click()
    assert window.isMaximized() and not window.isFullScreen()
    page.maximize_btn.click()
    assert not window.isMaximized() and not window.isFullScreen()
    window.close()


def test_share_start_failure_is_visible(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    messages = []
    def fail(*_):
        raise RuntimeError('Unable to prepare this clip')
    monkeypatch.setattr(page, '_create_share_window', fail)
    monkeypatch.setattr(clip_viewer.FthrMessageDialog, 'warning',
                        lambda parent, title, detail: messages.append((title, detail)))
    page._open_share_window(True)
    assert messages == [('Share could not start', 'Unable to prepare this clip')]
    window.close()


def test_switch_and_close_do_not_cancel_application_exports(monkeypatch, qtbot, tmp_path):
    from core import export_queue
    from core.export_lifecycle import ExportResult, ExportState
    release = threading.Event()
    seen = []
    def run(template, *args, **kwargs):
        release.wait(5)
        seen.append((template, kwargs['cancel'].is_set()))
        return ExportResult(ExportState.COMPLETED)
    monkeypatch.setattr(export_queue, 'run_export', run)
    monkeypatch.setattr(clip_viewer, 'clips_directory_from', lambda *_: tmp_path)
    window = shell(monkeypatch, qtbot, tmp_path)
    window.clip_page._crop_rect = (20, 20, 160, 120)
    window.clip_page._export_clip()
    first = export_queue.tasks()[-1]
    assert window.replace_clip(str(tmp_path / 'second.mp4'), autoplay=False)
    window.clip_page._crop_rect = (0, 0, 200, 100)
    window.clip_page._export_clip()
    second = export_queue.tasks()[-1]
    window.close()
    release.set()
    first.thread.join(3)
    second.thread.join(3)
    assert first.source != second.source
    assert all(not cancelled for _, cancelled in seen)
    assert any('crop=160:120:20:20' in ' '.join(cmd) for cmd, _ in seen)
    assert any('crop=200:100:0:0' in ' '.join(cmd) for cmd, _ in seen)


def test_repeated_switches_keep_controls_and_reset_edits(monkeypatch, qtbot, tmp_path):
    from core.clip_metadata_manager import ClipMetadataManager

    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    window = shell(monkeypatch, qtbot, tmp_path, metadata_manager=manager)
    page = window.clip_page
    controls = (page.play_btn, page.trim_slider, page.video_widget, page._native_video_widget)
    original_widgets = len(page.findChildren(QWidget))
    for index in range(15):
        old_token = page._prepare_cancel
        page._effects['contrast'] = 42
        page.trim_slider.start_pct = .25
        window.replace_clip(str(tmp_path / f'clip-{index}.mp4'),
                            autoplay=False, linked_import=index % 2 == 0)
        assert window.clip_page is page
        assert controls == (page.play_btn, page.trim_slider, page.video_widget,
                            page._native_video_widget)
        assert old_token.is_set()
        assert page._effects['contrast'] == 0 and page.trim_slider.start_pct == 0
        assert page.delete_btn.isEnabled() == (index % 2 == 1)
        qtbot.wait(1)
    assert len(page.findChildren(QWidget)) == original_widgets
    from ui.keyboard_navigation import KeyboardNavigation
    assert len(page.findChildren(KeyboardNavigation)) == 1
    window._teardown_player(wait_for_workers=False)
    assert not any(timer.isActive() for timer in page.findChildren(QTimer))


def test_uncommitted_metadata_is_saved_to_outgoing_clip(monkeypatch, qtbot, tmp_path):
    from core.clip_metadata_manager import ClipMetadataManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    source = tmp_path / 'first.mp4'
    source.write_bytes(b'synthetic editor media')
    (tmp_path / 'next.mp4').write_bytes(b'synthetic next editor media')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate(target_version=5)
    window = shell(monkeypatch, qtbot, tmp_path, metadata_manager=manager)
    old_path = window.clip_path
    window._tag_edit.insert('Keep this tag')
    window._desc_edit.insert('Outgoing description')
    window.replace_clip(str(tmp_path / 'next.mp4'), metadata_manager=manager)
    assert manager.get(old_path) == {'tag': 'Keep this tag', 'description': 'Outgoing description'}
    assert window._tag_edit.text() == '' and window._desc_edit.text() == ''
    assert manager.get(window.clip_path)['tag'] == ''


def test_retired_callbacks_cannot_write_into_next_clip(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    calls = []
    signals = clip_viewer._ClipMetadataWorker(page.clip_path, page._prepare_cancel).signals
    page._connect_clip_result(signals.ready, lambda value: calls.append(value))
    # Queue a worker result before replacement but deliver it afterwards.
    worker = threading.Thread(target=lambda: signals.ready.emit('old'))
    worker.start()
    worker.join()
    page._later(0, lambda: calls.append('old timer'))
    old_token = page._prepare_cancel
    window.replace_clip(str(tmp_path / 'next.mp4'), autoplay=False)
    page._on_audio_sources_discovered((), 'old failure', old_token)
    qtbot.wait(5)
    assert calls == []
    assert page._player_lifecycle_state != clip_viewer.PlayerLifecycleState.FAILED


def test_hash_cancellation_stops_between_blocks(monkeypatch, tmp_path):
    cancel = threading.Event()
    reads = []

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, count):
            reads.append(count)
            cancel.set()
            return b'first block'

    monkeypatch.setattr(Path, 'open', lambda *_: Reader())
    import pytest
    with pytest.raises(InterruptedError):
        audio_manifest.media_sha256(tmp_path / 'large.mp4', cancel)
    assert reads == [1024 * 1024]


def test_metadata_probe_uses_owned_cancellable_runner(monkeypatch):
    from core import media_process
    cancel = threading.Event()
    calls = []
    monkeypatch.setattr(media_metadata, 'get_ffprobe_exe', lambda: 'ffprobe')
    monkeypatch.setattr(media_process, 'run_media_process',
                        lambda command, token, **kwargs: calls.append(token))
    assert media_metadata.probe_video_metadata('clip.mp4', cancelled=cancel) is None
    assert calls == [cancel]


def test_first_frame_fallback_waits_for_native_playback(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    calls = []
    monkeypatch.setattr(page, '_update_video_renderer', lambda: None)
    monkeypatch.setattr(page, '_update_playback_readiness', lambda: None)
    # Buffered status can arrive before autoplay has started: do not decode immediately.
    page._on_media_status_changed(QMediaPlayer.MediaStatus.BufferedMedia)
    assert page._initial_preview_timer.isActive()
    assert page._initial_frame_worker is None
    page._native_output_enabled = True
    page._native_has_real_frame = True
    monkeypatch.setattr(clip_viewer, '_InitialFrameDecodeWorker',
                        lambda *_: calls.append(True))
    qtbot.wait(350)
    assert calls == []


@pytest.mark.parametrize('maximized', [False, True])
def test_fullscreen_navigation_preserves_restore_state(monkeypatch, qtbot, tmp_path, maximized):
    window = shell(monkeypatch, qtbot, tmp_path)
    window.resize(1100, 700)
    window.showMaximized() if maximized else window.show()
    qtbot.wait(30)
    page = window.clip_page
    geometry = window.geometry()
    page._toggle_fullscreen()
    qtbot.wait(30)
    window.replace_clip(str(tmp_path / 'next.mp4'), autoplay=False)
    assert window.isFullScreen()
    page._exit_fullscreen()
    qtbot.wait(30)
    assert window.isMaximized() == maximized
    if not maximized:
        assert window.geometry() == geometry


def test_quick_crop_feedback_resets_after_switch_and_repeated_saves(monkeypatch, qtbot, tmp_path):
    from core.settings_manager import SettingsManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    page.sm = SettingsManager()
    callbacks = []
    monkeypatch.setattr(page, '_later', lambda delay, callback: callbacks.append(callback))
    page._crop_rect = (0, 0, 100, 100)
    page._save_quick_crop()
    page._save_quick_crop()
    callbacks[-1]()
    assert page.save_qc_btn.text() == 'Save as quick crop'
    page._save_quick_crop()
    window.replace_clip(str(tmp_path / 'next.mp4'), autoplay=False)
    assert page.save_qc_btn.text() == 'Save as quick crop'


def test_rapid_navigation_opens_every_clip_without_recapturing(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    window.show()
    monkeypatch.setattr(window, 'grab', lambda: pytest.fail('editor page was snapshotted'))
    for index in range(4):
        path = str(tmp_path / f'next-{index}.mp4')
        assert window.replace_clip(path, autoplay=False)
        assert window.clip_path == path
        assert window.clip_page.geometry() == window.contentsRect()


def test_proxy_waits_for_idle_cancels_on_play_and_rejects_retired_result(
        monkeypatch, qtbot, tmp_path):
    prepare = clip_viewer.ClipViewer._prepare_playback_source
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    monkeypatch.setattr(page, '_prepare_playback_source', prepare.__get__(page))
    workers = []
    monkeypatch.setattr(clip_viewer.QThreadPool.globalInstance(), 'start', workers.append)
    state = [QMediaPlayer.PlaybackState.PlayingState]
    monkeypatch.setattr(page.player, 'playbackState', lambda: state[0])
    page._media_ready = True
    page._play_when_ready = False
    page._prepare_playback_source()
    assert workers == []
    state[0] = QMediaPlayer.PlaybackState.PausedState
    page._on_state_changed(state[0])
    assert page._proxy_prepare_timer.isActive()
    page._prepare_playback_source()
    retired = workers[-1]
    retired_token = page._proxy_cancel
    state[0] = QMediaPlayer.PlaybackState.PlayingState
    page._on_state_changed(state[0])
    assert retired_token.is_set() and not page._proxy_prepare_timer.isActive()
    state[0] = QMediaPlayer.PlaybackState.PausedState
    page._on_state_changed(state[0])
    page._on_seek_requested(.5)
    assert page._proxy_prepare_timer.remainingTime() > 1000
    page._prepare_playback_source()
    current = page._playback_source_worker
    retired.signals.ready.emit('obsolete-proxy.mp4')
    assert page._playback_source_worker is current and not page._proxy_checked
    assert page._playback_path == page.clip_path
    current.signals.ready.emit(page.clip_path)
    assert page._proxy_checked
    token = page._proxy_cancel
    window.replace_clip(str(tmp_path / 'next.mp4'), autoplay=False)
    assert token.is_set() and not page._proxy_checked


def test_preview_quality_switch_preserves_playing_position_edits_and_setting(
        monkeypatch, qtbot, tmp_path):
    from core import playback_proxy

    class Settings:
        values = {}
        saves = 0

        def get(self, key, default=None):
            return self.values.get(key, default)

        def set(self, key, value):
            self.values[key] = value

        def save_settings(self):
            self.saves += 1

    settings = Settings()
    monkeypatch.setattr(playback_proxy, 'cached_playback_path', lambda path, mode:
                        path if mode == 'original' else f'{path}-{mode}.mp4')
    window = shell(monkeypatch, qtbot, tmp_path, settings_manager=settings)
    page = window.clip_page
    page._compatibility_checked = True
    page._crop_rect = (200, 100, 1200, 800)
    page._effects['contrast'] = 20
    page.trim_slider.start_pct = .15
    edits = page._capture_editor_state()
    source_size = (page._src_w, page._src_h)
    page._latest_paused_seek_ms = 1800
    page._playback_source_worker = object()
    retired = page._proxy_cancel
    monkeypatch.setattr(page.player, 'playbackState',
                        lambda: QMediaPlayer.PlaybackState.PlayingState)
    monkeypatch.setattr(page.player, 'position', lambda: 3800)
    selected = []
    monkeypatch.setattr(page, '_set_media_source', lambda: selected.append(page._playback_path))

    quality = page.preview_quality_combo
    quality.setCurrentIndex(quality.findData('540p'))

    assert selected == [f'{page.clip_path}-540p.mp4']
    assert page._renderer_resume_position_ms == 3800
    assert page._play_when_ready and retired.is_set()
    assert page.video_widget._playback_height_limit == 540
    assert page._capture_editor_state() == edits
    assert (page._src_w, page._src_h) == source_size
    assert settings.get('editor_preview_quality') == '540p' and settings.saves == 1

    next_path = str(tmp_path / 'next.mp4')
    window.replace_clip(next_path, autoplay=False)

    assert page.preview_quality_combo is quality
    assert quality.currentData() == '540p' and settings.saves == 1
    assert page._playback_path == f'{next_path}-540p.mp4'


def test_original_preview_rebuilds_evicted_compatibility_copy(monkeypatch, qtbot, tmp_path):
    from core import playback_proxy

    monkeypatch.setattr(playback_proxy, 'cached_playback_path', lambda path, mode:
                        path if mode == 'original' else f'{path}-proxy.mp4')
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    page._compatibility_checked = True
    workers = []
    monkeypatch.setattr(clip_viewer.QThreadPool.globalInstance(), 'start', workers.append)
    monkeypatch.setattr(page.player, 'position', lambda: 2800)
    quality = page.preview_quality_combo
    quality.setCurrentIndex(quality.findData('original'))

    worker = workers[-1]
    assert page._compatibility_worker is worker
    assert page._playback_path.endswith('-proxy.mp4')
    worker.signals.ready.emit('rebuilt-compatibility.mp4')
    assert page._playback_path == 'rebuilt-compatibility.mp4'
    assert page._renderer_resume_position_ms == 2800


def test_failed_performance_preview_falls_back_to_compatible_source(
        monkeypatch, qtbot, tmp_path):
    from core import playback_proxy

    compatible = str(tmp_path / 'compatible.mp4')
    proxy = str(tmp_path / 'bad-proxy.mp4')
    monkeypatch.setattr(playback_proxy, 'cached_playback_path',
                        lambda _path, mode: compatible if mode == 'original' else proxy)
    removed = []
    monkeypatch.setattr(playback_proxy, 'discard_playback_cache',
                        lambda path, candidate: removed.append((path, candidate)))
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    page._compatibility_checked = True
    page._play_when_ready = True
    page._renderer_resume_position_ms = 5200

    page._on_player_error()
    qtbot.waitUntil(lambda: page._playback_path == compatible)

    assert removed == [(page.clip_path, proxy)]
    assert page._renderer_resume_position_ms == 5200
    assert page._play_when_ready and page._proxy_checked


def test_prepared_preview_is_applied_after_trim_handle_release(monkeypatch, qtbot, tmp_path):
    window = shell(monkeypatch, qtbot, tmp_path)
    page = window.clip_page
    # Proxy generation starts only after compatibility preparation; model that
    # completed precondition and cancel the shell's initial background probe.
    page._compatibility_checked = True
    page._cancel_compatibility_prepare()
    page.trim_slider.dragging = 'start'
    page._on_playback_source_ready('preview.mp4')
    assert page._prepared_playback_path == 'preview.mp4'
    assert page._playback_path == page.clip_path

    page.trim_slider.dragging = None
    page.trim_slider.range_edit_finished.emit()
    qtbot.waitUntil(lambda: page._playback_path == 'preview.mp4')
