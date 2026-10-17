"""The release gate refuses a bundle without the editor's QML scenes."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'verify_release_licenses_qml', ROOT / 'tools' / 'verify_release_licenses.py')
vrl = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vrl)


def _source_tree(root: Path) -> None:
    for name in vrl.QML_SCENE_FILES:
        path = root / vrl.QML_SCENE_SOURCE_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f'source {name}\n'.encode())


def _artifact(root: Path, source: Path, skip: str | None = None) -> None:
    for name in vrl.QML_SCENE_FILES:
        if name == skip:
            continue
        path = root / vrl.QML_SCENE_ARTIFACT_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((source / vrl.QML_SCENE_SOURCE_DIR / name).read_bytes())


def test_complete_bundle_passes(tmp_path):
    source, artifact = tmp_path / 'src', tmp_path / 'dist'
    _source_tree(source)
    _artifact(artifact, source)
    for platform in ('windows', 'linux'):
        rep = vrl.Report()
        vrl.check_qml_scenes(artifact, rep, platform, manifest_root=source)
        assert not rep.failures, rep.failures


def test_each_missing_scene_fails(tmp_path):
    source = tmp_path / 'src'
    _source_tree(source)
    for name in vrl.QML_SCENE_FILES:
        artifact = tmp_path / f'dist-{name}'
        _artifact(artifact, source, skip=name)
        rep = vrl.Report()
        vrl.check_qml_scenes(artifact, rep, 'windows', manifest_root=source)
        assert rep.failures == [
            f'windows artifact: application QML file missing: '
            f'{vrl.QML_SCENE_ARTIFACT_DIR}/{name}']


def test_stale_scene_fails(tmp_path):
    source, artifact = tmp_path / 'src', tmp_path / 'dist'
    _source_tree(source)
    _artifact(artifact, source)
    (artifact / vrl.QML_SCENE_ARTIFACT_DIR / 'color.frag.qsb').write_bytes(b'old')
    rep = vrl.Report()
    vrl.check_qml_scenes(artifact, rep, 'linux', manifest_root=source)
    assert len(rep.failures) == 1 and 'differs' in rep.failures[0]


def test_gate_files_match_the_real_sources():
    for name in vrl.QML_SCENE_FILES:
        assert (ROOT / vrl.QML_SCENE_SOURCE_DIR / name).is_file()


def test_specs_anchor_the_qml_filter_to_the_qt_tree():
    # A bare '/qml/' match also hits the application's ui/qml/ scenes.
    for spec in ('FTHR.spec', 'FTHR_linux.spec'):
        text = (ROOT / spec).read_text(encoding='utf-8')
        assert "'/qml/' in dest" not in text, spec
        assert re.search(r'dest\.startswith\(_QT_QML_DEST\)', text), spec
