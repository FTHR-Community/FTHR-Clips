"""Recover complete fragments from a closed, failed manual recording."""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.capture_highlights import CaptureHighlightError, media_identity
from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe, get_ffprobe_exe
from core.file_recovery import next_available_output_path, retry_file_operation
from core.media_process import run_media_process


_RECOVERY_TIMEOUT_SECONDS = 90.0
_VALIDATION_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class RecoveredRecording:
    """A playable copy of recovered fragments with its measured video duration."""

    path: Path
    duration_seconds: float


def _complete_fragment_end(source: Path, size: int, cancel: threading.Event,
                           deadline: float) -> int:
    """Find the last complete native moof/mdat pair without reading media data."""
    offset = 0
    complete_end = 0
    have_header = False
    pending_fragment = False
    with source.open('rb') as handle:
        while offset + 8 <= size:
            if cancel.is_set() or time.monotonic() >= deadline:
                return 0
            handle.seek(offset)
            header = handle.read(8)
            if len(header) != 8:
                break
            box_size, box_type = struct.unpack('>I4s', header)
            header_size = 8
            if box_size == 1:
                extended = handle.read(8)
                if len(extended) != 8:
                    break
                box_size, = struct.unpack('>Q', extended)
                header_size = 16
            # An unfinished box, including a size-zero mdat, has no proven end.
            if box_size < header_size or box_size > size - offset:
                break
            offset += box_size
            if box_type == b'moov':
                have_header = True
            elif box_type == b'moof':
                if not have_header or pending_fragment:
                    break
                pending_fragment = True
            elif box_type == b'mdat':
                if not pending_fragment or box_size == header_size:
                    break
                complete_end = offset
                pending_fragment = False
    return complete_end


def _recovered_video_duration(staged: Path, ffprobe: str, cancel: threading.Event,
                              deadline: float) -> float | None:
    """Check for decodable video within the first bounded packet sample."""
    remaining = min(_VALIDATION_TIMEOUT_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        return None
    result = run_media_process(
        [ffprobe, '-v', 'error', '-protocol_whitelist', 'file', '-select_streams', 'v:0',
         '-read_intervals', '%+#120', '-show_frames', '-show_entries',
         'frame=key_frame:stream=duration', '-of', 'json', str(staged)],
        cancel, timeout_seconds=remaining,
    )
    if result is None or result[0] != 0 or result[2]:
        return None
    try:
        payload = json.loads(result[1])
        duration = float(payload['streams'][0]['duration'])
        first_frame = payload['frames'][0]
        if first_frame['key_frame'] == 1 and math.isfinite(duration) and duration > 0:
            return duration
    except (ValueError, TypeError, KeyError, IndexError):
        # A header alone or a nondecodable video sample is insufficient.
        pass
    return None


def _publish_recovery(staged: Path, source: Path) -> Path:
    for _ in range(4):
        destination = next_available_output_path(
            source.parent, source.stem, '.mp4', stem_suffix=' (recovered)',
        )
        try:
            # Windows rename refuses replacement. POSIX link has the same
            # no-replace property and publishes the complete file atomically.
            if os.name == 'nt':
                retry_file_operation(staged.rename, destination)
            else:
                retry_file_operation(os.link, staged, destination)
            return destination
        except FileExistsError:
            continue
    raise FileExistsError('No unused recovered recording name is available.')


def recover_manual_recording(
        recording: str | Path, *, writer_closed: bool,
        expected_identity: Mapping[str, int] | None = None,
        cancel_event: threading.Event | None = None) -> RecoveredRecording | None:
    """Attempt bounded recovery after the caller proves the native writer closed.

    The source and its sidecars remain untouched. The returned file represents
    only the committed prefix, and should be presented as a partial recovery.
    Pass the original file identity when scheduling a deferred attempt.
    Run this off the UI thread. A failed or cancelled attempt publishes no file.
    """
    if not writer_closed:
        return None
    cancel = cancel_event if cancel_event is not None else threading.Event()
    if cancel.is_set():
        return None
    deadline = time.monotonic() + _RECOVERY_TIMEOUT_SECONDS
    source = Path(recording).absolute()
    staged = None
    try:
        # On Windows this also rejects an open writer and a reparse-point file.
        identity = media_identity(source)
        if expected_identity is not None and identity != expected_identity:
            return None
        end = _complete_fragment_end(source, identity['size'], cancel, deadline)
        if not end or media_identity(source) != identity:
            return None
        ffmpeg, ffprobe = get_ffmpeg_exe(), get_ffprobe_exe()
        with tempfile.NamedTemporaryFile(prefix='.fr-', suffix='.tmp',
                                         dir=source.parent, delete=False) as temporary:
            staged = Path(temporary.name)

        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancel.is_set():
            return None
        # Give MOV a seekable view ending at the last complete mdat. The
        # input is opened read-only and only local-file protocols are enabled.
        fragment_input = f'subfile,,start,0,end,{end},,:file:{source}'
        remuxed = run_media_process(
            [ffmpeg, '-v', 'error', '-nostdin', '-xerror', '-y',
             '-protocol_whitelist', 'subfile,file', '-i', fragment_input,
             '-map', '0:v:0', '-map', '0:a?', '-c', 'copy',
             '-movflags', 'use_metadata_tags', '-f', 'mp4', str(staged)],
            cancel, timeout_seconds=remaining,
        )
        if remuxed is None or remuxed[0] != 0 or remuxed[2] or staged.stat().st_size == 0:
            return None
        duration = _recovered_video_duration(staged, ffprobe, cancel, deadline)
        if duration is None or cancel.is_set() or media_identity(source) != identity:
            return None
        return RecoveredRecording(_publish_recovery(staged, source), duration)
    except (OSError, ValueError, CaptureHighlightError, FFmpegUnavailable,
            subprocess.SubprocessError):
        return None
    finally:
        if staged is not None:
            try:
                retry_file_operation(staged.unlink, missing_ok=True)
            except OSError:
                # The staging name is never interpreted as a finished recording.
                pass
