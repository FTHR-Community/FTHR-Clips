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

from upload_runtime import UploadRuntime


PLUGIN_ID = 'com.fthrclips.uploader'
PLUGIN_VERSION = '1.4.0'
TERMS_VERSION = '2026-10-15-v3'
PRIVACY_VERSION = '2026-10-15-v3'
CATBOX_LEGAL_VERSION = 'catbox-legal-2021-03-06'
_NETWORK_ACTIONS = {'test_connection', 'upload'}
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
    if provider in {'own_server', 'your_server'}:
        return _CUSTOM_PROVIDER
    return provider


def _require_provider_consent(
        provider: str,
        settings: dict[str, Any],
        request: dict[str, Any]) -> None:
    if provider == 'catbox':
        if settings.get('catbox_legal_accepted_version') != CATBOX_LEGAL_VERSION:
            raise PermissionError('Accept Catbox’s legal policies in FTHR Clips first.')
        return
    if provider in {_CUSTOM_PROVIDER, 'sftp'}:
        return
    raise ValueError('Unsupported upload provider.')


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






def _perform(request: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    action = str(request.get('action', '')).strip()
    if action in {'sftp_save', 'sftp_forget'}:
        from sftp_provider import configuration, forget_password, save_password
        config = dict(request.get('config') or {})
        configuration(config)
        if action == 'sftp_forget':
            forget_password(config)
        elif config.get('sftp_password'):
            save_password(config, config['sftp_password'])
        return {'ok': True, 'message': 'SFTP credentials updated.'}
    provider = _provider(settings, request)
    _require_provider_consent(provider, settings, request)
    runtime_settings = dict(settings)
    if isinstance(request.get('config'), dict):
        runtime_settings.update(request['config'])
    runtime = UploadRuntime(runtime_settings)
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
