"""Bounded reader/writer for native UUID highlight boxes in MP4 files.

The native capture writer appends one top-level ISO-BMFF ``uuid`` box after
the completed trailer.  This module deliberately reads only that bounded
metadata and never scans media payloads or hashes a clip.
"""

from __future__ import annotations

import os
import json
import struct
import stat as stat_module
import threading
import sys
from contextlib import contextmanager
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core.highlight_markers import (
    HighlightMarker,
    HighlightMarkerError,
    MAX_HIGHLIGHT_MARKERS,
    MAX_HIGHLIGHT_METADATA_BYTES,
    decode_highlight_markers,
    markers_to_payload,
    normalize_highlight_markers,
)


HIGHLIGHT_UUID = bytes.fromhex('9a68d1746bf042779b6d8b04e348432a')
_BOX_HEADER = struct.Struct('>I4s')
_MAX_TOP_LEVEL_BOXES = 100_000
_IDENTITY_FIELDS = frozenset(('st_dev', 'st_ino', 'size', 'mtime_ns'))
_KNOWN_MAJOR_BRANDS = frozenset({
    b'isom', b'iso2', b'iso3', b'iso4', b'iso5', b'iso6', b'avc1',
    b'mp41', b'mp42', b'mp71', b'dash', b'qt  ', b'M4V ', b'f4v ',
})
_APPEND_LOCKS: dict[str, threading.Lock] = {}
_APPEND_LOCKS_GUARD = threading.Lock()


def _append_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(os.fspath(path)))
    with _APPEND_LOCKS_GUARD:
        lock = _APPEND_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _APPEND_LOCKS[key] = lock
        return lock


class CaptureHighlightError(HighlightMarkerError):
    """The media or embedded highlight metadata cannot be trusted."""


class CaptureHighlightAppendError(CaptureHighlightError):
    """An append failed after a verified rollback to the original media."""

    def __init__(self, message: str, *, verified_rollback_identity=None):
        super().__init__(message)
        self.verified_rollback_identity = (
            dict(verified_rollback_identity)
            if isinstance(verified_rollback_identity, Mapping)
            else None)


def _path(path: os.PathLike[str] | str) -> Path:
    try:
        value = Path(path)
    except (TypeError, ValueError) as error:
        raise CaptureHighlightError('capture highlight path is invalid') from error
    if value.suffix.casefold() != '.mp4' or not value.name:
        raise CaptureHighlightError('capture highlight source must be a regular MP4 file')
    return value


def _identity_path(path: os.PathLike[str] | str) -> Path:
    """Coerce a supported library media path for identity-only operations."""
    try:
        value = Path(path)
    except (TypeError, ValueError) as error:
        raise CaptureHighlightError('capture highlight path is invalid') from error
    if not value.name or '\0' in str(value):
        raise CaptureHighlightError('capture highlight source is invalid')
    return value


def _path_stat(path: Path):
    try:
        path_stat = os.lstat(path)
    except OSError as error:
        raise CaptureHighlightError('capture highlight source is unavailable') from error
    if not stat_module.S_ISREG(path_stat.st_mode):
        raise CaptureHighlightError('capture highlight source is not a regular file')
    return path_stat


def media_identity(path: os.PathLike[str] | str) -> dict[str, int]:
    """Return the four-stat identity used by metadata ownership guards."""

    source = _identity_path(path)
    handle = _open_stable(source, writable=False)
    try:
        descriptor = _identity_from_stat(os.fstat(handle.fileno()))
        path_identity = _identity_from_stat(_path_stat(source))
        if descriptor != path_identity:
            raise CaptureHighlightError('capture highlight source changed before reading')
        result = descriptor
    finally:
        handle.close()
    if any(value < 0 for value in result.values()):
        raise CaptureHighlightError('capture highlight source identity is invalid')
    return result


def _identity_from_stat(stat) -> dict[str, int]:
    result = {
        'st_dev': int(stat.st_dev),
        'st_ino': int(stat.st_ino),
        'size': int(stat.st_size),
        'mtime_ns': int(stat.st_mtime_ns),
    }
    if any(value < 0 for value in result.values()):
        raise CaptureHighlightError('capture highlight source identity is invalid')
    return result


