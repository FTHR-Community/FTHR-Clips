"""clip_deduplicator.py
Detect and deduplicate overlapping recorded clips to save disk space.

Identifies shared frames/time-ranges between clips (e.g. repeated clipping
within the buffer duration) and merges or trims duplicate portions using FFmpeg
stream copying (-c copy), requiring minimal RAM (< 30 MB).

Note: Due to -c copy, trimming occurs at the nearest keyframe (I-frame),
so a 1-2 second jump or duplicate frames may appear at the stitch boundary.

TODO: Greedy pairing currently pairs earliest overlapping clips 1-to-1.
Chains of overlapping clips (A->B->C) require running deduplication multiple times.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import stat
from typing import Callable, Sequence

from core.audio_manifest import MANIFEST_SUFFIX
from core.clip_files import is_completed_video_path, iter_safe_tree
from core.ffmpeg_tools import get_ffmpeg_exe
from core.media_metadata import probe_video_metadata

try:
    from send2trash import send2trash
except ImportError:
    # Native fallback for moving files to OS trash / recycle bin without external dependencies
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        class _SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ('hwnd', wintypes.HWND),
                ('wFunc', wintypes.UINT),
                ('pFrom', ctypes.c_void_p),  # c_void_p prevents c_wchar_p null-truncation
                ('pTo', wintypes.LPCWSTR),
                ('fFlags', wintypes.WORD),
                ('fAnyOperationsAborted', wintypes.BOOL),
                ('hNameMappings', ctypes.c_void_p),
                ('lpszProgressTitle', wintypes.LPCWSTR),
            ]

        def send2trash(path: str | Path) -> None:
            """Move a file to the Windows Recycle Bin using SHFileOperationW."""
            p = Path(path).resolve()
            if not p.exists():
                return
            # SHFileOperationW requires a double-null terminated path string.
            # Use create_unicode_buffer so the buffer stays alive, and c_void_p
            # (not LPCWSTR / c_wchar_p) so Python does not truncate at the first \0.
            buf = ctypes.create_unicode_buffer(str(p) + '\0')  # create_unicode_buffer appends \0 → double-null
            op = _SHFILEOPSTRUCTW()
            op.wFunc = 0x0003  # FO_DELETE
            op.pFrom = ctypes.addressof(buf)
            op.fFlags = 0x0040 | 0x0010 | 0x0004  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
            res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
            if res != 0 or op.fAnyOperationsAborted:
                # Fallback to local hidden quarantine directory if recycle bin is unavailable
                q_dir = p.parent / '.fthr_quarantine'
                q_dir.mkdir(parents=True, exist_ok=True)
                dest = get_safe_output_path(q_dir / p.name)
                shutil.move(str(p), str(dest))
    else:
        def send2trash(path: str | Path) -> None:
            """Move a file to trash directory on POSIX systems or local quarantine fallback."""
            p = Path(path).resolve()
            if not p.exists():
                return
            q_dir = p.parent / '.fthr_quarantine'
            q_dir.mkdir(parents=True, exist_ok=True)
            dest = get_safe_output_path(q_dir / p.name)
            shutil.move(str(p), str(dest))


def _is_file_locked(path: Path) -> bool:
    """Return True if the file cannot be opened for writing because it is locked by another process.

    Read-only files are not considered locked because the OS Recycle Bin / trash
    can move read-only files without issues.
    """
    if not path.exists():
        return False
    try:
        st = path.stat()
        # If the file is marked read-only, opening with 'r+b' would raise PermissionError
        # purely due to the read-only attribute, not because another process has it locked.
        if not (st.st_mode & stat.S_IWRITE):
            return False
    except OSError:
        # File stat failed; treat un-statable file as not locked by another process
        return False

    try:
        with path.open("r+b"):
            return False
    except PermissionError:
        # File has write permission in mode flags, but open('r+b') was denied -> held by another process
        return True
    except OSError:
        # Non-permission I/O error means file is not locked by another process
        return False


_DATE_PATTERNS = [
    # FTHR default format: e.g. 17Sep2026_21-58-05
    (re.compile(r'(\d{1,2}[A-Za-z]{3}\d{4}_\d{2}-\d{2}-\d{2})'), '%d%b%Y_%H-%M-%S'),
    # Standard ISO format: e.g. 2026-09-17_21-58-05
    (re.compile(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})'), '%Y-%m-%d_%H-%M-%S'),
    # Compact format: e.g. 20260917_215805
    (re.compile(r'(\d{8}_\d{6})'), '%Y%m%d_%H%M%S'),
]
_NO_WINDOW = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}


@dataclass
class ClipRecord:
    path: Path
    start_time: float      # Unix epoch timestamp in seconds
    duration: float        # Duration in seconds
    end_time: float        # start_time + duration
    size_bytes: int
    width: int = 0
    height: int = 0
    fps: float = 0.0
    timestamp_confidence: str = 'HIGH'  # 'HIGH' or 'LOW'

    @property
    def formatted_start(self) -> str:
        return datetime.datetime.fromtimestamp(self.start_time).strftime('%Y-%m-%d %H:%M:%S')


@dataclass
class OverlapPair:
    first: ClipRecord
    second: ClipRecord
    overlap_seconds: float
    estimated_saved_bytes: int
    confidence: str = 'HIGH'       # 'HIGH' if both HIGH, else 'LOW'
    is_chained: bool = False       # True if part of an A->B->C chain
    actual_saved_bytes: int = 0    # Populated after merge
    clips: list[ClipRecord] | None = None

    def __post_init__(self):
        if self.clips is None:
            self.clips = [self.first, self.second]
        elif len(self.clips) >= 2:
            self.first = self.clips[0]
            self.second = self.clips[1]

    @property
    def summary(self) -> str:
        overlap_m = int(self.overlap_seconds // 60)
        overlap_s = int(self.overlap_seconds % 60)
        saved_mb = self.estimated_saved_bytes / (1024 * 1024)
        conf_str = " [Low confidence timestamp]" if self.confidence != "HIGH" else ""
        chain_str = f" [Chain of {len(self.clips)} clips]" if self.is_chained and len(self.clips) > 2 else (" [Chained overlap]" if self.is_chained else "")
        lines = [
            f"Overlap: {overlap_m}m {overlap_s}s ({self.overlap_seconds:.1f}s){conf_str}{chain_str} — "
            f"Est. saved space: {saved_mb:.1f} MB"
        ]
        for idx, clip in enumerate(self.clips or [self.first, self.second], start=1):
            lines.append(f"  {idx}) {clip.path.name} ({clip.duration:.1f}s)")
        return "\n".join(lines)


# Backwards compatibility alias for grouped overlaps
OverlapGroup = OverlapPair


def are_clips_stream_copy_compatible(r1: ClipRecord, r2: ClipRecord) -> bool:
    """Check if two clips can safely be concatenated using FFmpeg stream copy (-c copy).

    Clips with differing resolutions cannot be concatenated losslessly via stream copy.
    """
    if r1.path.parent != r2.path.parent:
        return False
    if r1.width > 0 and r2.width > 0 and (r1.width, r1.height) != (r2.width, r2.height):
        return False
    return True


def get_safe_output_path(target_path: Path, max_attempts: int = 9999) -> Path:
    """
    Generate a unique file path to prevent silent overwrites.

    Appends a counter to the filename if the target path already exists,
    stripping any existing suffix to prevent runaway accumulation.

    Args:
        target_path (Path): The intended output file path.
        max_attempts (int): Maximum number of suffixes to try before raising.

    Returns:
        Path: A guaranteed unique file path.

    Raises:
        RuntimeError: If no unique path could be found within max_attempts.
    """
    if not target_path.exists():
        return target_path

    # Clean off any existing _merged_# suffix to prevent _merged_1_merged_2
    base_stem = re.sub(r'_merged_\d+$', '', target_path.stem)

    for counter in range(1, max_attempts + 1):
        new_path = target_path.with_name(f"{base_stem}_merged_{counter}{target_path.suffix}")
        if not new_path.exists():
            return new_path
    raise RuntimeError(
        f"Could not find a unique output path after {max_attempts} attempts: {target_path}"
    )


# Backwards compatibility alias for existing code
resolve_unique_output_path = get_safe_output_path


def finalize_output(temp_out: Path, desired_out: Path, allow_overwrite: bool) -> Path:
    """Atomically move temporary output to final path with fresh collision check right before move."""
    final_path = desired_out if allow_overwrite else get_safe_output_path(desired_out)
    if final_path.exists() and not allow_overwrite:
        final_path = get_safe_output_path(final_path)
    if final_path.exists() and allow_overwrite:
        final_path.unlink()
    shutil.move(str(temp_out), str(final_path))
    return final_path


def quarantine_original_clips(file_paths: list[Path]) -> list[Path]:
    """
    Move original clips to the OS trash/recycle bin instead of permanent deletion.

    Args:
        file_paths (list[Path]): A list of paths to the original video clips.

    Returns:
        list[Path]: List of original file paths that were successfully moved to trash.
    """
    quarantined: list[Path] = []
    for file_path in file_paths:
        if not file_path.exists():
            continue
        if _is_file_locked(file_path):
            # File is still held open by the capture engine or another process.
            # Skip rather than risk moving a partially-written clip to the recycle bin.
            print(f"[Deduplication] Skipping locked file (still open by another process): {file_path.name}")
            continue
        # Safely move the video file and its metadata sidecars to the recycle bin/trash
        try:
            send2trash(file_path)
            quarantined.append(file_path)
        except Exception as e:
            print(f"[Deduplication] Failed to send {file_path.name} to trash: {e}")
            continue

        # Safely move both the standard audio manifest and legacy sidecars
        for suffix in (MANIFEST_SUFFIX, ".fthr-manifest"):
            sidecar = file_path.with_name(file_path.name + suffix)
            if sidecar.exists():
                try:
                    send2trash(sidecar)
                except Exception:
                    # Non-fatal: sidecar cleanup is best-effort and does not impact clip deduplication
                    pass

    return quarantined


def quarantine_clips(
    paths: Sequence[Path],
    quarantine_root: Path | None = None,
) -> list[Path]:
    """Safely move original clips and their sidecars to a quarantine directory instead of deleting."""
    quarantined: list[Path] = []
    timestamp_folder = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    for p in paths:
        if not p.is_file():
            continue
        q_dir = (quarantine_root or p.parent) / ".fthr_quarantine" / timestamp_folder
        try:
            q_dir.mkdir(parents=True, exist_ok=True)
            dest = get_safe_output_path(q_dir / p.name)
            shutil.move(str(p), str(dest))
            quarantined.append(dest)

            for suffix in (MANIFEST_SUFFIX, ".fthr-manifest"):
                sidecar = p.with_name(p.name + suffix)
                if sidecar.is_file():
                    sc_dest = get_safe_output_path(q_dir / sidecar.name)
                    shutil.move(str(sidecar), str(sc_dest))
        except OSError as e:
            print(f"[Deduplication] Failed to quarantine {p.name}: {e}")

    return quarantined


def calculate_timestamp_confidence(file_path: Path, duration_sec: float) -> str:
    """
    Determine the confidence level of a clip's derived start time.

    Args:
        file_path (Path): Path to the video file.
        duration_sec (float): The duration of the video in seconds.

    Returns:
        str: 'HIGH' if the timestamp is reliable, 'LOW' otherwise.
    """
    try:
        stat_res = file_path.stat()
    except OSError:
        return "LOW"

    # ctime semantics differ by platform:
    #   Windows: ctime = file creation time (unchanged by mv within drive, updated by copy or across drives)
    #   Linux:   ctime = inode change time  (updated by both mv and copy)
    #
    # When falling back to filesystem dates (because the filename lacked a recognized timestamp),
    # a ctime significantly newer than mtime indicates the file was copied or restored, meaning
    # the start time cannot be guaranteed with high precision.
    if hasattr(stat_res, 'st_ctime') and stat_res.st_ctime > stat_res.st_mtime + 60:
        return "LOW"

    return "HIGH"


def calculate_actual_savings(original_paths: list[Path], merged_path: Path) -> int:
    """
    Calculate the exact disk space saved after a successful merge operation.

    Args:
        original_paths (list[Path]): Paths of the original overlapping clips.
        merged_path (Path): Path of the new merged clip.

    Returns:
        int: The number of bytes saved (can be negative if the new file is larger).
    """
    original_size = sum(p.stat().st_size for p in original_paths if p.exists())
    merged_size = merged_path.stat().st_size if merged_path.exists() else 0

    return original_size - merged_size


def cluster_overlapping_clips(
    clips: Sequence[ClipRecord | dict],
) -> list[list[ClipRecord | dict]]:
    """
    Group all overlapping clips into distinct clusters for batch processing.

    Accepts either Sequence[ClipRecord] or Sequence[dict].
    When given ClipRecords, clusters are grouped by directory and validated for
    stream-copy compatibility.

    Args:
        clips: List of clip records or dictionaries containing 'start_time' and 'end_time'.

    Returns:
        list[list[ClipRecord | dict]]: A list of clusters (each cluster is >= 2 overlapping clips).
    """
    if not clips:
        return []

    def _start(c: ClipRecord | dict) -> float:
        return c.start_time if isinstance(c, ClipRecord) else c['start_time']

    def _end(c: ClipRecord | dict) -> float:
        return c.end_time if isinstance(c, ClipRecord) else c['end_time']

    def _dir_key(c: ClipRecord | dict) -> Path | None:
        if isinstance(c, ClipRecord):
            return c.path.parent
        if isinstance(c, dict) and 'path' in c:
            p = c['path']
            return p.parent if isinstance(p, Path) else Path(p).parent
        return None

    # Group by parent directory to isolate games/folders
    by_dir: dict[Path | None, list[ClipRecord | dict]] = {}
    for c in clips:
        by_dir.setdefault(_dir_key(c), []).append(c)

    all_clusters: list[list[ClipRecord | dict]] = []

    for group in by_dir.values():
        sorted_group = sorted(group, key=_start)
        if not sorted_group:
            continue

        current_cluster: list[ClipRecord | dict] = [sorted_group[0]]
        current_end = _end(sorted_group[0])

        for c in sorted_group[1:]:
            compatible = True
            if isinstance(c, ClipRecord) and isinstance(current_cluster[0], ClipRecord):
                compatible = are_clips_stream_copy_compatible(current_cluster[0], c)

            if compatible and _start(c) <= current_end:
                current_cluster.append(c)
                current_end = max(current_end, _end(c))
            else:
                if len(current_cluster) > 1:
                    all_clusters.append(current_cluster)
                current_cluster = [c]
                current_end = _end(c)

        if len(current_cluster) > 1:
            all_clusters.append(current_cluster)

    return all_clusters


def restore_quarantined_clips(
    quarantined_paths: Sequence[Path],
    target_dir: Path,
) -> list[Path]:
    """Restore quarantined clips back to target directory."""
    restored: list[Path] = []
    for qp in quarantined_paths:
        if not qp.is_file():
            continue
        dest = resolve_unique_output_path(target_dir / qp.name)
        try:
            shutil.move(str(qp), str(dest))
            restored.append(dest)
            for suffix in (MANIFEST_SUFFIX, ".fthr-manifest"):
                sidecar = qp.with_name(qp.name + suffix)
                if sidecar.is_file():
                    sc_dest = resolve_unique_output_path(target_dir / sidecar.name)
                    shutil.move(str(sidecar), str(sc_dest))
        except OSError as e:
            print(f"[Deduplication] Failed to restore {qp.name}: {e}")
    return restored


def parse_clip_start_time(
    path: Path,
    duration: float = 0.0,
    *,
    with_confidence: bool = False,
) -> float | tuple[float, str]:
    """Parse start time from filename timestamp, or fall back to file mtime."""
    stem = path.stem
    for pat, fmt in _DATE_PATTERNS:
        match = pat.search(stem)
        if match:
            try:
                dt = datetime.datetime.strptime(match.group(1), fmt)
                t = dt.timestamp()
                return (t, 'HIGH') if with_confidence else t
            except ValueError:
                # Pattern match was not a valid calendar date; try next pattern
                pass

    # Fallback to filesystem mtime minus duration with timestamp confidence calculation
    try:
        mtime = path.stat().st_mtime
        t = max(0.0, mtime - duration)
        conf = calculate_timestamp_confidence(path, duration)
        return (t, conf) if with_confidence else t
    except OSError:
        return (0.0, 'LOW') if with_confidence else 0.0


def scan_clip_records(
    directory: str | Path,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> list[ClipRecord]:
    """Scan directory and parse metadata for completed video clips."""
    root = Path(directory)
    if not root.is_dir():
        return []

    video_files: list[Path] = []
    for file_path in iter_safe_tree(root, cancel_event=cancel_event):
        if cancel_event and cancel_event.is_set():
            return []
        if file_path.name.startswith('.'):
            continue
        # Skip exported clips (they are rendered productions, not raw replay buffers)
        if '_export_' in file_path.name.casefold() or file_path.parent.name.casefold() in ('exports', 'shares'):
            continue
        if is_completed_video_path(file_path):
            video_files.append(file_path)

    total = len(video_files)
    records: list[ClipRecord] = []

    for idx, vpath in enumerate(video_files):
        if cancel_event and cancel_event.is_set():
            return []
        if progress_cb:
            progress_cb(idx, total, vpath.name)

        try:
            size = vpath.stat().st_size
            meta = probe_video_metadata(vpath)
            duration = meta.duration_seconds if (meta and meta.duration_seconds) else 0.0
            if duration <= 0.0:
                continue
            start_t, conf = parse_clip_start_time(vpath, duration, with_confidence=True)
            w = meta.width or 0
            h = meta.height or 0
            fps = meta.average_fps or 0.0
            records.append(
                ClipRecord(
                    path=vpath,
                    start_time=start_t,
                    duration=duration,
                    end_time=start_t + duration,
                    size_bytes=size,
                    width=w,
                    height=h,
                    fps=fps,
                    timestamp_confidence=conf,
                )
            )
        except Exception as e:
            # Corrupted, unreadable or non-video file probe failures are skipped
            print(f"[Deduplicator] Skipping unreadable clip candidate {vpath.name}: {e}")
            continue

    records.sort(key=lambda r: r.start_time)
    return records


def _run_ffmpeg_tool(
    cmd: list[str],
    cancel_event: threading.Event | None = None,
    poll_interval: float = 0.05,
) -> None:
    """Run an FFmpeg command safely, draining stderr to avoid OS pipe deadlock."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        **_NO_WINDOW,
    )
    stderr_chunks: list[str] = []
    while True:
        if cancel_event and cancel_event.is_set():
            proc.kill()
            proc.wait()
            raise InterruptedError("Deduplication cancelled")
        try:
            _, chunk = proc.communicate(timeout=poll_interval)
            if chunk:
                stderr_chunks.append(chunk)
            break
        except subprocess.TimeoutExpired:
            # Subprocess is still running; loop continues to check cancel_event and drain stderr chunks
            continue

    if proc.returncode != 0:
        full_err = "".join(stderr_chunks)
        raise RuntimeError(f"FFmpeg command failed (exit code {proc.returncode}): {full_err}")


