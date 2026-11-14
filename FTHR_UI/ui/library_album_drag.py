"""Private in-process album drag marker; never expose media as file URLs."""
from dataclasses import dataclass

from PySide6.QtCore import QMimeData

from core.library_index import LibraryRecord


class AlbumDragMimeData(QMimeData):
    def __init__(self):
        super().__init__()
        self.setData('application/x-fthr-album-selection', b'1')


@dataclass(frozen=True)
class AlbumDragSnapshot:
    records: tuple[LibraryRecord, ...]
    ordered: tuple[LibraryRecord, ...]
    catalog: tuple
    criteria: object
    revision: int
