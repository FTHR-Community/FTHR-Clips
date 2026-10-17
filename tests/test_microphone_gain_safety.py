from __future__ import annotations

import pytest


def _make_page(qtbot, monkeypatch, tmp_path, mic_volume: int):
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    settings = SettingsManager()
    settings.set('mic_volume', mic_volume)
    page = _SettingsPage(settings)
    qtbot.addWidget(page)
    return page, settings


@pytest.mark.parametrize(
    ('mic_volume', 'warning_hidden'),
    [(100, True), (150, False), (200, False)],
)
def test_microphone_gain_range_and_warning_state(
        qtbot, monkeypatch, tmp_path, mic_volume, warning_hidden):
    page, settings = _make_page(qtbot, monkeypatch, tmp_path, mic_volume)

    assert page.mic_gain_label.text() == 'Microphone gain'
    assert page.mic_vol_slider.minimum() == 0
    assert page.mic_vol_slider.maximum() == 200
    assert page.mic_vol_slider.value() == mic_volume
    assert page.mic_gain_warning.isHidden() is warning_hidden
    assert 'digital boost' in page.mic_gain_warning.text()
    assert 'Restart capture' in page.mic_gain_warning.text()
    assert '100% is unity' in page.mic_vol_slider.toolTip()
    assert 'Windows audio enhancements are independent' in (
        page.mic_vol_slider.toolTip())
    assert settings.get('mic_volume') == mic_volume


def test_existing_microphone_gain_is_loaded_without_rewrite_and_changes_save_once(
        qtbot, monkeypatch, tmp_path):
    page, settings = _make_page(qtbot, monkeypatch, tmp_path, 150)
    saves: list[int] = []
    settings.save_settings = lambda: saves.append(settings.get('mic_volume'))

    # Loading an existing boosted value must not rewrite or normalize it.
    assert settings.get('mic_volume') == 150
    assert saves == []

    page.mic_vol_slider.setValue(200)
    assert settings.get('mic_volume') == 200
    assert saves == [200]
    assert not page.mic_gain_warning.isHidden()

    page.mic_vol_slider.setValue(100)
    assert settings.get('mic_volume') == 100
    assert saves == [200, 100]
    assert page.mic_gain_warning.isHidden()



def test_windows_audio_preferences_restart_capture_and_persist(qtbot, monkeypatch, tmp_path):
    import main
    from core.windows_microphone_devices import WindowsMicrophoneEndpoint
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    page, settings = _make_page(qtbot, monkeypatch, tmp_path, 100)
    changes = []
    page.audio_capture_changed.connect(changes.append)
    page.mic_mono_check.setChecked(True)
    page._apply_playback_devices([WindowsMicrophoneEndpoint('render-id', 'Speakers', 1, True)])
    page.playback_combo.setCurrentIndex(1)
    assert settings.get('mic_downmix_mono') is True
    assert settings.get('playback_device_id') == 'render-id'
    assert changes == [True, True]
    page._apply_playback_devices([])
    assert page.playback_combo.currentData() == 'render-id'
    assert 'Windows default' in page.playback_combo.currentText()
    assert changes == [True, True]


def test_windows_master_multiplies_notification_volumes(monkeypatch):
    from ui import capture_card
    class Settings:
        def get(self, key, default=None):
            return 25 if key == 'sound_volume_master' else default
    monkeypatch.setattr(capture_card, 'SettingsManager', Settings)
    monkeypatch.setattr('sys.platform', 'win32')
    assert capture_card._master_volume(80) == 20
