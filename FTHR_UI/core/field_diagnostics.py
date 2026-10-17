"""Collect bounded diagnostics for user-exported support ZIPs.

Producers enqueue session, timing, error, and health events; a daemon writes
them to disk. Capture/audio loops perform no diagnostic file I/O.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO

from core.diagnostics import LOG_FILE, redact_secret


SCHEMA_VERSION = 1
DEFAULT_ROOT = Path.home() / '.fthr' / 'diagnostics'
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_ENGINE_LOG_BYTES = 2 * 1024 * 1024
MAX_EXPORTED_LOG_BYTES = 1024 * 1024
MAX_EVENT_QUEUE = 2048
MAX_RETAINED_SESSIONS = 5
_MARKER_NAME = 'active-session.json'
_ENGINE_EVENT_PREFIX = 'FTHR_DIAGNOSTIC_EVENT '


class DiagnosticError(str, Enum):
    """Stable internal subsystem errors with observable production boundaries."""

    ENGINE_START_FAILED = 'ENGINE_START_FAILED'
    ENGINE_HANDSHAKE_TIMEOUT = 'ENGINE_HANDSHAKE_TIMEOUT'
    MONITOR_SELECTION_NOT_CONFIGURED = 'MONITOR_SELECTION_NOT_CONFIGURED'
    MONITOR_SELECTION_STALE = 'MONITOR_SELECTION_STALE'
    MONITOR_SELECTION_AMBIGUOUS = 'MONITOR_SELECTION_AMBIGUOUS'
    MONITOR_SELECTION_NO_DISPLAYS = 'MONITOR_SELECTION_NO_DISPLAYS'
    MONITOR_PERSIST_FAILED = 'MONITOR_PERSIST_FAILED'
    CAPTURE_OUTPUT_OPEN_FAILED = 'CAPTURE_OUTPUT_OPEN_FAILED'
    CAPTURE_DXGI_INIT_FAILED = 'CAPTURE_DXGI_INIT_FAILED'
    CAPTURE_WGC_INIT_FAILED = 'CAPTURE_WGC_INIT_FAILED'
    CAPTURE_DXGI_RUNTIME_FAILED = 'CAPTURE_DXGI_RUNTIME_FAILED'
    CAPTURE_WGC_RUNTIME_FAILED = 'CAPTURE_WGC_RUNTIME_FAILED'
    CAPTURE_NO_FRAMES = 'CAPTURE_NO_FRAMES'
    CAPTURE_FRAME_STALLED = 'CAPTURE_FRAME_STALLED'
    ENCODER_ADAPTER_MISMATCH = 'ENCODER_ADAPTER_MISMATCH'
    ENCODER_INIT_FAILED = 'ENCODER_INIT_FAILED'
    ENCODER_NVENC_INIT_FAILED = 'ENCODER_NVENC_INIT_FAILED'
    ENCODER_AMF_INIT_FAILED = 'ENCODER_AMF_INIT_FAILED'
    ENCODER_QSV_INIT_FAILED = 'ENCODER_QSV_INIT_FAILED'
    ENCODER_SUBMIT_FAILED = 'ENCODER_SUBMIT_FAILED'
    ENCODER_OUTPUT_STALLED = 'ENCODER_OUTPUT_STALLED'
    CLIP_SAVE_FAILED = 'CLIP_SAVE_FAILED'
    CLIP_FINALIZE_FAILED = 'CLIP_FINALIZE_FAILED'
    AUDIO_OUTPUT_INIT_FAILED = 'AUDIO_OUTPUT_INIT_FAILED'
    AUDIO_MIC_INIT_FAILED = 'AUDIO_MIC_INIT_FAILED'
    AUDIO_NO_PACKETS = 'AUDIO_NO_PACKETS'
    SYSTEM_AUDIO_INIT_FAILED = 'SYSTEM_AUDIO_INIT_FAILED'
    SYSTEM_AUDIO_NO_PACKETS = 'SYSTEM_AUDIO_NO_PACKETS'
    MIC_INIT_FAILED = 'MIC_INIT_FAILED'
    MIC_NO_PACKETS = 'MIC_NO_PACKETS'
    AUDIO_ENDPOINT_NOT_FOUND = 'AUDIO_ENDPOINT_NOT_FOUND'
    AUDIO_ENDPOINT_SCAN_TIMEOUT = 'AUDIO_ENDPOINT_SCAN_TIMEOUT'
    AUDIO_DEVICE_INVALIDATED = 'AUDIO_DEVICE_INVALIDATED'
    AUDIO_FORMAT_UNSUPPORTED = 'AUDIO_FORMAT_UNSUPPORTED'
    AUDIO_RESAMPLE_FAILED = 'AUDIO_RESAMPLE_FAILED'
    AUDIO_ENCODER_FAILED = 'AUDIO_ENCODER_FAILED'
    AUDIO_PACKET_DISCONTINUITY = 'AUDIO_PACKET_DISCONTINUITY'
    PROCESS_AUDIO_INIT_FAILED = 'PROCESS_AUDIO_INIT_FAILED'
    PROCESS_AUDIO_SOURCE_LIMIT = 'PROCESS_AUDIO_SOURCE_LIMIT'
    PLAYBACK_INIT_FAILED = 'PLAYBACK_INIT_FAILED'
    PLAYBACK_DECODER_FAILED = 'PLAYBACK_DECODER_FAILED'
    PLAYBACK_STALLED = 'PLAYBACK_STALLED'
    EXPORT_INIT_FAILED = 'EXPORT_INIT_FAILED'
    EXPORT_PROCESS_FAILED = 'EXPORT_PROCESS_FAILED'
    EXPORT_STALLED = 'EXPORT_STALLED'
    LIBRARY_SCAN_FAILED = 'LIBRARY_SCAN_FAILED'
    LIBRARY_ROOT_UNAVAILABLE = 'LIBRARY_ROOT_UNAVAILABLE'
    LIBRARY_SCAN_CANCELLED = 'LIBRARY_SCAN_CANCELLED'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _safe_component(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9_.-]+', '-', value).strip('-') or 'unknown'


def _replacement_roots() -> list[tuple[str, str]]:
    roots: list[tuple[str, str]] = []
    candidates = (
        ('%LOCALAPPDATA%', os.environ.get('LOCALAPPDATA')),
        ('%APPDATA%', os.environ.get('APPDATA')),
        ('%USERPROFILE%', os.environ.get('USERPROFILE')),
        ('%TEMP%', os.environ.get('TEMP') or os.environ.get('TMP')),
        ('%USERPROFILE%', str(Path.home())),
    )
    seen: set[str] = set()
    for token, raw in candidates:
        if not raw:
            continue
        normal = os.path.normpath(str(raw))
        key = os.path.normcase(normal)
        if key in seen:
            continue
        seen.add(key)
        roots.append((normal, token))
    roots.sort(key=lambda item: len(item[0]), reverse=True)
    return roots


_WINDOWS_USER_ROOT = re.compile(
    r'(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]Users[\\/][^\\/\s"\']+')


def redact_path(value: Any) -> str:
    """Redact the current profile/application/temp roots in arbitrary text."""
    text = redact_secret(value)
    for root, token in _replacement_roots():
        text = re.sub(re.escape(root), token, text, flags=re.IGNORECASE)
        alternate = root.replace('\\', '/')
        if alternate != root:
            text = re.sub(re.escape(alternate), token, text,
                          flags=re.IGNORECASE)
    return _WINDOWS_USER_ROOT.sub('%USERPROFILE%', text)


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    """Convert an event value to bounded, JSON-safe, redacted data."""
    if depth > 6:
        return '<depth-limit>'
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return redact_path(str(value))[:2048]
    if isinstance(value, str):
        return redact_path(value)[:4096]
    if isinstance(value, Mapping):
        items = list(value.items())[:128]
        return {
            redact_secret(str(key))[:128]: _sanitize(item, depth=depth + 1)
            for key, item in items
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize(item, depth=depth + 1)
                for item in list(value)[:128]]
    return redact_path(repr(value))[:2048]


def bounded_tail(text: str, max_bytes: int = MAX_EXPORTED_LOG_BYTES) -> str:
    """Return a UTF-8-safe, redacted tail no larger than ``max_bytes``."""
    max_bytes = max(0, int(max_bytes))
    clean = redact_path(text)
    encoded = clean.encode('utf-8', errors='replace')
    if len(encoded) <= max_bytes:
        return clean
    prefix = '[earlier diagnostic log content omitted]\n'
    prefix_bytes = prefix.encode('utf-8')
    if max_bytes <= len(prefix_bytes):
        return prefix_bytes[:max_bytes].decode('utf-8', errors='ignore')
    suffix = encoded[-(max_bytes - len(prefix_bytes)):]
    result = prefix + suffix.decode('utf-8', errors='replace')
    while len(result.encode('utf-8', errors='replace')) > max_bytes:
        result = prefix + result[len(prefix) + 1:]
    return result


def _read_bounded_tail(path: Path, max_bytes: int) -> str:
    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
        return bounded_tail(data.decode('utf-8', errors='replace'), max_bytes)
    except OSError as error:
        return f'[diagnostic log unavailable: {type(error).__name__}]\n'


@dataclass(frozen=True)
class StallObservation:
    error: DiagnosticError | None
    reason: str


def classify_capture_stall(*, active: bool, frames_acquired: int,
                           last_acquired_ms: int | None, now_ms: int,
                           threshold_ms: int = 5000) -> StallObservation:
    if not active or last_acquired_ms is None:
        return StallObservation(None, 'capture inactive or timestamp unavailable')
    if now_ms - last_acquired_ms < threshold_ms:
        return StallObservation(None, 'capture progressing')
    code = (DiagnosticError.CAPTURE_NO_FRAMES if frames_acquired == 0
            else DiagnosticError.CAPTURE_FRAME_STALLED)
    return StallObservation(code, f'no acquired frame for {now_ms - last_acquired_ms}ms')


def classify_encoder_stall(*, active: bool, submissions: int,
                           last_submission_ms: int | None,
                           last_output_ms: int | None, now_ms: int,
                           threshold_ms: int = 5000) -> StallObservation:
    if not active or submissions == 0 or last_submission_ms is None:
        return StallObservation(None, 'encoder inactive or no submissions')
    reference = last_output_ms if last_output_ms is not None else last_submission_ms
    if now_ms - reference < threshold_ms:
        return StallObservation(None, 'encoder output progressing')
    return StallObservation(
        DiagnosticError.ENCODER_OUTPUT_STALLED,
        f'encoder submitted frames but produced no output for {now_ms - reference}ms')


def unavailable(reason: str) -> str:
    reason = _safe_component(reason).replace('-', '_')
    return f'unavailable:{reason}'


def build_adapter_chain(actual: Mapping[str, Any] | None,
                        *, unavailable_reason: str = 'not_reported') -> dict[str, Any]:
    """Return the complete monitor-to-encoder chain without silent omissions."""
    source = dict(actual or {})
    keys = (
        'monitor_id', 'windows_display', 'dxgi_output', 'monitor_adapter',
        'monitor_adapter_luid', 'capture_d3d11_device', 'capture_device_luid',
        'encoder_adapter', 'encoder_adapter_luid', 'capture_backend',
        'encoder_backend', 'codec',
    )
    result: dict[str, Any] = {}
    for key in keys:
        value = source.get(key)
        result[key] = value if value not in (None, '') else unavailable(unavailable_reason)
    return _sanitize(result)


def _cpu_model() -> str:
    model = platform.processor().strip()
    if model:
        return model
    return os.environ.get('PROCESSOR_IDENTIFIER', '').strip() or 'unavailable'


def _memory_snapshot() -> dict[str, int | str]:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ('length', ctypes.c_ulong), ('memory_load', ctypes.c_ulong),
            ('total_physical', ctypes.c_ulonglong),
            ('available_physical', ctypes.c_ulonglong),
            ('total_page_file', ctypes.c_ulonglong),
            ('available_page_file', ctypes.c_ulonglong),
            ('total_virtual', ctypes.c_ulonglong),
            ('available_virtual', ctypes.c_ulonglong),
            ('available_extended_virtual', ctypes.c_ulonglong),
        ]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {
                'total_physical_bytes': int(status.total_physical),
                'available_physical_bytes': int(status.available_physical),
            }
    except (AttributeError, OSError):
        # Older/non-Windows runtimes may not expose this ctypes API; the
        # portable sysconf probe below remains available.
        pass
    try:
        page_size = os.sysconf('SC_PAGE_SIZE')
        return {
            'total_physical_bytes': int(page_size * os.sysconf('SC_PHYS_PAGES')),
            'available_physical_bytes': int(page_size * os.sysconf('SC_AVPHYS_PAGES')),
        }
    except (AttributeError, OSError, ValueError):
        return {
            'total_physical_bytes': 'unavailable',
            'available_physical_bytes': 'unavailable',
        }


def _windows_version() -> dict[str, Any]:
    result: dict[str, Any] = {
        'system': platform.system(),
        'release': platform.release(),
        'version': platform.version(),
        'build': platform.version().split('.')[-1] if platform.version() else 'unavailable',
        'edition': 'unavailable',
        'architecture': platform.machine() or platform.architecture()[0],
    }
    try:
        import winreg
        path = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion'
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as key:
            for source, target in (
                    ('ProductName', 'edition'), ('DisplayVersion', 'display_version'),
                    ('CurrentBuildNumber', 'build'), ('UBR', 'update_build_revision')):
                try:
                    result[target] = winreg.QueryValueEx(key, source)[0]
                except OSError:
                    # Windows legitimately omits some version values; retain
                    # the explicit unavailable/default value for that field.
                    pass
    except (ImportError, OSError):
        # Registry access is best-effort support context and must never prevent
        # FTHR startup; platform.version fields remain in the snapshot.
        pass
    return result


def _default_command_runner(command: list[str], timeout: float) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=timeout, check=False,
        creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0),
    )
    if completed.returncode != 0:
        raise OSError(f'hardware query exited with code {completed.returncode}')
    return completed.stdout


def _gpu_vendor(name: str) -> str:
    lowered = name.lower()
    if 'nvidia' in lowered:
        return 'NVIDIA'
    if 'amd' in lowered or 'radeon' in lowered:
        return 'AMD'
    if 'intel' in lowered:
        return 'Intel'
    return 'unknown'


def collect_gpu_adapters(
        command_runner: Callable[[list[str], float], str] | None = None,
        *, platform_name: str | None = None) -> list[dict[str, Any]]:
    """Collect bounded public adapter properties; never PNP/serial identifiers."""
    current = platform_name or sys.platform
    if current != 'win32':
        return []
    runner = command_runner or _default_command_runner
    script = (
        "@(Get-CimInstance Win32_VideoController | Select-Object "
        "Name,AdapterRAM,DriverVersion) | ConvertTo-Json -Compress")
    try:
        raw = runner(['powershell', '-NoProfile', '-NonInteractive',
                      '-Command', script], 5.0)
        decoded = json.loads(raw or '[]')
        rows = decoded if isinstance(decoded, list) else [decoded]
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return []
    adapters: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:16]):
        if not isinstance(row, Mapping):
            continue
        name = str(row.get('Name') or 'unknown')
        try:
            vram = int(row.get('AdapterRAM')) if row.get('AdapterRAM') is not None else None
        except (TypeError, ValueError):
            vram = None
        adapters.append({
            'index': index,
            'name': name[:256],
            'vendor': _gpu_vendor(name),
            # Win32_VideoController.AdapterRAM is a 32-bit field and commonly
            # truncates modern GPUs above 4 GiB. Preserve it as reported data,
            # but never present it as a trustworthy physical VRAM capacity.
            'vram_bytes': unavailable('wmi_adapterram_not_reliable'),
            'adapter_ram_reported_bytes': (
                vram if vram is not None else 'unavailable'),
            'driver_version': str(row.get('DriverVersion') or 'unavailable')[:128],
            'adapter_luid': unavailable('engine_mapping_required'),
        })
    return adapters


def collect_hardware_snapshot(
        command_runner: Callable[[list[str], float], str] | None = None,
        *, platform_name: str | None = None) -> dict[str, Any]:
    return _sanitize({
        'os': _windows_version(),
        'cpu': {
            'model': _cpu_model(),
            'logical_processor_count': os.cpu_count() or 'unavailable',
        },
        'memory': _memory_snapshot(),
        'gpu_adapters': collect_gpu_adapters(
            command_runner, platform_name=platform_name),
    })


def process_memory_bytes() -> int | str:
    """Best-effort resident/private working set for bounded library snapshots."""
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ('cb', ctypes.c_ulong), ('page_fault_count', ctypes.c_ulong),
            ('peak_working_set_size', ctypes.c_size_t),
            ('working_set_size', ctypes.c_size_t),
            ('quota_peak_paged_pool_usage', ctypes.c_size_t),
            ('quota_paged_pool_usage', ctypes.c_size_t),
            ('quota_peak_non_paged_pool_usage', ctypes.c_size_t),
            ('quota_non_paged_pool_usage', ctypes.c_size_t),
            ('pagefile_usage', ctypes.c_size_t),
            ('peak_pagefile_usage', ctypes.c_size_t),
        ]
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    try:
        kernel32 = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ProcessMemoryCounters),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        if psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb):
            return int(counters.working_set_size)
    except (AttributeError, OSError):
        # psapi is unavailable outside a compatible Windows runtime; the
        # portable resource fallback below still provides best effort.
        pass
    try:
        import resource
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value if sys.platform == 'darwin' else value * 1024
    except (ImportError, OSError, ValueError):
        return 'unavailable'


def qt_display_snapshot(app: Any, window: Any = None) -> list[dict[str, Any]]:
    """Collect bounded logical display facts exposed by Qt.

    Qt geometry is logical/DIP geometry.  The physical size is explicitly an
    estimate based on the screen DPR; it is not used for capture selection.
    """
    if app is None:
        return []
    try:
        screens = list(app.screens())
        primary = app.primaryScreen()
    except Exception:
        # Qt may be tearing down while a report is exported. An empty display
        # snapshot is explicit and safer than making export fail at shutdown.
        return []
    result = []
    for index, screen in enumerate(screens[:32]):
        try:
            geometry = screen.geometry()
            available_getter = getattr(screen, 'availableGeometry', None)
            available = (available_getter() if callable(available_getter)
                         else geometry)
            name = str(screen.name() or f'display-{index}')
            dpr = round(float(screen.devicePixelRatio()), 3)
            logical_dpi = round(float(screen.logicalDotsPerInch()), 3)
            result.append({
                'index': index,
                'name': name[:128],
                'resolution': {
                    'width': int(geometry.width()), 'height': int(geometry.height()),
                },
                'desktop_coordinates': {
                    'x': int(geometry.x()), 'y': int(geometry.y()),
                },
                'logical_geometry': {
                    'x': int(geometry.x()), 'y': int(geometry.y()),
                    'width': int(geometry.width()), 'height': int(geometry.height()),
                },
                'logical_available_geometry': {
                    'x': int(available.x()), 'y': int(available.y()),
                    'width': int(available.width()), 'height': int(available.height()),
                },
                'refresh_hz': round(float(screen.refreshRate()), 3),
                'primary': screen is primary,
                'device_pixel_ratio': dpr,
                'logical_dpi': logical_dpi,
                'physical_size_estimate': {
                    'width': round(float(geometry.width()) * dpr),
                    'height': round(float(geometry.height()) * dpr),
                    'basis': 'logical_geometry × device_pixel_ratio estimate',
                },
                'hdr': unavailable('not_exposed_by_qt'),
                'owning_adapter': unavailable('engine_mapping_required'),
            })
        except Exception as error:
            result.append({
                'index': index,
                'status': unavailable(type(error).__name__.lower()),
            })
    # Awareness is a window/process property rather than a screen property.
    # Keep it on each display record for compatibility with simple JSONL
    # consumers, but only query it when a realized window was supplied.
    if window is not None:
        try:
            from core.windows_dpi import dpi_awareness_snapshot
            awareness = dpi_awareness_snapshot(window)
            for display in result:
                if isinstance(display, dict):
                    display['windows_dpi_awareness'] = awareness
        except Exception:
            # Diagnostic enrichment is best-effort and must never prevent an
            # explicitly requested support bundle from being exported.
            pass
    return _sanitize(result)


class EngineLogCapture:
    """Drain a child process pipe into a bounded rotating session log."""

    def __init__(self, path: Path, session: 'DiagnosticSession | None' = None,
                 *, max_bytes: int = MAX_ENGINE_LOG_BYTES,
                 startup_bytes: int = 256 * 1024):
        self.path = Path(path)
        self._session = session
        self._max_bytes = max_bytes
        self._startup_bytes = startup_bytes
        self._startup = deque()
        self._startup_size = 0
        self._stream: TextIO | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def attach(self, stream: TextIO | None) -> None:
        if stream is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = stream
        self._thread = threading.Thread(
            target=self._reader, name='fthr-engine-diagnostics', daemon=True)
        self._thread.start()

    def _append_startup(self, line: str) -> None:
        encoded_size = len(line.encode('utf-8', errors='replace'))
        with self._lock:
            self._startup.append(line)
            self._startup_size += encoded_size
            while self._startup and self._startup_size > self._startup_bytes:
                removed = self._startup.popleft()
                self._startup_size -= len(removed.encode('utf-8', errors='replace'))

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            current = self.path.stat().st_size if self.path.exists() else 0
            if current + incoming <= self._max_bytes:
                return
            previous = self.path.with_suffix('.log.old')
            if previous.exists():
                previous.unlink()
            self.path.replace(previous)
        except OSError:
            # Rotation is best effort: inability to rotate must not block or
            # terminate the engine-output drain thread.
            pass

    def _reader(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ''):
                self._append_startup(line)
                log_line = bounded_tail(redact_path(line), self._max_bytes)
                encoded_size = len(log_line.encode('utf-8', errors='replace'))
                self._rotate_if_needed(encoded_size)
                try:
                    with self.path.open(
                            'a', encoding='utf-8', errors='replace', newline='') as handle:
                        handle.write(log_line)
                except OSError:
                    # The engine pipe must keep draining even if diagnostic
                    # storage becomes unavailable mid-session.
                    pass
                stripped = line.strip()
                if stripped.startswith(_ENGINE_EVENT_PREFIX) and self._session is not None:
                    try:
                        payload = json.loads(stripped[len(_ENGINE_EVENT_PREFIX):])
                        if isinstance(payload, Mapping):
                            self._session.emit_native(payload)
                    except (TypeError, ValueError):
                        self._session.emit(
                            'engine', 'native_event_parse_failed',
                            error=DiagnosticError.ENGINE_START_FAILED,
                            detail='Malformed native diagnostic event')
        except (OSError, ValueError):
            # A closing/invalid pipe ends this best-effort drain; engine process
            # lifecycle diagnostics are emitted by the UI separately.
            pass

    def startup_text(self) -> str:
        with self._lock:
            return ''.join(self._startup)

    def close(self, timeout: float = 1.0) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                # The child process may already have closed its inherited pipe.
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None


class DiagnosticSession:
    """One bounded diagnostic session correlated across all FTHR subsystems."""

    def __init__(self, app_version: str, *, root: Path = DEFAULT_ROOT,
                 session_id: str | None = None,
                 wall_clock: Callable[[], str] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic,
                 max_event_bytes: int = MAX_EVENT_BYTES,
                 retained_sessions: int = MAX_RETAINED_SESSIONS):
        self.session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
        self.short_id = self.session_id.split('-')[0]
        self.app_version = str(app_version)
        self.root = Path(root)
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._start_monotonic = monotonic()
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        self.session_dir = self.root / f'session-{timestamp}-{self.short_id}'
        self.events_path = self.session_dir / 'events.jsonl'
        self.engine_log_path = self.session_dir / 'engine.log'
        self._marker_path = self.root / _MARKER_NAME
        self._max_event_bytes = int(max_event_bytes)
        self._retained_sessions = max(1, int(retained_sessions))
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue(MAX_EVENT_QUEUE)
        self._writer_thread: threading.Thread | None = None
        self._closed = False
        self._event_bytes = 0
        self._dropped_events = 0
        self._summary_lock = threading.RLock()
        self._summary: dict[str, Any] = {
            'schema_version': SCHEMA_VERSION,
            'session_id': self.session_id,
            'app_version': self.app_version,
            'started_utc': self._wall_clock(),
            'status': 'running',
            'requested_configuration': {},
            'actual_configuration': {},
            'adapter_topology': build_adapter_chain(None),
            'hardware': {},
            'displays': [],
            'health': {},
            'timings': {},
            'privacy': {
                'paths_redacted': True,
                'credentials_redacted': True,
                'media_included': False,
                'automatic_upload': False,
            },
        }

    def start(self) -> 'DiagnosticSession':
        self.root.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=False)
        previous = self._read_previous_marker()
        self._write_marker()
        self._enforce_retention()
        self._writer_thread = threading.Thread(
            target=self._writer, name='fthr-diagnostic-writer', daemon=True)
        self._writer_thread.start()
        self.emit('application', 'session_started', app_version=self.app_version)
        if previous:
            self.emit(
                'application', 'previous_session_unclean',
                previous_session_id=previous.get('session_id', 'unavailable'),
                previous_started_utc=previous.get('started_utc', 'unavailable'))
            self.update_summary('previous_session', {
                'status': 'did_not_shut_down_cleanly',
                'session_id': previous.get('session_id', 'unavailable'),
            })
        return self

    def _read_previous_marker(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self._marker_path.read_text(encoding='utf-8'))
            if isinstance(value, dict) and value.get('session_id') != self.session_id:
                return value
        except (OSError, ValueError, TypeError):
            # Missing/corrupt marker means there is no trustworthy prior-session
            # evidence, so do not infer an abnormal exit.
            pass
        return None

    def _write_marker(self) -> None:
        payload = {
            'session_id': self.session_id,
            'started_utc': self._summary['started_utc'],
            'pid': os.getpid(),
        }
        temporary = self._marker_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding='utf-8')
        os.replace(temporary, self._marker_path)

    def _enforce_retention(self) -> None:
        try:
            resolved_root = self.root.resolve()
            directories = sorted(
                (path for path in self.root.glob('session-*') if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in directories[self._retained_sessions:]:
                resolved = path.resolve()
                if resolved.parent == resolved_root and resolved.name.startswith('session-'):
                    shutil.rmtree(resolved)
        except OSError:
            # Retention is maintenance only. A locked old session must not
            # prevent the current session from collecting diagnostics.
            pass

    def emit(self, subsystem: str, event: str, *, state: str | None = None,
             error: DiagnosticError | str | None = None, **fields: Any) -> bool:
        if self._closed:
            return False
        record: dict[str, Any] = {
            'schema_version': SCHEMA_VERSION,
            'timestamp_utc': self._wall_clock(),
            'monotonic_ms': round((self._monotonic() - self._start_monotonic) * 1000),
            'session_id': self.session_id,
            'subsystem': str(subsystem),
            'event': str(event),
        }
        if state is not None:
            record['state'] = str(state)
        if error is not None:
            record['error_code'] = error.value if isinstance(error, Enum) else str(error)
        if fields:
            record['fields'] = fields
        record = _sanitize(record)
        try:
            self._events.put_nowait(record)
            return True
        except queue.Full:
            self._dropped_events += 1
            return False

    def emit_native(self, payload: Mapping[str, Any]) -> bool:
        fields = dict(payload)
        subsystem = str(fields.pop('subsystem', 'engine'))
        event = str(fields.pop('event', 'native_event'))
        state = fields.pop('state', None)
        error = fields.pop('error_code', None)
        if event == 'adapter_topology_resolved':
            self.update_summary('adapter_topology', build_adapter_chain(fields))
            self.merge_summary(
                'actual_configuration',
                resolved_monitor=fields.get('monitor_id', 'unavailable:not_reported'),
                resolved_output=fields.get('windows_display', 'unavailable:not_reported'),
                capture_backend=fields.get('capture_backend', 'unavailable:not_reported'),
                actual_codec=fields.get('codec', 'unavailable:not_reported'),
                actual_encoder=fields.get('encoder_backend', 'unavailable:not_reported'),
                capture_adapter=fields.get('monitor_adapter', 'unavailable:not_reported'),
                encoder_adapter=fields.get('encoder_adapter', 'unavailable:not_reported'),
                capture_dimensions={
                    'width': fields.get('capture_width', 'unavailable:not_reported'),
                    'height': fields.get('capture_height', 'unavailable:not_reported'),
                },
                encoder_dimensions={
                    'width': fields.get('encoder_width', 'unavailable:not_reported'),
                    'height': fields.get('encoder_height', 'unavailable:not_reported'),
                },
            )
        elif event == 'capture_health_snapshot':
            self.update_summary('native_capture_health', fields)
        elif event == 'audio_health_snapshot':
            source = str(fields.get('source', 'unknown_source'))
            self.merge_summary('audio_health', **{source: fields})
        return self.emit(subsystem, event, state=state, error=error, **fields)

    def _writer(self) -> None:
        try:
            with self.events_path.open(
                    'a', encoding='utf-8', errors='replace', newline='') as handle:
                while True:
                    record = self._events.get()
                    try:
                        if record is None:
                            return
                        line = json.dumps(record, ensure_ascii=False,
                                          separators=(',', ':'), sort_keys=True) + '\n'
                        size = len(line.encode('utf-8', errors='replace'))
                        if self._event_bytes + size <= self._max_event_bytes:
                            handle.write(line)
                            handle.flush()
                            self._event_bytes += size
                        else:
                            self._dropped_events += 1
                    finally:
                        self._events.task_done()
        except OSError:
            # If storage fails, drain the queue below so producers never block
            # capture/UI work waiting on the diagnostics writer.
            while True:
                try:
                    record = self._events.get_nowait()
                except queue.Empty:
                    # The failed writer has drained all outstanding work.
                    return
                self._events.task_done()
                if record is None:
                    return

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while self._events.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._events.unfinished_tasks == 0

    def update_summary(self, section: str, value: Any) -> None:
        with self._summary_lock:
            self._summary[str(section)] = _sanitize(value)

    def merge_summary(self, section: str, **values: Any) -> None:
        with self._summary_lock:
            current = self._summary.setdefault(str(section), {})
            if not isinstance(current, dict):
                current = {}
                self._summary[str(section)] = current
            current.update(_sanitize(values))

    def summary(self) -> dict[str, Any]:
        with self._summary_lock:
            result = json.loads(json.dumps(self._summary))
        result['event_bytes'] = self._event_bytes
        result['dropped_events'] = self._dropped_events
        result['generated_utc'] = self._wall_clock()
        return _sanitize(result)

    def collect_hardware_async(self, collector: Callable[[], dict[str, Any]] =
                               collect_hardware_snapshot) -> None:
        def work():
            try:
                snapshot = collector()
                self.update_summary('hardware', snapshot)
                self.emit('hardware', 'snapshot_collected',
                          adapter_count=len(snapshot.get('gpu_adapters', [])))
            except Exception as error:
                self.emit('hardware', 'snapshot_failed',
                          detail=f'{type(error).__name__}: {error}')
        threading.Thread(target=work, name='fthr-hardware-snapshot',
                         daemon=True).start()

    def export_zip(self, destination: Path, *, ui_log_path: Path = LOG_FILE,
                   displays: Iterable[Mapping[str, Any]] | None = None) -> Path:
        if displays is not None:
            self.update_summary('displays', list(displays))
        self.flush()
        destination = Path(destination)
        if destination.suffix.lower() != '.zip':
            destination = destination.with_suffix('.zip')
        destination.parent.mkdir(parents=True, exist_ok=True)
        summary = self.summary()
        summary['export'] = {
            'created_utc': self._wall_clock(),
            'files': [
                'diagnostic-summary.json', 'events.jsonl', 'ui.log',
                'engine.log', 'README.txt',
            ],
        }
        events = _read_bounded_tail(self.events_path, self._max_event_bytes)
        ui_log = _read_bounded_tail(Path(ui_log_path), MAX_EXPORTED_LOG_BYTES)
        engine_log = _read_bounded_tail(
            self.engine_log_path, MAX_EXPORTED_LOG_BYTES)
        readme = (
            'FTHR Clips Diagnostic Report\n'
            '============================\n\n'
            'This ZIP was explicitly exported by the user for alpha support.\n'
            'It contains bounded diagnostic events and logs only. It does not\n'
            'contain clips, screenshots, audio, credentials, browser data, or\n'
            'an arbitrary filesystem listing. Paths and known secrets are\n'
            'redacted. The report is never uploaded automatically.\n')
        temporary = destination.with_suffix(destination.suffix + '.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    'diagnostic-summary.json',
                    json.dumps(summary, indent=2, ensure_ascii=False,
                               sort_keys=True) + '\n')
                archive.writestr('events.jsonl', events)
                archive.writestr('ui.log', ui_log)
                archive.writestr('engine.log', engine_log)
                archive.writestr('README.txt', readme)
            os.replace(temporary, destination)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                # os.replace already consumed the temporary archive on success.
                pass
        self.emit('diagnostics', 'report_exported',
                  output_size_bytes=destination.stat().st_size)
        return destination

    def close(self, *, clean: bool = True) -> None:
        if self._closed:
            return
        self.emit('application', 'session_ended', state='clean' if clean else 'abnormal')
        self.merge_summary(
            'session_end', clean=clean,
            elapsed_ms=round((self._monotonic() - self._start_monotonic) * 1000))
        with self._summary_lock:
            self._summary['status'] = 'closed_cleanly' if clean else 'abnormal'
        self.flush()
        self._closed = True
        try:
            self._events.put(None, timeout=0.5)
        except queue.Full:
            # Shutdown stays bounded if a failed writer left the queue full;
            # the daemon thread cannot hold application exit hostage.
            pass
        thread = self._writer_thread
        if thread is not None:
            thread.join(timeout=2.0)
        if clean:
            try:
                marker = json.loads(self._marker_path.read_text(encoding='utf-8'))
                if marker.get('session_id') == self.session_id:
                    self._marker_path.unlink()
            except (OSError, ValueError, TypeError):
                # A missing/replaced marker belongs to no confirmed live
                # session and must not be deleted speculatively.
                pass


_session_lock = threading.Lock()
_session: DiagnosticSession | None = None
_playback_lock = threading.Lock()
_active_playback_instances = 0


def start_diagnostic_session(app_version: str, *, root: Path = DEFAULT_ROOT,
                             **kwargs: Any) -> DiagnosticSession:
    global _session
    with _session_lock:
        if _session is None or _session._closed:
            _session = DiagnosticSession(
                app_version, root=root, **kwargs).start()
        return _session


def get_diagnostic_session() -> DiagnosticSession | None:
    return _session


def emit_event(subsystem: str, event: str, *, state: str | None = None,
               error: DiagnosticError | str | None = None, **fields: Any) -> bool:
    session = get_diagnostic_session()
    return bool(session and session.emit(
        subsystem, event, state=state, error=error, **fields))


def playback_instance_created() -> int:
    global _active_playback_instances
    with _playback_lock:
        _active_playback_instances += 1
        count = _active_playback_instances
    emit_event('playback', 'player_created', active_instances=count)
    session = get_diagnostic_session()
    if session is not None:
        session.merge_summary('playback', active_instances=count)
    return count


def playback_instance_destroyed() -> int:
    global _active_playback_instances
    with _playback_lock:
        _active_playback_instances = max(0, _active_playback_instances - 1)
        count = _active_playback_instances
    emit_event('playback', 'player_destroyed', active_instances=count)
    session = get_diagnostic_session()
    if session is not None:
        session.merge_summary('playback', active_instances=count)
    return count


def end_diagnostic_session(*, clean: bool = True) -> None:
    global _session
    with _session_lock:
        session = _session
        _session = None
    if session is not None:
        session.close(clean=clean)


def reset_for_tests() -> None:
    global _session, _active_playback_instances
    with _session_lock:
        session = _session
        _session = None
    if session is not None:
        session.close(clean=True)
    with _playback_lock:
        _active_playback_instances = 0


def session_monitor_token(value: str) -> str:
    """Session-scoped monitor token that cannot be correlated across exports."""
    session = get_diagnostic_session()
    salt = session.session_id if session else str(uuid.uuid4())
    digest = hashlib.sha256(f'{salt}:{value}'.encode('utf-8')).hexdigest()[:12]
    return f'monitor-{digest}'


__all__ = [
    'DEFAULT_ROOT', 'DiagnosticError', 'DiagnosticSession', 'EngineLogCapture',
    'StallObservation', 'bounded_tail', 'build_adapter_chain',
    'classify_capture_stall', 'classify_encoder_stall',
    'collect_gpu_adapters', 'collect_hardware_snapshot',
    'emit_event', 'end_diagnostic_session',
    'get_diagnostic_session', 'process_memory_bytes', 'qt_display_snapshot',
    'playback_instance_created', 'playback_instance_destroyed',
    'redact_path', 'reset_for_tests', 'session_monitor_token',
    'start_diagnostic_session', 'unavailable',
]
