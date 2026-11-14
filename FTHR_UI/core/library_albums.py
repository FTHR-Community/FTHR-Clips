"""Small, SQLite-only helpers for the library album catalog.

The metadata manager owns locking, transactions, and the in-memory snapshot.
This module keeps the value object, name rules, and additive schema definition
in one place so album operations cannot accidentally alter media files.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Album:
    album_id: str
    name: str
    created_utc: str


def normalize_album_name(value: str) -> tuple[str, str]:
    """Return the display name and case-folded uniqueness key."""

    if not isinstance(value, str):
        raise TypeError("album name must be text")
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("album name contains a control or format character")
    name = " ".join(value.strip().split())
    if not name:
        raise ValueError("album name must not be blank")
    if len(name) > 80:
        raise ValueError("album name is limited to 80 characters")
    return name, name.casefold()


def create_album_schema(connection: sqlite3.Connection) -> None:
    """Create the schema-4 tables without touching existing media rows."""

    connection.execute(
        "CREATE TABLE albums ("
        "album_id TEXT PRIMARY KEY NOT NULL,"
        "name TEXT NOT NULL,"
        "name_key TEXT NOT NULL UNIQUE,"
        "created_utc TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE album_members ("
        "album_id TEXT NOT NULL REFERENCES albums(album_id) ON DELETE CASCADE,"
        "clip_id TEXT NOT NULL REFERENCES entries(clip_id) ON DELETE CASCADE,"
        "PRIMARY KEY(album_id,clip_id))"
    )
    connection.execute(
        "CREATE INDEX album_members_clip_id "
        "ON album_members(clip_id)"
    )


def album_from_row(row) -> Album:
    return Album(str(row[0]), str(row[1]), str(row[2]))
