from __future__ import annotations

import base64
import hashlib
import os
import socket
import sys
import threading
from pathlib import Path

import paramiko
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_Uploader'))
import sftp_provider as sftp
import upload_runtime
import uploader_service


def _fingerprint(key):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')


@pytest.fixture
def server_key():
    return paramiko.RSAKey.generate(2048)


@pytest.fixture(autouse=True)
def trusted_files(monkeypatch, tmp_path_factory):
    directory = tmp_path_factory.mktemp('trusted-hosts')
    # The frozen extension is a separate process; isolate its real Path.home()
    # as well as the source provider's monkeypatched paths.
    monkeypatch.setenv('USERPROFILE', str(directory))
    paths = (directory / 'ssh_known_hosts', directory / 'app_known_hosts')
    monkeypatch.setattr(sftp, 'known_hosts_paths', lambda: paths)
    return paths


@pytest.fixture
def server(tmp_path, server_key):
    """Real local SSH/SFTP peer, with authentication tracking and sandboxed files."""
    key = server_key
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    # The frozen credential-save process can take several seconds to unpack
    # before a second process opens the actual connection. Keep listening
    # until this fixture's teardown, not an arbitrary five-second deadline.
    listener.settimeout(0.1)
    stop = threading.Event()
    attempts = []
    failures = []
    permissions = []

    class Auth(paramiko.ServerInterface):
        def check_auth_password(self, username, password):
            attempts.append((username, password))
            return paramiko.AUTH_SUCCESSFUL if (username, password) == ('tester', ' secret ') else paramiko.AUTH_FAILED

        def get_allowed_auths(self, username):
            return 'password'

        def check_channel_request(self, kind, chanid):
            return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    class Files(paramiko.SFTPServerInterface):
        def _path(self, path):
            resolved = (tmp_path / path.lstrip('/')).resolve()
            if not resolved.is_relative_to(tmp_path):
                raise PermissionError()
            return resolved

        def stat(self, path):
            try:
                return paramiko.SFTPAttributes.from_stat(self._path(path).stat())
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def open(self, path, flags, attr):
            try:
                fd = os.open(self._path(path), flags | getattr(os, 'O_BINARY', 0), 0o600)
                stream = os.fdopen(fd, 'wb')
                handle = paramiko.SFTPHandle(flags)
                handle.writefile = stream
                return handle
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def chattr(self, path, attr):
            permissions.append(attr.st_mode)
            return paramiko.SFTP_OK

        def rename(self, oldpath, newpath):
            try:
                if self._path(newpath).exists():
                    return paramiko.SFTP_FAILURE
                self._path(oldpath).rename(self._path(newpath))
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

        def remove(self, path):
            try:
                self._path(path).unlink()
                return paramiko.SFTP_OK
            except OSError as exc:
                return paramiko.SFTPServer.convert_errno(exc.errno)

    def run():
        transport = None
        try:
            while not stop.is_set():
                try:
                    peer, _ = listener.accept()
                    break
                except TimeoutError:
                    continue
            else:
                return
            transport = paramiko.Transport(peer)
            transport.add_server_key(key)
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files)
            transport.start_server(server=Auth())
            stop.wait(8)
        except TimeoutError as exc:
            failures.append(exc)
        except (EOFError, OSError, paramiko.SSHException):
            # A rejected host key deliberately closes the peer before auth.
            pass
        except Exception as exc:
            failures.append(exc)
        finally:
            if transport:
                transport.close()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    config = {
        'upload_provider': 'sftp', 'sftp_host': '127.0.0.1',
        'sftp_port': listener.getsockname()[1], 'sftp_username': 'tester',
        'sftp_password': ' secret ', 'sftp_directory': '.',
        'sftp_fingerprint': _fingerprint(key),
    }
    yield config, tmp_path, attempts, permissions
    stop.set()
    listener.close()
    worker.join(5)
    assert not worker.is_alive()
    assert not failures


