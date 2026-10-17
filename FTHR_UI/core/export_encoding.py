"""Editor/share encoding policy. Hardware is verified with the actual options."""
from __future__ import annotations

import subprocess
import threading

from core.ffmpeg_tools import (
    _NO_WINDOW, _sdr_color_args, _seekable_video_args, software_video_args,
    size_constrained_video_args,
)

QUALITY_OPTIONS = (('Very fast', 'fast'), ('High quality', 'quality'),
                   ('Lossless · MKV', 'lossless'))
VIDEO_PLACEHOLDER = ['-c:v', 'fthr_export_encoder']
_working = {}
_probe_lock = threading.Lock()


def normalize_quality(value):
    return value if value in {'fast', 'quality', 'lossless'} else 'quality'


def video_args(quality, encoder, bitrate=None):
    quality = normalize_quality(quality)
    if quality == 'lossless':
        # FFV1 preserves the filtered frames without quantization. Do not force
        # yuv420p here: that could discard chroma/bit depth from imported media.
        return ['-c:v', 'ffv1', '-level', '3', '-coder', '1', '-context', '1']
    fast = quality == 'fast'
    args = ['-c:v', encoder]
    if encoder == 'h264_nvenc':
        args += ['-preset', 'p1' if fast else 'p5', '-tune', 'hq', '-rc', 'vbr']
        args += ['-b:v', f'{bitrate}k'] if bitrate else ['-cq', '23' if fast else '18', '-b:v', '0']
    elif encoder == 'h264_amf':
        args += ['-quality', 'speed' if fast else 'quality']
        args += (['-rc', 'vbr_peak', '-b:v', f'{bitrate}k'] if bitrate else
                 ['-rc', 'cqp', '-qp_i', '23' if fast else '18',
                  '-qp_p', '23' if fast else '18'])
    elif encoder == 'h264_qsv':
        args += ['-preset', 'veryfast' if fast else 'medium']
        args += ['-b:v', f'{bitrate}k'] if bitrate else ['-global_quality', '23' if fast else '18']
    return [*args, '-pix_fmt', 'yuv420p', *_seekable_video_args(), *_sdr_color_args()]


def software_args(quality, ffmpeg, bitrate=None):
    if quality == 'lossless':
        return video_args(quality, 'ffv1')
    args = (size_constrained_video_args(bitrate, ffmpeg) if bitrate else
            software_video_args(16000 if quality == 'fast' else 32000, ffmpeg))
    if args[1] == 'libopenh264':
        args += ['-coder', 'cavlc' if quality == 'fast' else 'cabac']
    return args


def choose_video_args(ffmpeg, quality='quality', bitrate=None, hardware=True, cancel=None):
    quality = normalize_quality(quality)
    if hardware and quality != 'lossless':
        for encoder in ('h264_nvenc', 'h264_amf', 'h264_qsv'):
            if cancel is not None and cancel.is_set():
                break
            args = video_args(quality, encoder, bitrate)
            key = (ffmpeg, quality, encoder, bitrate is not None)
            while not _probe_lock.acquire(timeout=.1):
                if cancel is not None and cancel.is_set():
                    return software_args(quality, ffmpeg, bitrate)
            try:
                if cancel is not None and cancel.is_set():
                    break
                works = _working.get(key)
                if works is None:
                    try:
                        works = subprocess.run(
                            [ffmpeg, '-v', 'error', '-f', 'lavfi', '-i',
                             'color=s=128x128:r=30', '-frames:v', '1', *args,
                             '-f', 'null', '-'], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=8, **_NO_WINDOW,
                        ).returncode == 0
                    except (OSError, subprocess.SubprocessError):
                        works = False
                    _working[key] = works
            finally:
                _probe_lock.release()
            if works:
                return args
    return software_args(quality, ffmpeg, bitrate)


def encode_command(template, args, quality):
    command = list(template)
    if 'fthr_export_encoder' in command:
        index = command.index('fthr_export_encoder') - 1
        command[index:index + 2] = args
    # Limit decoder/filter/encoder CPU work even with a hardware encoder.
    command[1:1] = ['-threads', '2', '-filter_threads', '2', '-filter_complex_threads', '2']
    command[-1:-1] = ['-threads:v', '2']
    if quality == 'lossless' and '-c:a' in command:
        index = command.index('-c:a') + 1
        if command[index] != 'copy':
            command[index] = 'flac'
            if '-b:a' in command:
                index = command.index('-b:a')
                del command[index:index + 2]
    return command
