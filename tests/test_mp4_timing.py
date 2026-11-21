import os
import struct
import threading

import pytest

from core import mp4_timing
from core.mp4_timing import (
    needs_mp4_presentation_normalization,
    normalize_mp4_presentation,
    normalize_mp4_presentation_in_place,
)


def _box(kind, payload=b'', *, extended=False):
    kind = kind.encode() if isinstance(kind, str) else kind
    if extended:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def _handler(handler_type):
    handler_type = handler_type.encode()
    return _box('hdlr', b'\0\0\0\0' + b'\0\0\0\0'
                + handler_type + b'\0' * 12)


def _stts(sample_count):
    return _box('stts', struct.pack('>B3sIII', 0, b'\0\0\0', 1,
                                    sample_count, 1000))


def _stco(offset, *, co64=False):
    kind = 'co64' if co64 else 'stco'
    value = struct.pack('>Q', offset) if co64 else struct.pack('>I', offset)
    return _box(kind, b'\0\0\0\0' + struct.pack('>I', 1) + value)


def _ctts(entries, *, version=0):
    body = bytearray(b'\0\0\0\0' + struct.pack('>I', len(entries)))
    for count, offset in entries:
        body += struct.pack('>I', count)
        body += (struct.pack('>i', offset) if version == 1
                 else struct.pack('>I', offset))
    body[0] = version
    return _box('ctts', body)


def _elst_entries(entries, *, version=0):
    payload = struct.pack('>B3sI', version, b'\0\0\0', len(entries))
    for duration, media_time, rate_integer, rate_fraction in entries:
        payload += (struct.pack('>Ii hH', duration, media_time,
                                rate_integer, rate_fraction)
                    if version == 0 else
                    struct.pack('>Qq hH', duration, media_time,
                                rate_integer, rate_fraction))
    return _box('elst', payload)


def _elst(media_time, *, version=0, entries=1, duration=10000,
          rate_integer=1, rate_fraction=0):
    values = [(duration, media_time, rate_integer, rate_fraction)]
    values.extend((0, 0, 1, 0) for _ in range(entries - 1))
    return _elst_entries(values, version=version)


def _track(handler_type, *, media_time=None, elst_version=0, ctts_entries=None,
           ctts_version=0, chunk_offset=0, co64=False, sample_count=2,
           minf_extra=b'', stsd_payload=None, include_dref=True,
           elst_entries=None):
    sample_entry_type = 'avc1' if handler_type == 'vide' else 'mp4a'
    if stsd_payload is None:
        stsd_payload = (b'\0' * 4 + struct.pack('>I', 1)
                        + _box(sample_entry_type, b'\0' * 6 + b'\0\1'))
    stsd = _box('stsd', stsd_payload)
    stbl_children = [stsd, _stts(sample_count)]
    if ctts_entries is not None:
        stbl_children.append(_ctts(ctts_entries, version=ctts_version))
    stbl_children.append(_stco(chunk_offset, co64=co64))
    stbl = _box('stbl', b''.join(stbl_children))
    dinf = (_box('dinf', _box('dref', b'\0' * 4 + struct.pack('>I', 1)
                               + _box('url ', b'\0\0\0\1')))
             if include_dref else b'')
    minf = _box('minf', stbl + dinf + minf_extra)
    mdia = _box('mdia', _handler(handler_type) + minf)
    if elst_entries is not None:
        edts = _box('edts', _elst_entries(elst_entries, version=elst_version))
        return _box('trak', edts + mdia)
    if media_time is not None:
        edts = _box('edts', _elst(media_time, version=elst_version))
        return _box('trak', edts + mdia)
    return _box('trak', mdia)


