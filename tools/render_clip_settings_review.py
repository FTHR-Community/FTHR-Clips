"""Render Clip's folded app settings and the shared popup using sample data."""
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication, QScrollArea, QWidget
from PySide6.QtTest import QTest
from ui.app_style import apply_app_style

out = ROOT / 'ui-preview' / 'clip-app-settings'
out.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as profile, ExitStack() as patches:
    patches.enter_context(patch('pathlib.Path.home', return_value=Path(profile)))
    original_streams = sys.stdout, sys.stderr
    import main
    from core.settings_manager import SettingsManager
    from core.game_profiles import create_profile, empty_catalog
    from core.capture_admission import add_executable, empty_policy
    from ui.customize_page import _AccordionSection
    app = QApplication([])
    main._load_fonts()
    apply_app_style(app)
    for method in ('_start_encoder_probe', '_populate_profile_mic_devices',
                   '_populate_mic_devices', '_populate_playback_devices'):
        patches.enter_context(patch.object(main._SettingsPage, method, lambda *a, **k: None))
    sm = SettingsManager()
    sm.save_game_capture_profiles(create_profile(empty_catalog(), 'Example game',
        r'C:\Games\Example\Game.exe', {}), expected_revision=0)
    sm.save_capture_admission_policy(add_executable(empty_policy(), sys.executable), expected_revision=0)
    page = main._SettingsPage(sm)
    page.show()
    for width in (1100, 1500):
        page.resize(width, 900)
        page.navigate_category_to(1)
        QTest.qWait(60)
        for section in page.stack.currentWidget().findChildren(_AccordionSection):
            section.collapse()
        QTest.qWait(80)
        page.grab().save(str(out / f'clip-{width}.png'))
        for category, name in ((7, 'profiles'), (8, 'allowed')):
            page.navigate_category_to(category)
            QTest.qWait(120)
            if category == 8:
                page.capture_admission_widget.set_available_windows([
                    {'exe_path': sys.executable, 'display_name': 'Python'},
                    {'exe_path': r'C:\Windows\explorer.exe', 'display_name': 'File Explorer'},
                ])
            scroll = page.stack.currentWidget().findChild(QScrollArea)
            from PySide6.QtCore import QPoint
            scroll.verticalScrollBar().setValue(page._clip_embedded_sections[category].mapTo(scroll.widget(), QPoint()).y())
            QTest.qWait(60)
            page.grab().save(str(out / f'{name}-{width}.png'))
            if category == 7:
                fields = page.profiles_widget._editor_scroll.verticalScrollBar()
                fields.setValue(fields.maximum())
                QTest.qWait(60)
                page.grab().save(str(out / f'profiles-bottom-{width}.png'))
                fields.setValue(0)
            page._clip_embedded_sections[category].collapse()
    combo = page.capture_admission_widget.running_windows_combo
    page._clip_embedded_sections[8].expand()
    scroll.ensureWidgetVisible(combo)
    QTest.qWait(60)
    combo.showPopup()
    QTest.qWait(60)
    combo.view().window().grab().save(str(out / 'app-dropdown.png'))
    print('Popup geometry:', combo.view().size(), combo.view().viewport().size(), combo.view().sizeHintForRow(0), combo.view().verticalScrollBar().maximum())
    combo.hidePopup()
    sm.set('bitrate_level', 'custom')
    bitrate = main.CaptureSettingsPopup(sm)
    bitrate.show()
    QTest.qWait(80)
    bitrate.grab().save(str(out / 'capture-bitrate-stepper.png'))
    bitrate.close()
    from PySide6.QtGui import QPixmap, QPainter, QColor
    host = QWidget()
    host.resize(720, 600)
    host.setStyleSheet('background: #141619;')
    trigger = main.TopBarButton('SOURCE', host)
    trigger.setGeometry(32, 24, 200, 40)
    host.show()
    source = main.SourcePopup(sm)
    source.show_below(trigger)
    QTest.qWait(80)
    canvas = QPixmap(host.size())
    canvas.fill(QColor('#141619'))
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, host.grab())
    offset = host.mapFromGlobal(source.pos())
    painter.drawPixmap(offset, source.grab())
    painter.end()
    canvas.save(str(out / 'source-attached.png'))
    source.close()
    host.close()
    print(out)
    page.close()
    import logging
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, main._LogTee) and stream._log is not None:
            stream._log.close()
    sys.stdout, sys.stderr = original_streams
