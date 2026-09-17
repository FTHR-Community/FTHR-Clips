"""Resolve FFmpeg and construct encoder arguments shared by UI media jobs.

Prefer the bundled LGPL build used by the capture engine. Its OpenH264
encoder uses bitrate control and does not accept x264 preset/CRF options.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

# Suppress the console window ffmpeg would otherwise flash on Windows.
_NO_WINDOW = {'creationflags': 0x08000000} if sys.platform == 'win32' else {}

_EXE_NAME = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
_PROBE_NAME = 'ffprobe.exe' if sys.platform == 'win32' else 'ffprobe'

# Full-quality export uses this bitrate ceiling when OpenH264 must re-encode.
# OpenH264 doesn't support CRF or lossless encoding.
MAXIMUM_QUALITY_VIDEO_BITRATE_KBPS = 200_000

# Resolved lazily, then cached — resolution touches the filesystem and the
# encoder probe spawns a process; neither should happen per clip.
_cached_exe: Optional[str] = None
_cached_encoder: Optional[str] = None
_cached_probe: Optional[str] = None


class FFmpegUnavailable(RuntimeError):
    """Raised when FFmpeg is unavailable; callers should display the reason."""


def _candidate_paths() -> list[Path]:
    """Where to look, most-specific first.

    Note the deliberate absence of ``imageio_ffmpeg`` from the top of this
    list: it is a GPL build and must never be what a *release* uses.
    """
    here = Path(__file__).resolve()
    out: list[Path] = []

    # Frozen bundle: FTHR.spec places FFmpeg beside the engine.
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        out.append(Path(meipass) / 'engine' / _EXE_NAME)
        out.append(Path(meipass) / _EXE_NAME)

    # AppImage layout: the engine sits beside AppRun.
    out.append(Path(sys.executable).parent / _EXE_NAME)
    out.append(Path(sys.executable).parent / 'engine' / _EXE_NAME)

    # Development runtime beside the native engine; parents[2] is the repo root.
    root = here.parents[2]
    for relative in (
        ('FTHRcapture', 'FTHRclips', 'third_party', 'ffmpeg', 'bin'),
        ('FTHRcapture_linux', 'third_party', 'ffmpeg', 'bin'),
    ):
        out.append(root.joinpath(*relative, _EXE_NAME))

    return out


def _candidate_probe_paths() -> list[Path]:
    """Find ffprobe beside each supported FFmpeg location."""

    return [path.with_name(_PROBE_NAME) for path in _candidate_paths()]


def get_ffmpeg_exe() -> str:
    """Return the bundled FFmpeg path, falling back to FFmpeg on PATH.

    Raise FFmpegUnavailable if neither exists. System builds may lack OpenH264;
    software_video_args probes their available encoders.
    """
    global _cached_exe
    if _cached_exe is not None:
        return _cached_exe

    for cand in _candidate_paths():
        try:
            if cand.is_file():
                _cached_exe = str(cand)
                return _cached_exe
        except OSError:
            # An update may remove a candidate directory; try the remaining locations.
            continue

    # System ffmpeg (typical on Linux; also fine for a dev box on Windows).
    from shutil import which
    found = which('ffmpeg')
    if found:
        _cached_exe = found
        return _cached_exe

    raise FFmpegUnavailable(
        'No ffmpeg binary found. FTHR Clips ships one next to the capture '
        'engine; if this is a source checkout, run the build script or install '
        'ffmpeg and make sure it is on your PATH. Watermark, webcam '
        'overlay and clip export need it.'
    )


def get_ffprobe_exe() -> str:
    """Resolve matching FFprobe for metadata reads on a worker.

    Playback decoding uses the native bridge; only metadata probing shells out.
    """

    global _cached_probe
    if _cached_probe is not None:
        return _cached_probe
    for candidate in _candidate_probe_paths():
        try:
            if candidate.is_file():
                _cached_probe = str(candidate)
                return _cached_probe
        except OSError:
            # An update may remove a candidate directory; try the remaining locations.
            continue
    from shutil import which
    found = which('ffprobe')
    if found:
        _cached_probe = found
        return _cached_probe
    raise FFmpegUnavailable(
        'No ffprobe binary found. FTHR Clips ships one with its reviewed '
        'FFmpeg runtime; imported multi-track clips need it for honest track labels.'
    )


def _probe_encoders(ffmpeg: str) -> str:
    """Return the best available software H.264 encoder name.

    Probing beats hardcoding because the PATH fallback above can land on a
    distro FFmpeg whose encoder set we do not control.
    """
    try:
        res = subprocess.run(
            [ffmpeg, '-hide_banner', '-encoders'],
            capture_output=True, text=True, timeout=20, **_NO_WINDOW,
        )
        available = res.stdout or ''
    except (OSError, subprocess.SubprocessError):
        # Probe failed — assume the encoder our own bundled build carries.
        return 'libopenh264'

    # Preference order. libopenh264 is what we ship; the rest are graceful
    # degradations for third-party FFmpeg builds.
    for name in ('libopenh264', 'h264_mf', 'libx264'):
        if name in available:
            return name
    return 'libopenh264'


def software_h264_encoder(ffmpeg: Optional[str] = None) -> str:
    """Name of the software H.264 encoder this installation should use."""
    global _cached_encoder
    if _cached_encoder is None:
        _cached_encoder = _probe_encoders(ffmpeg or get_ffmpeg_exe())
    return _cached_encoder


def _sdr_color_args() -> list[str]:
    """Return the color metadata shared by every editor video transcode."""
    # Editor/share transcodes must retain the capture engine's SDR range. If
    # this metadata is omitted, Windows players can guess full-range YUV and
    # expand studio-range samples a second time, making the saved clip brighter.
    return [
        '-color_range', 'tv',
        '-colorspace', 'bt709',
        '-color_primaries', 'bt709',
        '-color_trc', 'bt709',
        # OpenH264 omits primaries/transfer from its SPS. Patch the H.264 VUI after
        # encoding so exports retain the complete SDR color metadata.
        '-bsf:v',
        ('h264_metadata=video_full_range_flag=0:colour_primaries=1:'
         'transfer_characteristics=1:matrix_coefficients=1'),
    ]


def _seekable_video_args() -> list[str]:
    """Bound random-access decode work in every H.264 transcode.

    OpenH264's default GOP can span an entire clip. Media Foundation decodes
    that interval again on both seek and resume. Thirty frames also keeps
    exports/imported low-FPS footage usable without trusting FPS metadata.
    """
    return ['-g', '30', '-bf', '0']


def software_video_args(bitrate_kbps: int = 16000,
                        ffmpeg: Optional[str] = None) -> list[str]:
    """Select software H.264 arguments.

    OpenH264 uses a target bitrate (16 Mbit/s by default); system x264 uses
    CRF 18. Include seekable-video and SDR color metadata options.
    """
    enc = software_h264_encoder(ffmpeg)
    color_args = [*_seekable_video_args(), *_sdr_color_args()]

    if enc == 'libopenh264':
        return [
            '-c:v', 'libopenh264',
            '-b:v', f'{bitrate_kbps}k',
            # Never drop frames to hit the bitrate: the clip would desync
            # against the audio track muxed in afterwards.
            '-allow_skip_frames', '0',
            '-profile:v', 'high',
            *color_args,
        ]
    if enc == 'libx264':
        # Only reachable via a third-party/system FFmpeg that still has x264.
        # FTHR does not ship this path.
        return [
            '-c:v', 'libx264', '-preset', 'superfast', '-crf', '18',
            *color_args,
        ]
    # h264_mf (Windows MediaFoundation) and anything else: bitrate only.
    return ['-c:v', enc, '-b:v', f'{bitrate_kbps}k', *color_args]


def postprocess_video_args(active_codec: object = '',
                           bitrate_kbps: int = 16000,
                           ffmpeg: Optional[str] = None) -> list[str]:
    """Use the active H.264 hardware backend for visual post-processing.

    Unknown, unavailable, or non-H.264 backends use the software fallback.
    """
    encoder = str(active_codec or '').strip().lower()
    hardware_encoders = {
        'h264_nvenc', 'h264_amf', 'h264_qsv', 'h264_mf',
    }
    if encoder not in hardware_encoders:
        return software_video_args(bitrate_kbps, ffmpeg)

    args = [
        '-c:v', encoder,
        '-b:v', f'{max(2500, int(bitrate_kbps))}k',
        '-pix_fmt', 'yuv420p',
    ]
    if encoder == 'h264_nvenc':
        # P1 is NVENC's fastest preset; low-latency tuning is appropriate for
        # a short-lived clip finalizer and does not alter the source capture.
        args.extend(['-preset', 'p1', '-tune', 'll'])
    return [*args, *_seekable_video_args(), *_sdr_color_args()]


def maximum_quality_video_args(ffmpeg: Optional[str] = None) -> list[str]:
    """Choose video arguments for full-quality editor exports.

    Untouched video can be stream-copied by the caller. For re-encoding, x264
    uses CRF 0; OpenH264 uses the 200 Mbps ceiling with frame skipping disabled.
    Other H.264 encoders use the high-bitrate fallback.
    """
    enc = software_h264_encoder(ffmpeg)
    color_args = [*_seekable_video_args(), *_sdr_color_args()]
    if enc == 'libx264':
        return [
            '-c:v', 'libx264',
            '-preset', 'veryslow',
            '-crf', '0',
            *color_args,
        ]
    if enc == 'libopenh264':
        return [
            '-c:v', 'libopenh264',
            '-b:v', f'{MAXIMUM_QUALITY_VIDEO_BITRATE_KBPS}k',
            '-allow_skip_frames', '0',
            '-profile:v', 'high',
            *color_args,
        ]
    return [
        '-c:v', enc,
        '-b:v', f'{MAXIMUM_QUALITY_VIDEO_BITRATE_KBPS}k',
        *color_args,
    ]


def size_constrained_video_args(
        bitrate_kbps: int, ffmpeg: Optional[str] = None) -> list[str]:
    """Return H.264 arguments whose bitrate is a real file-size constraint.

    The normal third-party libx264 path uses CRF for quality exports. A target
    file size cannot rely on CRF, so this variant replaces it with ABR while
    retaining the reviewed encoder and preset selection.
    """
    args = software_video_args(bitrate_kbps, ffmpeg)
    constrained: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == '-crf' and index + 1 < len(args):
            index += 2
            continue
        constrained.append(args[index])
        index += 1
    if '-b:v' not in constrained:
        constrained += ['-b:v', f'{int(bitrate_kbps)}k']
    return constrained


def reset_cache() -> None:
    """Clear memoized resolution. Tests only."""
    global _cached_exe, _cached_encoder, _cached_probe
    _cached_exe = None
    _cached_encoder = None
    _cached_probe = None
