"""Discover usable encoders with a one-frame in-memory encode.

FFmpeg registration alone does not prove that hardware or drivers work.
Call discovery from a worker because driver initialization can block.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Iterable

from core.ffmpeg_tools import get_ffmpeg_exe


_CACHE_VERSION = 2


@dataclass(frozen=True)
class EncoderCapability:
    key: str
    label: str
    codecs: tuple[str, ...]
    codec_names: tuple[tuple[str, str], ...]
    supports_presets: bool = False

    def encoder_name_for(self, codec: str) -> str:
        return dict(self.codec_names).get(codec, '')


_BACKENDS: tuple[
    tuple[str, str, bool, tuple[tuple[str, tuple[str, ...]], ...]], ...
] = (
    ('nvenc', 'NVIDIA NVENC', True, (
        ('h264', ('h264_nvenc',)),
        ('hevc', ('hevc_nvenc',)),
        ('av1', ('av1_nvenc',)),
    )),
    ('amf', 'AMD AMF', False, (
        ('h264', ('h264_amf',)),
        ('hevc', ('hevc_amf',)),
        ('av1', ('av1_amf',)),
    )),
    ('qsv', 'Intel Quick Sync', False, (
        ('h264', ('h264_qsv',)),
        ('hevc', ('hevc_qsv',)),
        ('av1', ('av1_qsv',)),
    )),
    ('software', 'Software', False, (
        ('h264', ('libopenh264',)),
        ('hevc', ('libkvazaar',)),
        ('av1', ('libsvtav1', 'libaom-av1', 'librav1e')),
    )),
)


def _platform_backend_keys(platform: str) -> tuple[str, ...]:
    return ('nvenc', 'amf', 'qsv', 'software')


def _probe_command(ffmpeg: str, encoder_name: str) -> list[str]:
    return [
        ffmpeg,
        '-hide_banner',
        '-loglevel', 'error',
        '-f', 'lavfi',
        '-i', 'color=c=black:s=640x360:r=30:d=0.1',
        '-frames:v', '1',
        '-pix_fmt', 'nv12',
        '-c:v', encoder_name,
        '-f', 'null',
        '-',
    ]


def _encoder_works(
        ffmpeg: str,
        encoder_name: str,
        *,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: float = 5.0) -> bool:
    kwargs = {
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
        'timeout': timeout,
        'check': False,
    }
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        return runner(_probe_command(ffmpeg, encoder_name), **kwargs).returncode == 0
    except (OSError, subprocess.SubprocessError):
        # A missing/crashed/timed-out driver probe means this candidate must
        # simply stay out of the device-specific selector.
        return False


def probe_encoder_capabilities(
        ffmpeg: str | None = None,
        *,
        platform: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        timeout: float = 5.0) -> tuple[EncoderCapability, ...]:
    """Return only backends/codecs that complete a real one-frame encode."""
    platform = platform or sys.platform
    try:
        executable = ffmpeg or get_ffmpeg_exe()
    except Exception:
        return ()

    allowed = set(_platform_backend_keys(platform))
    found: list[EncoderCapability] = []
    for key, label, supports_presets, codec_candidates in _BACKENDS:
        if key not in allowed:
            continue
        codecs: list[str] = []
        codec_names: list[tuple[str, str]] = []
        for codec, encoder_names in codec_candidates:
            # Windows software replay currently uses the low-latency OpenH264 path.
            if platform.startswith('win') and key == 'software' and codec != 'h264':
                continue
            working_name = next((
                name for name in encoder_names
                if _encoder_works(
                    executable, name, runner=runner, timeout=timeout)
            ), '')
            if working_name:
                codecs.append(codec)
                codec_names.append((codec, working_name))
        if codecs:
            found.append(EncoderCapability(
                key=key,
                label=label,
                codecs=tuple(codecs),
                codec_names=tuple(codec_names),
                supports_presets=supports_presets,
            ))
    return tuple(found)


def available_codecs(
        capabilities: Iterable[EncoderCapability],
        encoder_key: str) -> tuple[str, ...]:
    """Return codec keys in UI priority order for one selected backend."""
    capabilities = tuple(capabilities)
    if encoder_key == 'auto':
        present = {codec for capability in capabilities for codec in capability.codecs}
    else:
        present = next((
            set(capability.codecs)
            for capability in capabilities
            if capability.key == encoder_key
        ), set())
    return tuple(codec for codec in ('h264', 'hevc', 'av1') if codec in present)


def _cache_path() -> Path:
    return Path.home() / '.fthr' / 'encoder_capabilities.json'


def _ffmpeg_signature(ffmpeg: str, platform: str) -> dict:
    path = Path(ffmpeg).resolve()
    stat = path.stat()
    return {
        'version': _CACHE_VERSION,
        'platform': platform,
        'ffmpeg': os.path.normcase(str(path)),
        'ffmpeg_size': stat.st_size,
        'ffmpeg_mtime_ns': stat.st_mtime_ns,
    }


def _serialize_capability(item: EncoderCapability) -> dict:
    return {
        'key': item.key,
        'label': item.label,
        'codecs': list(item.codecs),
        'codec_names': [list(pair) for pair in item.codec_names],
        'supports_presets': item.supports_presets,
    }


def _deserialize_capability(raw: dict) -> EncoderCapability:
    return EncoderCapability(
        key=str(raw['key']),
        label=str(raw['label']),
        codecs=tuple(str(value) for value in raw.get('codecs', ())),
        codec_names=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in raw.get('codec_names', ())
            if isinstance(pair, (list, tuple)) and len(pair) == 2),
        supports_presets=bool(raw.get('supports_presets', False)),
    )


def load_cached_encoder_capabilities(
        ffmpeg: str | None = None, *, platform: str | None = None,
        cache_file: Path | None = None) -> tuple[EncoderCapability, ...] | None:
    """Load a device result when the platform and FFmpeg build still match."""
    platform = platform or sys.platform
    try:
        executable = ffmpeg or get_ffmpeg_exe()
        expected = _ffmpeg_signature(executable, platform)
        path = cache_file or _cache_path()
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict):
            return None
        for key, value in expected.items():
            if raw.get(key) != value:
                return None
        capabilities = raw.get('capabilities')
        if not isinstance(capabilities, list):
            return None
        return tuple(
            _deserialize_capability(item)
            for item in capabilities
            if isinstance(item, dict))
    except (OSError, ValueError, TypeError, KeyError):
        # A missing, stale, or malformed cache is an expected cache miss.
        return None


def save_encoder_capabilities_cache(
        capabilities: Iterable[EncoderCapability], ffmpeg: str | None = None,
        *, platform: str | None = None,
        cache_file: Path | None = None) -> bool:
    """Persist discovery so normal app starts do not initialize every driver."""
    platform = platform or sys.platform
    try:
        executable = ffmpeg or get_ffmpeg_exe()
        data = _ffmpeg_signature(executable, platform)
        data['capabilities'] = [
            _serialize_capability(item) for item in capabilities]
        path = cache_file or _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
        os.replace(str(temporary), str(path))
        return True
    except (OSError, ValueError, TypeError):
        # Cache persistence is an optimization; discovery remains authoritative.
        return False
