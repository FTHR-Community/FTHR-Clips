"""Path fitting and bounded retries for generated file outputs."""

from __future__ import annotations

import errno
import hashlib
import os
import time
from collections.abc import Callable
from itertools import count
from pathlib import Path
from typing import ParamSpec, TypeVar


WINDOWS_CAPTURE_PATH_UNITS = 255
_DIGEST_BYTES = 6
_DIGEST_UNITS = _DIGEST_BYTES * 2
_LOCK_RETRY_DELAYS = (0.02, 0.04, 0.06)

_P = ParamSpec('_P')
_T = TypeVar('_T')


class OutputPathTooLongError(ValueError):
    """The configured root leaves insufficient space for a generated output."""


def windows_path_units(path: str | os.PathLike[str]) -> int:
    return len(os.fspath(path).encode('utf-16-le', errors='surrogatepass')) // 2


def _validate_component(name: str) -> None:
    if not name or name in ('.', '..') or any(char in name for char in '/\\:\0'):
        raise ValueError('Generated names must be single path components.')


def _component_budget(parent: Path, max_units: int) -> int:
    return max_units - windows_path_units(parent / 'x') + 1


def _name_digest(identity: str) -> str:
    return hashlib.blake2s(
        identity.encode('utf-16-le', errors='surrogatepass'), digest_size=_DIGEST_BYTES,
    ).hexdigest()


def _shortened_component(name: str, suffix: str, budget: int, *, identity: str) -> str:
    digest = _name_digest(identity)
    prefix_units = budget - windows_path_units(suffix) - _DIGEST_UNITS - 1
    # Decode only whole characters if the cut falls between UTF-16 surrogates.
    encoded = name.encode('utf-16-le', errors='surrogatepass')
    prefix = encoded[:2 * max(0, prefix_units)].decode(
        'utf-16-le', errors='ignore').rstrip('. _')
    return (f'{prefix}_{digest}' if prefix else digest) + suffix


def next_available_output_path(
    root: str | Path,
    stem: str,
    extension: str,
    *,
    generated_subdir: str | None = None,
    stem_suffix: str = '',
    max_units: int = WINDOWS_CAPTURE_PATH_UNITS,
) -> Path:
    """Pick an unused generated path without moving or renaming the root.

    The required stem suffix and extension survive shortening. Only an explicitly
    supplied generated subdirectory can be shortened if the filename alone cannot
    fit. The caller still needs a no-overwrite publication step for raced files.
    """
    if not stem:
        raise ValueError('Generated output needs a filename stem.')
    suffix = stem_suffix + extension
    _validate_component(stem + suffix)
    if generated_subdir is not None:
        _validate_component(generated_subdir)

    directory = Path(root)
    suffix_units = windows_path_units(suffix)
    min_name_units = suffix_units + 1
    if generated_subdir is not None:
        child_budget = _component_budget(directory, max_units) - 1 - min_name_units
        if windows_path_units(generated_subdir) > child_budget:
            if child_budget < _DIGEST_UNITS:
                raise OutputPathTooLongError(f'Cannot fit an output below {root}.')
            # Keep room for the full file token when the shortened folder allows.
            preferred_stem_units = min(windows_path_units(stem), _DIGEST_UNITS)
            child_budget = max(_DIGEST_UNITS, child_budget - preferred_stem_units + 1)
            generated_subdir = _shortened_component(
                generated_subdir, '', child_budget, identity=generated_subdir,
            )
        directory /= generated_subdir

    name_budget = _component_budget(directory, max_units)
    if min_name_units > name_budget:
        raise OutputPathTooLongError(f'Cannot fit an output below {root}.')

    compact_attempts = 0
    stem_budget = name_budget - suffix_units
    for sequence in count(1):
        sequence_suffix = '' if sequence == 1 else f'_{sequence}'
        name = f'{stem}{stem_suffix}{sequence_suffix}{extension}'
        if windows_path_units(name) > name_budget:
            if stem_budget >= _DIGEST_UNITS:
                name = _shortened_component(stem, suffix, name_budget, identity=name)
            else:
                # For very deep roots, walk the available compact names without
                # hashing each sequence into a potentially occupied slot again.
                slots = 16 ** stem_budget
                if compact_attempts >= min(slots, 256):
                    raise FileExistsError(errno.EEXIST, 'No unused output name', directory)
                start = int(_name_digest(stem), 16)
                token = (start + compact_attempts) % slots
                name = f'{token:0{stem_budget}x}{suffix}'
                compact_attempts += 1
        candidate = directory / name
        if not os.path.lexists(candidate):
            return candidate


def _is_transient_lock(error: OSError) -> bool:
    winerror = getattr(error, 'winerror', None)
    if winerror is not None:
        # Windows reports sharing and byte-range lock violations as EACCES.
        return winerror in (32, 33)
    return error.errno in (errno.EBUSY, errno.ETXTBSY)


def retry_file_operation(
    operation: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs,
) -> _T:
    """Retry a filesystem operation on sharing or lock violations for 120 ms."""
    for delay in _LOCK_RETRY_DELAYS:
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError) as error:
            # Ownership readers report open failures as ValueError with the
            # native OSError as their explicit cause.
            cause = error if isinstance(error, OSError) else error.__cause__
            if not isinstance(cause, OSError) or not _is_transient_lock(cause):
                raise
        time.sleep(delay)
    return operation(*args, **kwargs)
