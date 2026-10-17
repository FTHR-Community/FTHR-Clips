from __future__ import annotations

import ast
import hashlib
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
sys.path.insert(0, str(ROOT / 'FTHR_Uploader'))

import core.upload_manager as core_uploader
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
    assert spec.expected_sha256 == core_uploader.EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256


def test_windows_uploader_spec_is_unchanged(monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'win32')
    spec = core_uploader._uploader_spec()
    assert spec.filename == 'FTHR-Uploader.fthrplugin'
    assert spec.entrypoint == 'FTHR Uploader.exe'
    assert spec.expected_sha256 == core_uploader.EXPECTED_UPLOADER_BUNDLE_SHA256


def test_platform_data_root_uses_xdg_data_home_on_linux(monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'linux')
    monkeypatch.setenv('XDG_DATA_HOME', '/tmp/fthr-xdg-data')
    monkeypatch.delenv('LOCALAPPDATA', raising=False)
    assert core_uploader._platform_data_root() == Path('/tmp/fthr-xdg-data')


def test_platform_data_root_defaults_to_local_share_on_linux(monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'linux')
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    assert core_uploader._platform_data_root() == Path.home() / '.local' / 'share'


def test_linux_manifest_without_linux_binding_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'linux')
    bundle = _bundle(
        tmp_path,
        filename='FTHR-Uploader-linux.fthrplugin',
        plugin_id=core_uploader.UPLOADER_PLUGIN_ID,
        plugin_version=core_uploader.UPLOADER_PLUGIN_VERSION,
        entrypoint='FTHR-Uploader',
        legal_versions={
            'terms_version': core_uploader.UPLOADER_TERMS_VERSION,
            'privacy_version': core_uploader.UPLOADER_PRIVACY_VERSION,
        },
    )
    uploader_root = tmp_path / 'installed-uploader'
    manager = core_uploader.UploadManager(_CoreSettings())
    with (
            patch.object(manager, 'bundle_path', return_value=bundle),
            patch.object(core_uploader, 'EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256', ''),
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
            patch.object(core_uploader, '_SETTINGS_FILE', tmp_path / 'settings.json')):
        with pytest.raises(RuntimeError, match='no verified Linux'):
            manager.uploader_legal_text()
        ok, message = manager.activate_plugin(
            core_uploader.UPLOADER_TERMS_VERSION,
            core_uploader.UPLOADER_PRIVACY_VERSION,
        )
    assert not ok
    assert 'no verified Linux' in message
    assert not uploader_root.exists() or not any(uploader_root.iterdir())


def test_linux_activation_restores_uploader_executable_bit(tmp_path, monkeypatch):
    monkeypatch.setattr(core_uploader.sys, 'platform', 'linux')
    uploader_bundle = _bundle(
        tmp_path,
        filename='FTHR-Uploader-linux.fthrplugin',
        plugin_id=core_uploader.UPLOADER_PLUGIN_ID,
        plugin_version=core_uploader.UPLOADER_PLUGIN_VERSION,
        entrypoint='FTHR-Uploader',
        legal_versions={
            'terms_version': core_uploader.UPLOADER_TERMS_VERSION,
            'privacy_version': core_uploader.UPLOADER_PRIVACY_VERSION,
        },
    )
    uploader_root = tmp_path / 'installed-uploader'
    manager = core_uploader.UploadManager(_CoreSettings())
    with (
            patch.object(manager, 'bundle_path', return_value=uploader_bundle),
            patch.object(
                core_uploader, 'EXPECTED_UPLOADER_LINUX_BUNDLE_SHA256',
                _sha256(uploader_bundle)),
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
            patch.object(core_uploader, '_SETTINGS_FILE', tmp_path / 'settings.json'),
            patch.object(core_uploader, '_HISTORY_FILE', tmp_path / 'history.json'),
            patch.object(
                core_uploader, '_LEGACY_HISTORY_FILE', tmp_path / 'legacy.json')):
        ok, message = manager.activate_plugin(
            core_uploader.UPLOADER_TERMS_VERSION,
            core_uploader.UPLOADER_PRIVACY_VERSION,
        )
        assert ok, message
        assert manager.is_plugin_installed()
    executable = uploader_root / core_uploader.UPLOADER_PLUGIN_VERSION / 'FTHR-Uploader'
    assert executable.stat().st_mode & 0o111 == 0o111
    receipt = json.loads((uploader_root / 'activation.json').read_text(encoding='utf-8'))
    assert receipt['executable'] == str(executable)


def test_uploader_installs_only_after_consent(tmp_path, monkeypatch):
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
    uploader_root = tmp_path / 'installed-uploader'
    settings_file = tmp_path / 'uploader-settings.json'
    manager = core_uploader.UploadManager(_CoreSettings())

    def bundle_path(filename='FTHR-Uploader.fthrplugin'):
        return uploader_bundle

    with (
            patch.object(manager, 'bundle_path', side_effect=bundle_path),
            patch.object(
                core_uploader, 'EXPECTED_UPLOADER_BUNDLE_SHA256',
                _sha256(uploader_bundle)),
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
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
            'FTHR-Uploader.fthrplugin',)):
        pytest.skip('build optional packages with tools/build_optional_uploaders.py first')


def test_built_dormant_packages_match_core_release_bindings(built_windows_plugins):
    uploader = ROOT / 'plugin-packages' / 'FTHR-Uploader.fthrplugin'
    assert _sha256(uploader) == core_uploader.EXPECTED_UPLOADER_BUNDLE_SHA256
    with zipfile.ZipFile(uploader) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert set(archive.namelist()) == {
            'manifest.json',
            'TERMS_OF_SERVICE.txt',
            'PRIVACY_POLICY.txt',
            *(entry['path'] for entry in manifest['files']),
        }
        assert 'payload/FTHR Uploader.exe' in archive.namelist()
        assert 'payload/licenses/optional-uploader/paramiko/LICENSE' in archive.namelist()
        for entry in manifest['files']:
            assert hashlib.sha256(archive.read(entry['path'])).hexdigest() == entry['sha256']


def test_built_one_shot_packages_activate_and_run_locally(tmp_path, built_windows_plugins):
    uploader_root = tmp_path / 'uploader'
    settings_file = tmp_path / 'settings.json'
    with (
            patch.object(core_uploader, '_UPLOADER_ROOT', uploader_root),
            patch.object(
                core_uploader, '_UPLOADER_ACTIVATION_FILE',
                uploader_root / 'activation.json'),
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
        # Exercise SFTP inside the packaged executable without connecting to
        # a server or writing a credential to the user's vault.
        ok, message = manager.sftp_credentials({
            'sftp_host': 'example.com',
            'sftp_username': 'tester',
            'sftp_fingerprint': 'SHA256:' + 'A' * 43,
        })
        assert ok, message



@pytest.mark.parametrize('provider', ['lustful', 'fthr'])
def test_retired_provider_is_disabled_on_load(tmp_path, monkeypatch, provider):
    settings = tmp_path / 'settings.json'
    settings.write_text(json.dumps({
        'upload_provider': provider, 'upload_enabled': True,
        'lustful_api_key': 'obsolete', 'lustful_legal_accepted_version': 'obsolete',
    }), encoding='utf-8')
    monkeypatch.setattr(core_uploader, '_SETTINGS_FILE', settings)
    manager = core_uploader.UploadManager(_CoreSettings())
    assert not manager.get('upload_enabled')
    assert manager.get('upload_provider') == 'catbox'
    assert not any(key.startswith('lustful_') for key in manager._settings)
    manager.save_settings()
    assert not json.loads(settings.read_text())['upload_enabled']