def find_overlapping_pairs(
    records: Sequence[ClipRecord],
    min_overlap_seconds: float = 3.0,
) -> list[OverlapPair]:
    """Find pairs of clips that overlap chronologically without duplicating clips across pairs."""
    sorted_records = sorted(records, key=lambda r: r.start_time)

    # 1. Build adjacency mapping to detect chained overlaps (A -> B -> C)
    overlaps_per_clip: dict[Path, list[int]] = {r.path: [] for r in sorted_records}
    for i in range(len(sorted_records)):
        r1 = sorted_records[i]
        for j in range(i + 1, len(sorted_records)):
            r2 = sorted_records[j]
            if not are_clips_stream_copy_compatible(r1, r2):
                continue
            if r2.start_time >= r1.end_time:
                break
            overlap = min(r1.end_time, r2.end_time) - r2.start_time
            if overlap >= min_overlap_seconds:
                overlaps_per_clip[r1.path].append(j)
                overlaps_per_clip[r2.path].append(i)

    # 2. Form pairs greedily and mark chained pairs
    pairs: list[OverlapPair] = []
    used_paths: set[Path] = set()

    for i in range(len(sorted_records)):
        r1 = sorted_records[i]
        if r1.path in used_paths:
            continue

        for j in range(i + 1, len(sorted_records)):
            r2 = sorted_records[j]
            if r2.path in used_paths:
                continue

            if not are_clips_stream_copy_compatible(r1, r2):
                continue

            # If r2 starts after r1 ends, subsequent records also start after r1
            if r2.start_time >= r1.end_time:
                break

            # Calculate overlap duration
            overlap = min(r1.end_time, r2.end_time) - r2.start_time
            if overlap >= min_overlap_seconds:
                # Estimate duplicate byte savings
                ratio = overlap / r2.duration if r2.duration > 0 else 0.0
                saved_bytes = int(r2.size_bytes * min(1.0, ratio))
                confidence = 'HIGH' if (r1.timestamp_confidence == 'HIGH' and r2.timestamp_confidence == 'HIGH') else 'LOW'
                is_chained = len(overlaps_per_clip[r1.path]) > 1 or len(overlaps_per_clip[r2.path]) > 1

                pairs.append(
                    OverlapPair(
                        first=r1,
                        second=r2,
                        overlap_seconds=overlap,
                        estimated_saved_bytes=saved_bytes,
                        confidence=confidence,
                        is_chained=is_chained,
                        clips=[r1, r2],
                    )
                )
                used_paths.add(r1.path)
                used_paths.add(r2.path)
                break

    return pairs


