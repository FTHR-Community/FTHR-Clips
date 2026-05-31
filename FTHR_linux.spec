# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FTHR Clips — self-contained Linux bundle.
# Run on Linux: pyinstaller FTHR_linux.spec --clean
from pathlib import Path
import glob as _glob

ROOT       = Path(SPECPATH)
UI_DIR     = ROOT / 'FTHR_UI'
ASSETS_DIR = UI_DIR / 'assets'
ENGINE_BIN = ROOT / 'FTHRcapture_linux' / 'build' / 'FTHRclips'

# Qt6 plugin directories — bundle Wayland + XCB so the app works on both
_QT6_PLUG = Path('/usr/lib/qt6/plugins')

def _so(subdir, dest):
    d = _QT6_PLUG / subdir
    return [(str(p), dest) for p in d.glob('*.so')] if d.exists() else []

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_BIN), '.'),
        ('/usr/lib/libportaudio.so.2', '.'),
        *_so('platforms',                             'PyQt6/Qt6/plugins/platforms'),
        *_so('wayland-decoration-client',             'PyQt6/Qt6/plugins/wayland-decoration-client'),
        *_so('wayland-shell-integration',             'PyQt6/Qt6/plugins/wayland-shell-integration'),
        *_so('wayland-graphics-integration-client',   'PyQt6/Qt6/plugins/wayland-graphics-integration-client'),
        *_so('imageformats',                          'PyQt6/Qt6/plugins/imageformats'),
    ],
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),  'assets'),
        (str(ASSETS_DIR / 'fonts'),          'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),          'assets/icons'),
        (str(ASSETS_DIR / 'sounds'),         'assets/sounds'),
    ],
    hiddenimports=[
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'sounddevice',
        'numpy',
        'cv2',
        'imageio_ffmpeg',
        'keyboard',
        # All UI/core submodules (PyInstaller may miss dynamic imports)
        'ui.capture_card',
        'ui.capture_card_client',
        'ui.capture_card_process',
        'ui.capture_settings_widget',
        'ui.clip_grid',
        'ui.clip_viewer',
        'ui.customize_page',
        'ui.screenshot_editor',
        'ui.splash_screen',
        'ui.style',
        'ui.upload_settings_widget',
        'core.capture_bridge',
        'core.hotkey_manager',
        'core.mic_recorder',
        'core.settings_manager',
        'core.theme_manager',
        'core.upload_manager',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FTHRClips',
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name='FTHRClips',
)
