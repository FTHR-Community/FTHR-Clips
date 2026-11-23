"""Disposable persistent cache for versioned overlap inspection results."""
from __future__ import annotations

from contextlib import closing, contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

CACHE_MISS = object()
_MAX_ENTRIES = 16_384
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_DATABASE_PAGES = 8192  # 32 MiB at the explicitly selected page size.


def overlap_cache_path() -> Path:
    # Bump the filename if probe interpretation or the encoded results change.
    return Path.home() / '.fthr' / 'cache' / 'overlap-discovery-v1.sqlite3'


def _cache_key(identity) -> bytes:
    return sha256(json.dumps(identity, ensure_ascii=True, separators=(',', ':')).encode('ascii')).digest()


class OverlapProbeCache:
    """Access completed results from one background worker at a time."""

    def __init__(self, connection):
        self._connection = connection

    def get(self, identity):
        if self._connection is None:
            return CACHE_MISS
        try:
            row = self._connection.execute('SELECT result FROM probes WHERE identity = ?',
                                           (_cache_key(identity),)).fetchone()
        except sqlite3.Error:
            return CACHE_MISS
        return row[0] if row is not None else CACHE_MISS

    def put(self, identity, payload):
        if self._connection is None or (payload is not None and len(payload) > _MAX_PAYLOAD_BYTES):
            return
        key = _cache_key(identity)
        try:
            self._write(key, payload)
        except sqlite3.Error as error:
            if getattr(error, 'sqlite_errorcode', None) != sqlite3.SQLITE_FULL:
                return
            # Large positive results may reach the byte cap before the entry cap.
            try:
                with self._connection:
                    self._connection.execute(
                        'DELETE FROM probes WHERE rowid IN '
                        '(SELECT rowid FROM probes ORDER BY rowid LIMIT 1024)')
                self._write(key, payload)
            except sqlite3.Error:
                # Cache pressure must not turn a completed inspection into a failure.
                return

    def _write(self, key, payload):
        with self._connection:
            self._connection.execute('INSERT OR REPLACE INTO probes (identity, result) VALUES (?, ?)',
                                     (key, payload))
            self._connection.execute(
                'DELETE FROM probes WHERE rowid <= '
                '(SELECT rowid FROM probes ORDER BY rowid DESC LIMIT 1 OFFSET ?)',
                (_MAX_ENTRIES,))


@contextmanager
def open_overlap_probe_cache(path: Path):
    connection = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=.05)
        connection.execute('PRAGMA page_size = 4096')
        connection.execute(f'PRAGMA max_page_count = {_MAX_DATABASE_PAGES}')
        connection.execute('CREATE TABLE IF NOT EXISTS probes '
                           '(identity BLOB NOT NULL UNIQUE, result BLOB)')
    except (OSError, sqlite3.Error):
        if connection is not None:
            connection.close()
        yield OverlapProbeCache(None)
        return
    with closing(connection):
        yield OverlapProbeCache(connection)
