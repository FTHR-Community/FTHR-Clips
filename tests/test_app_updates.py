import hashlib
from http.client import IncompleteRead
import io
import json
from threading import Event
from urllib.request import Request

import pytest

from core import app_updates as updates
from generate_update_manifest import main as generate_manifest


PAYLOAD = b'example installer bytes'
URL = 'https://downloads.example.test/FTHR-2.1.0-windows-x64.exe'


def manifest(version='2.1.0', **changes):
    value = dict(schema_version=1, platform='windows-x64', version=version, url=URL,
                 sha256=hashlib.sha256(PAYLOAD).hexdigest(), size=len(PAYLOAD))
    value.update(changes)
    return value


class Response(io.BytesIO):
    status = 200

    def __init__(self, data, headers=None, url=URL):
        super().__init__(data)
        self.headers = headers or {}
        self.url = url

    def geturl(self):
        return self.url


def serve(monkeypatch, data, **kwargs):
    monkeypatch.setattr(updates, '_open', lambda url: Response(data, **kwargs))


@pytest.mark.parametrize(('current', 'candidate', 'newer'), [
    ('2.1.0-alpha', '2.1.0-alpha.1', True),
    ('2.1.0-alpha.9', '2.1.0-alpha.10', True),
    ('2.1.0-alpha', '2.1.0-beta', True),
    ('2.1.0-beta', '2.1.0-rc.1', True),
    ('2.1.0-rc.1', '2.1.0', True),
    ('2.1.0', '2.1.0-rc.2', False),
    ('2.1.0-alpha', '2.0.9', False),
    ('2.1.0-alpha', '2.1.0-alpha', False),
    ('2.1.0+old', '2.1.0+new', False),
])
def test_version_order(monkeypatch, current, candidate, newer):
    serve(monkeypatch, json.dumps(manifest(candidate)).encode())
    assert bool(updates.check_for_update('https://example.test/updates.json', current)) == newer


@pytest.mark.parametrize('changes', [
    {'version': '2.1'}, {'version': '02.1.0'}, {'version': '2.1.0-alpha.01'},
    {'version': '2.1.0-alpha..1'}, {'size': True}, {'size': 0},
    {'size': updates.MAX_INSTALLER_BYTES + 1}, {'sha256': 'invalid'},
    {'url': 'http://example.test/update.exe'}, {'url': 'https://example.test/update.zip'},
    # Deliberately add dummy userinfo to test rejection without a secret-like literal URL.
    {'url': URL.replace('downloads.example.test', 'user:pass@example.test')},
    {'platform': 'linux-x64'}, {'schema_version': True},
    {'notes': 'x' * (updates.MAX_NOTES_LENGTH + 1)},
])
def test_invalid_manifest(monkeypatch, changes):
    serve(monkeypatch, json.dumps(manifest(**changes)).encode())
    with pytest.raises(updates.UpdateError):
        updates.check_for_update('https://example.test/updates.json', '2.1.0-alpha')


@pytest.mark.parametrize('data', [b'not JSON', b'\xff', b'[]', b'x' * 65537],
                         ids=['not-json', 'not-utf8', 'array', 'too-large'])
def test_invalid_manifest_body(monkeypatch, data):
    serve(monkeypatch, data)
    with pytest.raises(updates.UpdateError):
        updates.check_for_update('https://example.test/updates.json', '2.1.0-alpha')


def test_manifest_declared_too_large(monkeypatch):
    serve(monkeypatch, b'{}', headers={'Content-Length': '65537'})
    with pytest.raises(updates.UpdateError):
        updates.check_for_update('https://example.test/updates.json', '2.1.0-alpha')


def test_short_manifest_rejected_even_when_json_complete(monkeypatch):
    data = json.dumps(manifest()).encode()
    serve(monkeypatch, data, headers={'Content-Length': str(len(data) + 1)})
    with pytest.raises(updates.UpdateError, match='incomplete'):
        updates.check_for_update('https://example.test/updates.json', '2.1.0-alpha')


def test_redirect_policy_and_timeout(monkeypatch):
    handler = updates._HTTPSRedirectHandler()
    req = Request(URL)
    with pytest.raises(updates.UpdateError, match='HTTPS'):
        handler.redirect_request(req, None, 302, 'Found', {}, 'http://example.test/a.exe')
    redirected = handler.redirect_request(req, None, 302, 'Found', {},
                                          'https://cdn.example.test/a.exe')
    assert redirected.full_url == 'https://cdn.example.test/a.exe'
    calls = []

    class Opener:
        def open(self, req, timeout):
            calls.append((req.full_url, timeout))
            return Response(PAYLOAD, url='http://example.test/a.exe')

    monkeypatch.setattr(updates.request, 'build_opener', lambda handler: Opener())
    with pytest.raises(updates.UpdateError):
        updates._open(URL)
    assert calls == [(URL, updates.REQUEST_TIMEOUT)]