def _windows_handle_identity(handle):
    """Reject reparse/non-disk handles before wrapping a Windows handle.

    ``FILE_FLAG_OPEN_REPARSE_POINT`` makes the open itself non-following.  A
    small handle-type check keeps named pipes/devices out of the MP4 parser;
    the regular-file fstat below remains the final cross-platform guard.
    """
    if sys.platform != 'win32':
        return
    import ctypes
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    FILE_TYPE_DISK = 0x0001
    FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
    FILE_INFO_BY_HANDLE_FILE_ATTRIBUTE_TAG_INFO = 9
    kernel32.GetFileType.argtypes = [ctypes.c_void_p]
    kernel32.GetFileType.restype = ctypes.c_uint32
    if kernel32.GetFileType(handle) != FILE_TYPE_DISK:
        raise CaptureHighlightError('capture highlight source is not a disk file')
    # FILE_ATTRIBUTE_REPARSE_POINT is intentionally refused even though the
    # handle was opened without following a reparse point.
    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [('FileAttributes', ctypes.c_uint32),
                    ('ReparseTag', ctypes.c_uint32)]
    info = _FileAttributeTagInfo()
    kernel32.GetFileInformationByHandleEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.GetFileInformationByHandleEx.restype = ctypes.c_bool
    if not kernel32.GetFileInformationByHandleEx(
            handle, FILE_INFO_BY_HANDLE_FILE_ATTRIBUTE_TAG_INFO,
            ctypes.byref(info), ctypes.sizeof(info)):
        raise CaptureHighlightError('could not inspect capture highlight handle')
    if info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise CaptureHighlightError('reparse-point media is refused')


def _close_windows_handle(raw_handle) -> None:
    """Close a CreateFileW handle with the correct pointer ABI."""
    import ctypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.CloseHandle(raw_handle)


