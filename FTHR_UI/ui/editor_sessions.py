"""Dormant editor sessions contain edit data, never a player or a timer."""

from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class EditorSession:
    clip_path: str
    button: Any = None
    state: dict | None = None
    window_geometry: Any = None
    window_maximized: bool = True


MAX_EDITOR_SESSIONS = 3
