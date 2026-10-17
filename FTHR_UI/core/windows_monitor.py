"""Stable Windows monitor identities for the capture-source UI (AUDIT-048)."""

from __future__ import annotations

import ctypes
import re
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


_DISPLAY_DEVICE_ACTIVE = 0x00000001
_DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
_DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
_EDD_GET_DEVICE_INTERFACE_NAME = 0x00000001


@dataclass(frozen=True)
class DisplayDeviceRecord:
    gdi_name: str
    friendly_name: str
    monitor_device_path: str
    active: bool
    primary: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    hmonitor: int = 0


@dataclass(frozen=True)
class MonitorChoice:
    gdi_name: str
    friendly_name: str
    device_path: str
    primary: bool
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    hmonitor: int = 0


class MonitorSelectionState(str, Enum):
    """Outcome of resolving the persisted monitor against current topology."""

    NOT_CONFIGURED = 'NOT_CONFIGURED'
    RESOLVED = 'RESOLVED'
    RECOVERED = 'RECOVERED'
    STALE = 'STALE'
    AMBIGUOUS = 'AMBIGUOUS'
    NO_DISPLAYS = 'NO_DISPLAYS'
    PERSIST_FAILED = 'PERSIST_FAILED'


@dataclass(frozen=True)
class MonitorSelectionResult:
    """Bounded, testable record of one monitor-selection decision.

    ``path`` holds a persistent device path or an explicit live desktop identity.  In particular, diagnostic sentinels and GDI names never
    become an engine argument through this object.
    """

    state: MonitorSelectionState
    code: str
    reason: str
    previous_state: MonitorSelectionState
    previous_path: str
    path: str = ''
    choice: MonitorChoice | None = None
    persist_required: bool = False

    @property
    def resolved(self) -> bool:
        return bool(self.path and self.choice is not None)

    def as_dict(self) -> dict[str, object]:
        choice = self.choice
        return {
            'state': self.state.value,
            'code': self.code,
            'reason': self.reason,
            'previous_state': self.previous_state.value,
            'previous_path': self.previous_path or 'unavailable:not_configured',
            'canonical_path': self.path or 'unavailable:not_resolved',
            'gdi_name': (getattr(choice, 'gdi_name', 'unavailable:not_resolved')
                         if choice else 'unavailable:not_resolved'),
            'primary': (bool(getattr(choice, 'primary', False))
                        if choice else None),
            'topology_resolution': 'resolved' if self.resolved else 'unresolved',
            'persist_required': self.persist_required,
        }


class MonitorSelectionError(ValueError):
    """Raised when startup cannot produce a safe native monitor argument."""

    def __init__(self, result: MonitorSelectionResult):
        self.result = result
        super().__init__(f'{result.code}: {result.reason}')


def normalize_monitor_device_path(device_path: str) -> str:
    """Match the engine's case/slash-insensitive persistent identity."""
    if device_path is None:
        return ''
    return device_path.strip().replace('/', '\\').lower()


def is_monitor_selection_sentinel(device_path: str | None) -> bool:
    """Return whether a value is a diagnostic/unresolved selection marker."""
    return normalize_monitor_device_path(device_path or '').startswith(
        'unavailable:')


def is_canonical_monitor_device_path(device_path: str | None) -> bool:
    """Recognize the persistent Windows display-interface form."""
    normalized = normalize_monitor_device_path(device_path or '')
    return normalized.startswith(r'\\?\display#') and len(normalized) > len(
        r'\\?\display#')


def desktop_monitor_identity(hmonitor: int, gdi_name: str) -> str:
    name = normalize_monitor_device_path(gdi_name).removeprefix('\\\\.\\')
    if hmonitor <= 0 or not re.fullmatch(r'display[0-9]+', name):
        return ''
    return f'fthr-monitor:{hmonitor:x}:{name}'


def is_monitor_capture_identity(device_path: str | None) -> bool:
    value = normalize_monitor_device_path(device_path or '')
    return is_canonical_monitor_device_path(value) or bool(
        re.fullmatch(r'fthr-monitor:[0-9a-f]+:display[0-9]+', value)
        and int(value.split(':')[1], 16) > 0)