def _make_mp4(*, front=True, ctts_entries=None, ctts_version=0,
              media_time=1000, elst_version=0, video_co64=False,
              audio_co64=False, video_offset=None, audio_offset=None,
              moov_extended=False, mdat_payload=b'video-and-audio-data',
              video_minf_extra=b'', video_stsd_payload=None,
              video_include_dref=True, video_elst_entries=None,
              audio_media_time=None, audio_elst_version=0,
              audio_elst_entries=None, audio_ctts_entries=None,
              audio_ctts_version=0, audio_count=1):
    ftyp = _box('ftyp', b'isom\0\0\0\0')
    video = _track('vide', media_time=media_time, elst_version=elst_version,
                   ctts_entries=ctts_entries, ctts_version=ctts_version,
                   chunk_offset=0 if video_offset is None else video_offset,
                   co64=video_co64, minf_extra=video_minf_extra,
                   stsd_payload=video_stsd_payload,
                   include_dref=video_include_dref,
                   elst_entries=video_elst_entries)
    audio_tracks = b''.join(
        _track('soun', media_time=audio_media_time,
               elst_version=audio_elst_version,
               elst_entries=audio_elst_entries,
               ctts_entries=audio_ctts_entries,
               ctts_version=audio_ctts_version,
               chunk_offset=0 if audio_offset is None else audio_offset,
               co64=audio_co64)
        for _ in range(audio_count))
    moov = _box('moov', video + audio_tracks, extended=moov_extended)
    mdat = _box('mdat', mdat_payload)
    if video_offset is None or audio_offset is None:
        data_offset = (len(ftyp) + len(moov) + 8) if front else len(ftyp) + 8
        video = _track('vide', media_time=media_time, elst_version=elst_version,
                       ctts_entries=ctts_entries, ctts_version=ctts_version,
                       chunk_offset=data_offset if video_offset is None else video_offset,
                       co64=video_co64, minf_extra=video_minf_extra,
                       stsd_payload=video_stsd_payload,
                       include_dref=video_include_dref,
                       elst_entries=video_elst_entries)
        audio_tracks = b''.join(
            _track('soun', media_time=audio_media_time,
                   elst_version=audio_elst_version,
                   elst_entries=audio_elst_entries,
                   ctts_entries=audio_ctts_entries,
                   ctts_version=audio_ctts_version,
                   chunk_offset=(data_offset if audio_offset is None
                                 else audio_offset),
                   co64=audio_co64)
            for _ in range(audio_count))
        moov = _box('moov', video + audio_tracks, extended=moov_extended)
    return (ftyp + moov + mdat) if front else (ftyp + mdat + moov)


def _top_level(data):
    boxes = []
    offset = 0
    while offset < len(data):
        size = struct.unpack_from('>I', data, offset)[0]
        header = 8
        if size == 1:
            size = struct.unpack_from('>Q', data, offset + 8)[0]
            header = 16
        boxes.append((data[offset + 4:offset + 8], offset, size, header))
        offset += size
    return boxes


def _output_moov(data):
    kind, offset, size, _header = next(box for box in _top_level(data)
                                       if box[0] == b'moov')
    parsed = mp4_timing._parse_boxes(data[offset:offset + size])
    return parsed[0]


def _video_stbl(moov):
    trak = next(box for box in moov.children if box.typ == b'trak'
                and any(child.typ == b'mdia' for child in box.children))
    mdia = next(child for child in trak.children if child.typ == b'mdia')
    minf = next(child for child in mdia.children if child.typ == b'minf')
    return next(child for child in minf.children if child.typ == b'stbl')


def _video_elst(moov):
    trak = next(box for box in moov.children if box.typ == b'trak'
                and any(child.typ == b'edts' for child in box.children))
    edts = next(child for child in trak.children if child.typ == b'edts')
    return next(child for child in edts.children if child.typ == b'elst')


def _tracks_with_handler(moov, handler_type):
    tracks = []
    for track in (child for child in moov.children if child.typ == b'trak'):
        mdia = next(child for child in track.children if child.typ == b'mdia')
        hdlr = next(child for child in mdia.children if child.typ == b'hdlr')
        if bytes(hdlr.payload)[8:12] == handler_type:
            tracks.append(track)
    return tracks


def _track_elst(track):
    edts = next(child for child in track.children if child.typ == b'edts')
    return next(child for child in edts.children if child.typ == b'elst')


def _track_stbl(track):
    mdia = next(child for child in track.children if child.typ == b'mdia')
    minf = next(child for child in mdia.children if child.typ == b'minf')
    return next(child for child in minf.children if child.typ == b'stbl')


def _elst_entry_count(elst):
    return struct.unpack_from('>I', bytes(elst.payload), 4)[0]


def _ctts_values(ctts):
    payload = bytes(ctts.payload)
    version = payload[0]
    values = []
    offset = 8
    for _ in range(struct.unpack_from('>I', payload, 4)[0]):
        count = struct.unpack_from('>I', payload, offset)[0]
        value = (struct.unpack_from('>i', payload, offset + 4)[0]
                 if version == 1 else struct.unpack_from('>I', payload, offset + 4)[0])
        values.append((count, value))
        offset += 8
    return version, values


