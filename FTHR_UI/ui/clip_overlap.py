"""Bounded, cancellable overlap discovery; no media probing on the Qt thread."""
from __future__ import annotations

import threading
from PySide6.QtCore import QObject, Signal, QRunnable, QThreadPool
from PySide6.QtWidgets import QApplication


class _Signals(QObject):
    finished = Signal(int, object, object)


class _Discovery(QRunnable):
    def __init__(self, generation, records, cache, signals):
        super().__init__()
        self.generation, self.records, self.cache = generation, records, cache
        self.signals = signals
        self.cancel = threading.Event()

    def run(self):
        cache, infos = {}, []
        try:
            from core.clip_overlap import probe_clip, find_overlaps
            for record in self.records:
                if self.cancel.is_set():
                    return
                key = (record.path, record.st_dev, record.st_ino,
                       record.size, record.mtime_ns)
                info = (self.cache[key] if key in self.cache
                        else probe_clip(record.path, cancel=self.cancel))
                cache[key] = info
                if len(cache) > 2048:
                    cache.pop(next(iter(cache)))
                if info is not None:
                    infos.append(info)
            overlaps = find_overlaps(infos) if not self.cancel.is_set() else {}
        except Exception:
            # Missing/invalid provenance is not a reason to interrupt browsing.
            overlaps = {}
        finally:
            self.signals.finished.emit(self.generation, locals().get('overlaps', {}), cache)


class OverlapDiscovery(QObject):
    changed = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        app = QApplication.instance()
        self.pool = QThreadPool(app or self)
        self.pool.setMaxThreadCount(1)
        self.signals = _Signals(app or self)
        self.signals.finished.connect(self._finished)
        self.generation = 0
        self.active = None
        self.pending = None
        self.cache = {}
        self.closed = False

    def update(self, records):
        if self.closed:
            return
        self.generation += 1
        self.pending = tuple(r for r in records if r.kind == 'video')
        self.changed.emit({})
        if self.active is not None:
            self.active.cancel.set()
        else:
            self._start()

    def _start(self):
        records, self.pending = self.pending, None
        worker = _Discovery(self.generation, records, dict(self.cache), self.signals)
        self.active = worker
        self.pool.start(worker)

    def _finished(self, generation, overlaps, cache):
        if self.active is None or self.active.generation != generation:
            return
        self.active = None
        if not self.closed and generation == self.generation:
            self.cache = cache
            self.changed.emit(overlaps)
        if self.pending is not None and not self.closed:
            self._start()

    def invalidate(self):
        """A newer library scan owns discovery from this point forward."""
        self.generation += 1
        self.pending = None
        if self.active is not None:
            self.active.cancel.set()

    def shutdown(self):
        self.closed = True
        self.generation += 1
        self.pending = None
        self.cache.clear()
        if self.active is not None:
            self.active.cancel.set()
