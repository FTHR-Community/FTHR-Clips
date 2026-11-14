"""Application-level truth for clip finalization and consumer readiness."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum, auto

from core.library_index import canonical_media_path


class ClipReadinessState(Enum):
    INITIALIZING = auto()
    RECOVERY_REQUIRED = auto()
    ENGINE_COMMITTED = auto()
    FINALIZING = auto()
    READY = auto()
    READY_WITH_WARNING = auto()
    FINALIZATION_FAILED = auto()


def _key(path: str | os.PathLike[str]) -> str:
    return canonical_media_path(path)


@dataclass
class ClipReadinessHandle:
    state: ClipReadinessState
    event: threading.Event = field(default_factory=threading.Event)
    warning_messages: list[str] = field(default_factory=list)


class ClipReadinessRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, ClipReadinessHandle] = {}
        self._lock = threading.RLock()
        self._initializing = False
        self._blocked: dict[str, set[str]] = {}

    def set_initializing(self, initializing: bool) -> None:
        with self._lock:
            self._initializing = bool(initializing)

    def block_keys(self, keys, *, reason='mutation') -> None:
        """Block already captured canonical keys without resolving paths again."""
        with self._lock:
            for key in keys:
                self._blocked.setdefault(key, set()).add(str(reason))

    def unblock_keys(self, keys, *, reason='mutation') -> None:
        with self._lock:
            for key in keys:
                reasons = self._blocked.get(key)
                if reasons is not None:
                    reasons.discard(str(reason))
                    if not reasons:
                        self._blocked.pop(key, None)

    def block_paths(self, paths, *, reason='mutation') -> None:
        self.block_keys(tuple(_key(path) for path in paths), reason=reason)

    def unblock_paths(self, paths, *, reason='mutation') -> None:
        self.unblock_keys(tuple(_key(path) for path in paths), reason=reason)

    def set_recovery_paths(self, paths) -> None:
        with self._lock:
            for key, reasons in list(self._blocked.items()):
                reasons.discard('recovery')
                if not reasons:
                    self._blocked.pop(key, None)
            for path in paths:
                self._blocked.setdefault(_key(path), set()).add('recovery')

    def rename_committed(self, source, destination) -> None:
        with self._lock:
            handle = self._handles.pop(_key(source), None)
            if handle is not None:
                self._handles[_key(destination)] = handle

    def engine_committed(
        self, path: str | os.PathLike[str], *, needs_finalization: bool
    ) -> ClipReadinessHandle:
        state = (
            ClipReadinessState.FINALIZING
            if needs_finalization
            else ClipReadinessState.READY
        )
        handle = ClipReadinessHandle(state)
        if state is ClipReadinessState.READY:
            handle.event.set()
        with self._lock:
            self._handles[_key(path)] = handle
        return handle

    def ready(self, path: str | os.PathLike[str]) -> None:
        handle = self._get_or_create(path)
        with self._lock:
            handle.state = ClipReadinessState.READY
            handle.event.set()

    def record_warning(
        self, path: str | os.PathLike[str], message: str
    ) -> None:
        handle = self._get_or_create(path)
        with self._lock:
            handle.warning_messages.append(str(message))

    def complete(self, path: str | os.PathLike[str]) -> None:
        handle = self._get_or_create(path)
        with self._lock:
            handle.state = (
                ClipReadinessState.READY_WITH_WARNING
                if handle.warning_messages
                else ClipReadinessState.READY
            )
            handle.event.set()

    def is_tracked(self, path: str | os.PathLike[str]) -> bool:
        with self._lock:
            return _key(path) in self._handles

    def finalization_failed(
        self,
        path: str | os.PathLike[str],
        message: str,
        *,
        base_clip_usable: bool,
    ) -> None:
        handle = self._get_or_create(path)
        with self._lock:
            handle.warning_messages.append(str(message))
            handle.state = (
                ClipReadinessState.READY_WITH_WARNING
                if base_clip_usable
                else ClipReadinessState.FINALIZATION_FAILED
            )
            handle.event.set()

    def state(self, path: str | os.PathLike[str]) -> ClipReadinessState:
        with self._lock:
            if self._initializing:
                return ClipReadinessState.INITIALIZING
            path_key = _key(path)
            if self._blocked.get(path_key):
                return ClipReadinessState.RECOVERY_REQUIRED
            handle = self._handles.get(path_key)
            if handle is not None:
                return handle.state
        if os.path.isfile(path):
            return ClipReadinessState.READY
        return ClipReadinessState.FINALIZATION_FAILED

    def can_access(self, path: str | os.PathLike[str]) -> bool:
        return self.state(path) in {
            ClipReadinessState.READY,
            ClipReadinessState.READY_WITH_WARNING,
        }

    def event_for(self, path: str | os.PathLike[str]) -> threading.Event:
        return self._get_or_create(path).event

    def warnings(self, path: str | os.PathLike[str]) -> tuple[str, ...]:
        with self._lock:
            handle = self._handles.get(_key(path))
            return tuple(handle.warning_messages) if handle else ()

    def forget(self, path: str | os.PathLike[str]) -> None:
        with self._lock:
            self._handles.pop(_key(path), None)

    def _get_or_create(self, path: str | os.PathLike[str]) -> ClipReadinessHandle:
        path_key = _key(path)
        with self._lock:
            handle = self._handles.get(path_key)
            if handle is None:
                state = (
                    ClipReadinessState.READY
                    if os.path.isfile(path)
                    else ClipReadinessState.ENGINE_COMMITTED
                )
                handle = ClipReadinessHandle(state)
                if state is ClipReadinessState.READY:
                    handle.event.set()
                self._handles[path_key] = handle
            return handle


_REGISTRY = ClipReadinessRegistry()


def get_clip_readiness_registry() -> ClipReadinessRegistry:
    return _REGISTRY
