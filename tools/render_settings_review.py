"""Render settings with an isolated profile and no device discovery."""
import os
import logging
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QWidget, QHBoxLayout
from PySide6.QtTest import QTest
output = ROOT / 'ui-preview' / ('settings-' + (sys.argv[1] if len(sys.argv) > 1 else 'after'))
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as profile, ExitStack() as patches:
    patches.enter_context(patch('pathlib.Path.home', return_value=Path(profile)))
    original_streams = sys.stdout, sys.stderr
    import main
    from core.settings_manager import SettingsManager
    from ui.app_style import apply_app_style
    app = QApplication([])
    main._load_fonts()
    apply_app_style(app)
    for method in ('_start_encoder_probe', '_populate_camera_devices',
                   '_populate_mic_devices', '_populate_playback_devices',
                   '_populate_profile_mic_devices', '_start_audio_preview_if_current'):
        patches.enter_context(patch.object(main._SettingsPage, method, lambda *a, **k: None))
    page = main._SettingsPage(SettingsManager())
    page.show()
    for width in (1100, 1500):
        page.resize(width, 850)
        for category, name in ((0, 'general'), (1, 'capture'), (2, 'audio'),
                               (3, 'overlays'), (5, 'performance'), (6, 'shortcuts'), (10, 'storage')):
            page.navigate_category_to(category)
            QTest.qWait(120)
            assert page.stack.currentIndex() == category
            if category == 3:
                page._overlay_sections['Images'].collapse()
                page._overlay_sections['Keyboard'].collapse()
                QTest.qWait(80)
            page.grab().save(str(output / f'{name}-{width}.png'))
            if category in (0, 1, 2, 5, 6, 10):
                from ui.customize_page import _AccordionSection
                sections = page.stack.currentWidget().findChildren(_AccordionSection)
                for section in sections:
                    section.expand()
                QTest.qWait(80)
                page.grab().save(str(output / f'{name}-expanded-{width}.png'))
                for section in sections[1:]:
                    section.collapse()
            if category == 3:
                for section in page._overlay_sections.values():
                    section.expand()
                QTest.qWait(80)
                page.grab().save(str(output / f'overlays-expanded-{width}.png'))
    page.hide()
    from ui.style import Colors
    toolbar = QWidget()
    toolbar.setStyleSheet(f'background: {Colors.SHELL_BG_2};')
    row = QHBoxLayout(toolbar)
    row.setContentsMargins(16, 12, 16, 12)
    for title in ('Capture settings', 'Capture source', 'Game detection', 'Hotkeys'):
        row.addWidget(main.TopBarButton(title), 1)
    toolbar.resize(1000, 68)
    toolbar.show()
    QTest.qWait(60)
    toolbar.grab().save(str(output / 'header-controls.png'))
    QTest.mouseMove(row.itemAt(1).widget(), row.itemAt(1).widget().rect().center())
    QTest.qWait(50)
    toolbar.grab().save(str(output / 'header-controls-hover.png'))
    toolbar.close()
    page.deleteLater()
    app.processEvents()
    print(output)
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, main._LogTee) and stream._log is not None:
            stream._log.close()
    sys.stdout, sys.stderr = original_streams
