"""Audio-source sidecars mapping clip-local UUIDs to media streams.

Manifests exclude process paths, window titles, command lines, URLs, usernames,
and live-device handles. Container stream titles are the fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID


MANIFEST_VERSION = 1
MANIFEST_SUFFIX = '.fthr-audio.json'
# Default Mix + Microphone + the approved eight application stems can produce
# ten real MP4 audio streams. The reader must accept the same contract as the
# native muxer; otherwise a valid rich clip would silently fall back to wrong
# generic playback labels.
MAX_SOURCES = 10
_SAFE_TEXT = re.compile(r"^[A-Za-z0-9 ._+()'\-]{1,128}$")
_SOURCE_TYPES = frozenset({'application', 'microphone', 'system'})


class AudioManifestError(ValueError):
    """Raised when an audio manifest cannot safely describe its media."""


@dataclass(frozen=True)
class AudioSourceManifestEntry:
    source_uuid: str
    stream_index: int
    source_type: str
    display_name: str
    persistent_identity: str
    icon_reference: str | None
    first_active_100ns: int
    last_active_100ns: int
    sample_rate: int
    channels: int
    sample_format: str


def manifest_path_for(media_path: str | Path) -> Path:
    """Return the adjacent sidecar without changing the media suffix."""
    media = Path(media_path)
    return media.with_name(media.name + MANIFEST_SUFFIX)


def media_sha256(media_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(media_path).open('rb') as media:
        for block in iter(lambda: media.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build_manifest(*, media_path: str | Path, transaction_id: str,
                   sources: list[AudioSourceManifestEntry]) -> dict[str, Any]:
    """Create and validate a manifest bound to the exact completed media file."""
    manifest = {
        'manifest_version': MANIFEST_VERSION,
        'transaction_id': _validate_uuid(transaction_id, 'transaction_id'),
        'media_file': Path(media_path).name,
        'media_sha256': media_sha256(media_path),
        'sources': [asdict(source) for source in sources],
    }
    validate_manifest(manifest, media_path=media_path)
    return manifest


def validate_manifest(manifest: dict[str, Any], *, media_path: str | Path | None = None) -> None:
    """Validate data and, when supplied, bind it to a specific media file."""
    if not isinstance(manifest, dict):
        raise AudioManifestError('manifest must be an object')
    if manifest.get('manifest_version') != MANIFEST_VERSION:
        raise AudioManifestError('unsupported manifest version')
    _validate_uuid(manifest.get('transaction_id'), 'transaction_id')
    media_file = manifest.get('media_file')
    if not isinstance(media_file, str) or Path(media_file).name != media_file:
        raise AudioManifestError('media_file must be a basename')
    media_hash = manifest.get('media_sha256')
    if not isinstance(media_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', media_hash):
        raise AudioManifestError('media_sha256 must be a SHA-256 digest')
    sources = manifest.get('sources')
    if not isinstance(sources, list) or not sources or len(sources) > MAX_SOURCES:
        raise AudioManifestError('sources must contain between one and ten entries')

    seen_uuid: set[str] = set()
    seen_stream: set[int] = set()
    for source in sources:
        _validate_source(source, seen_uuid, seen_stream)

    if media_path is not None:
        media = Path(media_path)
        if media.name != media_file:
            raise AudioManifestError('manifest media name does not match clip')
        if media_sha256(media) != media_hash:
            raise AudioManifestError('manifest media hash does not match clip')


def write_manifest_atomic(manifest: dict[str, Any], media_path: str | Path) -> Path:
    """Publish a validated sidecar with an atomic same-directory replacement."""
    validate_manifest(manifest, media_path=media_path)
    destination = manifest_path_for(media_path)
    temporary = destination.with_name(destination.name + '.partial')
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=True, sort_keys=True, separators=(',', ':')),
            encoding='utf-8')
        temporary.replace(destination)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A failed secondary cleanup cannot make a manifest appear valid;
            # keep the original publication error as the actionable failure.
            pass
        raise AudioManifestError(f'could not publish manifest: {error}') from error
    return destination


def rebind_manifest_after_media_replace(media_path: str | Path) -> bool:
    """Update the media hash after a rewrite that preserves every audio stream.

    Call only after copying all original audio streams in their original order.
    The old sidecar is validated, rebound, and published atomically. Returns
    False for missing or invalid sidecars, including legacy/imported clips.
    """

    media = Path(media_path)
    sidecar = manifest_path_for(media)
    if not sidecar.is_file() or not media.is_file():
        return False
    try:
        manifest = json.loads(sidecar.read_text(encoding='utf-8'))
        validate_manifest(manifest)
        if manifest.get('media_file') != media.name:
            return False
        manifest['media_sha256'] = media_sha256(media)
        write_manifest_atomic(manifest, media)
        return True
    except (OSError, json.JSONDecodeError, AudioManifestError):
        # Corrupted or inaccessible manifest cannot be refreshed; return False.
        return False


def read_manifest_for_media(media_path: str | Path) -> dict[str, Any] | None:
    """Return a validated FTHR manifest or ``None`` for legacy/imported clips.

    A stale/corrupt sidecar is intentionally treated as absent: the caller must
    fall back to container metadata rather than inventing source semantics.
    """
    path = manifest_path_for(media_path)
    if not path.is_file():
        return None
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
        validate_manifest(manifest, media_path=media_path)
        return manifest
    except (OSError, json.JSONDecodeError, AudioManifestError):
        # Sidecars are optional for legacy/imported clips. A stale/corrupt
        # sidecar must cleanly fall back to MP4 metadata, never invent tracks.
        return None


def verified_audio_tracks_for_export(
    media_path: str | Path,
) -> tuple[tuple[str, int, str], ...]:
    """Return export tracks from a valid, hash-bound manifest.

    Convert absolute MP4 stream indices to audio-relative FFmpeg ``0:a:N``
    selectors. Missing or stale sidecars yield no tracks.
    """

    manifest = read_manifest_for_media(media_path)
    if not manifest:
        return ()
    sources = sorted(manifest['sources'], key=lambda source: source['stream_index'])
    return tuple(
        (source['source_uuid'], audio_index, source['display_name'])
        for audio_index, source in enumerate(sources)
    )


def _validate_source(source: Any, seen_uuid: set[str], seen_stream: set[int]) -> None:
    if not isinstance(source, dict):
        raise AudioManifestError('source must be an object')
    source_uuid = _validate_uuid(source.get('source_uuid'), 'source_uuid')
    if source_uuid in seen_uuid:
        raise AudioManifestError('source UUID appears more than once')
    seen_uuid.add(source_uuid)
    stream_index = source.get('stream_index')
    if not isinstance(stream_index, int) or stream_index < 1:
        raise AudioManifestError('stream_index must be a positive audio stream index')
    if stream_index in seen_stream:
        raise AudioManifestError('stream index appears more than once')
    seen_stream.add(stream_index)
    if source.get('source_type') not in _SOURCE_TYPES:
        raise AudioManifestError('unknown source type')
    for field in ('display_name', 'persistent_identity', 'sample_format'):
        value = source.get(field)
        if not isinstance(value, str) or not _SAFE_TEXT.fullmatch(value):
            raise AudioManifestError(f'{field} is not manifest-safe')
    icon_reference = source.get('icon_reference')
    if icon_reference is not None and (
            not isinstance(icon_reference, str) or not _SAFE_TEXT.fullmatch(icon_reference)):
        raise AudioManifestError('icon_reference is not manifest-safe')
    for field in ('first_active_100ns', 'last_active_100ns'):
        if not isinstance(source.get(field), int) or source[field] < 0:
            raise AudioManifestError(f'{field} must be a non-negative integer')
    if source['last_active_100ns'] < source['first_active_100ns']:
        raise AudioManifestError('source active interval is inverted')
    if not isinstance(source.get('sample_rate'), int) or not 8000 <= source['sample_rate'] <= 192000:
        raise AudioManifestError('sample_rate is out of range')
    if not isinstance(source.get('channels'), int) or not 1 <= source['channels'] <= 8:
        raise AudioManifestError('channels is out of range')


def _validate_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise AudioManifestError(f'{field} must be a UUID string')
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise AudioManifestError(f'{field} must be a UUID string') from error
