from __future__ import annotations

from types import SimpleNamespace

from shiboken6 import isValid

from core.clip_metadata_manager import ClipMetadataManager
from ui import clip_grid
from ui.clip_grid import ClipThumbnail


def test_imported_card_delete_never_calls_remove(
        qapp, tmp_path, monkeypatch):
    source = tmp_path / 'external.mp4'
    source.write_bytes(b'user original')
    manager = ClipMetadataManager(database_path=tmp_path / 'metadata.sqlite3')
    manager.migrate()
    card = ClipThumbnail(str(source), imported=True, metadata_manager=manager)
    removed: list[str] = []
    monkeypatch.setattr('ui.clip_grid.os.remove', removed.append)
    messages = []
    monkeypatch.setattr(clip_grid.FthrMessageDialog, 'information',
                        lambda *args, **kwargs: messages.append(args))

    card._confirm_delete()

    assert removed == []
    assert source.read_bytes() == b'user original'
    assert messages and messages[0][1] == 'Original retained'


def test_grid_uses_normalized_link_provenance(qapp, tmp_path):
    from ui.clip_grid import ClipGrid

    source = tmp_path / 'external.mp4'
    source.write_bytes(b'linked')
    grid = SimpleNamespace(_imported_files={str(source)})

    assert ClipGrid.is_linked_import(
        grid, str(source.parent / '.' / source.name))


def test_finalizing_card_cannot_open_or_delete(qapp, tmp_path, monkeypatch):
    source = tmp_path / 'clip.mp4'
    source.write_bytes(b'base')
    card = ClipThumbnail(str(source), ready=False)
    opened: list[str] = []
    removed: list[str] = []
    card.opened.connect(lambda path, *_args: opened.append(path))
    monkeypatch.setattr('ui.clip_grid.os.remove', removed.append)

    card._on_share_click()
    card._confirm_delete()

    assert opened == []
    assert removed == []
    assert source.exists()


def test_delayed_card_feedback_is_cancelled_with_deleted_card(qtbot):
    card = ClipThumbnail('placeholder.mp4')
    card.show()
    card._show_action_feedback('Copied', duration_ms=50)
    timer = card._feedback_timer
    assert card._action_feedback.isVisible()
    assert timer.isActive()

    with qtbot.waitSignal(card.destroyed):
        card.deleteLater()
    assert not isValid(timer)
