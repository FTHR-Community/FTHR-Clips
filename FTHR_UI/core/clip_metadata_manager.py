from __future__ import annotations

import json
import hashlib
import math
import os
import re
import sqlite3
import stat as stat_module
import threading
import time
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from functools import wraps
from itertools import chain
from pathlib import Path
from uuid import UUID, uuid4
from collections.abc import Mapping

from core.clip_metadata_types import (
    BindingState, ClipBinding, ClipFile, FileEvidence, FileRole,
    MigrationResult, SchemaState, UnsupportedSchemaVersion,
    FileOperationState, RenameOperation,
)
from core.library_index import canonical_media_path
from core.library_albums import (
    Album, album_from_row, create_album_schema, normalize_album_name,
)
from core.library_searches import (
    SavedSearch, criteria_to_dict, normalize_search_criteria,
    normalize_search_name,
)
from core.library_covers import (
    CoverAssetStore, CoverCommittedError, cleanup_retired_asset,
    parse_cover_reference,
)
from core.highlight_markers import (
    HIGHLIGHT_MARKERS_FIELD,
    decode_highlight_markers,
    marker_dicts,
    markers_to_payload,
)
from core.library_tags import format_tags_text, normalize_tags


_EDITOR_DRAFT_LIMIT = 250
_CURRENT_SCHEMA_MAX = 6
_CURRENT_SCHEMA_VERSIONS = (1, 2, 3, 4, 5, 6)
_ALBUM_SCHEMA_VERSION = 4
_SAVED_SEARCH_SCHEMA_VERSION = 5
_RESTORE_RECEIPT_SCHEMA_VERSION = 6
_TRASH_SCHEMA_VERSIONS = (3, 4, 5, 6)
_JOURNAL_SCHEMA_VERSIONS = (2, 3, 4, 5, 6)
_DELETED = object()
_NO_EXPECTED_CLIP_ID = object()


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def _normalize_tag_delta(values):
    """Normalize delta values without applying the final 32-tag limit."""
    if isinstance(values, (str, bytes)):
        raise TypeError('tags must be an iterable of strings')
    try:
        iterator = iter(values)
    except TypeError as error:
        raise TypeError('tags must be an iterable of strings') from error
    result = []
    keys = set()
    for value in iterator:
        normalized = normalize_tags((value,))[0]
        key = normalized.casefold()
        if key not in keys:
            keys.add(key)
            result.append(normalized)
    return tuple(result)


def _sync_owned_legacy_tag(metadata, current_metadata, fields, schema_version):
    """Apply an explicit legacy tag write against the fresh SQL shape."""
    if 'tag' not in fields:
        return metadata
    has_canonical = (schema_version >= _SAVED_SEARCH_SCHEMA_VERSION
                     or 'library_tags' in current_metadata)
    if not has_canonical:
        metadata.pop('library_tags', None)
        return metadata
    try:
        metadata['library_tags'] = list(normalize_tags((metadata['tag'],)))
    except (TypeError, ValueError):
        metadata.pop('library_tags', None)
    return metadata


