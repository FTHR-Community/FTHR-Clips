"""The optional warm player must yield to capture and library mutations."""

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QPoint, QRect

from core.ui_performance import DEFAULTS, PRESETS
from ui.editor_preload_policy import EditorPreloadPolicy


@pytest.fixture
def policy(qtbot):
    owner = QObject()
    owner.settings_manager = {'editor_fast_open_enabled': True}
    owner.isVisible = lambda: True
    owner.isMinimized = lambda: False
    owner._file_operations_ready = True
    owner._manual_record_state = 'idle'
    owner.main_stack = SimpleNamespace(currentIndex=lambda: 0)
    owner.clip_grid = SimpleNamespace(_thumb_widgets={})
    owner._clip_readiness = SimpleNamespace(can_access=lambda path: True)
    owner._save_state = SimpleNamespace(is_busy=lambda: False)
    result = EditorPreloadPolicy(owner)
    calls = []
    result._loader = SimpleNamespace(
        request=lambda path, **kw: calls.append((path, kw)),
        clear=lambda: calls.append('clear'),
        keep_alive=lambda path: True,
        take=lambda path: ('prepared', path))
    yield result, owner, calls
    result.shutdown()


def test_preload_is_opt_in_even_for_presets():
    assert DEFAULTS['editor_fast_open_enabled'] is False
    assert all(not preset['editor_fast_open_enabled'] for preset in PRESETS.values())


def test_hover_debounces_latest_and_preserves_quality(policy, qtbot):
    preload, owner, calls = policy
    owner.settings_manager['editor_preview_quality'] = '540p'
    preload.request('first.mp4')
    preload.request('second.mp4')
    qtbot.waitUntil(lambda: bool(calls))
    assert calls == [('second.mp4', {'preview_quality': '540p'})]


@pytest.mark.parametrize('reason', ['disabled', 'background', 'recording', 'save',
                                  'editor', 'settings', 'recovery', 'bulk', 'shutdown'])
def test_busy_or_hidden_app_releases_speculative_work(policy, reason):
    preload, owner, calls = policy
    if reason == 'disabled':
        owner.settings_manager['editor_fast_open_enabled'] = False
    elif reason == 'background':
        owner._background_ui_paused = True
    elif reason == 'recording':
        owner._manual_record_state = 'preparing'
    elif reason == 'save':
        owner._save_state.is_busy = lambda: True
    elif reason == 'editor':
        owner._active_clip_viewer = object()
    elif reason == 'settings':
        owner.main_stack.currentIndex = lambda: 1
    elif reason == 'recovery':
        owner._file_operations_ready = False
    elif reason == 'bulk':
        owner.clip_grid._bulk_job = object()
    else:
        owner._shutdown_requested = True
    preload._tick()
    preload.request('clip.mp4')
    assert calls == ['clear']
    assert not preload._debounce.isActive()


def test_unready_clip_is_not_preloaded(policy):
    preload, owner, calls = policy
    owner._clip_readiness.can_access = lambda path: False
    preload.request('saving.mp4')
    preload._prepare_candidate()
    assert calls == []


def test_speculation_yields_to_cold_thumbnail_work(policy):
    preload, owner, calls = policy
    owner.clip_grid._thumbnail_jobs_inflight = {'clip.mp4'}
    preload.request('clip.mp4')
    preload._prepare_candidate()
    assert calls == [] and preload._candidate == 'clip.mp4'
    owner.clip_grid._thumbnail_jobs_inflight.clear()
    preload._prepare_candidate()
    assert calls == [('clip.mp4', {'preview_quality': 'auto'})]


