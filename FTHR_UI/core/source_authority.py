"""Stable source identity helpers shared by picker and game handoff."""

from __future__ import annotations

import os
from typing import Any, Mapping


def window_identity(window: Any) -> dict[str, Any] | None:
    """Build a conservative persisted identity for a selected HWND."""
    def field(name: str, default=None):
        return (window.get(name, default) if isinstance(window, Mapping)
                else getattr(window, name, default))

    try:
        hwnd = int(field('hwnd', 0) or 0)
        pid = int(field('pid', 0) or 0)
    except (TypeError, ValueError):
        # Malformed native handles are not safe enough to persist as identity.
        return None
    if not hwnd or not pid:
        return None
    exe_path = str(field('exe_path', '') or '').strip()
    class_name = str(field('class_name', '') or '').strip()
    if not exe_path or not class_name:
        return None
    return {
        'hwnd': hwnd,
        'pid': pid,
        'exe_path': os.path.normcase(os.path.abspath(exe_path)),
        'class_name': class_name.casefold(),
        'process_created': field('process_created', None),
    }


def identity_matches(saved: Any, candidate: Any) -> bool:
    """Require PID, executable, class and HWND equality; unknown fails closed."""
    current = window_identity(candidate)
    if not isinstance(saved, Mapping) or current is None:
        return False
    try:
        prior = {
            'hwnd': int(saved.get('hwnd', 0) or 0),
            'pid': int(saved.get('pid', 0) or 0),
            'exe_path': os.path.normcase(os.path.abspath(
                str(saved.get('exe_path', '') or ''))),
            'class_name': str(saved.get('class_name', '') or '').casefold(),
            'process_created': saved.get('process_created'),
        }
    except (TypeError, ValueError):
        # A damaged legacy identity must fail closed, not match another HWND.
        return False
    return prior == current
