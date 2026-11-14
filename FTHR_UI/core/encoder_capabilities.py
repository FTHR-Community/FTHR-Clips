"""Discover usable encoders outside the UI thread.

FFmpeg registration alone does not prove that hardware or drivers work.
Call discovery from a worker because driver initialization can block.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable, Iterable

from core.ffmpeg_tools import get_ffmpeg_exe


_CACHE_VERSION = 4
_PROBE_MARKER = b'FTHR_GPU_ENCODER_PROBE_V1'
_REQUIRED_NATIVE_DLLS = ('avcodec-62.dll', 'avutil-60.dll', 'swscale-9.dll')
_GPU_VENDOR_IDS = {'nvenc': 0x10de, 'amf': 0x1002, 'qsv': 0x8086}


def _file_sha256(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


@dataclass(frozen=True)
class GpuProbeContext:
    monitor: str = ''
    width: int = 0
    height: int = 0
    fps: int = 60
    bitrate_kbps: int = 25000
    preset: int = 4
    fit: int = 0

    def arguments(self) -> list[str]:
        return [self.monitor, str(self.width), str(self.height), str(self.fps),
                str(self.bitrate_kbps), str(self.preset), str(self.fit)]


def _native_signature(ffmpeg: str, engine: str | None) -> dict | None:
    """Old engines must never receive an unknown probe flag."""
    executable = Path(engine) if engine else Path(ffmpeg).parent / 'FTHRClips.exe'
    try:
        data = executable.read_bytes()
        if _PROBE_MARKER not in data:
            return None
        dlls = sorted(
            (path for path in executable.parent.glob('*.dll') if path.is_file()),
            key=lambda path: path.name.casefold())
        if any(not (executable.parent / name).is_file()
               for name in _REQUIRED_NATIVE_DLLS):
            return None
        hashes = {'engine': hashlib.sha256(data).hexdigest()}
        for dll in dlls:
            hashes[dll.name.casefold()] = _file_sha256(dll)
        return {'path': str(executable.resolve()), 'sha256': hashes}
    except OSError:
        # Missing/unreadable native files provide no GPU qualification evidence.
        return None


def _probe_luid(raw: dict, prefix: str) -> tuple[int, int] | None:
    high, low = raw.get(f'{prefix}_luid_high'), raw.get(f'{prefix}_luid_low')
    if (type(high) is int and type(low) is int and -(1 << 31) <= high < (1 << 31)
            and 0 <= low < (1 << 32) and (high or low)):
        return high, low
    return None


def _verified_encoder_path(raw: dict, backend: str) -> bool:
    encoder = _probe_luid(raw, 'adapter')
    driver = raw.get('driver_version')
    if (encoder is None or not isinstance(driver, str) or not driver.isascii()
            or not driver.isdigit() or not 0 < int(driver) < (1 << 63)):
        return False
    if not any(key in raw for key in ('input_path', 'capture_adapter_luid_high',
                                      'capture_adapter_luid_low', 'adapter_vendor_id')):
        # Earlier probe builds only produce the same-adapter GPU path.
        return raw.get('gpu_input') is True
    capture = _probe_luid(raw, 'capture_adapter')
    vendor = raw.get('adapter_vendor_id')
    if capture is None or type(vendor) is not int or vendor != _GPU_VENDOR_IDS[backend]:
        return False
    if raw.get('input_path') == 'same-adapter-gpu':
        return raw.get('gpu_input') is True and capture == encoder
    return (backend == 'nvenc' and raw.get('input_path') == 'cross-adapter-cpu'
            and raw.get('gpu_input') is False and capture != encoder)


def _gpu_encoder_works(engine: str, backend: str, codec: str,
                       context: GpuProbeContext, runner, timeout: float) -> bool:
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                  stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        result = runner([engine, '--probe-encoder', backend, codec,
                         *context.arguments()], **kwargs)
        if result.returncode != 0:
            return False
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='strict')
        prefix = _PROBE_MARKER.decode() + ' '
        lines = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
        if len(lines) != 1:
            return False
        raw = json.loads(lines[0])
        expected = dict(schema_version=1, backend=backend, codec=codec,
                        monitor=context.monitor,
                        hardware=True, frames_submitted=48,
                        frames_decoded=48, fps=context.fps,
                        bitrate_kbps=context.bitrate_kbps, preset=context.preset, fit=context.fit)
        if not isinstance(raw, dict) or any(
                type(raw.get(key)) is not type(value) or raw[key] != value
                for key, value in expected.items()):
            return False
        for name in ('source_width', 'source_height', 'width', 'height'):
            if type(raw.get(name)) is not int or not 2 <= raw[name] <= 8192:
                return False
        if raw['width'] != ((context.width or raw['source_width']) & ~1):
            return False
        if raw['height'] != ((context.height or raw['source_height']) & ~1):
            return False
        return _verified_encoder_path(raw, backend)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        # A failed process or malformed response keeps this candidate unverified.
        return False


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
        timeout: float = 15.0,
        engine: str | None = None,
        context: GpuProbeContext = GpuProbeContext()) -> tuple[EncoderCapability, ...]:
    """Windows hardware must pass the production capture path and decoded content.

    The native probe checks the monitor's GPU input path or NVENC's production
    cross-adapter transfer. Linux and software retain their FFmpeg initialization checks.
    """
    platform = platform or sys.platform
    try:
        executable = ffmpeg or get_ffmpeg_exe()
    except Exception:
        return ()

    allowed = set(_platform_backend_keys(platform))
    native = _native_signature(executable, engine) if platform == 'win32' else None
    adapter_before = _windows_adapter_signature() if platform == 'win32' else None
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
                if (_gpu_encoder_works(native['path'], key, codec, context, runner, timeout)
                    if platform == 'win32' and key != 'software' and native
                    else (False if platform == 'win32' and key != 'software'
                          else _encoder_works(executable, name, runner=runner, timeout=timeout)))
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
    if platform == 'win32' and (
            adapter_before != _windows_adapter_signature()
            or native != _native_signature(executable, engine)):
        return ()
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


def _windows_display_devices() -> list[tuple[str, str, str, str, int, int]]:
    """Enumerate present display-class devices, including render-only GPUs."""
    import ctypes
    from ctypes import wintypes
    import winreg

    cfg = ctypes.WinDLL('cfgmgr32')
    get_size = cfg.CM_Get_Device_ID_List_SizeW
    get_size.argtypes = [ctypes.POINTER(wintypes.ULONG), wintypes.LPCWSTR, wintypes.ULONG]
    get_size.restype = wintypes.DWORD
    get_list = cfg.CM_Get_Device_ID_ListW
    get_list.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.ULONG, wintypes.ULONG]
    get_list.restype = wintypes.DWORD
    locate = cfg.CM_Locate_DevNodeW
    locate.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.LPCWSTR, wintypes.ULONG]
    locate.restype = wintypes.DWORD
    get_status = cfg.CM_Get_DevNode_Status
    get_status.argtypes = [ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.ULONG),
                           wintypes.DWORD, wintypes.ULONG]
    get_status.restype = wintypes.DWORD

    display_class = '{4d36e968-e325-11ce-bfc1-08002be10318}'
    flags = 0x200 | 0x100  # CM_GETIDLIST_FILTER_CLASS | CM_GETIDLIST_FILTER_PRESENT
    size = wintypes.ULONG()
    if get_size(ctypes.byref(size), display_class, flags) or size.value < 2:
        raise OSError('Cannot size the display device list')
    buffer = ctypes.create_unicode_buffer(size.value)
    if get_list(display_class, buffer, size.value, flags):
        raise OSError('Cannot read the display device list')

    root = r'SYSTEM\CurrentControlSet'
    adapters = []
    for identity in buffer[:].rstrip('\0').split('\0'):
        if not identity:
            continue
        devnode, status, problem = wintypes.DWORD(), wintypes.ULONG(), wintypes.ULONG()
        if (locate(ctypes.byref(devnode), identity, 0)
                or get_status(ctypes.byref(status), ctypes.byref(problem), devnode, 0)):
            raise OSError('Cannot read display device status')
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f'{root}\\Enum\\{identity}') as key:
            driver_key = winreg.QueryValueEx(key, 'Driver')[0]
        if not isinstance(driver_key, str) or not driver_key.casefold().startswith(
                display_class + '\\'):
            raise ValueError('Missing display device driver key')
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f'{root}\\Control\\Class\\{driver_key}') as key:
            version = winreg.QueryValueEx(key, 'DriverVersion')[0]
            inf_path = winreg.QueryValueEx(key, 'InfPath')[0]
        if not all(isinstance(value, str) and value.strip() for value in (version, inf_path)):
            raise ValueError('Missing display device driver identity')
        # Only keep status bits relevant to adapter availability.
        adapters.append((identity.casefold(), driver_key.casefold(), version.strip(),
                         inf_path.casefold(), status.value & 0x408,
                         problem.value if status.value & 0x400 else 0))
    return adapters


def _windows_adapter_signature() -> str | None:
    """Bind cached results to present display adapters, drivers and topology."""
    if sys.platform != 'win32':
        return None
    try:
        adapters = _windows_display_devices()
        if not adapters:
            return None
        from core.windows_monitor import enumerate_windows_monitor_records
        # HMONITOR is a live desktop handle that can be invalidated by display
        # changes; stable device/topology fields define persistent identity.
        topology = sorted(
            (item.monitor_device_path.casefold(), item.gdi_name.casefold(),
             item.x, item.y, item.width, item.height, item.primary)
            for item in enumerate_windows_monitor_records() if item.active)
        if not topology:
            return None
        return json.dumps({'adapters': sorted(adapters), 'topology': topology}, separators=(',', ':'))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        # Without a complete driver/topology identity the persistent cache is bypassed.
        return None


def _ffmpeg_signature(ffmpeg: str, platform: str, engine: str | None = None,
                      context: GpuProbeContext = GpuProbeContext()) -> dict | None:
    path = Path(ffmpeg).resolve()
    stat = path.stat()
    signature = {
        'version': _CACHE_VERSION,
        'platform': platform,
        'ffmpeg': os.path.normcase(str(path)),
        'ffmpeg_size': stat.st_size,
        'ffmpeg_mtime_ns': stat.st_mtime_ns,
        'ffmpeg_sha256': _file_sha256(path),
    }
    if platform == 'win32':
        adapter_signature = _windows_adapter_signature()
        if adapter_signature is None:
            return None
        signature['windows_adapter_signature'] = adapter_signature
        native = _native_signature(ffmpeg, engine)
        if native is None:
            return None
        signature['native_probe'] = native
        signature['gpu_context'] = context.arguments()
    return signature


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
        cache_file: Path | None = None, engine: str | None = None,
        context: GpuProbeContext = GpuProbeContext()) -> tuple[EncoderCapability, ...] | None:
    """Load a device result when the platform and FFmpeg build still match."""
    platform = platform or sys.platform
    try:
        path = cache_file or _cache_path()
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or not isinstance(raw.get('capabilities'), list):
            return None
        executable = ffmpeg or get_ffmpeg_exe()
        expected = _ffmpeg_signature(executable, platform, engine, context)
        if expected is None:
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
        cache_file: Path | None = None, engine: str | None = None,
        context: GpuProbeContext = GpuProbeContext()) -> bool:
    """Persist discovery so normal app starts do not initialize every driver."""
    platform = platform or sys.platform
    try:
        executable = ffmpeg or get_ffmpeg_exe()
        data = _ffmpeg_signature(executable, platform, engine, context)
        if data is None:
            return False
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
