"""Replay storage preferences, native arguments, and folder diagnostics."""

from dataclasses import asdict, dataclass
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


REPLAY_STORAGE_KEY = 'replay_storage'
MIB = 1024 * 1024
MIN_BUDGET_MIB = 64
MAX_MEMORY_MIB = 16_384
MAX_DISK_MIB = 1_048_576
DEFAULT_MEMORY_MIB = 2_048
DEFAULT_DISK_MIB = 4_096


def _budget(value, default, maximum):
    if type(value) not in (int, str):
        return default
    try:
        return max(MIN_BUDGET_MIB, min(maximum, int(value)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ReplayStorageSettings:
    mode: str = 'memory'
    folder: str = ''
    memory_mib: int = DEFAULT_MEMORY_MIB
    disk_mib: int = DEFAULT_DISK_MIB
    budget_mode: str = 'auto'
    allow_low_resources: bool = False

    @classmethod
    def from_value(cls, value):
        if not isinstance(value, dict):
            return cls()
        mode = 'disk' if value.get('mode') == 'disk' else 'memory'
        folder = value.get('folder', '')
        if (not isinstance(folder, str) or any(ord(char) < 32 for char in folder)
                or (folder and not Path(folder).is_absolute())):
            mode, folder = 'memory', ''
        return cls(
            mode, folder,
            _budget(value.get('memory_mib'), DEFAULT_MEMORY_MIB, MAX_MEMORY_MIB),
            _budget(value.get('disk_mib'), DEFAULT_DISK_MIB, MAX_DISK_MIB),
            'manual' if value.get('budget_mode') == 'manual' else 'auto',
            value.get('allow_low_resources') is True,
        )

    def to_dict(self):
        return asdict(self)

    @property
    def directory(self) -> Path:
        return Path(self.folder or tempfile.gettempdir())

    def launch_args(self) -> list[str]:
        args = ['--replay-storage', self.mode]
        if self.budget_mode == 'manual':
            args.extend(['--replay-memory-mb', str(self.memory_mib)])
        else:
            args.extend(['--replay-budget-mode', 'auto'])
        if self.mode == 'disk':
            args.extend(['--replay-storage-path', str(self.directory)])
            if self.budget_mode == 'manual':
                args.extend(['--replay-storage-mb', str(self.disk_mib)])
        if self.allow_low_resources:
            args.extend(['--replay-low-resources', 'attempt'])
        return args


def estimate_replay_disk_bytes(buffer_seconds, bitrate_kbps, audio_tracks):
    """Estimate replay history plus one concurrent save and chunk headroom."""
    video_bytes_per_second = bitrate_kbps * 125
    audio_bytes_per_second = audio_tracks * 48_000 * 2 * 4
    history = (buffer_seconds + 5) * (video_bytes_per_second * 5 // 4
                                     + audio_bytes_per_second)
    # Native storage uses 16 MiB chunks; two partial boundary chunks may
    # remain pinned while capture advances during a save.
    return 2 * history + 32 * MIB


@dataclass(frozen=True)
class ReplayFolderStatus:
    free_bytes: int | None = None
    media: str = 'unknown'
    error: str = ''


def inspect_replay_folder(directory: Path) -> ReplayFolderStatus:
    """Probe on a worker thread. The native engine rechecks during startup."""
    try:
        if any(ord(char) < 32 for char in str(directory)):
            return ReplayFolderStatus(error='Choose a replay folder without control characters.')
        directory = directory.resolve(strict=True)
        if not directory.is_dir():
            return ReplayFolderStatus(error='Choose a folder for temporary replay data.')
        with tempfile.TemporaryFile(dir=directory, prefix='fthr-replay-check-') as probe:
            probe.write(b'\0')
            probe.flush()
        free_bytes = shutil.disk_usage(directory).free
    except (OSError, ValueError):
        return ReplayFolderStatus(
            error='The replay folder is unavailable or unwritable. Choose another folder.')
    media = 'unknown'
    if sys.platform == 'win32':
        media = _windows_media(directory)
    elif sys.platform.startswith('linux'):
        media = _linux_media(directory)
    return ReplayFolderStatus(free_bytes=free_bytes, media=media)


def _windows_media(directory):
    # Match the deepest partition mount point, then identify its physical
    # disk by UniqueId. A drive letter is insufficient for mounted folders.
    script = r'''
$ErrorActionPreference = 'Stop'
$folder = $env:FTHR_REPLAY_PROBE_PATH.TrimEnd('\') + '\'
$partition = Get-Partition | ForEach-Object {
    $item = $_
    foreach ($mount in $item.AccessPaths) {
        if ($mount) {
            $prefix = $mount.TrimEnd('\') + '\'
            if ($folder.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                [pscustomobject]@{ Length = $prefix.Length; DiskNumber = $item.DiskNumber }
            }
        }
    }
} | Sort-Object Length -Descending | Select-Object -First 1
if ($null -eq $partition) { return }
$disk = Get-Disk -Number $partition.DiskNumber
if ([string]::IsNullOrWhiteSpace($disk.UniqueId)) { return }
$media = @(Get-PhysicalDisk | Where-Object { $_.UniqueId -eq $disk.UniqueId })
if ($media.Count -eq 1) { $media[0].MediaType }
'''
    shell = (Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
             / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe')
    try:
        result = subprocess.run(
            [str(shell), '-NoProfile', '-NonInteractive', '-Command', script],
            env={**os.environ, 'FTHR_REPLAY_PROBE_PATH': str(directory)},
            creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True,
            timeout=8, check=True, text=True, errors='replace',
        )
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    media = result.stdout.strip().lower()
    return media if media in {'hdd', 'ssd'} else 'unknown'


def _linux_media(directory):
    try:
        device_id = directory.stat().st_dev
        device = Path(f'/sys/dev/block/{os.major(device_id)}:{os.minor(device_id)}')
        device = device.resolve(strict=True)
        if (device / 'partition').exists():
            device = device.parent
        # Virtual/aggregate devices do not always describe the backing media.
        if '/virtual/' in device.as_posix():
            return 'unknown'
        rotational = (device / 'queue/rotational').read_text().strip()
        return {'1': 'hdd', '0': 'ssd'}.get(rotational, 'unknown')
    except OSError:
        return 'unknown'