def _choice_matches(choice: MonitorChoice, identity: str) -> bool:
    return normalize_monitor_device_path(choice.device_path) == identity or (
        bool(identity) and desktop_monitor_identity(choice.hmonitor, choice.gdi_name) == identity)


def is_valid_monitor_device_path(
        device_path: str,
        choices: Iterable[MonitorChoice] | None = None) -> bool:
    """True when the saved device path still matches one of the active monitors."""
    normalized = normalize_monitor_device_path(device_path)
    if not is_monitor_capture_identity(normalized):
        return False
    available = list(enumerate_windows_monitors() if choices is None else choices)
    return sum(_choice_matches(choice, normalized) for choice in available) == 1


def build_monitor_choices(records: Iterable[DisplayDeviceRecord]) -> list[MonitorChoice]:
    """Return active monitors once each, keyed by stable device interface path."""
    choices: list[MonitorChoice] = []
    seen: set[str] = set()
    for record in records:
        device_path = normalize_monitor_device_path(record.monitor_device_path)
        if not is_canonical_monitor_device_path(device_path):
            device_path = desktop_monitor_identity(record.hmonitor, record.gdi_name)
        if not record.active or not is_monitor_capture_identity(device_path) \
                or device_path in seen:
            continue
        seen.add(device_path)
        choices.append(MonitorChoice(
            gdi_name=record.gdi_name,
            friendly_name=record.friendly_name or record.gdi_name,
            device_path=device_path,
            primary=record.primary,
            x=record.x,
            y=record.y,
            width=record.width,
            height=record.height,
            hmonitor=record.hmonitor,
        ))
    return choices


class _DisplayDeviceW(ctypes.Structure):
    _fields_ = [
        ('cb', wintypes.DWORD),
        ('DeviceName', wintypes.WCHAR * 32),
        ('DeviceString', wintypes.WCHAR * 128),
        ('StateFlags', wintypes.DWORD),
        ('DeviceID', wintypes.WCHAR * 128),
        ('DeviceKey', wintypes.WCHAR * 128),
    ]


class _MonitorInfoExW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('rcMonitor', wintypes.RECT),
        ('rcWork', wintypes.RECT),
        ('dwFlags', wintypes.DWORD),
        ('szDevice', wintypes.WCHAR * 32),
    ]


def _enumerate_monitor_snapshot() -> dict[str, tuple[int, int, int, int, int, bool]]:

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HANDLE,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    get_monitor_info = user32.GetMonitorInfoW
    get_monitor_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MonitorInfoExW)]
    get_monitor_info.restype = wintypes.BOOL
    bounds: dict[str, tuple[int, int, int, int, int, bool]] = {}

    @callback_type
    def collect(monitor, _dc, _rect, _data):
        info = _MonitorInfoExW()
        info.cbSize = ctypes.sizeof(info)
        if get_monitor_info(monitor, ctypes.byref(info)):
            rect = info.rcMonitor
            bounds[str(info.szDevice).casefold()] = (
                rect.left,
                rect.top,
                rect.right - rect.left,
                rect.bottom - rect.top,
                int(monitor),
                bool(info.dwFlags & _DISPLAY_DEVICE_PRIMARY_DEVICE),
            )
        return True

    enum_display_monitors = user32.EnumDisplayMonitors
    enum_display_monitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        callback_type,
        wintypes.LPARAM,
    ]
    enum_display_monitors.restype = wintypes.BOOL
    if not enum_display_monitors(None, None, collect, 0):
        return {}
    return bounds


def _enumerate_monitor_bounds() -> dict[str, tuple[int, int, int, int]]:
    return {name: value[:4] for name, value in _enumerate_monitor_snapshot().items()}


