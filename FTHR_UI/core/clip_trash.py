"""Owned-library trash, reversible restore and durable forward-only purge."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from core.audio_manifest import manifest_path_for, validate_manifest
from core.clip_metadata_types import (
    BindingState,
    FileOperationState as State,
    FileRole,
    MutationResult,
    RenameOperation,
    TrashStatus,
)
from core.clip_mutations import rename_no_replace
from core.library_index import canonical_media_path
from core.library_ownership import MediaOwnership, classify_media_path, media_file_identity


class TrashOperations:
    def __init__(self, host):
        self.host, self.manager = host, host.manager

    def supported(self, operation):
        self.manager._validate_trash_operation_format(operation)
        if operation.kind not in ("trash", "restore", "purge"):
            raise OSError("Unsupported trash operation format; update FTHR before recovery")
        if self.manager._writable_schema().version not in (3, 4, 5, 6):
            raise OSError("Trash operations require explicit schema 3, 4, 5, or 6 migration")

    @staticmethod
    def _identity(identity):
        if (
            set(identity) != {"st_dev", "st_ino", "size", "mtime_ns"}
            or any(type(value) is not int for value in identity.values())
            or identity["st_dev"] < 0
            or identity["st_ino"] <= 0
            or identity["size"] < 0
        ):
            raise OSError("Stable filesystem identity is required for trash operations")
        return identity

    def _parent(self, path):
        identity = self.host._parent(path)
        if (
            type(identity["st_dev"]) is not int
            or identity["st_dev"] < 0
            or type(identity["st_ino"]) is not int
            or identity["st_ino"] <= 0
        ):
            raise OSError("Stable trash parent identity is unavailable")
        return identity

    def _evidence(self, path, *, content=False):
        identity = self._identity(media_file_identity(path))
        evidence = self.host._evidence(path, content=content)
        if evidence["stat"] != identity:
            raise OSError("Trash file changed during evidence collection")
        return evidence

    def _matches(self, path, expected):
        if not os.path.lexists(path):
            return False
        observed = media_file_identity(path)
        if observed != expected["stat"]:
            return False
        if expected.get("content_sha256"):
            digest = hashlib.sha256()
            remaining = expected["stat"]["size"]
            with open(path, "rb") as handle:
                while remaining:
                    block = handle.read(min(1024 * 1024, remaining))
                    if not block:
                        return False
                    digest.update(block)
                    remaining -= len(block)
                if handle.read(1):
                    return False
            if digest.hexdigest() != expected["content_sha256"]:
                return False
        return media_file_identity(path) == expected["stat"]

    def _boundary(self, plan, *, missing_folder=False):
        admission = plan["admission"]
        if self.host._settings_provider is not None:
            current = self.host._admission(
                {**self.host._settings_provider(), "allow_linked": admission["allow_linked"]}
            )
            if current != admission:
                raise OSError("Library ownership settings changed")
        for parent, expected in plan["parents"].items():
            if missing_folder and parent == plan["folder"] and not os.path.lexists(parent):
                continue
            actual = self._parent(str(Path(parent) / "identity-probe"))
            if actual != expected:
                raise OSError("A trash or library parent identity changed")
        for path in plan["reserved_paths"]:
            self.host._plain_path(path, required=False)
            if (
                classify_media_path(path, admission["owned_root"], admission["imported_roots"])
                is not MediaOwnership.FTHR_OWNED
            ):
                raise OSError("Trash operations only support files owned by this library")
        if any(
            expected["st_dev"] != plan["expected_media"]["stat"]["st_dev"]
            for expected in plan["parents"].values()
        ):
            raise OSError("Cross-volume trash operations are not supported")

    def _files(self, clip_id):
        files = tuple(
            file
            for file in self.manager.files_for_clip(clip_id)
            if file.binding_state != BindingState.DETACHED
        )
        primaries = [file for file in files if file.role == FileRole.PRIMARY]
        if len(primaries) != 1 or primaries[0].binding_state == BindingState.CONFLICT:
            raise OSError("Trash requires one unambiguous primary file")
        primary = primaries[0]
        manifest = canonical_media_path(manifest_path_for(primary.path))
        if any(
            file.role != FileRole.PRIMARY
            and (file.role != FileRole.SIDECAR or file.path_key != manifest)
            for file in files
        ):
            raise OSError("Unknown or shared associated files cannot be moved to trash")
        return primary, files

    def _item(self, trash_id):
        item = self.manager.get_trash_item(trash_id)
        if item is None:
            raise KeyError(trash_id)
        if item.status != TrashStatus.TRASHED:
            raise OSError("This trash item is not available for restore or purge")
        return item

    def _base_plan(self, primary, files, source, destination, admission, trash_id, folder,
                   *, preserve_favorites=False):
        admission = self.host._admission(admission)
        has_manifest = os.path.lexists(manifest_path_for(source))
        expected = self._evidence(source, content=has_manifest or bool(primary.content_sha256))
        if (
            primary.identity is not None
            and dict(primary.identity) != expected["stat"]
            or primary.content_sha256
            and primary.content_sha256 != expected["content_sha256"]
        ):
            raise OSError("Registered primary identity changed")
        if primary.binding_state not in (
            BindingState.STAT_BOUND,
            BindingState.CONTENT_VERIFIED,
            BindingState.TRASHED,
        ):
            raise OSError("Trash requires an explicitly verified file binding")
        plan = dict(
            source=source,
            destination=destination,
            metadata_source=primary.path,
            media_file_id=primary.file_id,
            trash_id=trash_id,
            folder=str(folder),
            original_path=source,
            stored_path=destination,
            created_utc=datetime.now(timezone.utc).isoformat(),
            admission=admission,
            expected_media=expected,
            file_bindings=self.manager.operation_bindings_snapshot(files),
            files=[],
            moves=[],
            artifacts=[],
            reserved_paths=[source, destination],
            parents={},
            preserve_favorites=preserve_favorites,
        )
        member = dict(
            file_id=primary.file_id,
            role=FileRole.PRIMARY.value,
            source=source,
            destination=destination,
            expected=expected,
            final_expected=expected,
        )
        plan["files"].append(member)
        return plan

    def _manifest(self, plan, files, *, patch):
        source, destination = map(manifest_path_for, (plan["source"], plan["destination"]))
        sidecars = [file for file in files if file.role == FileRole.SIDECAR]
        if not os.path.lexists(source):
            if sidecars:
                raise OSError("Registered audio manifest is missing")
            return
        expected = self._evidence(source)
        with open(source, "rb") as handle:
            payload = handle.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024:
            raise ValueError("Audio manifest exceeds the safe size limit")
        data = json.loads(payload)
        validate_manifest(data)
        if (
            data["media_file"] != Path(plan["source"]).name
            or data["media_sha256"] != plan["expected_media"]["content_sha256"]
            or not self._matches(source, expected)
        ):
            raise OSError("Audio manifest does not match this original")
        expected["content_sha256"] = hashlib.sha256(payload).hexdigest()
        if sidecars and (
            dict(sidecars[0].identity or {}) != expected["stat"]
            or sidecars[0].content_sha256
            and sidecars[0].content_sha256 != expected["content_sha256"]
        ):
            raise OSError("Registered audio manifest identity changed")
        member = dict(
            file_id=sidecars[0].file_id if sidecars else str(uuid4()),
            role=FileRole.SIDECAR.value,
            source=str(source),
            destination=str(destination),
            expected=expected,
            final_expected=expected,
        )
        plan["files"].append(member)
        plan["reserved_paths"] += [str(source), str(destination)]
        if patch:
            stage = str(Path(plan["folder"]) / f".fthr-restore-{plan['op_id']}.stage")
            backup = str(Path(plan["folder"]) / f".fthr-restore-{plan['op_id']}.backup")
            data["media_file"] = Path(plan["destination"]).name
            patched = json.dumps(data, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            with open(stage, "xb") as handle:
                handle.write(patched)
                handle.flush()
                os.fsync(handle.fileno())
            stage_expected = self._evidence(stage, content=True)
            member["final_expected"] = stage_expected
            plan["moves"].append(dict(source=str(source), destination=backup, expected=expected))
            plan["moves"].append(
                dict(
                    source=plan["source"],
                    destination=plan["destination"],
                    expected=plan["expected_media"],
                )
            )
            plan["moves"].append(
                dict(source=stage, destination=str(destination), expected=stage_expected)
            )
            plan["artifacts"] = [
                dict(path=stage, expected=stage_expected),
                dict(path=backup, expected=expected),
            ]
            plan["original_manifest_b64"] = base64.b64encode(payload).decode("ascii")
            plan["patch_manifest_b64"] = base64.b64encode(patched).decode("ascii")
            plan["reserved_paths"] += [stage, backup]

    def _seal(self, plan, kind, clip_id):
        plan["reserved_paths"] = list(dict.fromkeys(plan["reserved_paths"]))
        parents = {str(Path(path).parent) for path in plan["reserved_paths"]}
        parents.update(
            (plan["admission"]["owned_root"], str(Path(plan["folder"]).parent), plan["folder"])
        )
        plan["parents"] = {
            parent: self._parent(str(Path(parent) / "identity-probe")) for parent in parents
        }
        self._boundary(plan)
        for member in plan["files"]:
            if not self._matches(member["source"], member["expected"]):
                raise OSError("Trash bundle changed during preparation")
        version = 2 if kind in ("trash", "purge") and plan["preserve_favorites"] else 1
        operation = RenameOperation(plan["op_id"], version, kind, clip_id, State.PREPARED, plan)
        operation = self.manager.prepare_trash_operation(
            operation, validate=lambda: self._boundary(plan)
        )
        self.host._point("after_prepared", operation)
        return operation

    def prepare_trash(self, clip_id, *, admission, expected_identity=None,
                      preserve_favorites=False):
        if type(preserve_favorites) is not bool:
            raise ValueError("preserve_favorites must be a boolean")
        with self.host._lock:
            primary, files = self._files(clip_id)
            if primary.binding_state in (BindingState.TRASHED, BindingState.PURGING):
                raise OSError("Clip is already in trash")
            admission = self.host._admission(admission)
            source = self.host._absolute(primary.path)
            if (
                classify_media_path(source, admission["owned_root"], admission["imported_roots"])
                is not MediaOwnership.FTHR_OWNED
            ):
                raise OSError("Only owned clips can be moved to trash")
            observed = self._evidence(source)
            if (
                expected_identity is not None
                and self._identity(dict(expected_identity)) != observed["stat"]
            ):
                raise OSError("The selected clip was replaced")
            root = Path(admission["owned_root"])
            self._parent(str(root / "identity-probe"))
            base = root / ".fthr-trash"
            base.mkdir(exist_ok=True)
            self._parent(str(base / "identity-probe"))
            trash_id = str(uuid4())
            folder = base / trash_id
            folder.mkdir()
            plan = self._base_plan(
                primary, files, source, str(folder / Path(source).name), admission, trash_id, folder,
                preserve_favorites=preserve_favorites,
            )
            plan["op_id"] = str(uuid4())
            self._manifest(plan, files, patch=False)
            plan["moves"] = [
                dict(
                    source=member["source"],
                    destination=member["destination"],
                    expected=member["expected"],
                )
                for member in plan["files"]
            ]
            return self._seal(plan, "trash", clip_id)

    def _destination(self, item, destination, admission):
        desired = Path(destination if destination is not None else item.original_path).absolute()
        self._parent(desired)
        if (
            classify_media_path(desired, admission["owned_root"], admission["imported_roots"])
            is not MediaOwnership.FTHR_OWNED
        ):
            raise OSError("Restore destination must belong to the owned library")
        # A missing source path can still have a registered owner.
        for index in range(10000):
            candidate = (
                desired
                if index == 0
                else desired.with_name(f"{desired.stem} (restored {index}){desired.suffix}")
            )
            if not os.path.lexists(candidate) and not os.path.lexists(manifest_path_for(candidate)):
                try:
                    if not self.manager.export_members_snapshot(candidate):
                        return str(candidate)
                except OSError:
                    continue
        raise FileExistsError("No free restore filename is available")

    def prepare_restore(self, trash_id, *, destination=None, admission):
        with self.host._lock:
            item = self._item(trash_id)
            primary, files = self._files(item.clip_id)
            admission = self.host._admission(admission)
            target = self._destination(item, destination, admission)
            plan = self._base_plan(
                primary,
                files,
                self.host._absolute(item.stored_path),
                target,
                admission,
                item.trash_id,
                Path(item.stored_path).parent,
            )
            plan.update(
                op_id=str(uuid4()),
                metadata_source=None,
                original_path=item.original_path,
                stored_path=item.stored_path,
                created_utc=item.created_utc,
            )
            self._manifest(plan, files, patch=True)
            if not plan["moves"]:
                plan["moves"] = [
                    dict(source=plan["source"], destination=target, expected=plan["expected_media"])
                ]
            return self._seal(plan, "restore", item.clip_id)

    def prepare_purge(self, trash_id, *, admission, preserve_favorites=False):
        if type(preserve_favorites) is not bool:
            raise ValueError("preserve_favorites must be a boolean")
        with self.host._lock:
            item = self._item(trash_id)
            primary, files = self._files(item.clip_id)
            source = self.host._absolute(item.stored_path)
            plan = self._base_plan(
                primary, files, source, source, admission, item.trash_id, Path(source).parent,
                preserve_favorites=preserve_favorites,
            )
            plan.update(
                op_id=str(uuid4()),
                metadata_source=None,
                original_path=item.original_path,
                stored_path=item.stored_path,
                created_utc=item.created_utc,
            )
            self._manifest(plan, files, patch=False)
            known = {member["source"] for member in plan["files"]}
            if any(entry.path not in known for entry in os.scandir(plan["folder"])):
                raise OSError("Unknown descendants prevent purging this trash item")
            return self._seal(plan, "purge", item.clip_id)

    def _move(self, move, plan, operation, *, rollback=False):
        deadline = time.monotonic() + 3.0
        last_error = None
        for attempt in range(30):
            if attempt and time.monotonic() >= deadline:
                raise last_error
            try:
                self._boundary(plan)
                if not self._matches(move["source"], move["expected"]):
                    raise OSError("Trash move source changed or is missing")
                if os.path.lexists(move["destination"]):
                    raise FileExistsError("A foreign file blocks the trash move")
                self.host._point("before_member_move", operation)
                if rollback:
                    rename_no_replace(move["source"], move["destination"])
                else:
                    with self.manager.trash_move_guard(
                        operation,
                        move["source"],
                        move["expected"],
                        validate=lambda: self._boundary(plan),
                    ):
                        rename_no_replace(move["source"], move["destination"])
                self.host._point("after_member_move", operation)
                return
            except OSError as error:
                if (
                    getattr(error, "winerror", 0) not in (32, 33)
                    or attempt == 29
                    or time.monotonic() >= deadline
                ):
                    raise
                # Sharing locks are transient. Release every SQLite/manager
                # guard before waiting in this existing file-operation worker.
                last_error = error
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _undo(self, move, plan, operation):
        source, destination = move["source"], move["destination"]
        if os.path.lexists(source):
            if not self._matches(source, move["expected"]):
                raise OSError("A foreign file blocks trash rollback")
            if os.path.lexists(destination) and self._matches(destination, move["expected"]):
                raise OSError("Duplicate trash identities require recovery")
            return
        if not self._matches(destination, move["expected"]):
            raise OSError("Trash rollback member is missing or changed")
        self._move(
            dict(source=destination, destination=source, expected=move["expected"]),
            plan,
            operation,
            rollback=True,
        )

    def _cleanup(self, operation):
        self.host._point("before_cleanup", operation)
        plan = operation.plan
        try:
            self._boundary(plan, missing_folder=True)
            for artifact in plan["artifacts"]:
                if os.path.lexists(artifact["path"]):
                    if not self._matches(artifact["path"], artifact["expected"]):
                        raise OSError("Changed private restore artifact was retained")
                    os.unlink(artifact["path"])
            remove_folder = (
                operation.kind == "trash" and operation.state == State.ROLLED_BACK
            ) or (operation.kind == "restore" and operation.state == State.COMMITTED)
            if remove_folder and os.path.lexists(plan["folder"]):
                self._boundary(plan)
                os.rmdir(plan["folder"])
            operation = self.manager.set_operation_state(
                operation.op_id, operation.state, cleanup_done=True
            )
        except Exception as error:
            # Terminal direction is preserved; only known private artifacts may
            # be removed and uncertain folders remain for an explicit retry.
            operation = self.manager.set_operation_state(
                operation.op_id, operation.state, error=str(error), cleanup_done=False
            )
        self.host._point("after_cleanup", operation)
        return operation

    def _purge(self, operation):
        plan = operation.plan
        try:
            self._boundary(plan, missing_folder=True)
            for member in plan["files"]:
                if not os.path.lexists(member["source"]):
                    continue
                if not self._matches(member["source"], member["expected"]):
                    raise OSError("Changed trash member was retained during purge")
                self.host._point("before_purge_member", operation)
                with self.manager.purge_member_guard(
                    operation,
                    member["source"],
                    member["expected"],
                    validate=lambda: self._boundary(plan),
                ):
                    os.unlink(member["source"])
                self.host._point("after_purge_member", operation)
            if os.path.lexists(plan["folder"]):
                self._boundary(plan)
                os.rmdir(plan["folder"])
            self.host._point("before_cleanup", operation)
            operation = self.manager.finish_purge(
                operation, validate=lambda: self._boundary(plan, missing_folder=True)
            )
            self.host._point("after_cleanup", operation)
        except Exception as error:
            # A durable purge is forward-only even after one or more unlinks.
            operation = self.manager.set_operation_state(
                operation.op_id, State.COMMITTED, error=str(error), cleanup_done=False
            )
        return operation

    def recover(self, operation, *, error=None):
        self.supported(operation)
        if operation.state == State.COMMITTED:
            try:
                self.manager.reload()
            except Exception as failure:
                # Durable direction is known, but a broken cache reload must
                # never trigger undo or remove the remaining recovery barrier.
                self.host._runtime_blocked.update(operation.plan["reserved_paths"])
                return self.manager.set_operation_state(
                    operation.op_id, operation.state, error=str(failure), cleanup_done=False
                )
            return (
                self._purge(operation)
                if operation.kind == "purge" and not operation.cleanup_done
                else (self._cleanup(operation) if not operation.cleanup_done else operation)
            )
        if operation.state == State.ROLLED_BACK:
            return self._cleanup(operation) if not operation.cleanup_done else operation
        try:
            self._boundary(operation.plan)
            operation = self.manager.set_operation_state(
                operation.op_id, State.ROLLING_BACK, error=error
            )
            for move in reversed(operation.plan["moves"]):
                self._undo(move, operation.plan, operation)
            operation = self.manager.set_operation_state(
                operation.op_id, State.ROLLED_BACK, error=error
            )
            return self._cleanup(operation)
        except Exception as failure:
            # Do not guess which uncertain/foreign file should be overwritten.
            return self.manager.set_operation_state(
                operation.op_id, State.RECOVERY_REQUIRED, error=str(failure)
            )

    def execute(self, op_id):
        with self.host._lock:
            operation = self.manager.get_operation(op_id)
            if operation is None:
                raise KeyError(op_id)
            self.supported(operation)
            if operation.state != State.PREPARED:
                return self.host._result(self.recover(operation))
            plan = operation.plan
            try:
                self._boundary(plan)
                for member in plan["files"]:
                    if not self._matches(member["source"], member["expected"]):
                        raise OSError("Trash input changed before execution")
                operation = self.manager.set_operation_state(op_id, State.APPLYING)
                self.host._point("after_applying", operation)
                for move in plan["moves"]:
                    self._move(move, plan, operation)
                self.host._point("before_metadata_commit", operation)
                for member in plan["files"]:
                    target = (
                        member["source"] if operation.kind == "purge" else member["destination"]
                    )
                    expected = (
                        member["expected"]
                        if operation.kind == "purge"
                        else member["final_expected"]
                    )
                    if not self._matches(target, expected):
                        raise OSError("Trash output changed before commit")
                operation = self.manager.commit_trash_operation(
                    op_id, validate=lambda: self._boundary(plan)
                )
                self.host._point("after_metadata_commit", operation)
                if operation.kind == "purge":
                    self.host._point("after_purge_decision", operation)
                    return self.host._result(self._purge(operation))
                return self.host._result(self._cleanup(operation))
            except Exception as error:
                # Fresh durable journal truth always decides recovery direction.
                try:
                    durable = self.manager.get_operation(op_id)
                except Exception:
                    # An unavailable journal can never justify speculative undo.
                    self.host._runtime_blocked.update(plan["reserved_paths"])
                    return MutationResult(
                        op_id,
                        State.RECOVERY_REQUIRED,
                        plan["source"],
                        plan["destination"],
                        str(error),
                    )
                return self.host._result(self.recover(durable or operation, error=str(error)))
