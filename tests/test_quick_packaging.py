"""The shared video UI needs its QML files and Qt Quick on both platforms."""

import ast
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('spec_name', ['FTHR.spec', 'FTHR_linux.spec'])
def test_shared_video_ui_is_not_excluded_from_packaging(spec_name):
    tree = ast.parse((ROOT / spec_name).read_text(encoding='utf-8'))
    analysis = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'Analysis'
    )
    arguments = {keyword.arg: keyword.value for keyword in analysis.keywords}
    imports = ast.literal_eval(arguments['hiddenimports'])
    excludes = ast.literal_eval(arguments['excludes'])
    for module in ('PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets'):
        assert module in imports, (spec_name, module)
        assert module not in excludes, (spec_name, module)
    data_targets = {
        node.value for node in ast.walk(arguments['datas'])
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert 'ui/qml' in data_targets


def test_required_quick_runtime_is_not_in_manual_pruning_instructions():
    instructions = (ROOT / 'BUILDING.md').read_text(encoding='utf-8')
    assert '"$int\\Qt6Quick*"' not in instructions
    assert '"$int\\Qt6Qml*"' not in instructions


@pytest.mark.parametrize('platform', ['windows', 'linux'])
def test_quick_modules_have_an_explicit_runtime_inventory(platform):
    manifest = json.loads((ROOT / 'tools' / 'qt_runtime_manifest.json').read_text())
    modules = manifest['approved_runtime_qt_modules'][platform]
    for module in ('Qml', 'QmlCore', 'Quick', 'QuickWidgets', 'MultimediaQuick'):
        assert module in modules


def test_shared_interface_assets_have_linux_package_paths():
    manifest = json.loads((ROOT / 'tools' / 'release_asset_manifest.json').read_text())
    bundled_paths = {
        path.relative_to(ROOT).as_posix()
        for directory in ('fonts', 'icons', 'sounds')
        for path in (ROOT / 'FTHR_UI' / 'assets' / directory).iterdir()
        if path.is_file() and path.suffix in ('.ttf', '.ico', '.png', '.svg', '.wav')
    } | {'FTHR_UI/assets/gatze.png'}
    entries = {entry['path']: entry for entry in manifest['assets']}
    for path in bundled_paths:
        relative = path.removeprefix('FTHR_UI/')
        assert f'usr/share/fthrclips/{relative}' in entries[path]['artifact_paths'].get('linux', [])


@pytest.mark.parametrize('spec_name, platform', [
    ('FTHR.spec', 'windows'), ('FTHR_linux.spec', 'linux'),
])
def test_runtime_filter_preserves_app_qml_and_only_reviewed_qt_modules(spec_name, platform):
    tree = ast.parse((ROOT / spec_name).read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_keep_reviewed_qt_runtime')
    manifest = json.loads((ROOT / 'tools' / 'qt_runtime_manifest.json').read_text())
    modules = manifest['approved_runtime_qt_modules'][platform]
    namespace = {
        '_SKIPPED_QT_PLUGINS': {'libqtiff.so'},
        '_APPROVED_QT_DLLS': {f'qt6{module.lower()}.dll' for module in modules},
        '_APPROVED_QT_LIBS': {f'libqt6{module.lower()}' for module in modules},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), spec_name, 'exec'), namespace)
    keep = namespace['_keep_reviewed_qt_runtime']
    for path in ('ui/qml/VideoPreview.qml', 'ui/qml/Snapshot.qml', 'ui/qml/color.frag.qsb'):
        assert keep((path, 'source', 'DATA')), (spec_name, path)
    suffix = '.dll' if platform == 'windows' else '.so.6'
    prefix = 'Qt6' if platform == 'windows' else 'libQt6'
    assert keep((f'PySide6/{prefix}Quick{suffix}', 'source', 'BINARY'))
    for module in ('VirtualKeyboard', 'Quick3D', 'Graphs', 'UnknownModule'):
        assert not keep((f'PySide6/{prefix}{module}{suffix}', 'source', 'BINARY'))
    assert not keep(('PySide6/Qt/qml/QtQuick3D/private/plugin', 'source', 'DATA'))
    assert keep(('PySide6/Qt/qml/QtQuick/qmldir', 'source', 'DATA'))
