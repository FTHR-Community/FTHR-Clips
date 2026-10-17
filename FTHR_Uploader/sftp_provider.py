"""Password SFTP with explicit server identity and OS-protected credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import posixpath
import re
import secrets
import stat
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import paramiko


class SftpError(RuntimeError):
    """Safe user-facing error; never contains a server response or password."""


def public_base_url(settings):
    if not settings.get('sftp_public_enabled', False):
        return ''
    value = str(settings.get('sftp_public_base_url', '')).strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)
                or any(ord(c) < 33 for c in value) or '\\' in value):
            raise ValueError()
    except ValueError as exc:
        raise SftpError('Enter a public HTTPS folder URL without credentials, query or fragment.') from exc
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/') + '/', '', ''))


def configuration(settings):
    public_base_url(settings)
    host = str(settings.get('sftp_host', '')).strip().rstrip('.').lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode('idna').decode('ascii')
        except UnicodeError as exc:
            raise SftpError('Enter a valid SFTP host domain or IP address.') from exc
        if (len(host) > 253 or not host or any(
                not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                for part in host.split('.'))):
            raise SftpError('Enter a host domain or IP address without a URL or path.') from None
    try:
        port = int(settings.get('sftp_port', 22))
    except (ValueError, TypeError) as exc:
        raise SftpError('SFTP port must be between 1 and 65535.') from exc
    if not 1 <= port <= 65535:
        raise SftpError('SFTP port must be between 1 and 65535.')
    username = str(settings.get('sftp_username', '')).strip()
    if not username or any(ord(c) < 32 for c in username):
        raise SftpError('Enter an SFTP username without control characters.')
    folder = str(settings.get('sftp_directory', '.') or '.').strip()
    if not folder or any(ord(c) < 32 for c in folder) or '\\' in folder:
        raise SftpError('Enter a remote folder using forward slashes.')
    fingerprint = str(settings.get('sftp_fingerprint', '')).strip()
    if not fingerprint:
        return host, port, username, folder, ''
    if not re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint):
        raise SftpError('Enter the server SHA256 host-key fingerprint from its operator.')
    try:
        digest = base64.b64decode(fingerprint[7:] + '=', validate=True)
    except ValueError as exc:
        raise SftpError('Invalid SHA256 host-key fingerprint.') from exc
    if base64.b64encode(digest).decode().rstrip('=') != fingerprint[7:]:
        raise SftpError('Invalid SHA256 host-key fingerprint.')
    return host, port, username, folder, fingerprint


def _vault():
    # Select a trusted backend explicitly; never allow a plaintext keyring fallback.
    if sys.platform != 'win32':
        raise SftpError('Saved SFTP passwords currently require Windows Credential Manager.')
    from keyring.backends.Windows import WinVaultKeyring
    return WinVaultKeyring()


def _credential_identity(settings):
    host, port, username, _, _ = configuration(settings)
    identity = hashlib.sha256(f'{host}\0{port}\0{username}'.encode()).hexdigest()
    return f'FTHR Clips/SFTP/{identity}', username


def save_password(settings, password):
    if not isinstance(password, str) or not password:
        raise SftpError('Enter a password to save.')
    service, username = _credential_identity(settings)
    try:
        _vault().set_password(service, username, password)
    except Exception as exc:
        raise SftpError('Could not save the password in Windows Credential Manager.') from exc


def forget_password(settings):
    service, username = _credential_identity(settings)
    try:
        vault = _vault()
        if vault.get_password(service, username) is not None:
            vault.delete_password(service, username)
    except Exception as exc:
        raise SftpError('Could not remove the saved SFTP password.') from exc


def _password(settings):
    supplied = settings.get('sftp_password')
    if supplied:
        return str(supplied)
    service, username = _credential_identity(settings)
    try:
        value = _vault().get_password(service, username)
    except Exception as exc:
        raise SftpError('Could not read Windows Credential Manager.') from exc
    if not value:
        raise SftpError('Enter and save an SFTP password first.')
    return value


class _PinnedKey(paramiko.MissingHostKeyPolicy):
    def __init__(self, fingerprint):
        self.fingerprint = fingerprint

    def missing_host_key(self, client, hostname, key):
        actual = 'SHA256:' + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        if not hmac.compare_digest(actual, self.fingerprint):
            raise SftpError('SFTP host-key mismatch. Connection stopped before sending credentials.')


def known_hosts_paths():
    return (Path.home() / '.ssh' / 'known_hosts',
            Path.home() / '.fthr' / 'uploader' / 'known_hosts')


class _UnknownHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        raise SftpError(
            'This SFTP host is not trusted yet. Enter its SHA256 fingerprint '
            'from the server operator and test the connection once, or add its '
            'verified key to your SSH known_hosts file.')


def remember_verified_host(host, port, key):
    """Store an already verified key; never called for an unverified peer."""
    target = known_hosts_paths()[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    keys = paramiko.HostKeys()
    if target.exists():
        keys.load(str(target))
    name = host if port == 22 else f'[{host}]:{port}'
    keys.add(name, key.get_name(), key)
    fd, temporary = tempfile.mkstemp(prefix='known_hosts-', dir=target.parent)
    os.close(fd)
    try:
        keys.save(temporary)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def connection(settings, *, require_remembered_host=False):
    host, port, username, folder, fingerprint = configuration(settings)
    password = _password(settings)
    client = paramiko.SSHClient()
    try:
        if fingerprint:
            # An explicit pin remains authoritative, including mismatches.
            client.set_missing_host_key_policy(_PinnedKey(fingerprint))
        else:
            for path in known_hosts_paths():
                if path.exists():
                    client.load_system_host_keys(str(path))
            client.set_missing_host_key_policy(_UnknownHost())
        client.connect(
            host, port=port, username=username, password=password,
            allow_agent=False, look_for_keys=False,
            timeout=8, banner_timeout=8, auth_timeout=8, channel_timeout=8,
            disabled_algorithms={
                'keys': ['ssh-rsa', 'ssh-dss'],
                'pubkeys': ['ssh-rsa', 'ssh-dss'],
                'kex': ['diffie-hellman-group1-sha1', 'diffie-hellman-group14-sha1',
                        'diffie-hellman-group-exchange-sha1'],
                'ciphers': ['3des-cbc', 'blowfish-cbc', 'aes128-cbc', 'aes192-cbc', 'aes256-cbc'],
                'macs': ['hmac-md5', 'hmac-md5-96', 'hmac-sha1', 'hmac-sha1-96'],
            },
        )
        with client.open_sftp() as sftp:
            sftp.get_channel().settimeout(20)
            directory = sftp.normalize(folder)
            if not stat.S_ISDIR(sftp.stat(directory).st_mode):
                raise SftpError('The configured remote folder is not a directory.')
            if fingerprint:
                # Only remember keys after pin verification and successful
                # authentication. A cache write failure must not abort uploads.
                try:
                    remember_verified_host(host, port, client.get_transport().get_remote_server_key())
                except (OSError, paramiko.SSHException, ValueError):
                    if require_remembered_host:
                        raise SftpError('Host verified, but its trusted key could not be saved.') from None
            yield sftp, directory, host, port
    except SftpError:
        raise
    except paramiko.AuthenticationException as exc:
        raise SftpError('SFTP authentication failed. Check the username and password.') from exc
    except paramiko.BadHostKeyException as exc:
        raise SftpError('SFTP host-key mismatch. Connection stopped before sending credentials. '
                        'Verify the changed key with the server operator.') from exc
    except (paramiko.hostkeys.InvalidHostKey, ValueError) as exc:
        raise SftpError('Could not read trusted SSH host keys. Check your known_hosts files.') from exc
    except (paramiko.SSHException, OSError, EOFError) as exc:
        raise SftpError('SFTP connection or transfer failed. Check the server, folder and permissions.') from exc
    finally:
        client.close()


def test_connection(settings):
    with connection(settings, require_remembered_host=True):
        return True, 'SFTP authenticated; host key verified and remote folder found. Write access is checked on upload.'


def upload(settings, path: Path):
    base_url = public_base_url(settings)
    with connection(settings) as (sftp, folder, host, port):
        # Opaque, unique names also avoid disclosing local filenames to third parties.
        suffix = path.suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,10}', suffix):
            suffix = '.clip'
        remote = posixpath.join(folder, f'fthr-{secrets.token_hex(16)}{suffix}')
        temporary = remote + '.part'
        try:
            with sftp.open(temporary, 'wx') as target:
                sftp.chmod(temporary, 0o600)
                with path.open('rb') as source:
                    while block := source.read(1024 * 1024):
                        target.write(block)
            if sftp.stat(temporary).st_size != path.stat().st_size:
                raise SftpError('Remote file size verification failed; the local clip was retained.')
            # Standard SFTP rename fails if the final filename already exists.
            sftp.rename(temporary, remote)
            if sftp.stat(remote).st_size != path.stat().st_size:
                raise SftpError('Final remote size verification failed; the local clip was retained.')
            if base_url:
                # Public access starts only after the complete file has been verified.
                sftp.chmod(remote, 0o644)
        except Exception:
            try:
                sftp.remove(temporary)
            except OSError:
                pass
            raise
        authority = f'[{host}]' if ':' in host else host
        sftp_url = f'sftp://{authority}:{port}{quote(remote, safe="/")}'
        public_url = base_url + quote(posixpath.basename(remote), safe='') if base_url else ''
        url = public_url or sftp_url
        return {'url': url, 'raw_url': url, 'sftp_url': sftp_url,
                'public_url': public_url, 'remote_path': remote, 'favorite': False}
