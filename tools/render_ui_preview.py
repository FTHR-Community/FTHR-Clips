"""Render the real Qt shell using an isolated profile and no capture services."""
from __future__ import annotations

import os
import logging
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtGui import QFontDatabase, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402


class _CaptureCard:
    def __init__(self, *_args):
        pass

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class _CaptureBridge:
    def get_audio_mappings(self):
        return {}


def main():
    output = ROOT / 'ui-preview' / 'ui-consistency'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fthr-ui-preview-') as temp, ExitStack() as stack:
        profile = Path(temp)
        stack.enter_context(patch('pathlib.Path.home', return_value=profile))
        original_streams = sys.stdout, sys.stderr
        import main as application
        from ui.app_style import apply_app_style
        from ui.clip_grid import ClipThumbnail

        app = QApplication.instance() or QApplication([])
        # Qt's offscreen plugin cannot enumerate Windows' native font database.
        # Register the installed faces for previews only; never redistribute
        # Microsoft's fonts with the product.
        if sys.platform == 'win32':
            for name in ('segoeui.ttf', 'seguisb.ttf', 'segoeuib.ttf'):
                QFontDatabase.addApplicationFont(str(Path('C:/Windows/Fonts') / name))
        application._load_fonts()
        apply_app_style(app)
        stack.enter_context(patch.object(application, 'CaptureCardClient', _CaptureCard))
        stack.enter_context(patch.object(application, 'CaptureBridge', _CaptureBridge))
        for name in ('_setup_hotkeys', '_start_mic_recorder', 'start_engine', 'stop_engine'):
            stack.enter_context(patch.object(application.MainWindow, name, lambda *_: None))
        stack.enter_context(patch.object(application._SettingsPage, '_start_encoder_probe', lambda *_: None))
        window = application.MainWindow()
        window.resize(1440, 900)
        window.show()
        QTest.qWait(180)
        settings = window.main_stack.widget(1)
        window._set_main_page(1)
        QTest.qWait(280)
        window.grab().save(str(output / 'settings.png'))
        for index, name in ((1, 'clip-settings'), (2, 'audio'), (3, 'visuals'),
                            (4, 'customize'), (6, 'shortcuts')):
            settings._tab_buttons[index].setChecked(True)
            settings.stack.setCurrentIndex(index)
            QTest.qWait(280)
            window.grab().save(str(output / f'{name}.png'))
        settings._tab_buttons[4].setChecked(True)
        settings.stack.setCurrentIndex(4)
        QTest.qWait(280)
        settings._customize_page._typography_section.toggle()
        QTest.qWait(280)
        window.grab().save(str(output / 'typography.png'))
        settings._customize_page._typography_section.collapse()
        settings._customize_page._colors_section.expand()
        QTest.qWait(280)
        window.grab().save(str(output / 'colors.png'))
        settings._customize_page._colors_section.collapse()
        for width, height in ((1920, 1080), (2560, 1440), (3840, 2160)):
            window.resize(width, height)
            QTest.qWait(280)
            window.grab().save(str(output / f'customize-{width}.png'))
        window.resize(1440, 900)
        window._set_main_page(0)
        QTest.qWait(280)
        window.grab().save(str(output / 'library.png'))
        QTest.mouseMove(window.record_btn, window.record_btn.rect().center())
        QTest.qWait(40)
        window.grab().save(str(output / 'record-hover.png'))
        window.resize(1100, 760)
        QTest.qWait(280)
        window.grab().save(str(output / 'library-small.png'))
        print('Toolbar labels:', [(button.text(), button.font().family(),
              button.width(), button.minimumSizeHint().width()) for button in
              (window.cap_btn, window.source_btn, window.game_detection_btn, window.hotkey_btn)])
        window.cap_settings_popup.show_below(window.cap_btn)
        QTest.qWait(280)
        window.cap_settings_popup.grab().save(str(output / 'capture-popup.png'))
        window.cap_settings_popup.hide()
        card = ClipThumbnail(str(profile / 'Overwatch_clip_from_04Oct2026_10-30-00.mp4'),
                             is_video=True, card_width=320, upload_enabled=True)
        card.set_image_thumbnail(QImage(str(ROOT / 'FTHR_UI/assets/preview_desktop.png')))
        card.show()
        QTest.qWait(280)
        card.grab().save(str(output / 'clip-card.png'))
        QTest.mouseMove(card.share_btn, card.share_btn.rect().center())
        QTest.qWait(40)
        card.grab().save(str(output / 'clip-card-hover.png'))
        card._show_action_feedback('Upload requested')
        card.grab().save(str(output / 'clip-card-feedback.png'))
        card.close()
        from ui.clip_viewer import ClipViewer
        from ui.clip_editor_window import ClipEditorWindow
        with ExitStack() as editor_patches:
            editor_patches.enter_context(patch('ui.clip_grid.get_cached_clip_metadata',
                                              return_value=(10, 1920, 1080, 60, 25_000_000, 25_000_000)))
            for method in ('_set_media_source', '_prepare_playback_source',
                           '_discover_audio_sources_async', '_load_timeline_thumbnails_if_idle'):
                editor_patches.enter_context(patch.object(ClipViewer, method, lambda *_: None))
            editor = ClipEditorWindow(str(profile / 'Overwatch_clip.mp4'), None,
                                      settings_manager=window.settings_manager,
                                      thumb_pixmap=card._thumb_pixmap)
            editor.resize(1440, 900)
            editor.show()
            QTest.qWait(280)
            editor.grab().save(str(output / 'clip-editor.png'))
            editor.close()
        window._shutdown_requested = True
        window._perform_full_shutdown()
        window.close()
        print(output)
        logging.shutdown()
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, application._LogTee) and stream._log is not None:
                stream._log.close()
        sys.stdout, sys.stderr = original_streams


if __name__ == '__main__':
    main()
