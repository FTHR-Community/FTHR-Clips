"""Disposable, low-resolution previews for editing demanding media.

Original media, editor metadata and exports always keep their original path.
Failures simply retain original playback; only complete previews enter cache.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path
import sys
import threading
import time

from core.ffmpeg_tools import get_ffmpeg_exe, get_ffprobe_exe, software_video_args
from core.hdr_color import hdr_color_from_stream, probe_hdr, sdr_tonemap_filter
from core.media_process import run_media_process
from core.media_identity import file_identity

CACHE_DIR = Path.home() / '.fthr' / 'playback'
_MAX_CACHE_BYTES = 2 * 1024**3
_MAX_COMPATIBILITY_GROWTH = 16 * 1024 * 1024
# Limit the source timeline as well as the wall time and output size. The
# background lane allows one encode at a time and the editor can cancel it.
_MAX_PROXY_DURATION_SECONDS = 15 * 60
_MAX_PROXY_ENCODE_SECONDS = 120
_PROXY_PRESETS = {
    'auto': (1280, 720, 4500),
    '540p': (960, 540, 2500),
    '720p': (1280, 720, 4500),
}
_HDR_TRANSFERS = {'smpte2084', 'arib-std-b67'}
_HDR_PREVIEW_SUFFIX = '.sdr-720.mp4'
_MP4_EXTENSIONS = {'.mp4', '.m4v', '.mov'}
_MP4_PCM_TAGS = {'fpcm', 'ipcm'}
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
            # Malformed probe metadata is not evidence of a keyframe.
            continue
    return False


def _cache_path(source: str, mode: str = 'auto') -> Path:
    version = 'preview-v5-win' if sys.platform == 'win32' else 'preview-v5'
    identity = f'{file_identity(source)!r}|{version}|{mode}'
    return CACHE_DIR / (hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.mp4')


def _compatibility_cache_path(source: str) -> Path:
    identity = f'{file_identity(source)!r}|presentation-v4'
    return CACHE_DIR / 'compatibility' / (
        hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.mp4')


def is_compatibility_cache_path(source: str, candidate: str) -> bool:
    """Return whether *candidate* is this source's normalized cache copy."""

    try:
        return Path(candidate) == _compatibility_cache_path(source)
    except OSError:
        # A retired source cannot have a valid identity-bound cache path.
        return False


def is_sdr_preview_path(source: str, candidate: str) -> bool:
    """Return whether the playback copy was tone mapped from HDR."""
    try:
        expected = _compatibility_cache_path(source).with_suffix(_HDR_PREVIEW_SUFFIX)
        return Path(candidate) == expected
    except OSError:
        # Without a source identity, the candidate cannot be confirmed as its SDR copy.
        return False


def _cache_entries() -> list[Path]:
    entries = []
    for directory in (CACHE_DIR, CACHE_DIR / 'compatibility'):
        try:
            entries.extend(directory.glob('*.mp4'))
        except OSError:
            # A missing cache directory is an ordinary empty-cache state.
            continue
    return entries


def _prune_cache(keep: Path) -> None:
    """Bound full proxies and compatibility copies as one disposable cache."""
    try:
        entries = sorted(
            (entry for entry in _cache_entries()
             if '.partial.' not in entry.name),
            key=lambda entry: entry.stat().st_mtime,
            reverse=True)
        total = keep.stat().st_size
    except OSError:
        # A cache entry can disappear while another process evicts it.
        return
    kept = 1
    for entry in entries:
        if entry == keep:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            # Another process may have evicted this entry since the cache was listed.
            continue
        total += size
        kept += 1
        if kept > 8 or total > _MAX_CACHE_BYTES:
            try:
                entry.unlink()
            except OSError:
                # Another open editor can hold a disposable Windows cache file.
                pass


def cached_playback_path(source: str, mode: str = 'auto') -> str:
    """Return the selected cached preview, with required compatibility as fallback."""
    try:
        compatibility = _compatibility_cache_path(source)
        candidates = [compatibility.with_suffix(_HDR_PREVIEW_SUFFIX)]
        if mode in _PROXY_PRESETS:
            candidates.append(_cache_path(source, mode))
        candidates.append(compatibility)
        for path in candidates:
            if path.is_file() and path.stat().st_size > 0:
                return str(path)
    except OSError:
        # Missing or unreadable cache data is a miss; the original remains playable.
        pass
    return source


