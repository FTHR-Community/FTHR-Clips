# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FTHR Clips — Windows bundle.
# Run on Windows: pyinstaller FTHR.spec --clean
from pathlib import Path
import glob as _glob

ROOT       = Path(SPECPATH)
UI_DIR     = ROOT / 'FTHR_UI'
ASSETS_DIR = UI_DIR / 'assets'
ENGINE_EXE = ROOT / 'FTHRcapture' / 'x64' / 'Release' / 'FTHRClips.exe'

# FFmpeg DLLs the C++ engine links against on Windows
_FFMPEG_BIN  = ROOT / 'FTHRcapture' / 'FTHRclips' / 'third_party' / 'ffmpeg' / 'bin'
_FFMPEG_DLLS = _glob.glob(str(_FFMPEG_BIN / '*.dll'))

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_EXE), '.'),
        *[(dll, '.') for dll in _FFMPEG_DLLS],
    ],
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),   'assets'),
        (str(ASSETS_DIR / 'fonts'),            'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),            'assets/icons'),
        (str(ASSETS_DIR / 'sounds'),           'assets/sounds'),
        (str(UI_DIR / 'ui' / 'capture_card_process.py'), 'ui'),
    ],
    hiddenimports=[
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'sounddevice',
        'numpy',
        'cv2',
        'imageio_ffmpeg',
        'keyboard',
        # UI submodules
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
        # Core submodules
        'core.audio_mixer',
        'core.camera_recorder',
        'core.capture_bridge',
        'core.focus_monitor',
        'core.game_detector',
        'core.hotkey_manager',
        'core.mic_recorder',
        'core.presets_manager',
        'core.settings_manager',
        'core.theme_manager',
        'core.upload_manager',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Qt modules unused on Windows (no Wayland, no QML)
        'PyQt6.QtQuick', 'PyQt6.QtQml', 'PyQt6.QtWebEngine',
        'PyQt6.QtWebEngineCore', 'PyQt6.QtBluetooth', 'PyQt6.QtPositioning',
        'PyQt6.QtSensors', 'PyQt6.QtLocation', 'PyQt6.Qt3D',
        'PyQt6.QtPdf', 'PyQt6.QtPdfWidgets', 'PyQt6.QtNfc',
        # Standard library bloat
        'tkinter', 'unittest', 'email', 'html', 'http', 'xmlrpc',
        'xml', 'pydoc', 'doctest', 'difflib', 'ftplib', 'imaplib',
        'poplib', 'smtplib', 'telnetlib', 'nntplib',
        # Scientific stack not used
        'matplotlib', 'scipy', 'pandas', 'PIL', 'IPython',
        'sklearn', 'skimage', 'sympy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
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
    bootloader_ignore_signals=False,
    strip=False,      # strip doesn't work reliably on Windows DLLs
    upx=True,
    console=False,
    icon=str(ASSETS_DIR / 'fthr_logo.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[
        # Don't UPX-compress these — they either break or gain nothing
        'vcruntime*.dll', 'api-ms-*.dll', 'msvcp*.dll',
        'FTHRClips.exe',  # the C++ engine
    ],
    name='FTHRClips',
)
