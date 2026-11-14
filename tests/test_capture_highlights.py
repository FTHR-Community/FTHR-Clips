from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from uuid import uuid4

import pytest

from core.capture_highlights import (
    HIGHLIGHT_UUID,
    MAX_HIGHLIGHT_METADATA_BYTES,
    CaptureHighlightError,
    read_capture_highlights,
    write_capture_highlights,
)


def _marker(position: int, label: str = '') -> dict:
    return {'marker_id': str(uuid4()), 'position_100ns': position, 'label': label}


def _box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack('>I4s', 8 + len(payload), box_type) + payload


def _uuid_box(payload: dict) -> bytes:
    encoded = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    return _box(b'uuid', HIGHLIGHT_UUID + encoded)


def _write_mp4(path: Path, *boxes: bytes) -> None:
    path.write_bytes(_box(b'ftyp', b'isom\x00\x00\x02\x00mp42') + b''.join(boxes))


def test_reader_merges_committed_batches_and_ignores_unrelated_uuid(tmp_path):
    first = _marker(100, 'one')
    second = _marker(200, 'two')
    payload = {'version': 1, 'markers': [first]}
    duplicate = {'version': 1, 'markers': [first]}
    _write_mp4(
        tmp_path / 'clip.mp4',
        _box(b'uuid', b'foreign' * 8),
        _uuid_box(payload),
        _uuid_box(duplicate),
        _uuid_box({'version': 1, 'markers': [second]}),
    )
    markers = read_capture_highlights(tmp_path / 'clip.mp4')
    assert [marker.position_100ns for marker in markers] == [100, 200]


def test_reader_rejects_future_and_conflicting_complete_own_payloads(tmp_path):
    marker = _marker(100)
    path = tmp_path / 'future.mp4'
    _write_mp4(path, _uuid_box({'version': 2, 'markers': []}))
    with pytest.raises(CaptureHighlightError):
        read_capture_highlights(path)

    path = tmp_path / 'conflict.mp4'
    _write_mp4(
        path,
        _uuid_box({'version': 1, 'markers': [marker]}),
        _uuid_box({'version': 1, 'markers': [dict(marker, label='changed')]}),
    )
    with pytest.raises(CaptureHighlightError):
        read_capture_highlights(path)


def test_writer_is_idempotent_and_keeps_exact_large_integer_position(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    marker = _marker(2 ** 63 - 1, 'exact')
    first_identity = write_capture_highlights(path, [marker])
    second_identity = write_capture_highlights(
        path, [marker], expected_identity=first_identity)
    assert second_identity == first_identity
    assert read_capture_highlights(path)[0].position_100ns == 2 ** 63 - 1


def test_terminal_truncated_box_is_treated_as_uncommitted_tail(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4s', 100, b'uuid') + HIGHLIGHT_UUID)
    assert read_capture_highlights(path) is None


def test_partial_tail_preserves_committed_markers_and_blocks_append(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    marker = _marker(77)
    write_capture_highlights(path, [marker])
    partial = b'{"version":1,"markers":'
    declared_size = 8 + 16 + len(partial) + 1
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4s', declared_size, b'uuid')
                     + HIGHLIGHT_UUID + partial)
    assert [item.position_100ns for item in read_capture_highlights(path)] == [77]
    with pytest.raises(CaptureHighlightError):
        write_capture_highlights(path, [marker])


def test_nonregular_mp4_source_is_refused_before_read_or_append(tmp_path):
    path = tmp_path / 'directory.mp4'
    path.mkdir()
    with pytest.raises(CaptureHighlightError):
        read_capture_highlights(path)
    with pytest.raises(CaptureHighlightError):
        write_capture_highlights(path, [])


def test_size_zero_own_box_is_refused_before_payload_read(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4s', 0, b'uuid') + HIGHLIGHT_UUID
                     + b'{"version":99,"markers":[]}'
                     + b'x' * 1024)
    with pytest.raises(CaptureHighlightError, match='size-zero own'):
        read_capture_highlights(path)


def test_extended_own_box_is_bounded_before_payload_read(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    own_payload = HIGHLIGHT_UUID + b'x' * (MAX_HIGHLIGHT_METADATA_BYTES + 1)
    size = 16 + len(own_payload)
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4sQ', 1, b'uuid', size) + own_payload)
    with pytest.raises(CaptureHighlightError, match='exceeds'):
        read_capture_highlights(path)


def test_writer_rejects_same_size_foreign_replacement_after_close(
        tmp_path, monkeypatch):
    import core.capture_highlights as capture_module

    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    original_open = capture_module._open_stable

    class ClosingReplacement:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            self.inner.close()
            replacement = path.with_name('foreign.mp4')
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)

    monkeypatch.setattr(
        capture_module, '_open_stable',
        lambda source, *, writable: ClosingReplacement(
            original_open(source, writable=writable)))
    with pytest.raises(CaptureHighlightError, match='identity changed'):
        write_capture_highlights(path, [_marker(7)])


@pytest.mark.parametrize('suffix', ['.mkv', '.avi'])
def test_media_identity_supports_library_video_suffixes(tmp_path, suffix):
    from core.capture_highlights import media_identity

    path = tmp_path / f'clip{suffix}'
    path.write_bytes(b'library video')
    identity = media_identity(path)
    assert identity['size'] == len(b'library video')


def test_writer_refuses_terminal_size_zero_mdat_append(tmp_path):
    path = tmp_path / 'clip.mp4'
    _write_mp4(path)
    with path.open('ab') as handle:
        handle.write(struct.pack('>I4s', 0, b'mdat') + b'payload')
    assert read_capture_highlights(path) is None
    with pytest.raises(CaptureHighlightError):
        write_capture_highlights(path, [])
