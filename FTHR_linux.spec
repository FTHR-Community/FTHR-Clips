# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FTHR Clips — Linux AppImage bundle.
# Run on Linux: pyinstaller FTHR_linux.spec --clean
from pathlib import Path
import sys as _sys

ROOT       = Path(SPECPATH)
UI_DIR     = ROOT / 'FTHR_UI'
ASSETS_DIR = UI_DIR / 'assets'
PLUGIN_BUNDLE = ROOT / 'plugin-packages' / 'FTHR-Uploader-linux.fthrplugin'
if not PLUGIN_BUNDLE.is_file():
    raise SystemExit(
        f'FTHR_linux.spec: missing {PLUGIN_BUNDLE}. '
        'Run tools/build_linux_uploader.py first.')
ENGINE_BIN = ROOT / 'FTHRcapture_linux' / 'build' / 'FTHRclips'
PLAYBACK_MIXER = ROOT / 'FTHRcapture_linux' / 'build' / 'libFTHRPlaybackMixer.so'

# Product version comes from FTHR_UI/version.py — never retype it here.
_sys.path.insert(0, str(UI_DIR))
from version import APP_ID  # noqa: E402

import sysconfig as _sysconfig  # noqa: E402
import PySide6 as _pyside6  # noqa: E402
from PySide6.QtCore import QLibraryInfo  # noqa: E402

_MULTIARCH = _sysconfig.get_config_var('MULTIARCH') or 'x86_64-linux-gnu'

# Use plugins from the pinned PySide6 wheel, never a host Qt installation with
# a potentially different minor version or licensing inventory.
_PYSIDE_ROOT = Path(_pyside6.__file__).resolve().parent
_QT6_PLUG = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)).resolve()
try:
    _QT6_PLUG_REL = _QT6_PLUG.relative_to(_PYSIDE_ROOT)
except ValueError as exc:
    raise SystemExit(
        f'FTHR_linux.spec: PySide6 plugin path {_QT6_PLUG} is outside '
        f'the pinned wheel at {_PYSIDE_ROOT}') from exc
_QT6_PLUGIN_DEST = Path('PySide6') / _QT6_PLUG_REL

_SKIPPED_QT_PLUGINS = {
    # The wheel plugin targets libtiff.so.5, which is not shipped by the wheel
    # and is unavailable on the supported Ubuntu 24.04 build baseline. FTHR
    # does not load TIFF assets, so carrying a plugin that cannot load is worse
    # than omitting that optional format handler.
    'libqtiff.so',
}


def _so(subdir):
    d = _QT6_PLUG / subdir
    dest = (_QT6_PLUGIN_DEST / subdir).as_posix()
    return [
        (str(p), dest)
        for p in d.glob('*.so')
        if p.name not in _SKIPPED_QT_PLUGINS
    ] if d.exists() else []


def _find_lib(soname):
    """Locate a system library in flat or multiarch distro layouts."""
    import ctypes.util
    for cand in (f'/usr/lib/{_MULTIARCH}/{soname}',
                 f'/usr/lib/{soname}',
                 f'/usr/lib64/{soname}',
                 f'/lib/{_MULTIARCH}/{soname}'):
        if Path(cand).exists():
            return cand
    # Last resort: ask the dynamic linker.
    stem = soname.split('.so')[0].removeprefix('lib')
    found = ctypes.util.find_library(stem)
    if found and Path(found).exists():
        return found
    raise SystemExit(
        f'FTHR_linux.spec: required system library {soname!r} not found.\n'
        f'  Arch:          sudo pacman -S portaudio\n'
        f'  Debian/Ubuntu: sudo apt install libportaudio2\n'
        f'  Fedora:        sudo dnf install portaudio')


_PORTAUDIO = _find_lib('libportaudio.so.2')

_WINDOWS_METADATA_NAMES = {'desktop.ini', 'thumbs.db', 'ehthumbs.db'}


def _asset_tree(source, destination):
    """Collect an asset tree without host-OS metadata files."""
    source = Path(source)
    entries = []
    for path in source.rglob('*'):
        if not path.is_file():
            continue
        if path.name.casefold() in _WINDOWS_METADATA_NAMES:
            continue
        relative_parent = path.parent.relative_to(source)
        target = (Path(destination) / relative_parent).as_posix()
        entries.append((str(path), target))
    return entries

# Bundle the pinned FFmpeg used to compile the engine. PyInstaller may also
# collect system FFmpeg through Qt/OpenCV; build_linux.sh removes unapproved
# copies before packaging.
_FFMPEG_ROOT = ROOT / 'FTHRcapture_linux' / 'third_party' / 'ffmpeg'
_FFMPEG_LIB = _FFMPEG_ROOT / 'lib'

if not _FFMPEG_LIB.is_dir():
    raise SystemExit(
        'FTHR_linux.spec: the pinned LGPL FFmpeg is missing.\n'
        f'  Expected: {_FFMPEG_LIB}\n'
        '  Fetch it: python tools/fetch_third_party.py --ffmpeg-linux\n'
        '  Bundling the distribution FFmpeg instead is what AUDIT-014 records:\n'
        '  it is a GPL build and the licence gate will refuse to package it.')

