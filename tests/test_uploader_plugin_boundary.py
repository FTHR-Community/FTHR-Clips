from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
sys.path.insert(0, str(ROOT / 'FTHR_Uploader'))
sys.path.insert(0, str(ROOT / 'FTHR_Hardware_ID'))

import core.upload_manager as core_uploader
import hardware_id_service
import uploader_service


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _CoreSettings:
    def __init__(self):
        self.settings = {}

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def save_settings(self):
        return True


def _bundle(
        root: Path,
        *,
        filename: str,
        plugin_id: str,
        plugin_version: str,
        entrypoint: str,
        legal_versions: dict[str, str]) -> Path:
    executable = b'MZ-independent-test-package'
    manifest = {
        'schema_version': 1,
        'plugin_id': plugin_id,
        'plugin_version': plugin_version,
        **legal_versions,
        'entrypoint': entrypoint,
        'files': [{
            'path': f'payload/{entrypoint}',
            'sha256': hashlib.sha256(executable).hexdigest(),
            'size': len(executable),
        }],
    }
    path = root / filename
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('TERMS_OF_SERVICE.txt', 'Test terms')
        archive.writestr('PRIVACY_POLICY.txt', 'Test privacy')
        archive.writestr(f'payload/{entrypoint}', executable)
    return path


def test_linux_uploader_spec_uses_linux_bundle_and_entrypoint(monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'linux')
    spec = core_uploader._uploader_spec()
    assert spec.filename == 'FTHR-Uploader-linux.fthrplugin'
    assert spec.entrypoint == 'FTHR-Uploader'


def test_linux_uploader_manifest_binds_a_platform_specific_hash():
    assert core_uploader.EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256
    assert core_uploader.EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 != (
        core_uploader.EXPECTED_UPLOADER_BUNDLE_SHA256)


def test_linux_builder_preserves_other_manifest_bindings():
    import runpy

    builder = runpy.run_path(str(ROOT / 'tools' / 'build_linux_uploader.py'))
    source = '''EXPECTED_UPLOADER_BUNDLE_SHA256 = 'windows'
EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = 'old-linux'
EXPECTED_HARDWARE_BUNDLE_SHA256 = 'hardware'
'''
    result = builder['bind_linux_bundle_hash'](source, 'new-linux')
    assert "EXPECTED_UPLOADER_BUNDLE_SHA256 = 'windows'" in result
    assert "EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256 = 'new-linux'" in result
    assert "EXPECTED_HARDWARE_BUNDLE_SHA256 = 'hardware'" in result