def discard_playback_cache(source: str, cached: str) -> None:
    try:
        cached_path = Path(cached)
        compatibility = _compatibility_cache_path(source)
        for expected in (compatibility.with_suffix(_HDR_PREVIEW_SUFFIX), compatibility,
                         *(_cache_path(source, mode) for mode in _PROXY_PRESETS)):
            if cached_path == expected:
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
            flags = packet['flags']
        except (KeyError, TypeError, ValueError):
            # Malformed packet metadata is not evidence of a long keyframe gap.
            continue
        if not math.isfinite(pts) or not isinstance(flags, str):
            continue
        if last_keyframe is not None and pts - last_keyframe > 2.1:
            return True
        if 'K' in flags:
            last_keyframe = pts
    return False


def _dimensions(video: dict) -> tuple[int, int]:
    try:
        width, height = (int(video[key]) for key in ('width', 'height'))
        if min(width, height) >= 2:
            return width, height
    except (KeyError, TypeError, ValueError, OverflowError):
        # Missing or malformed dimensions cannot justify an optional proxy.
        pass
    return 0, 0


def _demanding_video(video: dict, width: int, height: int) -> bool:
    if max(width, height) > 1920 or min(width, height) > 1080:
        return True
    try:
        fps = float(Fraction(video.get('avg_frame_rate', '0/1')))
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        # Invalid frame-rate metadata means keep the original playback path.
        return False
    # Denser codecs tend to require more work at the same pixel rate, in
    # particular when hardware decode is unavailable for the source profile.
    reference_fps = 25 if video.get('codec_name') in {'hevc', 'av1', 'vp9'} else 45
    return math.isfinite(fps) and width * height * fps > 1920 * 1080 * reference_fps


def _preview_scale(long_side: int, short_side: int) -> str:
    # Evaluate after FFmpeg applies display rotation. Retain portrait clips as
    # portrait and cap each dimension at its input size to avoid upscaling.
    return (
        f"scale=w='min(iw,if(gte(iw,ih),{long_side},{short_side}))':"
        f"h='min(ih,if(gte(iw,ih),{short_side},{long_side}))':"
        'force_original_aspect_ratio=decrease:force_divisible_by=2')


def _audio_streams(document):
    return [stream for stream in document['streams'] if stream.get('codec_type') == 'audio']


def _stream_timing_matches(source, output, *, start_tolerance):
    for key, tolerance in (('start_time', start_tolerance), ('duration', 0.2)):
        if source.get(key) in (None, 'N/A'):
            continue
        before, after = float(source[key]), float(output[key])
        if not (math.isfinite(before) and math.isfinite(after)
                and abs(before - after) <= tolerance):
            return False
    return True


def _performance_preview_matches(document, duration, video, audio, long_side, short_side):
    preview_duration = float(document['format']['duration'])
    if not math.isfinite(preview_duration) or abs(preview_duration - duration) > 0.2:
        return False
    output = next((stream for stream in document['streams']
                   if stream.get('codec_type') == 'video'), {})
    width, height = _dimensions(output)
    source_width, source_height = _dimensions(video)
    if (not width or max(width, height) > min(long_side, max(source_width, source_height))
            or min(width, height) > min(short_side, min(source_width, source_height))):
        return False
    if not _stream_timing_matches(video, output, start_tolerance=0.002):
        return False
    frames = video.get('nb_frames')
    if frames not in (None, 'N/A') and int(frames) != int(output['nb_frames']):
        return False
    output_audio = _audio_streams(document)
    return len(output_audio) == len(audio) and all(
        after.get('codec_name') == 'aac'
        and _stream_timing_matches(before, after, start_tolerance=0.05)
        for before, after in zip(audio, output_audio, strict=True))


