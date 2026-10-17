"""Release-gate tests for the AUDIT-013 Qt binding decision."""

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import verify_release_licenses as vrl  # noqa: E402


REAL_MANIFEST = ROOT / 'tools' / 'qt_runtime_manifest.json'


def _write(path: Path, content: str = '') -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def test_project_license_is_the_approved_gplv3_text():
    rep = vrl.Report()
    vrl.check_project_license(ROOT, rep, 'tree')
    assert not rep.failures, rep.failures


def test_project_license_rejects_mit_or_truncated_text(tmp_path):
    _write(tmp_path / 'LICENSE', 'MIT License\n')
    rep = vrl.Report()
    vrl.check_project_license(tmp_path, rep, 'fixture')
    assert any('not the approved GPL-3.0-only text' in failure
               for failure in rep.failures)


@pytest.mark.parametrize('change', ['truncate', 'edit_body', 'edit_notice'])
def test_project_license_still_rejects_changes_with_project_notice(tmp_path, change):
    text = (ROOT / 'LICENSE').read_text(encoding='utf-8')
    if change == 'truncate':
        text = text[:len(text) // 2]
    elif change == 'edit_body':
        text = text.replace('29 June 2007', '29 June 2008')
    else:
        text = text.replace('GPL-3.0-only', 'GPL-3.0-or-later')
    _write(tmp_path / 'LICENSE', text)
    rep = vrl.Report()
    vrl.check_project_license(tmp_path, rep, 'fixture')
    assert rep.failures


def _manifest_root(tmp_path: Path) -> Path:
    (tmp_path / 'tools').mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_MANIFEST, tmp_path / 'tools' / 'qt_runtime_manifest.json')
    return tmp_path


def _source_tree(tmp_path: Path) -> Path:
    root = _manifest_root(tmp_path)
    data = json.loads(REAL_MANIFEST.read_text(encoding='utf-8'))
    for entry in data['required_ui_resources']:
        source = ROOT / entry['source']
        destination = root / entry['source']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    _write(root / 'FTHR_UI' / 'app.py', 'from PySide6.QtCore import Signal\n')
    _write(
        root / 'requirements-alpha.txt',
        '\n'.join((
            'PySide6==6.11.1',
            'PySide6-Addons==6.11.1',
            'PySide6-Essentials==6.11.1',
            'shiboken6==6.11.1',
        )),
    )
    for name in ('requirements.in', 'FTHR.spec', 'FTHR_linux.spec', 'build_linux.sh'):
        _write(root / name, 'PySide6\n')
    return root


def _license_files(root: Path) -> None:
    data = json.loads(REAL_MANIFEST.read_text(encoding='utf-8'))
    for rel in data['required_license_files']:
        _write(root / rel, f'test fixture for {rel}\n')


def _ui_resources(root: Path, platform: str) -> None:
    data = json.loads(REAL_MANIFEST.read_text(encoding='utf-8'))
    for entry in data['required_ui_resources']:
        source = ROOT / entry['source']
        relative = entry['artifact_paths'][platform]
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _windows_artifact(tmp_path: Path) -> tuple[Path, Path]:
    manifest_root = _manifest_root(tmp_path / 'source')
    artifact = tmp_path / 'artifact'
    _license_files(artifact)
    _ui_resources(artifact, 'windows')
    _write(artifact / 'PySide6' / 'QtCore.pyd')
    for module in ('Core', 'Gui', 'Widgets', 'Multimedia',
                   'MultimediaWidgets', 'Qml', 'Quick', 'QuickWidgets'):
        _write(artifact / 'PySide6' / 'Qt' / 'bin' / f'Qt6{module}.dll')
    _write(artifact / 'PySide6' / 'Qt' / 'plugins' / 'platforms' /
           'qwindows.dll')
    return artifact, manifest_root


def _linux_artifact(tmp_path: Path) -> tuple[Path, Path]:
    manifest_root = _manifest_root(tmp_path / 'source')
    artifact = tmp_path / 'artifact'
    _license_files(artifact)
    _ui_resources(artifact, 'linux')
    _write(artifact / 'PySide6' / 'QtCore.so')
    for module in ('Core', 'Gui', 'Widgets', 'Multimedia',
                   'MultimediaWidgets', 'Qml', 'Quick', 'QuickWidgets'):
        _write(artifact / 'PySide6' / 'Qt' / 'lib' /
               f'libQt6{module}.so.6')
    _write(artifact / 'PySide6' / 'Qt' / 'plugins' / 'platforms' /
           'libqxcb.so')
    _write(artifact / 'PySide6' / 'Qt' / 'plugins' / 'platforms' /
           'libqwayland.so')
    return artifact, manifest_root


