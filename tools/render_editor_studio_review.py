"""Render the Studio editor with isolated settings and synthetic clip sources."""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
if '--interactive' not in sys.argv:
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase, QPixmap
from PySide6.QtTest import QTest
from core.playback_mix_model import PlaybackSource
from ui.clip_viewer import ClipViewer
from ui.clip_editor_window import ClipEditorWindow
from ui.app_style import apply_app_style
from core.settings_manager import SettingsManager

app = QApplication([])
for font in (ROOT / 'FTHR_UI/assets/fonts').glob('*.ttf'):
    QFontDatabase.addApplicationFont(str(font))
apply_app_style(app)
output = ROOT / 'ui-preview/editor-fitted'
poster_path = Path(sys.argv[sys.argv.index('--poster') + 1]) if '--poster' in sys.argv else ROOT / 'FTHR_UI/assets/preview_desktop.png'
poster = QPixmap(str(poster_path))
source_width, source_height = poster.width(), poster.height()
output.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='fthr-studio-') as profile, ExitStack() as patches:
    patches.enter_context(patch('pathlib.Path.home', return_value=Path(profile)))
    patches.enter_context(patch('ui.clip_grid.get_cached_clip_metadata', return_value=(30, source_width, source_height, 60, 25000000, 25000000)))
    for method in ('_set_media_source', '_prepare_playback_source', '_discover_audio_sources_async', '_load_timeline_thumbnails_if_idle'):
        patches.enter_context(patch.object(ClipViewer, method, lambda *a, **k: None))
    editor = ClipEditorWindow(str(Path(profile) / 'Sample_clip.mp4'), None,
        settings_manager=SettingsManager(), thumb_pixmap=poster)
    page = editor.clip_page
    sources = (
        PlaybackSource('game', 'Game audio', 'application', 1, 0, persistent_identity='game'),
        PlaybackSource('chat', 'Discord', 'application', 2, 1, persistent_identity='discord'),
        PlaybackSource('mic', 'Microphone', 'microphone', 3, 2),
    )
    page._audio_mix_panel.set_state(80, {'game':100,'chat':65,'mic':85}, {}, sources, True)
    page._playback_status_lbl.setText('Sample clip')
    for width,height in ((1440,900),(1920,1032),(2560,1440),(3840,2160),(960,600)):
        editor.resize(width,height)
        editor.show()
        QTest.qWait(100)
        editor.grab().save(str(output / f'studio-{width}.png'))
        print(width, 'actual', editor.width(), editor.height(), 'preview', page._video_clip_frame.size(), 'timeline',page._studio_timeline.size())
    editor.resize(1440,900)
    for panel in page._studio_panels:
        button = panel.findChild(__import__('ui.style',fromlist=['ThemedDropdownButton']).ThemedDropdownButton)
        if not button.isChecked(): button.click()
    QTest.qWait(100)
    editor.grab().save(str(output / 'studio-expanded.png'))
    editor.resize(2560,1440)
    crop_width = round(source_height * 9 / 16)
    page._crop_rect = ((source_width - crop_width) // 2, 0, crop_width, source_height)
    page._refresh_live_preview(immediate=True)
    QTest.qWait(100)
    editor.grab().save(str(output / 'studio-portrait.png'))
    page._crop_rect = None
    page._refresh_live_preview(immediate=True)
    from ui.running_app_picker import RunningAppPickerDialog
    picker = RunningAppPickerDialog(editor, title='Exclude application audio')
    picker.show()
    QTest.qWait(100)
    picker.grab().save(str(output / 'running-apps.png'))
    picker.close()
    if '--interactive' in sys.argv:
        editor.setWindowTitle('FTHR Studio isolated review')
        app.exec()
    editor._teardown_player(wait_for_workers=False)
    editor.hide()
print(output)