def prepare_playback_path(source: str, cancelled: threading.Event, mode: str = 'auto') -> str:
    """Build the selected editor preview after a bounded eligibility probe."""
    temporary = None
    fallback = source
    try:
        cached = cached_playback_path(source, mode)
        if mode not in _PROXY_PRESETS:
            return cached
        target = _cache_path(source, mode)
        if cached != source:
            if Path(cached) == target:
                return cached
            if is_compatibility_cache_path(source, cached):
                fallback = cached
            else:
                return cached
        if cancelled.is_set():
            return fallback
        playback_source = fallback
        with _PROBE_LOCK:
            decision = _PROBE_DECISIONS.get(target)
            if decision is not None:
                _PROBE_DECISIONS.move_to_end(target)
        if decision is False:
            return fallback
        probe = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-read_intervals', '0%+8', '-show_entries',
             'packet=pts_time,flags,stream_index:format=duration:stream=index,codec_type,'
             'codec_name,width,height,avg_frame_rate,color_transfer,start_time,duration,nb_frames',
             '-of', 'json', playback_source], cancelled, timeout_seconds=4, background='proxy')
        if probe is None or probe[0] != 0:
            return fallback
        document = json.loads(probe[1])
        video = next((stream for stream in document['streams']
                      if stream.get('codec_type') == 'video'), {})
        width, height = _dimensions(video)
        if not width or cancelled.is_set():
            return fallback
        transfer = video.get('color_transfer')
        if (transfer in _HDR_TRANSFERS
                or (transfer in (None, 'unknown') and probe_hdr(playback_source, cancelled))):
            if not cancelled.is_set() and _cache_path(source, mode) == target:
                _remember_probe(target, False)
            return fallback  # Keep HDR on its original, color-managed decoder path.
        duration = float(document.get('format', {}).get('duration', 0))
        if not math.isfinite(duration) or not 0 < duration <= _MAX_PROXY_DURATION_SECONDS:
            if (math.isfinite(duration) and duration > _MAX_PROXY_DURATION_SECONDS
                    and not cancelled.is_set() and _cache_path(source, mode) == target):
                _remember_probe(target, False)
            return fallback
        packets = [packet for packet in document.get('packets', [])
                   if isinstance(packet, dict) and packet.get('stream_index') == video['index']]
        needed = (mode != 'auto' or _demanding_video(video, width, height)
                  or needs_seek_proxy(packets))
        # Empty/malformed packet responses are not successful eligibility evidence.
        if (not needed and _has_keyframe_evidence(packets)
                and not cancelled.is_set() and _cache_path(source, mode) == target):
            _remember_probe(target, False)
        if not needed:
            return fallback
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f'.{os.getpid()}-{threading.get_ident()}.partial.mp4')
        ffmpeg = get_ffmpeg_exe()
        long_side, short_side, bitrate = _PROXY_PRESETS[mode]
        scale = _preview_scale(long_side, short_side)
        # Normalize full-range SDR samples before applying the encoder's
        # limited-range BT.709 tags. HDR goes through compatibility above.
        video_filter = f'{scale}:out_color_matrix=bt709:out_range=tv,format=yuv420p'
        audio = _audio_streams(document)
        # Copy AAC timing and samples. Other source codecs can be unavailable
        # to the Windows player, so use AAC for those container-audio fallbacks.
        audio_args = [
            option for index, stream in enumerate(audio) if stream.get('codec_name') != 'aac'
            for option in (f'-c:a:{index}', 'aac', f'-b:a:{index}', '128k',
                           f'-aac_coder:a:{index}', 'fast', f'-threads:a:{index}', '1')
        ]
        result = run_media_process(
            [ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '2',
             '-filter_threads', '1', '-copyts', '-i', playback_source,
             '-map', '0:v:0', '-map', '0:a?', '-vf', video_filter,
             *software_video_args(bitrate, ffmpeg), '-threads:v', '2',
             '-force_key_frames', 'expr:if(isnan(prev_forced_t),1,gte(t,prev_forced_t+1))',
             '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux',
             '-c:a', 'copy', *audio_args,
             '-avoid_negative_ts', 'disabled', '-movflags', '+faststart+write_colr',
             '-fs', str(_MAX_CACHE_BYTES), str(temporary)],
            cancelled, timeout_seconds=_MAX_PROXY_ENCODE_SECONDS, background='proxy')
        if (result is None or result[0] != 0 or cancelled.is_set()
                or _cache_path(source, mode) != target
                or not 0 < temporary.stat().st_size <= _MAX_CACHE_BYTES):
            return fallback
        checked = run_media_process(
            [get_ffprobe_exe(), '-v', 'error', '-show_entries',
             'format=duration:stream=codec_type,codec_name,width,height,start_time,duration,nb_frames',
             '-of', 'json', str(temporary)], cancelled, timeout_seconds=4, background='proxy')
        if checked is None or checked[0] != 0 or cancelled.is_set():
            return fallback
        if not _performance_preview_matches(
                json.loads(checked[1]), duration, video, audio, long_side, short_side):
            return fallback
        if sys.platform == 'win32':
            from core.mp4_timing import normalize_mp4_presentation_in_place
            # A hard normalizer failure must never publish an unverified Windows
            # proxy; an unsupported MP4 shape is still a valid proxy result.
            try:
                normalize_mp4_presentation_in_place(temporary, cancelled)
            except (OSError, RuntimeError, ValueError, TypeError):
                return fallback
            if cancelled.is_set():
                return fallback
        if (_cache_path(source, mode) != target
                or cancelled.is_set()
                or temporary.stat().st_size > _MAX_CACHE_BYTES):
            return fallback
        os.replace(temporary, target)
        _prune_cache(target)
        return str(target)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError, IndexError):
        # Preview optimization is optional and must not block source playback.
        return fallback
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Cancellation cleanup must not replace a successfully returned source path.
                pass


