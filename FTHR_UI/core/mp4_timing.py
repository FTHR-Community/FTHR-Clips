"""Safely normalize one supported MP4 presentation edit without re-encoding.

The helper deliberately supports a narrow, auditable MP4 shape.  It changes
only zero-duration empty edits in front of unambiguous positive-rate video and
audio edit lists.  Original edit entries and all composition timing tables
remain unchanged.  A new destination is streamed from the source in 1 MiB
blocks; media payloads are never loaded into memory.  Unsupported or malformed
media returns ``False`` and leaves no destination file.  Invalid API paths and
hard I/O failures raise the corresponding ``ValueError`` or ``OSError``.
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


_MAX_MOOV_BYTES = 16 * 1024 * 1024
_MAX_BOX_DEPTH = 32
_MAX_BOX_COUNT = 50_000
_COPY_BLOCK_BYTES = 1024 * 1024
_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1

_CONTAINER_TYPES = frozenset({
    b'dinf', b'edts', b'mdia', b'minf', b'moov', b'stbl', b'trak',
})
_REJECTED_FRAGMENT_TYPES = frozenset({
    b'mfra', b'moof', b'mvex', b'sidx', b'ssix',
    b'tfdt', b'tfhd', b'traf', b'trex', b'trun',
})
_REJECTED_PROTECTED_TYPES = frozenset({
    b'encv', b'enca', b'drms', b'saio', b'saiz', b'schi',
    b'schm', b'senc', b'sinf', b'tenc', b'tref',
})


class Mp4TimingError(ValueError):
    """Raised for an invalid source/destination API contract."""


class _Reject(Exception):
    """Internal marker for an unsupported or malformed MP4."""


@dataclass
class _Box:
    typ: bytes
    payload: bytes | bytearray | memoryview
    children: list['_Box'] | None = None
    extended: bool = False


@dataclass(frozen=True)
class _TopLevelBox:
    offset: int
    size: int
    typ: bytes
    header_size: int


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass
class _ParseBudget:
    count: int = 0


def _cancelled(cancelled: threading.Event | None) -> bool:
    return cancelled is not None and cancelled.is_set()


def _reject(message: str) -> None:
    raise _Reject(message)


def _unpack_u32(data: bytes | bytearray | memoryview, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        _reject('truncated MP4 integer')
    return struct.unpack_from('>I', data, offset)[0]


def _unpack_u64(data: bytes | bytearray | memoryview, offset: int) -> int:
    if offset < 0 or offset + 8 > len(data):
        _reject('truncated MP4 extended size')
    return struct.unpack_from('>Q', data, offset)[0]


def _parse_box_header(data: bytes | bytearray | memoryview, offset: int, end: int) -> tuple[bytes, int, int, bool]:
    if offset < 0 or offset + 8 > end or end > len(data):
        _reject('truncated MP4 box header')
    size32 = _unpack_u32(data, offset)
    typ = bytes(data[offset + 4:offset + 8])
    if size32 == 0:
        _reject(f'unsupported size-zero MP4 box {typ!r}')
    if size32 == 1:
        size = _unpack_u64(data, offset + 8)
        header_size = 16
        if offset + header_size > end:
            _reject('truncated extended MP4 box header')
        extended = True
    else:
        size = size32
        header_size = 8
        extended = False
    if size < header_size or size > end - offset:
        _reject(f'invalid MP4 box size for {typ!r}')
    return typ, size, header_size, extended


def _parse_boxes(data: bytes | bytearray | memoryview, start: int = 0, end: int | None = None,
                 *, depth: int = 0, budget: _ParseBudget | None = None,
                 cancelled: threading.Event | None = None) -> list[_Box]:
    # Keep one immutable owner for all nested slices. Mutable callers retain
    # the existing snapshot semantics; file input is already immutable bytes.
    if not (isinstance(data, memoryview) and isinstance(data.obj, bytes)):
        data = memoryview(bytes(data))
    if end is None:
        end = len(data)
    if depth > _MAX_BOX_DEPTH:
        _reject('MP4 box nesting exceeds the safety limit')
    if budget is None:
        budget = _ParseBudget()
    boxes: list[_Box] = []
    offset = start
    while offset < end:
        if _cancelled(cancelled):
            _reject('normalization cancelled')
        budget.count += 1
        if budget.count > _MAX_BOX_COUNT:
            _reject('MP4 box count exceeds the safety limit')
        typ, size, header_size, extended = _parse_box_header(data, offset, end)
        payload_start = offset + header_size
        payload_end = offset + size
        payload = data[payload_start:payload_end]
        children = (_parse_boxes(
            payload, depth=depth + 1, budget=budget, cancelled=cancelled)
                    if typ in _CONTAINER_TYPES else None)
        boxes.append(_Box(typ, payload, children, extended))
        offset = payload_end
    if offset != end:
        _reject('MP4 box boundaries do not cover the parent')
    return boxes


def _box_bytes(box: _Box) -> bytes:
    payload = (b''.join(_box_bytes(child) for child in box.children)
               if box.children is not None else bytes(box.payload))
    size = len(payload) + (16 if box.extended else 8)
    use_extended = box.extended or size > _UINT32_MAX
    if use_extended:
        size = len(payload) + 16
        if size > _UINT64_MAX:
            _reject(f'MP4 box {box.typ!r} is too large')
        return struct.pack('>I4sQ', 1, box.typ, size) + payload
    return struct.pack('>I4s', size, box.typ) + payload


def _walk(box: _Box) -> Iterable[_Box]:
    yield box
    if box.children is not None:
        for child in box.children:
            yield from _walk(child)


def _children_of(box: _Box, typ: bytes) -> list[_Box]:
    return [child for child in (box.children or []) if child.typ == typ]


def _single_child(box: _Box, typ: bytes, *, required: bool = True) -> _Box | None:
    children = _children_of(box, typ)
    if len(children) > 1:
        _reject(f'multiple {typ.decode("latin1")} boxes are ambiguous')
    if required and not children:
        _reject(f'missing {typ.decode("latin1")} box')
    return children[0] if children else None


def _read_file_header(source: BinaryIO, offset: int, file_size: int) -> _TopLevelBox:
    source.seek(offset)
    raw = source.read(8)
    if len(raw) != 8:
        _reject('truncated top-level MP4 box header')
    size32, typ = struct.unpack('>I4s', raw)
    if size32 == 0:
        _reject(f'unsupported size-zero top-level box {typ!r}')
    if size32 == 1:
        raw_extended = source.read(8)
        if len(raw_extended) != 8:
            _reject('truncated top-level extended MP4 box header')
        size = struct.unpack('>Q', raw_extended)[0]
        header_size = 16
    else:
        size = size32
        header_size = 8
    if size < header_size or size > file_size - offset:
        _reject(f'invalid top-level MP4 box size for {typ!r}')
    return _TopLevelBox(offset, size, typ, header_size)


def _scan_top_level(source: BinaryIO, file_size: int,
                    cancelled: threading.Event | None = None) -> list[_TopLevelBox]:
    result: list[_TopLevelBox] = []
    offset = 0
    while offset < file_size:
        if _cancelled(cancelled):
            _reject('normalization cancelled')
        box = _read_file_header(source, offset, file_size)
        result.append(box)
        if len(result) > _MAX_BOX_COUNT:
            _reject('MP4 top-level box count exceeds the safety limit')
        offset += box.size
    if offset != file_size:
        _reject('top-level MP4 boxes do not cover the file')
    return result


def _read_exact(source: BinaryIO, offset: int, size: int) -> bytes:
    source.seek(offset)
    data = source.read(size)
    if len(data) != size:
        _reject('truncated MP4 box payload')
    return data


def _inspect_source_file(source: BinaryIO, file_size: int,
                         cancelled: threading.Event | None) -> tuple[
                             list[_TopLevelBox], _TopLevelBox, _Box]:
    if _cancelled(cancelled):
        _reject('normalization cancelled')
    top_level = _scan_top_level(source, file_size, cancelled)
    if any(box.typ in _REJECTED_FRAGMENT_TYPES for box in top_level):
        _reject('fragmented MP4 is unsupported')
    moov_boxes = [box for box in top_level if box.typ == b'moov']
    if len(moov_boxes) != 1:
        _reject('MP4 must contain exactly one MOOV')
    moov_info = moov_boxes[0]
    if moov_info.size > _MAX_MOOV_BYTES:
        _reject('MOOV exceeds the 16 MiB safety limit')
    moov_data = _read_exact(source, moov_info.offset, moov_info.size)
    parsed_moov = _parse_boxes(moov_data, cancelled=cancelled)
    if len(parsed_moov) != 1 or parsed_moov[0].typ != b'moov':
        _reject('missing or malformed MOOV')
    return top_level, moov_info, parsed_moov[0]


def _file_identity(stat_result: os.stat_result) -> _FileIdentity:
    return _FileIdentity(
        getattr(stat_result, 'st_dev', 0),
        getattr(stat_result, 'st_ino', 0),
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _same_source_identity(source: Path, source_fd: BinaryIO,
                          expected_path: _FileIdentity,
                          expected_fd: _FileIdentity) -> bool:
    try:
        path_identity = _file_identity(source.stat())
        fd_identity = _file_identity(os.fstat(source_fd.fileno()))
    except OSError:
        # A disappearing or replaced source is an expected negative result.
        return False
    return path_identity == expected_path and fd_identity == expected_fd


def _parse_handler_type(trak: _Box) -> bytes:
    mdia = _single_child(trak, b'mdia')
    assert mdia is not None
    hdlr = _single_child(mdia, b'hdlr')
    assert hdlr is not None
    payload = bytes(hdlr.payload)
    if len(payload) < 12:
        _reject('truncated handler box')
    return payload[8:12]


def _validate_data_references(trak: _Box) -> int:
    drefs = [box for box in _walk(trak) if box.typ == b'dref']
    if len(drefs) > 1:
        _reject('multiple data-reference boxes are ambiguous')
    if not drefs:
        return 0
    payload = bytes(drefs[0].payload)
    if len(payload) < 8:
        _reject('truncated data-reference box')
    count = _unpack_u32(payload, 4)
    if count == 0:
        _reject('data-reference box has no entries')
    offset = 8
    for _ in range(count):
        typ, size, header_size, _extended = _parse_box_header(
            payload, offset, len(payload))
        entry_end = offset + size
        if typ != b'url ' or size != header_size + 4:
            _reject('external or unsupported MP4 data reference')
        flags = _unpack_u32(payload, offset + header_size) & 0x00FFFFFF
        if flags != 1:
            _reject('external MP4 data reference')
        offset = entry_end
    if offset != len(payload):
        _reject('malformed data-reference entries')
    return count


def _validate_track_shape(trak: _Box) -> bytes:
    for box in _walk(trak):
        if box.typ in _REJECTED_PROTECTED_TYPES:
            _reject(f'protected or externally referenced track box {box.typ!r}')
    return _parse_handler_type(trak)


def _parse_stts_sample_count(stts: _Box) -> int:
    payload = bytes(stts.payload)
    if len(payload) < 8 or payload[0] != 0:
        _reject('unsupported or truncated STTS box')
    count = _unpack_u32(payload, 4)
    expected = 8 + count * 8
    if expected != len(payload):
        _reject('malformed STTS entry data')
    total = 0
    offset = 8
    for _ in range(count):
        sample_count = _unpack_u32(payload, offset)
        if sample_count == 0:
            _reject('zero-count STTS entry')
        total += sample_count
        offset += 8
    if total == 0:
        _reject('empty STTS table')
    return total


def _parse_elst_entries(elst: _Box) -> tuple[int, list[tuple[int, int, int, int]]]:
    payload = bytes(elst.payload)
    if len(payload) < 8 or payload[0] not in (0, 1):
        _reject('unsupported or truncated ELST box')
    version = payload[0]
    count = _unpack_u32(payload, 4)
    entry_size = 20 if version == 1 else 12
    if len(payload) != 8 + count * entry_size:
        _reject('malformed edit list entries')
    entries = []
    for index in range(count):
        offset = 8 + index * entry_size
        if version == 0:
            entries.append((
                _unpack_u32(payload, offset),
                struct.unpack_from('>i', payload, offset + 4)[0],
                struct.unpack_from('>h', payload, offset + 8)[0],
                struct.unpack_from('>H', payload, offset + 10)[0],
            ))
        else:
            entries.append((
                _unpack_u64(payload, offset),
                struct.unpack_from('>q', payload, offset + 8)[0],
                struct.unpack_from('>h', payload, offset + 16)[0],
                struct.unpack_from('>H', payload, offset + 18)[0],
            ))
    return version, entries


def _is_positive_rate_entry(entry: tuple[int, int, int, int]) -> bool:
    segment_duration, media_time, rate_integer, rate_fraction = entry
    return (segment_duration > 0 and media_time > 0
            and rate_integer == 1 and rate_fraction == 0)


def _parse_elst_for_normalization(elst: _Box) -> tuple[int, bool]:
    version, entries = _parse_elst_entries(elst)
    # Only one positive entry is unambiguous.  A zero-empty edit followed by
    # one positive entry is already normalized and must remain byte-stable.
    eligible = (len(entries) == 1 and _is_positive_rate_entry(entries[0]))
    already_normalized = (
        len(entries) == 2
        and entries[0][0] == 0
        and entries[0][1] == -1
        and entries[0][2:] == (1, 0)
        and _is_positive_rate_entry(entries[1]))
    return version, eligible and not already_normalized


def _prepend_zero_empty_edit(elst: _Box, version: int) -> None:
    payload = bytes(elst.payload)
    count = _unpack_u32(payload, 4)
    entry_size = 20 if version == 1 else 12
    if count != 1 or len(payload) != 8 + entry_size:
        _reject('video edit list must contain exactly one original entry')
    prefix = (struct.pack('>B3sI', version, payload[1:4], 2)
              + (struct.pack('>Ii hH', 0, -1, 1, 0)
                 if version == 0 else
                 struct.pack('>Qq hH', 0, -1, 1, 0)))
    elst.payload = prefix + payload[8:]


def _validate_ctts(ctts: _Box, sample_count: int) -> None:
    payload = bytes(ctts.payload)
    if len(payload) < 8 or payload[0] not in (0, 1):
        _reject('unsupported or truncated CTTS box')
    entry_count = _unpack_u32(payload, 4)
    if len(payload) != 8 + entry_count * 8:
        _reject('malformed CTTS entry data')
    total_samples = 0
    offset = 8
    for _ in range(entry_count):
        count = _unpack_u32(payload, offset)
        if count == 0:
            _reject('zero-count CTTS entry')
        total_samples += count
        offset += 8
    if total_samples != sample_count:
        _reject('CTTS sample count does not match STTS')


def _validate_sample_description(stsd: _Box, data_reference_count: int) -> None:
    payload = bytes(stsd.payload)
    if len(payload) < 8:
        _reject('truncated sample-description box')
    entry_count = _unpack_u32(payload, 4)
    if entry_count == 0:
        _reject('sample-description box has no sample entries')
    entries = _parse_boxes(payload, start=8)
    if len(entries) != entry_count:
        _reject('sample-description entry count does not match entries')
    if any(entry.typ in (b'encv', b'enca', b'drms') for entry in entries):
        _reject('protected sample entry is unsupported')
    for entry in entries:
        entry_payload = bytes(entry.payload)
        if len(entry_payload) < 8:
            _reject('truncated sample-description entry')
        data_reference_index = struct.unpack_from('>H', entry_payload, 6)[0]
        if (data_reference_index == 0
                or data_reference_index > data_reference_count):
            _reject('sample entry has an invalid data-reference index')


def _offset_values(box: _Box) -> tuple[list[int], int]:
    payload = bytes(box.payload)
    if len(payload) < 8 or payload[0] != 0:
        _reject('unsupported or truncated chunk-offset box')
    count = _unpack_u32(payload, 4)
    width = 4 if box.typ == b'stco' else 8
    if len(payload) != 8 + count * width:
        _reject('malformed chunk-offset entries')
    values = [
        ( _unpack_u32(payload, 8 + index * width)
          if width == 4 else _unpack_u64(payload, 8 + index * width))
        for index in range(count)
    ]
    return values, width


def _shift_chunk_offsets(boxes: Iterable[_Box], *, moov_offset: int,
                         old_moov_end: int, delta: int,
                         mdat_ranges: list[tuple[int, int]]) -> None:
    offset_tables = [(box, *_offset_values(box)) for box in boxes]
    for _box, values, _width in offset_tables:
        for value in values:
            if not any(start <= value < end for start, end in mdat_ranges):
                _reject('chunk offset does not point into an MDAT payload')
            if moov_offset <= value < old_moov_end:
                _reject('chunk offset points inside the original MOOV')
    for box, values, width in offset_tables:
        payload = bytearray(box.payload)
        for index, value in enumerate(values):
            if not delta:
                continue
            shifted = value + delta if value >= old_moov_end else value
            limit = _UINT32_MAX if width == 4 else _UINT64_MAX
            if shifted < 0 or shifted > limit:
                _reject('chunk offset overflows its MP4 table width')
            struct.pack_into('>I' if width == 4 else '>Q', payload,
                             8 + index * width, shifted)
        box.payload = payload


def _validate_track_tables(tracks: list[_Box]) -> None:
    for track in tracks:
        data_reference_count = _validate_data_references(track)
        mdia = _single_child(track, b'mdia')
        assert mdia is not None
        minf = _single_child(mdia, b'minf')
        stbl = _single_child(minf, b'stbl')
        assert stbl is not None
        stsd = _single_child(stbl, b'stsd')
        assert stsd is not None
        _validate_sample_description(stsd, data_reference_count)
        stts_boxes = _children_of(stbl, b'stts')
        if len(stts_boxes) > 1:
            _reject('multiple STTS boxes are ambiguous')
        sample_count = (_parse_stts_sample_count(stts_boxes[0])
                        if stts_boxes else None)
        ctts_boxes = _children_of(stbl, b'ctts')
        if len(ctts_boxes) > 1:
            _reject('multiple CTTS boxes are ambiguous')
        if ctts_boxes:
            if sample_count is None:
                _reject('CTTS has no matching STTS table')
            _validate_ctts(ctts_boxes[0], sample_count)


def _validated_moov_targets(
        moov: _Box) -> tuple[list[_Box], list[tuple[_Box, int]]]:
    if moov.typ != b'moov' or moov.children is None:
        _reject('missing or malformed MOOV')
    if any(box.typ in _REJECTED_FRAGMENT_TYPES for box in _walk(moov)):
        _reject('fragmented MP4 is unsupported')
    if any(box.typ == b'cmov' for box in _walk(moov)):
        _reject('compressed MOOV is unsupported')

    tracks = _children_of(moov, b'trak')
    if not tracks:
        _reject('MOOV has no tracks')
    handlers = [_validate_track_shape(track) for track in tracks]
    video_tracks = [track for track, handler in zip(tracks, handlers, strict=True)
                    if handler == b'vide']
    if len(video_tracks) != 1:
        _reject('MP4 must contain exactly one video track')
    _validate_track_tables(tracks)

    targets: list[tuple[_Box, int]] = []
    for track, handler in zip(tracks, handlers, strict=True):
        if handler not in (b'vide', b'soun'):
            continue
        edts = _single_child(track, b'edts', required=False)
        if edts is None:
            continue
        elst = _single_child(edts, b'elst', required=False)
        if elst is None:
            continue
        version, eligible = _parse_elst_for_normalization(elst)
        if eligible:
            targets.append((elst, version))
    return tracks, targets


def _transform_moov(moov: _Box, *, old_moov_size: int,
                    moov_offset: int,
                    mdat_ranges: list[tuple[int, int]]) -> bytes | None:
    tracks, targets = _validated_moov_targets(moov)
    if not targets:
        return None
    for elst, version in targets:
        _prepend_zero_empty_edit(elst, version)

    offset_boxes = [box for track in tracks for box in _walk(track)
                    if box.typ in (b'stco', b'co64')]
    # Validate all offset tables before deciding whether a faststart shift is
    # needed.  The tables in audio tracks are only ever shifted for MOOV size.
    for box in offset_boxes:
        _offset_values(box)

    modified = _box_bytes(moov)
    delta = len(modified) - old_moov_size
    _shift_chunk_offsets(
        offset_boxes,
        moov_offset=moov_offset,
        old_moov_end=moov_offset + old_moov_size,
        delta=delta,
        mdat_ranges=mdat_ranges,
    )
    modified = _box_bytes(moov)
    if len(modified) > _MAX_MOOV_BYTES:
        _reject('normalized MOOV exceeds the 16 MiB safety limit')
    return modified


def _copy_range(source: BinaryIO, destination: BinaryIO, start: int,
                length: int, cancelled: threading.Event | None) -> bool:
    source.seek(start)
    remaining = length
    while remaining:
        if _cancelled(cancelled):
            return False
        chunk = source.read(min(_COPY_BLOCK_BYTES, remaining))
        if not chunk:
            _reject('source changed or ended while copying')
        destination.write(chunk)
        remaining -= len(chunk)
    return not _cancelled(cancelled)


def _validate_paths(source: Path, destination: Path) -> None:
    if source.resolve(strict=False) == destination.resolve(strict=False):
        raise Mp4TimingError('source and destination must be different paths')
    if destination.exists():
        raise FileExistsError(destination)
    if not source.is_file():
        raise FileNotFoundError(source)


def needs_mp4_presentation_normalization(
        source: str | Path,
        cancelled: threading.Event | None = None) -> bool:
    """Return whether ``source`` is eligible for the bounded MP4 rewrite.

    This performs the same bounded structure and source-identity checks as
    :func:`normalize_mp4_presentation`, without creating an output file or
    changing the source.  Unsupported, malformed, already-normalized, and
    cancelled inputs return ``False``; hard source I/O failures raise.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if _cancelled(cancelled):
        return False
    try:
        with source_path.open('rb') as source_file:
            expected_path = _file_identity(source_path.stat())
            expected_fd = _file_identity(os.fstat(source_file.fileno()))
            if expected_path != expected_fd:
                return False
            top_level, moov_info, moov = _inspect_source_file(
                source_file, expected_fd.size, cancelled)
            transformed = _transform_moov(
                moov,
                old_moov_size=moov_info.size,
                moov_offset=moov_info.offset,
                mdat_ranges=[
                    (box.offset + box.header_size, box.offset + box.size)
                    for box in top_level if box.typ == b'mdat'
                ],
            )
            return (transformed is not None
                    and not _cancelled(cancelled)
                    and _same_source_identity(source_path, source_file,
                                               expected_path, expected_fd))
    except _Reject:
        # Unsupported or malformed bounded structure means no normalization.
        return False


