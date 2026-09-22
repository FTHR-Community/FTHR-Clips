"""Provider networking owned exclusively by the optional uploader package."""

from __future__ import annotations

import http.client
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fthr_upload_api import (
    DEFAULT_BASE_URL,
    FthrUploadClient,
    load_credentials,
    resolve_api_key,
)


CATBOX_URL = 'https://catbox.moe/user/api.php'
HISTORY_FILE = Path.home() / '.fthr' / 'uploader' / 'upload_history.json'
_BOUNDARY = b'FTHRClipBoundary8f61d4'
_RETRY_DELAYS = (5, 15, 45)
_PROVIDER_LIMIT_BYTES = {
    'lustful': 100 * 1024 * 1024,
    'catbox': 200 * 1024 * 1024,
    'discord_webhook': 10 * 1024 * 1024,
}
_CUSTOM_PROVIDER = 'custom'
_DISCORD_WEBHOOK_PROVIDER = 'discord_webhook'


class UploadResponseError(RuntimeError):
    """An upload response that is safe to classify before retrying."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = int(status)


class UploadRuntime:
    def __init__(
            self,
            settings: dict[str, Any],
            hardware_id_provider: Callable[[], str] | None = None):
        self.settings = dict(settings)
        self._hardware_id_provider = hardware_id_provider

    def upload(self, file_path: str) -> tuple[bool, str, dict[str, Any]]:
        path = Path(file_path)
        if not path.is_file():
            return False, 'File no longer exists.', {}
        provider = self._provider()
        limit = _PROVIDER_LIMIT_BYTES.get(provider)
        try:
            actual = path.stat().st_size
        except OSError as exc:
            return False, f'File size could not be checked: {exc}', {}
        if limit is not None and actual > limit:
            return False, (
                f'{provider.title()} accepts files up to {limit // (1024 * 1024)} MB; '
                f'this file is {actual / (1024 * 1024):.1f} MB. '
                'Use Automatically compress in FTHR Clips.'), {}
        final_error = 'Upload failed.'
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                result = (
                    self._upload_catbox(path)
                    if provider == 'catbox'
                    else self._upload_lustful(path)
                    if provider == 'lustful'
                    else self._upload_discord_webhook(path)
                    if provider == _DISCORD_WEBHOOK_PROVIDER
                    else self._upload_custom(path)
                )
                entry = {
                    'status': 'ok',
                    'provider': provider,
                    'uploaded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    **result,
                }
                self._record_success(str(path), entry)
                if self.settings.get('upload_auto_delete', False):
                    try:
                        path.unlink()
                    except OSError as exc:
                        return True, f'Uploaded, but local delete failed: {exc}', entry
                return True, str(result.get('url') or 'Upload complete.'), entry
            except Exception as exc:
                final_error = str(exc)
                if isinstance(exc, UploadResponseError) and not _retryable_status(exc.status):
                    break
                if delay is None:
                    break
                sys.stderr.write(
                    f'[Uploader] Attempt {attempt} failed; retrying in {delay}s: {exc}\n')
                time.sleep(delay)
        return False, final_error, {}

    def test_connection(self) -> tuple[bool, str]:
        provider = self._provider()
        if provider == 'lustful':
            credentials = load_credentials()
            if not credentials:
                return False, 'Create or log in to a Lustful account first.'
            hardware_id = self._hardware_id()
            if credentials.get('hwid') != hardware_id:
                return False, 'This Lustful account is registered to a different PC.'
            data = self._lustful_client().verify(str(credentials['account_id']), hardware_id)
            return True, f'Lustful connected ({data.get("role", "user")}).'
        if provider == _CUSTOM_PROVIDER:
            return self._test_custom_connection()
        if provider == _DISCORD_WEBHOOK_PROVIDER:
            return self._test_discord_webhook_connection()
        request = urllib.request.Request(CATBOX_URL, method='HEAD')
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return True, f'Catbox reachable (HTTP {response.status}).'
        except urllib.error.HTTPError as exc:
            return True, f'Catbox reachable (HTTP {exc.code}).'
        except Exception as exc:
            return False, f'Could not reach Catbox: {exc}'

    def _provider(self) -> str:
        provider = str(self.settings.get('upload_provider', 'catbox')).lower()
        if provider == 'fthr':
            provider = 'lustful'
        if provider in {'own_server', 'your_server'}:
            provider = _CUSTOM_PROVIDER
        if provider in {'discord', 'discord_webhook'}:
            provider = _DISCORD_WEBHOOK_PROVIDER
        if provider not in {'catbox', 'lustful', _CUSTOM_PROVIDER, _DISCORD_WEBHOOK_PROVIDER}:
            raise ValueError('Only Catbox, Lustful, Discord Webhook, and your server are supported.')
        return provider

    def _hardware_id(self) -> str:
        if not self._hardware_id_provider:
            raise PermissionError(
                'Install Hardware Identity after accepting Lustful’s policies.')
        return self._hardware_id_provider()

    def _lustful_client(self) -> FthrUploadClient:
        return FthrUploadClient(
            api_key=str(self.settings.get('lustful_api_key', '')),
            base_url=DEFAULT_BASE_URL,
        )

    def _upload_catbox(self, path: Path) -> dict[str, Any]:
        fields = {'reqtype': 'fileupload'}
        userhash = str(self.settings.get('catbox_userhash', '')).strip()
        if userhash:
            fields['userhash'] = userhash
        status, raw = _multipart_post(
            CATBOX_URL, fields, 'fileToUpload', path, headers={})
        text = raw.decode('utf-8', errors='replace').strip()
        if not 200 <= status < 300:
            raise UploadResponseError(text or f'Catbox returned HTTP {status}.', status)
        if not text.startswith('https://'):
            raise RuntimeError(text or 'Catbox returned an invalid upload URL.')
        return {'url': text, 'raw_url': text, 'favorite': False}

    def _upload_lustful(self, path: Path) -> dict[str, Any]:
        credentials = load_credentials()
        if not credentials:
            raise RuntimeError('Create or log in to a Lustful account first.')
        account_id = str(credentials.get('account_id', '')).strip()
        hardware_id = self._hardware_id()
        if credentials.get('hwid') != hardware_id:
            raise PermissionError('This Lustful account is registered to a different PC.')
        self._lustful_client().verify(account_id, hardware_id)
        status, raw = _multipart_post(
            f'{DEFAULT_BASE_URL}/api/upload',
            {},
            'file',
            path,
            headers={
                'Authorization': f'Bearer {resolve_api_key(str(self.settings.get("lustful_api_key", "")))}',
                'X-Account-Id': account_id,
                'Accept': 'application/json',
            },
        )
        try:
            data = json.loads(raw.decode('utf-8', errors='replace'))
        except ValueError as exc:
            raise RuntimeError(f'Lustful returned HTTP {status} with invalid JSON.') from exc
        if not 200 <= status < 300:
            message = data.get('message') or data.get('error') if isinstance(data, dict) else ''
            raise UploadResponseError(str(message or f'Lustful returned HTTP {status}.'), status)
        if not isinstance(data, dict) or data.get('ok') is False:
            message = data.get('message') or data.get('error') if isinstance(data, dict) else ''
            raise RuntimeError(str(message or 'Lustful rejected the upload.'))
        return {
            'url': str(data.get('url') or data.get('raw_url') or ''),
            'raw_url': str(data.get('raw_url') or ''),
            'file_id': data.get('id'),
            'expires_at': str(data.get('expires_at') or ''),
            'favorite': False,
        }

    def _upload_custom(self, path: Path) -> dict[str, Any]:
        """Upload to the user-selected endpoint using the FTHR multipart contract."""

        url = _custom_server_url(self.settings)
        status, raw = _multipart_post(
            url,
            {},
            'clip',
            path,
            headers=_custom_server_headers(self.settings),
            require_https=False,
        )
        text = raw.decode('utf-8', errors='replace').strip()
        if not 200 <= status < 300:
            raise UploadResponseError(text or f'Your server returned HTTP {status}.', status)

        # Custom endpoints commonly return either a bare URL or a small JSON
        # object. Preserve the useful response in history without requiring
        # one particular server framework.
        response_url = ''
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            response_url = str(data.get('url') or data.get('raw_url') or '').strip()
        elif text.startswith(('http://', 'https://')):
            response_url = text
        result = {
            'url': response_url,
            'raw_url': response_url,
            'response': text[:4096],
            'favorite': False,
        }
        return result

    def _test_custom_connection(self) -> tuple[bool, str]:
        url = _custom_server_url(self.settings)
        request = urllib.request.Request(
            url,
            method='HEAD',
            headers=_custom_server_headers(self.settings),
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return True, f'Your server reachable (HTTP {response.status}).'
        except urllib.error.HTTPError as exc:
            # A 401/404/405 still proves the endpoint is reachable. The upload
            # request will report the endpoint's actual response if it fails.
            return True, f'Your server reachable (HTTP {exc.code}).'
        except Exception as exc:
            return False, f'Could not reach your server: {exc}'

    def _upload_discord_webhook(self, path: Path) -> dict[str, Any]:
        raw_url = _discord_webhook_url(self.settings)
        parsed = urllib.parse.urlparse(raw_url)
        query = urllib.parse.parse_qs(parsed.query)
        query['wait'] = ['true']
        new_query = urllib.parse.urlencode(query, doseq=True)
        request_url = urllib.parse.urlunparse(parsed._replace(query=new_query))

        payload = json.dumps({'content': f'🎬 New clip: {path.name}'})
        fields = {'payload_json': payload}

        status, raw = _multipart_post(
            request_url,
            fields,
            'file',
            path,
            headers={},
            require_https=True,
        )
        text = raw.decode('utf-8', errors='replace').strip()
        if not 200 <= status < 300:
            raise UploadResponseError(
                text or f'Discord returned HTTP {status}.', status)

        response_url = ''
        try:
            data = json.loads(text)
            if (isinstance(data, dict)
                    and isinstance(data.get('attachments'), list)
                    and data['attachments']):
                response_url = str(data['attachments'][0].get('url') or '').strip()
        except ValueError:
            pass

        return {
            'url': response_url or 'Discord upload complete.',
            'raw_url': response_url,
            'response': text[:4096],
            'favorite': False,
        }

    def _test_discord_webhook_connection(self) -> tuple[bool, str]:
        url = _discord_webhook_url(self.settings)
        request = urllib.request.Request(
            url,
            headers={'User-Agent': 'FTHR-Clips/Desktop-Uploader'},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return True, f'Discord Webhook reachable (HTTP {response.status}).'
        except urllib.error.HTTPError as exc:
            if exc.code in {200, 204}:
                return True, f'Discord Webhook reachable (HTTP {exc.code}).'
            return False, f'Discord Webhook returned HTTP {exc.code}.'
        except Exception as exc:
            return False, f'Could not reach Discord Webhook: {exc}'

    def _record_success(self, path: str, entry: dict[str, Any]) -> None:
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
            if not isinstance(history, dict):
                history = {}
        except (OSError, ValueError, TypeError):
            history = {}
        history[path] = entry
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = HISTORY_FILE.with_suffix('.tmp')
        temporary.write_text(json.dumps(history, indent=2), encoding='utf-8')
        os.replace(str(temporary), str(HISTORY_FILE))


def _retryable_status(status: int) -> bool:
    """Retry transient server failures, never permanent client failures."""
    return status == 429 or 500 <= status < 600


def _multipart_post(
        url: str,
        fields: dict[str, str],
        file_field: str,
        file_path: Path,
        headers: dict[str, str],
        *,
        require_https: bool = True) -> tuple[int, bytes]:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('Your server URL must use http:// or https://.')
    if require_https and scheme != 'https':
        raise ValueError('Uploader provider URL must use HTTPS.')
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            b'--' + _BOUNDARY + b'\r\n'
            + f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode('utf-8')
            + str(value).encode('utf-8') + b'\r\n')
    safe_name = file_path.name.replace('"', '_').replace('\\', '_')
    content_type = mimetypes.guess_type(file_path.name)[0] or 'application/octet-stream'
    file_header = (
        b'--' + _BOUNDARY + b'\r\n'
        + f'Content-Disposition: form-data; name="{file_field}"; '
          f'filename="{safe_name}"\r\n'.encode('utf-8')
        + f'Content-Type: {content_type}\r\n\r\n'.encode('ascii'))
    footer = b'\r\n--' + _BOUNDARY + b'--\r\n'
    content_length = sum(map(len, parts)) + len(file_header) + file_path.stat().st_size + len(footer)
    request_path = parsed.path or '/'
    if parsed.query:
        request_path += '?' + parsed.query
    connection_class = (
        http.client.HTTPSConnection if scheme == 'https'
        else http.client.HTTPConnection)
    connection = connection_class(parsed.hostname, parsed.port, timeout=120)
    try:
        connection.putrequest('POST', request_path)
        connection.putheader(
            'Content-Type', f'multipart/form-data; boundary={_BOUNDARY.decode("ascii")}')
        connection.putheader('Content-Length', str(content_length))
        connection.putheader('User-Agent', 'FTHR-Clips/Desktop-Uploader')
        for name, value in headers.items():
            connection.putheader(name, value)
        connection.endheaders()
        for part in parts:
            connection.send(part)
        connection.send(file_header)
        with file_path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                connection.send(chunk)
        connection.send(footer)
        response = connection.getresponse()
        return int(response.status), response.read()
    finally:
        connection.close()


def _custom_server_url(settings: dict[str, Any]) -> str:
    value = str(settings.get('upload_server_url', '') or '').strip()
    if value and not value.lower().startswith(('http://', 'https://')):
        value = f'https://{value}'
    if not value:
        raise ValueError('Set a server URL before connecting to your server.')
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme.lower() not in {'http', 'https'} or not parsed.hostname:
        raise ValueError('Your server URL must use http:// or https://.')
    return value


def _custom_server_headers(settings: dict[str, Any]) -> dict[str, str]:
    auth = str(settings.get('upload_auth_header', '') or '').strip()
    if any(character in auth for character in '\r\n'):
        raise ValueError('The authorization header contains an invalid line break.')
    return {'Authorization': auth} if auth else {}


def _discord_webhook_url(settings: dict[str, Any]) -> str:
    url = str(settings.get('discord_active_webhook', '') or settings.get('discord_webhook_url', '') or '').strip()
    if not url:
        webhooks = settings.get('discord_webhooks', [])
        if isinstance(webhooks, list) and webhooks:
            first = webhooks[0]
            if isinstance(first, dict):
                url = str(first.get('url', '')).strip()
            elif isinstance(first, str):
                url = first.strip()
    if not url:
        raise ValueError('Set a Discord Webhook URL before uploading.')
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != 'https' or not parsed.hostname:
        raise ValueError('Discord Webhook URL must use https://.')
    return url
