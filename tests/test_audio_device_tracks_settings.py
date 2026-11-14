"""Settings page: extra playback-device tracks and the per-program switch."""

from types import SimpleNamespace

import pytest

from core.settings_manager import SettingsManager
from main import _SettingsPage
from ui.motion import set_reduce_motion


@pytest.fixture
def audio_page(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    for name in ('_populate_mic_devices', '_populate_playback_devices',
                 '_refresh_keyboard_windows', '_start_encoder_probe'):
        monkeypatch.setattr(_SettingsPage, name, lambda *args, **kwargs: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(2)
    page._apply_playback_devices([
        SimpleNamespace(endpoint_id='{chat}', display_name='Chat (RODECaster)'),
        SimpleNamespace(endpoint_id='{game}', display_name='Game (RODECaster)'),
    ])
    yield page
    set_reduce_motion(False)


def test_added_devices_become_named_tracks_and_leave_the_picker(audio_page):
    page = audio_page
    emitted = []
    page.audio_capture_changed.connect(emitted.append)

    page.extra_device_combo.setCurrentIndex(
        page.extra_device_combo.findData('{chat}'))
    page.extra_device_name.setText('Chat')
    page._on_add_extra_audio_device()

    assert page.sm.get('audio_device_tracks') == [{'id': '{chat}', 'name': 'Chat'}]
    assert page.extra_device_combo.findData('{chat}') < 0
    assert page.extra_device_combo.findData('{game}') >= 0
    assert emitted == [True]


def test_blank_name_falls_back_to_the_device_name_and_remove_restores_it(audio_page):
    page = audio_page
    page.extra_device_combo.setCurrentIndex(
        page.extra_device_combo.findData('{game}'))
    page._on_add_extra_audio_device()

    assert page.sm.get('audio_device_tracks') == [
        {'id': '{game}', 'name': 'Game (RODECaster)'}]

    page._on_remove_extra_audio_device('{game}')

    assert page.sm.get('audio_device_tracks') == []
    assert page.extra_device_combo.findData('{game}') >= 0


def test_picker_locks_once_the_track_limit_is_reached(audio_page):
    page = audio_page
    page._apply_playback_devices([
        SimpleNamespace(endpoint_id=f'{{d{i}}}', display_name=f'Device {i}')
        for i in range(6)])
    for _ in range(4):
        page._on_add_extra_audio_device()

    assert len(page.sm.get('audio_device_tracks')) == 4
    assert not page.extra_device_add.isEnabled()


def test_record_mode_shows_the_device_list_only_for_several_devices(audio_page):
    page = audio_page
    page.show()

    assert page.audio_source_combo.currentData() == 'device'
    assert page.extra_audio_group.isHidden()

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('devices'))
    assert not page.extra_audio_group.isHidden()
    assert page.playback_combo.isHidden()
    assert page.sm.get('audio_source_mode') == 'devices'

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('programs'))
    assert page.extra_audio_group.isHidden()
    assert page.sm.get('per_app_audio_tracks') is True
    assert page.playback_combo.isHidden()

    page.audio_source_combo.setCurrentIndex(
        page.audio_source_combo.findData('device'))
    assert not page.playback_combo.isHidden()
