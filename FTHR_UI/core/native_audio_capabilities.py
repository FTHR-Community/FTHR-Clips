"""Fail-closed qualification of native Programs audio policy support."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import sys

_CAPABILITY = b'FTHR_AUDIO_EXCLUSIONS_CAPABILITY_V1'
_BACKUP_CAPABILITY = b'FTHR_AUDIO_BACKUP_CAPABILITY_V1'
_verified: set[tuple[str, str, bytes]] = set()


def native_audio_policy_arguments(*, audio_enabled=True, programs_enabled=True,
                                  exclusions='', keep_backup=True) -> list[str]:
    """Emit only policies which change native defaults and require support.

    An older engine rejects unknown named options. Default Programs capture,
    Device mode, and audio-off capture must not receive optional policy flags.
    The launch capability guard qualifies every non-default policy first.
    """
    if not audio_enabled or not programs_enabled:
        return []
    arguments = []
    if exclusions:
        arguments.extend(('--audio-exclusions', exclusions))
    if not keep_backup:
        arguments.extend(('--keep-system-audio-backup', '0'))
    return arguments


def native_audio_exclusions_supported(engine, *, runner=None, timeout=3.0) -> bool:
    return _native_audio_capability_supported(engine, _CAPABILITY,
        '--audio-exclusions-capability', runner=runner, timeout=timeout)


def native_audio_backup_control_supported(engine, *, runner=None, timeout=3.0) -> bool:
    return _native_audio_capability_supported(engine, _BACKUP_CAPABILITY,
        '--audio-backup-capability', runner=runner, timeout=timeout)


def _native_audio_capability_supported(engine, capability, command, *, runner, timeout) -> bool:
    """Never send an unknown probe command to an older capture executable.

    Qualification belongs to the exact executable bytes, not a mutable path or
    cached settings preference. The nested encoder-probe entry point exits
    before capture starts, including on older engines that support that probe.
    """
    try:
        path = Path(engine).resolve(strict=True)
        binary = path.read_bytes()
        if b'--probe-encoder' not in binary or capability not in binary:
            return False
        digest = hashlib.sha256(binary).hexdigest()
        key = (str(path), digest, capability)
        if key in _verified:
            return True
        kwargs = dict(stdin=subprocess.DEVNULL, capture_output=True,
                      timeout=timeout, check=False)
        if sys.platform == 'win32':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        result = (runner or subprocess.run)(
            [str(path), '--probe-encoder', command], **kwargs)
        if (result.returncode != 0 or result.stdout.strip() != capability
                or hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            return False
        _verified.add(key)
        return True
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return False
