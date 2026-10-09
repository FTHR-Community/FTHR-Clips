"""One-shot process boundary for the optional FTHR Upload Extension."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any

from fthr_upload_api import (
    DEFAULT_BASE_URL,
    FthrUploadClient,
    clear_credentials,
    load_credentials,
    save_credentials,
)
from hardware_identity_client import derive_hardware_id
from upload_runtime import UploadRuntime


PLUGIN_ID = 'com.fthrclips.uploader'
PLUGIN_VERSION = '1.1.0'
TERMS_VERSION = '2026-08-24-v1'
PRIVACY_VERSION = '2026-08-24-v1'
CATBOX_LEGAL_VERSION = 'catbox-legal-2021-03-06'
LUSTFUL_LEGAL_VERSION = 'lustful-legal-2026-07-27'
HARDWARE_POLICY_VERSION = 'lustful-2026-07-27-hwid-v1'
_NETWORK_ACTIONS = {'register', 'login', 'test_connection', 'upload'}
_CUSTOM_PROVIDER = 'custom'


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Invalid JSON object: {path}')
    return value


def _validate_activation(
        receipt_path: Path,
        request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not receipt_path.is_file():
        raise PermissionError('The Upload Extension has not been installed in FTHR Clips.')
    receipt = _read_object(receipt_path)
    if receipt.get('plugin_id') != PLUGIN_ID:
        raise PermissionError('Uploader activation belongs to a different package.')
    if receipt.get('plugin_version') != PLUGIN_VERSION:
        raise PermissionError('Uploader activation version does not match.')
    if receipt.get('terms_version') != TERMS_VERSION:
        raise PermissionError('Uploader Terms of Service must be accepted again.')
    if receipt.get('privacy_version') != PRIVACY_VERSION:
        raise PermissionError('Uploader Privacy Policy must be accepted again.')
    if request.get('activation_id') != receipt.get('activation_id'):
        raise PermissionError('Uploader activation token is invalid.')
    expected = str(receipt.get('executable_sha256', '')).lower()
    if not expected or _sha256(Path(sys.executable)).lower() != expected:
        raise PermissionError('Uploader executable integrity check failed.')
    settings_path = Path(str(receipt.get('settings_file', '')))
    settings = _read_object(settings_path) if settings_path.is_file() else {}
    if (str(request.get('action', '')) in _NETWORK_ACTIONS
            and not bool(settings.get('upload_enabled', False))):
        raise PermissionError('The Upload Extension is disabled in FTHR Clips.')
    return receipt, settings


def _provider(settings: dict[str, Any], request: dict[str, Any]) -> str:
    config = request.get('config') if isinstance(request.get('config'), dict) else {}
    provider = str(config.get(
        'upload_provider', settings.get('upload_provider', 'catbox'))).lower()
    if provider == 'fthr':
        return 'lustful'
    if provider in {'own_server', 'your_server'}:
        return _CUSTOM_PROVIDER
    if provider in {'discord', 'discord_webhook'}:
        return 'discord_webhook'
    return provider


def _require_provider_consent(
        provider: str,
        settings: dict[str, Any],
        request: dict[str, Any]) -> None:
    if provider == 'catbox':
        if settings.get('catbox_legal_accepted_version') != CATBOX_LEGAL_VERSION:
            raise PermissionError('Accept Catbox’s legal policies in FTHR Clips first.')
        return
    if provider in {_CUSTOM_PROVIDER, 'discord_webhook'}:
        return
    if provider != 'lustful':
        raise ValueError('Only Catbox, Lustful, Discord Webhook, and your server are supported.')
    if settings.get('lustful_legal_accepted_version') != LUSTFUL_LEGAL_VERSION:
        raise PermissionError('Accept Lustful’s Terms and Privacy Policy first.')
    if settings.get('lustful_hardware_policy_accepted_version') != HARDWARE_POLICY_VERSION:
        raise PermissionError('Install Hardware Identity after accepting Lustful’s policies.')
    if not isinstance(request.get('hardware_identity'), dict):
        raise PermissionError('The separate Hardware Identity capability is unavailable.')


def _validated_clip_path(request: dict[str, Any]) -> str:
    target = Path(str(request.get('path', ''))).expanduser().resolve(strict=True)
    if not target.is_file():
        raise ValueError('The selected clip does not exist.')
    roots = [
        Path(str(value)).expanduser().resolve(strict=False)
        for value in request.get('allowed_roots', [])
    ]
    if not roots or not any(target == root or root in target.parents for root in roots):
        raise PermissionError('The uploader may only read approved clip folders.')
    return str(target)


def _hardware_provider(request: dict[str, Any]):
    config = request.get('hardware_identity')
    if not isinstance(config, dict):
        return None
    return lambda: derive_hardware_id(config)


def _account_action(
        mode: str,
        account_id: str,
        settings: dict[str, Any],
        request: dict[str, Any]) -> dict[str, Any]:
    account_id = account_id.strip()
    if not account_id:
        raise ValueError('Account ID is required.')
    hardware_id = derive_hardware_id(dict(request['hardware_identity']))
    client = FthrUploadClient(
        api_key=str(settings.get('lustful_api_key', '')),
        base_url=DEFAULT_BASE_URL,
    )
    if mode == 'login':
        data = client.verify(account_id, hardware_id)
    elif mode == 'register':
        # Keep registration conflicts as errors. Verifying identity here would
        # turn account creation into an implicit login attempt.
        data = client.register(account_id, hardware_id)
    else:
        raise ValueError(f'Unsupported account action: {mode}')
    role = str(data.get('role', 'user'))
    save_credentials(account_id, hardware_id, role)
    return {
        **data,
        'account_id': account_id,
        'role': role,
        'mode': mode,
    }


def _perform(request: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get('action', '')).strip()
    if action == 'account_status':
        return {'ok': True, 'credentials': load_credentials()}
    if action == 'logout':
        clear_credentials()
        return {'ok': True, 'message': 'Logged out locally.'}
    provider = 'lustful' if action in {'register', 'login'} else _provider(settings, request)
    _require_provider_consent(provider, settings, request)
    if action in {'register', 'login'}:
        data = _account_action(
            action, str(request.get('account_id', '')), settings, request)
        return {'ok': True, 'data': data}
    runtime_settings = dict(settings)
    if isinstance(request.get('config'), dict):
        runtime_settings.update(request['config'])
    runtime = UploadRuntime(runtime_settings, _hardware_provider(request))
    if action == 'test_connection':
        ok, message = runtime.test_connection()
        return {'ok': ok, 'message': message}
    if action == 'upload':
        success, message, info = runtime.upload(_validated_clip_path(request))
        return {'ok': success, 'message': message, 'upload_info': info}
    raise ValueError(f'Unsupported uploader action: {action or "(missing)"}')


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--activation-receipt', required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict):
            raise ValueError('Uploader request must be a JSON object.')
        _, settings = _validate_activation(
            Path(args.activation_receipt).expanduser().resolve(strict=False), request)
        with contextlib.redirect_stdout(sys.stderr or io.StringIO()):
            response = _perform(request, settings)
    except Exception as exc:
        response = {'ok': False, 'message': str(exc)}
    sys.stdout.write(json.dumps(response, separators=(',', ':')) + '\n')
    sys.stdout.flush()
    return 0 if response.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