# Real files only — the .so and .so.N entries are symlinks into these, and
# PyInstaller resolves and flattens them anyway.
_FFMPEG_LIBS = sorted(
    p for p in _FFMPEG_LIB.glob('lib*.so.*')
    if p.is_file() and not p.is_symlink()
)
if len(_FFMPEG_LIBS) < 7:
    raise SystemExit(
        f'FTHR_linux.spec: expected 7 FFmpeg libraries in {_FFMPEG_LIB}, '
        f'found {len(_FFMPEG_LIBS)}. Re-run '
        f'`python tools/fetch_third_party.py --ffmpeg-linux --force`.')

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_BIN), '.'),
        (str(PLAYBACK_MIXER), '.'),
        (_PORTAUDIO, '.'),
        # The verified LGPL FFmpeg the engine was built against.
        *[(str(p), '.') for p in _FFMPEG_LIBS],
        *_so('platforms'),
        *_so('wayland-decoration-client'),
        *_so('wayland-shell-integration'),
        *_so('wayland-graphics-integration-client'),
        *_so('imageformats'),
    ],
    datas=[
        (str(ASSETS_DIR / 'favicon.ico'),    'assets'),
        (str(ASSETS_DIR / 'fthr_logo.png'),  'assets'),
        (str(ASSETS_DIR / 'preview_desktop.png'), 'assets'),
        (str(ASSETS_DIR / 'gary.png'), 'assets'),
        (str(PLUGIN_BUNDLE), 'plugin-packages'),
        # Licence paperwork must travel INSIDE the bundle (AUDIT-005), so a
        # portable copy is as complete as an installed one.
        (str(ROOT / 'LICENSE'), '.'),
        (str(ROOT / 'THIRD_PARTY_NOTICES.md'), '.'),
        (str(ROOT / 'licenses'), 'licenses'),
        (str(ROOT / 'tools' / 'release_asset_manifest.json'), 'licenses'),
        # LGPLv3 obliges us to ship FFmpeg's licence text with the binaries,
        # and the manifest is what tools/verify_release_licenses.py checks the
        # shipped libraries against.
        (str(_FFMPEG_ROOT / 'LICENSE.txt'), 'licenses/ffmpeg'),
        (str(ROOT / 'tools' / 'ffmpeg_manifest_linux.json'), 'licenses/ffmpeg'),
        *_asset_tree(ASSETS_DIR / 'fonts',   'assets/fonts'),
        *_asset_tree(ASSETS_DIR / 'icons',   'assets/icons'),
        *_asset_tree(ASSETS_DIR / 'sounds',  'assets/sounds'),
    ],
    hiddenimports=[
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'sounddevice',
        'numpy',
        'cv2',
        'keyboard',
        'keyboard.mouse',
        'keyboard._mouse_event',
        # UI submodules
        'ui.capture_card',
        'ui.capture_card_client',
        'ui.capture_card_process',
        'ui.capture_settings_widget',
        'ui.clip_grid',
        'ui.clip_viewer',
        'ui.customize_page',
        'ui.app_style',
        'ui.screenshot_editor',
        'ui.style',
        'ui.upload_settings_widget',
        # Core submodules
        'core.audio_mixer',
        'core.camera_recorder',
        'core.capture_bridge',
        'core.focus_monitor',
        'core.ffmpeg_playback',
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
        # Exclude imageio_ffmpeg; use the project-pinned FFmpeg runtime beside
        # the engine. See core/ffmpeg_tools.py.
        'imageio_ffmpeg',
        # Qt modules we don't use (Widgets-only app)
        'PySide6.QtQuick', 'PySide6.QtQml', 'PySide6.QtWebEngine',
        'PySide6.QtWebEngineCore', 'PySide6.QtBluetooth', 'PySide6.QtPositioning',
        'PySide6.QtSensors', 'PySide6.QtLocation', 'PySide6.Qt3D',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtNfc',
        # GPL-only Qt modules are outside the reviewed LGPL runtime.
        'PySide6.QtCanvasPainter', 'PySide6.QtCoap', 'PySide6.QtGraphs',
        'PySide6.QtGrpc', 'PySide6.QtHttpServer', 'PySide6.QtLottie',
        'PySide6.QtMqtt', 'PySide6.QtNetworkAuth', 'PySide6.QtQmlCompiler',
        'PySide6.QtQuick3D', 'PySide6.QtVirtualKeyboard',
        'PySide6.QtWaylandCompositor',
        # Standard library bloat
        'tkinter', 'unittest', 'html', 'xmlrpc',
        'xml', 'pydoc', 'doctest', 'difflib', 'ftplib', 'imaplib',
        'poplib', 'smtplib', 'telnetlib', 'nntplib',
        # Scientific stack not used
        'matplotlib', 'scipy', 'pandas', 'PIL', 'IPython',
        'sklearn', 'skimage', 'sympy',
    ],
    noarchive=False,
)

# QtGui's generic hook also sees the optional Virtual Keyboard plugin. It is a
# GPL-only Qt module and drags in unused QML/Quick libraries. Excludes stop
# Python imports but not hook-added binaries, so filter the actual artifact
# TOCs as a second, deterministic boundary.
def _keep_reviewed_qt_runtime(entry):
    dest = str(entry[0]).replace('\\', '/').casefold()
    name = dest.rsplit('/', 1)[-1]
    forbidden_prefixes = ('libqt6virtualkeyboard',)
    return (name not in _SKIPPED_QT_PLUGINS
            and 'virtualkeyboard' not in dest
            and not name.startswith(forbidden_prefixes))


a.binaries = [entry for entry in a.binaries
              if _keep_reviewed_qt_runtime(entry)]
a.datas = [entry for entry in a.datas
           if _keep_reviewed_qt_runtime(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_ID,
    debug=False,
    strip=True,
    upx=False,    # AppImage has its own compression; UPX on .so files can break things
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=True,
    upx=False,
    name=APP_ID,
)
