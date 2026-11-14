"""Website-hosted Windows updates. Downloading never launches an installer."""

from dataclasses import dataclass
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib import error, parse, request


DEFAULT_MANIFEST_URL = 'https://downloads.fthrclips.com/updates/windows-x64.json'
MAX_MANIFEST_BYTES = 64 * 1024
MAX_INSTALLER_BYTES = 2 * 1024**3
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 30 * 60
MAX_NOTES_LENGTH = 8000
_VERSION = re.compile(
    r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z'
)


class UpdateError(Exception):
    """The update could not be safely checked or downloaded."""


class UpdateCancelled(UpdateError):
    """The caller cancelled a download."""


@dataclass(frozen=True)
class UpdateRelease:
    version: str
    url: str
    sha256: str
    size: int
    notes: str = ''


def _version_parts(value):
    if not isinstance(value, str) or len(value) > 128:
        raise UpdateError('Invalid release version.')
    match = _VERSION.fullmatch(value)
    if not match:
        raise UpdateError('Versions must use semantic versioning (for example 2.1.0-alpha.1).')
    prerelease = match[4]
    identifiers = prerelease.split('.') if prerelease else []
    if any(p.isdigit() and len(p) > 1 and p.startswith('0') for p in identifiers):
        raise UpdateError('Numeric prerelease identifiers cannot have leading zeros.')
    # Stable versions sort after prereleases. Numeric identifiers sort before text.
    return (tuple(int(match[i]) for i in (1, 2, 3)), not bool(prerelease),
            tuple((0, int(p)) if p.isdigit() else (1, p) for p in identifiers))


def _https_url(url, *, installer=False):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 33 for c in url):
        raise UpdateError('Invalid update URL.')
    try:
        parts = parse.urlsplit(url)
        valid = (parts.scheme == 'https' and parts.hostname and not parts.username
                 and not parts.password and not parts.fragment)
        _ = parts.port  # Validate malformed ports as well.
    except ValueError as exc:
        raise UpdateError('Invalid update URL.') from exc
    if not valid:
        raise UpdateError('Updates require HTTPS URLs without credentials or fragments.')
    if installer and not parse.unquote(parts.path).lower().endswith('.exe'):
        raise UpdateError('Windows updates must link to an .exe installer.')
    return url


class _HTTPSRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    _https_url(url)
    opener = request.build_opener(_HTTPSRedirectHandler())
    response = opener.open(request.Request(url, headers={
        'User-Agent': 'FTHR-Clips-Updater/1', 'Cache-Control': 'no-cache',
        'Accept-Encoding': 'identity',
    }), timeout=REQUEST_TIMEOUT)
    try:
        _https_url(response.geturl())
        if response.status != 200:
            raise UpdateError('The update server did not return a complete response.')
    except Exception:
        response.close()
        raise
    return response


def _content_length(response, maximum):
    value = response.headers.get('Content-Length')
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise UpdateError('The update server returned an invalid content length.') from exc
    if size < 0 or size > maximum:
        raise UpdateError('The update response exceeds the allowed size.')
    return size


def _parse_manifest(manifest):
    if not isinstance(manifest, dict):
        raise UpdateError('The update manifest must be a JSON object.')
    if type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1:
        raise UpdateError('Unsupported update manifest schema.')
    if manifest.get('platform') != 'windows-x64':
        raise UpdateError('This update is not for Windows x64.')
    try:
        release = UpdateRelease(manifest['version'], manifest['url'], manifest['sha256'],
                                manifest['size'], manifest.get('notes', ''))
    except KeyError as exc:
        raise UpdateError('The update manifest is missing a required field.') from exc
    _validate_release(release)
    return release


def _validate_release(release):
    _version_parts(release.version)
    _https_url(release.url, installer=True)
    if not isinstance(release.sha256, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', release.sha256):
        raise UpdateError('The update SHA-256 is invalid.')
    if type(release.size) is not int or not 0 < release.size <= MAX_INSTALLER_BYTES:
        raise UpdateError('The installer size is invalid or exceeds the allowed size.')
    if not isinstance(release.notes, str) or len(release.notes) > MAX_NOTES_LENGTH:
        raise UpdateError('The update notes are invalid or too long.')


def check_for_update(manifest_url, current_version):
    """Return a strictly newer release, or None; raise UpdateError on failure."""
    current = _version_parts(current_version)
    try:
        with _open(manifest_url) as response:
            length = _content_length(response, MAX_MANIFEST_BYTES)
            data = response.read(MAX_MANIFEST_BYTES + 1)
        if len(data) > MAX_MANIFEST_BYTES:
            raise UpdateError('The update manifest exceeds the allowed size.')
        if length is not None and len(data) != length:
            raise UpdateError('The update manifest download is incomplete.')
        release = _parse_manifest(json.loads(data.decode('utf-8')))
        return release if _version_parts(release.version) > current else None
    except (OSError, error.URLError, HTTPException, ValueError, UnicodeError, RecursionError) as exc:
        raise UpdateError(f'Could not check for updates: {exc}') from exc


def download_update(release, destination_dir, progress=None, cancel_event=None):
    """Download and verify a release atomically; progress receives (bytes, total).

    Cancellation is checked between network reads, each bounded by REQUEST_TIMEOUT.
    A unique partial file is removed on any failure, and no installer is executed.
    """
    _validate_release(release)
    partial = None

    def check_cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise UpdateCancelled('Update download cancelled.')

    try:
        check_cancelled()
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        # Never use a remote filename as a local path. SemVer is validated above.
        final = destination / f'FTHR-Clips-{release.version}-windows-x64.exe'
        started = time.monotonic()
        with _open(release.url) as response:
            _https_url(response.geturl(), installer=True)
            length = _content_length(response, release.size)
            if length is not None and length != release.size:
                raise UpdateError('The installer content length does not match the manifest.')
            digest = hashlib.sha256()
            downloaded = 0
            with tempfile.NamedTemporaryFile(dir=destination, prefix='.fthr-update-',
                                             suffix='.part', delete=False) as output:
                partial = Path(output.name)
                if progress:
                    progress(0, release.size)
                while True:
                    check_cancelled()
                    if time.monotonic() - started > DOWNLOAD_TIMEOUT:
                        raise UpdateError('The update download timed out.')
                    chunk = response.read(min(256 * 1024, release.size - downloaded + 1))
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > release.size:
                        raise UpdateError('The installer is larger than the manifest declares.')
                    digest.update(chunk)
                    output.write(chunk)
                    if progress:
                        progress(downloaded, release.size)
                if downloaded != release.size:
                    raise UpdateError('The installer download is incomplete.')
                if digest.hexdigest() != release.sha256.lower():
                    raise UpdateError('The installer SHA-256 does not match the manifest.')
                output.flush()
                os.fsync(output.fileno())
        check_cancelled()
        os.replace(partial, final)
        partial = None
        return final
    except (OSError, error.URLError, HTTPException, ValueError) as exc:
        raise UpdateError(f'Could not download the update: {exc}') from exc
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)
