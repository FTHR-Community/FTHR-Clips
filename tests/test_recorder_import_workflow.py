"""Exercise recorder discovery, linking and relocation through the Qt import controls."""
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtWidgets import QFileDialog, QLabel, QPushButton

from core.settings_manager import SettingsManager
from main import _SettingsPage
from ui.clip_grid import ClipGrid


def test_recorder_import_workflow(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    settings = SettingsManager()
    owned = tmp_path / 'Owned'
    owned.mkdir()
    settings.set('clips_directory', str(owned))
    page = _SettingsPage(settings)
    grid = ClipGrid(settings)
    qtbot.addWidget(page)
    qtbot.addWidget(grid)
    page.connect_import_grid(grid)
    grid.show()
    dialogs = []
    paths = []

    def choose(parent, *args):
        dialogs.append(parent)
        return str(paths.pop(0))

    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', choose)
    sample = tmp_path / 'sample.mp4'
    writer = cv2.VideoWriter(str(sample), cv2.VideoWriter_fourcc(*'mp4v'), 10, (32, 32))
    assert writer.isOpened()
    writer.write(np.zeros((32, 32, 3), dtype=np.uint8))
    writer.release()
    payload = sample.read_bytes()

    def recording(folder, name):
        media = folder / name
        media.parent.mkdir(parents=True, exist_ok=True)
        media.write_bytes(payload)
        return media

    def result_labels():
        return [label.text() for label in page._scan_results_container.findChildren(QLabel)
                if label.parent() is page._scan_results_container]

    def wait_for_count(count):
        qtbot.waitUntil(lambda: len(grid._filtered_records()) == count
                        and not grid._pending_import_roots, timeout=10000)
        assert grid._filter == 'imported'

    try:
        grid.filter_combo.setCurrentIndex(grid.filter_combo.findText('Imported clips'))
        assert grid._add_import_folder.isVisible()
        assert 'originals stay in place' in grid._empty_detail_lbl.text()
        custom = tmp_path / 'Custom recordings'
        originals = [recording(custom, f'Game/Clip {i}.mp4') for i in range(2)]
        paths.append(custom)
        grid._add_import_folder.click()
        assert dialogs[-1] is grid
        wait_for_count(2)
        assert grid._import_status.text().endswith('2 new imported clips.')
        assert grid._import_status.isVisible()
        assert '_import_folders_layout' not in page.__dict__
        assert not page.isVisible()
        assert grid._add_import_folder.isVisible()
        with qtbot.waitSignal(grid.manage_import_folders_requested):
            grid._manage_import_folders.click()

        scan = page._scan_btn
        assert scan.text() == 'Find recording folders'
        choose_button = next(b for b in scan.parentWidget().findChildren(QPushButton)
                             if b.text() == 'Choose folder')
        scan.click()
        assert 'No recording folders found in the usual locations.' in result_labels()
        paths.append(custom)
        choose_button.click()
        wait_for_count(2)
        assert grid._import_status.text().endswith('Already linked.')

        videos = tmp_path / 'Videos'
        fixtures = {
            'Medal': 'Clips/Valorant/medal.mp4',
            'Captures': 'Game Bar capture.mp4',
            'Outplayed': 'Overwatch/session/highlight.mp4',
            'Plays.tv': 'Game/session.mp4',
            'AMD/ReLive': 'Game/replay.mp4',
            'Shadowplay Clips': 'Desktop/replay.mp4',
            'NVIDIA': 'Overwatch/replay.mp4',
            'Nvidia Highlights': 'Game/highlight.mp4',
        }
        originals.extend(recording(videos / root, name) for root, name in fixtures.items())
        scan.click()
        assert {Path(p) for _, p in page._scan_pending} == {videos / f for f in fixtures}
        for count in range(3, 11):
            row = page._scan_results_layout.itemAt(2).widget()
            next(b for b in row.findChildren(QPushButton) if b.text() == 'Add').click()
            wait_for_count(count)
            assert grid._import_status.text().endswith('1 new imported clip.')
        scan.click()
        assert 'All detected recording folders are already linked.' in result_labels()
        paths.append('')
        choose_button.click()
        assert dialogs[-1] is page and not paths
        assert all(p.read_bytes() == payload for p in originals)

        moved = tmp_path / 'Relocated'
        custom.rename(moved)
        page._refresh_import_folders_list()
        row = page._import_folders_layout.itemAt(0).widget()
        locate = next(b for b in row.findChildren(QPushButton) if b.text() == 'Locate folder')
        paths.extend(['', moved])
        locate.click()
        assert str(custom) in SettingsManager().get('imported_clip_folders')
        locate.click()
        wait_for_count(10)
        assert grid._import_status.text().endswith('2 new imported clips.')
        persisted = SettingsManager().get('imported_clip_folders')
        assert len(persisted) == 9 and str(moved) in persisted and str(custom) not in persisted
        assert {Path(r.path) for r in grid._filtered_records()} == {
            moved / p.relative_to(custom) if p.is_relative_to(custom) else p for p in originals}
        assert all(Path(r.path).read_bytes() == payload for r in grid._filtered_records())
        empty = tmp_path / 'No videos'
        empty.mkdir()
        (empty / 'screen.mov').touch()
        grid.filter_combo.setCurrentIndex(0)
        grid.set_background_paused(True)
        paths.append(empty)
        choose_button.click()
        qtbot.waitUntil(lambda: not grid._pending_import_roots)
        row = page._import_folders_layout.itemAt(9).widget()
        assert any('MP4, MKV, AVI' in label.text() for label in row.findChildren(QLabel))
        row = page._import_folders_layout.itemAt(0).widget()
        with qtbot.waitSignal(page.view_imported_requested):
            next(b for b in row.findChildren(QPushButton) if b.text() == 'View clips').click()

        failures = []
        monkeypatch.setattr(page, '_report_storage_change_failure', failures.append)
        monkeypatch.setattr(settings, 'save_settings', lambda: False)
        page._remove_import_folder(str(moved))
        assert failures and str(moved) in settings.get('imported_clip_folders')
        assert str(moved) in SettingsManager().get('imported_clip_folders')
        assert page._import_folders_layout.count() == 10
        grid.set_background_paused(False)
        grid.filter_combo.setCurrentIndex(grid.filter_combo.findText('Imported clips'))
        wait_for_count(10)
        assert {Path(r.path) for r in grid._filtered_records()} >= {
            moved / p.relative_to(custom) for p in originals[:2]}
        assert not list(owned.rglob('*.mp4'))
    finally:
        grid.shutdown()
