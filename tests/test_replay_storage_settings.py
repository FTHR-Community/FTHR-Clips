"""Replay mode migration and the Settings-to-native launch contract."""

import json
from threading import Event

import pytest

from PySide6.QtWidgets import QFileDialog

from core.capture_profile_policy import build_capture_config
from core.replay_storage import (
    MIB, REPLAY_STORAGE_KEY, ReplayFolderStatus, ReplayStorageSettings,
    estimate_replay_disk_bytes, inspect_replay_folder,
)
from core.settings_manager import SettingsManager
from main import BITRATE_PRESETS, _resolution_to_dims, _SettingsPage
from ui.settings_search import search_settings


def test_legacy_config_defaults_to_memory_and_invalid_values_are_normalized(tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    path = tmp_path / '.fthr' / 'settings.json'
    path.parent.mkdir()
    legacy = {'clip_length': 60}
    path.write_text(json.dumps(legacy), encoding='utf-8')
    settings = SettingsManager()
    assert settings.get('clip_length') == 60
    assert settings.get('allow_replay_gaps') is True
    assert settings.get('allow_low_fps_clips') is True
    assert settings.get(REPLAY_STORAGE_KEY) == ReplayStorageSettings().to_dict()

    path.write_text(json.dumps({**legacy, 'allow_replay_gaps': 'true', REPLAY_STORAGE_KEY: {
        'mode': 'disk', 'folder': 'relative', 'memory_mib': True,
        'disk_mib': '1048577',
    }}), encoding='utf-8')
    invalid = SettingsManager()
    assert invalid.get('allow_replay_gaps') is False
    assert invalid.get(REPLAY_STORAGE_KEY) == ReplayStorageSettings(
        disk_mib=1_048_576).to_dict()


def test_launch_config_detaches_replay_storage_from_global_preferences(tmp_path):
    folder = str(tmp_path / 'Temporary Replay ä')
    replay = ReplayStorageSettings('disk', folder, 1024, 8192, 'manual')
    values = dict(clip_length=30, framerate=60, resolution='source', bitrate_level='medium')
    prefs = replay.to_dict()
    config = build_capture_config(
        values=values, global_values={REPLAY_STORAGE_KEY: prefs, 'allow_replay_gaps': True},
        purpose='clips', bitrate_presets=BITRATE_PRESETS,
        resolution_to_dims=_resolution_to_dims, monitor='', codec='auto',
        encoder='auto', preset=4, scaling='stretch',
    )
    prefs['folder'] = 'changed after request was prepared'
    assert config.allow_replay_gaps is True
    assert config.replay_storage.launch_args() == [
        '--replay-storage', 'disk', '--replay-memory-mb', '1024',
        '--replay-storage-path', folder, '--replay-storage-mb', '8192',
    ]
    assert ReplayStorageSettings().launch_args() == [
        '--replay-storage', 'memory', '--replay-budget-mode', 'auto',
    ]
    migrated = ReplayStorageSettings.from_value({
        'mode': 'disk', 'folder': folder, 'memory_mib': 1024, 'disk_mib': 8192,
    })
    assert migrated.to_dict() == {**replay.to_dict(), 'budget_mode': 'auto'}
    assert migrated.launch_args() == [
        '--replay-storage', 'disk', '--replay-budget-mode', 'auto',
        '--replay-storage-path', folder,
    ]
    assert estimate_replay_disk_bytes(32, 25_000, 2) > estimate_replay_disk_bytes(32, 25_000, 0)


def test_replay_gap_toggle_persists_before_requesting_restart(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    page.stack.setCurrentIndex(1)
    checkbox = page.allow_replay_gaps_check
    assert checkbox.isChecked()

    for enabled in (False, True):
        with qtbot.waitSignal(page.replay_gap_policy_changed):
            checkbox.click()
        assert checkbox.isChecked() is enabled
        assert SettingsManager().get('allow_replay_gaps') is enabled

    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.replay_gap_policy_changed):
        checkbox.click()
    assert checkbox.isChecked()
    assert settings.get('allow_replay_gaps') is True
    assert settings.get('allow_low_fps_clips') is True
    assert not page.replay_gap_save_error.isHidden()


def test_probe_checks_writability_and_cleans_up_its_probe_file(tmp_path, monkeypatch):
    monkeypatch.setattr('core.replay_storage._windows_media', lambda path: 'hdd')
    monkeypatch.setattr('core.replay_storage._linux_media', lambda path: 'hdd')
    status = inspect_replay_folder(tmp_path)
    assert status.free_bytes > 0
    assert not status.error
    assert not list(tmp_path.iterdir())

    missing = inspect_replay_folder(tmp_path / 'disconnected')
    assert missing.error and missing.free_bytes is None


def test_settings_validates_folder_and_restarts_once_after_commit(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    good = tmp_path / 'HDD buffer'
    bad = tmp_path / 'Unavailable buffer'
    first_probe = Event()
    release_first = Event()

    def inspect(directory):
        if directory == bad:
            return ReplayFolderStatus(error='Choose another folder.')
        if directory == good:
            return ReplayFolderStatus(80 * MIB, 'hdd')
        first_probe.set()
        release_first.wait(3)
        return ReplayFolderStatus(100 * MIB, 'unknown')

    monkeypatch.setattr('ui.replay_storage.inspect_replay_folder', inspect)
    page = _SettingsPage(settings)
    monkeypatch.setattr(page, '_start_encoder_probe', lambda: None)
    qtbot.addWidget(page)
    page.resize(1000, 780)
    page.show()
    destination = search_settings('replay buffer')[0]
    page._open_setting_result(destination)
    qtbot.waitUntil(lambda: page.stack.currentIndex() == 10)
    widget = page.replay_storage_widget
    assert not widget.disk_details.isVisible()
    assert not widget._save_timer.isActive()

    widget.mode_combo.setCurrentIndex(widget.mode_combo.findData('disk'))
    qtbot.waitUntil(first_probe.is_set)
    qtbot.waitUntil(widget.browse_button.isVisible)
    assert not widget._save_timer.isActive()
    try:
        monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(bad))
        widget.browse_button.click()
        qtbot.waitUntil(widget.folder_error.isVisible)
        assert not widget._save_timer.isActive()
        assert settings.get(REPLAY_STORAGE_KEY)['mode'] == 'memory'

        monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *args: str(good))
        widget.browse_button.click()
        qtbot.waitUntil(widget.drive_advice.isVisible)
        assert 'HDD' in widget.drive_advice.text()
        assert '84 MB free' in widget.space_label.text()
        assert widget.capacity_warning.isVisible()
        with qtbot.waitSignal(widget._folder_checked, timeout=1000,
                              check_params_cb=lambda epoch, _: epoch == 1):
            release_first.set()
        assert widget.drive_advice.isVisible()
    finally:
        release_first.set()

    commits = []
    page.replay_storage_changed.connect(lambda: commits.append(True))
    with qtbot.waitSignal(page.replay_storage_changed, timeout=1500):
        widget.budget_mode_combo.setCurrentIndex(widget.budget_mode_combo.findData('manual'))
        widget.disk_budget.setValue(8)  # 3 GB
        widget.memory_budget.setValue(4)  # 1 GB
    assert commits == [True]
    assert not widget._save_timer.isActive()
    assert SettingsManager().get(REPLAY_STORAGE_KEY) == ReplayStorageSettings(
        'disk', str(good), 954, 2862, 'manual').to_dict()

    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.replay_storage_changed):
        widget.mode_combo.setCurrentIndex(widget.mode_combo.findData('memory'))
        qtbot.waitUntil(widget.save_error.isVisible)
    assert widget.save_error.isVisible()
    assert settings.get(REPLAY_STORAGE_KEY)['mode'] == 'disk'


