"""User-facing playback inventory states and bounded helper integration."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

import main
from core.capture_settings import encode_audio_device_tracks, resolve_audio_plan
from core.settings_manager import SettingsManager


# Save the real method before the page-construction fixtures patch it to avoid
# an unsolicited hardware scan. Tests below call this exact production method.
POPULATE_PLAYBACK_DEVICES = main._SettingsPage._populate_playback_devices


@pytest.fixture
def playback_page(qtbot, monkeypatch, tmp_path):
    if sys.platform != 'win32':
        pytest.skip('Windows playback selector is not exposed on this platform')
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for method in ('_populate_mic_devices', '_refresh_keyboard_windows',
                   '_start_encoder_probe'):
        monkeypatch.setattr(main._SettingsPage, method,
                            lambda *args, **kwargs: None)
    original_single_shot = main.QTimer.singleShot

    def without_initial_playback_scan(delay, callback, *args, **kwargs):
        if getattr(callback, '__name__', '') == '_populate_playback_devices':
            return None
        return original_single_shot(delay, callback, *args, **kwargs)

    monkeypatch.setattr(main.QTimer, 'singleShot',
                        staticmethod(without_initial_playback_scan))
    settings = SettingsManager()
    settings.settings.update({
        'audio_capture_enabled': True,
        'audio_capture_mode': 'combined',
        'audio_source_mode': 'devices',
        'audio_device_tracks': [],
        'audio_device_names': {},
        'playback_device_id': 'saved-main-id',
        'playback_device_name': 'Saved main output',
    })
    assert settings.save_settings()
    page = main._SettingsPage(settings)
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(2)
    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('devices'))
    yield page


def _endpoint(endpoint_id='device-a', name='Device A'):
    return SimpleNamespace(endpoint_id=endpoint_id, display_name=name,
                           device_state=1, is_default=False)


def _inventory(*, endpoints=None):
    endpoints = endpoints or []
    return json.dumps({
        'schema_version': 1,
        'microphones': [
            {'endpoint_id': endpoint_id, 'display_name': name,
             'device_state': 1, 'is_default': False}
            for endpoint_id, name in endpoints
        ],
    })


def _start_production_scan(page, qtbot, monkeypatch, runner, *, engine=True):
    calls = []
    if engine:
        page.engine_path = r'C:\isolated\FTHRclips.exe'
    else:
        page.engine_path = None

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return runner(args, **kwargs)

    monkeypatch.setattr(main.subprocess, 'run', fake_run)
    POPULATE_PLAYBACK_DEVICES(page)
    qtbot.waitUntil(lambda: not page._playback_scan_running, timeout=3000)
    return calls


@pytest.mark.parametrize('failure', ['no-engine', 'nonzero', 'invalid-json', 'timeout'])
def test_failed_or_unavailable_scan_is_error_not_empty_inventory(
        playback_page, qtbot, monkeypatch, failure):
    page = playback_page
    previous_tracks = [
        {'id': 'saved-a', 'name': 'Chat'},
        {'id': 'saved-b', 'name': 'Game'},
    ]
    page.sm.set('audio_device_tracks', previous_tracks)
    page.sm.set('audio_device_names', {'saved-a': 'Chat output',
                                      'saved-b': 'Game output'})

    def outcome(_args, **_kwargs):
        if failure == 'nonzero':
            return SimpleNamespace(returncode=2, stdout='[]', stderr='helper failed')
        if failure == 'invalid-json':
            return SimpleNamespace(returncode=0, stdout='not json', stderr='')
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('helper', 5)
        raise AssertionError('No helper call is expected without an engine path')

    calls = _start_production_scan(
        page, qtbot, monkeypatch, outcome, engine=failure != 'no-engine')

    assert page._playback_inventory_state == 'error'
    assert page.sm.get('audio_device_tracks') == previous_tracks
    assert page.sm.get('playback_device_id') == 'saved-main-id'
    assert not page.extra_device_add.isEnabled()
    assert page.extra_audio_inventory_hint.text()
    assert 'not confirmed' in page.extra_audio_inventory_hint.text().lower()
    assert len(calls) == (0 if failure == 'no-engine' else 1)


def test_successful_empty_scan_is_ready_empty_and_explicitly_has_no_mix_fallback(
        playback_page, qtbot, monkeypatch):
    page = playback_page
    calls = _start_production_scan(
        page, qtbot, monkeypatch,
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=_inventory(), stderr=''))

    assert len(calls) == 1
    assert calls[0][0] == [page.engine_path, '--list-playback-devices']
    assert calls[0][1]['timeout'] == 5
    assert page._playback_inventory_state == 'ready'
    assert page._playback_endpoints == []
    assert not page.extra_device_add.isEnabled()
    hint = page.audio_source_hint.text().lower()
    assert 'system mix' in hint and 'fallback' in hint
    assert ('no' in hint or 'not' in hint or "won't" in hint)
    assert 'manual recording' in hint


def test_split_windows_playback_roles_can_be_saved_together(
        playback_page, qtbot, monkeypatch):
    page = playback_page
    payload = json.dumps({'schema_version': 1, 'microphones': [
        {'endpoint_id': 'game-id', 'display_name': 'Game output',
         'device_state': 1, 'is_default': True, 'is_default_communications': False},
        {'endpoint_id': 'chat-id', 'display_name': 'Chat output',
         'device_state': 1, 'is_default': False, 'is_default_communications': True},
    ]})
    _start_production_scan(
        page, qtbot, monkeypatch, lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=payload, stderr=''))

    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('combined'))
    assert 'Several devices as separate tracks' in page.audio_source_hint.text()
    page.audio_source_combo.setCurrentIndex(page.audio_source_combo.findData('devices'))

    for endpoint_id, role in [('game-id', 'Windows playback default'),
                              ('chat-id', 'Windows communications default')]:
        assert 'capture both' in page.audio_source_hint.text()
        index = page.extra_device_combo.findData(endpoint_id)
        assert index >= 0
        assert role in page.extra_device_combo.itemText(index)
        page.extra_device_combo.setCurrentIndex(index)
        page._on_add_extra_audio_device()

    assert page.sm.get('audio_device_tracks') == [
        {'id': 'game-id', 'name': 'Game output'},
        {'id': 'chat-id', 'name': 'Chat output'},
    ]
    separate, applications, tracks = resolve_audio_plan(
        page.sm.get('audio_source_mode'), page.sm.get('audio_capture_mode'),
        page.sm.get('audio_device_tracks'), page.sm.get('per_app_audio_tracks'),
    )
    assert separate and not applications
    assert encode_audio_device_tracks(tracks) == 'Game output;game-id|Chat output;chat-id'
    assert 'capture both' not in page.audio_source_hint.text()


def test_ready_missing_all_saved_tracks_preserves_ids_and_requested_config(
        playback_page, monkeypatch):
    page = playback_page
    tracks = [
        {'id': 'offline-chat', 'name': 'Chat'},
        {'id': 'offline-game', 'name': 'Game'},
    ]
    page.sm.set('audio_device_tracks', tracks)
    page.sm.set('audio_device_names', {
        'offline-chat': 'Chat output', 'offline-game': 'Game output',
    })
    page._apply_playback_devices([], successful=True)

    labels = [page.extra_audio_devices_box.itemAt(i).layout().itemAt(0).widget().text()
              for i in range(page.extra_audio_devices_box.count())]
    assert len(labels) == 2
    assert all('not found' in label for label in labels)
    assert page.sm.get('audio_device_tracks') == tracks
    assert '2 of 2' in page.extra_audio_inventory_hint.text()

    from core.windows_monitor import MonitorChoice
    monitor = MonitorChoice(
        gdi_name=r'\\.\DISPLAY1', friendly_name='Primary',
        device_path=r'\\?\DISPLAY#PRIMARY\1', primary=True)
    page.sm.set('capture_monitor', monitor.device_path)
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [monitor])
    monkeypatch.setattr(main, 'emit_event', lambda *_args, **_kwargs: None)
    host = SimpleNamespace(
        settings_manager=page.sm, capture_fps=60, clip_duration=30,
        capture_width=1920, capture_height=1080, capture_bitrate=25_000,
        _active_game_window=None)
    config = main.MainWindow._requested_capture_config(host)
    assert config.separate_audio_enabled is True
    assert config.application_audio_tracks_enabled is False
    assert config.audio_device_tracks == encode_audio_device_tracks(tracks)


@pytest.mark.parametrize('state', ['not_checked', 'loading', 'error'])
def test_unconfirmed_inventory_never_claims_missing_single_device_fell_back(
        playback_page, state):
    page = playback_page
    page.sm.set('audio_source_mode', 'device')
    page.sm.set('playback_device_id', 'saved-main-id')
    page.sm.set('playback_device_name', 'Saved main output')
    page._playback_inventory_state = state
    page._refresh_playback_selection()

    text = page.playback_combo.currentText().lower()
    assert 'saved main output' in text
    assert 'availability not confirmed' in text
    assert 'unavailable; using windows default' not in text
    assert 'availability not confirmed' in page.playback_hint.text().lower()


def test_ready_but_missing_single_device_labels_the_real_fallback(
        playback_page):
    page = playback_page
    page.sm.set('audio_source_mode', 'device')
    page._apply_playback_devices([], successful=True)

    assert 'unavailable; using windows default' in page.playback_combo.currentText().lower()


def test_reappearing_device_restores_name_and_selected_id_without_mutating_settings(
        playback_page):
    page = playback_page
    page.sm.set('audio_device_tracks', [{'id': 'saved-a', 'name': 'Chat'}])
    page.sm.set('audio_device_names', {'saved-a': 'Chat output'})
    saved_before = list(page.sm.get('audio_device_tracks'))

    page._apply_playback_devices([], successful=True)
    assert 'not found' in page.extra_audio_devices_box.itemAt(0).layout().itemAt(0).widget().text()
    page._apply_playback_devices([_endpoint('saved-a', 'Chat output')], successful=True)

    label = page.extra_audio_devices_box.itemAt(0).layout().itemAt(0).widget().text()
    assert 'Chat' in label and 'found' in label
    assert page.sm.get('audio_device_tracks') == saved_before


def test_endpoint_name_markup_is_rendered_as_plain_text(playback_page):
    page = playback_page
    page.sm.set('audio_device_tracks', [{'id': 'markup-id', 'name': 'Game'}])
    page._apply_playback_devices(
        [_endpoint('markup-id', '<b>Speaker</b>')], successful=True)

    label = page.extra_audio_devices_box.itemAt(0).layout().itemAt(0).widget()
    assert '<b>Speaker</b>' in label.text()
    assert label.textFormat() == Qt.TextFormat.PlainText


def test_audio_off_keeps_saved_audio_selection_and_reports_it_is_disabled(
        playback_page, qtbot):
    page = playback_page
    saved = [{'id': 'saved-a', 'name': 'Chat'}]
    page.sm.set('audio_device_tracks', saved)
    page._apply_playback_devices([_endpoint('saved-a', 'Chat output')], successful=True)
    page.audio_capture_check.setChecked(False)

    assert page.sm.get('audio_capture_enabled') is False
    assert page.sm.get('audio_device_tracks') == saved
    assert 'audio capture is off' in page.audio_source_hint.text().lower()
    persisted = json.loads(page.sm.config_file.read_text(encoding='utf-8'))
    assert persisted['audio_device_tracks'] == saved
    assert persisted['audio_capture_enabled'] is False


@pytest.mark.parametrize('state', ['error', 'ready'])
def test_refresh_is_visible_and_clickable_in_several_device_mode_after_error_or_ready(
        playback_page, qtbot, monkeypatch, state):
    page = playback_page
    page.window().show()
    page.show()
    qtbot.waitUntil(lambda: page.isVisible(), timeout=1000)
    page._apply_playback_devices(
        [_endpoint('back', 'Speakers')] if state == 'ready' else [],
        successful=(state == 'ready'))
    button = _extra_refresh_button(page)
    assert button is not None
    assert not button.isHidden() and button.isEnabled()
    ancestors = []
    ancestor = button
    while ancestor is not None:
        ancestors.append((ancestor.objectName(), ancestor.isVisible(),
                          ancestor.isHidden(), ancestor.geometry().getRect()))
        ancestor = ancestor.parentWidget()
    assert button.isVisible(), ancestors
    assert button.focusPolicy() != Qt.FocusPolicy.NoFocus

    calls = []
    page.engine_path = page.window().engine_path = r'C:\isolated\FTHRclips.exe'
    monkeypatch.setattr(main.subprocess, 'run', lambda args, **kwargs: (
        calls.append((args, kwargs)) or SimpleNamespace(
            returncode=0,
            stdout=_inventory(endpoints=[('back', 'Speakers')]), stderr='')))
    button.setFocus()
    assert button.focusPolicy() != Qt.FocusPolicy.NoFocus
    assert not page._playback_scan_running
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(calls) == 1, timeout=1000)
    qtbot.waitUntil(lambda: not page._playback_scan_running, timeout=3000)
    assert len(calls) == 1
    assert page._playback_inventory_state == 'ready'

    button = _extra_refresh_button(page)
    assert button is not None and not button.isHidden() and button.isEnabled()
    page._apply_playback_devices([_endpoint('back', 'Speakers')], successful=True)
    assert not button.isHidden() and button.isEnabled()


def _extra_refresh_button(page):
    for button in page.extra_audio_group.findChildren(QPushButton):
        if button is page.playback_refresh:
            continue
        description = ' '.join((button.text(), button.accessibleName(),
                                button.toolTip())).lower()
        if 'refresh' in description or 'scan devices' in description:
            return button
    return None


def test_refresh_during_loading_does_not_launch_a_second_helper(
        playback_page, qtbot, monkeypatch):
    page = playback_page
    page.show()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_runner(args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        release.wait(2)
        return SimpleNamespace(returncode=0, stdout=_inventory(), stderr='')

    page.engine_path = page.window().engine_path = r'C:\isolated\FTHRclips.exe'
    monkeypatch.setattr(main.subprocess, 'run', blocked_runner)
    POPULATE_PLAYBACK_DEVICES(page)
    qtbot.waitUntil(entered.is_set, timeout=1000)
    button = _extra_refresh_button(page)
    if button is not None and button.isEnabled():
        qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    POPULATE_PLAYBACK_DEVICES(page)
    assert len(calls) == 1
    release.set()
    qtbot.waitUntil(lambda: not page._playback_scan_running, timeout=3000)
    assert page._playback_inventory_state == 'ready'


def test_failed_refresh_can_be_retried_successfully(
        playback_page, qtbot, monkeypatch):
    page = playback_page
    page.window().show()
    page.show()
    qtbot.waitUntil(lambda: page.isVisible(), timeout=1000)
    attempts = []

    def runner(_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            return SimpleNamespace(returncode=1, stdout='', stderr='unavailable')
        return SimpleNamespace(returncode=0,
                               stdout=_inventory(endpoints=[('back', 'Speakers')]),
                               stderr='')

    page.engine_path = page.window().engine_path = r'C:\isolated\FTHRclips.exe'
    monkeypatch.setattr(main.subprocess, 'run', runner)
    POPULATE_PLAYBACK_DEVICES(page)
    qtbot.waitUntil(lambda: not page._playback_scan_running, timeout=3000)
    assert page._playback_inventory_state == 'error'

    button = _extra_refresh_button(page)
    assert button is not None and not button.isHidden() and button.isEnabled(), (
        page.audio_source_combo.currentData(),
        page.extra_audio_group.isHidden(), page.stack.currentIndex(),
        page.isVisible())
    qtbot.mouseClick(button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(attempts) == 2, timeout=1000)
    qtbot.waitUntil(lambda: not page._playback_scan_running, timeout=3000)

    assert len(attempts) == 2
    assert page._playback_inventory_state == 'ready'
    assert [page.extra_device_combo.itemData(i)
            for i in range(page.extra_device_combo.count())] == ['back']


@pytest.mark.parametrize('track_ids', [[], ['saved-a']])
def test_several_mode_hint_distinguishes_empty_from_selected_device_tracks(
        playback_page, track_ids):
    page = playback_page
    tracks = [{'id': item, 'name': item}
              for item in track_ids]
    page.sm.set('audio_device_tracks', tracks)
    page._playback_inventory_state = 'ready'
    page._playback_endpoints = []
    page._refresh_extra_audio_devices()
    hint = page.audio_source_hint.text().lower()
    assert 'manual recordings' in hint and 'system mix' in hint
    if not tracks:
        assert 'fallback' in hint
        assert ('no' in hint or 'not' in hint or "won't" in hint)
    else:
        assert 'no combined system mix track' in hint
        assert 'fallback' not in hint


def test_add_track_requires_successfully_known_available_endpoint(
        playback_page, qtbot):
    page = playback_page
    page._apply_playback_devices([], successful=False)
    assert not page.extra_device_add.isEnabled()
    page._on_add_extra_audio_device()
    assert page.sm.get('audio_device_tracks') == []

    page._apply_playback_devices([_endpoint('a')], successful=True)
    page._on_add_extra_audio_device()
    assert len(page.sm.get('audio_device_tracks')) == 1
    for endpoint_id in ('b', 'c', 'd'):
        page._apply_playback_devices(
            [_endpoint(endpoint_id, f'Device {endpoint_id}')], successful=True)
        page._on_add_extra_audio_device()
    assert len(page.sm.get('audio_device_tracks')) == 4
    assert not page.extra_device_add.isEnabled()


def test_system_mode_manual_mix_fallback_and_program_mode_are_unchanged(playback_page):
    page = playback_page
    page._apply_playback_devices([], successful=True)
    page.sm.set('audio_source_mode', 'programs')
    page._sync_audio_source_mode()
    programs_hint = page.audio_source_hint.text().lower()
    assert 'manual recordings' in programs_hint
    assert 'system mix' in programs_hint

    page.sm.set('audio_source_mode', 'device')
    page._sync_audio_source_mode()
    page.playback_combo.setCurrentIndex(0)
    assert page.sm.get('playback_device_id') == ''
    assert 'windows default' in page.playback_combo.currentText().lower()


def test_linux_capture_keeps_legacy_combined_or_separated_policy(
        playback_page, monkeypatch):
    page = playback_page
    page.sm.set('audio_source_mode', 'devices')
    page.sm.set('audio_capture_mode', 'separated')
    page.sm.set('audio_device_tracks', [{'id': 'saved-a', 'name': 'Chat'}])
    monkeypatch.setattr(main.sys, 'platform', 'linux')
    host = SimpleNamespace(
        settings_manager=page.sm, capture_fps=60, clip_duration=30,
        capture_width=1920, capture_height=1080, capture_bitrate=25_000,
        _active_game_window=None)

    config = main.MainWindow._requested_capture_config(host)
    assert config.separate_audio_enabled is True
    assert config.application_audio_tracks_enabled is False
    assert config.audio_device_tracks == ''
    assert config.playback_endpoint_id == ''