def _probe_playback_streams(source: str, cancelled: threading.Event,
                            *, discover: bool = False) -> list[dict]:
    try:
        # MP4 track headers contain the compatibility timing and codec tags.
        # Other containers may need stream discovery to report their duration.
        mp4 = Path(source).suffix.lower() in _MP4_EXTENSIONS
        result = run_media_process(
            [get_ffprobe_exe(), '-v', 'error',
             *(['-nofind_stream_info'] if mp4 and not discover else []), '-show_entries',
             'format=duration:stream=codec_type,codec_tag_string,start_time,duration,'
             'nb_frames,r_frame_rate,'
             'color_transfer,color_space,color_primaries,color_range:stream_side_data',
             '-of', 'json', source],
            cancelled, timeout_seconds=4)
        if result is not None and result[0] == 0:
            document = json.loads(result[1])
            streams = document.get('streams', [])
            if isinstance(streams, list) and all(isinstance(s, dict) for s in streams):
                # Fragmented recordings store their initial timestamps in media
                # fragments, not the empty track header. Discover those packets
                # before validating the remux; never assume a zero start.
                if (mp4 and not discover and any(
                        s.get('codec_type') == 'video' and 'start_time' not in s
                        for s in streams)):
                    return _probe_playback_streams(source, cancelled, discover=True)
                if not mp4:
                    for stream in streams:
                        stream.setdefault('duration', document.get('format', {}).get('duration'))
                return streams
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError):
        # The existing presentation normalization can still be attempted.
        pass
    return []


def _compatible_remux_matches(source_streams: list[dict], output_streams: list[dict]) -> bool:
    source_video = next((s for s in source_streams if s.get('codec_type') == 'video'), {})
    output_video = next((s for s in output_streams if s.get('codec_type') == 'video'), {})
    if not output_video or source_video.get('codec_tag_string') != output_video.get('codec_tag_string'):
        return False
    try:
        source_start, output_start = (
            float(video['start_time']) for video in (source_video, output_video))
        source_duration, output_duration = (
            float(video['duration']) for video in (source_video, output_video))
        if (not all(math.isfinite(value) for value in (
                source_start, output_start, source_duration, output_duration))
                or source_duration <= 0 or output_duration <= 0
                or abs(source_start - output_start) > 0.002):
            return False
        duration_delta = abs(source_duration - output_duration)
        if duration_delta > 0.002:
            # The MP4 muxer can round an edit list down to the last sample
            # boundary. Irregular replay cadence can hold the final sample
            # longer than 1/r_frame_rate (for example 83 ms at nominal 60 FPS).
            # Allow only a bounded tail change with every sample retained and
            # an unchanged presentation start; truncated/remapped media fails.
            frames = int(source_video['nb_frames'])
            if (frames <= 0 or frames != int(output_video['nb_frames'])
                    or output_duration > source_duration
                    or duration_delta > 0.25 + 0.002):
                return False
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        # Incomplete timing metadata cannot establish that the remux preserved the video.
        return False
    source_audio = [s.get('codec_tag_string') for s in source_streams if s.get('codec_type') == 'audio']
    output_audio = [s.get('codec_tag_string') for s in output_streams if s.get('codec_type') == 'audio']
    return output_audio == ['mp4a' if tag in _MP4_PCM_TAGS else tag for tag in source_audio]


