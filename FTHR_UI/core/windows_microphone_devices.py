"""Discover Windows microphone IDs through the capture engine.

Persist the ID the engine opens. Friendly names are display-only, apart
from one-time migration of older settings.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import MappingProxyType
from typing import Callable, Iterable, Mapping


DEVICE_STATE_ACTIVE = 0x00000001


@dataclass(frozen=True)
class WindowsMicrophoneEndpoint:
    endpoint_id: str
    display_name: str
    device_state: int
    is_default: bool

    @property
    def is_active(self) -> bool:
        return bool(self.device_state & DEVICE_STATE_ACTIVE)


class MicrophoneDiscoveryError(RuntimeError):
    """Base class for a bounded microphone inventory operation."""


class MicrophoneDiscoveryTimeout(MicrophoneDiscoveryError):
    """The native helper or legacy inventory exceeded its deadline."""


class MicrophoneDiscoveryCancelled(MicrophoneDiscoveryError):
    """The inventory was superseded or cancelled by the caller."""


@dataclass(frozen=True)
class MicrophoneDiscoveryResult:
    """Immutable result handed from a discovery worker to the Qt thread."""

    native_endpoints: tuple[WindowsMicrophoneEndpoint, ...]
    legacy_indices: Mapping[str, tuple[int, ...]]
    native_error: str | None
    legacy_error: str | None
    generation: int = 0

    def __post_init__(self) -> None:
        # A frozen dataclass does not freeze a contained dict.  Copy it into a
        # read-only mapping so a stale worker result cannot be mutated while a
        # newer result is being applied by the settings page.
        object.__setattr__(
            self, 'legacy_indices',
            MappingProxyType({
                str(name): tuple(int(index) for index in indices)
                for name, indices in self.legacy_indices.items()
            }),
        )


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise MicrophoneDiscoveryCancelled('microphone discovery was cancelled')


def discovery_diagnostic_code(error: BaseException) -> str | None:
    """Map discovery failures to the stable Stage 6 audio taxonomy."""
    if isinstance(error, MicrophoneDiscoveryCancelled):
        return None
    if isinstance(error, MicrophoneDiscoveryTimeout):
        return 'AUDIO_ENDPOINT_SCAN_TIMEOUT'
    return 'AUDIO_ENDPOINT_NOT_FOUND'


def discovery_result_event_fields(
    result: MicrophoneDiscoveryResult,
) -> dict[str, int | bool]:
    """Return bounded inventory counts without endpoint IDs or names."""
    return {
        'generation': result.generation,
        'native_endpoint_count': len(result.native_endpoints),
        'active_native_endpoint_count': sum(
            1 for endpoint in result.native_endpoints if endpoint.is_active),
        'legacy_endpoint_count': len(result.legacy_indices),
        'native_scan_ok': result.native_error is None,
        'legacy_scan_ok': result.legacy_error is None,
    }


def _terminate_and_reap(process: subprocess.Popen[str]) -> None:
    """Kill a timed-out helper and always drain/reap it before returning."""
    try:
        process.kill()
    # The child may have exited between poll and kill; teardown remains safe.
    except OSError:
        pass
    try:
        process.communicate(timeout=2)
    # A second bounded wait is best effort after a driver-owned child closes.
    except (OSError, subprocess.TimeoutExpired):
        # A stuck helper cannot be waited on forever from the UI operation.
        # The normal Windows child is reaped above; this final wait is bounded.
        try:
            process.wait(timeout=2)
        # If the driver ignores termination, this bounded cleanup may expire.
        except (OSError, subprocess.TimeoutExpired):
            pass


def _run_native_inventory_process(
    engine_path: str | Path,
    *,
    timeout: float,
    cancel_event: threading.Event | None,
    popen_factory: Callable[..., subprocess.Popen[str]],
) -> str:
    """Run ``--list-microphones`` with cancellation and bounded reaping."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if sys.platform == 'win32' else 0
    try:
        process = popen_factory(
            [str(engine_path), '--list-microphones'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=flags,
        )
    except OSError as exc:
        raise MicrophoneDiscoveryError(
            f'native microphone discovery failed: {exc}') from exc
    deadline = time.monotonic() + max(0.01, float(timeout))
    try:
        while True:
            try:
                _raise_if_cancelled(cancel_event)
            except MicrophoneDiscoveryCancelled:
                _terminate_and_reap(process)
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_and_reap(process)
                raise MicrophoneDiscoveryTimeout(
                    f'native microphone discovery timed out after {timeout:g}s')
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            # TimeoutExpired is the normal polling signal before the deadline.
            except subprocess.TimeoutExpired:
                continue
            except OSError as exc:
                _terminate_and_reap(process)
                raise MicrophoneDiscoveryError(
                    f'native microphone discovery failed: {exc}') from exc
    except MicrophoneDiscoveryCancelled:
        raise

    if process.returncode != 0:
        detail = (stderr or stdout or 'unknown native error').strip()
        raise MicrophoneDiscoveryError(
            f'native microphone discovery failed: {detail}')
    return stdout


def parse_microphone_inventory(
    payload: str, *, strict: bool = False,
) -> list[WindowsMicrophoneEndpoint]:
    """Parse inventory; strict playback scans reject incomplete/malformed rows."""
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError('native microphone inventory was not valid JSON') from exc
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('native microphone inventory has an unsupported schema')
    if strict and type(data.get('schema_version')) is not int:
        raise ValueError('native microphone inventory has an unsupported schema')
    raw_endpoints = data.get('microphones')
    if not isinstance(raw_endpoints, list):
        raise ValueError('native microphone inventory has no endpoint list')

    result: list[WindowsMicrophoneEndpoint] = []
    seen_ids: set[str] = set()
    for item in raw_endpoints:
        if not isinstance(item, dict):
            if strict:
                raise ValueError('native microphone inventory has an invalid endpoint')
            continue
        endpoint_id = item.get('endpoint_id')
        display_name = item.get('display_name')
        state = item.get('device_state')
        if (not isinstance(endpoint_id, str) or not endpoint_id
                or not isinstance(display_name, str) or not display_name
                or not isinstance(state, int) or endpoint_id in seen_ids):
            if strict:
                raise ValueError('native microphone inventory has an invalid or duplicate endpoint')
            continue
        if strict and (type(state) is not int
                       or ('is_default' in item and type(item['is_default']) is not bool)):
            raise ValueError('native microphone inventory has invalid endpoint state')
        seen_ids.add(endpoint_id)
        result.append(WindowsMicrophoneEndpoint(
            endpoint_id=endpoint_id,
            display_name=display_name,
            device_state=state,
            is_default=bool(item.get('is_default', False)),
        ))
    return result


def list_native_microphones(
    engine_path: str | Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    timeout: float = 5,
    cancel_event: threading.Event | None = None,
) -> list[WindowsMicrophoneEndpoint]:
    """Return native endpoints or raise a bounded, user-presentable error."""
    _raise_if_cancelled(cancel_event)
    if runner is None:
        payload = _run_native_inventory_process(
            engine_path,
            timeout=timeout,
            cancel_event=cancel_event,
            popen_factory=popen_factory,
        )
        return parse_microphone_inventory(payload)
    try:
        result = runner(
            [str(engine_path), '--list-microphones'],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MicrophoneDiscoveryTimeout(
            f'native microphone discovery timed out after {timeout:g}s') from exc
    except OSError as exc:
        raise MicrophoneDiscoveryError(
            f'native microphone discovery failed: {exc}') from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'unknown native error').strip()
        raise MicrophoneDiscoveryError(
            f'native microphone discovery failed: {detail}')
    return parse_microphone_inventory(result.stdout)


def _query_legacy_indices_bounded(
    query_devices: Callable[[], Iterable[Mapping[str, object]]],
    *,
    timeout: float,
    cancel_event: threading.Event | None,
) -> dict[str, tuple[int, ...]]:
    """Run uncancellable PortAudio inventory in a daemon with a bounded wait.

    A stalled device API must not block Qt or the discovery generation.
    """
    _raise_if_cancelled(cancel_event)
    result: list[object] = []
    failure: list[BaseException] = []
    done = threading.Event()

    def _query() -> None:
        try:
            result.extend(query_devices())
        except Exception as exc:  # report third-party errors to caller
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=_query, name='fthr-mic-legacy-scan', daemon=True).start()
    deadline = time.monotonic() + max(0.01, float(timeout))
    while not done.wait(min(0.05, max(0, deadline - time.monotonic()))):
        _raise_if_cancelled(cancel_event)
        if time.monotonic() >= deadline:
            raise MicrophoneDiscoveryTimeout(
                f'legacy microphone discovery timed out after {timeout:g}s')
    _raise_if_cancelled(cancel_event)
    if failure:
        raise MicrophoneDiscoveryError(
            f'legacy microphone discovery failed: {failure[0]}') from failure[0]

    indexed: dict[str, list[int]] = {}
    for index, device in enumerate(result):
        if not isinstance(device, Mapping):
            continue
        try:
            if float(device.get('max_input_channels', 0) or 0) <= 0:
                continue
            name = str(device.get('name') or '')
        # Malformed optional PortAudio metadata is ignored for that device.
        except (TypeError, ValueError):
            continue
        if name:
            indexed.setdefault(name, []).append(index)
    return {name: tuple(indices) for name, indices in indexed.items()}


