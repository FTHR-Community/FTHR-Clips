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

    assert page.mic_gain_label.text() == 'Volume'
    assert page.mic_vol_slider.minimum() == 0
    assert page.mic_vol_slider.maximum() == 200
    assert page.mic_vol_slider.value() == mic_volume
    assert page.mic_gain_warning.isHidden() is warning_hidden
    assert 'digital boost' in page.mic_gain_warning.text()
    assert 'restarts capture' in page.mic_gain_warning.text()
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



def test_microphone_track_click_saves_gain_once_on_release(qtbot, monkeypatch, tmp_path):
    from PySide6.QtCore import QPoint, Qt

    import main
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    page, settings = _make_page(qtbot, monkeypatch, tmp_path, 100)
    slider = page.mic_vol_slider
    slider.resize(401, 30)
    restarts = []
    page.audio_capture_changed.connect(restarts.append)
    position = QPoint(300, slider.height() // 2)

    qtbot.mousePress(slider, Qt.MouseButton.LeftButton, pos=position)
    assert 145 <= slider.sliderPosition() <= 155
    assert page.mic_vol_value.text() == f'{slider.sliderPosition()}%'
    assert settings.get('mic_volume') == 100
    assert restarts == []

    qtbot.mouseRelease(slider, Qt.MouseButton.LeftButton, pos=position)
    assert settings.get('mic_volume') == slider.value()
    assert page.mic_vol_value.text() == f'{slider.value()}%'
    assert restarts == [True]


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


def test_failed_microphone_gain_save_restores_slider_without_restart(qtbot, monkeypatch, tmp_path):
    page, settings = _make_page(qtbot, monkeypatch, tmp_path, 100)
    slider = page.mic_vol_slider
    monkeypatch.setattr(settings, 'save_settings', lambda: False)
    with qtbot.assertNotEmitted(page.audio_capture_changed):
        slider.setValue(150)
    assert slider.value() == 100
    assert settings.get('mic_volume') == 100
    assert not page.mic_save_error.isHidden()