def test_uploader_and_hardware_identity_install_only_after_separate_consents(tmp_path, monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'win32')
    uploader_bundle = _bundle(
        tmp_path,
        filename='FTHR-Uploader.fthrplugin',
        plugin_id=core_uploader.UPLOADER_PLUGIN_ID,
        plugin_version=core_uploader.UPLOADER_PLUGIN_VERSION,
        entrypoint='FTHR Uploader.exe',
        legal_versions={
            'terms_version': core_uploader.UPLOADER_TERMS_VERSION,
            'privacy_version': core_uploader.UPLOADER_PRIVACY_VERSION,
        },
    )
    hardware_bundle = _bundle(
        tmp_path,
        filename='FTHR-Hardware-Identity.fthrplugin',
        plugin_id=core_uploader.HARDWARE_PLUGIN_ID,
        plugin_version=core_uploader.HARDWARE_PLUGIN_VERSION,
        entrypoint='FTHR Hardware Identity.exe',
        legal_versions={'policy_version': core_uploader.HARDWARE_POLICY_VERSION},
    )
    uploader_root = tmp_path / 'installed-uploader'
    hardware_root = tmp_path / 'installed-hardware'
    settings_file = tmp_path / 'uploader-settings.json'
    manager = core_uploader.UploadManager(_CoreSettings())

    def bundle_path(filename='FTHR-Uploader.fthrplugin'):
        return hardware_bundle if 'Hardware' in filename else uploader_bundle

    with (
            patch.object(manager, 'bundle_path', side_effect=bundle_path),
            patch.object(
                core_uploader, 'EXPECTED_UPLOADER_BUNDLE_SHA256',
                _sha256(uploader_bundle)),
            patch.object(
                core_uploader, 'EXPECTED_HARDWARE_BUNDLE_SHA256',
                _sha256(hardware_bundle)),
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(core_uploader, '_HARDWARE_ROOT', hardware_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
            patch.object(
                core_uploader, '_HARDWARE_ACTIVATION_FILE',
                hardware_root / 'activation.json'),
            patch.object(core_uploader, '_SETTINGS_FILE', settings_file),
            patch.object(core_uploader, '_HISTORY_FILE', tmp_path / 'history.json'),
            patch.object(
                core_uploader, '_LEGACY_HISTORY_FILE', tmp_path / 'legacy.json')):
        ok, _ = manager.activate_plugin('wrong', 'wrong')
        assert not ok
        assert not manager.is_plugin_installed()

        ok, message = manager.activate_plugin(
            core_uploader.UPLOADER_TERMS_VERSION,
            core_uploader.UPLOADER_PRIVACY_VERSION,
        )
        assert ok, message
        assert manager.is_plugin_installed()
        assert not manager.is_hardware_identity_installed()

        ok, _ = manager.activate_hardware_identity('wrong')
        assert not ok
        assert not manager.is_hardware_identity_installed()

        ok, message = manager.activate_hardware_identity(
            core_uploader.HARDWARE_POLICY_VERSION)
        assert ok, message
        assert manager.is_hardware_identity_installed()


def test_disabled_uploader_refuses_network_actions(tmp_path):
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({'upload_enabled': False}), encoding='utf-8')
    activation_id = 'test-activation'
    receipt = tmp_path / 'activation.json'
    receipt.write_text(json.dumps({
        'plugin_id': uploader_service.PLUGIN_ID,
        'plugin_version': uploader_service.PLUGIN_VERSION,
        'terms_version': uploader_service.TERMS_VERSION,
        'privacy_version': uploader_service.PRIVACY_VERSION,
        'activation_id': activation_id,
        'executable_sha256': _sha256(Path(sys.executable)),
        'settings_file': str(settings),
    }), encoding='utf-8')
    request = {'action': 'upload', 'activation_id': activation_id}
    try:
        uploader_service._validate_activation(receipt, request)
    except PermissionError as exc:
        assert 'disabled' in str(exc)
    else:
        raise AssertionError('disabled uploader accepted a network action')


def test_disabled_state_persists_across_manager_restart(tmp_path):
    settings = tmp_path / 'settings.json'
    first = core_uploader.UploadManager(_CoreSettings())
    with (
            patch.object(core_uploader, '_SETTINGS_FILE', settings),
            patch.object(first, 'is_plugin_installed', return_value=True)):
        ok, message = first.set_plugin_enabled(True)
        assert ok, message
        assert json.loads(settings.read_text(encoding='utf-8'))['upload_enabled'] is True

        ok, message = first.set_plugin_enabled(False)
        assert ok, message
        assert json.loads(settings.read_text(encoding='utf-8'))['upload_enabled'] is False

        restarted = core_uploader.UploadManager(_CoreSettings())
        assert restarted.get('upload_enabled') is False
        assert not restarted.is_enabled()


def test_toggle_reports_save_failure_and_restores_previous_state():
    manager = core_uploader.UploadManager(_CoreSettings())
    manager.set('upload_enabled', True)
    with (
            patch.object(manager, 'is_plugin_installed', return_value=True),
            patch.object(manager, 'save_settings', return_value=False)):
        ok, message = manager.set_plugin_enabled(False)

    assert not ok
    assert 'could not be saved' in message
    assert manager.get('upload_enabled') is True


def test_hardware_identity_requires_its_own_activation_receipt(tmp_path):
    try:
        hardware_id_service._validate_activation(
            tmp_path / 'missing.json',
            {'activation_id': 'missing'},
        )
    except PermissionError as exc:
        assert 'not been installed' in str(exc)
    else:
        raise AssertionError('hardware identity accepted a missing receipt')


def test_derived_hardware_id_is_stable_and_does_not_expose_raw_value():
    raw = 'windows-machine-guid:secret-raw-value'
    first = hardware_id_service.hardware_uuid(raw)
    second = hardware_id_service.hardware_uuid(raw)
    assert first == second
    assert raw not in first
    assert len(first) == 36