def find_overlapping_clusters(
    records: Sequence[ClipRecord],
    min_overlap_seconds: float = 3.0,
) -> list[OverlapPair]:
    """Find all overlapping clip clusters (including multi-clip chains A->B->C).

    Returns a list of OverlapPair objects (each representing either a 2-clip pair
    or a multi-clip cluster in its `.clips` attribute).
    """
    raw_clusters = cluster_overlapping_clips(records)
    results: list[OverlapPair] = []

    for cluster in raw_clusters:
        # Guarantee cluster contains only ClipRecords
        clip_records = [c for c in cluster if isinstance(c, ClipRecord)]
        if len(clip_records) < 2:
            continue

        clip_records.sort(key=lambda r: r.start_time)
        first_clip = clip_records[0]
        second_clip = clip_records[1]

        # Calculate total overlap duration across the cluster
        total_overlap = 0.0
        cur_end = first_clip.end_time
        total_saved_est = 0

        for c in clip_records[1:]:
            if c.start_time < cur_end:
                overlap = min(cur_end, c.end_time) - c.start_time
                if overlap >= min_overlap_seconds:
                    total_overlap += overlap
                    ratio = overlap / c.duration if c.duration > 0 else 0.0
                    total_saved_est += int(c.size_bytes * min(1.0, ratio))
                cur_end = max(cur_end, c.end_time)
            else:
                cur_end = c.end_time

        if total_overlap < min_overlap_seconds:
            continue

        conf = 'LOW' if any(c.timestamp_confidence == 'LOW' for c in clip_records) else 'HIGH'
        is_chained = len(clip_records) > 2

        results.append(
            OverlapPair(
                first=first_clip,
                second=second_clip,
                overlap_seconds=total_overlap,
                estimated_saved_bytes=total_saved_est,
                confidence=conf,
                is_chained=is_chained,
                clips=clip_records,
            )
        )

    return results


