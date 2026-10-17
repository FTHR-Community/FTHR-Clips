from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path


_EDITOR_DRAFT_LIMIT = 250


class ClipMetadataManager:
    def __init__(self):
        self._file = Path.home() / '.fthr' / 'clip_metadata.json'
        self._database = self._file.with_suffix('.sqlite3')
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._database.exists():
            try:
                with closing(sqlite3.connect(self._database)) as connection:
                    initialized = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
                    ).fetchone()
                    if not initialized:
                        # An interrupted first import rolled back its schema.
                        return self._load_legacy()
                    rows = connection.execute('SELECT path, value FROM entries').fetchall()
                return {path: json.loads(value) for path, value in rows}
            except (sqlite3.Error, json.JSONDecodeError) as error:
                # Never silently replace newer drafts with the legacy backup.
                raise OSError(f'Could not read clip metadata: {error}') from error
        return self._load_legacy()

    def _load_legacy(self):
        try:
            data = json.loads(self._file.read_text())
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save(self, paths=None):
        """Commit only changed entries, with one atomic legacy import on first use.

        SQLite's FULL synchronous transactions retain crash durability without
        rewriting every library entry when one draft changes. The legacy JSON
        remains untouched as a migration backup.
        """
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self._database)
            try:
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                with connection:
                    imported = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
                    ).fetchone()
                    connection.execute('CREATE TABLE IF NOT EXISTS entries '
                                       '(path TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    changed = set(self._data) if not imported or paths is None else set(paths)
                    for path in changed:
                        if path in self._data:
                            connection.execute('INSERT INTO entries VALUES (?, ?) '
                                               'ON CONFLICT(path) DO UPDATE SET value=excluded.value',
                                               (path, json.dumps(self._data[path], separators=(',', ':'))))
                        else:
                            connection.execute('DELETE FROM entries WHERE path=?', (path,))
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise OSError(f'Could not save clip metadata: {error}') from error

    def get(self, path: str) -> dict:
        entry = self._data.get(path, {})
        if not isinstance(entry, dict):
            entry = {}
        return {'tag': entry.get('tag', ''), 'description': entry.get('description', '')}

    def set(self, path: str, tag: str = None, description: str = None):
        entry = self._data.get(path)
        if not isinstance(entry, dict):
            entry = {}
            self._data[path] = entry
        if tag is not None:
            entry['tag'] = tag
        if description is not None:
            entry['description'] = description
        self._save({path})

    @staticmethod
    def _file_identity(path: str) -> dict | None:
        """Return the inexpensive identity used to reject stale edit drafts."""

        try:
            stat = os.stat(path)
        except OSError:
            # Missing/replaced media has no valid draft identity to return.
            return None
        return {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size}

    def get_editor_draft(self, path: str) -> dict | None:
        """Load a draft only when it still belongs to the same source file."""

        entry = self._data.get(path, {})
        draft = entry.get('editor_draft') if isinstance(entry, dict) else None
        if not isinstance(draft, dict) or draft.get('version') != 1:
            return None
        if draft.get('file') != self._file_identity(path):
            return None
        state = draft.get('state')
        return state if isinstance(state, dict) else None

    def set_editor_draft(self, path: str, state: dict) -> bool:
        """Atomically persist a small non-destructive clip-editor snapshot."""

        identity = self._file_identity(path)
        if identity is None or not isinstance(state, dict):
            return False
        entry = self._data.get(path)
        if not isinstance(entry, dict):
            entry = {}
            self._data[path] = entry
        entry['editor_draft'] = {
            'version': 1,
            'file': identity,
            'updated_at': time.time(),
            'state': state,
        }
        pruned = self._prune_editor_drafts()
        self._save({path, *pruned})
        return True

    def edited_duration(self, path: str, source_duration: float) -> float:
        """Return retained source time, falling back safely for invalid drafts."""
        draft = self.get_editor_draft(path)
        if draft is None:
            return source_duration
        try:
            start = float(draft.get('trim_start', 0.0))
            end = float(draft.get('trim_end', 1.0))
            segments = draft['segments']
            if (not math.isfinite(start) or not math.isfinite(end)
                    or not 0 <= start < end <= 1
                    or not isinstance(segments, list) or not 1 <= len(segments) <= 500):
                return source_duration
            kept = previous = 0.0
            for left, right, deleted in segments:
                left, right = float(left), float(right)
                if (not math.isfinite(left) or not math.isfinite(right)
                        or not 0 <= left < right <= 1 or abs(left - previous) > 0.0001):
                    return source_duration
                previous = right
                if not deleted:
                    kept += max(0.0, min(end, right) - max(start, left))
            if abs(previous - 1.0) > 0.0001 or kept <= 0:
                return source_duration
            return source_duration * kept
        except (KeyError, TypeError, ValueError, OverflowError):
            return source_duration

    def _prune_editor_drafts(self) -> set[str]:
        """Bound draft storage without touching tag/description metadata."""

        drafts = []
        for path, entry in self._data.items():
            if not isinstance(entry, dict):
                continue
            draft = entry.get('editor_draft')
            if not isinstance(draft, dict):
                continue
            updated = draft.get('updated_at', 0)
            drafts.append((updated if isinstance(updated, (int, float)) else 0, path))
        drafts.sort(reverse=True)
        pruned = set()
        for _updated, stale_path in drafts[_EDITOR_DRAFT_LIMIT:]:
            pruned.add(stale_path)
            stale_entry = self._data[stale_path]
            stale_entry.pop('editor_draft', None)
            if not stale_entry:
                self._data.pop(stale_path, None)
        return pruned

    def clear_editor_draft(self, path: str) -> None:
        """Remove an editor draft while preserving tags and descriptions."""

        entry = self._data.get(path)
        if not isinstance(entry, dict) or 'editor_draft' not in entry:
            return
        del entry['editor_draft']
        if not entry:
            self._data.pop(path, None)
        self._save({path})

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Retain tags and non-destructive editor drafts after a file rename."""
        if old_path in self._data:
            self._data[new_path] = self._data.pop(old_path)
            self._save({old_path, new_path})