def test_low_fps_toggle_persists_and_rolls_back_on_failure(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    checkbox = page.allow_low_fps_clips_check
    assert checkbox.isChecked()
    for enabled in (False, True):
        with qtbot.waitSignal(page.replay_gap_policy_changed):
            checkbox.click()
        assert SettingsManager().get('allow_low_fps_clips') is enabled
        assert settings.get('allow_replay_gaps') is True
    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.replay_gap_policy_changed):
        checkbox.click()
    assert checkbox.isChecked()
    assert settings.get('allow_low_fps_clips') is True
    assert not page.replay_low_fps_save_error.isHidden()


@pytest.mark.parametrize('enabled', [False, True])
def test_saved_replay_policies_keep_explicit_preferences(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    path = tmp_path / '.fthr' / 'settings.json'
    path.parent.mkdir()
    path.write_text(json.dumps({
        'allow_replay_gaps': enabled,
        'allow_low_fps_clips': enabled,
    }), encoding='utf-8')
    settings = SettingsManager()
    assert settings.get('allow_replay_gaps') is enabled
    assert settings.get('allow_low_fps_clips') is enabled


def test_launch_config_defaults_to_saving_gaps_and_low_fps():
    config = build_capture_config(
        values=dict(clip_length=30, framerate=60, resolution='source', bitrate_level='medium'),
        global_values={}, purpose='clips', bitrate_presets=BITRATE_PRESETS,
        resolution_to_dims=_resolution_to_dims, monitor='', codec='auto',
        encoder='auto', preset=4, scaling='stretch',
    )
    assert config.allow_replay_gaps is True
    assert config.allow_low_fps_clips is True


def test_low_resource_option_persists_and_reaches_engine(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    widget = page.replay_storage_widget
    assert not widget.allow_low_resources_check.isChecked()
    with qtbot.waitSignal(page.replay_storage_changed):
        widget.allow_low_resources_check.setChecked(True)
        assert not widget.low_resources_detail.isHidden()
    saved = ReplayStorageSettings.from_value(SettingsManager().get(REPLAY_STORAGE_KEY))
    assert saved.allow_low_resources
    assert saved.launch_args()[-2:] == ['--replay-low-resources', 'attempt']
    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.replay_storage_changed):
        widget.allow_low_resources_check.setChecked(False)
        qtbot.waitUntil(lambda: not widget.save_error.isHidden())
    assert ReplayStorageSettings.from_value(settings.get(REPLAY_STORAGE_KEY)).allow_low_resources
    assert not ReplayStorageSettings.from_value({'allow_low_resources': 'true'}).allow_low_resources