def test_source_selection_accepts_pyside_lock_and_packaging(tmp_path):
    root = _source_tree(tmp_path)
    rep = vrl.Report()
    vrl.check_qt_source_selection(root, rep)
    assert not rep.failures, rep.failures


def test_source_selection_rejects_production_pyqt_import(tmp_path):
    root = _source_tree(tmp_path)
    _write(root / 'FTHR_UI' / 'legacy.py', 'from PyQt6.QtCore import QObject\n')
    rep = vrl.Report()
    vrl.check_qt_source_selection(root, rep)
    assert any('production imports' in failure for failure in rep.failures)


def test_source_selection_rejects_unreviewed_binding_version(tmp_path):
    root = _source_tree(tmp_path)
    lock = root / 'requirements-alpha.txt'
    lock.write_text(lock.read_text(encoding='utf-8').replace(
        'PySide6==6.11.1', 'PySide6==6.12.0'), encoding='utf-8')
    rep = vrl.Report()
    vrl.check_qt_source_selection(root, rep)
    assert any('lock mismatch' in failure for failure in rep.failures)


@pytest.mark.parametrize('tamper', ['missing', 'changed'])
def test_source_selection_rejects_missing_or_tampered_ui_resource(tmp_path, tamper):
    root = _source_tree(tmp_path)
    path = root / 'FTHR_UI' / 'ui' / 'qml' / 'VideoPreview.qml'
    if tamper == 'missing':
        path.unlink()
    else:
        path.write_text('tampered\n', encoding='utf-8')
    rep = vrl.Report()
    vrl.check_qt_source_selection(root, rep)
    assert rep.failures
    assert any('UI resource' in failure for failure in rep.failures)


def test_windows_artifact_accepts_reviewed_binding_and_modules(tmp_path):
    artifact, manifest_root = _windows_artifact(tmp_path)
    rep = vrl.Report()
    vrl.check_qt_artifact(
        artifact, rep, 'windows', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


def test_linux_artifact_accepts_reviewed_binding_modules_and_ui_resources(tmp_path):
    artifact, manifest_root = _linux_artifact(tmp_path)
    rep = vrl.Report()
    vrl.check_qt_artifact(
        artifact, rep, 'linux', manifest_root=manifest_root)
    assert not rep.failures, rep.failures


@pytest.mark.parametrize('platform', ['windows', 'linux'])
@pytest.mark.parametrize('tamper', ['missing', 'changed'])
def test_artifact_rejects_missing_or_tampered_ui_resource(tmp_path, platform, tamper):
    factory = _windows_artifact if platform == 'windows' else _linux_artifact
    artifact, manifest_root = factory(tmp_path)
    relative = ('_internal/ui/qml/VideoPreview.qml'
                if platform == 'windows'
                else 'usr/share/fthrclips/ui/qml/VideoPreview.qml')
    path = artifact / relative
    if tamper == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'tampered')
    rep = vrl.Report()
    vrl.check_qt_artifact(
        artifact, rep, platform, manifest_root=manifest_root)
    assert rep.failures
    assert any('UI resource' in failure for failure in rep.failures)


def test_artifact_rejects_both_qt_bindings(tmp_path):
    artifact, manifest_root = _windows_artifact(tmp_path)
    _write(artifact / 'PyQt6' / 'QtCore.pyd')
    rep = vrl.Report()
    vrl.check_qt_artifact(
        artifact, rep, 'windows', manifest_root=manifest_root)
    assert any('both' in failure for failure in rep.failures)


@pytest.mark.parametrize('module', ('Graphs', 'VirtualKeyboard'))
def test_artifact_rejects_gpl_only_qt_module(tmp_path, module):
    artifact, manifest_root = _windows_artifact(tmp_path)
    _write(artifact / 'PySide6' / 'Qt' / 'bin' / f'Qt6{module}.dll')
    rep = vrl.Report()
    vrl.check_qt_artifact(
        artifact, rep, 'windows', manifest_root=manifest_root)
    assert any('GPL-only' in failure for failure in rep.failures)
