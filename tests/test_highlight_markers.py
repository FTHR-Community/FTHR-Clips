from __future__ import annotations

import json
from uuid import uuid4

import pytest

from core.highlight_markers import (
    MAX_HIGHLIGHT_MARKERS,
    HighlightMarkerError,
    UnsupportedHighlightMarkersError,
    decode_highlight_markers,
    markers_to_payload,
)


def _marker(position, label=''):
    return {
        'marker_id': str(uuid4()),
        'position_100ns': position,
        'label': label,
    }


def test_markers_are_strictly_validated_and_sorted_by_source_position_then_id():
    later = _marker(20, 'later')
    earlier = _marker(10, 'earlier')

    normalized = decode_highlight_markers(
        markers_to_payload([later, earlier]))

    assert [marker.position_100ns for marker in normalized] == [10, 20]
    assert [marker.label for marker in normalized] == ['earlier', 'later']


@pytest.mark.parametrize('position', [True, 1.0, -1, 2 ** 63])
def test_position_rejects_bool_float_and_out_of_range_values(position):
    with pytest.raises(HighlightMarkerError):
        markers_to_payload([_marker(position)])


def test_duplicate_ids_controls_future_versions_and_limits_fail_closed():
    duplicate = _marker(1)
    with pytest.raises(HighlightMarkerError):
        markers_to_payload([duplicate, dict(duplicate, position_100ns=2)])
    with pytest.raises(HighlightMarkerError):
        markers_to_payload([_marker(1, 'line\nfeed')])
    with pytest.raises(HighlightMarkerError):
        markers_to_payload([_marker(1, 'line\u2028separator')])
    with pytest.raises(HighlightMarkerError):
        markers_to_payload([_marker(1, 'paragraph\u2029separator')])

    future = {'version': 2, 'markers': []}
    with pytest.raises(UnsupportedHighlightMarkersError):
        decode_highlight_markers(future)

    too_many = [_marker(index) for index in range(MAX_HIGHLIGHT_MARKERS + 1)]
    with pytest.raises(HighlightMarkerError):
        markers_to_payload(too_many)


def test_payload_size_is_measured_as_utf8_json():
    marker = _marker(0, '✓')
    encoded = json.dumps(markers_to_payload([marker]), ensure_ascii=False,
                         separators=(',', ':')).encode('utf-8')
    assert encoded.endswith(b'}')


def test_encoded_codec_rejects_duplicate_keys_and_excessive_nesting():
    with pytest.raises(HighlightMarkerError):
        decode_highlight_markers('{"version":1,"version":1,"markers":[]}')
    nested = '[' * 300 + ']' * 300
    with pytest.raises(HighlightMarkerError):
        decode_highlight_markers(nested)