class ClipMetadataManager:
    def __init__(self, *, database_path=None, legacy_path=None):
        self._lock = threading.RLock()
        if database_path is not None:
            self._database = Path(self._path_text(database_path))
            self._file = (Path(self._path_text(legacy_path)) if legacy_path is not None
                          else self._database.with_suffix('.json'))
        elif legacy_path is not None:
            self._file = Path(self._path_text(legacy_path))
            self._database = self._file.with_suffix('.sqlite3')
        else:
            self._file = Path.home() / '.fthr' / 'clip_metadata.json'
            self._database = self._file.with_suffix('.sqlite3')
        if self._database.resolve() == self._file.resolve():
            raise ValueError('database and legacy metadata must be different files')
        self._cover_store = CoverAssetStore(self._database)
        self._id_data: dict = {}
        self._path_ids: dict = {}
        self._files: dict[str, ClipFile] = {}
        self._primary_index: dict[str, set[str]] = {}
        self._trashed_ids = set()
        self._albums: dict[str, Album] = {}
        self._album_members: dict[str, set[str]] = {}
        self._saved_searches: dict[str, SavedSearch] = {}
        self._schema_snapshot = SchemaState(0, 'empty', True)
        self._data: dict = self._load()

    @property
    def backup_cover_root(self) -> Path:
        """Return the private cover namespace without performing filesystem I/O."""
        return self._cover_store.root

    def _load(self) -> dict:
        schema = self.inspect_schema()
        if schema.kind == 'future':
            self._schema_snapshot = schema
            # Unknown versions are never interpreted as empty legacy input.
            return {}
        if schema.kind == 'current':
            data = self._load_current()
            self._schema_snapshot = schema
            return data
        self._schema_snapshot = schema
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
            _raw, data = self._strict_legacy()
            return data
        except FileNotFoundError:
            # No source exists; malformed or unreadable sources must not be
            # mistaken for an empty library and imported on a later write.
            return {}

    @_locked
    def _save(self, paths=None, *, staged_data=None, prune_ids=(), owned_fields=None):
        """Commit only changed entries, with one atomic legacy import on first use.

        SQLite's FULL synchronous transactions retain crash durability without
        rewriting every library entry when one draft changes. The legacy JSON
        remains untouched as a migration backup.
        staged_data contains only pending entries (or _DELETED tombstones),
        so a single update never copies the complete metadata collection.
        """
        staged = staged_data if staged_data is not None else {}
        schema = self._writable_schema()
        if schema.version in _CURRENT_SCHEMA_VERSIONS:
            self._save_current(paths, staged, prune_ids=prune_ids,
                               owned_fields=owned_fields)
            return
        self._database.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self._database)
            try:
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                with connection:
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    if version > _CURRENT_SCHEMA_MAX:
                        self._schema_snapshot = SchemaState(version, 'future', False)
                        raise UnsupportedSchemaVersion(f'unsupported metadata schema version {version}')
                    if version != 0:
                        raise RuntimeError('metadata schema changed; reopen the manager')
                    imported = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'"
                    ).fetchone()
                    connection.execute('CREATE TABLE IF NOT EXISTS entries '
                                       '(path TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    changed = (self._data.keys() | staged.keys()
                               if not imported or paths is None else set(paths))
                    for path in changed:
                        entry = staged.get(path, self._data.get(path, _DELETED))
                        if entry is not _DELETED:
                            connection.execute('INSERT INTO entries VALUES (?, ?) '
                                               'ON CONFLICT(path) DO UPDATE SET value=excluded.value',
                                               (path, json.dumps(entry, separators=(',', ':'))))
                        else:
                            connection.execute('DELETE FROM entries WHERE path=?', (path,))
            finally:
                connection.close()
        except sqlite3.Error as error:
            raise OSError(f'Could not save clip metadata: {error}') from error

    @_locked
    def _commit_changes(self, staged_data: dict, *, prune_ids=(), owned_fields=None) -> None:
        """Publish pending RAM entries only after their transaction commits."""
        staged_data = {path: deepcopy(entry) if entry is not _DELETED else _DELETED
                       for path, entry in staged_data.items()}
        self._save(staged_data.keys(), staged_data=staged_data, prune_ids=prune_ids,
                   owned_fields=owned_fields)
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            return
        for path, entry in staged_data.items():
            if entry is _DELETED:
                self._data.pop(path, None)
            else:
                self._data[path] = entry

    @_locked
    def get(self, path: str) -> dict:
        entry = self._entry_at(path)
        if not isinstance(entry, dict):
            entry = {}
        return deepcopy({'tag': entry.get('tag', ''), 'description': entry.get('description', '')})

    @_locked
    def set(self, path: str, tag: str = None, description: str = None):
        schema = self._writable_schema()
        if schema.version in _CURRENT_SCHEMA_VERSIONS:
            path = self._stored_primary_path(path)
        entry = self._entry_at(path)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if tag is not None:
            entry['tag'] = tag
            # Preserve the legacy alias exactly, while synchronizing a valid
            # one-item canonical list.  Old values outside new-tag limits
            # remain readable legacy data until an explicit replacement.
            had_canonical = 'library_tags' in entry
            try:
                canonical = list(normalize_tags((tag,)))
            except (TypeError, ValueError):
                entry.pop('library_tags', None)
                canonical = None
            if canonical is not None and (
                    schema.version >= _SAVED_SEARCH_SCHEMA_VERSION or had_canonical):
                entry['library_tags'] = canonical
        if description is not None:
            entry['description'] = description
        owned = set()
        if tag is not None:
            owned.add('tag')
            if schema.version >= _SAVED_SEARCH_SCHEMA_VERSION or had_canonical:
                owned.add('library_tags')
        if description is not None:
            owned.add('description')
        self._commit_changes({path: entry}, owned_fields={path: owned})

    @staticmethod
    def _metadata_tags(metadata):
        if not isinstance(metadata, dict):
            return ()
        if 'library_tags' in metadata:
            values = metadata.get('library_tags')
            if type(values) is not list:
                raise ValueError('stored library tags are malformed')
            try:
                return normalize_tags(values)
            except (TypeError, ValueError) as error:
                raise ValueError('stored library tags are malformed') from error
        legacy = metadata.get('tag')
        return (legacy,) if isinstance(legacy, str) and legacy else ()

    @_locked
    def mark_recorded(self, path, *, expected_identity, group=None):
        """Remember a completed capture, independently of configured folders.

        This is display provenance and never grants permission to delete media.
        Only the capture publication path should call this method.
        """
        path = self._path_text(path)
        self._validate_album_identity(expected_identity)
        if expected_identity is None:
            raise ValueError('Recorded media requires a captured file identity')
        evidence = self.inspect_media(path)
        if not self._identity_matches(evidence.stat, expected_identity):
            raise OSError('Recorded media changed before provenance was saved')
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError('Recorded group must be nonempty text')
        origin = {'kind': 'recorded', 'identity': dict(evidence.stat), 'group': group}
        schema = self._writable_schema()
        if schema.version in _CURRENT_SCHEMA_VERSIONS:
            self.register_media(evidence, adopt_legacy=True)
            path = self._stored_primary_path(path)
        entry = self._entry_at(path)
        entry = dict(entry) if isinstance(entry, dict) else {}
        entry['library_origin'] = origin
        self._recheck(evidence)
        self._commit_changes({path: entry}, owned_fields={path: {'library_origin'}})
        return True

    @_locked
    def recorded_media_snapshot(self):
        """Return defensive RAM-only display provenance for background scans."""
        result = {}
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            entries = ((file.path, self._id_data.get(file.clip_id), file)
                       for file in self._files.values()
                       if file.role == FileRole.PRIMARY and file.binding_state in (
                           BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED,
                           BindingState.UNVERIFIED, BindingState.LEGACY_UNVERIFIED))
        else:
            entries = ((path, metadata, None) for path, metadata in self._data.items())
        for path, metadata, file in entries:
            origin = metadata.get('library_origin') if isinstance(metadata, dict) else None
            if not isinstance(origin, dict) or origin.get('kind') != 'recorded':
                continue
            identity = (dict(file.identity) if file and file.identity is not None
                        else origin.get('identity'))
            if (not isinstance(identity, dict)
                    or set(identity) != {'st_dev', 'st_ino', 'size', 'mtime_ns'}
                    or any(type(value) is not int for value in identity.values())):
                continue
            result[self._path_key(path)] = {
                'identity': deepcopy(identity), 'group': origin.get('group')}
        return result

    @_locked
    def is_recorded(self, path, *, expected_identity=None):
        """Read display provenance only when its current stat evidence matches."""
        known = self.recorded_media_snapshot().get(self._path_key(path))
        if known is None:
            return False
        identity = expected_identity if expected_identity is not None else self._file_identity(path)
        return identity is not None and dict(identity) == known['identity']

    @_locked
    def get_tags(self, path) -> tuple[str, ...]:
        return tuple(self._metadata_tags(self._entry_at(path)))

    def _update_details_current(self, path, *, tags=None, description=None,
                                expected_identity=None, tag_changes=None) -> bool:
        """Apply one fresh-PRIMARY metadata transaction and publish its result."""
        self._require_current()
        path = self._path_text(path)
        normalized_tags = normalize_tags(tags) if tags is not None else None
        if description is not None and not isinstance(description, str):
            raise TypeError('description must be a string')
        self._validate_album_identity(expected_identity)
        from core.audio_manifest import manifest_path_for
        from core.clip_files import is_library_media_path
        if not is_library_media_path(path):
            raise ValueError('Only completed library media can have metadata')
        evidence = self.inspect_media(path)

        def retire_replaced_cache():
            for cached in tuple(self._primary_candidates(path)):
                if (cached.identity is not None
                        and not self._identity_matches(cached.identity, evidence.stat)):
                    self._publish_file(replace(cached, binding_state=BindingState.DETACHED),
                                       _DELETED)

        if expected_identity is not None and not self._identity_matches(
                expected_identity, evidence.stat):
            retire_replaced_cache()
            raise OSError('The selected file changed; select it again before changing metadata')
        if expected_identity is None:
            cached = self._primary_candidates(path)
            if any(file.identity is not None and not self._identity_matches(
                    file.identity, evidence.stat) for file in cached):
                retire_replaced_cache()
                raise OSError('Registered media identity changed; select the current file explicitly')

        key = self._path_key(path)
        adjacent = manifest_path_for(path)
        detached = None
        with self._current_transaction() as connection:
            rows = self._active_primary_rows(connection, key)
            if len(rows) > 1:
                raise ValueError('Conflicting file bindings prevent changing metadata')
            if rows and any(row[2] != FileRole.PRIMARY.value for row in rows):
                raise ValueError('The selected path belongs to an associated file')
            old = self._decode_file(rows[0]) if rows else None
            if old is not None:
                old = self._assert_cached_primary_current(connection, old)
                if old.binding_state in (BindingState.CONFLICT, BindingState.TRASHED,
                                         BindingState.PURGING, BindingState.DETACHED):
                    raise OSError('The selected clip is not an active primary')
            if old is None and expected_identity is None:
                raise OSError('Explicit current file identity is required to register metadata')
            self._assert_operation_available(
                connection, clip_ids=(old.clip_id,) if old else (),
                paths=(path, adjacent))
            if old is None:
                current = {}
                file = self._insert_primary(connection, path, current, evidence)
                old_metadata = {}
                file_changed = True
            else:
                row = connection.execute(
                    'SELECT path,value FROM entries WHERE clip_id=?',
                    (old.clip_id,)).fetchone()
                if row is None or row[0] != old.path:
                    raise OSError('Registered clip entry/path binding is inconsistent')
                current = json.loads(row[1])
                if not isinstance(current, dict):
                    raise ValueError('Invalid current clip metadata')
                old_metadata = deepcopy(current)
                replacement = (old.identity is not None
                               and not self._identity_matches(old.identity, evidence.stat))
                if replacement:
                    if expected_identity is None:
                        retire_replaced_cache()
                        raise OSError('Registered media identity changed; select the current file explicitly')
                    detached = self._detach_primary(connection, old)
                    current = {}
                    old_metadata = {}
                    file = self._insert_primary(connection, path, current, evidence)
                    file_changed = True
                elif old.identity is None:
                    if expected_identity is None:
                        raise OSError('Explicit current file identity is required to adopt legacy metadata')
                    state = (BindingState.CONTENT_VERIFIED if evidence.content_sha256
                             else BindingState.STAT_BOUND)
                    file = replace(old, identity=dict(evidence.stat), binding_state=state,
                                   content_sha256=evidence.content_sha256,
                                   provenance='explicit metadata adoption')
                    self._write_file(connection, file)
                    file_changed = True
                else:
                    file = old
                    file_changed = False

            if tag_changes is not None:
                additions, removals, replacement_tags = tag_changes
                if replacement_tags is not None:
                    effective_tags = replacement_tags
                else:
                    current_tags = tuple(self._metadata_tags(current))
                    effective_tags = normalize_tags(
                        tuple(value for value in current_tags
                              if value.casefold() not in removals)
                        + tuple(value for value in additions
                                if value.casefold() not in removals))
                current['library_tags'] = list(effective_tags)
                current['tag'] = format_tags_text(effective_tags)
            elif normalized_tags is not None:
                current['library_tags'] = list(normalized_tags)
                current['tag'] = format_tags_text(normalized_tags)
            if description is not None:
                current['description'] = description
            metadata_changed = current != old_metadata
            if metadata_changed:
                connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                                   (json.dumps(current, separators=(',', ':')), file.clip_id))
            self._recheck(evidence)
        if detached is not None:
            self._publish_file(detached, _DELETED)
        self._publish_primary_reconciliation(path, file, current)
        return bool(file_changed or metadata_changed)

    @_locked
    def set_details(self, path, *, tags=None, description=None, expected_identity=None) -> bool:
        return self._update_details_current(
            path, tags=tags, description=description,
            expected_identity=expected_identity)

    @_locked
    def update_tags(self, path, *, add=(), remove=(), replace=None,
                    expected_identity=None) -> bool:
        if replace is not None and (add or remove):
            raise ValueError('replace cannot be combined with add/remove')
        if replace is not None:
            replacement_tags = normalize_tags(replace)
            changes = ((), frozenset(), replacement_tags)
        else:
            additions = _normalize_tag_delta(add)
            removals = frozenset(value.casefold() for value in _normalize_tag_delta(remove))
            changes = (additions, removals, None)
        return self._update_details_current(
            path, expected_identity=expected_identity, tag_changes=changes)

    @_locked
    def metadata_bindings(self) -> dict[str, dict]:
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        if self._schema_snapshot.version == 0:
            return {}
        if self._schema_snapshot.version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')
        result = {}
        for file in self._files.values():
            if (file.role != FileRole.PRIMARY
                    or file.binding_state not in (BindingState.STAT_BOUND,
                                                   BindingState.CONTENT_VERIFIED)
                    or file.identity is None
                    or file.clip_id in self._trashed_ids
                    or self._path_ids.get(file.path) != file.clip_id
                    or len(self._primary_index.get(file.path_key, ())) != 1):
                continue
            fields = ('st_dev', 'st_ino', 'size', 'mtime_ns')
            identity = dict(file.identity)
            if set(identity) != set(fields) or any(
                    type(identity[field]) is not int for field in fields):
                continue
            metadata = self._id_data.get(file.clip_id, {})
            try:
                tags = list(self._metadata_tags(metadata))
                tag_error = None
            except (TypeError, ValueError) as error:
                tags = []
                tag_error = str(error)
            item = {'identity': {field: identity[field] for field in fields},
                    'tags': tags,
                    'description': metadata.get('description', '')
                    if isinstance(metadata, dict) else ''}
            if tag_error is not None:
                item['error'] = tag_error
            result[file.path_key] = item
        return deepcopy(result)

    @_locked
    def storage_bindings_snapshot(self):
        """Return one immutable, RAM-only storage-planning snapshot.

        Storage accounting must not call a metadata getter or walk the file
        system once per card.  This compact projection deliberately omits
        drafts and opaque editor state; the journal writer remains the final
        authority immediately before every move or purge.
        """
        from types import MappingProxyType
        from core.library_storage import StorageBindingsSnapshot

        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        if self._schema_snapshot.version == 0:
            return StorageBindingsSnapshot()
        if self._schema_snapshot.version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')
        fields = ('st_dev', 'st_ino', 'size', 'mtime_ns')
        rows = []
        by_path = {}
        for file in self._files.values():
            if file.binding_state == BindingState.DETACHED:
                continue
            identity = dict(file.identity or {})
            if (set(identity) != set(fields)
                    or any(type(identity[field]) is not int for field in fields)):
                identity = None
            metadata = self._id_data.get(file.clip_id, {})
            error = None
            try:
                flags = self._library_flags(metadata)
            except (TypeError, ValueError) as exc:
                flags = {'protected': False, 'favorite': False}
                error = str(exc)
            item = {
                'file_id': file.file_id,
                'clip_id': file.clip_id,
                'path': file.path,
                'path_key': file.path_key,
                'role': file.role.value,
                'binding_state': file.binding_state.value,
                'identity': identity,
                'flags': flags,
                'complete': file.binding_state in (
                    BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED,
                    BindingState.LEGACY_UNVERIFIED, BindingState.UNVERIFIED),
                'library_cover': (metadata.get('library_cover')
                                  if isinstance(metadata, dict) else None),
            }
            if error is not None:
                item['error'] = error
            rows.append(MappingProxyType(item))
            by_path[file.path_key] = item
        return StorageBindingsSnapshot(tuple(rows), by_path,
                                       frozenset(self._trashed_ids))

    @staticmethod
    def _validate_cover_identity(expected_identity):
        if expected_identity is None:
            raise ValueError('Expected file identity is required for library covers')
        from collections.abc import Mapping

        fields = {'st_dev', 'st_ino', 'size', 'mtime_ns'}
        if (not isinstance(expected_identity, Mapping)
                or set(expected_identity) != fields
                or any(type(value) is not int for value in expected_identity.values())
                or expected_identity['st_dev'] < 0
                or expected_identity['st_ino'] <= 0
                or expected_identity['size'] < 0):
            raise ValueError('Expected file identity requires four valid integer stat fields')

    def _require_cover_schema(self):
        schema = self._writable_schema()
        if schema.version not in (_SAVED_SEARCH_SCHEMA_VERSION,
                                  _RESTORE_RECEIPT_SCHEMA_VERSION):
            raise RuntimeError('explicit cover schema migration is required')

    @staticmethod
    def _cover_sql_primary(connection, path_key):
        rows = connection.execute(
            'SELECT * FROM clip_files WHERE path_key=? AND binding_state<>?',
            (path_key, BindingState.DETACHED.value)).fetchall()
        if len(rows) > 1:
            raise OSError('conflicting media bindings block library cover changes')
        if not rows:
            return None
        file = ClipMetadataManager._decode_file(rows[0])
        if file.role != FileRole.PRIMARY:
            raise OSError('the selected path is an associated file, not a primary clip')
        return file

    @staticmethod
    def _cover_metadata(connection, file):
        row = connection.execute(
            'SELECT path,value FROM entries WHERE clip_id=?', (file.clip_id,)).fetchone()
        if row is None or row[0] != file.path:
            raise OSError('registered clip entry/path binding is inconsistent')
        try:
            metadata = json.loads(row[1])
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError('invalid current clip metadata') from error
        if not isinstance(metadata, dict):
            raise ValueError('invalid current clip metadata')
        return metadata

    @staticmethod
    def _cover_revision(metadata):
        reference, error = parse_cover_reference(metadata.get('library_cover'))
        if error is not None:
            raise ValueError(error)
        return reference

    @staticmethod
    def _cover_primary_is_fresh(file, evidence):
        return (file.binding_state in (BindingState.STAT_BOUND,
                                       BindingState.CONTENT_VERIFIED)
                and file.identity is not None
                and dict(file.identity) == dict(evidence.stat))

    def cover_asset_path(self, clip_id, revision) -> Path:
        """Return a validated generated asset path for explicit cover work."""
        return self._cover_store.asset_path(str(clip_id), str(revision))

    def cover_asset_path_for_read(self, clip_id, revision) -> Path:
        """Build a bounded worker-read path without GUI-thread filesystem I/O."""
        return self._cover_store.asset_path_for_read(str(clip_id), str(revision))

    @_locked
    def cover_bindings(self) -> dict[str, dict]:
        """Return defensive, RAM-only cover state for active verified primaries."""
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        if self._schema_snapshot.version == 0:
            return {}
        if self._schema_snapshot.version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')
        fields = ('st_dev', 'st_ino', 'size', 'mtime_ns')
        result = {}
        for file in self._files.values():
            if (file.role != FileRole.PRIMARY
                    or file.binding_state not in (BindingState.STAT_BOUND,
                                                   BindingState.CONTENT_VERIFIED)
                    or file.identity is None
                    or file.clip_id in self._trashed_ids
                    or self._path_ids.get(file.path) != file.clip_id
                    or len(self._primary_index.get(file.path_key, ())) != 1):
                continue
            identity = dict(file.identity)
            if (set(identity) != set(fields)
                    or any(type(identity[field]) is not int for field in fields)):
                continue
            metadata = self._id_data.get(file.clip_id, {})
            reference, error = parse_cover_reference(
                metadata.get('library_cover') if isinstance(metadata, dict) else None)
            result[file.path_key] = {
                'clip_id': file.clip_id,
                'identity': {field: identity[field] for field in fields},
                'reference': reference.to_dict() if reference is not None else None,
                'error': error,
            }
        return deepcopy(result)

    def set_library_cover(self, path, png_bytes, *, expected_identity,
                          expected_clip_id, expected_cover_revision):
        """Atomically attach a staged immutable PNG to one verified clip."""
        self._require_cover_schema()
        path = self._path_text(path)
        self._validate_cover_identity(expected_identity)
        from core.clip_files import is_library_media_path
        from core.audio_manifest import manifest_path_for

        if not is_library_media_path(path):
            raise ValueError('Only completed library media can have a library cover')
        evidence = self.inspect_media(path)
        if not self._identity_matches(expected_identity, evidence.stat):
            raise OSError('The selected file changed; select it again before changing cover')
        staged, reference = self._cover_store.stage_png(png_bytes)
        final = None
        committed = False
        detached = None
        old_reference = None
        old_clip_id = None
        metadata = None
        file = None
        try:
            with self._lock:
                self._require_cover_schema()
                self._recheck(evidence)
                with self._current_transaction() as connection:
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    if version not in (_SAVED_SEARCH_SCHEMA_VERSION,
                                       _RESTORE_RECEIPT_SCHEMA_VERSION):
                        raise RuntimeError('explicit cover schema migration is required')
                    key = self._path_key(evidence.path)
                    old = self._cover_sql_primary(connection, key)
                    self._assert_operation_available(
                        connection, clip_ids=(old.clip_id,) if old else (),
                        paths=(evidence.path, manifest_path_for(evidence.path)))
                    if old is None:
                        if expected_clip_id is not None:
                            raise OSError('selected clip binding is no longer available')
                        metadata = {}
                    else:
                        if expected_clip_id is None or old.clip_id != str(expected_clip_id):
                            raise OSError('expected clip identity does not match current media')
                        metadata = self._cover_metadata(connection, old)
                        fresh = self._cover_primary_is_fresh(old, evidence)
                        if old.binding_state in (BindingState.CONFLICT,
                                                  BindingState.TRASHED,
                                                  BindingState.PURGING):
                            raise OSError('selected clip is not an active library source')
                        legacy_adoption = (
                            old.binding_state in (BindingState.UNVERIFIED,
                                                  BindingState.LEGACY_UNVERIFIED)
                            and old.identity is None)
                        if not fresh and not legacy_adoption:
                            if expected_cover_revision is not None:
                                raise OSError(
                                    'replacement media cannot reuse the previous cover')
                            # An explicit current ClipID plus current stat is
                            # the caller's replacement admission.  Detach the
                            # old generation, preserve its JSON/cover history,
                            # and create a clean owner for the new media.
                            detached = self._detach_primary(connection, old)
                            metadata = {}
                            file = self._insert_primary(
                                connection, evidence.path, metadata, evidence)
                            old = None
                        elif legacy_adoption:
                            file = replace(
                                old, identity=dict(evidence.stat),
                                binding_state=BindingState.STAT_BOUND,
                                provenance='explicit library cover adoption')
                            self._write_file(connection, file)
                        else:
                            file = old
                        if old is not None:
                            old_reference = self._cover_revision(metadata)
                            old_clip_id = old.clip_id
                    if file is None:
                        file = self._insert_primary(
                            connection, evidence.path, metadata, evidence)
                    current_reference = (self._cover_revision(metadata)
                                         if old is not None else None)
                    if expected_cover_revision != (
                            current_reference.revision if current_reference is not None else None):
                        raise OSError('library cover changed; reload before replacing it')
                    metadata = dict(metadata)
                    metadata['library_cover'] = reference.to_dict()
                    final = self._cover_store.publish(staged, file.clip_id, reference)
                    staged = None
                    connection.execute(
                        'UPDATE entries SET value=? WHERE clip_id=?',
                        (json.dumps(metadata, separators=(',', ':')), file.clip_id))
                    self._recheck(evidence)
                committed = True
                try:
                    if detached is not None:
                        self._publish_file(detached, _DELETED)
                    self._publish_primary_reconciliation(evidence.path, file, metadata)
                except (OSError, RuntimeError, ValueError, sqlite3.Error,
                        RecursionError):
                    # SQLite already committed the cover reference.  Reconcile
                    # while this writer lock is still held.  A second failure
                    # remains a committed result instead of becoming a write
                    # failure at the UI boundary.
                    try:
                        self.reload()
                    except (OSError, RuntimeError, ValueError, sqlite3.Error,
                            RecursionError) as reload_error:
                        raise CoverCommittedError(
                            'Cover saved; metadata reload is required',
                            reference=reference) from reload_error
            if old_reference is not None and old_clip_id is not None:
                cleanup_retired_asset(self._database, old_clip_id, old_reference)
            return reference
        finally:
            if staged is not None:
                try:
                    staged.unlink(missing_ok=True)
                except OSError:
                    # Best-effort staging cleanup must not mask the write result.
                    pass
            if not committed and final is not None and file is not None:
                cleanup_retired_asset(self._database, file.clip_id, reference)

    def clear_library_cover(self, path, *, expected_identity,
                            expected_clip_id, expected_cover_revision):
        """Remove one cover reference while retaining the media and metadata."""
        self._require_cover_schema()
        path = self._path_text(path)
        self._validate_cover_identity(expected_identity)
        from core.clip_files import is_library_media_path
        from core.audio_manifest import manifest_path_for

        if not is_library_media_path(path):
            raise ValueError('Only completed library media can clear a library cover')
        evidence = self.inspect_media(path)
        if not self._identity_matches(expected_identity, evidence.stat):
            raise OSError('The selected file changed; select it again before clearing cover')
        old_reference = None
        metadata = None
        file = None
        with self._lock:
            self._require_cover_schema()
            self._recheck(evidence)
            with self._current_transaction() as connection:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in (_SAVED_SEARCH_SCHEMA_VERSION,
                                   _RESTORE_RECEIPT_SCHEMA_VERSION):
                    raise RuntimeError('explicit cover schema migration is required')
                file = self._cover_sql_primary(connection, self._path_key(evidence.path))
                if file is None:
                    if expected_clip_id is None and expected_cover_revision is None:
                        # Resetting an already cover-free clip is an idempotent UI
                        # no-op.  A captured ID or revision still proves that the
                        # caller may be stale, so those cases remain fail-closed.
                        return False
                    raise OSError('selected clip binding is no longer available')
                self._assert_operation_available(
                    connection, clip_ids=(file.clip_id,),
                    paths=(evidence.path, manifest_path_for(evidence.path)))
                if (expected_clip_id is None
                        or file.clip_id != str(expected_clip_id)
                        or not self._cover_primary_is_fresh(file, evidence)):
                    raise OSError('expected clip identity does not match current media')
                metadata = self._cover_metadata(connection, file)
                old_reference = self._cover_revision(metadata)
                if old_reference is None:
                    if expected_cover_revision is not None:
                        raise OSError('library cover changed; reload before clearing it')
                    return False
                if expected_cover_revision != old_reference.revision:
                    raise OSError('library cover changed; reload before clearing it')
                metadata = dict(metadata)
                metadata.pop('library_cover', None)
                connection.execute(
                    'UPDATE entries SET value=? WHERE clip_id=?',
                    (json.dumps(metadata, separators=(',', ':')), file.clip_id))
                self._recheck(evidence)
            try:
                self._publish_primary_reconciliation(evidence.path, file, metadata)
            except (OSError, RuntimeError, ValueError, sqlite3.Error,
                    RecursionError):
                # The SQL removal is durable.  Reconcile under the same writer
                # lock and preserve the successful result for the caller.
                try:
                    self.reload()
                except (OSError, RuntimeError, ValueError, sqlite3.Error,
                        RecursionError) as reload_error:
                    raise CoverCommittedError(
                        'Cover reset; metadata reload is required',
                        reference=None, cleared=True) from reload_error
        cleanup_retired_asset(self._database, file.clip_id, old_reference)
        return True

    @staticmethod
    def _library_flags(metadata):
        if not isinstance(metadata, dict):
            raise ValueError('Invalid clip metadata; deletion is blocked')
        flags = metadata.get('library_flags', {})
        if not isinstance(flags, dict):
            raise ValueError('Invalid library protection flags; deletion is blocked')
        result = {}
        for name in ('favorite', 'protected'):
            value = flags.get(name, False)
            if type(value) is not bool:
                raise ValueError('Invalid library protection flags; deletion is blocked')
            result[name] = value
        # Favorites are the single user-facing retention flag. Interpret old
        # manually protected clips as favorites without rewriting metadata on
        # a read. Keep the legacy key as an alias for older guards/backups.
        favorite = result['favorite'] or result['protected']
        result['favorite'] = result['protected'] = favorite
        return result

    @_locked
    def get_library_flags(self, path):
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion('Library protection is unavailable for this metadata version')
        path = self._path_text(path)
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            if any(file.binding_state in (BindingState.TRASHED, BindingState.PURGING)
                   for file in self._primary_candidates(path)):
                raise OSError('Clip is in the trash and is not an active library source')
            binding = self.resolve_media(path)
            if binding is not None and binding.state in (BindingState.CONFLICT, BindingState.MISSING):
                raise OSError('Conflicting file bindings make library protection unavailable')
            metadata = (self._id_data.get(binding.clip_id, {}) if binding is not None
                        and binding.state != BindingState.DETACHED else {})
            return self._library_flags(metadata)
        return self._library_flags(self._entry_at(path))

    @_locked
    def get_highlight_markers(self, path, *, fresh=False, expected_identity=None):
        """Return defensive marker records from the validated metadata snapshot.

        ``library_highlights`` is an owned field with its own versioned
        contract.  Existing unsupported data raises instead of being treated
        as empty, so a later writer cannot accidentally erase a future format.
        """

        self._require_read_current()
        path = self._path_text(path)
        if fresh:
            metadata = self._fresh_highlight_snapshot(
                path, expected_identity=expected_identity)
            if HIGHLIGHT_MARKERS_FIELD not in metadata:
                return []
            return marker_dicts(decode_highlight_markers(
                metadata[HIGHLIGHT_MARKERS_FIELD], allow_encoded=False))
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            binding = self.resolve_media(path)
            if binding is None or binding.state in (
                    BindingState.DETACHED, BindingState.MISSING):
                return []
            if binding.state == BindingState.CONFLICT:
                raise OSError('Conflicting file bindings block highlight reads')
            metadata = self._id_data.get(binding.clip_id, {})
        else:
            metadata = self._entry_at(path)
        if not isinstance(metadata, dict) or HIGHLIGHT_MARKERS_FIELD not in metadata:
            return []
        return marker_dicts(decode_highlight_markers(
            metadata[HIGHLIGHT_MARKERS_FIELD], allow_encoded=False))

    @_locked
    def has_highlight_markers(self, path, *, fresh=False, expected_identity=None) -> bool:
        """Return SQL field presence without touching the media file.

        Presence is intentionally distinct from a valid empty list: a present
        malformed, future, null, or false value remains authoritative and
        blocks the embedded-media fallback.
        """

        path = self._path_text(path)
        if fresh:
            self._require_read_current()
            return self._fresh_highlight_presence(
                path, expected_identity=expected_identity)
        self._require_read_current()
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            binding = self.resolve_media(path)
            if binding is None or binding.state in (
                    BindingState.DETACHED, BindingState.MISSING):
                return False
            if binding.state == BindingState.CONFLICT:
                raise OSError('Conflicting file bindings block highlight reads')
            metadata = self._id_data.get(binding.clip_id, {})
        else:
            metadata = self._entry_at(path)
        return isinstance(metadata, dict) and HIGHLIGHT_MARKERS_FIELD in metadata

    @_locked
    def get_highlight_state_snapshot(self, path, *, expected_identity=None):
        """Return one identity-bound current SQL metadata snapshot.

        The caller may pass the identity captured when a clip was opened.  If
        omitted, this method obtains the current four-stat identity before
        entering the bounded read transaction.  No media payload is scanned.
        """
        self._require_read_current()
        path = self._path_text(path)
        return deepcopy(self._fresh_highlight_snapshot(
            path, expected_identity=expected_identity))

    def _fresh_highlight_snapshot(self, path, *, expected_identity=None):
        """Return one identity-bound current SQL entry without cache mutation."""
        evidence = self.inspect_media(path, hash_content=False)
        current_identity = evidence.stat
        if expected_identity is None:
            expected_identity = current_identity
        if (not isinstance(expected_identity, Mapping)
                or set(expected_identity) != {
                    'st_dev', 'st_ino', 'size', 'mtime_ns'}
                or any(type(value) is not int for value in expected_identity.values())):
            raise OSError('Current media identity is unavailable')
        if dict(current_identity) != dict(expected_identity):
            raise OSError('Current media identity changed during highlight read')
        if not self._database.exists():
            if self._schema_snapshot.version != 0:
                raise OSError('metadata database disappeared during highlight read')
            metadata = deepcopy(self._data.get(path, {}))
            if not isinstance(metadata, dict):
                raise OSError('Stored clip metadata is unavailable')
            try:
                self._recheck(evidence)
            except (OSError, ValueError) as error:
                raise OSError('Media changed during highlight read') from error
            return metadata
        try:
            metadata = None
            with self._read_connection(timeout=0.25) as connection:
                # Keep the primary row and its metadata value in one explicit
                # read-only snapshot.  A later SQL writer must not make this
                # read observe a mixture of bindings from two generations.
                connection.execute('BEGIN')
                version = int(connection.execute('PRAGMA user_version').fetchone()[0])
                if version > _CURRENT_SCHEMA_MAX:
                    self._schema_snapshot = SchemaState(version, 'future', False)
                    raise UnsupportedSchemaVersion(
                        f'unsupported metadata schema version {version}')
                if version == 0:
                    row = connection.execute(
                        'SELECT value FROM entries WHERE path=?', (path,)).fetchone()
                    metadata = json.loads(row[0]) if row is not None else {}
                    if not isinstance(metadata, dict):
                        raise OSError('Stored clip metadata is unavailable')
                elif version not in _CURRENT_SCHEMA_VERSIONS:
                    raise RuntimeError('explicit metadata migration is required')
                else:
                    path_key = self._path_key(path)
                    rows = self._active_primary_rows(connection, path_key)
                    if len(rows) > 1:
                        raise OSError('Conflicting file bindings block highlight reads')
                    if not rows:
                        metadata = {}
                    else:
                        file = self._decode_file(rows[0])
                        if (file.role != FileRole.PRIMARY
                                or file.binding_state not in (
                                    BindingState.STAT_BOUND,
                                    BindingState.CONTENT_VERIFIED)
                                or file.path != path
                                or file.path_key != path_key
                                or file.identity is None
                                or not self._identity_matches(file.identity, expected_identity)):
                            raise OSError('Current SQL clip owner is not backed by current media')
                        row = connection.execute(
                            'SELECT path,value FROM entries WHERE clip_id=?',
                            (file.clip_id,)).fetchone()
                        if row is None or row[0] != file.path:
                            raise OSError('Registered clip metadata is missing')
                        metadata = json.loads(row[1])
                        if not isinstance(metadata, dict):
                            raise OSError('Stored clip metadata is unavailable')
            if not isinstance(metadata, dict):
                raise OSError('Stored clip metadata is unavailable')
            try:
                self._recheck(evidence)
            except (OSError, ValueError) as error:
                raise OSError('Media changed during highlight read') from error
            return metadata
        except UnsupportedSchemaVersion:
            raise
        except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError,
                RecursionError) as error:
            raise OSError('Stored clip metadata is unavailable') from error

    def _fresh_highlight_presence(self, path, *, expected_identity=None) -> bool:
        """Read only the current owned field from SQL without replacing RAM.

        The queued viewer fallback uses this narrow snapshot at its apply
        boundary.  It deliberately preserves field presence for malformed,
        future, null, and false values and propagates schema/locking errors.
        """
        metadata = self._fresh_highlight_snapshot(
            path, expected_identity=expected_identity)
        return HIGHLIGHT_MARKERS_FIELD in metadata

    @_locked
    def set_highlight_markers(self, path, markers, expected_identity=None,
                              expected_clip_id=_NO_EXPECTED_CLIP_ID):
        """Atomically replace only ``library_highlights`` on one primary clip.

        The SQL row, file identity, clip binding and pending operation journal
        are rechecked under the same fresh writer transaction.  The manager's
        cached metadata is published only after that transaction commits.
        """

        self._require_current()
        from core.audio_manifest import manifest_path_for
        from core.clip_files import is_library_media_path

        path = self._path_text(path)
        if not is_library_media_path(path):
            raise ValueError('Only completed library media can have highlight markers')
        payload = markers_to_payload(markers)
        evidence = self.inspect_media(path)
        if expected_identity is not None and not self._identity_matches(
                expected_identity, evidence.stat):
            raise OSError(
                'The selected file changed; select it again before changing highlights')

        # The default write form carries no fresh authority.  Compare the
        # manager's cached primary with the freshly inspected file before the
        # transaction can adopt or merge a replacement SQL owner.  An
        # explicitly supplied identity remains the deliberate authority for
        # the current owner and is checked above.
        if expected_identity is None:
            cached = self._primary_candidates(path)
            if any(file.identity is not None and not self._identity_matches(
                    file.identity, evidence.stat) for file in cached):
                raise OSError(
                    'Registered media identity changed; select the current file explicitly')

        detached = None
        with self._current_transaction() as connection:
            key = self._path_key(evidence.path)
            rows = self._active_primary_rows(connection, key)
            if len(rows) > 1:
                raise ValueError(
                    'Conflicting file bindings prevent changing highlight markers')
            old = self._decode_file(rows[0]) if rows else None
            if old is not None and old.role != FileRole.PRIMARY:
                raise ValueError('The selected path belongs to an associated file')
            if expected_identity is None and old is not None:
                cached = tuple(self._primary_candidates(path))
                if not any(
                        candidate.file_id == old.file_id
                        and candidate.identity is not None
                        and self._identity_matches(candidate.identity, evidence.stat)
                        for candidate in cached):
                    raise OSError(
                        'Current SQL clip owner is not backed by fresh cached authority')
            # Validate the cached primary against the fresh SQL row before any
            # expected-ID decision, adoption, detach or insert can proceed.
            if old is not None:
                old = self._assert_cached_primary_current(connection, old)
            if expected_clip_id is not _NO_EXPECTED_CLIP_ID:
                if expected_clip_id is None:
                    if old is not None:
                        raise OSError('Expected an unregistered clip binding')
                elif old is None or old.clip_id != str(expected_clip_id):
                    raise OSError('Expected clip identity does not match current media')
            self._assert_operation_available(
                connection, clip_ids=(old.clip_id,) if old else (),
                paths=(evidence.path, manifest_path_for(evidence.path)))

            if old is not None:
                if old.binding_state in (
                        BindingState.CONFLICT, BindingState.TRASHED,
                        BindingState.PURGING, BindingState.DETACHED):
                    raise OSError('The selected clip is not an active primary')
                row = connection.execute(
                    'SELECT path,value FROM entries WHERE clip_id=?',
                    (old.clip_id,)).fetchone()
                if row is None or row[0] != old.path:
                    raise OSError('Registered clip entry/path binding is inconsistent')
                current = json.loads(row[1])
                if not isinstance(current, dict):
                    raise ValueError('Invalid current clip metadata')
                old_metadata = deepcopy(current)
                replacement = (old.identity is not None
                               and not self._identity_matches(old.identity, evidence.stat))
                same_file = (old.identity is not None
                             and old.identity.get('st_dev') == evidence.stat.get('st_dev')
                             and old.identity.get('st_ino') == evidence.stat.get('st_ino'))
                if (replacement and same_file and expected_identity is not None
                        and expected_clip_id is not _NO_EXPECTED_CLIP_ID
                        and expected_clip_id is not None
                        and str(expected_clip_id) == old.clip_id):
                    # Marker append and other operation-owned in-place writes
                    # change size/mtime while retaining the same file object.
                    # Advance the registered identity in place instead of
                    # detaching the clip or creating a replacement owner.
                    state = (BindingState.CONTENT_VERIFIED
                             if evidence.content_sha256
                             else BindingState.STAT_BOUND)
                    file = replace(old, identity=dict(evidence.stat),
                                   binding_state=state,
                                   content_sha256=evidence.content_sha256)
                    self._write_file(connection, file)
                    old = file
                    replacement = False
                if replacement:
                    if (expected_identity is None
                            or expected_clip_id in (_NO_EXPECTED_CLIP_ID, None)):
                        raise OSError(
                            'Registered media identity changed; select the current file explicitly')
                    detached = self._detach_primary(connection, old)
                    current = {}
                    file = self._insert_primary(connection, evidence.path, current, evidence)
                elif old.identity is None:
                    if expected_identity is None:
                        raise OSError(
                            'Explicit current file identity is required to adopt legacy metadata')
                    state = (BindingState.CONTENT_VERIFIED if evidence.content_sha256
                             else BindingState.STAT_BOUND)
                    file = replace(old, identity=dict(evidence.stat), binding_state=state,
                                   content_sha256=evidence.content_sha256,
                                   provenance='explicit highlight metadata adoption')
                    self._write_file(connection, file)
                else:
                    file = old
            else:
                if (expected_clip_id is not _NO_EXPECTED_CLIP_ID
                        and expected_clip_id is not None) or expected_identity is None:
                    raise OSError('Explicit current file identity is required to register highlights')
                current = {}
                old_metadata = {}
                file = self._insert_primary(connection, evidence.path, current, evidence)

            if HIGHLIGHT_MARKERS_FIELD in current:
                # Validation is intentionally performed against fresh SQL
                # before the owned-field update; unsupported existing data is
                # preserved by refusing the write.
                decode_highlight_markers(
                    current[HIGHLIGHT_MARKERS_FIELD], allow_encoded=False)
            current[HIGHLIGHT_MARKERS_FIELD] = payload
            metadata_changed = current != old_metadata
            if metadata_changed:
                connection.execute(
                    'UPDATE entries SET value=? WHERE clip_id=?',
                    (json.dumps(current, separators=(',', ':')), file.clip_id))
            self._recheck(evidence)

        if detached is not None:
            self._publish_file(detached, _DELETED)
        self._publish_primary_reconciliation(path, file, current)
        return marker_dicts(payload['markers'])

    @_locked
    def flagged_paths(self, flag):
        if flag not in ('favorite', 'protected'):
            raise ValueError('Unknown library flag')
        self._require_read_current()
        marked = {clip_id for clip_id, value in self._id_data.items()
                  if isinstance(value, dict) and isinstance(value.get('library_flags'), dict)
                  and self._library_flags(value)[flag]}
        return tuple(sorted(file.path for file in self._files.values()
                            if file.clip_id in marked and file.role == FileRole.PRIMARY
                            and self._observed_state(file) in (
                                BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED)))

    @_locked
    def set_library_flags(self, path, *, favorite=None, protected=None, expected_identity=None):
        for value in (favorite, protected):
            if value is not None and type(value) is not bool:
                raise ValueError('Library flags must be boolean values')
        self._require_current()
        from core.clip_files import is_library_media_path
        if not is_library_media_path(path):
            raise ValueError('Only completed library media can have library flags')
        if favorite is None and protected is None:
            return self.get_library_flags(path)
        evidence = self.inspect_media(path)
        if expected_identity is not None:
            from collections.abc import Mapping
            keys = {'st_dev', 'st_ino', 'size', 'mtime_ns'}
            if (not isinstance(expected_identity, Mapping)
                    or set(expected_identity) != keys
                    or any(type(value) is not int for value in expected_identity.values())
                    or expected_identity['st_dev'] < 0 or expected_identity['st_ino'] <= 0
                    or expected_identity['size'] < 0):
                raise ValueError('Expected file identity requires four valid integer stat fields')
            if dict(expected_identity) != dict(evidence.stat):
                raise OSError('The selected file changed; select it again before changing flags')
        detached = None
        with self._current_transaction() as connection:
            rows = connection.execute(
                'SELECT * FROM clip_files WHERE path_key=? AND binding_state<>?',
                (self._path_key(evidence.path), BindingState.DETACHED.value)).fetchall()
            if len(rows) > 1:
                raise ValueError('Conflicting file bindings prevent changing library flags')
            old = self._decode_file(rows[0]) if rows else None
            if old is not None and old.role != FileRole.PRIMARY:
                raise FileExistsError('This path belongs to another registered clip file')
            self._assert_operation_available(connection, paths=(evidence.path,),
                                             clip_ids=(old.clip_id,) if old else ())
            metadata = {}
            if old is not None:
                if old.binding_state == BindingState.CONFLICT:
                    raise ValueError('Conflicting legacy bindings cannot be adopted')
                row = connection.execute('SELECT value FROM entries WHERE clip_id=?', (old.clip_id,)).fetchone()
                if row is None:
                    raise ValueError('Registered clip metadata is missing')
                metadata = json.loads(row[0])
                self._library_flags(metadata)
            replacement = (old is not None and old.binding_state in (
                BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED)
                and dict(old.identity or {}) != dict(evidence.stat))
            if replacement:
                detached = self._detach_primary(connection, old)
                metadata = {}
            flags = self._library_flags(metadata)
            stored_flags = dict(metadata.get('library_flags', {}))
            # Explicit favorite intent is authoritative. The old protected
            # argument remains a compatibility alias, never an independent
            # state that can outlive removal of the favorite.
            selected = favorite if favorite is not None else protected
            flags['favorite'] = flags['protected'] = selected
            stored_flags.pop('favorite_auto_protected', None)
            metadata = {**metadata, 'library_flags': {**stored_flags, **flags}}
            if old is None or replacement:
                file = self._insert_primary(connection, evidence.path, metadata, evidence)
            else:
                file = replace(old, identity=dict(evidence.stat),
                               binding_state=(BindingState.CONTENT_VERIFIED
                                              if old.binding_state == BindingState.CONTENT_VERIFIED
                                              else BindingState.STAT_BOUND),
                               provenance='explicit library flag adoption')
                self._write_file(connection, file)
                connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                                   (json.dumps(metadata, separators=(',', ':')), file.clip_id))
            self._recheck(evidence)
        if detached is not None:
            self._publish_file(detached, _DELETED)
        for cached in tuple(self._primary_candidates(evidence.path)):
            if cached.file_id != file.file_id:
                self._publish_file(replace(cached, binding_state=BindingState.DETACHED), _DELETED)
        self._publish_file(file, metadata)
        return dict(flags)

    @staticmethod
    def _deletable_identity(path):
        from core.library_ownership import media_file_identity
        return media_file_identity(path)

    @contextmanager
    def delete_guard(self, path, expected_identity=None):
        """Hold fresh protection/operation checks across the caller's short unlink.

        This does not grant ownership and is not a filesystem transaction.
        """
        path = self._path_text(path)
        with self._lock:
            with self._current_transaction() as connection:
                identity = self._deletable_identity(path)
                if expected_identity is not None and dict(expected_identity) != identity:
                    raise OSError('The file changed; deletion was cancelled')
                key = self._path_key(path)
                suffix = '.fthr-audio.json'
                adjacent_media = path[:-len(suffix)] if path.casefold().endswith(suffix) else None
                adjacent_key = self._path_key(adjacent_media) if adjacent_media else None
                rows = connection.execute(
                    'SELECT f.*,e.value FROM clip_files f JOIN entries e ON e.clip_id=f.clip_id '
                    'WHERE f.binding_state<>?', (BindingState.DETACHED.value,)).fetchall()
                matching = [row for row in rows if row[4] == key]
                adjacent = [row for row in rows if row[4] == adjacent_key and row[2] == FileRole.PRIMARY.value]
                if len(matching) > 1 or len(adjacent) > 1:
                    raise OSError('Conflicting file bindings block deletion')
                for row in rows:
                    file = self._decode_file(row[:9])
                    flags = self._library_flags(json.loads(row[9]))
                    if file.path_key == key:
                        self._assert_operation_available(connection, clip_ids=(file.clip_id,), paths=(path,))
                        if file.binding_state == BindingState.CONFLICT:
                            raise OSError('Ambiguous or associated file bindings block deletion')
                        if file.identity is not None and dict(file.identity) != identity:
                            raise OSError('Registered file identity changed; deletion was cancelled')
                        if flags['protected']:
                            raise OSError('This clip is a favorite. Remove it from favorites before deleting it.')
                    elif file.path_key == adjacent_key and file.role == FileRole.PRIMARY:
                        self._assert_operation_available(connection, clip_ids=(file.clip_id,),
                                                         paths=(path, adjacent_media))
                        if flags['protected'] or file.binding_state == BindingState.CONFLICT:
                            raise OSError('The favorite clip retains its audio manifest. Remove the clip from favorites first.')
                    elif flags['protected']:
                        observed = self._file_identity(file.path)
                        identity_matches = (file.binding_state in (
                            BindingState.UNVERIFIED, BindingState.LEGACY_UNVERIFIED)
                            or (file.identity is not None and dict(file.identity) == observed))
                        if (observed is not None and identity_matches
                                and (observed['st_dev'], observed['st_ino']) ==
                                (identity['st_dev'], identity['st_ino'])):
                            raise OSError('Another favorite clip references this file (hard link).')
                self._assert_operation_available(connection, paths=(path, adjacent_media) if adjacent_media else (path,))
                if self._deletable_identity(path) != identity:
                    raise OSError('The file changed before deletion')
                yield dict(identity)

    @staticmethod
    def _file_identity(path: str) -> dict | None:
        """Return the inexpensive identity used to reject stale edit drafts."""

        try:
            stat = os.stat(path)
        except OSError:
            # Missing/replaced media has no valid draft identity to return.
            return None
        # An inexpensive replacement check, not a hash of in-place content.
        return {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size,
                'st_dev': stat.st_dev, 'st_ino': stat.st_ino}

    @_locked
    def get_editor_draft(self, path: str) -> dict | None:
        """Load a draft only when it still belongs to the same source file."""

        entry = self._entry_at(path)
        draft = entry.get('editor_draft') if isinstance(entry, dict) else None
        if (not isinstance(draft, dict) or type(draft.get('version')) is not int
                or draft['version'] != 1):
            return None
        saved_identity = draft.get('file')
        identity = self._file_identity(path)
        if (not isinstance(saved_identity, dict) or identity is None
                or any(saved_identity.get(key) != identity[key]
                       for key in ('mtime_ns', 'size'))):
            return None
        # Existing v1 drafts without inode fields retain their legacy checks.
        # Once either stronger field is present, both must still match.
        if (any(key in saved_identity for key in ('st_dev', 'st_ino'))
                and any(saved_identity.get(key) != identity[key]
                        for key in ('st_dev', 'st_ino'))):
            return None
        state = draft.get('state')
        return deepcopy(state) if isinstance(state, dict) else None

    @_locked
    def set_editor_draft(self, path: str, state: dict) -> bool:
        """Atomically persist a small non-destructive clip-editor snapshot."""

        if self._writable_schema().version in _CURRENT_SCHEMA_VERSIONS:
            path = self._stored_primary_path(path)
        identity = self._file_identity(path)
        if identity is None or not isinstance(state, dict):
            return False
        entry = self._entry_at(path)
        entry = dict(entry) if isinstance(entry, dict) else {}
        old_draft = entry.get('editor_draft')
        if isinstance(old_draft, dict) and (
                type(old_draft.get('version')) is not int or old_draft['version'] != 1):
            raise ValueError('unsupported editor draft version cannot be overwritten')
        entry['editor_draft'] = {
            'version': 1,
            'file': identity,
            'updated_at': time.time(),
            'state': deepcopy(state),
        }
        staged = {path: entry}
        prune_ids = self._prune_editor_drafts(staged)
        self._commit_changes(staged, prune_ids=prune_ids,
                             owned_fields={path: {'editor_draft'}})
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

    def _prune_editor_drafts(self, staged_data: dict) -> set[str]:
        """Bound only the legacy cache; current SQLite drafts are durable."""
        if self.inspect_schema().version in _CURRENT_SCHEMA_VERSIONS:
            return self._prune_current_editor_drafts(staged_data)

        drafts = []
        for path, entry in chain(
                self._data.items(),
                ((path, entry) for path, entry in staged_data.items()
                 if path not in self._data)):
            entry = staged_data.get(path, entry)
            if not isinstance(entry, dict):
                continue
            draft = entry.get('editor_draft')
            if (not isinstance(draft, dict) or type(draft.get('version')) is not int
                    or draft['version'] != 1):
                continue
            updated = draft.get('updated_at', 0)
            drafts.append((updated if isinstance(updated, (int, float)) else 0, path))
        drafts.sort(reverse=True)
        for _updated, stale_path in drafts[_EDITOR_DRAFT_LIMIT:]:
            stale_entry = dict(staged_data.get(stale_path, self._data.get(stale_path)))
            stale_entry.pop('editor_draft', None)
            staged_data[stale_path] = stale_entry if stale_entry else _DELETED
        return set()

    def _prune_current_editor_drafts(self, staged_data: dict) -> set[str]:
        """Keep every current-schema draft; only legacy JSON is size-bounded."""
        return set()

    @_locked
    def clear_editor_draft(self, path: str) -> None:
        """Remove an editor draft while preserving tags and descriptions."""

        if self._writable_schema().version in _CURRENT_SCHEMA_VERSIONS:
            path = self._stored_primary_path(path)
        entry = self._entry_at(path)
        if not isinstance(entry, dict) or 'editor_draft' not in entry:
            return
        draft = entry['editor_draft']
        if isinstance(draft, dict) and (
                type(draft.get('version')) is not int or draft['version'] != 1):
            raise ValueError('unsupported editor draft version cannot be cleared')
        entry = dict(entry)
        del entry['editor_draft']
        self._commit_changes({path: entry if entry else _DELETED},
                             owned_fields={path: {'editor_draft'}})

    @_locked
    def rename_path(self, old_path: str, new_path: str) -> None:
        """Retain tags and non-destructive editor drafts after a file rename."""
        schema = self._writable_schema()
        if schema.version in _CURRENT_SCHEMA_VERSIONS:
            return self._rename_current(old_path, new_path)
        if old_path == new_path or old_path not in self._data:
            return
        if new_path in self._data:
            raise FileExistsError(f'Clip metadata already exists for {new_path}')
        self._commit_changes({old_path: _DELETED, new_path: self._data[old_path]})

    @contextmanager
    def _read_connection(self, *, timeout=5.0):
        uri = self._database.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=float(timeout))) as connection:
            yield connection

    @_locked
    def inspect_schema(self) -> SchemaState:
        if not self._database.exists():
            return SchemaState(0, 'legacy_json' if self._file.exists() else 'empty', True)
        try:
            with self._read_connection() as connection:
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version > _CURRENT_SCHEMA_MAX:
                    self._schema_snapshot = SchemaState(version, 'future', False)
                    return self._schema_snapshot
                columns = [row[1] for row in connection.execute('PRAGMA table_info(entries)')]
                if version in _CURRENT_SCHEMA_VERSIONS:
                    if columns != ['clip_id', 'path', 'value']:
                        raise OSError('invalid version 1 metadata schema')
                    if version >= 2 and not connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_operations'").fetchone():
                        raise OSError('version 2 metadata journal is missing')
                    if version in _TRASH_SCHEMA_VERSIONS and not connection.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trash_items'").fetchone():
                        raise OSError('version 3 trash registry is missing')
                    if version >= _ALBUM_SCHEMA_VERSION:
                        for table in ('albums', 'album_members'):
                            if not connection.execute(
                                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                    (table,)).fetchone():
                                raise OSError('version 4 album catalog is missing')
                    if version >= _SAVED_SEARCH_SCHEMA_VERSION:
                        search_columns = [row[1] for row in connection.execute(
                            'PRAGMA table_info(saved_searches)')]
                        if search_columns != [
                                'search_id', 'name', 'name_key', 'created_utc', 'criteria_json']:
                            raise OSError('version 5 saved search catalog is missing or invalid')
                    if version >= _RESTORE_RECEIPT_SCHEMA_VERSION:
                        receipt_columns = [row[1] for row in connection.execute(
                            'PRAGMA table_info(restore_receipts)')]
                        if receipt_columns != [
                                'restore_uuid', 'manifest_sha256', 'destination_key',
                                'destination_root', 'committed_utc']:
                            raise OSError('version 6 restore receipt catalog is missing or invalid')
                    return SchemaState(version, 'current', True)
                if columns:
                    if columns != ['path', 'value']:
                        raise OSError('invalid legacy metadata schema')
                    return SchemaState(0, 'legacy_sqlite', True)
                return SchemaState(0, 'legacy_json' if self._file.exists() else 'empty', True)
        except sqlite3.Error as error:
            raise OSError(f'Could not inspect clip metadata: {error}') from error

    def _writable_schema(self) -> SchemaState:
        schema = self.inspect_schema()
        if not schema.writable:
            self._schema_snapshot = schema
            raise UnsupportedSchemaVersion(f'unsupported metadata schema version {schema.version}')
        if (self._schema_snapshot.version == 0) != (schema.version == 0):
            raise OSError('metadata schema changed; explicitly reload the manager')
        self._schema_snapshot = schema
        return schema

    def _require_current(self):
        if self._writable_schema().version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')

    def _require_read_current(self):
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        if self._schema_snapshot.version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')

    @_locked
    def reload(self) -> None:
        """Explicitly refresh an external metadata/schema snapshot, without hashes."""
        self._data = self._load()

    @staticmethod
    def _path_text(path) -> str:
        try:
            value = os.fspath(path)
        except TypeError as error:
            raise ValueError('media path must be a filesystem path') from error
        if (not isinstance(value, str) or not value.strip() or '\0' in value
                or (re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', value)
                    and not re.match(r'^[A-Za-z]:[\\/]', value))):
            raise ValueError('invalid media filesystem path')
        return value

    @classmethod
    def _path_key(cls, path) -> str:
        return canonical_media_path(cls._path_text(path))

    @classmethod
    def _legacy_path_key(cls, path: str) -> str:
        try:
            return cls._path_key(path)
        except ValueError:
            # Preserve opaque legacy keys without treating them as media paths.
            return 'legacy:' + path

    def _load_current(self) -> dict:
        try:
            with self._read_connection() as connection:
                connection.execute('BEGIN')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version not in _CURRENT_SCHEMA_VERSIONS:
                    if version > _CURRENT_SCHEMA_MAX:
                        raise UnsupportedSchemaVersion(
                            f'unsupported metadata schema version {version}')
                    raise RuntimeError('metadata schema changed during reload')
                rows = connection.execute('SELECT clip_id,path,value FROM entries').fetchall()
                files = connection.execute(
                    'SELECT file_id,clip_id,role,path,path_key,binding_state,'
                    'identity_json,content_sha256,provenance FROM clip_files').fetchall()
                trashed_ids = ({row[0] for row in connection.execute(
                    "SELECT clip_id FROM trash_items WHERE status IN ('TRASHED','PURGING')")}
                    if version in _TRASH_SCHEMA_VERSIONS else set())
                albums = (connection.execute(
                    'SELECT album_id,name,created_utc FROM albums ORDER BY created_utc,album_id'
                ).fetchall() if version >= _ALBUM_SCHEMA_VERSION else [])
                memberships = (connection.execute(
                    'SELECT album_id,clip_id FROM album_members'
                ).fetchall() if version >= _ALBUM_SCHEMA_VERSION else [])
                searches = (connection.execute(
                    'SELECT search_id,name,created_utc,criteria_json FROM saved_searches '
                    'ORDER BY created_utc,search_id'
                ).fetchall() if version >= _SAVED_SEARCH_SCHEMA_VERSION else [])
            id_data = {clip_id: json.loads(value) for clip_id, _path, value in rows}
            path_ids = {path: clip_id for clip_id, path, _value in rows if path is not None}
            file_map = {}
            for row in files:
                file = self._decode_file(row)
                file_map[file.file_id] = file
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise OSError(f'Could not read versioned clip metadata: {error}') from error
        primary_index = {}
        for file in file_map.values():
            if file.role == FileRole.PRIMARY and file.binding_state != BindingState.DETACHED:
                primary_index.setdefault(file.path_key, set()).add(file.file_id)
        album_map = {str(row[0]): album_from_row(row) for row in albums}
        album_members = {album_id: set() for album_id in album_map}
        for album_id, clip_id in memberships:
            if album_id in album_map:
                album_members[album_id].add(str(clip_id))
        saved_searches = {}
        for search_id, name, created_utc, criteria_json in searches:
            criteria_error = None
            try:
                criteria = normalize_search_criteria(json.loads(criteria_json))
                if criteria.album_id is not None and criteria.album_id not in album_map:
                    criteria_error = 'Referenced album no longer exists'
            except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
                criteria = None
                criteria_error = f'Unavailable saved search: {error}'
            saved_searches[str(search_id)] = SavedSearch(
                str(search_id), str(name), str(created_utc), criteria, criteria_error)
        self._id_data, self._path_ids, self._files = id_data, path_ids, file_map
        self._trashed_ids = trashed_ids
        self._primary_index = primary_index
        self._albums = album_map
        self._album_members = album_members
        self._saved_searches = saved_searches
        return {canonical_media_path(path): id_data[clip_id]
                for path, clip_id in path_ids.items()}

    @staticmethod
    def _decode_file(row) -> ClipFile:
        file_id, clip_id, role, path, key, state, identity, digest, provenance = row
        return ClipFile(file_id, clip_id, FileRole(role), path, key, BindingState(state),
                        json.loads(identity) if identity is not None else None, digest, provenance)

    @staticmethod
    def _create_schema(connection):
        connection.execute('CREATE TABLE entries (clip_id TEXT PRIMARY KEY NOT NULL,'
                           'path TEXT UNIQUE, value TEXT NOT NULL)')
        connection.execute('CREATE TABLE clip_files ('
                           'file_id TEXT PRIMARY KEY NOT NULL, '
                           'clip_id TEXT NOT NULL REFERENCES entries(clip_id), '
                           'role TEXT NOT NULL,path TEXT NOT NULL,path_key TEXT NOT NULL,'
                           'binding_state TEXT NOT NULL,identity_json TEXT,'
                           'content_sha256 TEXT,provenance TEXT)')
        connection.execute('CREATE INDEX clip_files_path_key ON clip_files(path_key)')
        connection.execute('CREATE INDEX clip_files_clip_id ON clip_files(clip_id)')

    @staticmethod
    def _write_file(connection, file: ClipFile):
        connection.execute(
            'INSERT INTO clip_files VALUES (?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(file_id) DO UPDATE SET '
            'clip_id=excluded.clip_id,role=excluded.role,path=excluded.path,'
            'path_key=excluded.path_key,binding_state=excluded.binding_state,'
            'identity_json=excluded.identity_json,content_sha256=excluded.content_sha256,'
            'provenance=excluded.provenance',
            (file.file_id, file.clip_id, file.role.value, file.path, file.path_key,
             file.binding_state.value,
             json.dumps(dict(file.identity)) if file.identity is not None else None,
             file.content_sha256, file.provenance))

    def _backfill_schema(self, connection, rows):
        keys = {}
        for path, _value in rows:
            key = self._legacy_path_key(path)
            keys[key] = keys.get(key, 0) + 1
        for path, value in rows:
            clip_id = str(uuid4())
            key = self._legacy_path_key(path)
            connection.execute('INSERT INTO entries VALUES (?,?,?)', (clip_id, path, value))
            state = BindingState.CONFLICT if keys[key] > 1 else BindingState.LEGACY_UNVERIFIED
            self._write_file(connection, ClipFile(
                str(uuid4()), clip_id, FileRole.PRIMARY, path, key, state,
                provenance='legacy migration; media identity not inspected'))

    @staticmethod
    def _backup_name(source: Path, *, target_version=1) -> Path:
        backup = source.with_name(source.name + f'.pre-v{target_version}-' + uuid4().hex + '.bak')
        # Reserve exclusively. Failed/incomplete backups are intentionally retained.
        with backup.open('xb'):
            pass
        return backup

    def _backup_database(self, *, target_version=1) -> Path:
        backup = self._backup_name(self._database, target_version=target_version)
        # The writer already holds BEGIN IMMEDIATE. Back up a separate reader,
        # never that writer (which would wait on its own uncommitted transaction).
        with self._read_connection() as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise OSError('metadata backup failed its integrity check')
        return backup

    def _strict_legacy(self) -> tuple[bytes, dict]:
        raw = self._file.read_bytes()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise OSError('legacy metadata is not valid JSON') from error
        if not isinstance(data, dict):
            raise OSError('legacy metadata must be a JSON object')
        return raw, data

    @_locked
    def migrate(self, *, target_version=2) -> MigrationResult:
        if type(target_version) is not int or target_version not in _CURRENT_SCHEMA_VERSIONS:
            raise ValueError('unsupported migration target')
        schema = self.inspect_schema()
        if not schema.writable:
            self._schema_snapshot = schema
            raise UnsupportedSchemaVersion(f'unsupported metadata schema version {schema.version}')
        if schema.version > target_version:
            raise ValueError('metadata schema downgrades are not supported')
        if schema.version == target_version:
            self.reload()
            return MigrationResult(schema.version, schema.version, False)
        self._database.parent.mkdir(parents=True, exist_ok=True)
        existed = self._database.exists()
        raw = legacy = None
        if schema.kind == 'legacy_json':
            raw, legacy = self._strict_legacy()
        backup = None
        try:
            with closing(sqlite3.connect(self._database)) as connection:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                with connection:
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    if version > _CURRENT_SCHEMA_MAX:
                        self._schema_snapshot = SchemaState(version, 'future', False)
                        raise UnsupportedSchemaVersion(f'unsupported metadata schema version {version}')
                    if version != schema.version:
                        raise RuntimeError('metadata schema changed; reopen before migration')
                    has_entries = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entries'").fetchone()
                    if has_entries and version == 0:
                        rows = connection.execute('SELECT path,value FROM entries').fetchall()
                        for _path, value in rows:
                            json.loads(value)  # Reject corrupt data, without dropping unknown fields.
                    elif version == 0:
                        rows = [(path, json.dumps(value, separators=(',', ':')))
                                for path, value in (legacy or {}).items()]
                    else:
                        rows = []
                    if existed:
                        backup = self._backup_database(target_version=target_version)
                    if raw is not None:
                        # SQLite+JSON inputs may need both source backups.
                        json_backup = self._backup_name(self._file, target_version=target_version)
                        with json_backup.open('wb') as output:
                            output.write(raw)
                            output.flush()
                            os.fsync(output.fileno())
                        if json_backup.read_bytes() != raw or self._file.read_bytes() != raw:
                            raise OSError('legacy metadata changed during backup')
                        backup = backup or json_backup
                    if version == 0:
                        if has_entries:
                            connection.execute('ALTER TABLE entries RENAME TO entries_v0')
                        self._create_schema(connection)
                        self._backfill_schema(connection, rows)
                        if has_entries:
                            connection.execute('DROP TABLE entries_v0')
                    if target_version >= 2 and version < 2:
                        self._create_operation_schema(connection)
                    if target_version >= 3 and version < 3:
                        self._create_trash_schema(connection)
                    if target_version >= _ALBUM_SCHEMA_VERSION and version < _ALBUM_SCHEMA_VERSION:
                        self._create_album_schema(connection)
                    if target_version >= _SAVED_SEARCH_SCHEMA_VERSION and version < _SAVED_SEARCH_SCHEMA_VERSION:
                        self._create_saved_search_schema(connection)
                    if (target_version >= _RESTORE_RECEIPT_SCHEMA_VERSION
                            and version < _RESTORE_RECEIPT_SCHEMA_VERSION):
                        self._create_restore_receipt_schema(connection)
                    connection.execute(f'PRAGMA user_version={target_version}')
        except (sqlite3.Error, json.JSONDecodeError) as error:
            raise OSError(f'Could not migrate clip metadata: {error}') from error
        self._data = self._load_current()
        self._schema_snapshot = SchemaState(target_version, 'current', True)
        return MigrationResult(schema.version, target_version, True, len(rows), backup)

    @staticmethod
    def _create_operation_schema(connection):
        connection.execute('CREATE TABLE file_operations ('
                           'op_id TEXT PRIMARY KEY NOT NULL,version INTEGER NOT NULL,'
                           'kind TEXT NOT NULL,clip_id TEXT NOT NULL REFERENCES entries(clip_id),'
                           'state TEXT NOT NULL,plan_json TEXT NOT NULL,error TEXT,'
                           'cleanup_done INTEGER NOT NULL DEFAULT 0)')

    @staticmethod
    def _create_album_schema(connection):
        create_album_schema(connection)

    @staticmethod
    def _create_saved_search_schema(connection):
        connection.execute(
            'CREATE TABLE saved_searches ('
            'search_id TEXT PRIMARY KEY NOT NULL,'
            'name TEXT NOT NULL,'
            'name_key TEXT NOT NULL UNIQUE,'
            'created_utc TEXT NOT NULL,'
            'criteria_json TEXT NOT NULL)')
        connection.execute(
            'CREATE INDEX saved_searches_created ON saved_searches(created_utc,search_id)')

    @staticmethod
    def _create_restore_receipt_schema(connection):
        connection.execute(
            'CREATE TABLE restore_receipts ('
            'restore_uuid TEXT PRIMARY KEY NOT NULL,'
            'manifest_sha256 TEXT NOT NULL,'
            'destination_key TEXT NOT NULL,'
            'destination_root TEXT NOT NULL,'
            'committed_utc TEXT NOT NULL)')

    def _require_album_schema(self):
        self._require_current()
        if self._schema_snapshot.version < _ALBUM_SCHEMA_VERSION:
            raise RuntimeError('explicit album schema migration is required')

    def _album_read_version(self):
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        version = self._schema_snapshot.version
        if version == 0:
            return version
        if version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')
        return version

    @staticmethod
    def _album_row(connection, album_id):
        return connection.execute(
            'SELECT album_id,name,created_utc FROM albums WHERE album_id=?',
            (str(album_id),)).fetchone()

    @staticmethod
    def _active_primary_rows(connection, path_key):
        return connection.execute(
            'SELECT * FROM clip_files WHERE path_key=? AND binding_state<>?',
            (path_key, BindingState.DETACHED.value)).fetchall()

    @staticmethod
    def _identity_matches(left, right):
        try:
            left, right = dict(left), dict(right)
        except (TypeError, ValueError):
            # Malformed identity evidence is never considered a match.
            return False
        return all(left.get(key) == right.get(key)
                   for key in ('st_dev', 'st_ino', 'size', 'mtime_ns'))

    @staticmethod
    def _album_membership_exists(connection, album_id, clip_id):
        return connection.execute(
            'SELECT 1 FROM album_members WHERE album_id=? AND clip_id=?',
            (album_id, clip_id)).fetchone() is not None

    @_locked
    def list_albums(self) -> tuple[Album, ...]:
        """Return the manager's committed RAM catalog; call reload() explicitly."""
        if self._album_read_version() < _ALBUM_SCHEMA_VERSION:
            return ()
        return tuple(sorted(self._albums.values(), key=lambda album: (
            album.created_utc, album.album_id)))

    @_locked
    def album_bindings(self, album_id) -> dict[str, dict[str, int]]:
        """Return defensive identity copies for the cached album filter."""
        if self._album_read_version() < _ALBUM_SCHEMA_VERSION:
            return {}
        clip_ids = self._album_members.get(str(album_id), set())
        by_clip = {}
        for file in self._files.values():
            if (file.clip_id not in clip_ids or file.role != FileRole.PRIMARY
                    or file.binding_state not in (BindingState.STAT_BOUND,
                                                   BindingState.CONTENT_VERIFIED)
                    or file.identity is None or file.clip_id in self._trashed_ids
                    or self._path_ids.get(file.path) != file.clip_id):
                continue
            if len(self._primary_index.get(file.path_key, ())) != 1:
                continue
            by_clip.setdefault(file.clip_id, []).append(file)
        result = {}
        for files in by_clip.values():
            if len(files) != 1:
                continue
            file = files[0]
            identity = dict(file.identity)
            fields = ('st_dev', 'st_ino', 'size', 'mtime_ns')
            if (set(identity) != set(fields)
                    or any(type(identity[field]) is not int for field in fields)):
                continue
            result[file.path_key] = {field: identity[field] for field in fields}
        return result

    @_locked
    def create_album(self, name: str) -> Album:
        self._require_album_schema()
        display_name, name_key = normalize_album_name(name)
        album_id = str(uuid4())
        created_utc = datetime.now(timezone.utc).isoformat()
        with self._current_transaction() as connection:
            try:
                connection.execute(
                    'INSERT INTO albums(album_id,name,name_key,created_utc) VALUES (?,?,?,?)',
                    (album_id, display_name, name_key, created_utc))
            except sqlite3.IntegrityError as error:
                raise ValueError('an album with this name already exists') from error
        album = Album(album_id, display_name, created_utc)
        self._albums[album_id] = album
        self._album_members[album_id] = set()
        return album

    @_locked
    def rename_album(self, album_id, name: str) -> Album:
        self._require_album_schema()
        display_name, name_key = normalize_album_name(name)
        album_id = str(album_id)
        with self._current_transaction() as connection:
            row = self._album_row(connection, album_id)
            if row is None:
                raise ValueError('Album no longer exists')
            try:
                connection.execute(
                    'UPDATE albums SET name=?,name_key=? WHERE album_id=?',
                    (display_name, name_key, album_id))
            except sqlite3.IntegrityError as error:
                raise ValueError('an album with this name already exists') from error
            album = Album(album_id, display_name, str(row[2]))
        self._albums[album_id] = album
        return album

    @_locked
    def delete_album(self, album_id) -> None:
        self._require_album_schema()
        album_id = str(album_id)
        with self._current_transaction() as connection:
            connection.execute('DELETE FROM albums WHERE album_id=?', (album_id,))
        self._albums.pop(album_id, None)
        self._album_members.pop(album_id, None)
        for search_id, saved in tuple(self._saved_searches.items()):
            if saved.criteria is not None and saved.criteria.album_id == album_id:
                self._saved_searches[search_id] = replace(
                    saved, error='Referenced album no longer exists')

    def _saved_search_read_version(self):
        if not self._schema_snapshot.writable:
            raise UnsupportedSchemaVersion(
                f'unsupported metadata schema version {self._schema_snapshot.version}')
        version = self._schema_snapshot.version
        if version == 0:
            return version
        if version not in _CURRENT_SCHEMA_VERSIONS:
            raise RuntimeError('explicit metadata migration is required')
        return version

    def _require_saved_search_schema(self):
        self._require_current()
        if self._schema_snapshot.version < _SAVED_SEARCH_SCHEMA_VERSION:
            raise RuntimeError('explicit saved search schema migration is required')

    @_locked
    def list_saved_searches(self) -> tuple[SavedSearch, ...]:
        """Return the committed RAM catalog; call reload() after external writes."""
        version = self._saved_search_read_version()
        if version < _SAVED_SEARCH_SCHEMA_VERSION:
            return ()
        return tuple(sorted(self._saved_searches.values(), key=lambda item: (
            item.created_utc, item.search_id)))

    @_locked
    def create_saved_search(self, name, criteria) -> SavedSearch:
        self._require_saved_search_schema()
        display_name, name_key = normalize_search_name(name)
        normalized = normalize_search_criteria(criteria)
        search_id = str(uuid4())
        created_utc = datetime.now(timezone.utc).isoformat()
        with self._current_transaction() as connection:
            if normalized.album_id is not None and self._album_row(
                    connection, normalized.album_id) is None:
                raise ValueError('Referenced album no longer exists')
            try:
                connection.execute(
                    'INSERT INTO saved_searches '
                    '(search_id,name,name_key,created_utc,criteria_json) VALUES (?,?,?,?,?)',
                    (search_id, display_name, name_key, created_utc,
                     json.dumps(criteria_to_dict(normalized), separators=(',', ':'))))
            except sqlite3.IntegrityError as error:
                raise ValueError('a saved search with this name already exists') from error
        saved = SavedSearch(search_id, display_name, created_utc, normalized)
        self._saved_searches[search_id] = saved
        return saved

    @_locked
    def rename_saved_search(self, search_id, name) -> SavedSearch:
        self._require_saved_search_schema()
        display_name, name_key = normalize_search_name(name)
        search_id = str(search_id)
        with self._current_transaction() as connection:
            row = connection.execute(
                'SELECT created_utc,criteria_json FROM saved_searches WHERE search_id=?',
                (search_id,)).fetchone()
            if row is None:
                raise ValueError('Saved search no longer exists')
            try:
                connection.execute(
                    'UPDATE saved_searches SET name=?,name_key=? WHERE search_id=?',
                    (display_name, name_key, search_id))
            except sqlite3.IntegrityError as error:
                raise ValueError('a saved search with this name already exists') from error
            criteria_error = None
            try:
                criteria = normalize_search_criteria(json.loads(row[1]))
            except (TypeError, ValueError, RecursionError, json.JSONDecodeError) as error:
                criteria = None
                criteria_error = f'Unavailable saved search: {error}'
            if (criteria is not None and criteria.album_id is not None
                    and self._album_row(connection, criteria.album_id) is None):
                criteria_error = 'Referenced album no longer exists'
            saved = SavedSearch(search_id, display_name, str(row[0]), criteria,
                                criteria_error)
        self._saved_searches[search_id] = saved
        return saved

    @_locked
    def update_saved_search(self, search_id, criteria) -> SavedSearch:
        self._require_saved_search_schema()
        normalized = normalize_search_criteria(criteria)
        search_id = str(search_id)
        with self._current_transaction() as connection:
            row = connection.execute(
                'SELECT name,created_utc FROM saved_searches WHERE search_id=?',
                (search_id,)).fetchone()
            if row is None:
                raise ValueError('Saved search no longer exists')
            if normalized.album_id is not None and self._album_row(
                    connection, normalized.album_id) is None:
                raise ValueError('Referenced album no longer exists')
            connection.execute(
                'UPDATE saved_searches SET criteria_json=? WHERE search_id=?',
                (json.dumps(criteria_to_dict(normalized), separators=(',', ':')), search_id))
            saved = SavedSearch(search_id, str(row[0]), str(row[1]), normalized)
        self._saved_searches[search_id] = saved
        return saved

    @_locked
    def delete_saved_search(self, search_id) -> None:
        self._require_saved_search_schema()
        search_id = str(search_id)
        with self._current_transaction() as connection:
            if connection.execute('DELETE FROM saved_searches WHERE search_id=?',
                                  (search_id,)).rowcount == 0:
                raise ValueError('Saved search no longer exists')
        self._saved_searches.pop(search_id, None)

    @staticmethod
    def _validate_album_identity(expected_identity):
        if expected_identity is None:
            return
        from collections.abc import Mapping
        fields = {'st_dev', 'st_ino', 'size', 'mtime_ns'}
        if (not isinstance(expected_identity, Mapping)
                or set(expected_identity) != fields
                or any(type(value) is not int for value in expected_identity.values())
                or expected_identity['st_dev'] < 0
                or expected_identity['st_ino'] <= 0
                or expected_identity['size'] < 0):
            raise ValueError('Expected file identity requires four valid integer stat fields')

    def _album_media_evidence(self, path, expected_identity):
        from core.clip_files import is_library_media_path

        if not is_library_media_path(path):
            raise ValueError('Only completed library media can belong to an album')
        self._validate_album_identity(expected_identity)
        evidence = self.inspect_media(path)
        identity = dict(evidence.stat)
        if expected_identity is not None and not self._identity_matches(expected_identity, identity):
            raise OSError('media identity changed before album membership update')
        return evidence

    @_locked
    def add_album_member(self, album_id, path, expected_identity=None) -> bool:
        self._require_album_schema()
        from core.audio_manifest import manifest_path_for

        album_id = str(album_id)
        path = self._path_text(path)
        if path.casefold().endswith('.fthr-audio.json'):
            raise ValueError('audio manifests cannot be registered as primary clips')
        evidence = self._album_media_evidence(path, expected_identity)
        key = self._path_key(path)
        adjacent = manifest_path_for(path)
        with self._current_transaction() as connection:
            album_row = self._album_row(connection, album_id)
            if album_row is None:
                raise ValueError('Album no longer exists')
            self._assert_operation_available(connection, paths=(path, adjacent))
            rows = self._active_primary_rows(connection, key)
            if any(row[2] != FileRole.PRIMARY.value for row in rows):
                raise OSError('the selected path is an associated file, not a primary clip')
            if len(rows) > 1:
                raise OSError('conflicting media bindings block album membership')
            replacement = False
            if not rows:
                metadata = {}
                file = self._insert_primary(connection, path, metadata, evidence)
            else:
                file = self._assert_cached_primary_current(
                    connection, self._decode_file(rows[0]))
                if file.binding_state in (BindingState.CONFLICT, BindingState.TRASHED,
                                          BindingState.PURGING, BindingState.DETACHED):
                    raise OSError('the selected clip is not a confirmed active primary')
                if (file.binding_state in (BindingState.STAT_BOUND,
                                            BindingState.CONTENT_VERIFIED)
                        and not self._identity_matches(file.identity, dict(evidence.stat))):
                    replacement = True
                elif file.binding_state not in (BindingState.UNVERIFIED,
                                                BindingState.LEGACY_UNVERIFIED,
                                                BindingState.STAT_BOUND,
                                                BindingState.CONTENT_VERIFIED):
                    raise OSError('registered media identity changed; reload and register it again')
                self._assert_operation_available(connection, clip_ids=(file.clip_id,),
                                                 paths=(path, adjacent))
                metadata_row = connection.execute(
                    'SELECT value FROM entries WHERE clip_id=?', (file.clip_id,)).fetchone()
                metadata = json.loads(metadata_row[0]) if metadata_row else {}
                if replacement:
                    self._detach_primary(connection, file)
                    metadata = {}
                    file = self._insert_primary(connection, path, metadata, evidence)
                else:
                    self._check_primary_collision(connection, path, file_id=file.file_id)
                    if file.binding_state == BindingState.CONTENT_VERIFIED:
                        state = file.binding_state
                        content_sha256 = file.content_sha256
                        provenance = file.provenance
                    else:
                        state = (BindingState.CONTENT_VERIFIED
                                 if evidence.content_sha256 else BindingState.STAT_BOUND)
                        content_sha256 = evidence.content_sha256 or file.content_sha256
                        provenance = 'explicit album membership adoption'
                    file = replace(file, identity=dict(evidence.stat), binding_state=state,
                                   content_sha256=content_sha256, provenance=provenance)
                    self._write_file(connection, file)
            inserted = connection.execute(
                'INSERT OR IGNORE INTO album_members(album_id,clip_id) VALUES (?,?)',
                (album_id, file.clip_id)).rowcount == 1
            self._recheck(evidence)
        self._publish_primary_reconciliation(path, file, metadata)
        self._albums[album_id] = album_from_row(album_row)
        self._album_members.setdefault(album_id, set()).add(file.clip_id)
        return inserted

    @_locked
    def remove_album_member(self, album_id, path, expected_identity=None) -> bool:
        self._require_album_schema()
        from core.audio_manifest import manifest_path_for

        album_id = str(album_id)
        path = self._path_text(path)
        key = self._path_key(path)
        with self._current_transaction() as connection:
            album_row = self._album_row(connection, album_id)
            if album_row is None:
                raise ValueError('Album no longer exists')
            rows = self._active_primary_rows(connection, key)
            if not rows:
                self._albums[album_id] = album_from_row(album_row)
                return False
            if any(row[2] != FileRole.PRIMARY.value for row in rows) or len(rows) != 1:
                raise OSError('conflicting media bindings block album membership')
            file = self._assert_cached_primary_current(
                connection, self._decode_file(rows[0]))
            if file.binding_state not in (BindingState.STAT_BOUND,
                                           BindingState.CONTENT_VERIFIED):
                raise OSError('the selected clip is not a confirmed active primary')
            metadata_row = connection.execute(
                'SELECT value FROM entries WHERE clip_id=?', (file.clip_id,)).fetchone()
            if metadata_row is None:
                raise OSError('Registered clip metadata is missing')
            metadata = json.loads(metadata_row[0])
            if not isinstance(metadata, dict):
                raise ValueError('Invalid current clip metadata')
            evidence = self._album_media_evidence(path, expected_identity)
            if not self._identity_matches(file.identity, dict(evidence.stat)):
                raise OSError('registered media identity changed; reload and retry')
            adjacent = manifest_path_for(path)
            self._assert_operation_available(connection, clip_ids=(file.clip_id,),
                                             paths=(path, adjacent))
            removed = connection.execute(
                'DELETE FROM album_members WHERE album_id=? AND clip_id=?',
                (album_id, file.clip_id)).rowcount == 1
            self._recheck(evidence)
        self._publish_primary_reconciliation(
            path, file, metadata)
        self._albums[album_id] = album_from_row(album_row)
        members = self._album_members.setdefault(album_id, set())
        members.discard(file.clip_id)
        return removed

    def inspect_media(self, path, *, hash_content=False) -> FileEvidence:
        """Explicit caller-owned I/O; hashing is never done by get/constructor."""
        path = self._path_text(path)
        before = os.stat(path)
        if not stat_module.S_ISREG(before.st_mode):
            raise ValueError('media evidence requires a regular file')
        identity = self._stat_identity(before)
        digest = None
        if hash_content:
            hasher = hashlib.sha256()
            with open(path, 'rb') as source:
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    hasher.update(block)
            digest = hasher.hexdigest()
            if self._stat_identity(os.stat(path)) != identity:
                raise ValueError('media changed while hashing')
        return FileEvidence(path, identity, digest)

    @staticmethod
    def _stat_identity(stat) -> dict:
        return {'st_dev': stat.st_dev, 'st_ino': stat.st_ino,
                'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}

    def _recheck(self, evidence: FileEvidence):
        if not isinstance(evidence, FileEvidence):
            raise ValueError('file evidence is required')
        try:
            current = self.inspect_media(evidence.path)
        except OSError as error:
            raise ValueError('file evidence is no longer available') from error
        if dict(current.stat) != dict(evidence.stat):
            raise ValueError('file changed since evidence was collected')
        if evidence.content_sha256 is not None and (
                not isinstance(evidence.content_sha256, str)
                or not re.fullmatch('[0-9a-f]{64}', evidence.content_sha256)):
            raise ValueError('invalid content SHA-256')

    def _primary_candidates(self, path) -> list[ClipFile]:
        key = self._path_key(path)
        return [self._files[file_id] for file_id in self._primary_index.get(key, ())]

    def _stored_primary_path(self, path) -> str:
        path = self._path_text(path)
        candidates = self._primary_candidates(path)
        if len(candidates) > 1:
            raise ValueError('conflicting canonical media paths require explicit resolution')
        return candidates[0].path if candidates else path

    def _check_primary_collision(self, connection, path, *, file_id=None):
        rows = connection.execute(
            'SELECT file_id FROM clip_files WHERE path_key=? AND binding_state<>?',
            (self._path_key(path), BindingState.DETACHED.value))
        if any(existing_id != file_id for (existing_id,) in rows):
            raise FileExistsError('media path is already bound to another active file')

    def _observed_state(self, file: ClipFile) -> BindingState:
        if file.binding_state in (BindingState.UNVERIFIED, BindingState.LEGACY_UNVERIFIED,
                                  BindingState.CONFLICT, BindingState.DETACHED,
                                  BindingState.TRASHED, BindingState.PURGING):
            return file.binding_state
        identity = self._file_identity(file.path)
        if identity is None:
            return BindingState.MISSING
        if file.binding_state in (BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED):
            if file.identity is None or identity != dict(file.identity):
                return BindingState.DETACHED
        return file.binding_state

    @_locked
    def resolve_media(self, path) -> ClipBinding | None:
        self._require_read_current()
        path = self._path_text(path)
        candidates = self._primary_candidates(path)
        if not candidates:
            return None
        if len(candidates) > 1:
            return ClipBinding(None, None, path, BindingState.CONFLICT)
        file = candidates[0]
        if file.binding_state in (BindingState.TRASHED, BindingState.PURGING):
            return None
        # Stat the requested alias as well; a former symlink may now differ.
        state = self._observed_state(replace(file, path=path))
        file_id = (None if state in (BindingState.UNVERIFIED, BindingState.LEGACY_UNVERIFIED)
                   else file.file_id)
        return ClipBinding(file.clip_id, file_id, file.path, state)

    def _entry_at(self, path):
        if not self._schema_snapshot.writable:
            return {}
        if self._schema_snapshot.version in _CURRENT_SCHEMA_VERSIONS:
            binding = self.resolve_media(path)
            if binding is None or binding.state in (BindingState.DETACHED, BindingState.CONFLICT, BindingState.MISSING):
                return {}
            return self._id_data.get(binding.clip_id, {})
        return self._data.get(path, {})

    @_locked
    def get_by_id(self, clip_id) -> dict:
        self._require_read_current()
        return deepcopy(self._id_data.get(clip_id, {}))

    @_locked
    def files_for_clip(self, clip_id) -> tuple[ClipFile, ...]:
        self._require_read_current()
        return tuple(file for file in self._files.values() if file.clip_id == clip_id)

    @staticmethod
    def _restore_uuid(value, label):
        if not isinstance(value, str):
            raise ValueError(f'{label} must be a canonical UUID string')
        try:
            parsed = UUID(value)
        except (ValueError, AttributeError) as error:
            raise ValueError(f'{label} must be a canonical UUID string') from error
        if str(parsed) != value:
            raise ValueError(f'{label} must be a canonical UUID string')
        return value

    @staticmethod
    def _restore_digest(value, label):
        if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
            raise ValueError(f'{label} must be a lowercase SHA-256')
        return value

    @staticmethod
    def _restore_identity(value, label, *, allow_none=False):
        if value is None and allow_none:
            return None
        fields = {'st_dev', 'st_ino', 'size', 'mtime_ns'}
        if (not isinstance(value, dict) or set(value) != fields
                or any(type(item) is not int for item in value.values())
                or value['st_dev'] < 0 or value['st_ino'] <= 0
                or value['size'] < 0):
            raise ValueError(f'{label} must contain four valid stat fields')
        return {field: value[field] for field in fields}

    @staticmethod
    def _restore_json_object(value, label):
        if not isinstance(value, dict):
            raise ValueError(f'{label} must be a JSON object')
        try:
            json.dumps(value, ensure_ascii=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError) as error:
            raise ValueError(f'{label} contains unsupported JSON') from error
        return value

    @staticmethod
    def _restore_path_identity(path, label):
        try:
            observed = os.lstat(path)
        except OSError as error:
            raise OSError(f'{label} is missing or inaccessible') from error
        attributes = getattr(observed, 'st_file_attributes', 0)
        reparse = getattr(stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        if (stat_module.S_ISLNK(observed.st_mode)
                or bool(attributes & reparse)
                or not stat_module.S_ISREG(observed.st_mode)):
            raise OSError(f'{label} must be a regular non-reparse file')
        return {
            'st_dev': observed.st_dev,
            'st_ino': observed.st_ino,
            'size': observed.st_size,
            'mtime_ns': observed.st_mtime_ns,
        }

    @staticmethod
    def _restore_root_path(value, label):
        try:
            raw = os.fspath(value)
        except TypeError as error:
            raise ValueError(f'{label} must be a filesystem path') from error
        if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
            raise ValueError(f'{label} must be a filesystem path')
        return os.path.abspath(raw)

    @staticmethod
    def _restore_under(path, root):
        try:
            return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
        except ValueError:
            # Different Windows drives cannot share an owned restore root.
            return False

    @staticmethod
    def _restore_reject_reparse_parents(path, root):
        current = Path(os.path.abspath(path))
        root = Path(os.path.abspath(root))
        while True:
            if current.exists() or current.is_symlink():
                try:
                    info = os.lstat(current)
                except OSError as error:
                    raise OSError('restore path parent is unavailable') from error
                attributes = getattr(info, 'st_file_attributes', 0)
                reparse = getattr(stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
                if stat_module.S_ISLNK(info.st_mode) or bool(attributes & reparse):
                    raise OSError('restore path contains a reparse point')
            if current == root:
                return
            if current.parent == current:
                raise OSError('restore path escapes its owned root')
            current = current.parent

    def _restore_validate_roots(self, destination_root, owned_root, imported_roots,
                                expected_root):
        destination_root = self._restore_root_path(destination_root, 'destination_root')
        owned_root = self._restore_root_path(owned_root, 'owned_root')
        imported_roots = tuple(
            self._restore_root_path(root, 'imported_root') for root in imported_roots)
        if not self._restore_under(destination_root, owned_root) or destination_root == owned_root:
            raise OSError('restore destination must be a new subfolder of the owned root')
        if any(self._restore_under(destination_root, root)
               or self._restore_under(root, destination_root)
               for root in imported_roots):
            raise OSError('restore destination overlaps an imported root')
        self._restore_reject_reparse_parents(destination_root, owned_root)
        if os.path.lexists(destination_root) and not os.path.isdir(destination_root):
            raise OSError('restore destination is not a directory')
        try:
            owned_identity = os.lstat(owned_root)
        except OSError as error:
            raise OSError('owned root is unavailable') from error
        attributes = getattr(owned_identity, 'st_file_attributes', 0)
        reparse = getattr(stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
        if (stat_module.S_ISLNK(owned_identity.st_mode)
                or bool(attributes & reparse)
                or not stat_module.S_ISDIR(owned_identity.st_mode)):
            raise OSError('owned root is not a regular directory')
        if expected_root is not None:
            if isinstance(expected_root, dict):
                if not isinstance(expected_root, dict) or not {
                        'st_dev', 'st_ino'} <= set(expected_root):
                    raise ValueError('expected_root identity is invalid')
                if any(type(expected_root.get(key)) is not int
                       for key in ('st_dev', 'st_ino')):
                    raise ValueError('expected_root identity is invalid')
                actual = {
                    'st_dev': owned_identity.st_dev, 'st_ino': owned_identity.st_ino,
                    'size': owned_identity.st_size, 'mtime_ns': owned_identity.st_mtime_ns,
                }
                if ((expected_root['st_dev'], expected_root['st_ino'])
                        != (actual['st_dev'], actual['st_ino'])):
                    raise OSError('owned root identity changed')
                if any(key in expected_root and expected_root[key] != actual[key]
                       for key in ('size', 'mtime_ns')):
                    raise OSError('owned root identity changed')
            elif self._restore_root_path(expected_root, 'expected_root') != owned_root:
                raise OSError('restore owned root changed')
        return destination_root, owned_root, imported_roots

    @_locked
    def export_backup_snapshot(self) -> dict:
        """Export logical metadata from one consistent read transaction."""
        self._require_read_current()
        version = self._schema_snapshot.version
        if version < _TRASH_SCHEMA_VERSIONS[0]:
            raise RuntimeError('explicit trash schema migration is required for backup export')
        try:
            with self._read_connection() as connection:
                connection.execute('BEGIN')
                observed_version = connection.execute('PRAGMA user_version').fetchone()[0]
                if observed_version != version or observed_version not in _CURRENT_SCHEMA_VERSIONS:
                    raise OSError('metadata schema changed during backup snapshot')
                if observed_version >= _RESTORE_RECEIPT_SCHEMA_VERSION:
                    receipt_columns = [row[1] for row in connection.execute(
                        'PRAGMA table_info(restore_receipts)')]
                    if receipt_columns != [
                            'restore_uuid', 'manifest_sha256', 'destination_key',
                            'destination_root', 'committed_utc']:
                        raise OSError('version 6 restore receipt catalog is missing or invalid')
                if observed_version >= _JOURNAL_SCHEMA_VERSIONS[0]:
                    unfinished = connection.execute(
                        "SELECT 1 FROM file_operations WHERE state NOT IN "
                        "('COMMITTED','ROLLED_BACK') OR cleanup_done=0 LIMIT 1"
                    ).fetchone()
                    if unfinished:
                        raise OSError('unfinished file operation blocks backup snapshot')
                entries = []
                for clip_id, path, raw_value in connection.execute(
                        'SELECT clip_id,path,value FROM entries ORDER BY clip_id'):
                    try:
                        value = json.loads(raw_value)
                    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
                        raise OSError('backup snapshot contains invalid clip metadata') from error
                    entries.append({'clip_id': clip_id, 'path': path,
                                    'value': self._restore_json_object(value, 'clip metadata')})
                files = []
                for row in connection.execute(
                        'SELECT file_id,clip_id,role,path,path_key,binding_state,'
                        'identity_json,content_sha256,provenance FROM clip_files '
                        'ORDER BY file_id'):
                    try:
                        identity = json.loads(row[6]) if row[6] is not None else None
                    except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
                        raise OSError('backup snapshot contains invalid file identity') from error
                    identity = self._restore_identity(identity, 'file identity', allow_none=True)
                    if row[7] is not None:
                        self._restore_digest(row[7], 'file content_sha256')
                    files.append({
                        'file_id': row[0], 'clip_id': row[1], 'role': row[2],
                        'path': row[3], 'path_key': row[4], 'binding_state': row[5],
                        'identity': identity, 'content_sha256': row[7],
                        'provenance': row[8],
                    })
                trash = []
                if observed_version in _TRASH_SCHEMA_VERSIONS:
                    for row in connection.execute(
                            'SELECT trash_id,clip_id,status,created_utc,original_path,'
                            'stored_path,members_json,error FROM trash_items '
                            'ORDER BY created_utc,trash_id'):
                        if row[2] == 'PURGING':
                            raise OSError('purging trash item blocks backup snapshot')
                        try:
                            members = json.loads(row[6])
                        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
                            raise OSError('backup snapshot contains invalid trash members') from error
                        if not isinstance(members, (list, dict)):
                            raise OSError('backup snapshot contains invalid trash members')
                        trash.append({
                            'trash_id': row[0], 'clip_id': row[1], 'status': row[2],
                            'created_utc': row[3], 'original_path': row[4],
                            'stored_path': row[5], 'members': members, 'error': row[7],
                        })
                albums = []
                memberships = []
                if observed_version >= _ALBUM_SCHEMA_VERSION:
                    albums = [
                        {'album_id': row[0], 'name': row[1], 'created_utc': row[2]}
                        for row in connection.execute(
                            'SELECT album_id,name,created_utc FROM albums '
                            'ORDER BY created_utc,album_id')]
                    memberships = [
                        {'album_id': row[0], 'clip_id': row[1]}
                        for row in connection.execute(
                            'SELECT album_id,clip_id FROM album_members '
                            'ORDER BY album_id,clip_id')]
                searches = []
                if observed_version >= _SAVED_SEARCH_SCHEMA_VERSION:
                    for row in connection.execute(
                            'SELECT search_id,name,created_utc,criteria_json FROM saved_searches '
                            'ORDER BY created_utc,search_id'):
                        try:
                            criteria = json.loads(row[3])
                        except (TypeError, ValueError, json.JSONDecodeError, RecursionError) as error:
                            raise OSError('backup snapshot contains invalid search criteria') from error
                        searches.append({
                            'search_id': row[0], 'name': row[1],
                            'created_utc': row[2],
                            'criteria': self._restore_json_object(criteria, 'search criteria'),
                        })
            return {
                'schema_version': observed_version,
                'entries': entries,
                'files': files,
                'trash': trash,
                'albums': albums,
                'memberships': memberships,
                'searches': searches,
            }
        except sqlite3.Error as error:
            raise OSError(f'Could not export backup snapshot: {error}') from error

    @_locked
    def get_restore_receipt(self, restore_uuid):
        """Return one committed restore receipt, or None when absent."""
        restore_uuid = self._restore_uuid(restore_uuid, 'restore_uuid')
        self._require_restore_schema()
        with self._read_connection() as connection:
            row = connection.execute(
                'SELECT restore_uuid,manifest_sha256,destination_key,destination_root,'
                'committed_utc FROM restore_receipts WHERE restore_uuid=?',
                (restore_uuid,)).fetchone()
        if row is None:
            return None
        return dict(zip(('restore_uuid', 'manifest_sha256', 'destination_key',
                         'destination_root', 'committed_utc'), row, strict=True))

    def _require_restore_schema(self):
        schema = self._writable_schema()
        if schema.version != _RESTORE_RECEIPT_SCHEMA_VERSION:
            raise RuntimeError('explicit restore receipt schema migration is required')

    def _prepare_restore_records(self, connection, records, destination_root,
                                 owned_root, imported_roots):
        if not isinstance(records, dict):
            raise TypeError('restore records must be a mapping')
        categories = ('entries', 'files', 'trash', 'albums', 'memberships', 'searches')
        if set(records) != set(categories):
            raise ValueError('restore records have unknown or missing categories')
        if any(not isinstance(records[name], list) for name in categories):
            raise TypeError('restore record categories must be lists')
        prepared = {name: [] for name in categories}
        entry_ids = set()
        entry_paths = set()
        for raw in records['entries']:
            if not isinstance(raw, dict) or not {'clip_id', 'path', 'value'} <= set(raw):
                raise ValueError('invalid restore entry record')
            clip_id = self._restore_uuid(raw['clip_id'], 'clip_id')
            if clip_id in entry_ids:
                raise ValueError('duplicate restore clip id')
            entry_ids.add(clip_id)
            value = self._restore_json_object(raw['value'], 'restore metadata')
            path = raw['path']
            if path is not None:
                path = self._restore_root_path(path, 'restore entry path')
                if not self._restore_under(path, destination_root):
                    raise OSError('active restore entry is outside the destination root')
                self._restore_reject_reparse_parents(path, destination_root)
                key = self._path_key(path)
                if key in entry_paths:
                    raise FileExistsError('duplicate restore entry path')
                entry_paths.add(key)
            prepared['entries'].append({'clip_id': clip_id, 'path': path, 'value': value})
        file_ids = set()
        file_paths = set()
        files_by_clip = {}
        for raw in records['files']:
            required = {'file_id', 'clip_id', 'role', 'path', 'path_key', 'binding_state',
                        'identity', 'content_sha256', 'provenance'}
            if not isinstance(raw, dict) or not required <= set(raw):
                raise ValueError('invalid restore file record')
            file_id = self._restore_uuid(raw['file_id'], 'file_id')
            clip_id = self._restore_uuid(raw['clip_id'], 'file clip_id')
            if clip_id not in entry_ids or file_id in file_ids:
                raise ValueError('restore file references an unknown or duplicate clip')
            file_ids.add(file_id)
            if raw['role'] not in {role.value for role in FileRole}:
                raise ValueError('invalid restore file role')
            if raw['binding_state'] not in {state.value for state in BindingState}:
                raise ValueError('invalid restore file binding state')
            if raw['binding_state'] not in {
                    BindingState.STAT_BOUND.value,
                    BindingState.CONTENT_VERIFIED.value,
                    BindingState.TRASHED.value}:
                raise ValueError('restore files must be freshly verified active or trash members')
            path = self._restore_root_path(raw['path'], 'restore file path')
            if not self._restore_under(path, destination_root):
                raise OSError('restore file is outside the destination root')
            self._restore_reject_reparse_parents(path, destination_root)
            path_key = self._path_key(path)
            if path_key != raw['path_key']:
                raise ValueError('restore file path_key does not match path')
            if path_key in file_paths:
                raise FileExistsError('duplicate restore file path')
            file_paths.add(path_key)
            identity = self._restore_identity(raw['identity'], 'restore file identity')
            if self._restore_path_identity(path, 'restore file') != identity:
                raise OSError('restore file identity changed before metadata commit')
            digest = raw['content_sha256']
            if digest is not None:
                self._restore_digest(digest, 'restore file content_sha256')
            provenance = raw['provenance']
            if provenance is not None and not isinstance(provenance, str):
                raise ValueError('restore file provenance must be text or null')
            row = {
                'file_id': file_id, 'clip_id': clip_id, 'role': raw['role'],
                'path': path, 'path_key': path_key,
                'binding_state': raw['binding_state'], 'identity': identity,
                'content_sha256': digest, 'provenance': provenance,
            }
            prepared['files'].append(row)
            files_by_clip.setdefault(clip_id, []).append(row)
        entry_by_id = {row['clip_id']: row for row in prepared['entries']}
        for clip_id, entry in entry_by_id.items():
            clip_files = files_by_clip.get(clip_id, [])
            primaries = [row for row in clip_files if row['role'] == FileRole.PRIMARY.value]
            if len(primaries) != 1:
                raise ValueError('each restored clip requires exactly one primary file')
            if entry['path'] is None:
                if any(row['binding_state'] != BindingState.TRASHED.value
                       for row in clip_files):
                    raise ValueError('trash restore files must be marked TRASHED')
            elif primaries[0]['path'] != entry['path']:
                raise ValueError('restore entry path does not match its primary file')
        trash_ids = set()
        for raw in records['trash']:
            required = {'trash_id', 'clip_id', 'status', 'created_utc', 'original_path',
                        'stored_path', 'members', 'error'}
            if not isinstance(raw, dict) or not required <= set(raw):
                raise ValueError('invalid restore trash record')
            trash_id = self._restore_uuid(raw['trash_id'], 'trash_id')
            clip_id = self._restore_uuid(raw['clip_id'], 'trash clip_id')
            if trash_id in trash_ids or clip_id not in entry_ids:
                raise ValueError('restore trash references an unknown or duplicate id')
            trash_ids.add(trash_id)
            if raw['status'] not in {'TRASHED', 'RESTORED', 'PURGED'}:
                raise ValueError('unfinished trash cannot be restored')
            if not isinstance(raw['created_utc'], str) or not raw['created_utc']:
                raise ValueError('invalid restore trash timestamp')
            original_path = self._restore_root_path(raw['original_path'], 'trash original_path')
            stored_path = self._restore_root_path(raw['stored_path'], 'trash stored_path')
            if (not self._restore_under(original_path, destination_root)
                    or not self._restore_under(stored_path, destination_root)):
                raise OSError('restore trash paths are outside the destination root')
            self._restore_reject_reparse_parents(original_path, destination_root)
            self._restore_reject_reparse_parents(stored_path, destination_root)
            if not isinstance(raw['members'], (list, dict)):
                raise ValueError('trash members must be a JSON list or object')
            self._restore_json_object({'members': raw['members']}, 'trash members')
            if raw['error'] is not None and not isinstance(raw['error'], str):
                raise ValueError('trash error must be text or null')
            if raw['status'] == 'TRASHED' and entry_by_id[clip_id]['path'] is not None:
                raise ValueError('TRASHED restore entries must have no active path')
            if raw['status'] == 'TRASHED':
                trash_folder = os.path.join(destination_root, '.fthr-trash', trash_id)
                primary = next(row for row in files_by_clip[clip_id]
                               if row['role'] == FileRole.PRIMARY.value)
                if stored_path != primary['path']:
                    raise ValueError('TRASHED stored_path must equal the primary path')
                all_trash_root = os.path.join(destination_root, '.fthr-trash')
                if self._restore_under(original_path, all_trash_root):
                    raise OSError('trash original_path must be outside the trash bundle')
                if not self._restore_under(stored_path, trash_folder):
                    raise OSError('TRASHED restore members must stay in the nested trash root')
                for member in files_by_clip[clip_id]:
                    if self._path_key(os.path.dirname(member['path'])) != self._path_key(trash_folder):
                        raise OSError('TRASHED restore files must be direct trash members')
                    if member['role'] not in {FileRole.PRIMARY.value, FileRole.SIDECAR.value}:
                        raise ValueError('TRASHED bundles cannot contain derived files')
                    if (member['role'] == FileRole.SIDECAR.value
                            and member['path'] != primary['path'] + '.fthr-audio.json'):
                        raise ValueError('TRASHED sidecar is not adjacent to the primary')
                canonical_members = [{
                    key: member[key] for key in (
                        'file_id', 'role', 'path', 'path_key', 'binding_state',
                        'identity', 'content_sha256', 'provenance')
                } for member in sorted(files_by_clip[clip_id], key=lambda item: item['file_id'])]
                if raw['members'] != canonical_members:
                    raise ValueError('TRASHED members do not match canonical file bindings')
            elif entry_by_id[clip_id]['path'] is None:
                raise ValueError('NULL restore entries require a current TRASHED item')
            prepared['trash'].append({
                'trash_id': trash_id, 'clip_id': clip_id, 'status': raw['status'],
                'created_utc': raw['created_utc'], 'original_path': original_path,
                'stored_path': stored_path, 'members': raw['members'], 'error': raw['error'],
            })
        for clip_id, entry in entry_by_id.items():
            clip_files = files_by_clip[clip_id]
            current_trash = [row for row in prepared['trash']
                             if row['clip_id'] == clip_id and row['status'] == 'TRASHED']
            if entry['path'] is None:
                if len(current_trash) != 1 or any(
                        row['binding_state'] != BindingState.TRASHED.value
                        for row in clip_files):
                    raise ValueError('NULL restore entries require exactly one current TRASHED item')
            else:
                if current_trash or any(row['binding_state'] not in {
                        BindingState.STAT_BOUND.value, BindingState.CONTENT_VERIFIED.value}
                        for row in clip_files):
                    raise ValueError('active restore entries cannot contain TRASHED files')
                if any(row['status'] == 'TRASHED' for row in prepared['trash']
                       if row['clip_id'] == clip_id):
                    raise ValueError('active restore entries cannot contain a TRASHED item')
        album_ids = set()
        album_map = {}
        used_album_names = {
            row[0] for row in connection.execute('SELECT name_key FROM albums')
        }
        for raw in records['albums']:
            if not isinstance(raw, dict) or not {'album_id', 'name', 'created_utc'} <= set(raw):
                raise ValueError('invalid restore album record')
            album_id = self._restore_uuid(raw['album_id'], 'album_id')
            if album_id in album_ids:
                raise ValueError('duplicate restore album id')
            album_ids.add(album_id)
            display, name_key = normalize_album_name(raw['name'])
            base = display
            suffix = 0
            while name_key in used_album_names:
                suffix += 1
                marker = f' (restored {suffix})'
                display, name_key = normalize_album_name(
                    base[:80 - len(marker)].rstrip() + marker)
                if suffix > 10000:
                    raise ValueError('unable to allocate a unique restored album name')
            used_album_names.add(name_key)
            if not isinstance(raw['created_utc'], str) or not raw['created_utc']:
                raise ValueError('invalid restore album timestamp')
            album_map[album_id] = album_id
            prepared['albums'].append({
                'album_id': album_id, 'name': display, 'name_key': name_key,
                'created_utc': raw['created_utc'],
            })
        for raw in records['memberships']:
            if not isinstance(raw, dict) or not {'album_id', 'clip_id'} <= set(raw):
                raise ValueError('invalid restore album membership')
            album_id = self._restore_uuid(raw['album_id'], 'membership album_id')
            clip_id = self._restore_uuid(raw['clip_id'], 'membership clip_id')
            if album_id not in album_ids or clip_id not in entry_ids:
                raise ValueError('restore membership references an unknown id')
            prepared['memberships'].append({'album_id': album_id, 'clip_id': clip_id})
        search_ids = set()
        used_search_names = {
            row[0] for row in connection.execute('SELECT name_key FROM saved_searches')
        }
        for raw in records['searches']:
            if not isinstance(raw, dict) or not {'search_id', 'name', 'created_utc', 'criteria'} <= set(raw):
                raise ValueError('invalid restore saved search record')
            search_id = self._restore_uuid(raw['search_id'], 'search_id')
            if search_id in search_ids:
                raise ValueError('duplicate restore search id')
            search_ids.add(search_id)
            display, name_key = normalize_search_name(raw['name'])
            base = display
            suffix = 0
            while name_key in used_search_names:
                suffix += 1
                marker = f' (restored {suffix})'
                display, name_key = normalize_search_name(
                    base[:80 - len(marker)].rstrip() + marker)
                if suffix > 10000:
                    raise ValueError('unable to allocate a unique restored search name')
            used_search_names.add(name_key)
            criteria = self._restore_json_object(raw['criteria'], 'restore search criteria')
            criteria = dict(criteria)
            album_id = criteria.get('album_id')
            if album_id is not None:
                self._restore_uuid(album_id, 'search album_id')
                if album_id in album_map:
                    criteria['album_id'] = album_map[album_id]
            criteria = criteria_to_dict(normalize_search_criteria(criteria))
            if not isinstance(raw['created_utc'], str) or not raw['created_utc']:
                raise ValueError('invalid restore search timestamp')
            prepared['searches'].append({
                'search_id': search_id, 'name': display, 'name_key': name_key,
                'created_utc': raw['created_utc'], 'criteria': criteria,
            })
        existing_entry_ids = {row[0] for row in connection.execute('SELECT clip_id FROM entries')}
        if entry_ids.intersection(existing_entry_ids):
            raise FileExistsError('restore clip id already exists')
        existing_file_ids = {row[0] for row in connection.execute('SELECT file_id FROM clip_files')}
        if file_ids.intersection(existing_file_ids):
            raise FileExistsError('restore file id already exists')
        existing_paths = {row[0] for row in connection.execute(
            'SELECT path_key FROM clip_files WHERE binding_state<>?',
            (BindingState.DETACHED.value,))}
        if existing_paths.intersection(file_paths):
            raise FileExistsError('restore path is already bound to an existing file')
        existing_entry_paths = {
            self._path_key(row[0]) for row in connection.execute(
                'SELECT path FROM entries WHERE path IS NOT NULL')
        }
        if existing_entry_paths.intersection(entry_paths):
            raise FileExistsError('restore path is already used by an existing entry')
        for table, ids in (('albums', album_ids), ('saved_searches', search_ids),
                           ('trash_items', trash_ids)):
            if ids:
                existing = {row[0] for row in connection.execute(
                    f'SELECT {"album_id" if table == "albums" else "search_id" if table == "saved_searches" else "trash_id"} FROM {table}')}
                if ids.intersection(existing):
                    raise FileExistsError(f'restore {table} id already exists')
        return prepared

    @_locked
    def commit_backup_restore(self, restore_uuid, manifest_sha256, destination_root,
                              records, *, owned_root, imported_roots=(), expected_root=None):
        """Atomically insert verified, freshly rebased restore metadata."""
        restore_uuid = self._restore_uuid(restore_uuid, 'restore_uuid')
        manifest_sha256 = self._restore_digest(manifest_sha256, 'manifest_sha256')
        if isinstance(imported_roots, (str, bytes)):
            raise TypeError('imported_roots must be an iterable of paths')
        destination_root, owned_root, imported_roots = self._restore_validate_roots(
            destination_root, owned_root, imported_roots, expected_root)
        self._require_restore_schema()
        destination_key = self._path_key(destination_root)
        with self._current_transaction() as connection:
            receipt = connection.execute(
                'SELECT restore_uuid,manifest_sha256,destination_key,destination_root,'
                'committed_utc FROM restore_receipts WHERE restore_uuid=?',
                (restore_uuid,)).fetchone()
            if receipt is not None:
                if tuple(receipt[1:3]) != (manifest_sha256, destination_key):
                    raise ValueError('restore UUID is already committed with different content')
                connection.commit()
                self.reload()
                return {
                    'already_imported': True, 'restore_uuid': receipt[0],
                    'manifest_sha256': receipt[1], 'destination_key': receipt[2],
                    'destination_root': receipt[3], 'committed_utc': receipt[4],
                }
            prepared = self._prepare_restore_records(
                connection, records, destination_root, owned_root, imported_roots)
            for row in prepared['entries']:
                connection.execute(
                    'INSERT INTO entries(clip_id,path,value) VALUES (?,?,?)',
                    (row['clip_id'], row['path'],
                     json.dumps(row['value'], ensure_ascii=True, allow_nan=False,
                                separators=(',', ':'))))
            for row in prepared['files']:
                connection.execute(
                    'INSERT INTO clip_files(file_id,clip_id,role,path,path_key,binding_state,'
                    'identity_json,content_sha256,provenance) VALUES (?,?,?,?,?,?,?,?,?)',
                    (row['file_id'], row['clip_id'], row['role'], row['path'],
                     row['path_key'], row['binding_state'],
                     json.dumps(row['identity'], separators=(',', ':')),
                     row['content_sha256'], row['provenance']))
            for row in prepared['trash']:
                connection.execute(
                    'INSERT INTO trash_items(trash_id,clip_id,status,created_utc,original_path,'
                    'stored_path,members_json,error) VALUES (?,?,?,?,?,?,?,?)',
                    (row['trash_id'], row['clip_id'], row['status'], row['created_utc'],
                     row['original_path'], row['stored_path'],
                     json.dumps(row['members'], ensure_ascii=True, allow_nan=False,
                                separators=(',', ':')), row['error']))
            for row in prepared['albums']:
                connection.execute(
                    'INSERT INTO albums(album_id,name,name_key,created_utc) VALUES (?,?,?,?)',
                    (row['album_id'], row['name'], row['name_key'], row['created_utc']))
            for row in prepared['memberships']:
                connection.execute(
                    'INSERT INTO album_members(album_id,clip_id) VALUES (?,?)',
                    (row['album_id'], row['clip_id']))
            for row in prepared['searches']:
                connection.execute(
                    'INSERT INTO saved_searches(search_id,name,name_key,created_utc,criteria_json) '
                    'VALUES (?,?,?,?,?)',
                    (row['search_id'], row['name'], row['name_key'], row['created_utc'],
                     json.dumps(row['criteria'], ensure_ascii=True, allow_nan=False,
                                separators=(',', ':'))))
            committed_utc = datetime.now(timezone.utc).isoformat()
            connection.execute(
                'INSERT INTO restore_receipts(restore_uuid,manifest_sha256,destination_key,'
                'destination_root,committed_utc) VALUES (?,?,?,?,?)',
                (restore_uuid, manifest_sha256, destination_key, destination_root,
                 committed_utc))
        self.reload()
        return {
            'already_imported': False, 'restore_uuid': restore_uuid,
            'manifest_sha256': manifest_sha256, 'destination_key': destination_key,
            'destination_root': destination_root, 'committed_utc': committed_utc,
            'clip_ids': sorted(row['clip_id'] for row in prepared['entries']),
            'file_ids': sorted(row['file_id'] for row in prepared['files']),
        }

    @_locked
    def export_members_snapshot(self, path) -> tuple[ClipFile, ...]:
        """Read authoritative active export bindings without modifying the store."""
        from core.audio_manifest import manifest_path_for
        media = self._path_text(path)
        if not self._database.is_file():
            raise OSError('Library metadata is unavailable for original export')
        try:
            with closing(sqlite3.connect(self._database.absolute().as_uri() + '?mode=ro', uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute('BEGIN')
                version = connection.execute('PRAGMA user_version').fetchone()[0]
                if version > _CURRENT_SCHEMA_MAX:
                    raise UnsupportedSchemaVersion(f'unsupported metadata schema version {version}')
                if version not in _CURRENT_SCHEMA_VERSIONS:
                    raise OSError('Original export requires initialized library metadata')
                rows = connection.execute(
                    'SELECT * FROM clip_files WHERE path_key=? AND binding_state<>?',
                    (self._path_key(media), BindingState.DETACHED.value)).fetchall()
                if len(rows) > 1:
                    raise OSError('Conflicting library bindings prevent original export')
                primary = self._decode_file(rows[0]) if rows else None
                if primary is not None and (primary.role != FileRole.PRIMARY
                                            or primary.binding_state == BindingState.CONFLICT):
                    raise OSError('The selected path is not an unambiguous primary clip')
                self._assert_operation_available(
                    connection, paths=(media, manifest_path_for(media)),
                    clip_ids=(primary.clip_id,) if primary else ())
                if primary is None:
                    return ()
                members = tuple(self._decode_file(row) for row in connection.execute(
                    'SELECT * FROM clip_files WHERE clip_id=? AND binding_state<>? ORDER BY file_id',
                    (primary.clip_id, BindingState.DETACHED.value)))
                if any(member.binding_state == BindingState.CONFLICT for member in members):
                    raise OSError('Conflicting clip members prevent original export')
                return members
        except (sqlite3.Error, ValueError, TypeError) as error:
            raise OSError(f'Could not inspect original export members: {error}') from error

    @contextmanager
    def _current_transaction(self):
        self._require_current()
        try:
            with closing(sqlite3.connect(self._database)) as connection:
                connection.execute('PRAGMA foreign_keys=ON')
                connection.execute('PRAGMA synchronous=FULL')
                connection.execute('BEGIN IMMEDIATE')
                with connection:
                    version = connection.execute('PRAGMA user_version').fetchone()[0]
                    if version > _CURRENT_SCHEMA_MAX:
                        self._schema_snapshot = SchemaState(version, 'future', False)
                        raise UnsupportedSchemaVersion(f'unsupported metadata schema version {version}')
                    if version not in _CURRENT_SCHEMA_VERSIONS:
                        raise RuntimeError('metadata schema changed; reopen the manager')
                    yield connection
        except sqlite3.Error as error:
            raise OSError(f'Could not save clip metadata: {error}') from error

    def _insert_primary(self, connection, path, metadata, evidence=None) -> ClipFile:
        self._assert_operation_available(connection, paths=(path,))
        self._check_primary_collision(connection, path)
        clip_id, file_id = str(uuid4()), str(uuid4())
        state = (BindingState.CONTENT_VERIFIED if evidence.content_sha256 else BindingState.STAT_BOUND
                 ) if evidence is not None else BindingState.UNVERIFIED
        file = ClipFile(file_id, clip_id, FileRole.PRIMARY, path, self._path_key(path), state,
                        dict(evidence.stat) if evidence is not None else None,
                        evidence.content_sha256 if evidence is not None else None,
                        'explicit registration' if evidence is not None else 'metadata only')
        connection.execute('INSERT INTO entries VALUES (?,?,?)',
                           (clip_id, path, json.dumps(metadata, separators=(',', ':'))))
        self._write_file(connection, file)
        return file

    def _assert_cached_primary_current(self, connection, file):
        row = connection.execute('SELECT * FROM clip_files WHERE file_id=?', (file.file_id,)).fetchone()
        entry = connection.execute('SELECT path FROM entries WHERE clip_id=?', (file.clip_id,)).fetchone()
        current = self._decode_file(row) if row is not None else None
        if (current is None or replace(current, provenance=None) != replace(file, provenance=None)
                or entry is None or entry[0] != file.path):
            raise OSError('Cached clip binding changed; reload before modifying this file')
        return current

    def _detach_primary(self, connection, file):
        file = self._assert_cached_primary_current(connection, file)
        connection.execute('UPDATE entries SET path=NULL WHERE clip_id=?', (file.clip_id,))
        detached = replace(file, binding_state=BindingState.DETACHED)
        self._write_file(connection, detached)
        return detached

    def _publish_primary_reconciliation(self, path, file, metadata):
        """Publish a fresh primary and retire stale same-path RAM candidates."""
        for cached in tuple(self._primary_candidates(path)):
            if cached.file_id != file.file_id:
                self._publish_file(replace(cached, binding_state=BindingState.DETACHED), _DELETED)
        self._publish_file(file, metadata)

    def _publish_file(self, file: ClipFile, metadata):
        previous = self._files.get(file.file_id)
        if previous is not None and previous.role == FileRole.PRIMARY:
            ids = self._primary_index.get(previous.path_key)
            if ids is not None:
                ids.discard(previous.file_id)
                if not ids:
                    self._primary_index.pop(previous.path_key, None)
        self._files[file.file_id] = file
        if file.role == FileRole.PRIMARY and file.binding_state != BindingState.DETACHED:
            self._primary_index.setdefault(file.path_key, set()).add(file.file_id)
        if file.role == FileRole.PRIMARY:
            if file.binding_state == BindingState.DETACHED:
                self._path_ids.pop(file.path, None)
                self._data.pop(file.path_key, None)
            else:
                self._path_ids[file.path] = file.clip_id
                if metadata is not _DELETED:
                    self._id_data[file.clip_id] = metadata
                self._data[file.path_key] = self._id_data[file.clip_id]

    @_locked
    def register_media(self, evidence: FileEvidence, *, adopt_legacy=False) -> ClipBinding:
        self._require_current()
        self._recheck(evidence)
        candidates = self._primary_candidates(evidence.path)
        if len(candidates) > 1:
            if adopt_legacy:
                raise ValueError('canonical legacy conflicts cannot be adopted automatically')
            return ClipBinding(None, None, evidence.path, BindingState.CONFLICT)
        old = candidates[0] if candidates else None
        if old is not None and old.binding_state in (
                BindingState.UNVERIFIED, BindingState.LEGACY_UNVERIFIED, BindingState.CONFLICT):
            if not adopt_legacy or old.binding_state == BindingState.CONFLICT:
                return ClipBinding(old.clip_id, None, old.path, old.binding_state)
            replacement = False
        else:
            replacement = old is not None and (
                dict(old.identity or {}) != dict(evidence.stat)
                or (evidence.content_sha256 is not None and old.content_sha256 is not None
                    and old.content_sha256 != evidence.content_sha256))
        detached = None
        with self._current_transaction() as connection:
            self._assert_operation_available(
                connection, clip_ids=(old.clip_id,) if old else (), paths=(evidence.path,))
            self._recheck(evidence)
            if old is not None:
                old = self._assert_cached_primary_current(connection, old)
            if old is None or replacement:
                if old is not None:
                    detached = self._detach_primary(connection, old)
                file = self._insert_primary(connection, evidence.path, {}, evidence)
            else:
                self._check_primary_collision(connection, evidence.path, file_id=old.file_id)
                state = (BindingState.CONTENT_VERIFIED if evidence.content_sha256
                         else old.binding_state if old.binding_state == BindingState.CONTENT_VERIFIED
                         else BindingState.STAT_BOUND)
                file = replace(old, identity=dict(evidence.stat), binding_state=state,
                               content_sha256=evidence.content_sha256 or old.content_sha256,
                               provenance='explicit legacy adoption' if adopt_legacy else old.provenance)
                self._write_file(connection, file)
            self._recheck(evidence)
        if detached is not None:
            self._publish_file(detached, _DELETED)
        self._publish_file(file, {} if old is None or replacement else self._id_data[old.clip_id])
        return ClipBinding(file.clip_id, file.file_id, file.path, file.binding_state)

    def _save_current(self, paths, staged, *, prune_ids=(), owned_fields=None):
        changed = self._data.keys() | staged.keys() if paths is None else set(paths)
        publications = []
        pruned_metadata = {}
        with self._current_transaction() as connection:
            writer_schema_version = connection.execute(
                'PRAGMA user_version').fetchone()[0]
            for path in changed:
                path = self._path_text(path)
                metadata = staged.get(path, self._data.get(path, _DELETED))
                # A registered clip survives removal of its final editor draft.
                metadata = {} if metadata is _DELETED else metadata
                candidates = self._primary_candidates(path)
                if len(candidates) > 1:
                    raise ValueError('conflicting canonical media paths require explicit resolution')
                old = candidates[0] if candidates else None
                self._assert_operation_available(
                    connection, clip_ids=(old.clip_id,) if old else (), paths=(path,))
                if old is not None:
                    old = self._assert_cached_primary_current(connection, old)
                if old is not None and old.binding_state == BindingState.CONFLICT:
                    raise ValueError('conflicting legacy binding cannot be selected implicitly')
                if old is not None and self._observed_state(old) == BindingState.DETACHED:
                    fields = (owned_fields or {}).get(path)
                    if fields is None:
                        fields = (owned_fields or {}).get(self._path_key(path))
                    if fields is None:
                        raise OSError(
                            'Registered media identity changed; explicit metadata fields are required')
                    replacement_metadata = {
                        field: deepcopy(metadata[field])
                        for field in fields if field in metadata
                    }
                    current = connection.execute(
                        'SELECT value FROM entries WHERE clip_id=?',
                        (old.clip_id,)).fetchone()
                    if current is None:
                        raise OSError('Registered clip metadata is missing')
                    current_metadata = json.loads(current[0])
                    if not isinstance(current_metadata, dict):
                        raise ValueError('Invalid current clip metadata')
                    replacement_metadata = _sync_owned_legacy_tag(
                        replacement_metadata, current_metadata, fields,
                        writer_schema_version)
                    publications.append((self._detach_primary(connection, old), _DELETED))
                    evidence = self.inspect_media(path)
                    file = self._insert_primary(
                        connection, path, replacement_metadata, evidence)
                    metadata = replacement_metadata
                    self._recheck(evidence)
                elif old is None:
                    fields = (owned_fields or {}).get(path)
                    if fields is None:
                        fields = (owned_fields or {}).get(self._path_key(path))
                    if fields is not None and isinstance(metadata, dict):
                        metadata = _sync_owned_legacy_tag(
                            deepcopy(metadata), {}, fields, writer_schema_version)
                    file = self._insert_primary(connection, path, metadata)
                else:
                    file = old
                    current = connection.execute('SELECT value FROM entries WHERE clip_id=?',
                                                 (file.clip_id,)).fetchone()
                    if current is None:
                        raise OSError('Registered clip metadata is missing')
                    current_metadata = json.loads(current[0])
                    if not isinstance(current_metadata, dict):
                        raise ValueError('Invalid current clip metadata')
                    fields = (owned_fields or {}).get(path)
                    if fields is not None:
                        if 'editor_draft' in fields:
                            current_draft = current_metadata.get('editor_draft')
                            if (isinstance(current_draft, dict)
                                    and (type(current_draft.get('version')) is not int
                                         or current_draft['version'] != 1)):
                                raise ValueError(
                                    'unsupported editor draft version cannot be overwritten')
                        merged = deepcopy(current_metadata)
                        for field in fields:
                            if field in metadata:
                                merged[field] = deepcopy(metadata[field])
                            else:
                                merged.pop(field, None)
                        metadata = merged
                        metadata = _sync_owned_legacy_tag(
                            metadata, current_metadata, fields, writer_schema_version)
                    else:
                        metadata = dict(metadata)
                        for protected_field in (
                                'library_flags', 'library_tags', 'library_cover', 'library_origin',
                                HIGHLIGHT_MARKERS_FIELD):
                            if protected_field in current_metadata:
                                metadata[protected_field] = deepcopy(
                                    current_metadata[protected_field])
                                if protected_field == 'library_tags':
                                    if 'tag' in current_metadata:
                                        metadata['tag'] = deepcopy(current_metadata['tag'])
                                    else:
                                        metadata.pop('tag', None)
                            else:
                                metadata.pop(protected_field, None)
                    connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                                       (json.dumps(metadata, separators=(',', ':')), file.clip_id))
                publications.append((file, metadata))
            if prune_ids is None:
                # Current SQLite drafts are durable; never reconstruct a
                # bounded eviction set while saving another clip.
                prune_ids = ()
            for clip_id in prune_ids:
                if (connection.execute('PRAGMA user_version').fetchone()[0] in _TRASH_SCHEMA_VERSIONS
                        and connection.execute("SELECT 1 FROM trash_items WHERE clip_id=? "
                                               "AND status IN ('TRASHED','PURGING')", (clip_id,)).fetchone()):
                    continue
                self._assert_operation_available(connection, clip_ids=(clip_id,))
                row = connection.execute('SELECT value FROM entries WHERE clip_id=?', (clip_id,)).fetchone()
                if row is None:
                    continue
                metadata = json.loads(row[0])
                draft = metadata.get('editor_draft') if isinstance(metadata, dict) else None
                if (not isinstance(draft, dict) or type(draft.get('version')) is not int
                        or draft['version'] != 1):
                    continue
                metadata.pop('editor_draft')
                connection.execute('UPDATE entries SET value=? WHERE clip_id=?',
                                   (json.dumps(metadata, separators=(',', ':')), clip_id))
                pruned_metadata[clip_id] = metadata
        for file, metadata in publications:
            self._publish_file(file, metadata)
        for clip_id, metadata in pruned_metadata.items():
            self._id_data[clip_id] = metadata
            for path, existing_id in self._path_ids.items():
                if existing_id == clip_id:
                    self._data[canonical_media_path(path)] = metadata

    def _rename_current(self, old_path, new_path):
        old_path, new_path = self._path_text(old_path), self._path_text(new_path)
        if old_path == new_path:
            return
        candidates = self._primary_candidates(old_path)
        if not candidates:
            return
        if len(candidates) != 1 or candidates[0].binding_state == BindingState.CONFLICT:
            raise ValueError('conflicting legacy binding cannot be renamed implicitly')
        old = candidates[0]
        new_key = self._path_key(new_path)
        if any(file.path_key == new_key and file.file_id != old.file_id
               and file.binding_state != BindingState.DETACHED for file in self._files.values()):
            raise FileExistsError(f'Clip metadata already exists for {new_path}')
        identity = self._file_identity(new_path)
        if (identity is not None and old.identity is not None
                and dict(old.identity) != identity):
            raise ValueError('rename destination is not the registered media file')
        file = replace(old, path=new_path, path_key=new_key,
                       identity=identity if old.identity is not None and identity is not None
                       else old.identity)
        with self._current_transaction() as connection:
            self._assert_operation_available(connection, clip_ids=(old.clip_id,),
                                             paths=(old_path, new_path))
            old = self._assert_cached_primary_current(connection, old)
            file = replace(file, provenance=old.provenance)
            connection.execute('UPDATE entries SET path=? WHERE clip_id=?', (new_path, old.clip_id))
            self._write_file(connection, file)
            if identity is not None:
                self._recheck(FileEvidence(new_path, identity))
        self._path_ids.pop(old.path, None)
        self._data.pop(old.path_key, None)
        self._publish_file(file, self._id_data[old.clip_id])

    def associate_file(self, clip_id, evidence: FileEvidence, *,
                       role: FileRole = FileRole.SIDECAR) -> ClipFile:
        """Explicit association, not ownership or permission to delete a file."""
        role = FileRole(role)
        if role == FileRole.PRIMARY:
            raise ValueError('primary files must be registered, not associated')
        with self._lock:
            self._require_current()
            primaries = [file for file in self._files.values()
                         if file.clip_id == clip_id and file.role == FileRole.PRIMARY
                         and file.binding_state != BindingState.DETACHED]
            if (len(primaries) != 1 or self._observed_state(primaries[0]) not in
                    (BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED)):
                raise ValueError('association requires a currently bound primary file')
            primary = primaries[0]
            primary_evidence = self.inspect_media(primary.path)
        # Source/sidecar hashing can be expensive; no manager lock or SQL
        # transaction is held while the explicit association validates content.
        self._recheck(evidence)
        if role == FileRole.SIDECAR:
            from core.audio_manifest import manifest_path_for, read_manifest_for_media
            expected = manifest_path_for(primary.path)
            if (os.path.normcase(os.path.abspath(evidence.path))
                    != os.path.normcase(os.path.abspath(expected))):
                raise ValueError('sidecar is not the manifest adjacent to the primary')
            verified = self.inspect_media(evidence.path, hash_content=True)
            if dict(verified.stat) != dict(evidence.stat):
                raise ValueError('sidecar changed before validation')
            if evidence.content_sha256 is not None and evidence.content_sha256 != verified.content_sha256:
                raise ValueError('sidecar content does not match supplied evidence')
            manifest = read_manifest_for_media(primary.path)
            if manifest is None:
                raise ValueError('sidecar is missing, invalid or belongs to different media')
            if primary.content_sha256 is not None and primary.content_sha256 != manifest['media_sha256']:
                raise ValueError('primary content no longer matches its registered hash')
            evidence = verified
        with self._lock:
            self._require_current()
            if (self._files.get(primary.file_id) != primary
                    or self._observed_state(primary) not in
                    (BindingState.STAT_BOUND, BindingState.CONTENT_VERIFIED)):
                raise ValueError('primary binding changed during association')
            self._recheck(primary_evidence)
            self._recheck(evidence)
            key = self._path_key(evidence.path)
            detached = None
            with self._current_transaction() as connection:
                self._assert_operation_available(connection, clip_ids=(clip_id,),
                                                 paths=(primary.path, evidence.path))
                # Other manager instances may have committed since this cache
                # loaded. Even an idempotent return must honor current ownership.
                active = [self._decode_file(row) for row in connection.execute(
                    'SELECT file_id,clip_id,role,path,path_key,binding_state,'
                    'identity_json,content_sha256,provenance FROM clip_files '
                    'WHERE path_key=? AND binding_state<>?',
                    (key, BindingState.DETACHED.value))]
                if any(file.clip_id != clip_id or file.role != role for file in active):
                    raise FileExistsError('associated path is already registered elsewhere')
                if len(active) > 1:
                    raise ValueError('conflicting associated paths')
                old = active[0] if active else None
                if (old is not None and dict(old.identity or {}) == dict(evidence.stat)
                        and (evidence.content_sha256 is None
                             or old.content_sha256 == evidence.content_sha256)):
                    file = old
                else:
                    file = ClipFile(str(uuid4()), clip_id, role, evidence.path, key,
                                    BindingState.CONTENT_VERIFIED if evidence.content_sha256
                                    else BindingState.STAT_BOUND,
                                    dict(evidence.stat), evidence.content_sha256,
                                    'validated audio manifest' if role == FileRole.SIDECAR
                                    else 'explicit derived association')
                    detached = replace(old, binding_state=BindingState.DETACHED) if old else None
                    if detached is not None:
                        self._write_file(connection, detached)
                    self._write_file(connection, file)
                self._recheck(primary_evidence)
                self._recheck(evidence)
            if detached is not None:
                self._files[detached.file_id] = detached
            self._files[file.file_id] = file
            return file

    @staticmethod
    def _operation_from_row(row) -> RenameOperation:
        return RenameOperation(row[0], row[1], row[2], row[3], FileOperationState(row[4]),
                               json.loads(row[5]), row[6], bool(row[7]))

    @staticmethod
    def _validate_trash_operation_format(operation):
        """Validate the favorite-protection journal format at every write boundary."""
        if operation.kind not in ('trash', 'restore', 'purge'):
            return
        if type(operation.version) is not int or operation.version not in (1, 2):
            raise OSError('Unsupported trash operation format; update FTHR before recovery')
        plan = operation.plan
        if not isinstance(plan, dict):
            raise OSError('Unsupported trash operation payload')
        marker_present = 'preserve_favorites' in plan
        marker = plan.get('preserve_favorites', False)
        if marker_present and type(marker) is not bool:
            raise OSError('Invalid storage protection marker')
        if operation.version == 1:
            if marker:
                raise OSError('Favorite-protected storage journals require format v2')
        elif operation.kind not in ('trash', 'purge') or marker is not True:
            raise OSError('Invalid favorite-protected storage journal format')

    def _journal_required(self):
        if self._writable_schema().version not in _JOURNAL_SCHEMA_VERSIONS:
            raise RuntimeError('explicit journal schema migration is required')

    def _assert_operation_available(self, connection, *, clip_ids=(), paths=(), ignore_op_id=None, allow_trash=False):
        if connection.execute('PRAGMA user_version').fetchone()[0] not in _JOURNAL_SCHEMA_VERSIONS:
            return
        keys = {self._path_key(path) for path in paths}
        if not allow_trash and connection.execute('PRAGMA user_version').fetchone()[0] in _TRASH_SCHEMA_VERSIONS:
            for clip_id in set(clip_ids):
                if connection.execute(
                        "SELECT 1 FROM trash_items WHERE clip_id=? AND status IN ('TRASHED','PURGING') LIMIT 1",
                        (clip_id,)).fetchone():
                    raise OSError('Clip is in the trash or has an unfinished purge')
            for key in keys:
                if connection.execute(
                        "SELECT 1 FROM clip_files f WHERE f.path_key=? AND f.binding_state<>'DETACHED' "
                        "AND EXISTS (SELECT 1 FROM trash_items t WHERE t.clip_id=f.clip_id "
                        "AND t.status IN ('TRASHED','PURGING')) LIMIT 1", (key,)).fetchone():
                    raise OSError('Clip is in the trash or has an unfinished purge')
        for op_id, clip_id, payload in connection.execute(
                "SELECT op_id,clip_id,plan_json FROM file_operations "
                "WHERE state NOT IN ('COMMITTED','ROLLED_BACK') "
                "OR (kind='purge' AND state='COMMITTED' AND cleanup_done=0)"):
            if op_id == ignore_op_id:
                continue
            plan = json.loads(payload)
            occupied = {self._path_key(path) for path in plan.get('reserved_paths', ())}
            if clip_id in clip_ids or keys.intersection(occupied):
                raise OSError('Clip files are reserved by an unfinished rename operation')

    @staticmethod
    def operation_bindings_snapshot(files):
        """Cache evidence only; writer transactions compare authoritative rows."""
        return sorted(({
            'file_id': file.file_id, 'clip_id': file.clip_id, 'role': file.role.value,
            'path': file.path, 'path_key': file.path_key,
            'binding_state': file.binding_state.value, 'identity': dict(file.identity or {}),
            'content_sha256': file.content_sha256,
        } for file in files if file.binding_state != BindingState.DETACHED),
            key=lambda item: item['file_id'])

    def _check_operation_bindings(self, connection, operation):
        current = tuple(self._decode_file(row) for row in connection.execute(
            'SELECT * FROM clip_files WHERE clip_id=?', (operation.clip_id,)))
        if self.operation_bindings_snapshot(current) != operation.plan.get('file_bindings'):
            raise OSError('Registered clip files changed; reload the library and retry')

    @_locked
    def prepare_operation(self, operation: RenameOperation) -> RenameOperation:
        self._journal_required()
        if (operation.version != 1 or operation.kind != 'rename'
                or operation.state != FileOperationState.PREPARED):
            raise ValueError('invalid prepared rename operation')
        with self._current_transaction() as connection:
            self._assert_operation_available(connection, clip_ids=(operation.clip_id,),
                                             paths=operation.plan['reserved_paths'])
            entry = connection.execute('SELECT path FROM entries WHERE clip_id=?',
                                       (operation.clip_id,)).fetchone()
            if entry is None or entry[0] != operation.plan['metadata_source']:
                raise ValueError('rename metadata binding changed during preparation')
            self._check_operation_bindings(connection, operation)
            self._check_plan_stats(operation.plan, prepared=True)
            connection.execute('INSERT INTO file_operations VALUES (?,?,?,?,?,?,?,?)',
                               (operation.op_id, 1, 'rename', operation.clip_id,
                                FileOperationState.PREPARED.value,
                                json.dumps(operation.plan, separators=(',', ':')), None, 0))
        return operation

    @staticmethod
    def _check_plan_stats(plan, *, prepared=False):
        pairs = [(plan['source'] if prepared else plan['destination'], plan['expected_media'])]
        if plan.get('manifest_source'):
            pairs.append((plan['manifest_source'] if prepared else plan['manifest_destination'],
                          plan['expected_manifest'] if prepared else plan['expected_stage']))
            if prepared:
                pairs.append((plan['stage'], plan['expected_stage']))
        for path, expected in pairs:
            if ClipMetadataManager._file_identity(path) != expected['stat']:
                raise ValueError('rename file identity changed before metadata commit')

    @_locked
    def get_operation(self, op_id) -> RenameOperation | None:
        self._journal_required()
        with self._read_connection() as connection:
            row = connection.execute('SELECT * FROM file_operations WHERE op_id=?', (op_id,)).fetchone()
        return self._operation_from_row(row) if row is not None else None

    @_locked
    def pending_operations(self) -> tuple[RenameOperation, ...]:
        self._journal_required()
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM file_operations WHERE state NOT IN ('COMMITTED','ROLLED_BACK') "
                'OR cleanup_done=0 ORDER BY rowid').fetchall()
        return tuple(self._operation_from_row(row) for row in rows)

    @_locked
    def set_operation_state(self, op_id, state, *, error=None, cleanup_done=None) -> RenameOperation:
        self._journal_required()
        state = FileOperationState(state)
        with self._current_transaction() as connection:
            row = connection.execute('SELECT * FROM file_operations WHERE op_id=?', (op_id,)).fetchone()
            if row is None:
                raise KeyError(op_id)
            operation = self._operation_from_row(row)
            self._validate_trash_operation_format(operation)
            if (operation.state in (FileOperationState.COMMITTED, FileOperationState.ROLLED_BACK)
                    and operation.state != state):
                raise ValueError('terminal file operation state cannot be changed')
            if state == FileOperationState.COMMITTED and operation.state != state:
                raise ValueError('only metadata commit can commit a rename')
            if (operation.kind == 'purge' and state == FileOperationState.COMMITTED
                    and cleanup_done is True and not operation.cleanup_done):
                raise ValueError('Only purge finalization can complete committed purge cleanup')
            done = operation.cleanup_done if cleanup_done is None else bool(cleanup_done)
            connection.execute('UPDATE file_operations SET state=?,error=?,cleanup_done=? WHERE op_id=?',
                               (state.value, error, int(done), op_id))
            if operation.kind in ('trash', 'restore', 'purge') and connection.execute(
                    'PRAGMA user_version').fetchone()[0] in _TRASH_SCHEMA_VERSIONS:
                connection.execute('UPDATE trash_items SET error=? WHERE trash_id=?',
                                   (error, operation.plan['trash_id']))
        return replace(operation, state=state, error=error, cleanup_done=done)

    @_locked
    def commit_operation_rename(self, op_id) -> RenameOperation:
        self._journal_required()
        with self._current_transaction() as connection:
            row = connection.execute('SELECT * FROM file_operations WHERE op_id=?', (op_id,)).fetchone()
            if row is None:
                raise KeyError(op_id)
            operation = self._operation_from_row(row)
            if operation.state == FileOperationState.COMMITTED:
                return operation
            if (operation.version != 1 or operation.kind != 'rename'
                    or operation.state != FileOperationState.APPLYING):
                raise ValueError('rename operation is not applying')
            plan = operation.plan
            self._assert_operation_available(connection, clip_ids=(operation.clip_id,),
                                             paths=plan['reserved_paths'], ignore_op_id=op_id)
            entry = connection.execute('SELECT path,value FROM entries WHERE clip_id=?',
                                       (operation.clip_id,)).fetchone()
            if entry is None or entry[0] != plan['metadata_source']:
                raise ValueError('rename metadata path changed')
            self._check_operation_bindings(connection, operation)
            self._check_plan_stats(plan)
            row = connection.execute('SELECT * FROM clip_files WHERE file_id=?',
                                     (plan['media_file_id'],)).fetchone()
            if row is None:
                raise ValueError('primary file binding is missing')
            primary = self._decode_file(row)
            if primary.clip_id != operation.clip_id or primary.role != FileRole.PRIMARY:
                raise ValueError('primary file ownership changed')
            self._check_primary_collision(connection, plan['destination'], file_id=primary.file_id)
            primary = replace(primary, path=plan['destination'], path_key=self._path_key(plan['destination']),
                              identity=plan['expected_media']['stat'],
                              content_sha256=plan['expected_media'].get('content_sha256'),
                              binding_state=BindingState.CONTENT_VERIFIED
                              if plan['expected_media'].get('content_sha256') else BindingState.STAT_BOUND)
            connection.execute('UPDATE entries SET path=? WHERE clip_id=?',
                               (primary.path, operation.clip_id))
            self._write_file(connection, primary)
            sidecar = None
            if plan.get('manifest_source'):
                for (existing_id,) in connection.execute(
                        'SELECT file_id FROM clip_files WHERE path_key=? AND binding_state<>?',
                        (self._path_key(plan['manifest_destination']), BindingState.DETACHED.value)):
                    if existing_id != plan['manifest_file_id']:
                        raise FileExistsError('manifest destination has another registered owner')
                sidecar = ClipFile(plan['manifest_file_id'], operation.clip_id, FileRole.SIDECAR,
                                   plan['manifest_destination'], self._path_key(plan['manifest_destination']),
                                   BindingState.CONTENT_VERIFIED, plan['expected_stage']['stat'],
                                   plan['expected_stage']['content_sha256'], 'journaled audio manifest rename')
                self._write_file(connection, sidecar)
            connection.execute('UPDATE file_operations SET state=?,error=NULL WHERE op_id=?',
                               (FileOperationState.COMMITTED.value, op_id))
        # If publication fails, durable journal truth remains COMMITTED. The
        # service must reload and must never undo the already committed files.
        self._path_ids.pop(plan['metadata_source'], None)
        self._data.pop(plan['metadata_source'], None)
        self._publish_file(primary, json.loads(entry[1]))
        if sidecar is not None:
            self._files[sidecar.file_id] = sidecar
        return replace(operation, state=FileOperationState.COMMITTED, error=None)

    @staticmethod
    def _create_trash_schema(connection):
        connection.execute(
            "CREATE TABLE trash_items ("
            "trash_id TEXT PRIMARY KEY NOT NULL,clip_id TEXT NOT NULL REFERENCES entries(clip_id),"
            "status TEXT NOT NULL,created_utc TEXT NOT NULL,original_path TEXT NOT NULL,"
            "stored_path TEXT NOT NULL,members_json TEXT NOT NULL,error TEXT)"
        )
        connection.execute('CREATE INDEX trash_items_clip_status ON trash_items(clip_id,status)')

    @staticmethod
    def _trash_item_from_row(row):
        from core.clip_metadata_types import TrashItem, TrashStatus

        return TrashItem(*row[:5], TrashStatus(row[5]), row[6])

    @_locked
    def list_trash(self, *, include_history=False):
        if type(include_history) is not bool:
            raise ValueError("include_history must be a boolean")
        if self._writable_schema().version not in _TRASH_SCHEMA_VERSIONS:
            raise RuntimeError("explicit trash schema migration is required")
        where = "" if include_history else "WHERE status IN ('TRASHED','PURGING') "
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT trash_id,clip_id,original_path,stored_path,created_utc,status,error "
                "FROM trash_items " + where + "ORDER BY created_utc,trash_id"
            ).fetchall()
        return tuple(self._trash_item_from_row(row) for row in rows)

    @_locked
    def get_trash_item(self, trash_id):
        if self._writable_schema().version not in _TRASH_SCHEMA_VERSIONS:
            raise RuntimeError("explicit trash schema migration is required")
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT trash_id,clip_id,original_path,stored_path,created_utc,status,error "
                "FROM trash_items WHERE trash_id=?",
                (trash_id,),
            ).fetchone()
        return self._trash_item_from_row(row) if row is not None else None

    def _assert_trash_protection(self, connection, operation):
        from core.audio_manifest import manifest_path_for

        self._validate_trash_operation_format(operation)
        preserve_favorites = operation.plan.get('preserve_favorites', False)
        if type(preserve_favorites) is not bool:
            raise ValueError('Invalid storage protection marker')
        protected_members = {
            (member["expected"]["stat"]["st_dev"], member["expected"]["stat"]["st_ino"])
            for member in operation.plan["files"]
        }
        for row in connection.execute(
            "SELECT f.*,e.value FROM clip_files f JOIN entries e "
            "ON e.clip_id=f.clip_id WHERE f.binding_state<>?",
            (BindingState.DETACHED.value,),
        ):
            file = self._decode_file(row[:9])
            flags = self._library_flags(json.loads(row[9]))
            guarded = flags["protected"] or (preserve_favorites and flags["favorite"])
            if file.clip_id == operation.clip_id and guarded:
                raise OSError("Favorite clips cannot be moved to trash or purged")
            if guarded:
                observed = self._file_identity(file.path)
                identity_matches = file.identity is None or dict(file.identity) == observed
                if (
                    observed is not None
                    and identity_matches
                    and (observed["st_dev"], observed["st_ino"]) in protected_members
                ):
                        raise OSError("A favorite hard link references a trash bundle member")
                # Native adjacent manifests inherit their primary's protection,
                # including when they have no explicit SIDECAR registry row.
                if file.role == FileRole.PRIMARY and (identity_matches or observed is None):
                    adjacent = self._file_identity(str(manifest_path_for(file.path)))
                    if (
                        adjacent is not None
                        and (adjacent["st_dev"], adjacent["st_ino"]) in protected_members
                    ):
                        raise OSError("A favorite clip references this audio manifest (hard link)")

    def _trash_operation_checks(self, connection, operation, *, final=False, check_files=True):
        self._validate_trash_operation_format(operation)
        if connection.execute("PRAGMA user_version").fetchone()[0] not in _TRASH_SCHEMA_VERSIONS:
            raise RuntimeError("explicit trash schema migration is required")
        plan = operation.plan
        self._assert_operation_available(
            connection,
            clip_ids=(operation.clip_id,),
            paths=plan["reserved_paths"],
            ignore_op_id=operation.op_id,
            allow_trash=operation.kind != "trash",
        )
        self._check_operation_bindings(connection, operation)
        row = connection.execute(
            "SELECT path FROM entries WHERE clip_id=?", (operation.clip_id,)
        ).fetchone()
        expected_path = plan["metadata_source"] if operation.kind == "trash" else None
        if row is None or row[0] != expected_path:
            raise OSError("Clip activation changed during trash operation")
        if operation.kind != "trash":
            trash = connection.execute(
                "SELECT clip_id,status FROM trash_items WHERE trash_id=?", (plan["trash_id"],)
            ).fetchone()
            if trash != (operation.clip_id, "TRASHED"):
                raise OSError("Trash item changed or is no longer available")
        if operation.kind in ("trash", "purge"):
            self._assert_trash_protection(connection, operation)
        for member in plan["files"] if check_files else ():
            self._check_primary_collision(connection, member["source"], file_id=member["file_id"])
            path = (
                member["destination"] if final and operation.kind != "purge" else member["source"]
            )
            expected = (
                member["final_expected"]
                if final and operation.kind != "purge"
                else member["expected"]
            )
            if self._deletable_identity(path) != expected["stat"]:
                raise OSError("Trash member changed before durable commit")

    @_locked
    def prepare_trash_operation(self, operation, *, validate):
        self._validate_trash_operation_format(operation)
        if (
            type(operation.version) is not int
            or operation.version not in (1, 2)
            or operation.kind not in ("trash", "restore", "purge")
            or operation.state != FileOperationState.PREPARED
        ):
            raise ValueError("Unsupported trash operation")
        with self._current_transaction() as connection:
            self._trash_operation_checks(connection, operation)
            validate()
            connection.execute(
                "INSERT INTO file_operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    operation.op_id,
                    operation.version,
                    operation.kind,
                    operation.clip_id,
                    operation.state.value,
                    json.dumps(operation.plan, separators=(",", ":")),
                    None,
                    0,
                ),
            )
        return operation

    @_locked
    def commit_trash_operation(self, op_id, *, validate):
        with self._current_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM file_operations WHERE op_id=?", (op_id,)
            ).fetchone()
            if row is None:
                raise KeyError(op_id)
            operation = self._operation_from_row(row)
            self._validate_trash_operation_format(operation)
            if operation.state == FileOperationState.COMMITTED:
                return operation
            if operation.state != FileOperationState.APPLYING or operation.kind not in (
                "trash",
                "restore",
                "purge",
            ):
                raise OSError("Trash operation is not applying")
            self._trash_operation_checks(connection, operation, final=operation.kind != "purge")
            validate()
            plan = operation.plan
            if operation.kind == "trash":
                connection.execute(
                    "UPDATE entries SET path=NULL WHERE clip_id=?", (operation.clip_id,)
                )
                connection.execute(
                    "INSERT INTO trash_items VALUES (?,?,?,?,?,?,?,NULL)",
                    (
                        plan["trash_id"],
                        operation.clip_id,
                        "TRASHED",
                        plan["created_utc"],
                        plan["original_path"],
                        plan["stored_path"],
                        json.dumps(plan["file_bindings"]),
                    ),
                )
            elif operation.kind == "restore":
                self._check_primary_collision(
                    connection, plan["destination"], file_id=plan["media_file_id"]
                )
                connection.execute(
                    "UPDATE entries SET path=? WHERE clip_id=?",
                    (plan["destination"], operation.clip_id),
                )
                connection.execute(
                    "UPDATE trash_items SET status='RESTORED',error=NULL WHERE trash_id=?",
                    (plan["trash_id"],),
                )
            else:
                connection.execute(
                    "UPDATE trash_items SET status='PURGING',error=NULL WHERE trash_id=?",
                    (plan["trash_id"],),
                )
            for member in plan["files"]:
                state = (
                    BindingState.TRASHED
                    if operation.kind == "trash"
                    else BindingState.PURGING
                    if operation.kind == "purge"
                    else BindingState.CONTENT_VERIFIED
                    if member["final_expected"].get("content_sha256")
                    else BindingState.STAT_BOUND
                )
                self._check_primary_collision(
                    connection, member["destination"], file_id=member["file_id"]
                )
                file = ClipFile(
                    member["file_id"],
                    operation.clip_id,
                    FileRole(member["role"]),
                    member["destination"],
                    self._path_key(member["destination"]),
                    state,
                    member["final_expected"]["stat"],
                    member["final_expected"].get("content_sha256"),
                    "journaled " + operation.kind,
                )
                self._write_file(connection, file)
            if operation.kind == "purge":
                current = tuple(
                    self._decode_file(row)
                    for row in connection.execute(
                        "SELECT * FROM clip_files WHERE clip_id=?", (operation.clip_id,)
                    )
                )
                plan["file_bindings"] = self.operation_bindings_snapshot(current)
            connection.execute(
                "UPDATE file_operations SET state='COMMITTED',plan_json=?,error=NULL WHERE op_id=?",
                (json.dumps(plan, separators=(",", ":")), op_id),
            )
        self.reload()
        return self.get_operation(op_id)

    @contextmanager
    def purge_member_guard(self, operation, path, expected, *, validate):
        with self._lock:
            with self._current_transaction() as connection:
                row = connection.execute(
                    "SELECT state,kind,cleanup_done FROM file_operations WHERE op_id=?",
                    (operation.op_id,),
                ).fetchone()
                if row != ("COMMITTED", "purge", 0):
                    raise OSError("Purge has no unfinished durable deletion decision")
                self._check_operation_bindings(connection, operation)
                self._assert_trash_protection(connection, operation)
                validate()
                if self._deletable_identity(path) != expected["stat"]:
                    raise OSError("Changed trash file was retained")
                yield

    @_locked
    def finish_purge(self, operation, *, validate):
        cover_cleanup = None
        with self._current_transaction() as connection:
            row = connection.execute(
                "SELECT state,kind FROM file_operations WHERE op_id=?", (operation.op_id,)
            ).fetchone()
            if row != ("COMMITTED", "purge"):
                raise OSError("Purge is not committed")
            self._check_operation_bindings(connection, operation)
            self._assert_trash_protection(connection, operation)
            validate()
            for member in operation.plan["files"]:
                if os.path.lexists(member["source"]):
                    raise OSError("Trash member still exists; metadata was retained")
            entry = connection.execute(
                "SELECT value FROM entries WHERE clip_id=?", (operation.clip_id,)
            ).fetchone()
            if entry is not None:
                try:
                    current_metadata = json.loads(entry[0])
                except (TypeError, ValueError, json.JSONDecodeError):
                    current_metadata = None
                if isinstance(current_metadata, dict):
                    cover_reference, cover_error = parse_cover_reference(
                        current_metadata.get('library_cover'))
                    if cover_error is None and cover_reference is not None:
                        cover_cleanup = (operation.clip_id, cover_reference)
            connection.execute(
                "UPDATE entries SET value='{}',path=NULL WHERE clip_id=?", (operation.clip_id,)
            )
            if connection.execute('PRAGMA user_version').fetchone()[0] >= _ALBUM_SCHEMA_VERSION:
                connection.execute(
                    "DELETE FROM album_members WHERE clip_id=?", (operation.clip_id,)
                )
            connection.execute(
                "UPDATE clip_files SET binding_state='DETACHED' WHERE clip_id=?",
                (operation.clip_id,),
            )
            connection.execute(
                "UPDATE trash_items SET status='PURGED',error=NULL WHERE trash_id=?",
                (operation.plan["trash_id"],),
            )
            connection.execute(
                "UPDATE file_operations SET cleanup_done=1,error=NULL WHERE op_id=?",
                (operation.op_id,),
            )
        self.reload()
        if cover_cleanup is not None:
            # Purge is already durable; an unavailable asset must never turn
            # the committed metadata result into a failed purge.
            cleanup_retired_asset(self._database, *cover_cleanup)
        return self.get_operation(operation.op_id)

    @contextmanager
    def trash_move_guard(self, operation, path, expected, *, validate):
        with self._lock:
            with self._current_transaction() as connection:
                self._trash_operation_checks(connection, operation, check_files=False)
                validate()
                if self._deletable_identity(path) != expected["stat"]:
                    raise OSError("Trash source changed immediately before move")
                yield
