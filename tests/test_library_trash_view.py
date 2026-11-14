"""Trash confirmations preserve literal user-provided text."""
from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QDialog, QWidget

from core.clip_metadata_types import FileOperationState, MutationResult, TrashItem, TrashStatus
from ui.dialogs import FthrMessageDialog
from ui.library_trash import LibraryTrashDialog, LibraryTrashPage


def _item(index, *, status=TrashStatus.TRASHED, error=None):
    return TrashItem(str(index), f'clip-{index}', f'C:/synthetic/library/Camera & {index}.mp4',
                     f'C:/synthetic/library/.fthr-trash/{index}/Camera & {index}.mp4',
                     '2026-10-20T00:00:00+00:00', status, error)


def test_trash_is_an_embedded_shell_page_not_a_top_level_dialog(qtbot):
    shell = QWidget()
    qtbot.addWidget(shell)
    page = LibraryTrashPage(shell, load=lambda done: done((), None),
                            restore=lambda *args, **kwargs: None,
                            purge=lambda *args, **kwargs: None)
    qtbot.addWidget(page)
    assert isinstance(page, QWidget)
    assert not isinstance(page, QDialog)
    assert page.parentWidget() is shell
    assert page.windowType() != Qt.WindowType.Dialog
    assert LibraryTrashDialog is LibraryTrashPage


@pytest.mark.parametrize('name', ['<b>literal clip</b>.mp4', 'Camera & microphone.png'])
def test_trash_confirmation_displays_names_as_literal_plain_text(qtbot, name):
    message = f'Delete permanently: {name}\nThis cannot be undone.'
    dialog = FthrMessageDialog('Delete permanently', message, question=True, plain_text=True)
    qtbot.addWidget(dialog)
    label = next(label for label in dialog.findChildren(QLabel) if label.text() == message)
    assert label.textFormat() == Qt.TextFormat.PlainText
    assert label.text() == message


def test_existing_message_dialog_default_keeps_automatic_text_format(qtbot):
    message = '<b>Existing formatted message</b>'
    dialog = FthrMessageDialog('Existing message', message)
    qtbot.addWidget(dialog)
    label = next(label for label in dialog.findChildren(QLabel) if label.text() == message)
    assert label.textFormat() == Qt.TextFormat.AutoText


def test_trash_view_is_event_driven_plaintext_and_has_no_per_row_widgets(qtbot):
    callbacks = []
    dialog = LibraryTrashDialog(load=callbacks.append, restore=lambda *args, **kwargs: None,
                                 purge=lambda *args, **kwargs: None)
    qtbot.addWidget(dialog)
    dialog.refresh()
    assert len(callbacks) == 1 and dialog.tree.topLevelItemCount() == 0
    items = tuple(_item(index, error='<b>Keep & report</b>') for index in range(1000))
    callbacks[0](items, None)
    assert dialog.tree.topLevelItemCount() == 1000
    assert dialog.tree.uniformRowHeights()
    row = dialog.tree.topLevelItem(0)
    assert row.text(0) == 'Camera & 0.mp4' and row.text(4) == '<b>Keep & report</b>'
    assert all(dialog.tree.itemWidget(row, column) is None for column in range(dialog.tree.columnCount()))
    qtbot.wait(150)
    assert len(callbacks) == 1


def test_stale_async_load_cannot_overwrite_newer_trash_rows(qtbot):
    callbacks = []
    dialog = LibraryTrashDialog(load=callbacks.append, restore=lambda *args, **kwargs: None,
                                 purge=lambda *args, **kwargs: None)
    qtbot.addWidget(dialog)
    dialog.refresh()
    dialog.refresh()
    callbacks[1]((_item(2),), None)
    callbacks[0]((_item(1),), None)
    assert dialog.tree.topLevelItemCount() == 1
    assert dialog.tree.topLevelItem(0).text(0) == 'Camera & 2.mp4'


def test_purging_row_is_visible_but_cannot_be_restored(qtbot):
    pending = _item(1, status=TrashStatus.PURGING, error='A foreign file was retained')
    dialog = LibraryTrashDialog(load=lambda done: done((pending,), None),
                                 restore=lambda *args, **kwargs: None,
                                 purge=lambda *args, **kwargs: None)
    qtbot.addWidget(dialog)
    dialog.refresh()
    dialog.tree.topLevelItem(0).setSelected(True)
    assert not dialog.restore_btn.isEnabled() and not dialog.restore_to_btn.isEnabled()
    assert dialog.purge_btn.isEnabled()
    assert dialog.tree.topLevelItem(0).text(3) == 'Deletion pending'
    assert dialog.tree.topLevelItem(0).text(4) == pending.error