def _open_stable(path: Path, *, writable: bool):
    """Open one no-follow descriptor and prove it is the current regular file."""
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, 'O_NONBLOCK', 0)
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    raw_handle = None
    fd = None
    try:
        if sys.platform == 'win32':
            import ctypes
            import msvcrt
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.CreateFileW.argtypes = [
                ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel32.CreateFileW.restype = ctypes.c_void_p
            access = 0x80000000 | (0x40000000 if writable else 0)
            raw_handle = kernel32.CreateFileW(
                str(path), access, 0x00000001, None, 3, 0x00200000, None)
            if raw_handle in (None, ctypes.c_void_p(-1).value):
                # Preserve winerror so callers can distinguish a temporary
                # sharing violation from permissions or a missing source.
                raise ctypes.WinError(ctypes.get_last_error())
            _windows_handle_identity(raw_handle)
            fd = msvcrt.open_osfhandle(int(raw_handle), os.O_BINARY)
            raw_handle = None  # fd now owns the Windows handle
        else:
            fd = os.open(path, flags)
        descriptor = os.fstat(fd)
        if not stat_module.S_ISREG(descriptor.st_mode):
            raise CaptureHighlightError('capture highlight source is not a regular file')
        path_stat = _path_stat(path)
        identity = _identity_from_stat(descriptor)
        if identity != _identity_from_stat(path_stat):
            raise CaptureHighlightError('capture highlight source changed before reading')
        return os.fdopen(fd, 'r+b' if writable else 'rb', closefd=True)
    except CaptureHighlightError:
        if fd is not None:
            os.close(fd)
        elif raw_handle is not None and sys.platform == 'win32':
            _close_windows_handle(raw_handle)
        raise
    except (OSError, ValueError) as error:
        if fd is not None:
            os.close(fd)
        elif raw_handle is not None and sys.platform == 'win32':
            _close_windows_handle(raw_handle)
        raise CaptureHighlightError('could not open capture highlight source') from error


@contextmanager
def _writer_lock(path: Path):
    lock = _append_lock(path)
    if not lock.acquire(blocking=False):
        raise CaptureHighlightError('capture highlight source is busy')
    descriptor_lock = None
    try:
        yield
    finally:
        if descriptor_lock is not None:
            descriptor_lock.release()
        lock.release()


def _check_identity(expected_identity: Mapping[str, Any] | None,
                    actual: Mapping[str, int]) -> None:
    if expected_identity is None:
        return
    if (not isinstance(expected_identity, Mapping)
            or set(expected_identity) != _IDENTITY_FIELDS
            or any(type(value) is not int for value in expected_identity.values())
            or dict(expected_identity) != dict(actual)):
        raise CaptureHighlightError('capture highlight source changed')


def _rollback_capture_highlight_append(
        handle, source: Path, before: Mapping[str, int], start: int,
        ) -> tuple[dict[str, int] | None, OSError | None]:
    """Rollback one append only while the original descriptor still owns it.

    The returned identity is a sealed post-close path identity.  A path or
    descriptor replacement is deliberately reported as an unverified rollback
    so callers cannot turn a foreign file into a recoverable warning.
    """

    try:
        own = _identity_from_stat(os.fstat(handle.fileno()))
        current_path = _identity_from_stat(_path_stat(source))
        if (own['st_dev'] != before['st_dev']
                or own['st_ino'] != before['st_ino']
                or current_path['st_dev'] != before['st_dev']
                or current_path['st_ino'] != before['st_ino']
                or own['size'] < start):
            return None, None
        handle.flush()
        os.ftruncate(handle.fileno(), start)
        os.fsync(handle.fileno())
        rolled = _identity_from_stat(os.fstat(handle.fileno()))
        if (rolled['st_dev'] != before['st_dev']
                or rolled['st_ino'] != before['st_ino']
                or rolled['size'] != before['size']):
            return None, None
        handle.close()
        final = _identity_from_stat(_path_stat(source))
        if (final['st_dev'] != before['st_dev']
                or final['st_ino'] != before['st_ino']
                or final['size'] != before['size']
                or final['mtime_ns'] < rolled['mtime_ns']):
            return None, None
        return final, None
    except CaptureHighlightError:
        return None, None
    except (OSError, ValueError) as error:
        return None, error


def _read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise CaptureHighlightError('capture highlight metadata is truncated')
    return data


def _is_plausible_ftyp(payload: bytes) -> bool:
    if len(payload) < 8:
        return False
    major = payload[:4]
    if major in _KNOWN_MAJOR_BRANDS:
        return True
    # A vendor brand is still plausible when it uses the ISO printable brand
    # grammar.  This avoids rejecting valid MP4 variants while excluding a
    # random binary prefix from being mistaken for a container.
    return all(0x20 <= byte <= 0x7e for byte in major)


def _merge_markers(target: dict[str, HighlightMarker], markers) -> None:
    for marker in markers:
        previous = target.get(marker.marker_id)
        if previous is not None and previous != marker:
            raise CaptureHighlightError(
                'conflicting duplicate highlight marker metadata')
        target[marker.marker_id] = marker
        if len(target) > MAX_HIGHLIGHT_MARKERS:
            raise CaptureHighlightError(
                f'at most {MAX_HIGHLIGHT_MARKERS} highlight markers are supported')


def _decode_uuid_payload(payload: bytes):
    if len(payload) < len(HIGHLIGHT_UUID) or payload[:16] != HIGHLIGHT_UUID:
        return None
    marker_payload = payload[16:]
    if len(marker_payload) > MAX_HIGHLIGHT_METADATA_BYTES:
        raise CaptureHighlightError(
            f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
    try:
        return decode_highlight_markers(marker_payload)
    except HighlightMarkerError as error:
        raise CaptureHighlightError(str(error)) from error


def _read_capture_highlights_handle(handle, source: Path,
                                    expected_identity: Mapping[str, Any] | None = None,
                                    ) -> tuple[tuple[HighlightMarker, ...] | None, bool]:
    """Parse one already-secured descriptor and report an incomplete tail."""
    descriptor_before = _identity_from_stat(os.fstat(handle.fileno()))
    path_before = _identity_from_stat(_path_stat(source))
    _check_identity(expected_identity, descriptor_before)
    if descriptor_before != path_before:
        raise CaptureHighlightError('capture highlight source changed before reading')
    file_size = descriptor_before['size']
    marker_map: dict[str, HighlightMarker] = {}
    saw_own_box = False
    saw_ftyp = False
    saw_tail = False
    box_count = 0
    owned_bytes = 0
    handle.seek(0)
    while handle.tell() < file_size:
        box_start = handle.tell()
        header = handle.read(8)
        if len(header) < 8:
            saw_tail = True
            break
        box_count += 1
        if box_count > _MAX_TOP_LEVEL_BOXES:
            raise CaptureHighlightError('MP4 contains too many top-level boxes')
        size32, box_type = _BOX_HEADER.unpack(header)
        header_size = 8
        if size32 == 1:
            extended = handle.read(8)
            if len(extended) < 8:
                saw_tail = True
                break
            size = struct.unpack('>Q', extended)[0]
            header_size = 16
        elif size32 == 0:
            size = file_size - box_start
        else:
            size = size32
        if size < header_size:
            raise CaptureHighlightError('malformed MP4 top-level box size')
        if box_start + size > file_size:
            # A declared larger-than-EOF final box is an uncommitted append.
            saw_tail = True
            break
        payload_size = size - header_size
        if box_type == b'ftyp':
            probe = _read_exact(handle, min(payload_size, 4096))
            if not _is_plausible_ftyp(probe):
                raise CaptureHighlightError('source is not a plausible MP4 file')
            saw_ftyp = True
            if payload_size > len(probe):
                handle.seek(payload_size - len(probe), os.SEEK_CUR)
        elif box_type == b'mdat':
            handle.seek(payload_size, os.SEEK_CUR)
        elif box_type == b'uuid':
            if payload_size < 16:
                # A short terminal UUID is a writer tail, while a complete
                # nonterminal UUID remains malformed container data.
                if box_start + size == file_size:
                    saw_tail = True
                    break
                handle.seek(payload_size, os.SEEK_CUR)
                continue
            user_type = _read_exact(handle, 16)
            own_size = payload_size - 16
            if user_type != HIGHLIGHT_UUID:
                handle.seek(own_size, os.SEEK_CUR)
                continue
            if size32 == 0:
                # Native and this writer always emit explicit sizes.  Refuse
                # size-zero own boxes before reading their unbounded payload;
                # they are not a recoverable terminal append.
                raise CaptureHighlightError(
                    'size-zero own highlight box is unsupported')
            else:
                saw_own_box = True
                owned_bytes += own_size
                if owned_bytes > MAX_HIGHLIGHT_METADATA_BYTES:
                    raise CaptureHighlightError(
                        f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
                decoded = _decode_uuid_payload(user_type + _read_exact(handle, own_size))
                if decoded is not None:
                    _merge_markers(marker_map, decoded)
        else:
            handle.seek(payload_size, os.SEEK_CUR)
        if (size32 == 0 and box_start + size == file_size
                and box_type != b'uuid'):
            # ISO size-zero boxes consume the remainder of the file.  They
            # are readable, but an append would become part of that box and
            # hide the new UUID metadata inside it.
            saw_tail = True
        if handle.tell() != box_start + size:
            handle.seek(box_start + size, os.SEEK_SET)
    descriptor_after = _identity_from_stat(os.fstat(handle.fileno()))
    path_after = _identity_from_stat(_path_stat(source))
    _check_identity(expected_identity, descriptor_after)
    if descriptor_before != descriptor_after or descriptor_after != path_after:
        raise CaptureHighlightError('capture highlight source changed while reading')
    if not saw_ftyp:
        raise CaptureHighlightError('source is not a plausible MP4 file')
    markers = (tuple(sorted(marker_map.values(),
                            key=lambda marker: (marker.position_100ns, marker.marker_id)))
               if saw_own_box else None)
    return markers, saw_tail


def read_capture_highlights(
        path: os.PathLike[str] | str,
        expected_identity: Mapping[str, Any] | None = None,
        ) -> tuple[HighlightMarker, ...] | None:
    """Read and merge committed native UUID boxes through one stable handle."""
    source = _path(path)
    try:
        with _open_stable(source, writable=False) as handle:
            markers, _tail = _read_capture_highlights_handle(
                handle, source, expected_identity=expected_identity)
            return markers
    except CaptureHighlightError:
        raise
    except (OSError, OverflowError, struct.error, ValueError) as error:
        raise CaptureHighlightError('could not read capture highlight metadata') from error


def _uuid_box(markers) -> bytes:
    payload = markers_to_payload(markers)
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    total = 8 + 16 + len(encoded)
    if total > 0xFFFFFFFF:
        raise CaptureHighlightError('capture highlight box is too large')
    return struct.pack('>I4s', total, b'uuid') + HIGHLIGHT_UUID + encoded


def write_capture_highlights(
        path: os.PathLike[str] | str,
        markers,
        expected_identity: Mapping[str, Any] | None = None,
        ) -> dict[str, int]:
    """Append one canonical UUID box after a stable finalized MP4.

    Existing valid own metadata must match the requested final marker set; a
    conflicting or future payload fails closed instead of being overwritten.
    """

    source = _path(path)
    normalized = normalize_highlight_markers(markers)
    box = _uuid_box(normalized)
    if len(box) - 24 > MAX_HIGHLIGHT_METADATA_BYTES:
        raise CaptureHighlightError(
            f'highlight metadata exceeds {MAX_HIGHLIGHT_METADATA_BYTES} bytes')
    with _writer_lock(source):
        handle = None
        before = None
        start = None
        after_write = None
        try:
            handle = _open_stable(source, writable=True)
            before = _identity_from_stat(os.fstat(handle.fileno()))
            _check_identity(expected_identity, before)
            existing, tail = _read_capture_highlights_handle(
                handle, source, expected_identity=before)
            if tail:
                raise CaptureHighlightError(
                    'capture highlight source has an incomplete terminal box')
            if existing is not None:
                if tuple(existing) != tuple(normalized):
                    raise CaptureHighlightError(
                        'existing capture highlight metadata conflicts with final markers')
                return before
            # POSIX advisory locking closes the cross-process append race. The
            # Windows handle denies write/delete sharing for the whole operation.
            if sys.platform != 'win32':
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise CaptureHighlightError('capture highlight source is busy') from error
            current = _identity_from_stat(os.fstat(handle.fileno()))
            path_current = _identity_from_stat(_path_stat(source))
            if current != before or path_current != before:
                raise CaptureHighlightError('capture highlight source changed before append')
            start = before['size']
            handle.seek(start)
            remaining = memoryview(box)
            while remaining:
                written = os.write(handle.fileno(), remaining)
                if written <= 0:
                    raise OSError('short capture highlight append')
                remaining = remaining[written:]
            handle.flush()
            os.fsync(handle.fileno())
            after_write = _identity_from_stat(os.fstat(handle.fileno()))
            if (after_write['size'] != before['size'] + len(box)
                    or after_write['st_dev'] != before['st_dev']
                    or after_write['st_ino'] != before['st_ino']):
                raise CaptureHighlightError('capture highlight append did not commit cleanly')
        except CaptureHighlightError as error:
            if handle is not None and start is not None:
                rolled, rollback_error = _rollback_capture_highlight_append(
                    handle, source, before, start)
                if rolled is not None:
                    raise CaptureHighlightAppendError(
                        str(error), verified_rollback_identity=rolled) from error
                if rollback_error is not None:
                    raise CaptureHighlightError(
                        f'capture highlight append failed and rollback failed: '
                        f'{rollback_error}') from error
            raise
        except (OSError, OverflowError, ValueError) as error:
            if handle is not None and start is not None:
                rolled, rollback_error = _rollback_capture_highlight_append(
                    handle, source, before, start)
                if rolled is not None:
                    raise CaptureHighlightAppendError(
                        'could not append capture highlight metadata',
                        verified_rollback_identity=rolled) from error
                if rollback_error is not None:
                    raise CaptureHighlightError(
                        f'capture highlight append failed and rollback failed: '
                        f'{rollback_error}') from error
            raise CaptureHighlightError('could not append capture highlight metadata') from error
        finally:
            if handle is not None:
                handle.close()
        try:
            final = _identity_from_stat(_path_stat(source))
        except (OSError, CaptureHighlightError) as error:
            raise CaptureHighlightError('capture highlight final identity unavailable') from error
        if (after_write is None
                or final['st_dev'] != after_write['st_dev']
                or final['st_ino'] != after_write['st_ino']
                or final['size'] != after_write['size']
                or final['mtime_ns'] < after_write['mtime_ns']):
            raise CaptureHighlightError(
                'capture highlight final identity changed after append')
        return final


# Descriptive aliases used by post-processing callers.
append_capture_highlights = write_capture_highlights
read_embedded_highlights = read_capture_highlights
