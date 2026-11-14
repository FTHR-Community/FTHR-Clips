from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_Uploader'))

import upload_runtime
import uploader_service


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




def test_provider_actions_require_versioned_consent():
    request = {'config': {'upload_provider': 'catbox'}}
    try:
        uploader_service._require_provider_consent('catbox', {}, request)
    except PermissionError as exc:
        assert 'Catbox' in str(exc)
    else:
        raise AssertionError('Catbox action accepted missing legal consent')



def test_custom_provider_does_not_require_third_party_consent():
    uploader_service._require_provider_consent('custom', {}, {})


def test_retired_providers_and_account_actions_are_rejected(tmp_path, monkeypatch):
    import pytest
    calls = []
    monkeypatch.setattr(upload_runtime, '_multipart_post', lambda *a, **k: calls.append(a))
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'clip')
    for provider in ('lustful', 'fthr'):
        with pytest.raises(ValueError, match='Unsupported upload provider'):
            upload_runtime.UploadRuntime({'upload_provider': provider}).upload(str(clip))
        with pytest.raises(ValueError, match='Unsupported upload provider'):
            uploader_service._perform({'action': 'test_connection'}, {'upload_provider': provider})
    for action in ('register', 'login', 'account_status', 'logout'):
        with pytest.raises(ValueError, match='Unsupported uploader action'):
            uploader_service._perform({'action': action}, {'upload_provider': 'custom'})
    assert calls == []