def discover_microphones(
    engine_path: str | Path | None,
    *,
    legacy_query: Callable[[], Iterable[Mapping[str, object]]] | None = None,
    timeout: float = 5,
    cancel_event: threading.Event | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    generation: int = 0,
) -> MicrophoneDiscoveryResult:
    """Discover native and legacy inputs for one cancellable generation."""
    _raise_if_cancelled(cancel_event)
    native: tuple[WindowsMicrophoneEndpoint, ...] = ()
    native_error: str | None = None
    if engine_path is not None:
        try:
            native = tuple(list_native_microphones(
                engine_path,
                timeout=timeout,
                cancel_event=cancel_event,
                popen_factory=popen_factory,
            ))
        except MicrophoneDiscoveryCancelled:
            raise
        except MicrophoneDiscoveryTimeout:
            raise
        except (MicrophoneDiscoveryError, ValueError) as exc:
            native_error = str(exc)

    _raise_if_cancelled(cancel_event)
    legacy: dict[str, tuple[int, ...]] = {}
    legacy_error: str | None = None
    if legacy_query is not None:
        try:
            legacy = _query_legacy_indices_bounded(
                legacy_query, timeout=timeout, cancel_event=cancel_event)
        except MicrophoneDiscoveryCancelled:
            raise
        except MicrophoneDiscoveryError as exc:
            legacy_error = str(exc)

    return MicrophoneDiscoveryResult(
        native_endpoints=native,
        legacy_indices=legacy,
        native_error=native_error,
        legacy_error=legacy_error,
        generation=generation,
    )


