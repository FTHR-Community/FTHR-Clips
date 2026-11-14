"""Typed, strictly validated saved-library-search criteria."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from uuid import UUID


_CRITERIA_KEYS = frozenset({
    "version", "text", "tags_all", "album_id", "kind", "favorites_only", "sort",
})
_KINDS = frozenset({"all", "clips", "screenshots", "imported", "favorites"})
_SORTS = frozenset({"newest", "oldest"})
_MAX_SEARCH_TAG_LENGTH = 32768
_MAX_SEARCH_TAGS = 32


def _collapse_spaces(value: str) -> str:
    result = []
    pending_space = False
    for char in value:
        if char.isspace():
            pending_space = True
            continue
        if pending_space and result:
            result.append(" ")
        pending_space = False
        result.append(char)
    return "".join(result).strip()


def _normalized_search_text(value: str, *, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise ValueError(f"{field} cannot contain control or format characters")
    value = _collapse_spaces(value)
    if len(value) > limit:
        raise ValueError(f"{field} is too long")
    return value


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    version: int = 1
    text: str = ""
    tags_all: tuple[str, ...] = ()
    album_id: str | None = None
    kind: str = "all"
    favorites_only: bool = False
    sort: str = "newest"


@dataclass(frozen=True, slots=True)
class SavedSearch:
    search_id: str
    name: str
    created_utc: str
    criteria: SearchCriteria | None
    error: str | None = None


def normalize_search_name(name: str) -> tuple[str, str]:
    display = _normalized_search_text(name, field="saved search name", limit=80)
    if not display:
        raise ValueError("saved search name cannot be empty")
    return display, display.casefold()


def _normalize_tags(values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise TypeError("tags_all must be a list or tuple of strings")
    result = []
    keys = set()
    for value in values:
        normalized = _normalized_search_text(value, field="search tag", limit=_MAX_SEARCH_TAG_LENGTH)
        if not normalized:
            raise ValueError("search tags cannot be empty")
        key = normalized.casefold()
        if key not in keys:
            keys.add(key)
            result.append(key)
            if len(result) > _MAX_SEARCH_TAGS:
                raise ValueError("a saved search cannot have more than 32 tags")
    return tuple(result)


def normalize_search_criteria(value) -> SearchCriteria:
    if isinstance(value, SearchCriteria):
        mapping = {
            "version": value.version, "text": value.text, "tags_all": value.tags_all,
            "album_id": value.album_id, "kind": value.kind,
            "favorites_only": value.favorites_only, "sort": value.sort,
        }
    elif isinstance(value, dict):
        if set(value) != _CRITERIA_KEYS:
            raise ValueError("saved search criteria has unknown or missing fields")
        mapping = value
    else:
        raise TypeError("saved search criteria must be a mapping or SearchCriteria")
    if type(mapping.get("version")) is not int or mapping["version"] != 1:
        raise ValueError("unsupported saved search criteria version")
    text = _normalized_search_text(mapping.get("text"), field="search text", limit=200)
    tags_all = _normalize_tags(mapping.get("tags_all"))
    album_id = mapping.get("album_id")
    if album_id is not None:
        if not isinstance(album_id, str):
            raise TypeError("album_id must be a UUID string or null")
        try:
            album_id = str(UUID(album_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("album_id must be a UUID string or null") from error
    kind = mapping.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("unsupported saved search kind")
    favorites_only = mapping.get("favorites_only")
    if type(favorites_only) is not bool:
        raise TypeError("favorites_only must be a boolean")
    sort = mapping.get("sort")
    if not isinstance(sort, str) or sort not in _SORTS:
        raise ValueError("unsupported saved search sort")
    return SearchCriteria(1, text, tags_all, album_id, kind, favorites_only, sort)


def criteria_to_dict(criteria) -> dict:
    criteria = normalize_search_criteria(criteria)
    return {
        "version": criteria.version,
        "text": criteria.text,
        "tags_all": list(criteria.tags_all),
        "album_id": criteria.album_id,
        "kind": criteria.kind,
        "favorites_only": criteria.favorites_only,
        "sort": criteria.sort,
    }
