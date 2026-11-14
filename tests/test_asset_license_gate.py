"""Release-gate tests for the AUDIT-013 asset provenance allowlist."""

import hashlib
import json
import subprocess
import sys
import wave
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import verify_release_licenses as vrl  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fixture(tmp_path: Path, *, status: str = 'approved') -> tuple[Path, bytes]:
    content = b'approved image bytes'
    asset = tmp_path / 'assets' / 'logo.png'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(content)
    notice = tmp_path / 'licenses' / 'asset-notice.txt'
    notice.parent.mkdir(parents=True)
    notice.write_text('fixture notice\n', encoding='utf-8')
    manifest = {
        'schema_version': 1,
        'allowed_redistribution_status': 'approved',
        'scanned_source_roots': ['assets'],
        'asset_extensions': ['.mp3', '.png'],
        'origins': {
            'fixture': {
                'type': 'project-generated',
                'notice': 'licenses/asset-notice.txt',
            },
        },
        'assets': [{
            'path': 'assets/logo.png',
            'sha256': _sha(content),
            'media_type': 'image/png',
            'usage': ['fixture'],
            'distributions': ['windows'],
            'artifact_paths': {
                'windows': ['_internal/assets/logo.png'],
            },
            'license': 'MIT',
            'copyright': 'Copyright fixture',
            'origin': 'fixture',
            'redistribution_status': status,
        }],
    }
    manifest_path = tmp_path / 'tools' / 'release_asset_manifest.json'
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    return tmp_path, content


def test_real_asset_manifest_and_generated_outputs_are_current():
    rep = vrl.Report()
    vrl.check_asset_manifest(ROOT, rep)
    assert not rep.failures, rep.failures
    result = subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'generate_release_assets.py'),
         '--check'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_bundled_sounds_are_generated_pcm_wav_files():
    sound_dir = ROOT / 'FTHR_UI' / 'assets' / 'sounds'
    expected_names = {
        'clip_captured.wav',
        'error.wav',
        'screenshot_saved.wav',
        'startup.wav',
        'upload_failed.wav',
        'upload_successful.wav',
    }
    assert {path.name for path in sound_dir.iterdir()} == expected_names

    hashes = set()
    for name in expected_names:
        sound = sound_dir / name
        hashes.add(_sha(sound.read_bytes()))
        with wave.open(str(sound), 'rb') as audio:
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 44_100
            assert audio.getnframes() > 0

    assert len(hashes) == len(expected_names)


def test_asset_manifest_rejects_hash_drift(tmp_path):
    root, _ = _fixture(tmp_path)
    (root / 'assets' / 'logo.png').write_bytes(b'modified')
    rep = vrl.Report()
    vrl.check_asset_manifest(root, rep)
    assert any('SHA-256 differs' in failure for failure in rep.failures)


def test_asset_manifest_rejects_undocumented_file(tmp_path):
    root, _ = _fixture(tmp_path)
    (root / 'assets' / 'unknown.mp3').write_bytes(b'unknown sample')
    rep = vrl.Report()
    vrl.check_asset_manifest(root, rep)
    assert any('undocumented assets' in failure for failure in rep.failures)


def test_asset_manifest_rejects_unresolved_status(tmp_path):
    root, _ = _fixture(tmp_path, status='UNRESOLVED')
    rep = vrl.Report()
    vrl.check_asset_manifest(root, rep)
    assert any('redistribution status' in failure for failure in rep.failures)


def test_asset_artifact_accepts_exact_allowlist(tmp_path):
    source, content = _fixture(tmp_path / 'source')
    artifact = tmp_path / 'artifact'
    path = artifact / '_internal' / 'assets' / 'logo.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    rep = vrl.Report()
    vrl.check_asset_artifact(
        artifact, rep, 'windows', manifest_root=source)
    assert not rep.failures, rep.failures


def test_asset_artifact_rejects_unapproved_extra_file(tmp_path):
    source, content = _fixture(tmp_path / 'source')
    artifact = tmp_path / 'artifact'
    path = artifact / '_internal' / 'assets' / 'logo.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    (path.parent / 'legacy.mp3').write_bytes(b'unknown')
    rep = vrl.Report()
    vrl.check_asset_artifact(
        artifact, rep, 'windows', manifest_root=source)
    assert any('unapproved assets' in failure for failure in rep.failures)


def test_asset_artifact_rejects_unapproved_os_metadata(tmp_path):
    source, content = _fixture(tmp_path / 'source')
    artifact = tmp_path / 'artifact'
    path = artifact / '_internal' / 'assets' / 'logo.png'
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    (path.parent / 'desktop.ini').write_text(
        '[LocalizedFileNames]\n', encoding='utf-8')
    rep = vrl.Report()
    vrl.check_asset_artifact(
        artifact, rep, 'windows', manifest_root=source)
    assert any('desktop.ini' in failure for failure in rep.failures)


def _folder_manifest_entry():
    manifest = json.loads((ROOT / 'tools' / 'release_asset_manifest.json').read_text(encoding='utf-8'))
    entry, = [item for item in manifest['assets']
              if item['path'] == 'FTHR_UI/assets/icons/folder.svg']
    return manifest, entry


