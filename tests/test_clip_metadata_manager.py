import sys, os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
import copy
import sqlite3
import pytest
from pathlib import Path


def test_edited_duration_applies_trim_and_deleted_ranges_and_rejects_stale_files(tmp_path, monkeypatch):
    from core.clip_metadata_manager import ClipMetadataManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'video')
    manager = ClipMetadataManager()
    draft = {'trim_start': .1, 'trim_end': .9,
             'segments': [[0, .3, False], [.3, .6, True], [.6, 1, False]]}
    manager.set_editor_draft(str(clip), draft)
    assert manager.edited_duration(str(clip), 60) == pytest.approx(30)
    clip.write_bytes(b'replaced video')
    assert manager.edited_duration(str(clip), 60) == 60
    draft['segments'] = [[0, .3, False], [.6, 1, False]]
    manager.set_editor_draft(str(clip), draft)
    assert manager.edited_duration(str(clip), 60) == 60


def test_get_returns_defaults_for_unknown_clip(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    import core.clip_metadata_manager as m
    mgr = m.ClipMetadataManager()
    result = mgr.get('/some/clip.mp4')
    assert result == {'tag': '', 'description': ''}


def test_set_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    import core.clip_metadata_manager as m
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/a.mp4', tag='Gaming', description='Epic moment')
    assert mgr.get('/clips/a.mp4') == {'tag': 'Gaming', 'description': 'Epic moment'}


def test_set_partial_update_preserves_other_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    import core.clip_metadata_manager as m
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/b.mp4', tag='FPS', description='First take')
    mgr.set('/clips/b.mp4', tag='RPG')
    assert mgr.get('/clips/b.mp4') == {'tag': 'RPG', 'description': 'First take'}


def test_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    import core.clip_metadata_manager as m
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/c.mp4', tag='Highlight', description='Nice play')

    # A fresh manager reads the committed data as a new session would.
    mgr2 = m.ClipMetadataManager()
    assert mgr2.get('/clips/c.mp4') == {'tag': 'Highlight', 'description': 'Nice play'}


def test_atomic_write_uses_transaction(tmp_path, monkeypatch):
    """A saved entry must be committed and independently readable."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    import core.clip_metadata_manager as m
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/d.mp4', tag='Test')

    meta_file = Path(tmp_path) / '.fthr' / 'clip_metadata.sqlite3'
    assert meta_file.exists()
    assert m.ClipMetadataManager().get('/clips/d.mp4')['tag'] == 'Test'


def test_editor_draft_roundtrip_is_bound_to_the_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    import core.clip_metadata_manager as m
    clip = tmp_path / 'draft.mp4'
    clip.write_bytes(b'original clip')
    state = {
        'crop_rect': [10, 20, 640, 360],
        'stretch_ratio': 1.25,
        'effects': {'contrast': 20},
        'segments': [[0.0, 1.0, False]],
    }

    mgr = m.ClipMetadataManager()
    assert mgr.set_editor_draft(str(clip), state) is True

    assert m.ClipMetadataManager().get_editor_draft(str(clip)) == state

    # Replacing a file at the same path must not apply an old clip's edit.
    clip.write_bytes(b'a different replacement clip')
    assert m.ClipMetadataManager().get_editor_draft(str(clip)) is None


def test_clearing_editor_draft_preserves_clip_details(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    import core.clip_metadata_manager as m
    clip = tmp_path / 'details.mp4'
    clip.write_bytes(b'clip')
    mgr = m.ClipMetadataManager()
    mgr.set(str(clip), tag='Highlight', description='Keep this')
    mgr.set_editor_draft(str(clip), {'segments': []})

    mgr.clear_editor_draft(str(clip))

    assert mgr.get_editor_draft(str(clip)) is None
    assert mgr.get(str(clip)) == {
        'tag': 'Highlight', 'description': 'Keep this'}


def test_editor_draft_storage_is_bounded_without_pruning_clip_details(
        tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    import core.clip_metadata_manager as m
    clip = tmp_path / 'new.mp4'
    clip.write_bytes(b'clip')
    mgr = m.ClipMetadataManager()
    for index in range(m._EDITOR_DRAFT_LIMIT):
        mgr._data[f'old-{index}.mp4'] = {
            'tag': f'Tag {index}',
            'editor_draft': {
                'version': 1,
                'updated_at': index,
                'file': {'mtime_ns': 1, 'size': 1},
                'state': {},
            },
        }

    mgr.set_editor_draft(str(clip), {'segments': []})

    draft_count = sum(
        'editor_draft' in entry for entry in mgr._data.values()
        if isinstance(entry, dict))
    assert draft_count == m._EDITOR_DRAFT_LIMIT
    assert mgr._data['old-0.mp4']['tag'] == 'Tag 0'
    assert 'editor_draft' not in mgr._data['old-0.mp4']


def test_legacy_import_and_incremental_draft_write(tmp_path, monkeypatch):
    import sqlite3
    from core.clip_metadata_manager import ClipMetadataManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    folder = tmp_path / '.fthr'
    folder.mkdir()
    legacy = folder / 'clip_metadata.json'
    legacy.write_text(json.dumps({'old.mp4': {'tag': 'Keep', 'description': 'Legacy'}}))
    backup = legacy.read_bytes()
    manager = ClipMetadataManager()
    clip = tmp_path / 'new.mp4'
    clip.write_bytes(b'media')
    manager.set_editor_draft(str(clip), {'segments': [[0, 1, False]]})
    with sqlite3.connect(manager._database) as db:
        db.execute('CREATE TABLE changes (path TEXT)')
        db.execute('CREATE TRIGGER changed AFTER UPDATE ON entries BEGIN '
                   'INSERT INTO changes VALUES (new.path); END')
    manager.set_editor_draft(str(clip), {'segments': [[0, .5, False]]})
    with sqlite3.connect(manager._database) as db:
        assert db.execute('SELECT path FROM changes').fetchall() == [(str(clip),)]
    restored = ClipMetadataManager()
    assert restored.get('old.mp4')['tag'] == 'Keep'
    assert restored.get_editor_draft(str(clip))['segments'] == [[0, .5, False]]
    assert legacy.read_bytes() == backup


def test_failed_transaction_keeps_previous_draft_and_atomic_rename(tmp_path, monkeypatch):
    from core.clip_metadata_manager import ClipMetadataManager
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'media')
    manager = ClipMetadataManager()
    manager.set_editor_draft(str(clip), {'value': 1})
    before = copy.deepcopy(manager._data)
    with pytest.raises(TypeError):
        manager.set_editor_draft(str(clip), {'unserializable': object()})
    assert manager._data == before
    restored = ClipMetadataManager()
    assert restored.get_editor_draft(str(clip)) == {'value': 1}
    destination = tmp_path / 'renamed.mp4'
    clip.rename(destination)
    restored.rename_path(str(clip), str(destination))
    reopened = ClipMetadataManager()
    assert reopened.get_editor_draft(str(clip)) is None
    assert reopened.get_editor_draft(str(destination)) == {'value': 1}


@pytest.fixture
def isolated_metadata(tmp_path, monkeypatch):
    from core.clip_metadata_manager import ClipMetadataManager

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    clip = tmp_path / 'clip.mp4'
    clip.write_bytes(b'media')
    manager = ClipMetadataManager()
    return manager, clip


def _assert_metadata_unchanged_after_reopen(manager, snapshot):
    from core.clip_metadata_manager import ClipMetadataManager

    assert manager._data == snapshot
    assert ClipMetadataManager()._data == snapshot


@pytest.mark.parametrize('abort', ['ABORT', 'ROLLBACK'])
@pytest.mark.parametrize('operation', [
    'tag', 'description', 'new-entry', 'draft', 'clear-draft',
    'clear-last-draft', 'rename',
])
def test_sqlite_failure_preserves_memory_and_committed_metadata(
        isolated_metadata, operation, abort):
    manager, clip = isolated_metadata
    path = str(clip)
    if operation != 'clear-last-draft':
        manager.set(path, tag='Keep tag', description='Keep description')
    manager.set_editor_draft(path, {'segments': [[0, 1, False]], 'value': 'keep'})
    snapshot = copy.deepcopy(manager._data)
    destination = str(clip.with_name('destination.mp4'))
    event = ('INSERT' if operation == 'new-entry' else
             'DELETE' if operation in {'clear-last-draft', 'rename'} else 'UPDATE')
    with sqlite3.connect(manager._database) as connection:
        connection.execute(
            f'CREATE TRIGGER reject_change BEFORE {event} ON entries '
            f"BEGIN SELECT RAISE({abort}, 'injected metadata failure'); END")

    with pytest.raises(OSError, match='injected metadata failure'):
        if operation == 'tag':
            manager.set(path, tag='Discard tag')
        elif operation == 'description':
            manager.set(path, description='Discard description')
        elif operation == 'new-entry':
            manager.set(destination, tag='Never committed')
        elif operation == 'draft':
            manager.set_editor_draft(path, {'value': 'discard'})
        elif operation in {'clear-draft', 'clear-last-draft'}:
            manager.clear_editor_draft(path)
        else:
            manager.rename_path(path, destination)

    _assert_metadata_unchanged_after_reopen(manager, snapshot)
    assert manager.get_editor_draft(path) == {
        'segments': [[0, 1, False]], 'value': 'keep'}


@pytest.mark.parametrize('retain_details', [False, True])
def test_failed_current_draft_insert_rolls_back_new_draft_and_old_draft_together(
        isolated_metadata, retain_details):
    manager, old_clip = isolated_metadata
    if retain_details:
        manager.set(str(old_clip), tag='Keep tag', description='Keep description')
    manager.set_editor_draft(str(old_clip), {'value': 'old'})
    snapshot = copy.deepcopy(manager._data)
    new_clip = old_clip.with_name('new.mp4')
    new_clip.write_bytes(b'new media')
    with sqlite3.connect(manager._database) as connection:
        connection.execute(
            'CREATE TRIGGER reject_draft_insert BEFORE INSERT ON entries '
            "BEGIN SELECT RAISE(ABORT, 'injected draft insert failure'); END")

    with pytest.raises(OSError, match='injected draft insert failure'):
        manager.set_editor_draft(str(new_clip), {'value': 'new'})

    _assert_metadata_unchanged_after_reopen(manager, snapshot)
    assert manager.get_editor_draft(str(old_clip)) == {'value': 'old'}
    assert manager.get_editor_draft(str(new_clip)) is None


def test_sqlite_write_lock_does_not_publish_uncommitted_memory(
        isolated_metadata, monkeypatch):
    import core.clip_metadata_manager as module

    manager, clip = isolated_metadata
    manager.set(str(clip), tag='Keep', description='Committed')
    manager.set_editor_draft(str(clip), {'value': 'keep'})
    snapshot = copy.deepcopy(manager._data)
    connect = sqlite3.connect

    def bounded_connect(*args, **kwargs):
        kwargs['timeout'] = 0.01
        return connect(*args, **kwargs)

    with connect(manager._database) as blocker:
        blocker.execute('BEGIN IMMEDIATE')
        monkeypatch.setattr(module.sqlite3, 'connect', bounded_connect)
        with pytest.raises(OSError, match='locked'):
            manager.set(str(clip), tag='Never committed', description='Discard')
        assert manager._data == snapshot
        blocker.rollback()

    _assert_metadata_unchanged_after_reopen(manager, snapshot)


def test_metadata_rename_collision_preserves_both_entries(isolated_metadata):
    manager, clip = isolated_metadata
    destination = clip.with_name('existing.mp4')
    destination.write_bytes(b'another media')
    for path, tag in ((clip, 'Source'), (destination, 'Destination')):
        manager.set(str(path), tag=tag, description=tag + ' details')
        manager.set_editor_draft(str(path), {'value': tag})
    snapshot = copy.deepcopy(manager._data)

    with pytest.raises(FileExistsError):
        manager.rename_path(str(clip), str(destination))

    _assert_metadata_unchanged_after_reopen(manager, snapshot)
    assert manager.get_editor_draft(str(clip)) == {'value': 'Source'}
    assert manager.get_editor_draft(str(destination)) == {'value': 'Destination'}


def test_metadata_rename_same_path_and_missing_source_are_noops(isolated_metadata):
    manager, clip = isolated_metadata
    manager.set(str(clip), tag='Keep')
    manager.set_editor_draft(str(clip), {'value': 'keep'})
    snapshot = copy.deepcopy(manager._data)
    with sqlite3.connect(manager._database) as connection:
        connection.execute('CREATE TRIGGER reject_noop BEFORE UPDATE ON entries '
                           "BEGIN SELECT RAISE(ABORT, 'no write expected'); END")

    manager.rename_path(str(clip), str(clip))
    manager.rename_path(str(clip.with_name('missing.mp4')), str(clip))

    _assert_metadata_unchanged_after_reopen(manager, snapshot)


def test_new_draft_rejects_equal_size_replacement_with_restored_mtime(
        isolated_metadata):
    manager, clip = isolated_metadata
    manager.set(str(clip), tag='Keep', description='Keep details')
    manager.set_editor_draft(str(clip), {'segments': [[0, .5, False], [.5, 1, True]]})
    original = clip.stat()
    replacement = clip.with_name('replacement.mp4')
    replacement.write_bytes(b'other')
    # Retain the original too: this models replacement without any deletion.
    clip.rename(clip.with_name('original-retained.mp4'))
    replacement.rename(clip)
    os.utime(clip, ns=(original.st_atime_ns, original.st_mtime_ns))
    current = clip.stat()
    assert current.st_size == original.st_size
    assert current.st_mtime_ns == original.st_mtime_ns
    assert (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino)

    assert manager.get_editor_draft(str(clip)) is None
    assert manager.edited_duration(str(clip), 10.0) == 10.0
    from core.clip_metadata_manager import ClipMetadataManager
    assert ClipMetadataManager().get_editor_draft(str(clip)) is None
    assert manager.get(str(clip)) == {'tag': 'Keep', 'description': 'Keep details'}


def test_legacy_size_and_mtime_draft_remains_readable_after_sqlite_commit(
        tmp_path, monkeypatch):
    from core.clip_metadata_manager import ClipMetadataManager

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    clip = tmp_path / 'legacy.mp4'
    clip.write_bytes(b'legacy media')
    stat = clip.stat()
    state = {'segments': [[0, 1, False]], 'value': 'legacy'}
    directory = tmp_path / '.fthr'
    directory.mkdir()
    legacy = directory / 'clip_metadata.json'
    legacy.write_text(json.dumps({str(clip): {
        'tag': 'Keep', 'editor_draft': {
            'version': 1, 'file': {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns},
            'updated_at': 1, 'state': state,
        },
    }}), encoding='utf-8')
    original = legacy.read_bytes()
    manager = ClipMetadataManager()
    assert manager.get_editor_draft(str(clip)) == state

    manager.set(str(clip), description='Committed without rewriting legacy identity')

    assert ClipMetadataManager().get_editor_draft(str(clip)) == state
    assert legacy.read_bytes() == original