def test_core_has_no_provider_network_client_and_uploader_has_no_hardware_reader():
    core_source = (ROOT / 'FTHR_UI' / 'core' / 'upload_manager.py').read_text(
        encoding='utf-8')
    core_tree = ast.parse(core_source)
    core_imports = {
        alias.name
        for node in ast.walk(core_tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ''
        for node in ast.walk(core_tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not core_imports.intersection({
        'urllib.request', 'http.client', 'requests', 'httpx', 'aiohttp', 'socket'})

    uploader_imports = set()
    for path in (ROOT / 'FTHR_Uploader').glob('*.py'):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        uploader_imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names)
        uploader_imports.update(
            node.module or ''
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom))
    assert 'winreg' not in uploader_imports
    assert 'platform' not in uploader_imports
    assert 'uuid' not in uploader_imports


def test_consent_ui_is_the_only_activation_callsite():
    callsites = []
    for path in (ROOT / 'FTHR_UI').rglob('*.py'):
        if path.name == 'upload_manager.py':
            continue
        source = path.read_text(encoding='utf-8')
        if '.activate_plugin(' in source or '.activate_hardware_identity(' in source:
            callsites.append(path.relative_to(ROOT).as_posix())
    assert callsites == ['FTHR_UI/ui/upload_settings_widget.py']


@pytest.fixture
def built_windows_plugins():
    if sys.platform != 'win32':
        pytest.skip('frozen optional packages contain Windows executables')
    packages = ROOT / 'plugin-packages'
    if not all((packages / name).is_file() for name in (
            'FTHR-Uploader.fthrplugin', 'FTHR-Hardware-Identity.fthrplugin')):
        pytest.skip('build optional packages with tools/build_optional_uploaders.py first')


def test_built_dormant_packages_match_core_release_bindings(built_windows_plugins):
    uploader = ROOT / 'plugin-packages' / 'FTHR-Uploader.fthrplugin'
    hardware = ROOT / 'plugin-packages' / 'FTHR-Hardware-Identity.fthrplugin'
    assert _sha256(uploader) == core_uploader.EXPECTED_UPLOADER_BUNDLE_SHA256
    assert _sha256(hardware) == core_uploader.EXPECTED_HARDWARE_BUNDLE_SHA256
    with zipfile.ZipFile(uploader) as archive:
        assert set(archive.namelist()) == {
            'manifest.json',
            'TERMS_OF_SERVICE.txt',
            'PRIVACY_POLICY.txt',
            'payload/FTHR Uploader.exe',
        }
    with zipfile.ZipFile(hardware) as archive:
        assert set(archive.namelist()) == {
            'manifest.json',
            'TERMS_OF_SERVICE.txt',
            'PRIVACY_POLICY.txt',
            'payload/FTHR Hardware Identity.exe',
        }


def test_built_one_shot_packages_activate_and_run_locally(tmp_path, built_windows_plugins):
    uploader_root = tmp_path / 'uploader'
    hardware_root = tmp_path / 'hardware'
    settings_file = tmp_path / 'settings.json'
    with (
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(core_uploader, '_HARDWARE_ROOT', hardware_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
            patch.object(
                core_uploader, '_HARDWARE_ACTIVATION_FILE',
                hardware_root / 'activation.json'),
            patch.object(core_uploader, '_SETTINGS_FILE', settings_file),
            patch.object(core_uploader, '_HISTORY_FILE', tmp_path / 'history.json'),
            patch.object(
                core_uploader, '_LEGACY_HISTORY_FILE', tmp_path / 'legacy.json')):
        manager = core_uploader.UploadManager(_CoreSettings())
        ok, message = manager.activate_plugin(
            core_uploader.UPLOADER_TERMS_VERSION,
            core_uploader.UPLOADER_PRIVACY_VERSION,
        )
        assert ok, message
        response = manager._invoke_plugin('account_status', timeout=30)
        assert response.get('ok'), response

        ok, message = manager.activate_hardware_identity(
            core_uploader.HARDWARE_POLICY_VERSION)
        assert ok, message
        receipt_path = hardware_root / 'activation.json'
        receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
        completed = subprocess.run(
            [receipt['executable'], '--activation-receipt', str(receipt_path)],
            input=json.dumps({
                'action': 'derive_hardware_id',
                'activation_id': receipt['activation_id'],
            }),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        result = json.loads(completed.stdout.strip())
        assert result.get('ok'), result
        assert len(result.get('hardware_id', '')) == 36
