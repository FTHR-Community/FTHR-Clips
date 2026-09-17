from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_Uploader'))

import upload_runtime
import uploader_service
from fthr_upload_api import FthrApiError


def test_catbox_uses_documented_multipart_fields(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip-data')
    captured = {}

    def multipart(url, fields, file_field, file_path, headers):
        captured.update({
            'url': url,
            'fields': fields,
            'file_field': file_field,
            'file_path': file_path,
            'headers': headers,
        })
        return 200, b'https://files.catbox.moe/example.mp4'

    monkeypatch.setattr(upload_runtime, '_multipart_post', multipart)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    runtime = upload_runtime.UploadRuntime({
        'upload_provider': 'catbox',
        'catbox_userhash': 'test-userhash',
        'upload_auto_delete': False,
    })
    ok, message, info = runtime.upload(str(clip))
    assert ok, message
    assert captured['url'] == 'https://catbox.moe/user/api.php'
    assert captured['fields'] == {
        'reqtype': 'fileupload',
        'userhash': 'test-userhash',
    }
    assert captured['file_field'] == 'fileToUpload'
    assert captured['file_path'] == clip
    assert info['url'] == 'https://files.catbox.moe/example.mp4'


def test_lustful_uses_documented_headers_and_file_field(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip-data')
    captured = {}

    class Client:
        def verify(self, account_id, hardware_id):
            assert account_id == '0123456789abcdef'
            assert hardware_id == 'derived-hardware-id'
            return {'ok': True, 'role': 'user'}

    def multipart(url, fields, file_field, file_path, headers):
        captured.update({
            'url': url,
            'fields': fields,
            'file_field': file_field,
            'file_path': file_path,
            'headers': headers,
        })
        return 200, json.dumps({
            'ok': True,
            'id': 7,
            'url': 'https://fthr.lustful.wtf/files/example',
            'raw_url': 'https://fthr.lustful.wtf/raw/example',
            'expires_at': '2026-08-31 12:00:00',
        }).encode()

    monkeypatch.setattr(upload_runtime, '_multipart_post', multipart)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    monkeypatch.setattr(upload_runtime, 'load_credentials', lambda: {
        'account_id': '0123456789abcdef',
        'hwid': 'derived-hardware-id',
    })
    monkeypatch.setattr(upload_runtime, 'resolve_api_key', lambda _value='': 'app-key')
    runtime = upload_runtime.UploadRuntime(
        {'upload_provider': 'lustful', 'upload_auto_delete': False},
        hardware_id_provider=lambda: 'derived-hardware-id',
    )
    monkeypatch.setattr(runtime, '_lustful_client', lambda: Client())
    ok, message, info = runtime.upload(str(clip))
    assert ok, message
    assert captured['url'] == 'https://fthr.lustful.wtf/api/upload'
    assert captured['fields'] == {}
    assert captured['file_field'] == 'file'
    assert captured['headers']['Authorization'] == 'Bearer app-key'
    assert captured['headers']['X-Account-Id'] == '0123456789abcdef'
    assert info['file_id'] == 7


def test_custom_server_uses_configured_url_auth_and_clip_field(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip-data')
    captured = {}

    def multipart(url, fields, file_field, file_path, headers, **kwargs):
        captured.update({
            'url': url,
            'fields': fields,
            'file_field': file_field,
            'file_path': file_path,
            'headers': headers,
            'kwargs': kwargs,
        })
        return 201, b'{"url":"https://clips.example/123"}'

    monkeypatch.setattr(upload_runtime, '_multipart_post', multipart)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    runtime = upload_runtime.UploadRuntime({
        'upload_provider': 'custom',
        'upload_server_url': 'http://localhost:8080/upload',
        'upload_auth_header': 'Bearer test-token',
        'upload_auto_delete': False,
    })

    ok, message, info = runtime.upload(str(clip))

    assert ok, message
    assert captured['url'] == 'http://localhost:8080/upload'
    assert captured['fields'] == {}
    assert captured['file_field'] == 'clip'
    assert captured['file_path'] == clip
    assert captured['headers'] == {'Authorization': 'Bearer test-token'}
    assert captured['kwargs'] == {'require_https': False}
    assert info['url'] == 'https://clips.example/123'


def test_register_does_not_fall_back_to_verify_on_conflict(monkeypatch):
    calls: list[str] = []

    class Client:
        def register(self, account_id, hardware_id):
            calls.append('register')
            raise FthrApiError('registration conflict', 409)

        def verify(self, account_id, hardware_id):
            calls.append('verify')
            raise AssertionError('registration must not become a login attempt')

    monkeypatch.setattr(uploader_service, 'FthrUploadClient', lambda **_kwargs: Client())
    monkeypatch.setattr(
        uploader_service, 'derive_hardware_id', lambda _config: 'derived-hardware-id')
    monkeypatch.setattr(uploader_service, 'save_credentials', lambda *_args, **_kwargs: {})

    try:
        uploader_service._account_action(
            'register', 'new-account-id', {}, {'hardware_identity': {}})
    except FthrApiError as exc:
        assert str(exc) == 'registration conflict'
    else:
        raise AssertionError('registration conflict should be returned to the caller')
    assert calls == ['register']


def test_provider_actions_require_versioned_consent():
    request = {'config': {'upload_provider': 'catbox'}}
    try:
        uploader_service._require_provider_consent('catbox', {}, request)
    except PermissionError as exc:
        assert 'Catbox' in str(exc)
    else:
        raise AssertionError('Catbox action accepted missing legal consent')

    settings = {
        'lustful_legal_accepted_version': uploader_service.LUSTFUL_LEGAL_VERSION,
        'lustful_hardware_policy_accepted_version': uploader_service.HARDWARE_POLICY_VERSION,
    }
    try:
        uploader_service._require_provider_consent('lustful', settings, {})
    except PermissionError as exc:
        assert 'Hardware Identity' in str(exc)
    else:
        raise AssertionError('Lustful action accepted a missing hardware capability')


def test_custom_server_does_not_require_third_party_consent():
    uploader_service._require_provider_consent('custom', {}, {})


def test_permanent_http_upload_failure_is_not_retried(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip-data')
    attempts = 0

    def multipart(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        return 413, b'file too large'

    monkeypatch.setattr(upload_runtime, '_multipart_post', multipart)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    runtime = upload_runtime.UploadRuntime({'upload_provider': 'catbox'})

    ok, message, _info = runtime.upload(str(clip))

    assert not ok
    assert 'file too large' in message
    assert attempts == 1


def test_server_error_upload_is_retried(tmp_path, monkeypatch):
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip-data')
    attempts = 0

    def multipart(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return 503, b'temporarily unavailable'
        return 200, b'https://files.catbox.moe/example.mp4'

    monkeypatch.setattr(upload_runtime, '_multipart_post', multipart)
    monkeypatch.setattr(upload_runtime, 'HISTORY_FILE', tmp_path / 'history.json')
    monkeypatch.setattr(upload_runtime.time, 'sleep', lambda _delay: None)
    runtime = upload_runtime.UploadRuntime({'upload_provider': 'catbox'})

    ok, message, _info = runtime.upload(str(clip))

    assert ok, message
    assert attempts == 2