def normalize_mp4_presentation(
        source: str | Path,
        destination: str | Path,
        cancelled: threading.Event | None = None) -> bool:
    """Write a normalized copy of a supported non-fragmented MP4.

    The source is never overwritten.  ``False`` means the media was
    unsupported, malformed, unchanged, or cancellation was requested; any
    partially written destination is removed.  Existing destinations and
    source/path I/O failures raise ``ValueError`` or ``OSError``.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    _validate_paths(source_path, destination_path)
    if _cancelled(cancelled):
        return False

    created_destination = False
    successful = False
    try:
        with source_path.open('rb') as source_file:
            initial_path_identity = _file_identity(source_path.stat())
            initial_fd_identity = _file_identity(os.fstat(source_file.fileno()))
            if initial_path_identity != initial_fd_identity:
                return False
            file_size = initial_fd_identity.size
            top_level, moov_info, parsed_moov = _inspect_source_file(
                source_file, file_size, cancelled)
            normalized_moov = _transform_moov(
                parsed_moov,
                old_moov_size=moov_info.size,
                moov_offset=moov_info.offset,
                mdat_ranges=[
                    (box.offset + box.header_size, box.offset + box.size)
                    for box in top_level if box.typ == b'mdat'
                ],
            )
            if normalized_moov is None:
                return False
            if not _same_source_identity(source_path, source_file,
                                          initial_path_identity,
                                          initial_fd_identity):
                return False
            if _cancelled(cancelled):
                return False

            with destination_path.open('xb') as destination_file:
                created_destination = True
                cursor = 0
                for box in top_level:
                    if box.typ == b'moov':
                        if not _copy_range(source_file, destination_file, cursor,
                                           box.offset - cursor, cancelled):
                            return False
                        if _cancelled(cancelled):
                            return False
                        destination_file.write(normalized_moov)
                        cursor = box.offset + box.size
                    else:
                        if not _copy_range(source_file, destination_file, cursor,
                                           box.offset - cursor, cancelled):
                            return False
                        if not _copy_range(source_file, destination_file,
                                           box.offset, box.size, cancelled):
                            return False
                        cursor = box.offset + box.size
                if cursor != file_size:
                    _reject('copy did not cover the source file')
                if _cancelled(cancelled):
                    return False
                destination_file.flush()
                os.fsync(destination_file.fileno())
            if not _same_source_identity(source_path, source_file,
                                         initial_path_identity,
                                         initial_fd_identity):
                return False
            successful = True
            return True
    except _Reject:
        # Unsupported or malformed bounded structure means no output.
        return False
    finally:
        if created_destination and not successful:
            try:
                destination_path.unlink(missing_ok=True)
            except OSError:
                # Preserve the original failure; callers still never receive
                # a successful result for a destination that could not cleanly
                # be finalized.
                pass


def normalize_mp4_presentation_in_place(
        source: str | Path,
        cancelled: threading.Event | None = None,
        *,
        commit_replace=None) -> bool:
    """Replace an eligible unpublished MP4 with a normalized temporary copy.

    The caller owns the unpublished path and is responsible for any related
    manifest or sidecar updates.  The source is checked again after the copy
    and immediately before replacement; cancellation or any failed
    eligibility check leaves the original path untouched.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if _cancelled(cancelled):
        return False

    temp_path: Path | None = None
    temp_dir = tempfile.TemporaryDirectory(
        dir=str(source_path.parent),
        prefix=f'.{source_path.name}.normalize-',
    )
    try:
        temp_path = Path(temp_dir.name) / source_path.name
        with source_path.open('rb') as source_file:
            expected_path = _file_identity(source_path.stat())
            expected_fd = _file_identity(os.fstat(source_file.fileno()))
            if expected_path != expected_fd:
                return False
            if not normalize_mp4_presentation(source_path, temp_path, cancelled):
                return False
            if _cancelled(cancelled) or not _same_source_identity(
                    source_path, source_file, expected_path, expected_fd):
                return False
        if _cancelled(cancelled):
            return False
        if commit_replace is None:
            os.replace(temp_path, source_path)
        elif commit_replace(temp_path, source_path) is False:
            return False
        temp_path = None
        return True
    finally:
        temp_dir.cleanup()