def _active_monitor_children(
        children: Iterable[_DisplayDeviceW],
        *, adapter_in_monitor_snapshot: bool) -> list[_DisplayDeviceW]:
    """Choose driver-reported active children without guessing topology.

    A few drivers omit ACTIVE on the only child even though its adapter is
    visible to EnumDisplayMonitors.  The single-child rule is the narrow
    inference allowed for that case; multiple inactive children remain
    unresolved.
    """
    children = list(children)
    active = [
        child for child in children
        if bool(child.StateFlags & _DISPLAY_DEVICE_ACTIVE)
    ]
    if active:
        return active
    if (adapter_in_monitor_snapshot and len(children) == 1
            and is_canonical_monitor_device_path(children[0].DeviceID)):
        return children
    return []


def enumerate_windows_monitor_records() -> list[DisplayDeviceRecord]:
    """Enumerate active display targets and their stable interface paths."""

    enum_display_devices = ctypes.WinDLL('user32', use_last_error=True).EnumDisplayDevicesW
    enum_display_devices.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_DisplayDeviceW),
        wintypes.DWORD,
    ]
    enum_display_devices.restype = wintypes.BOOL

    records: list[DisplayDeviceRecord] = []
    # A live desktop handle is authoritative even when the PnP monitor is disabled.
    for gdi_name, (x, y, width, height, hmonitor, primary) in _enumerate_monitor_snapshot().items():
        children = []
        monitor_index = 0
        while True:
            monitor = _DisplayDeviceW()
            monitor.cb = ctypes.sizeof(monitor)
            if not enum_display_devices(gdi_name, monitor_index, ctypes.byref(monitor),
                                        _EDD_GET_DEVICE_INTERFACE_NAME):
                break
            monitor_index += 1
            children.append(monitor)
        candidates = _active_monitor_children(children, adapter_in_monitor_snapshot=True)
        monitor = candidates[0] if len(candidates) == 1 else None
        records.append(DisplayDeviceRecord(
            gdi_name=gdi_name,
            friendly_name=monitor.DeviceString if monitor else gdi_name,
            monitor_device_path=monitor.DeviceID if monitor else '',
            active=width > 0 and height > 0, primary=primary,
            x=x, y=y, width=width, height=height, hmonitor=hmonitor,
        ))
    return records


def enumerate_windows_monitors() -> list[MonitorChoice]:
    return build_monitor_choices(enumerate_windows_monitor_records())


def default_windows_monitor_path(
        choices: Iterable[MonitorChoice] | None = None) -> str:
    """Return a unique primary path, or a sole unambiguous monitor path."""
    available = list(
        enumerate_windows_monitors() if choices is None else choices)
    if not available:
        return ''
    primaries = [choice for choice in available
                 if bool(getattr(choice, 'primary', False))]
    if len(primaries) == 1:
        return normalize_monitor_device_path(primaries[0].device_path)
    if not primaries and len(available) == 1:
        return normalize_monitor_device_path(available[0].device_path)
    return ''


def _previous_selection_state(saved_path: str | None,
                              choices: list[MonitorChoice]) -> MonitorSelectionState:
    normalized = normalize_monitor_device_path(saved_path or '')
    if not normalized or is_monitor_selection_sentinel(normalized):
        return MonitorSelectionState.NOT_CONFIGURED
    if not is_monitor_capture_identity(normalized):
        return MonitorSelectionState.STALE
    if sum(_choice_matches(choice, normalized) for choice in choices) == 1:
        return MonitorSelectionState.RESOLVED
    return MonitorSelectionState.STALE


def _deterministic_default_choice(
        choices: list[MonitorChoice]) -> MonitorChoice | None:
    primaries = [choice for choice in choices
                 if bool(getattr(choice, 'primary', False))]
    if len(primaries) == 1:
        return primaries[0]
    if not primaries and len(choices) == 1:
        return choices[0]
    return None


