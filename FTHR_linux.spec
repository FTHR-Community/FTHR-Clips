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
    # and is unavailable on the Ubuntu 22.04 build baseline. FTHR
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

# The CLI tools the exporter and watermarking shell out to. RPATH
# `$ORIGIN/../lib:$ORIGIN` (patched by fetch_third_party.py) resolves the
# libraries beside them in _internal/. Verified by banner/NEEDED/RPATH in the
# licence gate; strip=True is fine because they are not hash-pinned.
_FFMPEG_TOOLS = [_FFMPEG_ROOT / 'bin' / n for n in ('ffmpeg', 'ffprobe')]
_missing_tools = [p.name for p in _FFMPEG_TOOLS if not p.is_file()]
if _missing_tools:
    raise SystemExit(
        'FTHR_linux.spec: the pinned FFmpeg CLI tools are missing: '
        f'{", ".join(_missing_tools)} (expected in {_FFMPEG_ROOT / "bin"}).\n'
        '  Re-run `python tools/fetch_third_party.py --ffmpeg-linux --force`.')

a = Analysis(
    [str(UI_DIR / 'main.py')],
    pathex=[str(UI_DIR)],
    binaries=[
        (str(ENGINE_BIN), '.'),
        (str(PLAYBACK_MIXER), '.'),
        (_PORTAUDIO, '.'),
        # The verified LGPL FFmpeg the engine was built against.
        *[(str(p), '.') for p in _FFMPEG_LIBS],
        *[(str(p), '.') for p in _FFMPEG_TOOLS],
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
        (str(ASSETS_DIR / 'gatze.png'), 'assets'),
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
        # The capture card runs this file in a child interpreter, and the
        # editor loads its QML scenes and precompiled shader (color.frag.qsb)
        # from beside ui/quick_*.py at runtime.
        (str(UI_DIR / 'ui' / 'capture_card_process.py'), 'ui'),
        *_asset_tree(UI_DIR / 'ui' / 'qml', 'ui/qml'),
    ],
    hiddenimports=[
        'PySide6.QtDBus',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        # The 2.0 editor renders previews and snapshots in QQuickWidget scenes.
        'PySide6.QtQuick',
        'PySide6.QtQuickWidgets',
        'PySide6.QtQml',
        'PySide6.QtNetwork',
        'sounddevice',
        'numpy',
        'cv2',
        'jeepney',
        'jeepney.io.blocking',
        'jeepney.io.threading',
        'core.portal_shortcuts',
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
        'core.instance_activation',
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
        # Qt modules we don't use. Quick/QML power the editor's video preview
        # and snapshots, so they are filtered below rather than excluded.
        'PySide6.QtWebEngine',
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

# Qt hooks collect every optional QML module and its libraries. Excludes stop
# Python imports but not hook-added binaries, so filter the actual artifact
# TOCs as a second, deterministic boundary: keep the QML runtime the
# application's QtQuick and QtMultimedia scenes import, and only the Qt
# libraries reviewed in tools/qt_runtime_manifest.json. The Virtual Keyboard
# is a GPL-only Qt module and never ships.
import json as _json  # noqa: E402
_REVIEWED_QT = _json.loads(
    (ROOT / 'tools' / 'qt_runtime_manifest.json').read_text(encoding='utf-8'))
_APPROVED_QT_SONAMES = {
    f'libqt6{module.lower()}.so'
    for module in _REVIEWED_QT['approved_runtime_qt_modules']['linux']}
# Destination of the wheel's QML import tree, e.g. pyside6/qt/qml/.
_QT_QML_DEST = (_QT6_PLUGIN_DEST.parent / 'qml').as_posix().casefold() + '/'
_KEPT_QML_DIRS = ('qtqml', 'qtqml/models', 'qtqml/workerscript',
                  'qtquick', 'qtmultimedia')
# The compiler runtime stays on the host. PyInstaller puts _internal first on
# LD_LIBRARY_PATH, so a bundled libstdc++/libgcc_s built on the Ubuntu 22.04
# baseline shadows the host's newer copy for every library the process or its
# children dlopen; Mesa's VA-API driver (radeonsi_drv_video.so via
# libSPIRV-Tools) then fails with "GLIBCXX_3.4.32 not found" and the engine
# silently falls back to software encoding. Every supported host (glibc 2.35
# baseline and newer) ships a libstdc++ at least as new as the one the bundle
# was built against.
_HOST_TOOLCHAIN_LIBS = {'libstdc++.so.6', 'libgcc_s.so.1'}


def _keep_reviewed_qt_runtime(entry):
    dest = str(entry[0]).replace('\\', '/').casefold()
    name = dest.rsplit('/', 1)[-1]
    forbidden_prefixes = ('libqt6virtualkeyboard',)
    # QML debugger/profiler plugins are development tooling, and
    # libqmldbg_quick3dprofiler links the GPL-only Quick 3D.
    if '/plugins/qmltooling/' in dest:
        return False
    if dest.startswith(_QT_QML_DEST):
        qml_parent = dest[len(_QT_QML_DEST):].rpartition('/')[0]
        if qml_parent not in _KEPT_QML_DIRS:
            return False
    if name.startswith('libqt6') and '.so' in name:
        if name.split('.so', 1)[0] + '.so' not in _APPROVED_QT_SONAMES:
            return False
    return (name not in _SKIPPED_QT_PLUGINS
            and name not in _HOST_TOOLCHAIN_LIBS
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