def _chunk_value(stbl, kind):
    box = next(child for child in stbl.children if child.typ == kind)
    payload = bytes(box.payload)
    return (struct.unpack_from('>Q', payload, 8)[0]
            if kind == b'co64' else struct.unpack_from('>I', payload, 8)[0])


def test_zero_empty_edit_preserves_ctts_and_shifts_faststart_offsets(tmp_path):
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'normalized.mp4'
    original = _make_mp4(front=True, media_time=1000)
    source.write_bytes(original)

    original_moov = _output_moov(original)
    original_elst = bytes(_video_elst(original_moov).payload)
    original_stts = bytes(next(child for child in _video_stbl(original_moov).children
                               if child.typ == b'stts').payload)
    assert normalize_mp4_presentation(source, destination)
    output = destination.read_bytes()
    moov = _output_moov(output)
    elst = _video_elst(moov)
    payload = bytes(elst.payload)
    assert payload[:4] == original_elst[:4]
    assert struct.unpack_from('>I', payload, 4)[0] == 2
    assert payload[8:20] == struct.pack('>Ii hH', 0, -1, 1, 0)
    assert payload[20:] == original_elst[8:]
    stbl = _video_stbl(moov)
    assert not any(child.typ == b'ctts' for child in stbl.children)
    assert bytes(next(child for child in stbl.children if child.typ == b'stts').payload) == \
        original_stts
    old_mdat = next(box for box in _top_level(original) if box[0] == b'mdat')
    new_mdat = next(box for box in _top_level(output) if box[0] == b'mdat')
    assert original[old_mdat[1] + old_mdat[3]:old_mdat[1] + old_mdat[2]] == \
        output[new_mdat[1] + new_mdat[3]:new_mdat[1] + new_mdat[2]]
    delta = new_mdat[1] - old_mdat[1]
    assert _chunk_value(stbl, b'stco') == old_mdat[1] + old_mdat[3] + delta
    assert needs_mp4_presentation_normalization(source)
    assert not needs_mp4_presentation_normalization(destination)


def test_existing_ctts_v0_and_v1_remain_byte_identical(tmp_path):
    for version, entries in (
            (0, [(2, 1400)]),
            (1, [(1, -200), (1, 1200)])):
        source = tmp_path / f'source-{version}.mp4'
        destination = tmp_path / f'normalized-{version}.mp4'
        original = _make_mp4(ctts_entries=entries, ctts_version=version,
                             media_time=1000)
        source.write_bytes(original)
        original_ctts = next(child for child in _video_stbl(_output_moov(original)).children
                             if child.typ == b'ctts')

        assert normalize_mp4_presentation(source, destination)
        moov = _output_moov(destination.read_bytes())
        ctts = next(child for child in _video_stbl(moov).children
                    if child.typ == b'ctts')
        assert bytes(ctts.payload) == bytes(original_ctts.payload)
        assert _ctts_values(ctts) == (version, entries)
        assert struct.unpack_from('>I', bytes(_video_elst(moov).payload), 4)[0] == 2


def test_tail_moov_does_not_shift_chunk_offsets_and_extended_moov_is_supported(tmp_path):
    source = tmp_path / 'tail.mp4'
    destination = tmp_path / 'tail-normalized.mp4'
    original = _make_mp4(front=False, moov_extended=True, media_time=1000)
    source.write_bytes(original)
    old_mdat = next(box for box in _top_level(original) if box[0] == b'mdat')

    assert normalize_mp4_presentation(source, destination)
    output = destination.read_bytes()
    new_mdat = next(box for box in _top_level(output) if box[0] == b'mdat')
    assert new_mdat[1] == old_mdat[1]
    assert output[new_mdat[1]:new_mdat[1] + new_mdat[2]] == \
        original[old_mdat[1]:old_mdat[1] + old_mdat[2]]
    moov = _output_moov(output)
    moov_top = next(box for box in _top_level(output) if box[0] == b'moov')
    assert moov_top[3] == 16
    assert not any(child.typ == b'ctts' for child in _video_stbl(moov).children)
    assert _chunk_value(_video_stbl(moov), b'stco') == \
        _chunk_value(_video_stbl(_output_moov(original)), b'stco')
    assert struct.unpack_from('>I', bytes(_video_elst(moov).payload), 4)[0] == 2


