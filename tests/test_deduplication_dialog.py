import pytest
from PySide6.QtWidgets import QApplication

from ui.deduplication_dialog import ClipDeduplicationDialog


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_deduplication_dialog_initialization(qapp, tmp_path):
    dlg = ClipDeduplicationDialog(tmp_path)
    try:
        assert hasattr(dlg, "merge_btn")
        assert hasattr(dlg, "scan_btn")
        assert hasattr(dlg, "preview_btn")
        assert hasattr(dlg, "disclaimer_label")
        assert "without re-encoding" in dlg.disclaimer_label.text().lower()
    finally:
        if dlg._scan_worker and dlg._scan_worker.isRunning():
            dlg._scan_worker.cancel()
            dlg._scan_worker.wait(1000)
        dlg.close()