def test_select_all_selects_loaded_rows_and_preserves_batch_guards(qtbot, monkeypatch):
    items = (_item(1), _item(2, status=TrashStatus.PURGING))
    callbacks, requests = [], []
    monkeypatch.setattr(FthrMessageDialog, 'question', lambda *args, **kwargs: True)
    dialog = LibraryTrashDialog(load=callbacks.append, restore=lambda *args, **kwargs: None,
                                 purge=lambda identity, done: requests.append((identity, done)))
    qtbot.addWidget(dialog)
    assert not dialog.select_all_btn.isEnabled()
    dialog.refresh()
    callbacks[-1](items, None)

    dialog.refresh()
    dialog.select_all_btn.click()
    callbacks[-1](items, None)
    assert len(dialog.tree.selectedItems()) == 2
    assert not dialog.restore_btn.isEnabled()
    assert dialog.purge_btn.isEnabled()
    assert not requests

    dialog.purge_btn.click()
    assert len(requests) == 1
    assert not dialog.select_all_btn.isEnabled()


@pytest.mark.parametrize('cleanup_done', [False, True])
def test_purge_counts_completion_only_when_fresh_status_and_cleanup_agree(qtbot, monkeypatch, cleanup_done):
    items = [_item(1)]
    requests = []
    confirmations = []

    def confirm(*args, **kwargs):
        confirmations.append((args, kwargs))
        return True

    monkeypatch.setattr(FthrMessageDialog, 'question', confirm)
    dialog = LibraryTrashDialog(load=lambda done: done(tuple(items), None),
                                 restore=lambda *args, **kwargs: None,
                                 purge=lambda identity, done: requests.append((identity, done)))
    qtbot.addWidget(dialog)
    dialog.refresh()
    dialog.tree.topLevelItem(0).setSelected(True)
    dialog.start_action('purge')
    assert len(requests) == 1 and requests[0][0] == '1'
    assert confirmations[0][0][1] == 'Delete permanently'
    assert confirmations[0][1]['plain_text'] is True
    fresh = _item(1, status=TrashStatus.PURGED if cleanup_done else TrashStatus.PURGING)
    items[:] = [] if cleanup_done else [fresh]
    result = MutationResult('operation', FileOperationState.COMMITTED, fresh.stored_path,
                            fresh.stored_path, None if cleanup_done else 'Foreign member retained')
    requests[0][1](result, fresh, cleanup_done)
    qtbot.waitUntil(lambda: dialog.cancel_btn.isHidden())
    if cleanup_done:
        assert dialog.tree.topLevelItemCount() == 0
        assert '1 permanently deleted' in dialog.status_label.text()
    else:
        assert dialog.tree.topLevelItemCount() == 1
        assert '0 permanently deleted' in dialog.status_label.text()
        assert 'Foreign member retained' in dialog.status_label.text()


def test_cancel_remaining_finishes_current_restore_and_never_starts_next(qtbot, monkeypatch, tmp_path):
    items = [_item(1), _item(2)]
    requests, changed = [], []
    monkeypatch.setattr(FthrMessageDialog, 'question', lambda *args, **kwargs: True)

    def restore(identity, done, *, destination=None):
        requests.append((identity, destination, done))

    dialog = LibraryTrashDialog(load=lambda done: done(tuple(items), None), restore=restore,
                                 purge=lambda *args, **kwargs: None)
    qtbot.addWidget(dialog)
    dialog.library_changed.connect(lambda: changed.append(True))
    dialog.refresh()
    for index in range(2):
        dialog.tree.topLevelItem(index).setSelected(True)
    dialog.start_action('restore', directory=str(tmp_path))
    assert len(requests) == 1
    assert requests[0][1] == str(tmp_path / 'Camera & 1.mp4')
    dialog.cancel_remaining()
    fresh = _item(1, status=TrashStatus.RESTORED)
    items[:] = items[1:]
    requests[0][2](MutationResult('operation', FileOperationState.COMMITTED,
                                 fresh.stored_path, requests[0][1]), fresh, True)
    qtbot.waitUntil(lambda: dialog.cancel_btn.isHidden())
    assert len(requests) == 1 and changed == [True]
    assert 'cancelled' in dialog.status_label.text()
    assert dialog.tree.topLevelItemCount() == 1
