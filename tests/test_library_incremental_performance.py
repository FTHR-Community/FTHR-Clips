"""Focused checks for incremental library updates that avoid full rebuild work."""
from types import SimpleNamespace

from core.library_index import LibraryRecord
from ui import clip_grid


def _record(path, *, mtime_ns, size=1):
    return LibraryRecord(
        path=path,
        identity=path,
        root_ids=(),
        imported=False,
        kind="video",
        size=size,
        mtime_ns=mtime_ns,
    )


def test_saved_path_insertion_uses_index_metadata_and_places_after_ties(monkeypatch):
    records = {
        "old.mp4": _record("old.mp4", mtime_ns=10),
        "same-a.mp4": _record("same-a.mp4", mtime_ns=20),
        "same-b.mp4": _record("same-b.mp4", mtime_ns=20),
        "new.mp4": _record("new.mp4", mtime_ns=30),
        "saved.mp4": _record("saved.mp4", mtime_ns=20),
    }
    # The insertion must use the existing index; touching the filesystem for
    # any existing path would make a save scale with the library size.
    monkeypatch.setattr(clip_grid.os, "stat", lambda *_: (_ for _ in ()).throw(
        AssertionError("incremental insertion performed filesystem I/O")))

    for sort, initial, expected in (
        ("oldest", ["old.mp4", "same-a.mp4", "same-b.mp4", "new.mp4"],
         ["old.mp4", "same-a.mp4", "same-b.mp4", "saved.mp4", "new.mp4"]),
        ("newest", ["new.mp4", "same-a.mp4", "same-b.mp4", "old.mp4"],
         ["new.mp4", "same-a.mp4", "same-b.mp4", "saved.mp4", "old.mp4"]),
    ):
        ordered = initial.copy()
        grid = SimpleNamespace(_sort=sort, _records=records)
        clip_grid.ClipGrid._insert_saved_path(grid, ordered, "saved.mp4")
        assert ordered == expected


def test_saved_path_insertion_orders_longest_by_indexed_size(monkeypatch):
    records = {
        "small.mp4": _record("small.mp4", mtime_ns=30, size=10),
        "large.mp4": _record("large.mp4", mtime_ns=10, size=90),
        "middle.mp4": _record("middle.mp4", mtime_ns=20, size=50),
        "saved.mp4": _record("saved.mp4", mtime_ns=15, size=50),
    }
    monkeypatch.setattr(clip_grid.os, "stat", lambda *_: (_ for _ in ()).throw(
        AssertionError("incremental insertion performed filesystem I/O")))
    ordered = ["large.mp4", "middle.mp4", "small.mp4"]
    clip_grid.ClipGrid._insert_saved_path(
        SimpleNamespace(_sort="longest", _records=records), ordered, "saved.mp4")
    assert ordered == ["large.mp4", "middle.mp4", "saved.mp4", "small.mp4"]


def test_saved_path_with_missing_index_metadata_preserves_zero_fallback():
    ordered = ["unknown-existing.mp4", "known-old.mp4"]
    records = {
        "known-old.mp4": _record("known-old.mp4", mtime_ns=1),
        "saved.mp4": _record("saved.mp4", mtime_ns=2),
    }
    clip_grid.ClipGrid._insert_saved_path(
        SimpleNamespace(_sort="oldest", _records=records), ordered, "saved.mp4")
    assert ordered == ["unknown-existing.mp4", "known-old.mp4", "saved.mp4"]
    clip_grid.ClipGrid._insert_saved_path(
        SimpleNamespace(_sort="oldest", _records=records), ordered, "unknown-saved.mp4")
    assert ordered == ["unknown-existing.mp4", "unknown-saved.mp4", "known-old.mp4", "saved.mp4"]


def test_filtered_record_count_path_skips_ordering():
    records = {
        "new": _record("new", mtime_ns=30),
        "old": _record("old", mtime_ns=10),
    }
    grid = SimpleNamespace(_records=records, _filter="all", _sort="newest")

    assert clip_grid.ClipGrid._filtered_records(grid, ordered=False) == list(records.values())
    assert [record.path for record in clip_grid.ClipGrid._filtered_records(grid)] == ["new", "old"]


def test_watcher_sync_only_adds_and_removes_directory_delta(monkeypatch, tmp_path):
    keep = tmp_path / "keep"
    remove = tmp_path / "remove"
    add = tmp_path / "add"
    missing = tmp_path / "missing"
    for path in (keep, remove, add):
        path.mkdir()

    class Watcher:
        def __init__(self, directories):
            self.paths = set(directories)
            self.added = []
            self.removed = []
            self.directory_reads = 0

        def directories(self):
            self.directory_reads += 1
            return list(self.paths)

        def addPath(self, path):
            self.added.append(path)
            self.paths.add(path)
            return True

        def removePath(self, path):
            self.removed.append(path)
            self.paths.discard(path)
            return True

    watcher = Watcher((str(keep), str(remove)))
    grid = SimpleNamespace(_shutdown_started=False, _watcher=watcher)
    checked_directories = []
    original_isdir = clip_grid.os.path.isdir

    def counted_isdir(path):
        checked_directories.append(path)
        return original_isdir(path)

    monkeypatch.setattr(clip_grid.os.path, "isdir", counted_isdir)

    clip_grid.ClipGrid._sync_watched_directories(
        grid, (str(keep), str(add), str(missing)))

    assert watcher.added == [str(add)]
    assert watcher.removed == [str(remove)]
    assert watcher.paths == {str(keep), str(add)}
    assert watcher.directory_reads == 1
    assert set(checked_directories) == {str(add), str(missing)}
    assert len(checked_directories) == 2
