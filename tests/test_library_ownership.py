from __future__ import annotations

import pytest

from core.library_ownership import (
    MediaOwnership,
    add_import_root,
    classify_media_path,
    generic_delete_owned_media,
    remove_import_root,
)


@pytest.mark.parametrize('import_location, expected', [
    ('ancestor', MediaOwnership.FTHR_OWNED),
    ('same', MediaOwnership.LINKED_IMPORTED),
    ('nested', MediaOwnership.LINKED_IMPORTED),
])
def test_overlapping_imports_use_the_more_specific_root(tmp_path, import_location, expected):
    root = tmp_path / 'Videos' / 'FTHR Clips'
    clip = root / 'Desktop' / 'capture.mp4'
    imported = {'ancestor': root.parent, 'same': root, 'nested': clip.parent}[import_location]
    assert classify_media_path(clip, root, [imported]) is expected
    external = root.parent / 'Other App' / 'original.mp4'
    assert classify_media_path(external, root, [root.parent]) is MediaOwnership.LINKED_IMPORTED


def test_nested_import_stays_protected_with_an_ancestor_import(tmp_path):
    root = tmp_path / 'Videos' / 'FTHR Clips'
    imported = root / 'Imported'
    for roots in ([root.parent, imported], [imported, root.parent]):
        assert classify_media_path(imported / 'original.mp4', root, roots) is MediaOwnership.LINKED_IMPORTED


def test_owned_clip_can_be_deleted(tmp_path):
    from core.clip_metadata_manager import ClipMetadataManager

    root = tmp_path / 'FTHR_Clips'
    clip = root / 'Desktop' / 'owned.mp4'
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b'owned')

    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    result = generic_delete_owned_media(clip, root, [], metadata_manager=manager)

    assert result.deleted
    assert result.ownership is MediaOwnership.FTHR_OWNED
    assert not clip.exists()


def test_imported_linked_clip_generic_delete_never_removes_source(tmp_path):
    root = tmp_path / 'FTHR_Clips'
    imported = tmp_path / 'Medal'
    source = imported / 'linked.mp4'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'user original')

    result = generic_delete_owned_media(source, root, [imported])

    assert not result.deleted
    assert result.ownership is MediaOwnership.LINKED_IMPORTED
    assert source.read_bytes() == b'user original'
    assert source == imported / 'linked.mp4'


def test_removing_import_root_only_changes_registration(tmp_path):
    imported = tmp_path / 'Medal'
    source = imported / 'linked.mp4'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'user original')

    roots = remove_import_root([str(imported)], str(imported))

    assert roots == []
    assert source.read_bytes() == b'user original'


def test_import_root_registration_deduplicates_equivalent_paths(tmp_path):
    imported = tmp_path / 'clips'
    imported.mkdir()

    roots = add_import_root([str(imported)], str(imported / '.'))

    assert roots == [str(imported.resolve())]


def test_missing_imported_source_fails_safely(tmp_path):
    root = tmp_path / 'FTHR_Clips'
    imported = tmp_path / 'external'
    missing = imported / 'missing.mp4'

    result = generic_delete_owned_media(missing, root, [imported])

    assert not result.deleted
    assert result.ownership is MediaOwnership.LINKED_IMPORTED
    assert result.reason


def test_symlink_inside_owned_root_cannot_delete_external_target(tmp_path):
    root = tmp_path / 'FTHR_Clips'
    root.mkdir()
    external = tmp_path / 'external.mp4'
    external.write_bytes(b'user original')
    link = root / 'looks-owned.mp4'
    try:
        link.symlink_to(external)
    except OSError:
        # Windows developer machines may not grant symlink creation.
        return

    result = generic_delete_owned_media(link, root, [])

    assert not result.deleted
    assert result.ownership is MediaOwnership.EXTERNAL
    assert external.read_bytes() == b'user original'