def test_download_verified_and_atomic(monkeypatch, tmp_path):
    serve(monkeypatch, PAYLOAD, headers={'Content-Length': str(len(PAYLOAD))})
    progress = []
    release = updates._parse_manifest(manifest())
    output = updates.download_update(release, tmp_path, lambda done, total: progress.append((done, total)))
    assert output.read_bytes() == PAYLOAD
    assert list(tmp_path.iterdir()) == [output]
    assert progress == [(0, len(PAYLOAD)), (len(PAYLOAD), len(PAYLOAD))]


@pytest.mark.parametrize(('body', 'headers'), [
    (b'corrupt installer bytes', {}), (PAYLOAD[:-1], {}), (PAYLOAD + b'extra', {}),
    (PAYLOAD, {'Content-Length': '1'}), (PAYLOAD, {'Content-Length': 'invalid'}),
])
def test_failed_download_cleans_partial_preserves_previous(monkeypatch, tmp_path, body, headers):
    serve(monkeypatch, body, headers=headers)
    final = tmp_path / 'FTHR-Clips-2.1.0-windows-x64.exe'
    final.write_bytes(b'previous good file')
    with pytest.raises(updates.UpdateError):
        updates.download_update(updates._parse_manifest(manifest()), tmp_path)
    assert final.read_bytes() == b'previous good file'
    assert list(tmp_path.iterdir()) == [final]


def test_cancel_during_download_removes_partial(monkeypatch, tmp_path):
    serve(monkeypatch, PAYLOAD)
    event = Event()

    def progress(done, total):
        if done:
            event.set()

    with pytest.raises(updates.UpdateCancelled):
        updates.download_update(updates._parse_manifest(manifest()), tmp_path, progress, event)
    assert not list(tmp_path.iterdir())


def test_cancel_before_download_never_opens_network(monkeypatch, tmp_path):
    event = Event()
    event.set()
    monkeypatch.setattr(updates, '_open', lambda url: pytest.fail('network used'))
    with pytest.raises(updates.UpdateCancelled):
        updates.download_update(updates._parse_manifest(manifest()), tmp_path, cancel_event=event)
    assert not list(tmp_path.iterdir())


def test_network_failure_wrapped(monkeypatch, tmp_path):
    def fail(url):
        raise TimeoutError('test timeout')
    monkeypatch.setattr(updates, '_open', fail)
    with pytest.raises(updates.UpdateError, match='timeout'):
        updates.check_for_update(URL, '2.1.0-alpha')
    with pytest.raises(updates.UpdateError, match='timeout'):
        updates.download_update(updates._parse_manifest(manifest()), tmp_path)
    assert not list(tmp_path.iterdir())


def test_incomplete_http_response_is_wrapped_and_cleaned(monkeypatch, tmp_path):
    class TruncatedResponse(Response):
        def read(self, size=-1):
            raise IncompleteRead(b'partial', 20)

    monkeypatch.setattr(updates, '_open', lambda url: TruncatedResponse(b''))
    with pytest.raises(updates.UpdateError):
        updates.check_for_update(URL, '2.1.0-alpha')
    with pytest.raises(updates.UpdateError):
        updates.download_update(updates._parse_manifest(manifest()), tmp_path)
    assert not list(tmp_path.iterdir())


def test_generator_hashes_actual_file(tmp_path):
    installer = tmp_path / 'installer.exe'
    installer.write_bytes(PAYLOAD)
    output = tmp_path / 'windows-x64.json'
    assert generate_manifest([str(installer), '--version', '2.1.0', '--url', URL,
                              '--output', str(output), '--notes', 'A new release']) == 0
    result = json.loads(output.read_text())
    assert result == manifest(notes='A new release')


def test_generator_rejects_bad_release_without_output(tmp_path):
    installer = tmp_path / 'installer.exe'
    installer.write_bytes(PAYLOAD)
    output = tmp_path / 'windows-x64.json'
    with pytest.raises(SystemExit):
        generate_manifest([str(installer), '--version', 'bad', '--url', URL,
                           '--output', str(output)])
    assert not output.exists()