def test_real_sftp_upload_confirms_bytes_and_private_permissions(server, tmp_path, monkeypatch):
    config, remote_root, attempts, permissions = server
    clip = tmp_path / 'private local name.mp4'
    payload = bytes(range(256)) * 4097
    clip.write_bytes(payload)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert ok, message
    assert attempts == [('tester', ' secret ')]
    assert permissions == [0o600]
    remote = remote_root / info['remote_path'].lstrip('/')
    assert remote.read_bytes() == payload
    assert remote.name != clip.name
    assert not list(remote_root.glob('*.part'))
    assert 'secret' not in info['url'] and 'tester' not in info['url']
    assert clip.exists()


@pytest.mark.parametrize('store,hashed', [(0, False), (0, True), (1, False)])
def test_blank_fingerprint_uses_trusted_key(server, server_key, trusted_files, store, hashed):
    config, _, attempts, _ = server
    name = f"[{config['sftp_host']}]:{config['sftp_port']}"
    if hashed:
        name = paramiko.HostKeys.hash_host(name)
    keys = paramiko.HostKeys()
    keys.add(name, server_key.get_name(), server_key)
    keys.save(str(trusted_files[store]))
    config['sftp_fingerprint'] = ''
    assert sftp.test_connection(config)[0]
    assert attempts == [('tester', ' secret ')]


@pytest.mark.parametrize('case', ['unknown', 'wrong_port', 'changed', 'wrong_pin'])
def test_untrusted_or_changed_keys_never_authenticate(server, server_key, trusted_files, case):
    config, _, attempts, _ = server
    keys = paramiko.HostKeys()
    name = f"[{config['sftp_host']}]:{config['sftp_port']}"
    if case == 'wrong_port':
        name = config['sftp_host']
    key = paramiko.RSAKey.generate(2048) if case == 'changed' else server_key
    if case != 'unknown':
        keys.add(name, key.get_name(), key)
        keys.save(str(trusted_files[0]))
    config['sftp_fingerprint'] = (_fingerprint(paramiko.RSAKey.generate(2048))
                                  if case == 'wrong_pin' else '')
    with pytest.raises(sftp.SftpError, match='not trusted|before sending credentials'):
        sftp.test_connection(config)
    assert attempts == []
    assert not trusted_files[1].exists()


def test_verified_pin_is_remembered_without_losing_other_hosts(server, server_key, trusted_files):
    config, _, _, _ = server
    other = paramiko.RSAKey.generate(2048)
    keys = paramiko.HostKeys()
    keys.add('other.example', other.get_name(), other)
    keys.save(str(trusted_files[1]))
    assert sftp.test_connection(config)[0]
    keys.load(str(trusted_files[1]))
    assert keys.check('other.example', other)
    assert keys.check(f"[{config['sftp_host']}]:{config['sftp_port']}", server_key)


def test_blank_fingerprint_is_valid_for_saving_credentials():
    config = {'sftp_host': 'example.com', 'sftp_username': 'user'}
    assert sftp.configuration(config)[-1] == ''



def test_real_host_key_mismatch_sends_no_password(server):
    config, _, attempts, _ = server
    config['sftp_fingerprint'] = _fingerprint(paramiko.RSAKey.generate(2048))
    with pytest.raises(sftp.SftpError, match='before sending credentials'):
        sftp.test_connection(config)
    assert attempts == []


def test_real_auth_failure_is_sanitized_and_not_retried(server, monkeypatch):
    config, root, attempts, _ = server
    config['sftp_password'] = 'do-not-log-me'
    config['upload_auto_delete'] = True
    clip = root / 'clip.mp4'
    clip.write_bytes(b'clip')
    monkeypatch.setattr(upload_runtime.time, 'sleep', lambda _: pytest.fail('SFTP must not retry'))
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert not ok and 'authentication failed' in message
    assert 'do-not-log-me' not in message
    assert len(attempts) == 1 and clip.exists() and info == {}


def test_real_connection_test_verifies_directory_without_writing(server):
    config, root, _, _ = server
    ok, message = sftp.test_connection(config)
    assert ok and 'Write access is checked on upload' in message
    assert not list(root.iterdir())


