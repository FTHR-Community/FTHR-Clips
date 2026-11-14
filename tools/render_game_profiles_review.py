"""Render the profile flow with isolated, synthetic settings and app data."""
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtCore import QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QScrollArea
from ui.app_style import apply_app_style

out = ROOT / '.impeccable' / 'review' / 'game-profiles'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile, ExitStack() as patches:
    patches.enter_context(patch('pathlib.Path.home', return_value=Path(profile)))
    import main
    from core.settings_manager import SettingsManager
    from core.game_profiles import create_profile, empty_catalog
    app = QApplication([])
    main._load_fonts()
    apply_app_style(app)
    for method in ('_start_encoder_probe', '_populate_profile_mic_devices',
                   '_populate_mic_devices', '_populate_playback_devices'):
        patches.enter_context(patch.object(main._SettingsPage, method, lambda *a, **k: None))
    sm = SettingsManager()
    catalog = create_profile(empty_catalog(), 'Example game', r'C:\Games\Example\Game.exe')
    sm.save_game_capture_profiles(catalog, expected_revision=0)
    catalog = create_profile(catalog, 'Another game', r'C:\Games\Another\Another.exe')
    sm.save_game_capture_profiles(catalog, expected_revision=1)
    page = main._SettingsPage(sm)
    page.show()
    page.navigate_category_to(7)
    widget = page.profiles_widget
    widget.set_available_windows([
        {'exe_path': r'C:\Games\New\NewGame.exe', 'display_name': 'New game — running'},
        {'exe_path': r'C:\Windows\explorer.exe', 'display_name': 'File Explorer'},
    ])

    def capture(name, width):
        QTest.qWait(100)
        scroll = page.stack.currentWidget().findChild(QScrollArea)
        scroll.verticalScrollBar().setValue(
            page._clip_embedded_sections[7].mapTo(scroll.widget(), QPoint()).y())
        QTest.qWait(60)
        page.grab().save(str(out / f'{name}-{width}.png'))

    for width in (800, 1100, 1500):
        page.resize(width, 900)
        widget._show_library()
        capture('list', width)
        widget.new_button.click()
        widget.manual_path_edit.setText(r'C:\Games\New\NewGame.exe')
        capture('manual', width)
        widget._source_tabs.setCurrentIndex(1)
        capture('running', width)
        widget.continue_button.click()
        widget._bindings['framerate'].toggle.setChecked(True)
        widget._bindings['framerate'].editor.setValue(120)
        for index, section in enumerate(('clips', 'recording', 'audio', 'camera')):
            widget._section_tabs.setCurrentIndex(index)
            capture(section, width)
        widget.discard_button.click()
    page.close()