def _prepare_hdr_sdr_preview(source, source_streams, color, temporary, cancelled):
    """Build an editor-only SDR file for the current SDR preview surface."""
    from core.export_encoding import choose_video_args

    ffmpeg = get_ffmpeg_exe()
    deadline = time.monotonic() + 120
    video_args = choose_video_args(
        ffmpeg, quality='fast', bitrate=5000, cancel=cancelled)
    software_args = software_video_args(5000, ffmpeg)
    # Bound conversion and playback cost without changing the editor's source
    # coordinates, which come from the original file's metadata.
    size = (
        "w='min(iw,if(gte(iw,ih),1280,720))':"
        "h='min(ih,if(gte(iw,ih),720,1280))':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2")
    tag_input = (f'setparams=range={color.range}:color_primaries={color.primaries}:'
                 f'color_trc={color.transfer}:colorspace={color.matrix}')
    gpu_filter = (
        f'{tag_input},libplacebo={size}:format=yuv420p:colorspace=bt709:'
        'color_primaries=bt709:color_trc=bt709:range=tv')
    cpu_filter = f'scale={size},' + sdr_tonemap_filter(color)
    for video_filter, encoder_args in ((gpu_filter, video_args), (cpu_filter, software_args)):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled.is_set():
            return False
        result = run_media_process(
            [ffmpeg, '-v', 'error', '-nostdin', '-y', '-threads', '2',
             '-filter_threads', '2', '-copyts', '-i', source,
             '-map', '0:v:0', '-map', '0:a?', '-vf', video_filter,
             *encoder_args, '-threads:v', '2',
             '-fps_mode', 'passthrough', '-enc_time_base:v', 'demux',
             '-c:a', 'aac', '-b:a', '192k', '-aac_coder', 'fast', '-threads:a', '1',
             '-avoid_negative_ts', 'disabled', '-movflags', '+faststart+write_colr',
             '-fs', str(_MAX_CACHE_BYTES), str(temporary)],
            cancelled, timeout_seconds=remaining, background='proxy')
        if (result is not None and result[0] == 0 and not cancelled.is_set()
                and _hdr_preview_matches(
                    source_streams, _probe_playback_streams(str(temporary), cancelled))):
            return True
    return False


def _hdr_preview_matches(source_streams, output_streams):
    source_video = next(s for s in source_streams if s.get('codec_type') == 'video')
    output_video = next((s for s in output_streams if s.get('codec_type') == 'video'), {})
    # The size cap can produce a valid but truncated container. Require the
    # full video timeline before making the preview available to the editor.
    if any(output_video.get(key) != 'bt709'
           for key in ('color_transfer', 'color_space', 'color_primaries')):
        return False
    try:
        for key, tolerance in (('start_time', 0.002), ('duration', 0.2)):
            before = float(source_video.get(key, 0))
            after = float(output_video[key])
            if not (math.isfinite(before) and math.isfinite(after)
                    and abs(before - after) <= tolerance):
                return False
    except (KeyError, TypeError, ValueError):
        # An SDR copy with unverified timing must stay out of the preview cache.
        return False
    return sum(s.get('codec_type') == 'audio' for s in output_streams) == sum(
        s.get('codec_type') == 'audio' for s in source_streams)


