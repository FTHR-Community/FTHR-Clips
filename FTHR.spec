# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FTHR Clips Windows bundle.
# Run on Windows: pyinstaller FTHR.spec --clean
from pathlib import Path

block_cipher = None

ROOT       = Path(SPECPATH)
UI_DIR     = ROOT / 'FTHR_UI'
ASSETS_DIR = UI_DIR / 'assets'
ENGINE_EXE = ROOT / 'FTHRcapture' / 'x64' / 'Release' / 'FTHRClips.exe'

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_EXE), '.'),
    ],
    datas=[
        (str(ASSETS_DIR / 'fthr_logo.png'),   'assets'),
        (str(ASSETS_DIR / 'fonts'),            'assets/fonts'),
        (str(ASSETS_DIR / 'icons'),            'assets/icons'),
        (str(ASSETS_DIR / 'sounds'),           'assets/sounds'),
    ],
    hiddenimports=[
        'PyQt6.QtMultimedia',
        'PyQt6.QtMultimediaWidgets',
        'sounddevice',
        'numpy',
        'cv2',
        'imageio_ffmpeg',
        'keyboard',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FTHRClips',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ASSETS_DIR / 'fthr_logo.png'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='FTHRClips',
)
