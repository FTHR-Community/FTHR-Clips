"""Byte-bounded image caches with idle expiry; never touch the global Qt cache."""

from collections import OrderedDict
from collections.abc import MutableMapping
import threading
import time
import weakref

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QApplication


def image_bytes(value):
    if isinstance(value, QImage):
        return value.sizeInBytes()
    if isinstance(value, QPixmap):
        return value.width() * value.height() * max(1, value.depth() // 8)
    if isinstance(value, (tuple, list)):
        return sum(image_bytes(item) for item in value)
    return 0


_caches = []


class ImageLRU(MutableMapping):
    """Own only entries that fit; costs include pixel buffers, not object overhead.

    Callers remain responsible for images also held by visible widgets. A lock
    permits the timeline decoder to publish QImages while the GUI expires them.
    """

    def __init__(self, max_bytes, *, max_entries=64, ttl=120):
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.ttl = ttl
        self._entries = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()
        _caches.append(weakref.ref(self))

    @property
    def bytes_used(self):
        with self._lock:
            return self._bytes

    def __getitem__(self, key):
        with self._lock:
            value, cost, touched = self._entries[key]
            now = time.monotonic()
            if now - touched >= self.ttl:
                del self[key]
                raise KeyError(key)
            self._entries[key] = (value, cost, now)
            self._entries.move_to_end(key)
            return value

    def __setitem__(self, key, value):
        cost = image_bytes(value)
        with self._lock:
            self.pop(key, None)
            if cost > self.max_bytes:
                return
            self._entries[key] = (value, cost, time.monotonic())
            self._bytes += cost
            self.expire()
            while self._bytes > self.max_bytes or len(self._entries) > self.max_entries:
                self.popitem(last=False)

    def __delitem__(self, key):
        with self._lock:
            _, cost, _ = self._entries.pop(key)
            self._bytes -= cost

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._entries))

    def __len__(self):
        with self._lock:
            return len(self._entries)

    def move_to_end(self, key):
        with self._lock:
            self._entries.move_to_end(key)

    def popitem(self, last=True):
        with self._lock:
            key, (value, cost, _) = self._entries.popitem(last=last)
            self._bytes -= cost
            return key, value

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def expire(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            for key, (_, _, touched) in tuple(self._entries.items()):
                if now - touched >= self.ttl:
                    del self[key]


def start_image_cache_maintenance():
    """Install one lightweight GUI timer for caches unused by any active widget."""
    app = QApplication.instance()
    if app is None or hasattr(app, '_image_cache_expiry_timer'):
        return
    timer = QTimer(app)
    timer.setInterval(30_000)
    timer.timeout.connect(_expire_caches)
    app._image_cache_expiry_timer = timer
    timer.start()


def _expire_caches():
    live = []
    for reference in _caches:
        cache = reference()
        if cache is not None:
            cache.expire()
            live.append(reference)
    _caches[:] = live
