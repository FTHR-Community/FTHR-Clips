"""Cancellable overlap discovery with foreground admission and retained progress."""
from __future__ import annotations

from itertools import islice
from pathlib import Path
import threading
import time

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication

_BATCH_SIZE = 64
_RESUME_DELAY_MS = 250
_PROBE_SPACING_MS = 240
_RETRY_SECONDS = 30


class _Signals(QObject):
    finished = Signal(int, object, object, object)


class _Discovery(QRunnable):
    def __init__(self, generation, records, positives, rebuild, signals, cache_path):
        super().__init__()
        self.generation = generation
        self.records = records
        self.positives = positives
        self.rebuild = rebuild
        self.signals = signals
        self.cache_path = cache_path
        self.cancel = threading.Event()
        self.probed = False

    def run(self):
        from core.clip_overlap import find_overlaps, inspect_clip
        from core.clip_overlap_cache import open_overlap_probe_cache

        completed, failed, overlaps = {}, [], None
        try:
            with open_overlap_probe_cache(self.cache_path) as cache:
                for path, expected in self.records:
                    if self.cancel.is_set():
                        break
                    result = inspect_clip(path, self.cancel, cache=cache, expected=expected)
                    self.probed = self.probed or result.probed
                    if result.complete:
                        completed[path] = result.info
                    elif not self.cancel.is_set():
                        failed.append(path)
                    if result.probed:
                        # Conclusive cached results are cheap to batch. Release
                        # the foreground lane after one fresh subprocess.
                        break
            new_positives = [info for info in completed.values() if info is not None]
            if self.rebuild or new_positives:
                overlaps = find_overlaps((*self.positives, *new_positives))
        finally:
            self.signals.finished.emit(self.generation, completed, failed, overlaps)


class OverlapDiscovery(QObject):
    changed = Signal(object)

    def __init__(self, parent=None, *, cache_path=None):
        super().__init__(parent)
        from core.clip_overlap_cache import overlap_cache_path

        app = QApplication.instance()
        self.pool = QThreadPool(app or self)
        self.pool.setMaxThreadCount(1)
        self.signals = _Signals(app or self)
        self.signals.finished.connect(self._finished)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._start)
        self._cache_path = Path(cache_path) if cache_path is not None else overlap_cache_path()
        self.generation = 0
        self.active = None
        self.cache = {}
        self.closed = False
        self._paused = False
        self._scrolling = False
        self._records = {}
        self._pending = {}
        self._retry = {}
        self._priority = ()
        self._positives = {}
        self._overlaps = {}
        self._rebuild = False

    def update(self, records):
        if self.closed:
            return
        next_records = {r.path: (r.size, r.mtime_ns, r.st_ino, r.st_dev) for r in records
                        if r.kind == 'video' and r.path.lower().endswith('.mp4')}
        if next_records == self._records:
            self._schedule()
            return

        outdated = {path for path, identity in self._records.items()
                    if next_records.get(path) != identity}
        self._records = next_records
        for path in outdated:
            self.cache.pop(path, None)
            self._retry.pop(path, None)
            if self._positives.pop(path, None) is not None:
                self._rebuild = True
        if outdated and self._overlaps:
            still_valid = {path: tuple(partner for partner in partners if partner not in outdated)
                           for path, partners in self._overlaps.items() if path not in outdated}
            self._publish({path: partners for path, partners in still_valid.items() if partners})

        self.generation += 1
        self._pending = {path: None for path in next_records if path not in self.cache
                         and path not in self._retry}
        self._interrupt()
        self._schedule(_RESUME_DELAY_MS)

    def prioritize(self, paths):
        if not self.closed:
            self._priority = tuple(paths)

    def set_paused(self, paused):
        if self._paused != paused:
            self._paused = paused
            self._admission_changed()

    def set_scrolling(self, scrolling):
        if self._scrolling != scrolling:
            self._scrolling = scrolling
            self._admission_changed()

    def _admission_changed(self):
        if self._paused or self._scrolling:
            self._interrupt()
        else:
            self._schedule(_RESUME_DELAY_MS)

    def _interrupt(self):
        self._timer.stop()
        if self.active is not None:
            self.active.cancel.set()

    def _blocked(self):
        return self.closed or self._paused or self._scrolling or self.active is not None

    def _schedule(self, delay=0):
        if self._blocked() or self._timer.isActive():
            return
        if self._pending or self._rebuild:
            self._timer.start(delay)
        elif self._retry:
            remaining_ms = int((min(self._retry.values()) - time.monotonic()) * 1000)
            self._timer.start(max(delay, remaining_ms, 1))

    def _start(self):
        if self._blocked():
            return
        now = time.monotonic()
        for path, until in tuple(self._retry.items()):
            if until <= now:
                del self._retry[path]
                self._pending[path] = None
        if not self._pending and not self._rebuild:
            self._schedule()
            return
        paths = dict.fromkeys(path for path in self._priority if path in self._pending)
        paths.update(dict.fromkeys(islice(self._pending, _BATCH_SIZE)))
        records = tuple((path, self._records[path]) for path in islice(paths, _BATCH_SIZE))
        worker = _Discovery(self.generation, records, tuple(self._positives.values()),
                            self._rebuild, self.signals, self._cache_path)
        self.active = worker
        self.pool.start(worker)

    def _finished(self, generation, completed, failed, overlaps):
        worker = self.active
        if worker is None or worker.generation != generation:
            return
        self.active = None
        if not self.closed and generation == self.generation:
            for path, info in completed.items():
                self._pending.pop(path, None)
                self.cache[path] = info
                if info is not None:
                    self._positives[path] = info
            retry_at = time.monotonic() + _RETRY_SECONDS
            for path in failed:
                self._pending.pop(path, None)
                self._retry[path] = retry_at
            if overlaps is not None:
                self._rebuild = False
                self._publish(overlaps)
        delay = _PROBE_SPACING_MS if worker.probed else 0
        if worker.cancel.is_set():
            delay = max(delay, _RESUME_DELAY_MS)
        self._schedule(delay)

    def _publish(self, overlaps):
        if overlaps != self._overlaps:
            self._overlaps = overlaps
            self.changed.emit(overlaps)

    def shutdown(self):
        self.closed = True
        self.generation += 1
        self._interrupt()
        self.cache.clear()
        self._records.clear()
        self._pending.clear()
        self._retry.clear()
        self._positives.clear()
        self._overlaps.clear()
        self._priority = ()