def test_audio_stco_and_co64_shift_with_faststart_moov(tmp_path):
    source = tmp_path / 'audio-offsets.mp4'
    destination = tmp_path / 'audio-offsets-normalized.mp4'
    original = _make_mp4(front=True, media_time=1000, audio_co64=True)
    source.write_bytes(original)
    old_mdat = next(box for box in _top_level(original) if box[0] == b'mdat')

    assert normalize_mp4_presentation(source, destination)
    output = destination.read_bytes()
    moov = _output_moov(output)
    audio_trak = next(trak for trak in moov.children if trak.typ == b'trak'
                      and not any(child.typ == b'edts' for child in trak.children))
    mdia = next(child for child in audio_trak.children if child.typ == b'mdia')
    minf = next(child for child in mdia.children if child.typ == b'minf')
    stbl = next(child for child in minf.children if child.typ == b'stbl')
    new_mdat = next(box for box in _top_level(output) if box[0] == b'mdat')
    assert _chunk_value(stbl, b'co64') == old_mdat[1] + old_mdat[3] + (new_mdat[1] - old_mdat[1])


def test_positive_audio_edit_is_prepended_and_audio_tables_stay_byte_identical(tmp_path):
    source = tmp_path / 'audio-positive.mp4'
    destination = tmp_path / 'audio-positive-normalized.mp4'
    original = _make_mp4(
        media_time=1000,
        ctts_entries=[(2, 1400)],
        audio_media_time=200,
        audio_elst_version=1,
        audio_ctts_entries=[(2, -55)],
        audio_ctts_version=1,
    )
    source.write_bytes(original)
    original_moov = _output_moov(original)
    original_video_elst = bytes(_video_elst(original_moov).payload)
    original_audio = _tracks_with_handler(original_moov, b'soun')[0]
    original_audio_elst = bytes(_track_elst(original_audio).payload)
    original_video_stbl = _video_stbl(original_moov)
    original_audio_stbl = _track_stbl(original_audio)
    original_tables = [
        bytes(next(child for child in original_video_stbl.children
                   if child.typ == kind).payload)
        for kind in (b'stts', b'ctts')
    ] + [
        bytes(next(child for child in original_audio_stbl.children
                   if child.typ == kind).payload)
        for kind in (b'stts', b'ctts')
    ]

    assert normalize_mp4_presentation(source, destination)
    output_moov = _output_moov(destination.read_bytes())
    output_video_elst = bytes(_video_elst(output_moov).payload)
    output_audio_elst = bytes(_track_elst(
        _tracks_with_handler(output_moov, b'soun')[0]).payload)
    assert _elst_entry_count(_video_elst(output_moov)) == 2
    assert _elst_entry_count(_track_elst(
        _tracks_with_handler(output_moov, b'soun')[0])) == 2
    assert output_video_elst[20:] == original_video_elst[8:]
    assert output_audio_elst[8:28] == struct.pack('>Qq hH', 0, -1, 1, 0)
    assert output_audio_elst[28:] == original_audio_elst[8:]
    output_video_stbl = _video_stbl(output_moov)
    output_audio_stbl = _track_stbl(_tracks_with_handler(output_moov, b'soun')[0])
    output_tables = [
        bytes(next(child for child in output_video_stbl.children
                   if child.typ == kind).payload)
        for kind in (b'stts', b'ctts')
    ] + [
        bytes(next(child for child in output_audio_stbl.children
                   if child.typ == kind).payload)
        for kind in (b'stts', b'ctts')
    ]
    assert output_tables == original_tables


@pytest.mark.parametrize('video_kwargs', [
    {'media_time': None},
    {'media_time': 0},
    {'media_time': None,
     'video_elst_entries': [(0, -1, 1, 0), (10000, 1000, 1, 0)]},
])
def test_audio_edit_is_eligible_when_video_is_missing_zero_or_already_normalized(
        tmp_path, video_kwargs):
    source = tmp_path / f"audio-only-{len(video_kwargs)}.mp4"
    destination = tmp_path / f"audio-only-{len(video_kwargs)}-out.mp4"
    source.write_bytes(_make_mp4(audio_media_time=200, **video_kwargs))

    assert normalize_mp4_presentation(source, destination)
    output_moov = _output_moov(destination.read_bytes())
    audio = _tracks_with_handler(output_moov, b'soun')[0]
    assert _elst_entry_count(_track_elst(audio)) == 2
    video_tracks = _tracks_with_handler(output_moov, b'vide')
    if video_kwargs.get('video_elst_entries'):
        assert bytes(_track_elst(video_tracks[0]).payload) == _elst_entries(
            video_kwargs['video_elst_entries'])[8:]
    elif video_kwargs['media_time'] is None:
        assert not any(child.typ == b'edts' for child in video_tracks[0].children)
    else:
        assert bytes(_track_elst(video_tracks[0]).payload) == _elst(0)[8:]


