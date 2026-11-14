"""Selection follows indexed files rather than recycled Qt card instances."""
from __future__ import annotations

import builtins
import os
from dataclasses import replace
from pathlib import Path

import pytest

from core.library_index import LibraryRecord, canonical_media_path, scan_library
from core.library_selection import LibrarySelection


@pytest.fixture
def records(tmp_path):
    return tuple(LibraryRecord(
        path=str(tmp_path / f'{name}.mp4'),
        identity=canonical_media_path(tmp_path / f'{name}.mp4'),
        root_ids=(str(tmp_path),), imported=False, kind='video',
        size=20, mtime_ns=100, st_dev=1, st_ino=index + 10,
    ) for index, name in enumerate('abcde'))


def test_plain_click_replaces_selection_and_control_click_toggles(records):
    selection = LibrarySelection()
    a, b, c, _, _ = records
    selection.select(a, records)
    selection.select(c, records, toggle=True)
    assert selection.selected_records == (a, c)
    selection.select(a, records, toggle=True)
    assert selection.selected_records == (c,)
    selection.select(b, records)
    assert selection.selected_records == (b,)
    assert selection.contains(b) and not selection.contains(c)
    selection.clear()
    assert selection.selected_records == ()


@pytest.mark.parametrize('reverse', [False, True])
def test_shift_replaces_range_and_control_shift_adds_to_it(records, reverse):
    ordered = tuple(reversed(records)) if reverse else records
    selection = LibrarySelection()
    selection.select(ordered[0], ordered)
    selection.select(ordered[4], ordered, toggle=True)
    selection.select(ordered[2], ordered, toggle=True, extend=True)
    assert selection.selected_records == (ordered[0], *ordered[2:])
    selection.select(ordered[3], ordered, extend=True)
    assert selection.selected_records == ordered[3:]


def test_shift_uses_new_sort_order_after_refresh(records):
    selection = LibrarySelection()
    selection.select(records[1], records)
    reordered = (records[4], records[1], records[3], records[0], records[2])
    selection.reconcile(reordered)
    selection.select(records[0], reordered, extend=True)
    assert selection.selected_records == reordered[1:4]


def test_select_all_is_only_the_supplied_filtered_records(records):
    selection = LibrarySelection()
    visible = (records[4], records[1])
    selection.select_all(visible)
    assert selection.selected_records == visible
    assert not selection.contains(records[0])
    selection.select_all(())
    assert selection.selected_records == ()


def test_shift_without_an_anchor_selects_only_the_clicked_record(records):
    selection = LibrarySelection()
    selection.select(records[2], records, extend=True)
    assert selection.selected_records == (records[2],)
    selection.clear()
    selection.select(records[4], records, extend=True, toggle=True)
    assert selection.selected_records == (records[4],)


def test_refresh_replaces_snapshots_and_reorders_without_losing_selection(records):
    selection = LibrarySelection()
    selection.select_all((records[0], records[2]))
    refreshed = tuple(replace(record, imported=True) for record in reversed(records))
    selection.reconcile(refreshed)
    assert selection.selected_records == (refreshed[2], refreshed[4])
    assert all(record.imported for record in selection.selected_records)


def test_refresh_drops_deleted_records_and_same_path_replacement(records):
    selection = LibrarySelection()
    selection.select_all(records[:3])
    replacement = replace(records[1], st_ino=999)
    selection.reconcile((replacement, records[2], records[4]))
    assert selection.selected_records == (records[2],)
    assert not selection.contains(replacement)
    assert not selection.contains(records[1])


@pytest.mark.parametrize('field,value', [
    ('size', 21), ('mtime_ns', 101), ('st_dev', 2), ('st_dev', None), ('st_ino', None),
])
def test_changed_file_evidence_is_not_retained_as_the_old_selection(records, field, value):
    selection = LibrarySelection()
    selection.select(records[0], records)
    changed = replace(records[0], **{field: value})
    selection.reconcile((changed, *records[1:]))
    assert selection.selected_records == ()


def test_rename_retains_selection_only_for_the_same_file(records, tmp_path):
    selection = LibrarySelection()
    selection.select(records[0], records)
    destination = tmp_path / 'renamed.mp4'
    renamed = replace(records[0], path=str(destination), identity=canonical_media_path(destination))
    selection.rename(records[0].path, renamed)
    assert selection.selected_records == (renamed,)
    assert selection.contains(renamed) and not selection.contains(records[0])
    different = replace(renamed, path=str(tmp_path / 'foreign.mp4'), st_ino=1000)
    selection.rename(renamed.path, different)
    assert not selection.contains(different)


def test_legacy_record_can_be_selected_without_inventing_file_identity(records):
    legacy = replace(records[0], st_dev=None, st_ino=None)
    selection = LibrarySelection()
    selection.select(legacy, (legacy,))
    assert selection.selected_records == (legacy,)
    assert legacy.file_identity is None


def test_record_file_identity_is_complete_and_defensive(records):
    record = records[0]
    expected = {'st_dev': 1, 'st_ino': 10, 'size': 20, 'mtime_ns': 100}
    assert record.file_identity == expected
    record.file_identity['size'] = -1
    assert record.file_identity == expected
    assert replace(record, st_dev=None).file_identity is None
    assert replace(record, st_ino=None).file_identity is None


def test_large_filtered_selection_and_reconcile_do_not_read_the_filesystem(records, monkeypatch):
    many = tuple(replace(records[0], path=f'{records[0].path}.{index}.mp4',
                         identity=f'{records[0].identity}.{index}.mp4', st_ino=index + 100)
                 for index in range(1000))
    selection = LibrarySelection()

    def forbidden(*args, **kwargs):
        pytest.fail('Selection must use indexed snapshots without filesystem I/O')

    with monkeypatch.context() as guard:
        guard.setattr(builtins, 'open', forbidden)
        guard.setattr(Path, 'stat', forbidden)
        guard.setattr(os, 'stat', forbidden)
        guard.setattr(os, 'lstat', forbidden)
        selection.select_all(many)
        assert selection.selected_records == many
        selection.reconcile(tuple(reversed(many)))
        assert selection.selected_records == tuple(reversed(many))
        selection.select(many[10], many, toggle=True)
        assert len(selection.selected_records) == 999
        assert not selection.contains(many[10])
        selection.clear()


def test_real_scan_provides_the_current_stat_evidence(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'synthetic indexed file')
    result = scan_library(tmp_path)
    assert len(result.records) == 1
    info = media.stat()
    assert result.records[0].file_identity == {
        'st_dev': info.st_dev, 'st_ino': info.st_ino,
        'size': info.st_size, 'mtime_ns': info.st_mtime_ns,
    }
