import sys, os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import json
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
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
    mgr = m.ClipMetadataManager()
    result = mgr.get('/some/clip.mp4')
    assert result == {'tag': '', 'description': ''}


def test_set_and_get_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/a.mp4', tag='Gaming', description='Epic moment')
    assert mgr.get('/clips/a.mp4') == {'tag': 'Gaming', 'description': 'Epic moment'}


def test_set_partial_update_preserves_other_fields(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/b.mp4', tag='FPS', description='First take')
    mgr.set('/clips/b.mp4', tag='RPG')
    assert mgr.get('/clips/b.mp4') == {'tag': 'RPG', 'description': 'First take'}


def test_persists_to_disk(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/c.mp4', tag='Highlight', description='Nice play')

    # Reload to simulate new session
    reload(m)
    mgr2 = m.ClipMetadataManager()
    assert mgr2.get('/clips/c.mp4') == {'tag': 'Highlight', 'description': 'Nice play'}


def test_atomic_write_uses_transaction(tmp_path, monkeypatch):
    """A saved entry must be committed and independently readable."""
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))  # Path.home() on Windows
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
    mgr = m.ClipMetadataManager()
    mgr.set('/clips/d.mp4', tag='Test')

    meta_file = Path(tmp_path) / '.fthr' / 'clip_metadata.sqlite3'
    assert meta_file.exists()
    assert m.ClipMetadataManager().get('/clips/d.mp4')['tag'] == 'Test'


def test_editor_draft_roundtrip_is_bound_to_the_source_file(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
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

    reload(m)
    assert m.ClipMetadataManager().get_editor_draft(str(clip)) == state

    # Replacing a file at the same path must not apply an old clip's edit.
    clip.write_bytes(b'a different replacement clip')
    assert m.ClipMetadataManager().get_editor_draft(str(clip)) is None


def test_clearing_editor_draft_preserves_clip_details(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
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
    from importlib import reload
    import core.clip_metadata_manager as m
    reload(m)
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
    with pytest.raises(TypeError):
        manager.set_editor_draft(str(clip), {'unserializable': object()})
    restored = ClipMetadataManager()
    assert restored.get_editor_draft(str(clip)) == {'value': 1}
    destination = tmp_path / 'renamed.mp4'
    clip.rename(destination)
    restored.rename_path(str(clip), str(destination))
    reopened = ClipMetadataManager()
    assert reopened.get_editor_draft(str(clip)) is None
    assert reopened.get_editor_draft(str(destination)) == {'value': 1}
