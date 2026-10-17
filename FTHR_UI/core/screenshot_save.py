"""Publish screenshots atomically without overwriting existing files.

QScreen capture runs on the Qt thread. ScreenshotPngSaveWorker encodes and
stages the PNG before making the completed file visible.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QThread, Signal, QRect
from PySide6.QtGui import QImage, QImageReader


class _PngWritable(Protocol):
    def save(self, file_name: str, fmt: str) -> bool: ...


class ScreenshotSaveError(RuntimeError):
    """A user-actionable screenshot save failure."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f'{code}: {detail}')


@dataclass(frozen=True)
class ScreenshotPaths:
    """A final PNG name and its same-directory, non-library staging name."""

    final: Path
    staged: Path


def _ensure_output_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ScreenshotSaveError(
            'OUTPUT_DIRECTORY_UNAVAILABLE',
            f'Could not create the screenshot folder: {error}',
        ) from error
    if not directory.is_dir():
        raise ScreenshotSaveError(
            'OUTPUT_DIRECTORY_UNAVAILABLE',
            f'Screenshot output path is not a directory: {directory}',
        )


def _next_available_path(directory: Path, stem: str) -> Path:
    candidate = directory / f'{stem}.png'
    suffix = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = directory / f'{stem}_{suffix:02d}.png'
        suffix += 1
    return candidate


def _create_staged_path(final_path: Path) -> Path:
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f'.{final_path.stem}.',
            suffix='.png.partial',
            dir=final_path.parent,
        )
        os.close(descriptor)
    except OSError as error:
        raise ScreenshotSaveError(
            'OUTPUT_DIRECTORY_UNAVAILABLE',
            f'Could not create a staged screenshot file: {error}',
        ) from error
    return Path(temporary)


def reserve_screenshot_paths(
    directory: str | Path,
    *,
    now: datetime | None = None,
    stem: str | None = None,
) -> ScreenshotPaths:
    """Reserve a timestamped output name and OS-allocated staging file.

    Append a sequence suffix when the output name already exists.
    """

    output_directory = Path(directory)
    _ensure_output_directory(output_directory)
    timestamp = (now or datetime.now()).strftime('%Y%m%d_%H%M%S_%f')
    final_stem = stem or f'screenshot_from_{timestamp}'
    final_path = _next_available_path(output_directory, final_stem)
    return ScreenshotPaths(final=final_path, staged=_create_staged_path(final_path))


def reserve_cropped_screenshot_paths(final_path: str | Path) -> ScreenshotPaths:
    """Reserve a sibling output for the editor's existing cropped-image mode."""

    final = Path(final_path)
    _ensure_output_directory(final.parent)
    cropped_stem = f'{final.stem}_cropped'
    cropped_final = _next_available_path(final.parent, cropped_stem)
    return ScreenshotPaths(
        final=cropped_final,
        staged=_create_staged_path(cropped_final),
    )


def write_png_to_staged(image: _PngWritable, staged_path: str | Path) -> None:
    """Encode one complete PNG into a non-final, same-directory staging file."""

    staged = Path(staged_path)
    try:
        saved = image.save(str(staged), 'PNG')
    except Exception as error:
        staged.unlink(missing_ok=True)
        raise ScreenshotSaveError(
            'IMAGE_ENCODE_FAILED', f'Could not encode the screenshot: {error}') from error
    if not saved:
        staged.unlink(missing_ok=True)
        raise ScreenshotSaveError(
            'IMAGE_ENCODE_FAILED', 'Qt could not encode the screenshot as PNG.')


def publish_staged_png(staged_path: str | Path, final_path: str | Path) -> None:
    """Publish a staged PNG without overwriting a raced destination.

    Use rename on Windows and a same-directory hard link on POSIX, where
    rename would replace an existing file.
    """

    staged = Path(staged_path)
    final = Path(final_path)
    try:
        os.rename(staged, final)
        return
    except FileExistsError as error:
        raise ScreenshotSaveError(
            'OUTPUT_COLLISION',
            f'Refusing to overwrite an existing file: {final.name}',
        ) from error
    except OSError as error:
        raise ScreenshotSaveError(
            'WRITE_FAILED', f'Could not publish the screenshot: {error}') from error
    try:
        staged.unlink()
    except OSError as error:
        # Roll back the exact link created above. A failure never leaves a
        # final-looking name while reporting that publication failed.
        final.unlink(missing_ok=True)
        raise ScreenshotSaveError(
            'WRITE_FAILED', f'Could not remove screenshot staging file: {error}'
        ) from error


class ScreenshotPngSaveWorker(QThread):
    """Encode a GUI-thread snapshot without blocking the UI event loop."""

    succeeded = Signal(str)
    failed = Signal(str, str)

    def __init__(self, image: QImage, staged_path: str | Path, parent=None):
        super().__init__(parent)
        # QImage is implicitly shared.  Make this worker's ownership explicit
        # before the QPixmap and GUI event handler continue.
        self._image = image.copy()
        self._staged_path = Path(staged_path)

    def run(self) -> None:
        try:
            write_png_to_staged(self._image, self._staged_path)
        except ScreenshotSaveError as error:
            self.failed.emit(error.code, error.detail)
        else:
            self.succeeded.emit(str(self._staged_path))
        finally:
            self._image = QImage()


class ScreenshotCropSaveWorker(QThread):
    """Read original pixels only during saving, with no full image on the GUI."""

    succeeded = Signal(str)
    failed = Signal(str, str)

    def __init__(self, source_path, crop_rect, staged_path, parent=None):
        super().__init__(parent)
        self._source_path = str(source_path)
        self._crop_rect = QRect(crop_rect)
        self._staged_path = Path(staged_path)

    def run(self):
        try:
            reader = QImageReader(self._source_path)
            reader.setAutoTransform(True)
            image = reader.read()
            if image.isNull():
                raise ScreenshotSaveError('IMAGE_ENCODE_FAILED', reader.errorString())
            rect = self._crop_rect.intersected(image.rect())
            if rect.isEmpty():
                raise ScreenshotSaveError('IMAGE_ENCODE_FAILED', 'The selected crop is empty.')
            cropped = image.copy(rect)
            image = QImage()
            write_png_to_staged(cropped, self._staged_path)
            cropped = QImage()
        except ScreenshotSaveError as error:
            self._staged_path.unlink(missing_ok=True)
            self.failed.emit(error.code, error.detail)
        else:
            self.succeeded.emit(str(self._staged_path))