def test_scroll_defers_hover_and_resumes_the_visible_candidate(policy):
    preload, owner, calls = policy
    owner.clip_scroll_area = SimpleNamespace(
        viewport=lambda: SimpleNamespace(rect=lambda: QRect(0, 0, 500, 500)))
    owner.clip_grid._thumb_widgets = {'settled.mp4': SimpleNamespace(
        is_video=True, ready=True, _mutation_busy=False, isVisible=lambda: True,
        rect=lambda: QRect(0, 0, 100, 100), mapTo=lambda *args: QPoint(0, 0))}
    preload._loader.cancel_unprepared = lambda: calls.append('cancel pending') or True
    preload.request('old.mp4')
    owner.clip_grid._scrolling = True
    preload.scroll_activity_changed(True)
    preload.request('passing.mp4')
    preload._tick()
    assert not preload._debounce.isActive()
    assert calls == ['cancel pending']
    owner.clip_grid._scrolling = False
    preload.scroll_activity_changed(False)
    preload._prepare_candidate()
    assert calls[-1] == ('settled.mp4', {'preview_quality': 'auto'})


def test_take_transfers_once_and_clears_other_work(policy):
    preload, owner, calls = policy
    assert preload.take('clip.mp4') == ('prepared', 'clip.mp4')
    assert calls == ['clear']
    owner.settings_manager['editor_fast_open_enabled'] = False
    assert preload.take('clip.mp4') is None


def test_mutation_suspends_new_requests(policy):
    preload, owner, calls = policy
    preload.clear(suspend=True)
    preload.request('renaming.mp4')
    assert not preload._debounce.isActive()
    assert calls == ['clear']


def test_visible_candidate_follows_scroll_without_overriding_hover(policy):
    preload, owner, calls = policy
    viewport = SimpleNamespace(rect=lambda: QRect(0, 0, 500, 500))
    owner.clip_scroll_area = SimpleNamespace(viewport=lambda: viewport)
    def card(y):
        return SimpleNamespace(is_video=True, ready=True, _mutation_busy=False,
                               isVisible=lambda: True, rect=lambda: QRect(0, 0, 100, 100),
                               mapTo=lambda viewport, point: QPoint(0, y))
    owner.clip_grid._thumb_widgets = {'first.mp4': card(0), 'second.mp4': card(150)}
    preload._tick()
    assert preload._candidate == 'first.mp4'
    preload._prepare_candidate()
    preload.request('second.mp4')
    preload._prepare_candidate()
    preload._tick()
    assert preload._candidate is None
    owner.clip_grid._thumb_widgets = {'second.mp4': card(-200), 'third.mp4': card(0)}
    preload._tick()
    assert preload._candidate == 'third.mp4'


def test_missing_visible_preload_retries_after_backoff(policy):
    preload, owner, calls = policy
    owner.clip_scroll_area = SimpleNamespace(
        viewport=lambda: SimpleNamespace(rect=lambda: QRect(0, 0, 500, 500)))
    owner.clip_grid._thumb_widgets = {'clip.mp4': SimpleNamespace(
        is_video=True, ready=True, _mutation_busy=False, isVisible=lambda: True,
        rect=lambda: QRect(0, 0, 100, 100), mapTo=lambda *args: QPoint(0, 0))}
    preload._last_requested = 'clip.mp4'
    preload._last_requested_at = 0.0
    preload._loader.keep_alive = lambda path: False
    preload._tick()
    assert preload._candidate == 'clip.mp4'


def test_setting_persists_and_releases_preload(qtbot, monkeypatch, tmp_path):
    from pathlib import Path
    from core.settings_manager import SettingsManager
    from main import _SettingsPage
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda *_: None)
    sm = SettingsManager()
    page = _SettingsPage(sm)
    qtbot.addWidget(page)
    page._ensure_category(5)
    check = page._ui_performance_checks['editor_fast_open_enabled']
    assert not check.isChecked()
    calls = []
    page._editor_preload = SimpleNamespace(refresh=lambda: calls.append('refresh'))
    check.setChecked(True)
    assert SettingsManager().get('editor_fast_open_enabled') is True
    check.setChecked(False)
    assert SettingsManager().get('editor_fast_open_enabled') is False
    assert calls == ['refresh', 'refresh']
