"""Strict, versioned validation for clip highlight marker metadata.

The marker payload is deliberately independent of the metadata database
schema.  A future or malformed payload is readable only as an error and must
never be silently replaced by a writer.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID


HIGHLIGHT_MARKERS_VERSION = 1
MAX_HIGHLIGHT_MARKERS = 4096
MAX_HIGHLIGHT_METADATA_BYTES = 4 * 1024 * 1024
MAX_HIGHLIGHT_LABEL_LENGTH = 120
MAX_POSITION_100NS = (2 ** 63) - 1
HIGHLIGHT_MARKERS_FIELD = 'library_highlights'
_MAX_JSON_DEPTH = 256

_CONTAINER_FIELDS = frozenset(('version', 'markers'))
_MARKER_FIELDS = frozenset(('marker_id', 'position_100ns', 'label'))


class HighlightMarkerError(ValueError):
    """Base error for invalid marker input or stored marker metadata."""


class UnsupportedHighlightMarkersError(HighlightMarkerError):
    """The stored payload is from a future or otherwise unsupported format."""


@dataclass(frozen=True, slots=True)
class HighlightMarker:
    """A normalized marker in original presentation time (100 ns units)."""

    marker_id: str
    position_100ns: int
    label: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'marker_id': self.marker_id,
            'position_100ns': self.position_100ns,
            'label': self.label,
        }

    def __getitem__(self, key: str) -> Any:
        """Allow callers to use normalized markers like read-only records."""
        if key not in _MARKER_FIELDS:
            raise KeyError(key)
        return getattr(self, key)


def _canonical_marker_id(value: Any) -> str:
    if not isinstance(value, str):
        raise HighlightMarkerError('marker_id must be a canonical UUID string')
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as error:
        raise HighlightMarkerError(
            'marker_id must be a canonical UUID string') from error
    canonical = str(parsed)
    if value != canonical:
        raise HighlightMarkerError('marker_id must be a canonical UUID string')
    return canonical


def _validate_label(value: Any) -> str:
    if not isinstance(value, str):
        raise HighlightMarkerError('label must be a string')
    if len(value) > MAX_HIGHLIGHT_LABEL_LENGTH:
        raise HighlightMarkerError(
            f'label must be at most {MAX_HIGHLIGHT_LABEL_LENGTH} characters')
    if any(character in '\r\n\u2028\u2029'
           or unicodedata.category(character).startswith('C')
           for character in value):
        raise HighlightMarkerError('label must not contain control characters')
    return value


def _normalize_marker(value: Any) -> HighlightMarker:
    if isinstance(value, HighlightMarker):
        marker = value
    elif isinstance(value, Mapping):
        if set(value) != _MARKER_FIELDS:
            raise HighlightMarkerError(
                'marker fields must be marker_id, position_100ns and label')
        marker = HighlightMarker(
            marker_id=value['marker_id'],
            position_100ns=value['position_100ns'],
            label=value['label'],
        )
    else:
        raise HighlightMarkerError('each marker must be a mapping')

    marker_id = _canonical_marker_id(marker.marker_id)
    position = marker.position_100ns
    if type(position) is not int or not 0 <= position <= MAX_POSITION_100NS:
        raise HighlightMarkerError(
            'position_100ns must be an exact integer from 0 to 2**63-1')
    label = _validate_label(marker.label)
    return HighlightMarker(marker_id, position, label)


def normalize_highlight_markers(markers: Any) -> tuple[HighlightMarker, ...]:
    """Validate, de-duplicate-check and deterministically sort markers."""

    if isinstance(markers, (str, bytes, bytearray)) or not isinstance(markers, Sequence):
        raise HighlightMarkerError('markers must be a sequence')
    if len(markers) > MAX_HIGHLIGHT_MARKERS:
        raise HighlightMarkerError(
            f'at most {MAX_HIGHLIGHT_MARKERS} highlight markers are supported')
    normalized = tuple(_normalize_marker(value) for value in markers)
    ids = [marker.marker_id for marker in normalized]
    if len(set(ids)) != len(ids):
        raise HighlightMarkerError('marker_id values must be unique within one clip')
    return tuple(sorted(normalized, key=lambda marker: (
        marker.position_100ns, marker.marker_id)))


def markers_to_payload(markers: Any) -> dict[str, Any]:
    """Return the exact JSON object stored in ``library_highlights``."""

    normalized = normalize_highlight_markers(markers)
    payload = {
        'version': HIGHLIGHT_MARKERS_VERSION,
        'markers': [marker.as_dict() for marker in normalized],
    }
    encoded = _encoded_payload(payload)
    if len(encoded) > MAX_HIGHLIGHT_METADATA_BYTES:
        raise HighlightMarkerError(
            f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
    return payload


def _encoded_payload(payload: Mapping[str, Any]) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError,
            OverflowError) as error:
        raise HighlightMarkerError('highlight metadata is not valid JSON') from error


def encode_highlight_markers(markers: Any) -> dict[str, Any]:
    """Validate and encode markers as their JSON-compatible container."""

    return markers_to_payload(markers)


class _DuplicateJsonKey(ValueError):
    """Internal signal used to reject ambiguous encoded objects."""


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _validate_json_depth(raw: bytes) -> None:
    """Bound nesting before ``json.loads`` can recurse on attacker input."""

    depth = 0
    in_string = False
    escaped = False
    for value in raw:
        character = chr(value)
        if in_string:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in '[{':
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise HighlightMarkerError('library_highlights JSON is too deeply nested')
        elif character in ']}':
            depth = max(0, depth - 1)


def _decode_json_payload(payload: str | bytes | bytearray) -> Any:
    try:
        raw = payload.encode('utf-8') if isinstance(payload, str) else bytes(payload)
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise HighlightMarkerError('library_highlights is not valid JSON') from error
    if len(raw) > MAX_HIGHLIGHT_METADATA_BYTES:
        raise HighlightMarkerError(
            f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
    _validate_json_depth(raw)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_reject_duplicate_keys)
    except (_DuplicateJsonKey, UnicodeDecodeError, json.JSONDecodeError,
            RecursionError, TypeError, ValueError) as error:
        raise HighlightMarkerError('library_highlights is not valid JSON') from error


def decode_highlight_markers(payload: Any, *, allow_encoded: bool = True) -> tuple[HighlightMarker, ...]:
    """Decode a stored container, rejecting future and malformed payloads."""

    if isinstance(payload, (str, bytes, bytearray)):
        if not allow_encoded:
            raise HighlightMarkerError('library_highlights must be an object')
        payload = _decode_json_payload(payload)
    if not isinstance(payload, Mapping):
        raise HighlightMarkerError('library_highlights must be an object')
    if set(payload) != _CONTAINER_FIELDS:
        raise UnsupportedHighlightMarkersError(
            'unsupported library_highlights fields cannot be interpreted')
    version = payload.get('version')
    if type(version) is not int or version != HIGHLIGHT_MARKERS_VERSION:
        raise UnsupportedHighlightMarkersError(
            'unsupported library_highlights version')
    markers = payload.get('markers')
    if not isinstance(markers, list):
        raise HighlightMarkerError('library_highlights.markers must be a list')
    normalized = normalize_highlight_markers(markers)
    # JSON object key order is semantically irrelevant.  Re-encoding still
    # exercises the UTF-8/size boundary without requiring a particular order
    # from older writers.
    if len(_encoded_payload(payload)) > MAX_HIGHLIGHT_METADATA_BYTES:
        raise HighlightMarkerError(
            f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
    return normalized


def marker_dicts(markers: Any) -> list[dict[str, Any]]:
    """Return defensive JSON-compatible marker records for public APIs."""

    return [marker.as_dict() for marker in normalize_highlight_markers(markers)]


# Friendly aliases for callers that use parse/serialize terminology.
parse_highlight_markers = decode_highlight_markers
serialize_highlight_markers = markers_to_payload
