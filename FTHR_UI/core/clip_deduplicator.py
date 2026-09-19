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
import time
from typing import Callable, Sequence

from core.clip_files import is_completed_video_path, iter_safe_tree
from core.ffmpeg_tools import get_ffmpeg_exe
from core.media_metadata import probe_video_metadata

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
    timestamp_confidence: str = 'exact'  # 'exact' or 'inferred'

    @property
    def formatted_start(self) -> str:
        return datetime.datetime.fromtimestamp(self.start_time).strftime('%Y-%m-%d %H:%M:%S')


@dataclass
class OverlapPair:
    first: ClipRecord
    second: ClipRecord
    overlap_seconds: float
    estimated_saved_bytes: int
    confidence: str = 'exact'      # 'exact' if both exact, else 'inferred'
    is_chained: bool = False       # True if part of an A->B->C chain
    actual_saved_bytes: int = 0    # Populated after merge

    @property
    def summary(self) -> str:
        overlap_m = int(self.overlap_seconds // 60)
        overlap_s = int(self.overlap_seconds % 60)
        saved_mb = self.estimated_saved_bytes / (1024 * 1024)
        conf_str = " [Inferred timestamp]" if self.confidence != "exact" else ""
        chain_str = " [Chained overlap]" if self.is_chained else ""
        return (
            f"Overlap: {overlap_m}m {overlap_s}s ({self.overlap_seconds:.1f}s){conf_str}{chain_str} — "
            f"Est. saved space: {saved_mb:.1f} MB\n"
            f"  1) {self.first.path.name} ({self.first.duration:.1f}s)\n"
            f"  2) {self.second.path.name} ({self.second.duration:.1f}s)"
        )


def resolve_unique_output_path(target_path: Path) -> Path:
    """Generate a unique path if target_path already exists (e.g. name (1).mp4)."""
    if not target_path.exists():
        return target_path

    directory = target_path.parent
    stem = target_path.stem
    suffix = target_path.suffix

    counter = 1
    while True:
        candidate = directory / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


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
            dest = q_dir / p.name
            if dest.exists():
                dest = resolve_unique_output_path(dest)
            shutil.move(str(p), str(dest))
            quarantined.append(dest)

            sidecar = p.with_name(p.name + ".fthr-manifest")
            if sidecar.is_file():
                sc_dest = q_dir / sidecar.name
                shutil.move(str(sidecar), str(sc_dest))
        except OSError as e:
            print(f"[Deduplication] Failed to quarantine {p.name}: {e}")

    return quarantined


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
            sidecar = qp.with_name(qp.name + ".fthr-manifest")
            if sidecar.is_file():
                shutil.move(str(sidecar), str(target_dir / sidecar.name))
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
                return (t, 'exact') if with_confidence else t
            except ValueError:
                # Pattern match was not a valid calendar date; try next pattern
                pass

    # Fallback to filesystem mtime minus duration
    try:
        mtime = path.stat().st_mtime
        t = max(0.0, mtime - duration)
        return (t, 'inferred') if with_confidence else t
    except OSError:
        return (0.0, 'inferred') if with_confidence else 0.0


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
            if r1.path.parent != r2.path.parent:
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

            # Only pair clips that reside in the same game/desktop folder
            if r1.path.parent != r2.path.parent:
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
                confidence = 'exact' if (r1.timestamp_confidence == 'exact' and r2.timestamp_confidence == 'exact') else 'inferred'
                is_chained = len(overlaps_per_clip[r1.path]) > 1 or len(overlaps_per_clip[r2.path]) > 1

                pairs.append(
                    OverlapPair(
                        first=r1,
                        second=r2,
                        overlap_seconds=overlap,
                        estimated_saved_bytes=saved_bytes,
                        confidence=confidence,
                        is_chained=is_chained,
                    )
                )
                used_paths.add(r1.path)
                used_paths.add(r2.path)
                break

    return pairs


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
        output_path = candidate if allow_overwrite else resolve_unique_output_path(candidate)
    elif not allow_overwrite and output_path.exists():
        output_path = resolve_unique_output_path(output_path)

    cut_offset = max(0.0, pair.overlap_seconds)

    try:
        td_dir = out_dir if out_dir.is_dir() else None
    except Exception:
        td_dir = None

    with tempfile.TemporaryDirectory(prefix="fthr_dedup_", dir=td_dir) as td:
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
            process = subprocess.Popen(copy_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **_NO_WINDOW)
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    raise InterruptedError("Deduplication cancelled")
                time.sleep(0.05)
            if process.returncode != 0:
                _, stderr = process.communicate()
                raise RuntimeError(f"FFmpeg copy failed: {stderr}")
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
            process = subprocess.Popen(trim_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **_NO_WINDOW)
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    raise InterruptedError("Deduplication cancelled")
                time.sleep(0.05)
            if process.returncode != 0:
                _, stderr = process.communicate()
                raise RuntimeError(f"FFmpeg trim failed: {stderr}")

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
            process = subprocess.Popen(concat_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **_NO_WINDOW)
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.kill()
                    raise InterruptedError("Deduplication cancelled")
                time.sleep(0.05)
            if process.returncode != 0:
                _, stderr = process.communicate()
                raise RuntimeError(f"FFmpeg concat failed: {stderr}")

        # Move to destination atomically
        if output_path.exists() and allow_overwrite:
            output_path.unlink()
        shutil.move(str(temp_out), str(output_path))

    initial_bytes = pair.first.size_bytes + pair.second.size_bytes
    if remove_originals:
        if quarantine:
            quarantine_clips([pair.first.path, pair.second.path])
        else:
            for p in (pair.first.path, pair.second.path):
                try:
                    p.unlink(missing_ok=True)
                    sidecar = p.with_name(p.name + ".fthr-manifest")
                    if sidecar.is_file():
                        sidecar.unlink(missing_ok=True)
                except OSError:
                    # Non-fatal: original files could not be unlinked (e.g. file lock); merged file remains safe
                    pass
        merged_size = output_path.stat().st_size if output_path.exists() else 0
        pair.actual_saved_bytes = max(0, initial_bytes - merged_size)
    else:
        pair.actual_saved_bytes = 0

    return output_path
