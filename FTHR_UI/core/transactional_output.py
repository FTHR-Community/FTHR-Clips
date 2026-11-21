"""Same-directory staging for atomically published media outputs."""

from __future__ import annotations

import os
import uuid
from pathlib import Path


def create_staged_output_path(final_path: str | os.PathLike[str]) -> Path:
    final = Path(final_path)
    token = uuid.uuid4().hex
    # Keep FFmpeg's temporary output within the Windows path budget even
    # when the final filename uses most of it. FFmpeg needs the media suffix.
    return final.with_name(f'.partial.{token}{final.suffix}')


def commit_staged_output(
    staged_path: str | os.PathLike[str], final_path: str | os.PathLike[str]
) -> None:
    staged = Path(staged_path)
    final = Path(final_path)
    if staged.parent.resolve() != final.parent.resolve():
        raise ValueError('staged and final output must be in the same directory')
    if not staged.is_file():
        raise FileNotFoundError(staged)
    os.replace(staged, final)


def discard_staged_output(staged_path: str | os.PathLike[str]) -> None:
    try:
        Path(staged_path).unlink()
    except FileNotFoundError:
        # Cleanup is idempotent; another cancellation path may have removed it.
        pass
