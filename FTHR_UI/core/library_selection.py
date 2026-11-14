"""Pure selection snapshots for the currently ordered library results."""
from __future__ import annotations

import os
from collections.abc import Iterable

from core.library_index import LibraryRecord


def _same_file(left: LibraryRecord, right: LibraryRecord) -> bool:
    return (left.file_identity == right.file_identity
            and left.size == right.size and left.mtime_ns == right.mtime_ns)


class LibrarySelection:
    def __init__(self):
        self._selected: dict[str, LibraryRecord] = {}
        self._ordered: tuple[LibraryRecord, ...] = ()
        self._anchor: LibraryRecord | None = None

    @property
    def count(self) -> int:
        return len(self._selected)

    @property
    def has_selection(self) -> bool:
        return bool(self._selected)

    @property
    def selected_records(self) -> tuple[LibraryRecord, ...]:
        return tuple(record for record in self._ordered if self.contains(record))

    def contains(self, record: LibraryRecord) -> bool:
        prior = self._selected.get(record.identity)
        return prior is not None and _same_file(prior, record)

    def clear(self) -> None:
        self._selected.clear()
        self._anchor = None

    def reconcile(self, records: Iterable[LibraryRecord]) -> None:
        self._ordered = tuple(records)
        current = {record.identity: record for record in self._ordered}
        self._selected = {key: current[key] for key, prior in self._selected.items()
                          if key in current and _same_file(prior, current[key])}
        if self._anchor is not None:
            candidate = current.get(self._anchor.identity)
            self._anchor = (candidate if candidate is not None
                            and _same_file(self._anchor, candidate) else None)

    def select(self, record: LibraryRecord, ordered_records: Iterable[LibraryRecord],
               *, toggle=False, extend=False) -> None:
        self.reconcile(ordered_records)
        current = next((item for item in self._ordered
                        if item.identity == record.identity and _same_file(item, record)), None)
        if current is None:
            return
        if extend and self._anchor is not None:
            ids = [item.identity for item in self._ordered]
            start, end = sorted((ids.index(self._anchor.identity), ids.index(current.identity)))
            if not toggle:
                self._selected.clear()
            self._selected.update((item.identity, item) for item in self._ordered[start:end + 1])
        else:
            selected = self.contains(current)
            if not toggle:
                self._selected.clear()
            if toggle and selected:
                self._selected.pop(current.identity, None)
            else:
                self._selected[current.identity] = current
            self._anchor = current

    def select_all(self, records: Iterable[LibraryRecord]) -> None:
        self._ordered = tuple(records)
        self._selected = {record.identity: record for record in self._ordered}
        self._anchor = self._ordered[0] if self._ordered else None

    def rename(self, old_path: str, new_record: LibraryRecord) -> None:
        key = os.path.normcase(os.path.normpath(os.fspath(old_path)))
        old = next((record for record in self._ordered
                    if os.path.normcase(os.path.normpath(record.path)) == key), None)
        if old is None:
            return
        selected = self.contains(old)
        self._selected.pop(old.identity, None)
        same = old.file_identity is not None and _same_file(old, new_record)
        self._ordered = tuple(new_record if record.identity == old.identity else record
                              for record in self._ordered)
        if selected and same:
            self._selected[new_record.identity] = new_record
        if self._anchor is not None and self._anchor.identity == old.identity:
            self._anchor = new_record if same else None