def test_folder_is_exact_original_generated_geometry_with_separate_mit_origin():
    import generate_release_assets as generator

    manifest, entry = _folder_manifest_entry()
    data = (ROOT / entry['path']).read_bytes()
    assert data == generator.folder_svg() == generator.folder_svg()
    assert len(data) == 310
    assert _sha(data) == entry['sha256'] == 'f1cedb48514b45d1222b28d14e990c2828104e763fc9b759d3606bb9d5f422a4'
    # Exact old-byte identity from the external asset-fix-before backup.
    assert _sha(data) != 'bc15fec3acb56ddee493ffcf0fdd8d44e8c46846009489428f69a094299cf99f'
    assert entry['origin'] == 'fthr-folder-geometry-20261031'
    assert entry['license'] == 'MIT' and entry['media_type'] == 'image/svg+xml'
    assert entry['distributions'] == ['windows', 'linux']
    assert entry['artifact_paths'] == {
        'windows': ['_internal/assets/icons/folder.svg'],
        'linux': ['usr/share/fthrclips/assets/icons/folder.svg'],
    }
    assert manifest['origins'][entry['origin']] == {
        'type': 'project-generated',
        'creation_date': '2026-10-31',
        'generator': 'tools/generate_release_assets.py',
        'generator_function': 'folder_svg',
        'generator_version': 1,
        'external_creative_inputs': [],
        'description': 'New original 24x24 folder outline specified as polygon and line geometry; replaces prior unresolved bytes, not copied or relicensed from them.',
        'notice': 'licenses/FTHR-GENERATED-ASSETS.txt',
    }
    assert generator.GENERATOR_VERSION == 1


def test_folder_renders_transparent_nonblank24px_without_foreign_content(qapp):
    from PySide6.QtCore import QSize, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    _manifest, entry = _folder_manifest_entry()
    data = (ROOT / entry['path']).read_bytes()
    document = ET.fromstring(data)
    assert b'<!DOCTYPE' not in data and b'<!ENTITY' not in data
    namespace = '{http://www.w3.org/2000/svg}'
    assert document.attrib == {'width': '24', 'height': '24', 'viewBox': '0 0 24 24'}
    assert [element.tag for element in document.iter()] == [
        namespace + name for name in ('svg', 'g', 'polygon', 'line')]
    for element in document.iter():
        for key, value in element.attrib.items():
            assert key not in ('href', 'src') and not key.startswith('on')
            assert 'url(' not in value.lower() and '://' not in value
    renderer = QSvgRenderer(data)
    assert renderer.isValid() and renderer.defaultSize() == QSize(24, 24)
    image = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()
    pixels = [image.pixelColor(x, y) for y in range(24) for x in range(24)]
    assert 0 < sum(pixel.alpha() > 0 for pixel in pixels) < 24 * 24
    assert image.pixelColor(0, 0).alpha() == 0
    assert image.pixelColor(12, 14).alpha() == 0
    assert all(image.pixelColor(x, y).alpha() == 0
               for x, y in ([(i, 0) for i in range(24)] + [(i, 23) for i in range(24)]
                            + [(0, i) for i in range(24)] + [(23, i) for i in range(24)]))
    assert any(pixel.alpha() > 200 and all(abs(actual - expected) <= 2
               for actual, expected in zip(pixel.getRgb()[:3], (180, 189, 199), strict=True))
               for pixel in pixels)


@pytest.mark.parametrize('platform', ['windows', 'linux'])
def test_folder_real_entry_passes_exact_mini_artifact_and_rejects_tampering(tmp_path, platform):
    manifest, entry = _folder_manifest_entry()
    source = tmp_path / 'source'
    manifest_file = source / 'tools' / 'release_asset_manifest.json'
    manifest_file.parent.mkdir(parents=True)
    mini = {
        'schema_version': manifest['schema_version'],
        'allowed_redistribution_status': manifest['allowed_redistribution_status'],
        'scanned_source_roots': ['FTHR_UI/assets/icons'],
        'asset_extensions': ['.svg'],
        'origins': {entry['origin']: manifest['origins'][entry['origin']]},
        'assets': [entry],
    }
    manifest_file.write_text(json.dumps(mini), encoding='utf-8')
    artifact = tmp_path / 'artifact'
    path, = [artifact / relative for relative in entry['artifact_paths'][platform]]
    path.parent.mkdir(parents=True)
    data = (ROOT / entry['path']).read_bytes()
    path.write_bytes(data)
    rep = vrl.Report()
    vrl.check_asset_artifact(artifact, rep, platform, manifest_root=source)
    assert not rep.failures, rep.failures
    path.write_bytes(data + b'<!--tampered-->')
    rejected = vrl.Report()
    vrl.check_asset_artifact(artifact, rejected, platform, manifest_root=source)
    assert any('asset hash mismatch' in failure and path.relative_to(artifact).as_posix() in failure
               for failure in rejected.failures), rejected.failures
