"""Whole-theme reset and repeated Apply must preserve the live controls."""

from pathlib import Path
import threading

from PySide6.QtWidgets import QWidget

from core.settings_manager import SettingsManager
from core.theme_manager import DEFAULT_COLORS, ThemeManager
from ui.app_style import apply_app_style
from ui.customize_page import CustomizePage, _IconRow, _SoundRow
from ui import capture_card_client


def test_reset_all_restores_every_section_and_persists_volumes(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    settings = SettingsManager()
    settings.set('camera_enabled', True)  # An unrelated setting must survive.
    theme = ThemeManager()
    theme.set_color('BG', '#abcdef')
    theme.set_font('body', 'Segoe UI')
    theme.set_global_icon_tint('#123456')
    theme.set_icon_tint('play.png', '#fedcba')
    theme.set_capture_card_color('CAPTURE_CARD_BG', '#123456')
    asset = tmp_path / 'imported.png'
    asset.write_bytes((Path(__file__).parents[1] / 'FTHR_UI/assets/gary.png').read_bytes())
    theme.set_custom_icon('play.png', asset)
    sound = tmp_path / 'imported.wav'
    sound.write_bytes((Path(__file__).parents[1] / 'FTHR_UI/assets/sounds/error.wav').read_bytes())
    theme.set_custom_sound('error', sound)
    font = tmp_path / 'imported.ttf'
    font.write_bytes(b'custom font fixture')
    theme.set_custom_font(['Imported font'], font)
    for key in _SoundRow._VOLUME_KEYS.values():
        settings.set(f'sound_volume_{key}', 17)
    theme.save()
    page = CustomizePage(settings)
    qtbot.addWidget(page)
    page.resize(1100, 700)
    page.show()
    page._sounds_section.expand()
    rows = page.findChildren(_SoundRow)
    applied = []
    page.theme_applied.connect(lambda: applied.append(True))
    page._reset_btn.click()
    assert applied == [True]
    assert not theme.has_any_customization()
    assert page._sounds_section.expanded
    assert page.findChildren(_SoundRow) == rows
    assert all(row._volume_slider.value() == 100 for row in rows)
    assert all(row._file_lbl.text() == '(default)' for row in rows)
    assert all(swatch.get_color() == DEFAULT_COLORS[swatch.token] for swatch in page._swatches)
    assert all(combo.currentData() == 'Oswald' for combo in page._font_combos.values())
    assert all(combo.findData('Imported font') < 0 for combo in page._font_combos.values())
    assert not theme.get_custom_icon_path('play.png')
    assert not theme.get_custom_sound_path('error')
    assert not theme.get_custom_font_paths()
    ThemeManager._instance = None
    assert not ThemeManager().has_any_customization()
    reloaded = SettingsManager()
    assert reloaded.get('camera_enabled') is True
    assert all(reloaded.get(f'sound_volume_{key}') == 100
               for key in _SoundRow._VOLUME_KEYS.values())


def test_repeated_theme_refresh_preserves_controls_and_style(qtbot, monkeypatch, tmp_path, qapp):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ThemeManager, '_instance', None)
    page = CustomizePage(SettingsManager())
    qtbot.addWidget(page)
    page._icons_section.expand()
    controls = page.findChildren(QWidget)
    icon_rows = page.findChildren(_IconRow)
    sound_players = [row._sound_playback for row in page.findChildren(_SoundRow)]
    apply_app_style(qapp)
    style = qapp.style()
    for _ in range(3):
        apply_app_style(qapp)
        page.refresh_theme()
    assert qapp.style() is style
    assert page.findChildren(QWidget) == controls
    assert page.findChildren(_IconRow) == icon_rows
    assert [row._sound_playback for row in page.findChildren(_SoundRow)] == sound_players
    assert page._icons_section.expanded


def test_card_restart_does_not_wait_for_slow_old_process(monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Process:
        stdin = None

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            entered.set()
            release.wait(5)
            finished.set()

    client = capture_card_client.CaptureCardClient.__new__(capture_card_client.CaptureCardClient)
    client._proc = Process()
    launches = []
    monkeypatch.setattr(client, '_launch', lambda: launches.append(True))
    try:
        client.restart()
        assert entered.wait(1)
        assert launches == [True]
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(1)