def merge_overlapping_pair(
    pair: OverlapPair,
    output_path: Path | None = None,
    remove_originals: bool = False,
    allow_overwrite: bool = False,
    quarantine: bool = True,
    cancel_event: threading.Event | None = None,
) -> Path:
    """Merge two overlapping clips into one contiguous clip using fast stream copy.

    Preserves all audio and video tracks losslessly (-map 0 -c copy), requiring minimal
    RAM (< 30 MB) and finishing in only a few seconds per clip. Trimming occurs at the
    nearest keyframe (I-frame), so a 1-2 second jump may appear at the stitch boundary.
    """
    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Deduplication cancelled")

    ffmpeg = get_ffmpeg_exe()
    out_dir = pair.first.path.parent
    if output_path is None:
        stem = f"{pair.first.path.stem}_merged_{pair.second.path.stem[-8:]}"
        candidate = out_dir / f"{stem}.mp4"
        output_path = candidate if allow_overwrite else get_safe_output_path(candidate)
    elif not allow_overwrite and output_path.exists():
        output_path = get_safe_output_path(output_path)

    cut_offset = max(0.0, pair.overlap_seconds)

    try:
        td_dir = out_dir if out_dir.is_dir() else None
    except Exception:
        td_dir = None

    with tempfile.TemporaryDirectory(prefix="fthr_dedup_", dir=td_dir, ignore_cleanup_errors=True) as td:
        temp_out = Path(td) / "merged_out.mp4"

        # Edge case: If clip 2 is completely subsumed within clip 1
        # (overlap covers clip 2's duration), clip 1 already contains all recorded content.
        if cut_offset >= (pair.second.duration - 0.5):
            copy_cmd = [
                ffmpeg, "-y", "-v", "error", "-nostdin",
                "-i", str(pair.first.path),
                "-map", "0",
                "-c", "copy",
                str(temp_out),
            ]
            _run_ffmpeg_tool(copy_cmd, cancel_event=cancel_event)
        else:
            # 1. Trim second clip using stream copy, preserving all audio/video tracks
            # and resetting timestamps to 0 to prevent concat demuxer sync issues.
            trimmed_second = Path(td) / "trimmed_second.mp4"
            trim_cmd = [
                ffmpeg, "-y", "-v", "error", "-nostdin",
                "-ss", f"{cut_offset:.3f}",
                "-i", str(pair.second.path),
                "-map", "0",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                str(trimmed_second),
            ]
            _run_ffmpeg_tool(trim_cmd, cancel_event=cancel_event)

            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Deduplication cancelled")

            # 2. Write concat list using forward slashes (.as_posix()) to prevent Windows backslash escaping errors
            concat_list = Path(td) / "concat_list.txt"
            p1_str = pair.first.path.resolve().as_posix().replace("'", "'\\''")
            p2_str = trimmed_second.resolve().as_posix().replace("'", "'\\''")
            concat_list.write_text(
                f"file '{p1_str}'\nfile '{p2_str}'\n",
                encoding="utf-8",
            )

            # 3. Concatenate using stream copy, preserving all audio and video tracks
            concat_cmd = [
                ffmpeg, "-y", "-v", "error", "-nostdin",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-map", "0",
                "-c", "copy",
                str(temp_out),
            ]
            _run_ffmpeg_tool(concat_cmd, cancel_event=cancel_event)

        # Move to destination atomically with fresh collision check
        output_path = finalize_output(temp_out, output_path, allow_overwrite)

    if remove_originals:
        orig_paths = [pair.first.path, pair.second.path]
        orig_sizes = {p: p.stat().st_size for p in orig_paths if p.exists()}
        if quarantine:
            quarantined = quarantine_original_clips(orig_paths)
        else:
            quarantined = []
            for p in orig_paths:
                try:
                    p.unlink(missing_ok=True)
                    quarantined.append(p)
                    for suffix in (MANIFEST_SUFFIX, ".fthr-manifest"):
                        sidecar = p.with_name(p.name + suffix)
                        sidecar.unlink(missing_ok=True)
                except OSError:
                    # Non-fatal: original files could not be unlinked (e.g. file lock); merged file remains safe
                    pass

        merged_size = output_path.stat().st_size if output_path.exists() else 0
        freed = sum(orig_sizes.get(p, 0) for p in quarantined)
        pair.actual_saved_bytes = max(0, freed - merged_size)
    else:
        pair.actual_saved_bytes = 0

    return output_path