def test_multiple_audio_tracks_grow_moov_and_shift_all_chunk_offsets(tmp_path):
    source = tmp_path / 'multiple-audio.mp4'
    destination = tmp_path / 'multiple-audio-normalized.mp4'
    original = _make_mp4(
        audio_media_time=200,
        audio_count=2,
        audio_co64=True,
        audio_ctts_entries=[(2, 12)],
    )
    source.write_bytes(original)
    old_top = _top_level(original)
    old_mdat = next(box for box in old_top if box[0] == b'mdat')

    assert normalize_mp4_presentation(source, destination)
    output = destination.read_bytes()
    new_top = _top_level(output)
    new_mdat = next(box for box in new_top if box[0] == b'mdat')
    output_moov = _output_moov(output)
    delta = new_mdat[1] - old_mdat[1]
    assert len(_tracks_with_handler(output_moov, b'soun')) == 2
    for audio in _tracks_with_handler(output_moov, b'soun'):
        assert _elst_entry_count(_track_elst(audio)) == 2
        assert _chunk_value(_track_stbl(audio), b'co64') == \
            old_mdat[1] + old_mdat[3] + delta


def test_complex_audio_edit_list_remains_byte_identical_and_does_not_block_video(
        tmp_path):
    source = tmp_path / 'late-audio.mp4'
    destination = tmp_path / 'late-audio-normalized.mp4'
    original = _make_mp4(
        media_time=1000,
        audio_elst_entries=[(1000, -1, 1, 0), (10000, 200, 1, 0)],
    )
    source.write_bytes(original)
    original_audio_elst = bytes(_track_elst(
        _tracks_with_handler(_output_moov(original), b'soun')[0]).payload)

    assert normalize_mp4_presentation(source, destination)
    output_moov = _output_moov(destination.read_bytes())
    output_audio = _tracks_with_handler(output_moov, b'soun')[0]
    assert bytes(_track_elst(output_audio).payload) == original_audio_elst

    second = tmp_path / 'late-audio-second.mp4'
    assert normalize_mp4_presentation(destination, second) is False
    assert not second.exists()


def test_moov_between_mdats_shifts_only_offsets_after_the_original_moov(tmp_path):
    ftyp = _box('ftyp', b'isom\0\0\0\0')
    before = _box('mdat', b'before-moov')
    before_payload = len(ftyp) + 8
    moov_offset = len(ftyp) + len(before)
    placeholder_video = _track('vide', media_time=1000,
                               chunk_offset=0)
    placeholder_audio = _track('soun', chunk_offset=before_payload,
                               co64=True)
    placeholder_moov = _box('moov', placeholder_video + placeholder_audio)
    after = _box('mdat', b'after-moov')
    after_payload = moov_offset + len(placeholder_moov) + 8
    video = _track('vide', media_time=1000, chunk_offset=after_payload)
    audio = _track('soun', chunk_offset=before_payload, co64=True)
    moov = _box('moov', video + audio)
    original = ftyp + before + moov + after
    source = tmp_path / 'middle.mp4'
    destination = tmp_path / 'middle-normalized.mp4'
    source.write_bytes(original)

    assert normalize_mp4_presentation(source, destination)
    output = destination.read_bytes()
    old_top = _top_level(original)
    new_top = _top_level(output)
    old_moov = next(box for box in old_top if box[0] == b'moov')
    new_moov = next(box for box in new_top if box[0] == b'moov')
    delta = new_moov[2] - old_moov[2]
    old_mdat_boxes = [box for box in old_top if box[0] == b'mdat']
    new_mdat_boxes = [box for box in new_top if box[0] == b'mdat']
    assert [box[1] for box in new_mdat_boxes] == [
        old_mdat_boxes[0][1], old_mdat_boxes[1][1] + delta]
    for old_box, new_box in zip(old_mdat_boxes, new_mdat_boxes, strict=True):
        assert original[old_box[1] + old_box[3]:old_box[1] + old_box[2]] == \
            output[new_box[1] + new_box[3]:new_box[1] + new_box[2]]

    output_moov = _output_moov(output)
    tracks = [track for track in output_moov.children if track.typ == b'trak']
    video_stbl = _video_stbl(output_moov)
    audio_mdia = next(child for child in tracks[1].children if child.typ == b'mdia')
    audio_minf = next(child for child in audio_mdia.children if child.typ == b'minf')
    audio_stbl = next(child for child in audio_minf.children if child.typ == b'stbl')
    assert _chunk_value(video_stbl, b'stco') == after_payload + delta
    assert _chunk_value(audio_stbl, b'co64') == before_payload