class MicrophoneDiscoveryJob:
    """One background inventory generation owned by the settings page."""

    def __init__(self, engine_path: str | Path | None, *,
                 legacy_query: Callable[[], Iterable[Mapping[str, object]]] | None = None,
                 timeout: float = 5,
                 generation: int = 0,
                 worker: Callable[[threading.Event], MicrophoneDiscoveryResult] | None = None):
        self.engine_path = engine_path
        self.legacy_query = legacy_query
        self.timeout = timeout
        self.generation = generation
        self._worker = worker
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._result: MicrophoneDiscoveryResult | None = None
        self._error: BaseException | None = None
        self._callback: Callable[[MicrophoneDiscoveryResult | None, BaseException | None], None] | None = None

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def result(self) -> MicrophoneDiscoveryResult | None:
        return self._result

    @property
    def error(self) -> BaseException | None:
        return self._error

    def start(self, callback: Callable[[MicrophoneDiscoveryResult | None, BaseException | None], None]) -> None:
        if self._thread is not None:
            raise RuntimeError('microphone discovery job already started')
        self._callback = callback

        def _run() -> None:
            try:
                if self._worker is not None:
                    self._result = self._worker(self._cancel_event)
                else:
                    self._result = discover_microphones(
                        self.engine_path,
                        legacy_query=self.legacy_query,
                        timeout=self.timeout,
                        cancel_event=self._cancel_event,
                        generation=self.generation,
                    )
            except Exception as exc:
                self._error = exc
            finally:
                self._done.set()
                if self._callback is not None:
                    self._callback(self._result, self._error)

        self._thread = threading.Thread(
            target=_run, name=f'fthr-mic-discovery-{self.generation}', daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)


def migrate_legacy_microphone_name(
    legacy_name: str | None,
    endpoints: Iterable[WindowsMicrophoneEndpoint],
) -> str | None:
    """Resolve an old friendly-name setting only when it is unambiguous.

    Returning ``None`` leaves the old selection unresolved; callers must not
    turn that case into a random default-device binding.
    """
    if not legacy_name:
        return None
    matches = [endpoint.endpoint_id for endpoint in endpoints
               if endpoint.is_active and endpoint.display_name == legacy_name]
    return matches[0] if len(matches) == 1 else None
