"""The Windows and Linux uploader builders share one plugin identity and
cooperate on FTHR_UI/core/uploader_bundle_manifest.py."""

from __future__ import annotations

import runpy
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WINDOWS_BUILDER = runpy.run_path(str(ROOT / 'tools' / 'build_optional_uploaders.py'))
render = WINDOWS_BUILDER['render_core_manifest']


def _manifest(windows: str, linux: str | None = None) -> str:
    return render('', windows_hash=windows, linux_hash=linux)


def test_windows_build_keeps_the_linux_bundle_hash():
    existing = _manifest('old-windows', 'linux-hash')
    result = render(existing, windows_hash='new-windows')
    assert "EXPECTED_UPLOADER_BUNDLE_SHA256 = 'new-windows'" in result
    assert "EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = 'linux-hash'" in result


def test_linux_build_keeps_the_windows_bundle_hash():
    existing = _manifest('windows-hash')
    result = render(existing, linux_hash='new-linux')
    assert "EXPECTED_UPLOADER_BUNDLE_SHA256 = 'windows-hash'" in result
    assert "EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = 'new-linux'" in result


def test_rendering_is_stable_for_the_committed_manifest():
    committed = (ROOT / 'FTHR_UI' / 'core' / 'uploader_bundle_manifest.py').read_text(
        encoding='utf-8')
    assert render(committed) == committed


def test_regenerated_manifest_takes_versions_from_the_shared_builder():
    stale = '''UPLOADER_PLUGIN_VERSION = '1.1.0'
UPLOADER_TERMS_VERSION = '2026-08-24-v1'
EXPECTED_UPLOADER_BUNDLE_SHA256 = 'windows'
EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = 'linux'
'''
    namespace: dict[str, object] = {}
    exec(render(stale, linux_hash='new-linux'), namespace)  # noqa: S102
    assert namespace['UPLOADER_PLUGIN_VERSION'] == WINDOWS_BUILDER['UPLOADER_PLUGIN_VERSION']
    assert namespace['UPLOADER_TERMS_VERSION'] == WINDOWS_BUILDER['UPLOADER_TERMS_VERSION']
    assert namespace['UPLOADER_PRIVACY_VERSION'] == WINDOWS_BUILDER['UPLOADER_PRIVACY_VERSION']
    assert namespace['EXPECTED_UPLOADER_BUNDLE_SHA256'] == 'windows'
    assert namespace['EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256'] == 'new-linux'


def test_linux_builder_has_no_private_copy_of_the_plugin_constants():
    source = (ROOT / 'tools' / 'build_linux_uploader.py').read_text(encoding='utf-8')
    for literal in ('1.1.0', '1.4.0', '2026-08-24-v1', '2026-10-15-v3'):
        assert literal not in source
    assert 'FTHR_Hardware_ID' not in source
    assert "'sftp_provider'" in source


def test_requirement_check_reports_missing_and_mismatched_pins(tmp_path):
    requirements = tmp_path / 'requirements.txt'
    requirements.write_text(
        '# comment\n'
        'pytest==0.0.1\n'
        'fthr-package-that-does-not-exist==1.0\n'
        "never-on-this-platform==1.0 ; sys_platform == 'nonexistent-os'\n",
        encoding='utf-8')
    problems = WINDOWS_BUILDER['missing_uploader_requirements'](requirements)
    assert any(problem.startswith('pytest ') for problem in problems)
    assert 'fthr-package-that-does-not-exist is not installed' in problems
    assert not any('never-on-this-platform' in problem for problem in problems)
    assert len(problems) == 2


def test_every_bundle_carries_the_whole_uploader_licence_inventory():
    files = WINDOWS_BUILDER['uploader_license_files']()
    packages = {path.relative_to(ROOT / 'licenses' / 'optional-uploader').parts[0]
                for path in files}
    assert {'paramiko', 'keyring', 'SecretStorage', 'jeepney', 'pywin32-ctypes'} <= packages
