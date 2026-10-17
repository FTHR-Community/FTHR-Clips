"""Disposable seek-friendly previews for old media with long GOPs.

Original media, editor metadata and exports always keep their original path.
Failures simply retain original playback; only complete previews enter cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import threading
from collections import OrderedDict

from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe, software_video_args
from core.media_process import run_media_process
from core.media_identity import file_identity

CACHE_DIR = Path.home() / '.fthr' / 'playback'
_PROBE_DECISIONS: OrderedDict[Path, bool] = OrderedDict()
_PROBE_LOCK = threading.Lock()


def _remember_probe(target: Path, needed: bool) -> None:
    with _PROBE_LOCK:
        _PROBE_DECISIONS[target] = needed
        _PROBE_DECISIONS.move_to_end(target)
        while len(_PROBE_DECISIONS) > 128:
            _PROBE_DECISIONS.popitem(last=False)


def _has_keyframe_evidence(packets) -> bool:
    for packet in packets:
        try:
            if ('K' in packet.get('flags', '')
                    and math.isfinite(float(packet['pts_time']))):
                return True
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return False


def _cache_path(source: str) -> Path:
    identity = f'{file_identity(source)!r}|gop30-v2'
    return CACHE_DIR / (hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.mp4')


def cached_playback_path(source: str) -> str:
    try:
        path = _cache_path(source)
        if path.is_file() and path.stat().st_size > 0:
            return str(path)
    except OSError:
        # Missing or unreadable cache data is a miss; the original remains playable.
        pass
    return source


def discard_playback_cache(source: str, cached: str) -> None:
    try:
        expected = _cache_path(source)
        if Path(cached) == expected:
            expected.unlink(missing_ok=True)
    except OSError:
        # A locked/missing disposable preview cannot prevent original playback.
        pass


def needs_seek_proxy(packets: list[dict]) -> bool:
    """Require measured >2-second keyframe gaps; never guess from probe failure."""
    last_keyframe = None
    for packet in packets:
        try:
            pts = float(packet['pts_time'])
        except (KeyError, TypeError, ValueError):
            # Malformed packet metadata is not evidence of a long keyframe gap.
            continue
        if not math.isfinite(pts):
            continue
        if last_keyframe is not None and pts - last_keyframe > 2.1:
            return True
        if 'K' in packet.get('flags', ''):
            last_keyframe = pts
    return False


def prepare_playback_path(source: str, cancelled: threading.Event) -> str:
    """Probe at most eight seconds, then build one bounded optional preview."""
    temporary = None
    try:
        cached = cached_playback_path(source)
        if cached != source or cancelled.is_set():
            return cached
        target = _cache_path(source)
        with _PROBE_LOCK:
            decision = _PROBE_DECISIONS.get(target)
            if decision is not None:
                _PROBE_DECISIONS.move_to_end(target)
        if decision is False:
            return source
        probe = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-select_streams', 'v:0',
             '-read_intervals', '0%+8', '-show_entries',
             'packet=pts_time,flags:format=duration:stream=color_transfer',
             '-of', 'json', source], cancelled, timeout_seconds=4)
        if probe is None or probe[0] != 0:
            return source
        document = json.loads(probe[1])
        if any(stream.get('color_transfer') in {'smpte2084', 'arib-std-b67'}
               for stream in document.get('streams', [])):
            if not cancelled.is_set() and _cache_path(source) == target:
                _remember_probe(target, False)
            return source  # Keep HDR on its original, color-managed decoder path.
        duration = float(document.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or not 0 < duration <= 300:
            if (math.isfinite(duration) and duration > 300
                    and not cancelled.is_set() and _cache_path(source) == target):
                _remember_probe(target, False)
            return source
        packets = document.get('packets', [])
        needed = needs_seek_proxy(packets)
        # Empty/malformed packet responses are not successful eligibility evidence.
        if (not needed and _has_keyframe_evidence(packets)
                and not cancelled.is_set() and _cache_path(source) == target):
            _remember_probe(target, False)
        if not needed:
            return source
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f'.{os.getpid()}-{threading.get_ident()}.partial.mp4')
        ffmpeg = get_ffmpeg_exe()
        result = run_media_process(
            [ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '2',
             '-filter_threads', '1', '-i', source, '-map', '0:v:0', '-map', '0:a?',
             *software_video_args(16000, ffmpeg), '-threads:v', '2',
             '-c:a', 'copy', '-movflags', '+faststart', str(temporary)],
            cancelled, timeout_seconds=120, background='proxy')
        if (result is None or result[0] != 0 or cancelled.is_set()
                or _cache_path(source) != target
                or temporary.stat().st_size > 2 * 1024**3):
            return source
        checked = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'json', str(temporary)], cancelled, timeout_seconds=4)
        if checked is None or checked[0] != 0 or cancelled.is_set():
            return source
        preview_duration = float(json.loads(checked[1])['format']['duration'])
        if not math.isfinite(preview_duration) or abs(preview_duration - duration) > 0.2:
            return source
        os.replace(temporary, target)
        # Cache data is disposable. Keep at most eight previews / 2 GiB,
        # excluding the one about to be opened. Locked Windows files stay put.
        entries = sorted(CACHE_DIR.glob('*.mp4'), key=lambda p: p.stat().st_mtime,
                         reverse=True)
        total = target.stat().st_size
        kept = 1
        for entry in entries:
            if entry == target or '.partial.' in entry.name:
                continue
            total += entry.stat().st_size
            kept += 1
            if kept > 8 or total > 2 * 1024**3:
                try:
                    entry.unlink()
                except OSError:
                    # Another open editor can hold a Windows cache file; eviction is best effort.
                    pass
        return str(target)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        # Preview optimization is optional and must not block source playback.
        return source
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cancellation cleanup must not replace a successfully returned source path.
                pass
