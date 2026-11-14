"""Journaled same-directory file renames, with identity-checked crash recovery.

This protects process-interruption recovery, not power-loss directory durability.
Registration/association do not grant ownership or deletion permission.
"""
from __future__ import annotations

import base64
import json
import hashlib
import os
import stat
import sys
import threading
from pathlib import Path
from uuid import uuid4

from core.audio_manifest import manifest_path_for, validate_manifest
from core.clip_metadata_types import (
    BindingState, FileOperationState as State, FileRole, MutationResult, RenameOperation,
)
from core.clip_mutations import rename_no_replace
from core.library_index import canonical_media_path
from core.library_ownership import MediaOwnership, classify_media_path

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


class _Conflict(OSError):
    """Foreign or changed files must be retained, never guessed away."""


class ClipFileOperations:
    def __init__(self, manager, *, settings_provider=None, checkpoint=None):
        self.manager = manager
        from core.clip_trash import TrashOperations
        self._trash = TrashOperations(self)
        self._settings_provider = settings_provider
        self._checkpoint = checkpoint
        self._runtime_blocked: set[str] = set()
        key = canonical_media_path(manager._database)
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _point(self, name, operation):
        if self._checkpoint is not None:
            self._checkpoint(name, operation)

    @staticmethod
    def _absolute(path) -> str:
        return os.path.abspath(os.fspath(path))

    @staticmethod
    def _plain_path(path, *, required=True):
        path = Path(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if required:
                raise
            return
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE:
            raise _Conflict('Symlink or reparse-point file changes are not supported')

    @classmethod
    def _parent(cls, path) -> dict:
        parent = Path(path).parent
        for ancestor in (parent, *parent.parents):
            cls._plain_path(ancestor)
            if not ancestor.is_dir():
                raise _Conflict('Rename parent is not a directory')
        info = parent.stat()
        return {'st_dev': info.st_dev, 'st_ino': info.st_ino}

    @classmethod
    def _admission(cls, value) -> dict:
        owned = value.get('owned_root')
        roots = value.get('imported_roots', ())
        if (not isinstance(owned, (str, os.PathLike)) or not os.fspath(owned)
                or not isinstance(roots, (list, tuple))
                or type(value.get('allow_linked', False)) is not bool):
            raise ValueError('invalid rename ownership admission')
        return {'owned_root': cls._absolute(owned),
                'imported_roots': [cls._absolute(path) for path in roots],
                'allow_linked': value.get('allow_linked', False)}

    def _validate_boundary(self, plan):
        admission = plan['admission']
        if self._settings_provider is not None:
            current = self._admission({**self._settings_provider(),
                                       'allow_linked': admission['allow_linked']})
            if current != admission:
                raise _Conflict('Library ownership settings changed during rename')
        for path in plan['reserved_paths']:
            self._plain_path(path, required=False)
            if self._parent(path) != plan['parent_identity']:
                raise _Conflict('Rename parent identity changed')
        ownership = classify_media_path(plan['source'], admission['owned_root'], admission['imported_roots'])
        if (ownership is MediaOwnership.EXTERNAL or
                (ownership is MediaOwnership.LINKED_IMPORTED and not admission['allow_linked'])):
            raise _Conflict('The original is not owned or explicitly confirmed as linked media')

    def _evidence(self, path, *, content=False) -> dict:
        evidence = self.manager.inspect_media(path, hash_content=content)
        return {'stat': dict(evidence.stat), 'content_sha256': evidence.content_sha256}

    def _matches(self, path, expected) -> bool:
        if not os.path.lexists(path):
            return False
        self._plain_path(path)
        observed = self._evidence(path, content=bool(expected.get('content_sha256')))
        return (observed['stat'] == expected['stat'] and
                (not expected.get('content_sha256') or
                 observed['content_sha256'] == expected['content_sha256']))

    @staticmethod
    def _actual_name(path):
        path = Path(path)
        # Windows existence checks accept both spellings of a case-only rename.
        for item in os.scandir(path.parent):
            if item.name == path.name:
                return True
        return False

    def prepare_trash(self, clip_id, *, admission, expected_identity=None,
                      preserve_favorites=False):
        return self._trash.prepare_trash(
            clip_id, admission=admission, expected_identity=expected_identity,
            preserve_favorites=preserve_favorites)

    def prepare_restore(self, trash_id, *, destination=None, admission):
        return self._trash.prepare_restore(trash_id, destination=destination, admission=admission)

    def prepare_purge(self, trash_id, *, admission, preserve_favorites=False):
        return self._trash.prepare_purge(
            trash_id, admission=admission,
            preserve_favorites=preserve_favorites)

    def list_trash(self, *, include_history=False):
        return self.manager.list_trash(include_history=include_history)

    def get_trash_item(self, trash_id):
        return self.manager.get_trash_item(trash_id)

    def prepare_rename(self, clip_id, destination, *, admission) -> RenameOperation:
        with self._lock:
            files = self.manager.files_for_clip(clip_id)
            primaries = [file for file in files if file.role == FileRole.PRIMARY
                         and file.binding_state != BindingState.DETACHED]
            if len(primaries) != 1 or primaries[0].binding_state in (
                    BindingState.CONFLICT, BindingState.TRASHED, BindingState.PURGING):
                raise ValueError('Rename requires one unambiguous primary file')
            primary = primaries[0]
            source = self._absolute(primary.path)
            destination = self._absolute(self.manager._path_text(destination))
            if source == destination:
                raise ValueError('Rename destination is unchanged')
            if Path(source).parent != Path(destination).parent:
                raise ValueError('Only same-directory renames are supported')
            case_only = (sys.platform == 'win32' and canonical_media_path(source) ==
                         canonical_media_path(destination))
            if os.path.lexists(destination) and not (case_only and os.path.samefile(source, destination)):
                raise FileExistsError('Rename destination already exists')
            op_id = str(uuid4())
            old_manifest = str(manifest_path_for(source))
            new_manifest = str(manifest_path_for(destination))
            has_manifest = os.path.lexists(old_manifest)
            plan = {
                'source': source, 'destination': destination, 'metadata_source': primary.path,
                'media_file_id': primary.file_id, 'case_only': case_only,
                'file_bindings': self.manager.operation_bindings_snapshot(files),
                'manifest_source': old_manifest if has_manifest else None,
                'manifest_destination': new_manifest if has_manifest else None,
                'stage': str(Path(source).parent / f'.fthr-rename-{op_id}.stage') if has_manifest else None,
                'backup': str(Path(source).parent / f'.fthr-rename-{op_id}.backup') if has_manifest else None,
                'admission': self._admission(admission), 'parent_identity': self._parent(source),
                'expected_media': self._evidence(source, content=bool(primary.content_sha256) or has_manifest),
            }
            plan['reserved_paths'] = [source, destination]
            if has_manifest:
                plan['reserved_paths'] += [old_manifest, new_manifest, plan['stage'], plan['backup']]
            self._validate_boundary(plan)
            if primary.content_sha256 and primary.content_sha256 != plan['expected_media']['content_sha256']:
                raise _Conflict('Registered media content changed before rename')
            binding = self.manager.resolve_media(source)
            if binding is None or binding.clip_id != clip_id or binding.state in (
                    BindingState.DETACHED, BindingState.MISSING, BindingState.CONFLICT):
                raise ValueError('Primary media changed before rename')
            sidecars = [file for file in files if file.role != FileRole.PRIMARY
                        and file.binding_state != BindingState.DETACHED]
            if any(file.role != FileRole.SIDECAR or canonical_media_path(file.path) !=
                   canonical_media_path(old_manifest) for file in sidecars):
                raise ValueError('Unknown associated files prevent a safe rename')
            if sidecars and not has_manifest:
                raise ValueError('Registered manifest is missing')
            if has_manifest:
                private = {canonical_media_path(plan['stage']), canonical_media_path(plan['backup'])}
                ordinary = {canonical_media_path(path) for path in
                            (source, destination, old_manifest, new_manifest)}
                if len(private) != 2 or private.intersection(ordinary):
                    raise ValueError('Private rename artifact paths overlap media paths')
                if os.path.lexists(plan['backup']):
                    raise FileExistsError('Private rename backup path is occupied')
                if os.path.lexists(new_manifest) and not (
                        case_only and os.path.samefile(old_manifest, new_manifest)):
                    raise FileExistsError('Manifest destination already exists')
                before = self._evidence(old_manifest)
                with open(old_manifest, 'rb') as handle:
                    original = handle.read(1024 * 1024 + 1)
                if len(original) > 1024 * 1024:
                    raise ValueError('Audio manifest is too large to rename safely')
                if self._evidence(old_manifest)['stat'] != before['stat']:
                    raise _Conflict('Audio manifest changed while reading')
                plan['expected_manifest'] = {
                    'stat': before['stat'], 'content_sha256': hashlib.sha256(original).hexdigest()}
                if sidecars and (dict(sidecars[0].identity or {}) != plan['expected_manifest']['stat']
                                 or (sidecars[0].content_sha256 and sidecars[0].content_sha256 !=
                                     plan['expected_manifest']['content_sha256'])):
                    raise _Conflict('Registered sidecar changed before rename')
                manifest = json.loads(original)
                validate_manifest(manifest)
                if (manifest['media_file'] != Path(source).name or
                        manifest['media_sha256'] != plan['expected_media']['content_sha256']):
                    raise ValueError('Audio manifest belongs to different media')
                manifest['media_file'] = Path(destination).name
                patch = json.dumps(manifest, ensure_ascii=True, separators=(',', ':')).encode('utf-8')
                plan['original_manifest_bytes'] = base64.b64encode(original).decode('ascii')
                plan['patch_manifest_bytes'] = base64.b64encode(patch).decode('ascii')
                plan['manifest_file_id'] = sidecars[0].file_id if sidecars else str(uuid4())
                with open(plan['stage'], 'xb') as target:
                    target.write(patch)
                    target.flush()
                    os.fsync(target.fileno())
                plan['expected_stage'] = self._evidence(plan['stage'], content=True)
            if not self._matches(source, plan['expected_media']):
                raise _Conflict('Media changed during preparation')
            operation = RenameOperation(op_id, 1, 'rename', clip_id, State.PREPARED, plan)
            operation = self.manager.prepare_operation(operation)
            self._point('after_prepared', operation)
            return operation

    def _move(self, source, destination, expected, plan):
        self._validate_boundary(plan)
        if not self._matches(source, expected):
            raise _Conflict('Rename source is missing or its identity changed')
        case_only = (sys.platform == 'win32' and canonical_media_path(source) ==
                     canonical_media_path(destination))
        if os.path.lexists(destination) and not (
                case_only and os.path.samefile(source, destination)):
            raise _Conflict('A foreign file occupies the rename destination')
        rename_no_replace(source, destination)

    def _preflight(self, plan):
        self._validate_boundary(plan)
        pairs = [(plan['source'], plan['destination'], plan['expected_media'])]
        if plan.get('manifest_source'):
            pairs.append((plan['manifest_source'], plan['manifest_destination'], plan['expected_manifest']))
            if not self._matches(plan['stage'], plan['expected_stage']):
                raise _Conflict('Private manifest stage changed before execution')
            if os.path.lexists(plan['backup']):
                raise _Conflict('Private manifest backup path is occupied')
        for source, destination, expected in pairs:
            if not self._matches(source, expected):
                raise _Conflict('Rename input changed before execution')
            if os.path.lexists(destination) and not (
                    sys.platform == 'win32' and canonical_media_path(source) ==
                    canonical_media_path(destination) and os.path.samefile(source, destination)):
                raise _Conflict('A foreign file occupies the rename destination')

    def _cleanup(self, operation):
        self._point('before_cleanup', operation)
        plan = operation.plan
        try:
            self._validate_boundary(plan)
            if plan.get('stage'):
                for path, expected in ((plan['stage'], plan['expected_stage']),
                                       (plan['backup'], plan['expected_manifest'])):
                    if os.path.lexists(path):
                        if not self._matches(path, expected):
                            raise _Conflict('Foreign file occupies a private rename artifact')
                        os.unlink(path)
            operation = self.manager.set_operation_state(
                operation.op_id, operation.state, cleanup_done=True)
        except Exception as error:
            # Cleanup is only for verified private artifacts; terminal state is
            # never undone because cleanup failed or an artifact became foreign.
            operation = self.manager.set_operation_state(
                operation.op_id, operation.state, error=str(error), cleanup_done=False)
        self._point('after_cleanup', operation)
        return operation

    @staticmethod
    def _result(operation):
        return MutationResult(operation.op_id, operation.state, operation.plan['source'],
                              operation.plan['destination'], operation.error)

    @staticmethod
    def _check_supported(operation):
        if operation.version != 1 or operation.kind != 'rename':
            raise OSError('Unsupported file operation format; update FTHR before recovery')

    def execute(self, op_id) -> MutationResult:
        with self._lock:
            operation = self.manager.get_operation(op_id)
            if operation is None:
                raise KeyError(op_id)
            if operation.kind in ('trash', 'restore', 'purge'):
                return self._trash.execute(op_id)
            self._check_supported(operation)
            if operation.state == State.COMMITTED:
                self.manager.reload()
                return self._result(self._cleanup(operation) if not operation.cleanup_done else operation)
            if (operation.state != State.PREPARED or operation.version != 1
                    or operation.kind != 'rename'):
                return self._result(self._recover(operation))
            plan = operation.plan
            try:
                self._preflight(plan)
                operation = self.manager.set_operation_state(op_id, State.APPLYING)
                self._point('after_applying', operation)
                if plan.get('manifest_source'):
                    self._move(plan['manifest_source'], plan['backup'], plan['expected_manifest'], plan)
                    self._point('after_sidecar_backup', operation)
                self._move(plan['source'], plan['destination'], plan['expected_media'], plan)
                self._point('after_media_rename', operation)
                if plan.get('stage'):
                    self._move(plan['stage'], plan['manifest_destination'], plan['expected_stage'], plan)
                    self._point('after_manifest_rename', operation)
                self._point('before_metadata_commit', operation)
                operation = self.manager.commit_operation_rename(op_id)
                self._point('after_metadata_commit', operation)
                return self._result(self._cleanup(operation))
            except Exception as error:
                # Fresh durable journal truth decides direction, even if RAM
                # publication failed after a successful SQLite COMMIT.
                try:
                    durable = self.manager.get_operation(op_id)
                except Exception:
                    # Unknown DB truth must never trigger a speculative FS undo.
                    self._runtime_blocked.update(plan['reserved_paths'])
                    return MutationResult(op_id, State.RECOVERY_REQUIRED, plan['source'],
                                          plan['destination'], str(error))
                if durable is not None and durable.state == State.COMMITTED:
                    try:
                        self.manager.reload()
                    except Exception:
                        # Files remain committed; keep consumer paths blocked
                        # until a successful cache reload/restart is possible.
                        self._runtime_blocked.update(plan['reserved_paths'])
                    return self._result(self._cleanup(durable))
                if isinstance(error, _Conflict):
                    return self._result(self.manager.set_operation_state(
                        op_id, State.RECOVERY_REQUIRED, error=str(error)))
                return self._result(self._recover(durable or operation, error=str(error)))

    def _restore(self, original, moved, expected, plan):
        same_key = canonical_media_path(original) == canonical_media_path(moved)
        if same_key and sys.platform == 'win32':
            if self._actual_name(original) and self._matches(original, expected):
                return
            if self._actual_name(moved) and self._matches(moved, expected):
                self._move(moved, original, expected, plan)
                return
            raise _Conflict('Case-only rename identity cannot be recovered safely')
        if os.path.lexists(original):
            if not self._matches(original, expected) or os.path.lexists(moved):
                raise _Conflict('A foreign file blocks rollback')
            return
        if not self._matches(moved, expected):
            raise _Conflict('The renamed file cannot be identified for rollback')
        self._move(moved, original, expected, plan)

    def _recover(self, operation, *, error=None):
        if operation.kind in ('trash', 'restore', 'purge'):
            return self._trash.recover(operation, error=error)
        self._check_supported(operation)
        if operation.state == State.COMMITTED:
            self.manager.reload()
            return self._cleanup(operation) if not operation.cleanup_done else operation
        if operation.state == State.ROLLED_BACK:
            return self._cleanup(operation) if not operation.cleanup_done else operation
        try:
            if operation.version != 1 or operation.kind != 'rename':
                raise _Conflict('Unsupported operation format requires manual recovery')
            plan = operation.plan
            self._validate_boundary(plan)
            operation = self.manager.set_operation_state(operation.op_id, State.ROLLING_BACK, error=error)
            if plan.get('stage'):
                patch = plan['manifest_destination']
                # Before the original manifest moved, case-only names refer to
                # that original, not to a published patch. Its backup proves phase.
                backup_present = os.path.lexists(plan['backup'])
                if os.path.lexists(patch) and self._matches(patch, plan['expected_stage']):
                    self._restore(plan['stage'], patch, plan['expected_stage'], plan)
                elif os.path.lexists(patch) and (backup_present or not plan['case_only']):
                    # The original name may still exist before any FS step.
                    if patch != plan['manifest_source'] or not self._matches(patch, plan['expected_manifest']):
                        raise _Conflict('Foreign manifest blocks rollback')
                self._restore(plan['manifest_source'], plan['backup'], plan['expected_manifest'], plan)
            self._restore(plan['source'], plan['destination'], plan['expected_media'], plan)
            operation = self.manager.set_operation_state(operation.op_id, State.ROLLED_BACK, error=error)
            return self._cleanup(operation)
        except Exception as failure:
            # Preserve all uncertain files and retain their readiness barrier.
            return self.manager.set_operation_state(operation.op_id, State.RECOVERY_REQUIRED,
                                                    error=str(failure))

    def recover_operation(self, op_id) -> MutationResult:
        """Recover only the requested operation under the shared service lock."""
        with self._lock:
            operation = self.manager.get_operation(op_id)
            if operation is None:
                raise KeyError(op_id)
            if operation.kind in ('trash', 'restore', 'purge'):
                self._trash.supported(operation)
            else:
                self._check_supported(operation)
            return self._result(self._recover(operation))

    def recover_pending(self) -> tuple[MutationResult, ...]:
        with self._lock:
            results = []
            for operation in self.manager.pending_operations():
                results.append(self._result(self._recover(operation)))
            return tuple(results)

    def blocked_paths(self) -> tuple[str, ...]:
        with self._lock:
            blocked = set(self._runtime_blocked)
            for operation in self.manager.pending_operations():
                if (operation.state not in (State.COMMITTED, State.ROLLED_BACK)
                        or operation.kind == 'purge' and not operation.cleanup_done):
                    blocked.update(operation.plan.get('reserved_paths', ()))
            return tuple(sorted(blocked))
