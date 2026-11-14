"""AUDIT-028 completed-clip filtering and crash-recovery tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.clip_files import (
    cleanup_stale_partial_clips,
    is_completed_video_path,
    is_fthr_temporary_dir,
    is_fthr_owned_orphan_audio_manifest_path,
    is_fthr_owned_partial_path,
    is_library_media_path,
    is_partial_clip_path,
)


@pytest.mark.parametrize('path', ['foo.mp4', 'foo.MP4', 'foo.mkv', 'foo.avi'])
def test_completed_video_names_are_accepted(path: str) -> None:
    assert is_completed_video_path(path)


@pytest.mark.parametrize(
    'path',
    [
        'foo.mp4.partial',
        'foo.MP4.PARTIAL',
        'foo.partial',
        'foo.mp4.tmp',
        '.clip.partial.token.mp4',
    ],
)
def test_partial_or_incomplete_names_are_rejected(path: str) -> None:
    assert not is_completed_video_path(path)
    assert not is_library_media_path(path)


def test_partial_check_is_explicit_not_an_extension_accident() -> None:
    partial = Path('desktop_clip_from_11Aug2026_12-00-00.mp4.partial')
    assert is_partial_clip_path(partial)
    assert is_fthr_owned_partial_path(partial)
    assert not is_completed_video_path(partial)


def test_library_accepts_completed_video_and_image_names() -> None:
    assert is_library_media_path('clip.mp4')
    assert is_library_media_path('screenshot.png')


def test_fthr_post_processing_directories_are_identifiable() -> None:
    assert is_fthr_temporary_dir('.fthr-finalize-123')
    assert not is_fthr_temporary_dir('recordings')


def test_startup_cleanup_removes_only_old_fthr_partials(tmp_path: Path) -> None:
    old = tmp_path / 'Desktop' / 'desktop_clip_from_11Aug2026_12-00-00.mp4.partial'
    fresh = tmp_path / 'Desktop' / 'desktop_clip_from_11Aug2026_12-01-00.mp4.partial'
    unrelated = tmp_path / 'Desktop' / 'manual-recording.mp4.partial'
    old.parent.mkdir()
    for path in (old, fresh, unrelated):
        path.write_bytes(b'partial')

    now = 2_000_000.0
    os.utime(old, (now - 90_000, now - 90_000))
    os.utime(fresh, (now - 60, now - 60))
    os.utime(unrelated, (now - 90_000, now - 90_000))

    result = cleanup_stale_partial_clips(tmp_path, now=now)

    assert result.removed == (old,)
    assert result.failures == ()
    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()


def test_cleanup_failure_is_secondary_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial = tmp_path / 'game_clip_from_11Aug2026_12-00-00.mp4.partial'
    partial.write_bytes(b'partial')
    now = 2_000_000.0
    os.utime(partial, (now - 90_000, now - 90_000))

    original_unlink = Path.unlink

    def fail_target(path: Path, *args: object, **kwargs: object) -> None:
        if path == partial:
            raise PermissionError('still locked')
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'unlink', fail_target)
    result = cleanup_stale_partial_clips(tmp_path, now=now)

    assert result.removed == ()
    assert result.failures[0][0] == partial
    assert 'still locked' in result.failures[0][1]
    assert partial.exists()


def test_startup_cleanup_removes_only_old_orphaned_fthr_audio_manifests(tmp_path: Path) -> None:
    from core.clip_metadata_manager import ClipMetadataManager

    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    old_orphan = (
        tmp_path / 'Desktop' / 'desktop_clip_from_11Aug2026_12-00-00.mp4.fthr-audio.json'
    )
    paired_manifest = (
        tmp_path / 'Desktop' / 'desktop_clip_from_11Aug2026_12-01-00.mp4.fthr-audio.json'
    )
    paired_media = tmp_path / 'Desktop' / 'desktop_clip_from_11Aug2026_12-01-00.mp4'
    unrelated = tmp_path / 'Desktop' / 'manual-recording.mp4.fthr-audio.json'
    old_orphan.parent.mkdir()
    for path in (old_orphan, paired_manifest, paired_media, unrelated):
        path.write_bytes(b'fixture')
    now = 2_000_000.0
    for path in (old_orphan, paired_manifest, paired_media, unrelated):
        os.utime(path, (now - 90_000, now - 90_000))

    assert is_fthr_owned_orphan_audio_manifest_path(old_orphan)
    assert not is_fthr_owned_orphan_audio_manifest_path(unrelated)
    result = cleanup_stale_partial_clips(tmp_path, now=now, metadata_manager=manager)

    assert result.removed == (old_orphan,)
    assert not old_orphan.exists()
    assert paired_manifest.exists()
    assert unrelated.exists()


def test_negative_cleanup_age_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match='non-negative'):
        cleanup_stale_partial_clips(tmp_path, minimum_age_seconds=-1)


@pytest.mark.parametrize('protection', ['favorite', 'protected', 'unprotected'])
def test_orphan_manifest_cleanup_reads_fresh_primary_flags_even_without_sidecar_binding(
        tmp_path, protection):
    from core.clip_metadata_manager import ClipMetadataManager

    media = tmp_path / 'desktop_clip_from_11Aug2026_12-00-00.mp4'
    media.write_bytes(b'synthetic media')
    manifest = media.with_name(media.name + '.fthr-audio.json')
    manifest.write_bytes(b'synthetic manifest retained byte for byte')
    database = tmp_path / 'metadata.sqlite3'
    stale = ClipMetadataManager(database_path=database)
    stale.migrate()
    binding = stale.register_media(stale.inspect_media(media))
    fresh = ClipMetadataManager(database_path=database)
    if protection != 'unprotected':
        fresh.set_library_flags(media, **{protection: True})
    # This is precisely the unassociated JSON window: no SIDECAR registration.
    assert len(fresh.files_for_clip(binding.clip_id)) == 1
    media.rename(tmp_path / 'retained-primary.mp4')
    now = 2_000_000.0
    os.utime(manifest, (now - 90_000, now - 90_000))
    result = cleanup_stale_partial_clips(tmp_path, now=now, metadata_manager=stale)
    if protection == 'unprotected':
        assert result.removed == (manifest,) and not manifest.exists()
    else:
        assert result.removed == ()
        assert manifest.read_bytes() == b'synthetic manifest retained byte for byte'
        assert result.failures
    assert (tmp_path / 'retained-primary.mp4').read_bytes() == b'synthetic media'


@pytest.mark.parametrize('context', ['none', 'corrupt', 'future'])
def test_orphan_manifest_is_kept_when_protection_context_cannot_be_trusted(tmp_path, context):
    from contextlib import closing
    import sqlite3
    from core.clip_metadata_manager import ClipMetadataManager

    manifest = tmp_path / 'desktop_clip_from_11Aug2026_12-00-00.mp4.fthr-audio.json'
    manifest.write_bytes(b'retained orphan data')
    manager = None
    if context != 'none':
        database = tmp_path / 'metadata.sqlite3'
        manager = ClipMetadataManager(database_path=database)
        manager.migrate()
        if context == 'future':
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute('PRAGMA user_version=99')
        else:
            database.rename(tmp_path / 'retained-valid-metadata.sqlite3')
            database.write_bytes(b'corrupt synthetic database')
    now = 2_000_000.0
    os.utime(manifest, (now - 90_000, now - 90_000))
    result = cleanup_stale_partial_clips(tmp_path, now=now, metadata_manager=manager)
    assert result.removed == ()
    assert manifest.read_bytes() == b'retained orphan data'
    assert result.failures