def merge_clip_cluster(
    cluster: Sequence[ClipRecord],
    output_path: Path | None = None,
    remove_originals: bool = False,
    allow_overwrite: bool = False,
    cancel_event: threading.Event | None = None,
) -> tuple[Path, int]:
    """
    Merge a cluster of overlapping clips (>= 2 clips) into one continuous clip in a single FFmpeg pass.

    Assumes cluster clips are sorted chronologically by start time.

    Args:
        cluster: List of ClipRecords in the cluster.
        output_path: Destination path or None to auto-generate.
        remove_originals: Whether to move original clips to OS trash/quarantine.
        allow_overwrite: Whether existing destination can be replaced.
        cancel_event: Cancellation signal.

    Returns:
        Tuple of (path to merged output clip, bytes_saved: int).
        bytes_saved is 0 when remove_originals is False.
    """
    if not cluster:
        raise ValueError("Cannot merge an empty cluster")
    if len(cluster) == 1:
        return cluster[0].path, 0

    if cancel_event and cancel_event.is_set():
        raise InterruptedError("Deduplication cancelled")

    ffmpeg = get_ffmpeg_exe()
    out_dir = cluster[0].path.parent
    if output_path is None:
        first_stem = cluster[0].path.stem
        last_suffix = cluster[-1].path.stem[-8:]
        candidate = out_dir / f"{first_stem}_cluster_{last_suffix}.mp4"
        output_path = candidate if allow_overwrite else get_safe_output_path(candidate)
    elif not allow_overwrite and output_path.exists():
        output_path = get_safe_output_path(output_path)

    try:
        td_dir = out_dir if out_dir.is_dir() else None
    except Exception:
        td_dir = None

    with tempfile.TemporaryDirectory(prefix="fthr_cluster_", dir=td_dir, ignore_cleanup_errors=True) as td:
        segments_to_concat: list[Path] = [cluster[0].path]
        timeline_end = cluster[0].end_time

        for idx, clip in enumerate(cluster[1:], start=1):
            if cancel_event and cancel_event.is_set():
                raise InterruptedError("Deduplication cancelled")

            # If this clip ends before or at current timeline end, it is completely redundant
            if clip.end_time <= timeline_end + 0.1:
                continue

            if clip.start_time < timeline_end:
                cut_offset = timeline_end - clip.start_time
                trimmed_seg = Path(td) / f"trimmed_seg_{idx}.mp4"
                trim_cmd = [
                    ffmpeg, "-y", "-v", "error", "-nostdin",
                    "-ss", f"{cut_offset:.3f}",
                    "-i", str(clip.path),
                    "-map", "0",
                    "-c", "copy",
                    "-avoid_negative_ts", "make_zero",
                    str(trimmed_seg),
                ]
                _run_ffmpeg_tool(trim_cmd, cancel_event=cancel_event)
                segments_to_concat.append(trimmed_seg)
                timeline_end = clip.end_time
            else:
                # No overlap with previous clip, take entirely
                segments_to_concat.append(clip.path)
                timeline_end = clip.end_time

        temp_out = Path(td) / "cluster_merged_out.mp4"
        if len(segments_to_concat) == 1:
            shutil.copyfile(str(segments_to_concat[0]), str(temp_out))
        else:
            concat_list = Path(td) / "concat_list.txt"
            lines = []
            for p in segments_to_concat:
                escaped = p.resolve().as_posix().replace("'", "'\\''")
                lines.append(f"file '{escaped}'")
            concat_list.write_text("\n".join(lines) + "\n", encoding="utf-8")

            concat_cmd = [
                ffmpeg, "-y", "-v", "error", "-nostdin",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_list),
                "-map", "0",
                "-c", "copy",
                str(temp_out),
            ]
            _run_ffmpeg_tool(concat_cmd, cancel_event=cancel_event)

        # Move to destination atomically with fresh collision check
        output_path = finalize_output(temp_out, output_path, allow_overwrite)

    if remove_originals:
        orig_paths = [c.path for c in cluster]
        orig_sizes = {p: p.stat().st_size for p in orig_paths if p.exists()}
        quarantined = quarantine_original_clips(orig_paths)
        merged_size = output_path.stat().st_size if output_path.exists() else 0
        freed = sum(orig_sizes.get(p, 0) for p in quarantined)
        actual_saved = max(0, freed - merged_size)
        return output_path, actual_saved
    return output_path, 0