def resolve_monitor_selection(
        saved_path: str | None,
        choices: Iterable[MonitorChoice]) -> MonitorSelectionResult:
    """Resolve persisted, legacy, or first-run monitor state deterministically.

    No ordering/index fallback is used.  This function is shared by the source
    popup and background startup so opening settings is never required to make
    first-run capture safe.
    """
    available = list(choices)
    previous_path = normalize_monitor_device_path(saved_path or '')
    previous_state = _previous_selection_state(previous_path, available)
    if not available:
        return MonitorSelectionResult(
            state=MonitorSelectionState.NO_DISPLAYS,
            code='MONITOR_SELECTION_NO_DISPLAYS',
            reason='no usable Windows monitor choices are present',
            previous_state=previous_state,
            previous_path=previous_path,
        )

    # A canonical persisted path wins, including a valid secondary monitor.
    if (previous_state is MonitorSelectionState.RESOLVED
            and is_monitor_capture_identity(previous_path)):
        choice = next(choice for choice in available
                      if _choice_matches(choice, previous_path))
        return MonitorSelectionResult(
            state=MonitorSelectionState.RESOLVED,
            code='MONITOR_SELECTION_RESOLVED',
            reason='saved canonical monitor path matches current topology',
            previous_state=previous_state,
            previous_path=previous_path,
            path=previous_path,
            choice=choice,
        )

    if (previous_path.startswith('fthr-monitor:')
            or (previous_state is MonitorSelectionState.STALE
                and any(choice.device_path.startswith('fthr-monitor:') for choice in available))):
        return MonitorSelectionResult(
            state=MonitorSelectionState.STALE, code='MONITOR_SELECTION_STALE',
            reason='live desktop identity changed; select the monitor again',
            previous_state=previous_state, previous_path=previous_path)

    # Migrate a legacy GDI/QScreen name only when it maps uniquely.
    legacy_matches = [
        choice for choice in available
        if previous_path and not is_monitor_capture_identity(previous_path)
        and not is_monitor_selection_sentinel(previous_path)
        and str(getattr(choice, 'gdi_name', '')).casefold()
        == previous_path.casefold()
    ]
    if len(legacy_matches) == 1:
        choice = legacy_matches[0]
        return MonitorSelectionResult(
            state=MonitorSelectionState.RECOVERED,
            code='MONITOR_SELECTION_RECOVERED',
            reason='legacy GDI/QScreen name migrated to canonical path',
            previous_state=previous_state,
            previous_path=previous_path,
            path=normalize_monitor_device_path(choice.device_path),
            choice=choice,
            persist_required=True,
        )

    choice = _deterministic_default_choice(available)
    if choice is None:
        return MonitorSelectionResult(
            state=MonitorSelectionState.AMBIGUOUS,
            code='MONITOR_SELECTION_AMBIGUOUS',
            reason=(
                'current topology has no unique primary and no sole monitor; '
                'refusing arbitrary monitor selection'),
            previous_state=previous_state,
            previous_path=previous_path,
        )
    recovered = previous_state is MonitorSelectionState.STALE
    return MonitorSelectionResult(
        state=(MonitorSelectionState.RECOVERED
               if recovered else MonitorSelectionState.RESOLVED),
        code=('MONITOR_SELECTION_RECOVERED'
              if recovered else 'MONITOR_SELECTION_RESOLVED'),
        reason=('stale monitor path recovered using deterministic current '
                'primary/sole-monitor policy' if recovered else
                'first-run monitor resolved using deterministic current '
                'primary/sole-monitor policy'),
        previous_state=previous_state,
        previous_path=previous_path,
        path=normalize_monitor_device_path(choice.device_path),
        choice=choice,
        persist_required=True,
    )


def validate_engine_monitor_argument(device_path: str | None) -> str:
    """Guard the final argv boundary against empty, GDI, and sentinel values."""
    normalized = normalize_monitor_device_path(device_path or '')
    if not is_monitor_capture_identity(normalized):
        previous_state = (MonitorSelectionState.NOT_CONFIGURED
                          if not normalized or is_monitor_selection_sentinel(normalized)
                          else MonitorSelectionState.STALE)
        result = MonitorSelectionResult(
            state=previous_state,
            code=('MONITOR_SELECTION_NOT_CONFIGURED'
                  if previous_state is MonitorSelectionState.NOT_CONFIGURED
                  else 'MONITOR_SELECTION_STALE'),
            reason='engine monitor argument is not a canonical persistent path',
            previous_state=previous_state,
            previous_path=normalized,
        )
        raise MonitorSelectionError(result)
    return normalized
