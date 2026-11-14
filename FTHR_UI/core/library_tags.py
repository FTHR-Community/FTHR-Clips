"""Validation and CSV helpers for additive clip tag metadata."""

from __future__ import annotations

import csv
import io
import unicodedata
from collections.abc import Iterable


MAX_TAGS = 32
MAX_TAG_LENGTH = 80


def _collapse_spaces(value: str) -> str:
    parts = []
    pending_space = False
    for char in value:
        if char.isspace():
            pending_space = True
            continue
        if pending_space and parts:
            parts.append(" ")
        pending_space = False
        parts.append(char)
    return "".join(parts).strip()


def _normalize_tag(value: str, *, limit: int = MAX_TAG_LENGTH) -> str:
    if not isinstance(value, str):
        raise TypeError("tags must contain strings")
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError("tags cannot contain control or format characters")
    value = _collapse_spaces(value)
    if not value:
        raise ValueError("tags cannot be empty")
    if len(value) > limit:
        raise ValueError(f"tags cannot exceed {limit} characters")
    return value


def normalize_tags(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize explicit tags and remove duplicates by Unicode casefold key."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError("tags must be an iterable of strings")
    result = []
    keys = set()
    for value in values:
        normalized = _normalize_tag(value)
        key = normalized.casefold()
        if key in keys:
            continue
        keys.add(key)
        result.append(normalized)
        if len(result) > MAX_TAGS:
            raise ValueError(f"a clip cannot have more than {MAX_TAGS} tags")
    return tuple(result)


def parse_tags_text(text: str) -> tuple[str, ...]:
    """Parse the strict CSV editor representation used for new tag edits."""

    if not isinstance(text, str):
        raise TypeError("tag text must be a string")
    if not text.strip():
        return ()
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""),
                               strict=True, skipinitialspace=True))
    except (csv.Error, TypeError) as error:
        raise ValueError("invalid tag CSV") from error
    if len(rows) != 1:
        raise ValueError("tag CSV must contain one record")
    return normalize_tags(rows[0])


def format_tags_text(values: Iterable[str]) -> str:
    """Format strings as CSV without changing legacy values.

    Validation belongs to ``normalize_tags``.  This formatter is also used
    for compatibility display, where old values may exceed current limits or
    contain characters that new edits reject.
    """

    if isinstance(values, (str, bytes)):
        raise TypeError("tags must be an iterable of strings")
    formatted = []
    for tag in values:
        if not isinstance(tag, str):
            raise TypeError("tags must contain strings")
        quoted = (tag[:1].isspace() or tag[-1:].isspace()
                  or any(char in tag for char in ',\"\r\n'))
        if quoted:
            formatted.append('"' + tag.replace('"', '""') + '"')
        else:
            formatted.append(tag)
    return ", ".join(formatted)
