"""Event-driven view of recoverable, application-owned library Trash."""

from pathlib import Path
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid

from core.clip_metadata_types import TrashStatus
from ui.dialogs import FthrMessageDialog
from ui.style import (
    Colors,
    Fonts,
    button_primary_qss,
    button_secondary_qss,
    label_body,
    set_theme_style,
)


class LibraryTrashPage(QWidget):
    """Embedded, event-driven view for recoverable library Trash entries.

    The page is owned by the main shell and stays alive while navigation moves
    between pages.  Loads and mutations retain the generation/job guards from
    the old dialog so a page switch can cancel the remaining batch without
    allowing a late callback to repaint a newer view.
    """

    library_changed = Signal()

    def __init__(self, parent=None, *, load, restore, purge):
        super().__init__(parent)
        self.setObjectName("libraryTrashPage")
        self._load, self._restore, self._purge = load, restore, purge
        self._generation = 0
        self._job = None
        self._cancel = False
        self._result_summary = None
        self.body_layout = QVBoxLayout(self)
        self.body_layout.setContentsMargins(28, 24, 28, 24)
        self.body_layout.setSpacing(12)
        heading = QLabel("Library Trash")
        heading.setObjectName("libraryTrashHeading")
        set_theme_style(heading, lambda: label_body(Colors.TEXT, Fonts.SIZE_H2))
        self.body_layout.addWidget(heading)
        hint = QLabel(
            "Owned clips remain recoverable until you choose Delete permanently. Linked originals are retained."
        )
        hint.setWordWrap(True)
        set_theme_style(hint, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        selection_row = QHBoxLayout()
        selection_row.setSpacing(12)
        selection_row.addWidget(hint, 1)
        self.select_all_btn = QPushButton("Select all")
        set_theme_style(self.select_all_btn, button_secondary_qss)
        selection_row.addWidget(self.select_all_btn)
        self.body_layout.addLayout(selection_row)
        self.tree = QTreeWidget(self)
        self.tree.setObjectName("libraryTrashTree")
        self.tree.setAccessibleName("Recoverable library clips")
        self.tree.setHeaderLabels(
            ["Name", "Original location", "Moved to Trash", "Status", "Error"]
        )
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumHeight(320)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(60)
        header.setStretchLastSection(True)
        for column, width in enumerate((240, 280, 150, 130)):
            self.tree.setColumnWidth(column, width)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        set_theme_style(
            self.tree,
            lambda: (
                f"QTreeWidget {{ color: {Colors.TEXT}; background: {Colors.SURFACE_1}; "
                f"border: 1px solid {Colors.BORDER}; font-size: {Fonts.SIZE_BODY}px; font-family: {Fonts.BODY}; }}"
                f"QHeaderView::section {{ background: {Colors.SURFACE_3}; color: {Colors.TEXT}; padding: 6px; }}"
                f"QTreeWidget::item:selected {{ background: {Colors.ACCENT}; color: {Colors.BG}; }}"
            ),
        )
        self.body_layout.addWidget(self.tree)
        self.select_all_btn.clicked.connect(self.tree.selectAll)
        QWidget.setTabOrder(self.select_all_btn, self.tree)
        self.status_label = QLabel("Loading Trash…")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(self.status_label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        self.body_layout.addWidget(self.status_label)
        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.restore_btn = QPushButton("Restore")
        self.restore_to_btn = QPushButton("Restore to…")
        self.purge_btn = QPushButton("Delete permanently / Retry")
        self.cancel_btn = QPushButton("Cancel remaining")
        for button in (self.refresh_btn, self.restore_btn, self.restore_to_btn, self.cancel_btn):
            set_theme_style(button, button_secondary_qss)
            row.addWidget(button)
        set_theme_style(self.restore_btn, button_primary_qss)
        set_theme_style(self.purge_btn, lambda: button_secondary_qss() + (
            f"QPushButton {{ color: {Colors.ERROR}; border-color: {Colors.ERROR}; }}"
            f"QPushButton:hover {{ background: {Colors.ERROR_SOFT}; }}"
            f"QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}"
        ))
        row.addWidget(self.purge_btn)
        self.body_layout.addLayout(row)
        self.refresh_btn.clicked.connect(self.refresh)
        self.restore_btn.clicked.connect(lambda: self.start_action("restore"))
        self.restore_to_btn.clicked.connect(self._choose_restore_folder)
        self.purge_btn.clicked.connect(lambda: self.start_action("purge"))
        self.cancel_btn.clicked.connect(self.cancel_remaining)
        self._update_actions()

    def _selected(self):
        return tuple(row.data(0, Qt.ItemDataRole.UserRole) for row in self.tree.selectedItems())

    def _update_actions(self):
        items = self._selected()
        busy = self._job is not None
        self.refresh_btn.setEnabled(not busy)
        self.tree.setEnabled(not busy)
        self.select_all_btn.setEnabled(not busy and self.tree.topLevelItemCount() > 0)
        restorable = bool(items) and all(item.status == TrashStatus.TRASHED for item in items)
        self.restore_btn.setEnabled(restorable and not busy)
        self.restore_to_btn.setEnabled(restorable and not busy)
        self.purge_btn.setEnabled(bool(items) and not busy)
        retry = bool(items) and all(item.status == TrashStatus.PURGING for item in items)
        self.purge_btn.setText("Retry deletion" if retry else "Delete permanently")
        self.cancel_btn.setVisible(busy)

    def refresh(self):
        if self._job is not None:
            return
        self._generation += 1
        generation = self._generation
        self.refresh_btn.setEnabled(False)

        def loaded(items, error):
            if not isValid(self) or generation != self._generation:
                return
            if error:
                self.status_label.setText("Trash could not be loaded: " + str(error))
                self._update_actions()
                return
            selected = {item.trash_id for item in self._selected()}
            self.tree.clear()
            for item in items:
                try:
                    created = (
                        datetime.fromisoformat(item.created_utc)
                        .astimezone()
                        .strftime("%b %d, %Y %H:%M")
                    )
                except (TypeError, ValueError):
                    created = str(item.created_utc)
                row = QTreeWidgetItem(
                    [
                        Path(item.original_path).name,
                        str(Path(item.original_path).parent),
                        created,
                        "Deletion pending" if item.status == TrashStatus.PURGING else "Restorable",
                        item.error or "",
                    ]
                )
                row.setData(0, Qt.ItemDataRole.UserRole, item)
                self.tree.addTopLevelItem(row)
                row.setSelected(item.trash_id in selected)
            count = f"{len(items)} item(s) in Trash" if items else "Trash is empty."
            self.status_label.setText(
                self._result_summary + "\n" + count if self._result_summary else count
            )
            self._update_actions()

        try:
            self._load(loaded)
        except Exception as error:
            loaded((), str(error))

    def _choose_restore_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Restore into an existing owned library folder"
        )
        if folder:
            self.start_action("restore", directory=folder)

    def start_action(self, kind, *, directory=None):
        if self._job is not None:
            return
        items = self._selected()
        if not items or kind not in ("restore", "purge"):
            return
        if kind == "purge" and not FthrMessageDialog.question(
            self,
            "Delete permanently",
            f"Permanently delete {len(items)} selected trash item(s)? "
            "Their original files and saved edits cannot be recovered after this step.",
            plain_text=True,
        ):
            return
        if kind == "restore" and not FthrMessageDialog.question(
            self,
            "Restore clips",
            f"Restore {len(items)} selected clip(s)? Existing originals are kept; "
            "a free restored filename will be chosen when needed.",
            plain_text=True,
        ):
            return
        if items != self._selected():
            return
        self._cancel = False
        self._job = dict(kind=kind, items=items, index=0, successes=0, failures=[])
        self._update_actions()
        self._next(directory)

    def cancel_remaining(self):
        self._cancel = True

    def _next(self, directory=None):
        job = self._job
        if job is None:
            return
        if self._cancel or job["index"] >= len(job["items"]):
            self._job = None
            summary = f"{job['successes']} {'restored' if job['kind'] == 'restore' else 'permanently deleted'}, {len(job['failures'])} incomplete"
            if self._cancel:
                summary += " (cancelled)"
            if job["failures"]:
                summary += "\n" + "\n".join(
                    f"{name}: {reason}" for name, reason in job["failures"][:10]
                )
            self._result_summary = summary
            self.status_label.setText(summary)
            self._update_actions()
            self.refresh()
            if job["kind"] == "restore" and job["successes"]:
                self.library_changed.emit()
            return
        item = job["items"][job["index"]]
        job["index"] += 1
        finished = False

        def completed(result, fresh_item, cleanup_done):
            nonlocal finished
            if finished:
                return
            finished = True
            if not isValid(self) or self._job is not job:
                return
            success = (
                result.succeeded
                and fresh_item is not None
                and (
                    fresh_item.status == TrashStatus.RESTORED
                    if job["kind"] == "restore"
                    else fresh_item.status == TrashStatus.PURGED and cleanup_done is True
                )
            )
            if success:
                job["successes"] += 1
            else:
                job["failures"].append(
                    (
                        Path(item.original_path).name,
                        result.error
                        or "The operation is incomplete; the item remains available for recovery",
                    )
                )
            self.status_label.setText(f"{job['index']} / {len(job['items'])} processed")
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, self, lambda: self._next(directory))

        try:
            if job["kind"] == "purge":
                self._purge(item.trash_id, completed)
            else:
                destination = (
                    str(Path(directory) / Path(item.original_path).name) if directory else None
                )
                self._restore(item.trash_id, completed, destination=destination)
        except Exception as error:
            from core.clip_metadata_types import MutationResult, FileOperationState

            completed(
                MutationResult(
                    "",
                    FileOperationState.ROLLED_BACK,
                    item.stored_path,
                    item.original_path,
                    str(error),
                ),
                item,
                False,
            )

    def hideEvent(self, event):
        self._generation += 1
        self._cancel = True
        super().hideEvent(event)


# Keep the import name available for standalone callers and older tests while
# the production shell now embeds the page in its QStackedWidget.
LibraryTrashDialog = LibraryTrashPage
