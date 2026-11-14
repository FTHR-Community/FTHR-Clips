"""Temporary independent evidence capture; no discovery or real profile writes."""
import os, sys, tempfile, logging
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
output = ROOT / 'ui-preview/audio-compact-review'
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory() as profile, ExitStack() as patches:
    patches.enter_context(patch('pathlib.Path.home', return_value=Path(profile)))
    streams = sys.stdout, sys.stderr
    import main
    from core.settings_manager import SettingsManager
    from ui.app_style import apply_app_style
    from ui.customize_page import _AccordionSection
    app = QApplication([])
    main._load_fonts()
    apply_app_style(app)
    for method in ('_start_encoder_probe', '_populate_camera_devices', '_populate_mic_devices', '_populate_playback_devices', '_populate_profile_mic_devices', '_start_audio_preview_if_current'):
        patches.enter_context(patch.object(main._SettingsPage, method, lambda *a, **k: None))
    sm = SettingsManager()
    sm.set('audio_source_mode', 'programs')
    sm.set('per_app_audio_tracks', True)
    page = main._SettingsPage(sm)
    page.show()
    page.navigate_category_to(2)
    QTest.qWait(150)
    for section in page.stack.currentWidget().findChildren(_AccordionSection):
        section.expand()
    for width in (1100, 1500):
        page.resize(width, 850)
        for name, exclusions, backup, gain in (('programs', [], True, 100), ('backup-off', [], False, 100), ('excluded', ['discord'], True, 100), ('gain-warning', [], True, 130)):
            sm.set('application_audio_exclusions', exclusions)
            sm.set('keep_system_audio_backup', backup)
            page._sync_audio_source_mode()
            page.mic_vol_slider.setValue(gain)
            QTest.qWait(100)
            page.grab().save(str(output / f'{name}-{width}.png'))
    page.hide()
    page.deleteLater()
    app.processEvents()
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, main._LogTee) and stream._log is not None:
            stream._log.close()
    sys.stdout, sys.stderr = streams
print(output)
