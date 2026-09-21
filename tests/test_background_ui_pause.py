from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest


class _PauseTarget:
    def __init__(self):
        self.states: list[bool] = []

    def set_background_paused(self, paused: bool) -> None:
        self.states.append(bool(paused))


class _SettingsPauseTarget:
    def __init__(self):
        self.states: list[bool] = []

    def set_background_ui_paused(self, paused: bool) -> None:
        self.states.append(bool(paused))


def test_background_ui_pause_is_enabled_by_default(tmp_path, monkeypatch):
    from core.settings_manager import SettingsManager

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)

    assert SettingsManager().get('pause_ui_in_background') is True


def test_performance_toggle_does_not_change_card_or_sound_options(
        qtbot, tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    settings = SettingsManager()
    settings.set('capture_card_enabled', False)
    settings.set('notification_sounds_enabled', True)
    settings.set('sound_volume_clip', 37)
    settings.set('sound_volume_screenshot', 18)
    settings.save_settings()

    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    changed: list[bool] = []
    page.background_ui_pause_changed.connect(changed.append)

    assert page.performance_background_pause_check.isChecked()
    page.performance_background_pause_check.setChecked(False)

    assert changed == [False]
    assert settings.get('pause_ui_in_background') is False
    assert settings.get('capture_card_enabled') is False
    assert settings.get('notification_sounds_enabled') is True
    assert settings.get('sound_volume_clip') == 37
    assert settings.get('sound_volume_screenshot') == 18


def test_clip_viewer_autoplay_toggle_loads_and_persists(
        qtbot, tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    settings = SettingsManager()
    settings.set('clip_viewer_autoplay', False)
    settings.save_settings()

    page = _SettingsPage(settings)
    qtbot.addWidget(page)

    assert page.performance_clip_autoplay_check.isChecked() is False
    page.performance_clip_autoplay_check.setChecked(True)

    assert settings.get('clip_viewer_autoplay') is True
    settings.save_settings()
    assert SettingsManager().get('clip_viewer_autoplay') is True


def test_clip_grid_coalesces_refreshes_until_foreground(
        qtbot, tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from ui.clip_grid import ClipGrid

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    settings = SettingsManager()
    grid = ClipGrid(settings)
    qtbot.addWidget(grid)
    loads: list[str] = []
    monkeypatch.setattr(grid, '_load_clips', lambda: loads.append('load'))

    grid.set_background_paused(True)
    grid.force_refresh()

    assert not grid.refresh_timer.isActive()
    assert grid._background_refresh_pending
    assert loads == []

    grid.set_background_paused(False)

    assert grid.refresh_timer.isActive()
    assert not grid._background_refresh_pending
    assert loads == ['load']


def test_application_inactive_state_drives_the_saved_pause_preference(qtbot):
    from PySide6.QtCore import Qt
    from main import MainWindow

    applied: list[bool] = []
    settings = {'pause_ui_in_background': True}
    host = SimpleNamespace(
        _background_start=False,
        settings_manager=SimpleNamespace(
            get=lambda key, default=None: settings.get(key, default)),
        isVisible=lambda: True,
        isMinimized=lambda: False,
        _apply_background_ui_paused=lambda paused: applied.append(bool(paused)),
    )

    MainWindow._refresh_background_ui_pause_state(
        host, Qt.ApplicationState.ApplicationInactive)
    settings['pause_ui_in_background'] = False
    MainWindow._refresh_background_ui_pause_state(
        host, Qt.ApplicationState.ApplicationInactive)

    assert applied == [True, False]


def test_game_clip_saved_in_background_appears_on_return_without_another_save(
        qtbot, tmp_path, monkeypatch):
    from ui.clip_grid import ClipGrid

    settings = SimpleNamespace(get=lambda key, default=None: {
        'clips_directory': str(tmp_path), 'imported_clip_folders': [],
    }.get(key, default))
    monkeypatch.setattr(ClipGrid, '_start_thumbnail_worker', lambda *_: None)
    grid = ClipGrid(settings)
    qtbot.addWidget(grid)
    try:
        qtbot.waitUntil(lambda: grid._active_scan_worker is None)
        game_dir = tmp_path / 'VALORANT'
        game_dir.mkdir()
        for cycle in range(3):
            grid.set_background_paused(True)
            clip = game_dir / f'game-{cycle}.mp4'
            clip.write_bytes(b'completed clip fixture')
            grid.upsert_saved_clip(str(clip), ready=False)
            grid.upsert_saved_clip(str(clip), ready=True)
            assert str(clip) not in grid._thumb_widgets

            grid.set_background_paused(False)
            qtbot.waitUntil(lambda: grid._active_scan_worker is None)
            assert str(clip) in grid._thumb_widgets
            assert grid._thumb_widgets[str(clip)].ready
            assert len(grid._records) == cycle + 1
            assert not grid._pending_saved_clips
    finally:
        grid.shutdown()


def test_background_pause_leaves_core_timer_card_and_sound_state_alone(qtbot):
    from PySide6.QtCore import QTimer
    from main import MainWindow

    core_timer = QTimer()
    core_timer.start(500)
    grid = _PauseTarget()
    settings_page = _SettingsPauseTarget()
    options = {
        'capture_card_enabled': False,
        'notification_sounds_enabled': True,
        'sound_volume_clip': 42,
    }
    ui_updates: list[bool] = []
    hotkey_state = {'registered': True}
    host = SimpleNamespace(
        _background_ui_paused=False,
        _first_frame_painted=True,
        _ui_ready=True,
        _pending_status_display=None,
        clip_grid=grid,
        _settings_page_widget=settings_page,
        status_timer=core_timer,
        capture_card=object(),
        hotkey_manager=hotkey_state,
        settings_manager=options,
        setUpdatesEnabled=lambda enabled: ui_updates.append(bool(enabled)),
        update=lambda: None,
    )

    MainWindow._apply_background_ui_paused(host, True)

    assert ui_updates == [False]
    assert grid.states == [True]
    assert settings_page.states == [True]
    assert core_timer.isActive()
    assert hotkey_state == {'registered': True}
    assert options == {
        'capture_card_enabled': False,
        'notification_sounds_enabled': True,
        'sound_volume_clip': 42,
    }
    core_timer.stop()


def test_thumbnail_cancel_resume_re_enriches_card_and_allows_open(
        qtbot, tmp_path, monkeypatch):
    """A cancelled thumbnail must not strand an otherwise valid library card."""
    from PySide6.QtCore import QTimer, QThreadPool, QRunnable, Qt
    from PySide6.QtWidgets import QApplication
    from ui import clip_grid as clip_grid_module
    from ui.clip_grid import ClipGrid, ClipThumbnail
    from core.library_index import LibraryRecord, canonical_media_path

    QApplication.instance() or QApplication([])
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'fixture')
    media_path = str(media)

    class _FakeThumbnailWorker(QRunnable):
        workers = []

        def __init__(self, file_path, cancel_event):
            super().__init__()
            self.file_path = file_path
            self.cancel_event = cancel_event
            self.signals = clip_grid_module._ThumbnailSignals()
            self.setAutoDelete(False)
            self.workers.append(self)

        def run(self):
            # The test drives result/cancellation signals explicitly.
            return None

    monkeypatch.setattr(clip_grid_module, '_ThumbnailWorker', _FakeThumbnailWorker)

    card = ClipThumbnail(media_path, is_video=True, ready=True)
    qtbot.addWidget(card)
    card.show()
    opened: list[str] = []
    card.opened.connect(lambda path, *_args: opened.append(path))

    grid = ClipGrid.__new__(ClipGrid)
    grid._shutdown_started = False
    grid._background_paused = False
    grid._background_refresh_pending = False
    grid._scan_generation = 0
    grid._active_scan_worker = None
    grid._pending_saved_clips = {}
    grid._thread_pool = QThreadPool()
    grid._thumbnail_cancel_event = threading.Event()
    grid._thumbnail_generation = 0
    grid._thumbnail_jobs_inflight = set()
    grid._thumbnail_job_fingerprints = {}
    grid._thumbnail_job_owners = {}
    grid._thumbnail_workers = {}
    grid._thumbnail_jobs_submitted = 0
    grid._thumbnail_jobs_completed = 0
    grid._thumbnail_diagnostics_completed = 0
    grid._thumbnail_total_elapsed_ms = 0
    grid._metadata_total_elapsed_ms = 0
    grid._thumbnail_cache_hits = 0
    grid._thumbnail_queue_peak = 0
    grid._thumb_widgets = {media_path: card}
    grid._records = {
        canonical_media_path(media_path): LibraryRecord(
            media_path, canonical_media_path(media_path), (), False, 'video',
            media.stat().st_size, media.stat().st_mtime_ns)
    }
    grid._imported_files = set()
    grid.refresh_timer = QTimer()
    grid._debounce_timer = QTimer()
    loads: list[str] = []
    grid._load_clips = lambda: loads.append('scan')

    assert card.ready is True
    assert card._thumbnail_ready is False
    assert media_path in grid._thumb_widgets
    grid._start_thumbnail_worker(media_path)
    assert grid._thumbnail_job_owners
    assert len(_FakeThumbnailWorker.workers) == 1
    assert grid._thumbnail_jobs_inflight == {media_path}

    grid.set_background_paused(True)
    # Pause cancellation does not revoke ownership from the still-running
    # worker.  Its terminal callback must arrive before a replacement is
    # allowed to start.
    assert grid._thumbnail_jobs_inflight == {media_path}
    grid.set_background_paused(False)

    # Resume does not race the cancelled old generation.
    assert len(_FakeThumbnailWorker.workers) == 1
    old_worker = _FakeThumbnailWorker.workers[0]
    old_worker.signals.finished.emit(media_path, '', 12)
    old_worker.signals.diagnostic_finished.emit(1, 0, True)
    QApplication.processEvents()

    # Once the old owner reaches terminal cleanup, resume owns a fresh
    # generation and requeues the visible card without a library scan.
    assert len(_FakeThumbnailWorker.workers) == 2
    assert loads == []
    replacement = _FakeThumbnailWorker.workers[-1]
    replacement.signals.finished.emit(media_path, '', 12)
    replacement.signals.diagnostic_finished.emit(1, 0, True)
    QApplication.processEvents()

    # Duration survived, but a failed image remains eligible for enrichment.
    assert card._thumbnail_ready is False
    assert card.duration_label.text() == '0:12'
    qtbot.mouseClick(card, Qt.MouseButton.LeftButton, pos=card.rect().center())
    assert opened == [media_path]


@pytest.mark.parametrize('terminal_state', ('success', 'failure', 'cancel'))
def test_thumbnail_terminal_cleanup_releases_only_owned_job(terminal_state):
    from ui.clip_grid import ClipGrid

    grid = ClipGrid.__new__(ClipGrid)
    grid._thumbnail_jobs_inflight = {'clip.mp4'}
    grid._thumbnail_job_fingerprints = {'clip.mp4': 'fp'}
    grid._thumbnail_job_owners = {'clip.mp4': (4, 'fp')}
    grid._thumbnail_workers = {}

    assert grid._finish_thumbnail_job('clip.mp4', 4, 'fp') is True
    assert grid._thumbnail_jobs_inflight == set()
    assert grid._thumbnail_job_fingerprints == {}
    assert grid._thumbnail_job_owners == {}


@pytest.mark.parametrize('terminal_state', ('success', 'failure', 'cancel', 'exception'))
def test_thumbnail_worker_emits_terminal_diagnostic_for_every_exit_path(
        terminal_state, tmp_path, monkeypatch):
    """Worker finally owns terminal cleanup for all real exit paths."""
    from PySide6.QtGui import QImage
    from ui import clip_grid

    media = tmp_path / f'{terminal_state}.mp4'
    media.write_bytes(b'fixture')
    cache = tmp_path / f'{terminal_state}.jpg'
    monkeypatch.setattr(clip_grid, 'THUMB_CACHE_DIR', str(tmp_path))
    monkeypatch.setattr(clip_grid, '_get_cached_thumb_path',
                        lambda _path: str(cache))
    monkeypatch.setattr(clip_grid, 'is_completed_video_path', lambda _path: True)

    cancel_event = threading.Event()
    if terminal_state == 'cancel':
        cancel_event.set()
    elif terminal_state == 'exception':
        monkeypatch.setattr(
            clip_grid, '_probe_with_owned_process',
            lambda *_args: (_ for _ in ()).throw(RuntimeError('probe failed')))
    else:
        monkeypatch.setattr(
            clip_grid, '_probe_with_owned_process',
            lambda _path, _cancel: SimpleNamespace(
                duration_seconds=12.0, width=1920, height=1080,
                average_fps=60.0, video_bitrate_bps=1, total_bitrate_bps=1))
    if terminal_state == 'success':
        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(0xFFFFFFFF)
        monkeypatch.setattr(
            clip_grid, '_decode_thumbnail_with_owned_process',
            lambda _path, _cancel: image)
    elif terminal_state == 'failure':
        monkeypatch.setattr(
            clip_grid, '_decode_thumbnail_with_owned_process',
            lambda _path, _cancel: None)

    worker = clip_grid._ThumbnailWorker(str(media), cancel_event)
    diagnostics: list[tuple] = []
    worker.signals.diagnostic_finished.connect(
        lambda *args: diagnostics.append(args))
    worker.run()

    assert len(diagnostics) == 1


def test_stale_thumbnail_terminal_cannot_release_replacement_job():
    from ui.clip_grid import ClipGrid

    grid = ClipGrid.__new__(ClipGrid)
    grid._thumbnail_jobs_inflight = {'clip.mp4'}
    grid._thumbnail_job_fingerprints = {'clip.mp4': 'new-fp'}
    grid._thumbnail_job_owners = {'clip.mp4': (5, 'new-fp')}
    grid._thumbnail_workers = {}

    assert grid._finish_thumbnail_job('clip.mp4', 4, 'old-fp') is False
    assert grid._thumbnail_jobs_inflight == {'clip.mp4'}
    assert grid._thumbnail_job_owners == {'clip.mp4': (5, 'new-fp')}


def test_current_generation_failure_cleanup_does_not_resubmit():
    """A failed current job is terminal; it must not spin a retry loop."""
    from ui.clip_grid import ClipGrid

    class _FakeThreadPool:
        def activeThreadCount(self):
            return 0

    grid = ClipGrid.__new__(ClipGrid)
    grid._thumbnail_generation = 7
    grid._thumbnail_jobs_inflight = {'clip.mp4'}
    grid._thumbnail_job_fingerprints = {'clip.mp4': 'fp'}
    grid._thumbnail_job_owners = {'clip.mp4': (7, 'fp')}
    grid._thumbnail_workers = {}
    grid._thumbnail_jobs_completed = 0
    grid._thumbnail_diagnostics_completed = 0
    grid._thumbnail_jobs_submitted = 0
    grid._thumbnail_total_elapsed_ms = 0
    grid._metadata_total_elapsed_ms = 0
    grid._thumbnail_cache_hits = 0
    grid._thread_pool = _FakeThreadPool()
    grid._records = {}

    grid._on_thumb_diagnostic(1, 0, False, 'clip.mp4', 7, 'fp')

    assert grid._thumbnail_jobs_inflight == set()
    assert grid._thumbnail_job_owners == {}


def test_repeated_pause_resume_does_not_strand_thumbnail_ownership(
        qtbot, tmp_path, monkeypatch):
    """Each pause/resume cycle gets one replacement job, never duplicates."""
    from PySide6.QtCore import QTimer, QRunnable
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from ui import clip_grid as clip_grid_module
    from ui.clip_grid import ClipGrid, ClipThumbnail
    from core.library_index import LibraryRecord, canonical_media_path

    QApplication.instance() or QApplication([])
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'fixture')
    media_path = str(media)
    thumbnail_path = tmp_path / 'thumb.jpg'
    thumbnail = QImage(8, 8, QImage.Format.Format_RGB32)
    thumbnail.fill(0xFFFFFFFF)
    assert thumbnail.save(str(thumbnail_path), 'JPG')

    class _FakeThumbnailWorker(QRunnable):
        workers = []

        def __init__(self, file_path, cancel_event):
            super().__init__()
            self.signals = clip_grid_module._ThumbnailSignals()
            self.setAutoDelete(False)
            self.workers.append(self)

        def run(self):
            return None

    monkeypatch.setattr(clip_grid_module, '_ThumbnailWorker', _FakeThumbnailWorker)
    card = ClipThumbnail(media_path, is_video=True, ready=True)
    qtbot.addWidget(card)
    grid = ClipGrid.__new__(ClipGrid)
    grid._shutdown_started = False
    grid._background_paused = False
    grid._background_refresh_pending = False
    grid._scan_generation = 0
    grid._active_scan_worker = None
    grid._pending_saved_clips = {}
    class _FakeThreadPool:
        def start(self, _worker):
            return None

        def activeThreadCount(self):
            return 0

    grid._thread_pool = _FakeThreadPool()
    grid._thumbnail_cancel_event = threading.Event()
    grid._thumbnail_generation = 0
    grid._thumbnail_jobs_inflight = set()
    grid._thumbnail_job_fingerprints = {}
    grid._thumbnail_job_owners = {}
    grid._thumbnail_workers = {}
    grid._thumbnail_jobs_submitted = 0
    grid._thumbnail_jobs_completed = 0
    grid._thumbnail_diagnostics_completed = 0
    grid._thumbnail_total_elapsed_ms = 0
    grid._metadata_total_elapsed_ms = 0
    grid._thumbnail_cache_hits = 0
    grid._thumbnail_queue_peak = 0
    grid._thumb_widgets = {media_path: card}
    grid._records = {
        canonical_media_path(media_path): LibraryRecord(
            media_path, canonical_media_path(media_path), (), False, 'video',
            media.stat().st_size, media.stat().st_mtime_ns)
    }
    grid._imported_files = set()
    grid.refresh_timer = QTimer()
    grid._debounce_timer = QTimer()
    grid._load_clips = lambda: None

    assert card.ready is True
    assert card._thumbnail_ready is False
    assert media_path in grid._thumb_widgets
    grid._start_thumbnail_worker(media_path)
    assert grid._thumbnail_job_owners
    for _cycle in range(20):
        grid.set_background_paused(True)
        assert grid._thumbnail_jobs_inflight == {media_path}
        old_worker = _FakeThumbnailWorker.workers[-1]
        grid.set_background_paused(False)
        assert len(_FakeThumbnailWorker.workers) == (2 * _cycle) + 1
        assert grid._thumbnail_jobs_inflight == {media_path}
        # No replacement may run concurrently with the cancelled owner.
        assert grid._thumbnail_job_owners[media_path][0] == _cycle
        old_worker.signals.diagnostic_finished.emit(1, 0, False)
        QApplication.processEvents()
        assert len(_FakeThumbnailWorker.workers) == (2 * _cycle) + 2
        assert len(grid._thumbnail_jobs_inflight) == 1
        replacement = _FakeThumbnailWorker.workers[-1]
        replacement.signals.finished.emit(media_path, str(thumbnail_path), 1)
        replacement.signals.diagnostic_finished.emit(1, 0, False)
        QApplication.processEvents()
        assert not grid._thumbnail_jobs_inflight
        assert not card._thumb_pixmap.isNull()
        card._thumbnail_ready = False
        if _cycle < 19:
            # The next cycle starts with a fresh visible enrichment request;
            # this keeps each cancellation/resume assertion independent while
            # still exercising twenty consecutive cycles.
            grid._start_thumbnail_worker(media_path)
            assert len(grid._thumbnail_jobs_inflight) == 1

    assert len(_FakeThumbnailWorker.workers) == 40


def test_background_pause_waits_for_the_first_painted_frame(qtbot, monkeypatch):
    """A never-painted window must not have updates disabled: on Wayland the
    compositor maps a surface only after its first buffer and grants focus
    only to mapped windows, so pausing first left the app invisible."""
    import main as main_module
    from main import MainWindow

    calls: list[str] = []
    host = SimpleNamespace(
        _first_frame_painted=False,
        _background_ui_pause_deferred=False,
        _background_ui_paused=False,
        _ui_ready=True,
        _pending_status_display=None,
        setUpdatesEnabled=lambda enabled: calls.append(f'updates={enabled}'),
        clip_grid=SimpleNamespace(set_background_paused=lambda p: calls.append(f'grid={p}')),
        _settings_page_widget=SimpleNamespace(
            set_background_ui_paused=lambda p: calls.append(f'settings={p}')),
        update=lambda: calls.append('update'),
        _refresh_background_ui_pause_state=lambda: calls.append('refresh'),
    )
    scheduled: list = []
    monkeypatch.setattr(main_module.QTimer, 'singleShot',
                        staticmethod(lambda _ms, fn: scheduled.append(fn)))

    MainWindow._apply_background_ui_paused(host, True)
    assert calls == []
    assert host._background_ui_pause_deferred is True
    assert host._background_ui_paused is False

    # Unpausing before the first frame is harmless and applies normally.
    MainWindow._apply_background_ui_paused(host, False, force=True)
    assert 'updates=True' in calls
    calls.clear()

    MainWindow._note_first_frame_painted(host)
    assert host._first_frame_painted is True
    assert host._background_ui_pause_deferred is False
    assert len(scheduled) == 1
    scheduled[0]()
    assert calls == ['refresh']

    # Once painted, the pause applies as before.
    MainWindow._apply_background_ui_paused(host, True)
    assert calls[-3:] == ['updates=False', 'grid=True', 'settings=True']
    MainWindow._note_first_frame_painted(host)
    assert len(scheduled) == 1