@pytest.mark.parametrize('bad_source', [
    b'\0\0\0\0free',
    b'\0\0\0\x10moov\0\0',
])
def test_malformed_or_size_zero_input_returns_false_without_output(tmp_path, bad_source):
    source = tmp_path / 'bad.mp4'
    destination = tmp_path / 'bad-normalized.mp4'
    source.write_bytes(bad_source)

    assert normalize_mp4_presentation(source, destination) is False
    assert not destination.exists()


def test_existing_destination_and_same_path_are_rejected(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(_make_mp4())
    existing = tmp_path / 'existing.mp4'
    existing.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        normalize_mp4_presentation(source, existing)
    with pytest.raises(ValueError):
        normalize_mp4_presentation(source, source)


def test_no_edit_list_change_and_unsupported_edit_rates_return_false(tmp_path):
    no_edit = tmp_path / 'no-edit.mp4'
    no_edit_out = tmp_path / 'no-edit-out.mp4'
    no_edit.write_bytes(_make_mp4(media_time=0))
    assert normalize_mp4_presentation(no_edit, no_edit_out) is False
    assert needs_mp4_presentation_normalization(no_edit) is False
    assert not no_edit_out.exists()

    rate = tmp_path / 'rate.mp4'
    rate_out = tmp_path / 'rate-out.mp4'
    data = _make_mp4(media_time=1000)
    # The helper below constructs the supported shape, so replace its 1.0
    # rate integer in the video ELST payload with 2.
    marker = struct.pack('>hH', 1, 0)
    rate.write_bytes(data.replace(marker, struct.pack('>hH', 2, 0), 1))
    assert normalize_mp4_presentation(rate, rate_out) is False
    assert not rate_out.exists()


def test_version1_edit_list_preserves_original_entry_and_is_idempotent(tmp_path):
    source = tmp_path / 'v1.mp4'
    destination = tmp_path / 'v1-normalized.mp4'
    second_destination = tmp_path / 'v1-normalized-again.mp4'
    original = _make_mp4(elst_version=1, media_time=1000)
    source.write_bytes(original)
    original_elst = bytes(_video_elst(_output_moov(original)).payload)

    assert normalize_mp4_presentation(source, destination)
    output_elst = bytes(_video_elst(_output_moov(destination.read_bytes())).payload)
    assert output_elst[:4] == original_elst[:4]
    assert struct.unpack_from('>I', output_elst, 4)[0] == 2
    assert output_elst[8:28] == struct.pack('>Qq hH', 0, -1, 1, 0)
    assert output_elst[28:] == original_elst[8:]

    assert normalize_mp4_presentation(destination, second_destination) is False
    assert not second_destination.exists()
    assert needs_mp4_presentation_normalization(destination) is False


def test_in_place_normalization_replaces_only_after_successful_copy(tmp_path):
    source = tmp_path / 'unpublished.mp4'
    original = _make_mp4(media_time=1000)
    source.write_bytes(original)

    assert normalize_mp4_presentation_in_place(source)
    normalized = source.read_bytes()
    assert normalized != original
    assert needs_mp4_presentation_normalization(source) is False
    assert _video_elst(_output_moov(normalized)).payload[4:8] == \
        struct.pack('>I', 2)

    no_edit = tmp_path / 'no-edit-in-place.mp4'
    no_edit_original = _make_mp4(media_time=0)
    no_edit.write_bytes(no_edit_original)
    assert normalize_mp4_presentation_in_place(no_edit) is False
    assert no_edit.read_bytes() == no_edit_original

    cancelled = threading.Event()
    cancelled.set()
    cancelled_source = tmp_path / 'cancelled-in-place.mp4'
    cancelled_original = _make_mp4(media_time=1000)
    cancelled_source.write_bytes(cancelled_original)
    assert normalize_mp4_presentation_in_place(
        cancelled_source, cancelled) is False
    assert cancelled_source.read_bytes() == cancelled_original


def test_in_place_normalization_can_commit_through_owned_callback(tmp_path):
    source = tmp_path / ('.game_clip_from_19Nov2026_21-56-15_export_1762a3e0f1ad.partial.'
                         + 'a' * 32 + '.mp4')
    original = _make_mp4(media_time=1000)
    source.write_bytes(original)
    calls = []

    def commit_replace(temp_path, destination):
        assert temp_path.parent.parent == source.parent
        assert len(str(temp_path)) < len(str(source))
        calls.append((temp_path, destination))
        os.replace(temp_path, destination)
        return {'committed': True}

    assert normalize_mp4_presentation_in_place(
        source, commit_replace=commit_replace)
    assert calls and calls[0][1] == source
    assert source.read_bytes() != original
    assert list(tmp_path.iterdir()) == [source]


def test_missing_sample_entries_and_malformed_data_references_are_rejected(tmp_path):
    no_entries = tmp_path / 'no-entries.mp4'
    no_entries_out = tmp_path / 'no-entries-out.mp4'
    no_entries.write_bytes(_make_mp4(video_stsd_payload=b'\0' * 8,
                                     video_include_dref=False))
    assert normalize_mp4_presentation(no_entries, no_entries_out) is False
    assert not no_entries_out.exists()

    empty_dref = _box('dinf', _box('dref', b'\0' * 4 + struct.pack('>I', 0)))
    bad_dref = tmp_path / 'bad-dref.mp4'
    bad_dref_out = tmp_path / 'bad-dref-out.mp4'
    bad_dref.write_bytes(_make_mp4(video_minf_extra=empty_dref,
                                   video_include_dref=False))
    assert normalize_mp4_presentation(bad_dref, bad_dref_out) is False
    assert not bad_dref_out.exists()

    external_dref = _box(
        'dinf',
        _box('dref', b'\0' * 4 + struct.pack('>I', 1)
             + _box('url ', b'\0' * 4)),
    )
    external = tmp_path / 'external-dref.mp4'
    external_out = tmp_path / 'external-dref-out.mp4'
    external.write_bytes(_make_mp4(video_minf_extra=external_dref,
                                   video_include_dref=False))
    assert normalize_mp4_presentation(external, external_out) is False
    assert not external_out.exists()

    missing_dref = tmp_path / 'missing-dref.mp4'
    missing_dref_out = tmp_path / 'missing-dref-out.mp4'
    missing_dref.write_bytes(_make_mp4(video_include_dref=False))
    assert normalize_mp4_presentation(missing_dref, missing_dref_out) is False
    assert not missing_dref_out.exists()

    wrong_index_stsd = (b'\0' * 4 + struct.pack('>I', 1)
                        + _box('avc1', b'\0' * 6 + b'\0\2'))
    wrong_index = tmp_path / 'wrong-index.mp4'
    wrong_index_out = tmp_path / 'wrong-index-out.mp4'
    wrong_index.write_bytes(_make_mp4(video_stsd_payload=wrong_index_stsd))
    assert normalize_mp4_presentation(wrong_index, wrong_index_out) is False
    assert not wrong_index_out.exists()


def test_box_nesting_and_box_count_limits_are_enforced(tmp_path):
    nested = b'leaf'
    for _ in range(mp4_timing._MAX_BOX_DEPTH + 1):
        nested = _box('trak', nested)
    deep_source = tmp_path / 'deep.mp4'
    deep_output = tmp_path / 'deep-out.mp4'
    deep_source.write_bytes(_box('ftyp', b'isom') + _box('moov', nested)
                            + _box('mdat', b'data'))
    assert normalize_mp4_presentation(deep_source, deep_output) is False
    assert not deep_output.exists()

    many_boxes = b''.join(_box('free') for _ in
                          range(mp4_timing._MAX_BOX_COUNT + 1))
    count_source = tmp_path / 'many-boxes.mp4'
    count_output = tmp_path / 'many-boxes-out.mp4'
    count_source.write_bytes(_box('ftyp', b'isom') + _box('moov', many_boxes)
                             + _box('mdat', b'data'))
    assert normalize_mp4_presentation(count_source, count_output) is False
    assert not count_output.exists()


def test_cancellation_during_top_level_and_moov_scans_removes_output(
        tmp_path, monkeypatch):
    source = tmp_path / 'scan-cancel.mp4'
    source.write_bytes(_box('free', b'prefix') + _make_mp4())

    cancelled_top = threading.Event()
    original_header = mp4_timing._read_file_header

    def cancel_after_header(*args, **kwargs):
        result = original_header(*args, **kwargs)
        cancelled_top.set()
        return result

    monkeypatch.setattr(mp4_timing, '_read_file_header', cancel_after_header)
    top_output = tmp_path / 'top-cancel-out.mp4'
    assert normalize_mp4_presentation(source, top_output, cancelled_top) is False
    assert not top_output.exists()

    monkeypatch.setattr(mp4_timing, '_read_file_header', original_header)
    cancelled_moov = threading.Event()
    original_parse_header = mp4_timing._parse_box_header

    def cancel_after_parse(*args, **kwargs):
        result = original_parse_header(*args, **kwargs)
        cancelled_moov.set()
        return result

    monkeypatch.setattr(mp4_timing, '_parse_box_header', cancel_after_parse)
    moov_output = tmp_path / 'moov-cancel-out.mp4'
    assert normalize_mp4_presentation(source, moov_output, cancelled_moov) is False
    assert not moov_output.exists()


def test_ctts_sample_mismatch_is_rejected_but_signed_bounds_are_unchanged(tmp_path):
    mismatch = tmp_path / 'bad-ctts.mp4'
    mismatch_out = tmp_path / 'bad-ctts-out.mp4'
    mismatch.write_bytes(_make_mp4(ctts_entries=[(1, 1000)], media_time=1000))
    assert normalize_mp4_presentation(mismatch, mismatch_out) is False
    assert not mismatch_out.exists()

    for version, entries in (
            (0, [(1, 0xFFFFFFFF), (1, 0)]),
            (1, [(1, -2147483648), (1, 2147483647)])):
        source = tmp_path / f'valid-ctts-{version}.mp4'
        destination = tmp_path / f'valid-ctts-{version}-out.mp4'
        original = _make_mp4(ctts_entries=entries, ctts_version=version,
                             media_time=1000)
        source.write_bytes(original)
        assert normalize_mp4_presentation(source, destination)
        output_ctts = next(child for child in
                           _video_stbl(_output_moov(destination.read_bytes())).children
                           if child.typ == b'ctts')
        assert _ctts_values(output_ctts) == (version, entries)


def test_stco_overflow_and_fragmented_input_are_rejected(tmp_path):
    source = tmp_path / 'overflow.mp4'
    output = tmp_path / 'overflow-out.mp4'
    source.write_bytes(_make_mp4(video_offset=0xFFFFFFFF,
                                 audio_offset=0xFFFFFFFF))
    assert normalize_mp4_presentation(source, output) is False
    assert needs_mp4_presentation_normalization(source) is False
    assert not output.exists()

    fragmented = tmp_path / 'fragmented.mp4'
    fragmented_out = tmp_path / 'fragmented-out.mp4'
    fragmented.write_bytes(_box('moof', b'x') + _make_mp4())
    assert normalize_mp4_presentation(fragmented, fragmented_out) is False
    assert not fragmented_out.exists()


def test_cancelled_copy_and_replaced_source_remove_partial_output(tmp_path, monkeypatch):
    source = tmp_path / 'source.mp4'
    destination = tmp_path / 'cancelled.mp4'
    source.write_bytes(_make_mp4(mdat_payload=b'x' * (2 * 1024 * 1024)))
    cancelled = threading.Event()
    original_copy = mp4_timing._copy_range
    calls = 0

    def cancel_after_first_copy(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original_copy(*args, **kwargs)
        if calls >= 2:
            cancelled.set()
        return result

    monkeypatch.setattr(mp4_timing, '_copy_range', cancel_after_first_copy)
    assert normalize_mp4_presentation(source, destination, cancelled) is False
    assert not destination.exists()

    replacement_output = tmp_path / 'replaced-out.mp4'
    calls = 0
    monkeypatch.setattr(mp4_timing, '_copy_range', original_copy)

    def replaced_after_copy(*args, **kwargs):
        nonlocal calls
        calls += 1
        return calls < 2

    monkeypatch.setattr(mp4_timing, '_same_source_identity', replaced_after_copy)
    assert normalize_mp4_presentation(source, replacement_output) is False
    assert not replacement_output.exists()