def test_failed_rename_cleans_partial_and_retains_local(server, monkeypatch):
    config, root, _, _ = server
    config['upload_auto_delete'] = True
    clip = root / 'clip.mp4'
    clip.write_bytes(b'original')

    def reject(*args):
        raise PermissionError('remote error with sensitive detail')

    monkeypatch.setattr(paramiko.SFTPClient, 'rename', reject)
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert not ok and 'sensitive detail' not in message and info == {}
    assert clip.read_bytes() == b'original'
    assert not list(root.glob('*.part'))


def test_verified_sftp_success_allows_requested_local_delete(server, monkeypatch):
    config, root, _, _ = server
    config['upload_auto_delete'] = True
    clip = root / 'clip.mp4'
    clip.write_bytes(b'original')
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', root / 'history.json')
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert ok, message
    assert not clip.exists()
    assert (root / info['remote_path'].lstrip('/')).read_bytes() == b'original'


def test_public_upload_creates_https_link_and_web_readable_file(server, monkeypatch):
    config, root, _, permissions = server
    config.update(sftp_public_enabled=True,
                  sftp_public_base_url='https://videos.example.com/shared/clips')
    clip = root / 'personal name.mp4'
    clip.write_bytes(b'clip')
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', root / 'history.json')
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert ok, message
    assert permissions == [0o600, 0o644]
    filename = Path(info['remote_path']).name
    assert info['url'] == 'https://videos.example.com/shared/clips/' + filename
    assert info['raw_url'] == info['url'] == info['public_url']
    assert info['sftp_url'].startswith('sftp://127.0.0.1:')
    assert 'personal' not in info['url']
    import json
    recorded = json.loads((root / 'history.json').read_text())[str(clip)]
    assert recorded['url'] == info['url']
    from ui.clip_grid import ClipThumbnail
    assert ClipThumbnail._upload_url(recorded) == info['url']


def test_disabled_public_mapping_keeps_files_private_even_with_url(server, monkeypatch):
    config, root, _, permissions = server
    config.update(sftp_public_enabled=False,
                  sftp_public_base_url='https://videos.example.com/clips/')
    clip = root / 'clip.mp4'
    clip.write_bytes(b'clip')
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', root / 'history.json')
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert ok, message
    assert permissions == [0o600]
    assert info['url'].startswith('sftp://') and info['public_url'] == ''


def test_public_permission_failure_retains_local_and_returns_no_link(server, monkeypatch):
    config, root, _, permissions = server
    config.update(sftp_public_enabled=True, sftp_public_base_url='https://example.com/clips/',
                  upload_auto_delete=True)
    clip = root / 'clip.mp4'
    clip.write_bytes(b'clip')
    original = paramiko.SFTPClient.chmod

    def chmod(client, path, mode):
        if mode == 0o644:
            raise PermissionError('Permission change rejected')
        return original(client, path, mode)

    monkeypatch.setattr(paramiko.SFTPClient, 'chmod', chmod)
    ok, message, info = upload_runtime.UploadRuntime(config).upload(str(clip))
    assert not ok and info == {}
    assert clip.exists() and permissions == [0o600]
    assert not list(root.glob('*.part'))


@pytest.mark.parametrize('value', [
    '', 'http://example.com/clips', 'https://' + 'user:secret@' + 'example.com/clips',
    'https://example.com/clips?token=secret', 'https://example.com/clips#fragment',
    'https://example.com:0/clips', 'https://example.com:invalid/clips',
    'https:///clips', 'file:///clips', 'https://example.com/a\x00b',
    'https://example.com\\evil/clips',
])
def test_public_mapping_rejects_unsafe_url_before_network(value, monkeypatch):
    monkeypatch.setattr(paramiko, 'SSHClient', lambda: pytest.fail('Must validate before network'))
    with pytest.raises(sftp.SftpError, match='public HTTPS folder URL'):
        sftp.upload({'sftp_public_enabled': True, 'sftp_public_base_url': value}, Path('clip.mp4'))


