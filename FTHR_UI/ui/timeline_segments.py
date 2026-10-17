"""Edit-driven invalidation for mutable timeline intervals."""

from dataclasses import dataclass
from weakref import WeakMethod


@dataclass
class TimelineSegment:
    """One non-destructive source interval in the editor timeline."""

    start_pct: float
    end_pct: float
    deleted: bool = False

    def __setattr__(self, name, value):
        changed = name in ('start_pct', 'end_pct', 'deleted') and (
            name not in self.__dict__ or self.__dict__[name] != value)
        object.__setattr__(self, name, value)
        if changed:
            observers = self.__dict__.get('_observers', [])
            live = []
            for reference in observers:
                callback = reference()
                if callback is not None:
                    live.append(reference)
                    callback()
            if observers:
                self._observers = live


class TimelineSegments(list):
    """List-compatible intervals; structural and field edits advance a version."""

    def __init__(self, values=()):
        super().__init__(values)
        self.version = 0
        self._observed = []
        self._reference = WeakMethod(self._segment_changed)
        self._rebind()

    def _segment_changed(self):
        self.version += 1

    def detach(self):
        for segment in self._observed:
            segment._observers = [ref for ref in segment._observers
                                  if ref is not self._reference and ref() is not None]
        self._observed = []

    def _rebind(self):
        self.detach()
        seen = set()
        for segment in self:
            if id(segment) in seen:
                continue
            seen.add(id(segment))
            observers = segment.__dict__.setdefault('_observers', [])
            observers.append(self._reference)
            self._observed.append(segment)
        self.version += 1

    def _mutate(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        finally:
            # Even a generator/sort callback that raises may have changed a list.
            self._rebind()

    def __setitem__(self, key, value):
        self._mutate(super().__setitem__, key, value)

    def __delitem__(self, key):
        self._mutate(super().__delitem__, key)

    def append(self, value):
        self._mutate(super().append, value)

    def extend(self, values):
        self._mutate(super().extend, values)

    def insert(self, index, value):
        self._mutate(super().insert, index, value)

    def pop(self, index=-1):
        return self._mutate(super().pop, index)

    def remove(self, value):
        self._mutate(super().remove, value)

    def clear(self):
        self._mutate(super().clear)

    def reverse(self):
        self._mutate(super().reverse)

    def sort(self, *, key=None, reverse=False):
        self._mutate(super().sort, key=key, reverse=reverse)

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, count):
        self._mutate(super().__imul__, count)
        return self
