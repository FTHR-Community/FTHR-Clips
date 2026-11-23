"""Apply source-time silence ranges without changing media duration."""

from array import array
from bisect import bisect_right
import sys

from core.ffmpeg_tools import FFmpegUnavailable, get_ffmpeg_exe
from core.media_process import run_media_process


MAX_WAVEFORM_MS = 120_000
WAVEFORM_PEAKS = 720
_WAVEFORM_SAMPLE_RATE = 6000


def decode_waveform(path, streams: tuple[int, ...], duration_ms: int, cancelled) -> tuple[int, ...]:
    """Summarize recorded tracks in fixed source-time bins with bounded work."""
    if not streams or not 0 < duration_ms <= MAX_WAVEFORM_MS or cancelled.is_set():
        return ()
    try:
        ffmpeg = get_ffmpeg_exe()
    except FFmpegUnavailable:
        return ()
    command = [ffmpeg, '-v', 'error', '-threads', '1',
               '-filter_complex_threads', '1', '-i', path]
    resample = f'aresample={_WAVEFORM_SAMPLE_RATE}:first_pts=0'
    if len(streams) == 1:
        command += ['-map', f'0:a:{streams[0]}', '-af', resample]
    else:
        filters = audio = ''
        for index, stream in enumerate(streams):
            label = f'[w{index}]'
            filters += (f'[0:a:{stream}]{resample},aformat=sample_rates={_WAVEFORM_SAMPLE_RATE}:'
                        f'channel_layouts=mono{label};')
            audio += label
        command += ['-filter_complex',
                    f'{filters}{audio}amix=inputs={len(streams)}:dropout_transition=0[wave]',
                    '-map', '[wave]']
    command += ['-vn', '-sn', '-dn', '-t', f'{duration_ms / 1000:.3f}',
                '-ac', '1', '-ar', str(_WAVEFORM_SAMPLE_RATE), '-f', 's16le', '-']
    result = run_media_process(command, cancelled, timeout_seconds=10, background='timeline')
    if (result is None or result[0] != 0 or not result[1]
            or len(result[1]) % 2 or cancelled.is_set()):
        return ()
    pcm = array('h', result[1])
    if sys.byteorder != 'little':
        pcm.byteswap()
    expected_samples = round(duration_ms * _WAVEFORM_SAMPLE_RATE / 1000)
    bin_count = min(WAVEFORM_PEAKS, expected_samples)
    peaks = []
    for index in range(bin_count):
        first = index * expected_samples // bin_count
        end = (index + 1) * expected_samples // bin_count
        samples = pcm[first:end]
        peaks.append(max(max(samples), -min(samples)) if samples else 0)
    return tuple(peaks)


def ffmpeg_silence_filter(ranges, start: float, end: float, *,
                          normal_gain: float = 1.0, cut_gain: float = 0.0) -> str | None:
    """Apply normal and cut coefficients at exact sample timestamps."""
    local = [(max(start, cut_start) - start, min(end, cut_end) - start)
             for cut_start, cut_end in ranges if cut_start < end and cut_end > start]
    if not local:
        return None
    if local == [(0, end - start)]:
        return f'volume={cut_gain:g}'

    def condition(first, last):
        if first == last:
            low, high = local[first]
            return f'gte(t,{low:.6f})*lt(t,{high:.6f})'
        middle = (first + last + 1) // 2
        return (f'if(lt(t,{local[middle][0]:.6f}),'
                f'{condition(first, middle - 1)},{condition(middle, last)})')

    affected = '0' if cut_gain == 0 else f'{cut_gain:.8f}*val(ch)'
    untouched = 'val(ch)' if normal_gain == 1 else f'{normal_gain:.8f}*val(ch)'
    return f"aeval='if({condition(0, len(local) - 1)},{affected},{untouched})':c=same"


def silence_pcm_intervals(pcm: bytes, *, frame_bytes: int, first_source_frame: int,
                          speed: float, ranges: tuple, range_ends: tuple) -> bytes:
    """Zero intersecting output samples while leaving uncut blocks untouched."""
    if not ranges:
        return pcm
    output_frames = len(pcm) // frame_bytes
    end_source_frame = first_source_frame + output_frames * speed
    edited = None
    for index in range(bisect_right(range_ends, first_source_frame), len(ranges)):
        start, end = ranges[index]
        if start >= end_source_frame:
            break
        left = max(0, round((start - first_source_frame) / speed)) * frame_bytes
        right = min(output_frames, round((end - first_source_frame) / speed)) * frame_bytes
        if left >= right:
            continue
        if edited is None:
            edited = bytearray(pcm)
        edited[left:right] = bytes(right - left)
    return bytes(edited) if edited is not None else pcm
