"""Controller and settings-state tests for the storage boundary."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from core.library_storage import (
    StorageBindingsSnapshot, StorageFile, StorageInventory, StoragePolicy,
)
from core.settings_manager import SettingsManager
from ui.library_storage import (
    LibraryStorageController, LibraryStoragePanel, _bytes_text, _parse_bytes_text,
    _storage_limit_text,
)


class _Settings:
    def __init__(self, root):
        self.values = {
            "clips_directory": str(root),
            "imported_clip_folders": [],
            "storage_policies": {},
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def storage_policy_for(self, root, *, strict=False):
        key = os.path.normcase(os.path.abspath(str(root)))
        return StoragePolicy.from_mapping(self.values["storage_policies"].get(key))

    def save_storage_policy(self, root, policy):
        key = os.path.normcase(os.path.abspath(str(root)))
        self.values["storage_policies"][key] = policy.to_dict()
        return True


class _Metadata:
    def storage_bindings_snapshot(self):
        return StorageBindingsSnapshot()

    def list_trash(self):
        return ()


class _Main(QObject):
    _ui_call = Signal(object)

    def __init__(self, root):
        super().__init__()
        self._ui_call.connect(lambda callback: callback())
        self.settings_manager = _Settings(root)
        self.clip_metadata_manager = _Metadata()
        self._file_operations_ready = True
        self._manual_record_state = "idle"
        self._screenshot_inflight = False
        self._active_clip_viewer = None
        self._parked_editor_shell = None
        self._editor_sessions = ()
        self.clip_grid = None
        self.upload_manager = None

    def _launch_file_worker(self, work):
        work()


def test_controller_refresh_publishes_real_inventory(qtbot, tmp_path):
    root = tmp_path / "clips"
    root.mkdir()
    clip = root / "Desktop" / "sample.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"clip")
    main = _Main(root)
    controller = LibraryStorageController(main)
    previews = []
    controller.preview_ready.connect(previews.append)
    assert controller.request_refresh(force=True)
    assert controller.inventory is not None
    assert controller.inventory.root == os.path.abspath(str(root))
    assert previews and previews[-1].complete
    controller.close()


def test_reopening_storage_reuses_recent_default_inventory(qtbot, tmp_path, monkeypatch):
    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    scans = []
    controller.inventory_changed.connect(scans.append)

    controller.set_visible(True)
    controller.set_visible(False)
    controller.set_visible(True)
    assert len(scans) == 1

    assert controller.request_refresh(force=True)
    assert len(scans) == 2

    controller.set_visible(False)
    monkeypatch.setattr(controller, "_REOPEN_FRESHNESS_SECONDS", 0)
    controller.set_visible(True)
    assert len(scans) == 3
    controller.close()


@pytest.mark.parametrize("change", ["imported", "policy", "root"])
def test_reopening_storage_refreshes_changed_context(qtbot, tmp_path, change):
    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    scans = []
    controller.inventory_changed.connect(scans.append)
    controller.set_visible(True)
    controller.set_visible(False)

    if change == "imported":
        main.settings_manager.values["imported_clip_folders"] = [str(tmp_path / "imported")]
    elif change == "policy":
        main.settings_manager.save_storage_policy(tmp_path, StoragePolicy(max_library_bytes=1000))
    else:
        controller.root_changed()
    controller.set_visible(True)

    assert len(scans) == 2
    controller.close()


@pytest.mark.parametrize("policy", [
    StoragePolicy(max_library_bytes=1000),
    StoragePolicy(trash_retention_days=7, automatic_trash_cleanup=True),
])
def test_storage_rules_refresh_on_reopen(qtbot, tmp_path, policy):
    main = _Main(tmp_path)
    main.settings_manager.save_storage_policy(tmp_path, policy)
    controller = LibraryStorageController(main)
    scans = []
    controller.inventory_changed.connect(scans.append)

    controller.set_visible(True)
    controller.set_visible(False)
    controller.set_visible(True)

    assert len(scans) == 2
    controller.close()


def test_repeated_storage_show_does_not_queue_second_inventory(qtbot, tmp_path, monkeypatch):
    main = _Main(tmp_path)
    jobs = []
    monkeypatch.setattr(main, "_launch_file_worker", jobs.append)
    controller = LibraryStorageController(main)
    controller.set_visible(True)
    controller.set_visible(True)

    assert len(jobs) == 1
    assert not controller._pending_refresh
    assert not controller.request_refresh(force=True)
    jobs.pop()()
    assert controller.inventory is not None
    qtbot.waitUntil(lambda: len(jobs) == 1)
    jobs.pop()()
    assert not jobs
    controller.close()


def test_storage_reopen_replaces_cancelled_scan(qtbot, tmp_path, monkeypatch):
    main = _Main(tmp_path)
    jobs = []
    monkeypatch.setattr(main, "_launch_file_worker", jobs.append)
    controller = LibraryStorageController(main)
    controller.set_visible(True)
    controller.set_visible(False)
    controller.set_visible(True)

    assert len(jobs) == 1
    jobs.pop()()
    assert controller.inventory is None
    qtbot.waitUntil(lambda: len(jobs) == 1)
    jobs.pop()()
    assert controller.inventory is not None
    controller.close()


def test_global_busy_state_defers_cleanup_without_filesystem_probe(qtbot, tmp_path):
    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    main._manual_record_state = "recording"
    assert not controller._storage_busy_hint()
    main._file_operations_ready = False
    assert controller._storage_busy_hint()
    main._file_operations_ready = True
    controller.close()


def test_busy_path_snapshot_keeps_unrelated_clip_paths_available(qtbot, tmp_path):
    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    viewer_path = tmp_path / "viewer.mp4"
    record_path = tmp_path / "recording.mp4"
    main._active_clip_viewer = type("Viewer", (), {"clip_path": str(viewer_path)})()
    main._manual_record_path = str(record_path)
    busy = set(controller._busy_paths_snapshot())
    assert {str(viewer_path), str(record_path)} <= busy
    assert not controller._storage_busy_hint()
    controller.close()


def test_storage_policy_save_rolls_back_when_persistence_raises(tmp_path):
    manager = SettingsManager.__new__(SettingsManager)
    manager.settings = {
        "storage_policies": {},
        "clips_directory": str(tmp_path),
    }
    manager.save_settings = lambda: (_ for _ in ()).throw(OSError("disk full"))
    policy = StoragePolicy(max_library_bytes=123)
    try:
        manager.save_storage_policy(tmp_path, policy)
    except OSError:
        pass
    else:
        raise AssertionError("save_storage_policy must propagate persistence errors")
    assert manager.settings["storage_policies"] == {}


def test_storage_display_uses_rounded_decimal_usage_and_exact_limits():
    assert _bytes_text(1024) == "1 KB"
    assert _bytes_text(1024 ** 2) == "1 MB"
    assert _bytes_text(1024 ** 3) == "1 GB"
    assert _bytes_text(3 * 10 ** 9 + 700 * 10 ** 6) == "4 GB"
    assert _storage_limit_text(0) == "Off"
    assert _storage_limit_text(500_000_000) == "500 MB"
    assert _storage_limit_text(2_000_000_000) == "2 GB"
    for amount in (1, 1000, 10 ** 9, 10 ** 12):
        assert _parse_bytes_text(_storage_limit_text(amount)) == amount
    assert _parse_bytes_text("2 GiB") == 2 * 1024 ** 3
    assert _parse_bytes_text("OFF") == 0


def test_panel_reloads_root_and_rejects_stale_save_binding(qtbot, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    main = _Main(first)
    controller = LibraryStorageController(main)
    panel = LibraryStoragePanel(main.settings_manager, controller)
    assert panel._root == str(first)

    main.settings_manager.values["clips_directory"] = str(second)
    controller.root_changed()
    assert panel._root == str(second)

    third = tmp_path / "third"
    main.settings_manager.values["clips_directory"] = str(third)
    panel.max_library_bytes_edit.setText("1 GiB")
    panel._save_policy()
    assert panel._root == str(third)
    assert "changed" in panel.status_label.text().casefold()
    assert not main.settings_manager.values["storage_policies"]
    controller.close()


def test_panel_uses_discrete_stops_and_preserves_exact_typed_limits(qtbot, tmp_path, monkeypatch):
    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    panel = LibraryStoragePanel(main.settings_manager, controller)
    qtbot.addWidget(panel)
    cap = 7_250_000_000
    monkeypatch.setattr("ui.library_storage.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=cap))
    assert panel.max_library_bytes_edit.text() == "Off"
    assert panel.trash_max_bytes_edit.text() == "Off"
    assert panel.trash_retention_days_edit.text() == "No auto delete"
    assert not panel.max_library_bytes_slider.isEnabled()
    panel._show_inventory(StorageInventory(str(tmp_path), {}, free_bytes=cap))
    assert panel.max_library_bytes_slider.isEnabled()
    assert panel.max_library_bytes_slider.maximum() == 5

    panel.max_library_bytes_slider.setValue(1)
    assert panel.max_library_bytes_edit.text() == "500 MB"
    panel.max_library_bytes_slider.setValue(panel.max_library_bytes_slider.maximum())
    assert panel.max_library_bytes_edit.text() == "7 GB"
    panel.max_library_bytes_edit.setText("3 B")
    assert panel.max_library_bytes_slider.value() == 1
    panel.trash_max_bytes_slider.setValue(2)
    assert panel.trash_max_bytes_edit.text() == "1 GB"
    panel.trash_retention_days_slider.setValue(3)
    assert panel.trash_retention_days_edit.text() == "7"
    assert panel.preview_summary_label.text() == "Review available after the storage limits are saved."
    assert not panel.preview_btn.isEnabled()

    qtbot.waitUntil(lambda: not panel._save_timer.isActive())
    policy = main.settings_manager.storage_policy_for(tmp_path)
    assert policy.max_library_bytes == 3
    assert policy.trash_max_bytes == 10 ** 9
    assert policy.trash_retention_days == 7
    assert panel.preview_btn.isEnabled()

    panel.trash_retention_days_slider.setValue(0)
    assert panel.trash_retention_days_edit.text() == "No auto delete"
    assert not panel.preview_btn.isEnabled()
    controller.close()


def test_panel_checks_fresh_free_space_and_preserves_existing_limit(qtbot, tmp_path, monkeypatch):
    main = _Main(tmp_path)
    saved_limit = 4 * 1024 ** 3 + 1
    main.settings_manager.save_storage_policy(tmp_path, StoragePolicy(max_library_bytes=saved_limit))
    controller = LibraryStorageController(main)
    panel = LibraryStoragePanel(main.settings_manager, controller)
    qtbot.addWidget(panel)
    panel._show_inventory(StorageInventory(str(tmp_path), {}, free_bytes=6 * 10 ** 9))
    requests = []

    def disk_usage(path):
        requests.append(path)
        return SimpleNamespace(free=1_750_000_000)

    monkeypatch.setattr("ui.library_storage.shutil.disk_usage", disk_usage)
    panel.trash_max_bytes_edit.setText("2 GB")
    qtbot.waitUntil(lambda: not panel._save_timer.isActive())
    assert requests == [str(tmp_path)]
    assert "exceeds available space. Try 1 GB or less." in panel.status_label.text()
    assert main.settings_manager.storage_policy_for(tmp_path).trash_max_bytes == 0
    assert panel.trash_max_bytes_edit.text() == "2 GB"
    assert panel.trash_max_bytes_slider.maximum() == 2

    panel.trash_max_bytes_edit.setText("1 GB")
    qtbot.waitUntil(lambda: not panel._save_timer.isActive())
    policy = main.settings_manager.storage_policy_for(tmp_path)
    assert policy.max_library_bytes == saved_limit
    assert policy.trash_max_bytes == 10 ** 9

    def unavailable(path):
        raise OSError(path)

    monkeypatch.setattr("ui.library_storage.shutil.disk_usage", unavailable)
    panel.max_library_bytes_edit.setText("500 MB")
    qtbot.waitUntil(lambda: not panel._save_timer.isActive())
    assert "could not be checked" in panel.status_label.text()
    assert not panel.max_library_bytes_slider.isEnabled()
    assert main.settings_manager.storage_policy_for(tmp_path) == policy

    panel.max_library_bytes_edit.setText("Off")
    qtbot.waitUntil(lambda: not panel._save_timer.isActive())
    assert main.settings_manager.storage_policy_for(tmp_path).max_library_bytes == 0
    controller.close()


@pytest.mark.parametrize("complete", [True, False], ids=["complete", "incomplete"])
def test_preview_summary_survives_storage_ready_status(qtbot, tmp_path, complete):
    root = tmp_path / "clips"
    root.mkdir()
    main = _Main(root)
    policy = StoragePolicy(max_library_bytes=1)
    key = os.path.normcase(os.path.abspath(str(root)))
    main.settings_manager.values["storage_policies"][key] = policy.to_dict()
    controller = LibraryStorageController(main)
    panel = LibraryStoragePanel(main.settings_manager, controller)
    assert panel.preview_summary_label.text() == "No cleanup preview."
    protected = StorageFile(
        str(root / "Desktop" / "protected.mp4"),
        {"st_dev": 1, "st_ino": 2, "size": 10, "mtime_ns": 1},
        "active", 10, 1, protected=True,
    )
    inventory = StorageInventory(
        str(root), {}, files=(protected,), active_bytes=10, complete=complete,
    )

    controller._inventory_done((
        controller._generation, str(root), policy, (), inventory, None,
    ))

    assert panel.status_label.text() == "Storage ready"
    summary = panel.preview_summary_label.text()
    assert "0 clips" in summary
    assert "estimated reclaim 0 B" in summary
    assert "remaining 9 B" in summary
    if complete:
        assert "incomplete" not in summary.casefold()
        assert "cleanup unavailable" not in summary.casefold()
    else:
        assert "incomplete" in summary.casefold()
        assert "cleanup unavailable" in summary.casefold()
    controller.close()


def test_library_limit_commits_typed_value_after_editing_and_reports_failure(
        qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt

    main = _Main(tmp_path)
    controller = LibraryStorageController(main)
    panel = LibraryStoragePanel(main.settings_manager, controller)
    qtbot.addWidget(panel)
    attempts = []

    def reject_save(root, policy):
        attempts.append(policy)
        return False

    monkeypatch.setattr(main.settings_manager, 'save_storage_policy', reject_save)
    assert not panel._save_timer.isActive()
    field = panel.trash_retention_days_edit
    field.selectAll()
    qtbot.keyClicks(field, '30')
    assert not panel._save_timer.isActive()
    assert attempts == []

    qtbot.keyClick(field, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: bool(attempts))
    assert len(attempts) == 1 and attempts[0].trash_retention_days == 30
    assert 'could not be saved' in panel.status_label.text()
    assert main.settings_manager.storage_policy_for(tmp_path).trash_retention_days == 0
    assert not panel.preview_btn.isEnabled()
    controller.close()
