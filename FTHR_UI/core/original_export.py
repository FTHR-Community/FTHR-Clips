"""Cancellable byte-exact original bundles; sources are never modified."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
from uuid import uuid4

from core.audio_manifest import manifest_path_for, validate_manifest
from core.clip_files import FTHR_TEMP_DIR_PREFIX
from core.clip_metadata_types import FileRole
from core.clip_mutations import rename_no_replace
from core.library_index import canonical_media_path
from core.library_ownership import media_file_identity

_BLOCK_SIZE = 1024 * 1024
_MANIFEST_LIMIT = 1024 * 1024
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)


@dataclass(frozen=True)
class OriginalExportResult:
    successes: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    cancelled: bool = False
    output: str | None = None


@dataclass(frozen=True)
class _PreparedOriginal:
    record: object
    sidecar_identity: dict | None
    members: tuple


def _folder_identity(path):
    path = Path(path)
    for ancestor in (path, *path.parents):
        info = os.lstat(ancestor)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & _REPARSE):
            raise OSError('Linked or reparse-point output folders are not supported')
    return info_identity(os.lstat(path))


def info_identity(info):
    if (type(info.st_dev) is not int or info.st_dev < 0
            or type(info.st_ino) is not int or info.st_ino <= 0):
        raise OSError('Stable filesystem identity is unavailable; original export was refused')
    return (info.st_dev, info.st_ino)


def _optional_sidecar(path):
    try:
        evidence = media_file_identity(path)
        _validate_evidence(evidence)
        return evidence
    except FileNotFoundError:
        # Legacy originals can legitimately have no optional audio manifest.
        return None


def _validate_evidence(evidence):
    keys = {'st_dev', 'st_ino', 'size', 'mtime_ns'}
    if (set(evidence) != keys or any(type(value) is not int for value in evidence.values())
            or evidence['st_dev'] < 0 or evidence['st_ino'] <= 0 or evidence['size'] < 0):
        raise OSError('Stable selected-file identity is unavailable; refresh or use a supported filesystem')


def _check_members(record, members):
    primary = canonical_media_path(record.path)
    sidecar = canonical_media_path(manifest_path_for(record.path))
    for member in members:
        if not ((member.role == FileRole.PRIMARY and member.path_key == primary)
                or (member.role == FileRole.SIDECAR and member.path_key == sidecar)):
            raise OSError('Additional registered clip files cannot be exported as an original bundle')


def prepare_originals(records, *, member_snapshot=None):
    """Freeze sidecar and registry evidence before waiting for an export slot."""
    prepared, failures = [], []
    for record in records:
        try:
            if record.file_identity is None:
                raise ValueError('No complete selected-file evidence; refresh and select again')
            _validate_evidence(record.file_identity)
            if media_file_identity(record.path) != record.file_identity:
                raise OSError('The selected original has changed')
            members = tuple(member_snapshot(record.path)) if member_snapshot else ()
            _check_members(record, members)
            sidecar = _optional_sidecar(manifest_path_for(record.path))
            prepared.append(_PreparedOriginal(record, sidecar, members))
        except (OSError, ValueError, TypeError) as error:
            failures.append((record.path, str(error)))
    return tuple(prepared), tuple(failures)


def _member_signature(members):
    # Provenance and optional content-verification annotations do not change
    # bundle membership. Retain identity, role and path changes in the guard.
    return tuple(sorted((member.file_id, member.clip_id, member.role.value,
                         member.path, member.path_key,
                         tuple(sorted(dict(member.identity or {}).items())))
                        for member in members))


def _check_item(item, member_snapshot=None):
    record = item.record
    if media_file_identity(record.path) != record.file_identity:
        raise OSError('The selected original changed during export')
    if _optional_sidecar(manifest_path_for(record.path)) != item.sidecar_identity:
        raise OSError('The audio manifest changed during export')
    if member_snapshot and _member_signature(member_snapshot(record.path)) != _member_signature(item.members):
        raise OSError('Registered clip files changed during export')


def _check_cancel(cancel):
    if cancel.is_set():
        raise InterruptedError('Original export cancelled')


def _handle_identity(handle):
    info = os.fstat(handle.fileno())
    info_identity(info)
    return {'st_dev': info.st_dev, 'st_ino': info.st_ino,
            'size': info.st_size, 'mtime_ns': info.st_mtime_ns}


def _copy_primary(item, destination, cancel, owned):
    record = item.record
    expected = record.file_identity
    digest = hashlib.sha256()
    total = 0
    with open(record.path, 'rb') as source, open(destination, 'xb') as output:
        owned[destination] = info_identity(os.fstat(output.fileno()))
        while True:
            _check_cancel(cancel)
            if (_handle_identity(source) != expected
                    or (total % (16 * _BLOCK_SIZE) == 0
                        and media_file_identity(record.path) != expected)):
                raise OSError('The original changed while it was being copied')
            block = source.read(_BLOCK_SIZE)
            if not block:
                break
            output.write(block)
            digest.update(block)
            total += len(block)
        output.flush()
        os.fsync(output.fileno())
        if (total != expected['size'] or os.fstat(output.fileno()).st_size != expected['size']
                or _handle_identity(source) != expected):
            raise OSError('The original copy is incomplete or changed')
    if media_file_identity(record.path) != expected:
        raise OSError('The original was replaced during copying')
    return digest.hexdigest()


def _copy_manifest(item, package, digest, owned):
    if item.sidecar_identity is None:
        return
    path = manifest_path_for(item.record.path)
    with open(path, 'rb') as source:
        if _handle_identity(source) != item.sidecar_identity:
            raise OSError('The audio manifest was replaced')
        payload = source.read(_MANIFEST_LIMIT + 1)
        if len(payload) > _MANIFEST_LIMIT:
            raise ValueError('The audio manifest exceeds the safe export size')
        if _handle_identity(source) != item.sidecar_identity:
            raise OSError('The audio manifest changed while reading')
    data = json.loads(payload)
    validate_manifest(data)
    if data['media_file'] != Path(item.record.path).name or data['media_sha256'] != digest:
        raise ValueError('The audio manifest does not describe this exact original')
    destination = package / path.name
    with open(destination, 'xb') as output:
        owned[destination] = info_identity(os.fstat(output.fileno()))
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())


def _cleanup_owned(stage, parent, parent_identity, owned):
    """Remove only recorded identities inside this invocation's private stage."""
    if _folder_identity(parent) != parent_identity:
        raise OSError('Output parent changed; private staging files were retained')
    if _folder_identity(stage) != owned[stage]:
        raise OSError('Staging folder changed; files were retained')
    for path in sorted(owned, key=lambda value: len(value.parts), reverse=True):
        # Parent validation prevents following a swapped staging subdirectory.
        parent_seen = _folder_identity(path.parent)
        if path.parent in owned and parent_seen != owned[path.parent]:
            raise OSError('A staging parent changed; files were retained')
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            # An owned staging entry may already be gone; there is nothing to remove.
            continue
        if info_identity(info) != owned[path] or stat.S_ISLNK(info.st_mode):
            raise OSError('A staging identity changed; files were retained')
        if stat.S_ISDIR(info.st_mode):
            path.rmdir()  # Unexpected children are retained, never recursively deleted.
        elif stat.S_ISREG(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & _REPARSE:
            path.unlink()
        else:
            raise OSError('Unexpected staging object; files were retained')


def _validate_stage(stage, owned, copied_stats):
    pending = [stage]
    seen = set()
    while pending:
        directory = pending.pop()
        if _folder_identity(directory) != owned[directory]:
            raise OSError('An export package folder changed before publication')
        seen.add(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path not in owned:
                    raise OSError('Unexpected file appeared inside the private export stage')
                info = os.lstat(path)
                if info_identity(info) != owned[path]:
                    raise OSError('An export package identity changed before publication')
                if stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & _REPARSE:
                    pending.append(path)
                elif path in copied_stats and media_file_identity(path) == copied_stats[path]:
                    seen.add(path)
                else:
                    raise OSError('An original copy changed before publication')
    if seen != set(owned):
        raise OSError('An original copy disappeared before publication')


def export_originals(records, directory, *, cancel=None, member_snapshot=None,
                     prepared=None, initial_failures=(), progress=None):
    """Publish one unique batch directory only after all retained items validate."""
    cancel = cancel if cancel is not None else threading.Event()
    records = tuple(records)
    if prepared is None:
        prepared, failures = prepare_originals(records, member_snapshot=member_snapshot)
    else:
        failures = ()
    failures = list(initial_failures) + list(failures)
    parent = Path(os.path.abspath(os.fspath(directory)))
    parent_identity = _folder_identity(parent)
    token = uuid4().hex
    stage = parent / f'{FTHR_TEMP_DIR_PREFIX}originals-{token}'
    final = parent / f'FTHR-originals-{token}'
    stage.mkdir()  # Exclusive creation; never reuse a preexisting folder.
    owned = {stage: _folder_identity(stage)}
    retained = []
    copied_stats = {}
    published = False
    cancelled = False
    try:
        for index, item in enumerate(prepared):
            _check_cancel(cancel)
            package = stage / f'{index + 1:04d}-{Path(item.record.path).stem[:80]}'
            try:
                if _folder_identity(parent) != parent_identity or _folder_identity(stage) != owned[stage]:
                    raise OSError('Output folders changed during export')
                _check_item(item, member_snapshot)
                package.mkdir()
                owned[package] = _folder_identity(package)
                digest = _copy_primary(item, package / Path(item.record.path).name, cancel, owned)
                _copy_manifest(item, package, digest, owned)
                for path in owned:
                    if path.parent == package:
                        copied_stats[path] = media_file_identity(path)
                _check_item(item, member_snapshot)
                retained.append((item, package))
            except InterruptedError:
                raise
            except (OSError, ValueError, TypeError) as error:
                failures.append((item.record.path, str(error)))
                # A failed item must not be included in a successful final batch.
                item_owned = {path: identity for path, identity in owned.items()
                              if path == package or package in path.parents}
                if item_owned:
                    _cleanup_owned(package, stage, owned[stage], item_owned)
                    for path in item_owned:
                        owned.pop(path, None)
            if progress:
                progress((index + 1) / max(1, len(prepared)) * 95)
        # Revalidate every successful source and authoritative registry directly
        # before publishing; no partial batch is exposed if late evidence moved.
        validated = []
        for item, package in retained:
            _check_cancel(cancel)
            try:
                _check_item(item, member_snapshot)
            except (OSError, ValueError, TypeError) as error:
                failures.append((item.record.path, str(error)))
                item_owned = {path: identity for path, identity in owned.items()
                              if path == package or package in path.parents}
                _cleanup_owned(package, stage, owned[stage], item_owned)
                for path in item_owned:
                    owned.pop(path, None)
            else:
                validated.append((item, package))
        retained = validated
        _check_cancel(cancel)
        if retained:
            if _folder_identity(parent) != parent_identity or _folder_identity(stage) != owned[stage]:
                raise OSError('Output folders changed before publication')
            _validate_stage(stage, owned, copied_stats)
            _check_cancel(cancel)
            rename_no_replace(stage, final)
            published = True
    except InterruptedError:
        cancelled = True
    finally:
        if not published:
            _cleanup_owned(stage, parent, parent_identity, owned)
    return OriginalExportResult(
        tuple(item.record.path for item, _package in retained) if published else (),
        tuple(failures), cancelled, str(final) if published else None)