def test_public_mapping_normalizes_trailing_slash():
    settings = {'sftp_public_enabled': True, 'sftp_public_base_url': 'https://example.com/clips/'}
    assert sftp.public_base_url(settings) == 'https://example.com/clips/'
    settings['sftp_public_base_url'] = 'https://example.com'
    assert sftp.public_base_url(settings) == 'https://example.com/'


@pytest.mark.parametrize('key,value', [
    ('sftp_host', 'https://example.com/upload'), ('sftp_host', 'host@evil'),
    ('sftp_port', 0), ('sftp_port', 'invalid'), ('sftp_username', ''),
    ('sftp_directory', 'bad\x00path'),
    ('sftp_fingerprint', 'MD5:00:11'),
])
def test_invalid_config_fails_before_network(key, value):
    config = {'sftp_host': 'example.com', 'sftp_port': 22, 'sftp_username': 'user',
              'sftp_fingerprint': 'SHA256:' + base64.b64encode(b'x' * 32).decode().rstrip('=')}
    config[key] = value
    with pytest.raises(sftp.SftpError):
        sftp.configuration(config)


def test_vault_password_is_scoped_and_never_returned(monkeypatch):
    config = {'sftp_host': 'example.com', 'sftp_port': 22, 'sftp_username': 'user',
              'sftp_fingerprint': 'SHA256:' + base64.b64encode(b'x' * 32).decode().rstrip('=')}
    values = {}

    class Vault:
        def set_password(self, service, username, password):
            values[service, username] = password

        def get_password(self, service, username):
            return values.get((service, username))

        def delete_password(self, service, username):
            values.pop((service, username))

    monkeypatch.setattr(sftp, '_vault', Vault)
    response = uploader_service._perform({'action': 'sftp_save', 'config': {
        **config, 'sftp_password': ' secret '}}, {})
    assert response['ok'] and 'secret' not in str(response)
    assert sftp._password(config) == ' secret '
    with pytest.raises(sftp.SftpError, match='save an SFTP password'):
        sftp._password({**config, 'sftp_port': 2222})
    sftp.forget_password(config)
    assert not values


def test_core_never_persists_password(tmp_path, monkeypatch):
    from core import upload_manager
    from test_uploader_plugin_boundary import _CoreSettings
    monkeypatch.setattr(upload_manager, '_SETTINGS_FILE', tmp_path / 'settings.json')
    manager = upload_manager.UploadManager(_CoreSettings())
    with pytest.raises(ValueError, match='credential vault'):
        manager.set('sftp_password', 'secret')
    manager._settings['sftp_password'] = 'secret'
    assert manager.save_settings()
    assert 'secret' not in (tmp_path / 'settings.json').read_text()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows frozen uploader')
def test_frozen_extension_connects_to_real_sftp(server, tmp_path, monkeypatch):
    from core import upload_manager
    from test_uploader_plugin_boundary import _CoreSettings
    config, _, attempts, _ = server
    root = tmp_path / 'installed'
    monkeypatch.setattr(upload_manager, '_UPLOADER_ROOT', root)
    monkeypatch.setattr(upload_manager, '_UPLOADER_ACTIVATION_FILE', root / 'activation.json')
    monkeypatch.setattr(upload_manager, '_SETTINGS_FILE', tmp_path / 'settings.json')
    manager = upload_manager.UploadManager(_CoreSettings())
    ok, message = manager.activate_plugin(
        upload_manager.UPLOADER_TERMS_VERSION, upload_manager.UPLOADER_PRIVACY_VERSION)
    assert ok, message
    ok, message = manager.sftp_credentials(config)
    assert ok, message
    try:
        config.pop('sftp_password')
        ok, message = manager.test_connection(config)
        assert ok, message
    finally:
        ok, message = manager.sftp_credentials(config, forget=True)
        assert ok, message
    assert attempts == [('tester', ' secret ')]
