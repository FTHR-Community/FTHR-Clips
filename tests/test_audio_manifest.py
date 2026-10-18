import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from core.audio_manifest import (
    AudioManifestError,
    AudioSourceManifestEntry,
    build_manifest,
    manifest_path_for,
    rebind_manifest_after_media_replace,
    read_manifest_for_media,
    validate_manifest,
    verified_audio_tracks_for_export,
    write_manifest_atomic,
)


def _entry(*, stream_index=2, source_type='application', name='VALORANT'):
    return AudioSourceManifestEntry(
        source_uuid=str(uuid4()), stream_index=stream_index,
        source_type=source_type, display_name=name,
        persistent_identity='valorant-win64-shipping', icon_reference='app-icon',
        first_active_100ns=10, last_active_100ns=20,
        sample_rate=48000, channels=2, sample_format='fltp')


def test_manifest_is_bound_to_media_and_keeps_only_portable_semantics(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'finished clip bytes')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[_entry()])

    assert manifest['manifest_version'] == 1
    assert manifest['media_file'] == 'clip.mp4'
    assert 'C:' not in json.dumps(manifest)
    validate_manifest(manifest, media_path=media)


def test_manifest_rejects_private_or_path_like_identity(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'finished clip bytes')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[_entry()])
    manifest['sources'][0]['persistent_identity'] = r'C:\\Users\\Tom\\Game.exe'

    with pytest.raises(AudioManifestError, match='manifest-safe'):
        validate_manifest(manifest, media_path=media)


def test_manifest_detects_media_replacement_and_falls_back_cleanly(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'first version')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()), sources=[_entry()])
    sidecar = write_manifest_atomic(manifest, media)
    assert sidecar == manifest_path_for(media)
    assert read_manifest_for_media(media) == manifest

    media.write_bytes(b'replaced version')
    assert read_manifest_for_media(media) is None


def test_lossless_media_replace_can_rebind_the_existing_source_manifest(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'original multitrack media')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()),
        sources=[_entry(stream_index=1), _entry(stream_index=2, name='Discord')])
    write_manifest_atomic(manifest, media)

    media.write_bytes(b'post-processed media with the same ordered audio streams')
    assert read_manifest_for_media(media) is None
    assert rebind_manifest_after_media_replace(media) is True

    rebound = read_manifest_for_media(media)
    assert rebound is not None
    assert [source['display_name'] for source in rebound['sources']] == [
        'VALORANT', 'Discord']
    assert rebound['media_sha256'] != manifest['media_sha256']


def test_rebind_refuses_invalid_sidecar(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'media')
    sidecar = manifest_path_for(media)
    sidecar.write_text('{"not":"a manifest"}', encoding='utf-8')

    assert rebind_manifest_after_media_replace(media) is False
    assert sidecar.read_text(encoding='utf-8') == '{"not":"a manifest"}'


def test_manifest_rejects_duplicate_streams_and_more_than_fourteen_sources(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'finished clip bytes')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()),
        sources=[_entry(stream_index=2), _entry(stream_index=3, name='Discord')])
    manifest['sources'][1]['stream_index'] = 2
    with pytest.raises(AudioManifestError, match='stream index'):
        validate_manifest(manifest, media_path=media)

    manifest['sources'] = [_entry(stream_index=index + 2, name=f'App {index}')
                           .__dict__ for index in range(14)]
    validate_manifest(manifest)

    manifest['sources'].append(_entry(stream_index=16, name='App 14').__dict__)
    with pytest.raises(AudioManifestError, match='between one and fourteen'):
        validate_manifest(manifest, media_path=media)


def test_verified_export_tracks_use_manifest_order_not_raw_stream_labels(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'finished clip bytes')
    manifest = build_manifest(
        media_path=media, transaction_id=str(uuid4()),
        sources=[_entry(stream_index=3, name='Discord'), _entry(stream_index=1, name='VALORANT')])
    write_manifest_atomic(manifest, media)

    tracks = verified_audio_tracks_for_export(media)

    assert tracks[0][1:] == (0, 'VALORANT')
    assert tracks[1][1:] == (1, 'Discord')
    media.write_bytes(b'replaced')
    assert verified_audio_tracks_for_export(media) == ()