def prepare_compatibility_path(source: str, cancelled: threading.Event) -> str:
    """Prepare a display-compatible source before the first editor playback."""
    temporary = None
    try:
        cached = cached_playback_path(source, mode='original')
        if cached != source or cancelled.is_set():
            return cached
        compatibility = _compatibility_cache_path(source)
        source_path = Path(source)
        source_size = source_path.stat().st_size
        mp4 = source_path.suffix.lower() in _MP4_EXTENSIONS
        source_streams = _probe_playback_streams(source, cancelled)
        audio_streams = [s for s in source_streams if s.get('codec_type') == 'audio']
        pcm_audio_indices = [i for i, stream in enumerate(audio_streams)
                             if stream.get('codec_tag_string') in _MP4_PCM_TAGS]
        # Required codec compatibility must work for long recordings too.
        # Video is copied and PCM shrinks to AAC; bound growth relative to the
        # source while allowing one entry to exceed the optional proxy budget.
        pcm_remux = bool(pcm_audio_indices and mp4)
        output_limit = (max(_MAX_CACHE_BYTES, source_size + _MAX_COMPATIBILITY_GROWTH)
                        if pcm_remux else _MAX_CACHE_BYTES)
        video = next((s for s in source_streams if s.get('codec_type') == 'video'), {})
        color = hdr_color_from_stream(video)
        if color is not None or (video and video.get('color_transfer') in {None, 'unknown'}):
            # Imported files can signal HDR or mastering data in the bitstream.
            color = probe_hdr(source, cancelled) or color
        if cancelled.is_set():
            return source
        if color is None and (sys.platform != 'win32'
                              or (not pcm_remux and source_size >
                                  _MAX_CACHE_BYTES - _MAX_COMPATIBILITY_GROWTH)):
            return source
        target = (compatibility.with_suffix(_HDR_PREVIEW_SUFFIX)
                  if color is not None else compatibility)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(
            f'.{os.getpid()}-{threading.get_ident()}.partial.mp4')
        from core.mp4_timing import (
            normalize_mp4_presentation, normalize_mp4_presentation_in_place,
        )
        if color is not None:
            if not _prepare_hdr_sdr_preview(
                    source, source_streams, color, temporary, cancelled):
                return source
            if sys.platform == 'win32':
                normalize_mp4_presentation_in_place(temporary, cancelled)
        elif pcm_audio_indices and mp4:
            # The editor mixer decodes the original float tracks. AAC is only
            # for the Windows player and its container-audio fallback.
            # FFmpeg's fast AAC coder is suited to this higher bitrate.
            audio_args = [
                option for index in pcm_audio_indices
                for option in (f'-c:a:{index}', 'aac', f'-b:a:{index}', '192k',
                               f'-aac_coder:a:{index}', 'fast', f'-threads:a:{index}', '1')
            ]
            result = run_media_process(
                [get_ffmpeg_exe(), '-v', 'error', '-nostdin', '-y',
                 '-copyts', '-i', source, '-map', '0:v:0', '-map', '0:a',
                 '-c', 'copy', '-copytb', '1', *audio_args,
                 '-avoid_negative_ts', 'disabled', '-movflags', '+faststart',
                 str(temporary)], cancelled, timeout_seconds=120, background=True)
            if (result is None or result[0] != 0 or cancelled.is_set()
                    or temporary.stat().st_size > output_limit):
                return source
            normalize_mp4_presentation_in_place(temporary, cancelled)
            if not _compatible_remux_matches(
                    source_streams, _probe_playback_streams(str(temporary), cancelled)):
                return source
        elif not normalize_mp4_presentation(source, temporary, cancelled):
            return source
        if (cancelled.is_set()
                or _compatibility_cache_path(source) != compatibility
                or temporary.stat().st_size <= 0
                or temporary.stat().st_size > (output_limit if color is None else _MAX_CACHE_BYTES)):
            return source
        os.replace(temporary, target)
        _prune_cache(target)
        return str(target)
    except (OSError, RuntimeError, ValueError, TypeError, AttributeError, KeyError):
        # This optimization is optional; native playback keeps the original.
        return source
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # A cancelled cleanup must not replace the original playback path.
                pass
