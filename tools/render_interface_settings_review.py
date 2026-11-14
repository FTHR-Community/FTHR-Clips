"""Render Interface Studio against the real settings UI without starting capture."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path[:0] = [str(ROOT / 'FTHR_UI'), str(ROOT)]

from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMainWindow, QStackedWidget, QVBoxLayout, QWidget

import main
from core.settings_manager import SettingsManager
from plugins.interface_editor import Plugin
from ui.app_style import apply_app_style


def render():
    app = QApplication([])
    for font in (ROOT / 'FTHR_UI/assets/fonts').glob('*.ttf'):
        QFontDatabase.addApplicationFont(str(font))
    apply_app_style(app)
    with TemporaryDirectory() as directory, ExitStack() as patches:
        patches.enter_context(patch('pathlib.Path.home', return_value=Path(directory)))
        for method in ('_start_encoder_probe', '_populate_camera_devices',
                       '_populate_mic_devices', '_populate_playback_devices',
                       '_start_audio_preview_if_current'):
            patches.enter_context(patch.object(main._SettingsPage, method, lambda *a, **k: None))
        window = QMainWindow()
        root = QWidget()
        window.setCentralWidget(root)
        layout = QVBoxLayout(root)
        window.main_stack = QStackedWidget()
        layout.addWidget(window.main_stack)
        window.main_stack.addWidget(QWidget())
        settings = SettingsManager()
        window._settings_page_widget = main._SettingsPage(settings)
        window.main_stack.addWidget(window._settings_page_widget)
        window.main_stack.setCurrentIndex(1)
        window.resize(1440, 900)
        window.show()
        QTest.qWait(80)
        plugin = Plugin(SimpleNamespace(window=window, settings=settings))
        plugin.start()
        try:
            plugin.interface_host_ready()
            plugin.edit_design()
            studio = plugin.studio
            studio.resize(1440, 900)
            studio.surfaces.setCurrentIndex(studio.surfaces.findData('settings:0'))
            QTest.qWait(100)
            output = ROOT / 'ui-preview/interface-editor/studio-settings.png'
            output.parent.mkdir(parents=True, exist_ok=True)
            studio.grab().save(str(output))
            print(output)
        finally:
            plugin.stop()
            window.close()


if __name__ == '__main__':
    render()
