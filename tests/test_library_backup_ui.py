"""Focused Qt contracts for the library backup controller and panel."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QObject, Signal

from core.library_backup import BackupProgress, BackupResult, VerifiedBackup
from ui.library_backup import LibraryBackupController, LibraryBackupPanel, _result_text


class _Settings:
    def __init__(self, root):
        self.values = {
            "clips_directory": str(root),
            "imported_clip_folders": [],
        }

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Main(QObject):
    _ui_call = Signal(object)

    def __init__(self, root):
        super().__init__()
        self._ui_call.connect(lambda callback: callback())
        self.settings_manager = _Settings(root)
        self.clip_metadata_manager = object()
        self._file_operations_ready = True
        self._shutdown_requested = False
        self._shutdown_complete = False
        self.clip_grid = SimpleNamespace(refresh_after_backup_restore=lambda: None)
        self.library_storage_controller = SimpleNamespace(request_refresh=lambda **_kwargs: None)
        self.jobs = []
        self.released = []
        self.prepare_calls = 0
        self.capture_calls = 0

    def _prepare_backup_admission(self):
        self.prepare_calls += 1
        return ""

    def _capture_backup_admission_snapshot(self):
        self.capture_calls += 1
        return ""

    def _reserve_clip_paths(self, paths):
        token = {"paths": tuple(paths)}
        self.jobs.append(token)
        return token, ""

    def _release_clip_paths(self, token):
        self.released.append(token)

    def _launch_file_worker(self, work):
        work()


class _FakeService:
    def __init__(self, _manager, _root, **_kwargs):
        pass

    def create_backup(self, destination, *, options, cancel, progress):
        progress(BackupProgress("copy", 1, 1, 4, "done"))
        return BackupResult(str(destination), True, 1, 1, ())


class _AsyncService(_FakeService):
    def create_backup(self, destination, *, options, cancel, progress):
        progress(BackupProgress("copy", 1, 2, 4, "copying"))
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if cancel.is_set():
                raise InterruptedError("cancelled")
            time.sleep(0.005)
        progress(BackupProgress("copy", 2, 2, 8, "done"))
        return BackupResult(str(destination), True, 1, 1, ())


class _ClaimingService:
    def __init__(self, _manager, _root, *, reserve, release, **_kwargs):
        self.reserve = reserve
        self.release = release

    def create_backup(self, destination, *, options, cancel, progress):
        assert self.reserve(("source.mp4",))
        raise OSError("source changed")


class _VerifyService(_FakeService):
    def verify_backup(self, package, *, cancel, progress):
        return VerifiedBackup(str(package), "a" * 64,
                              {"entries": [], "files": [], "excluded": []})


class _OverlappingClaimingService(_ClaimingService):
    def create_backup(self, destination, *, options, cancel, progress):
        assert self.reserve(("source.mp4", "alias.mp4"))
        assert self.reserve(("alias.mp4", "second.mp4"))
        raise OSError("source changed")


class _AsyncMain(_Main):
    def _launch_file_worker(self, work):
        threading.Thread(target=work, daemon=True).start()


class _CancelDuringReserveMain(_Main):
    def __init__(self, root):
        super().__init__(root)
        self.controller = None
        self.foreign_token = object()

    def _reserve_clip_paths(self, paths):
        token = super()._reserve_clip_paths(paths)[0]
        if self.controller is not None:
            self.controller._cancel.set()
        return token, ""


class _ReleaseErrorMain(_Main):
    def _release_clip_paths(self, token):
        self.released.append(token)
        raise OSError("release failed")


class _CompletedClaimService(_ClaimingService):
    def create_backup(self, destination, *, options, cancel, progress):
        try:
            assert self.reserve(("source.mp4",))
            return BackupResult(str(destination), True, 1, 1, ())
        finally:
            self.release(("source.mp4",))

    def restore_backup(self, package, destination, *, cancel, progress,
                       expected_manifest_sha256=None):
        try:
            assert self.reserve(("source.mp4",))
            return BackupResult(str(package), True, 1, 1, (),
                                destination_root=str(destination))
        finally:
            self.release(("source.mp4",))


def test_controller_uses_one_worker_and_publishes_coalesced_result(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _FakeService)
    main = _Main(tmp_path / "clips")
    controller = LibraryBackupController(main)
    results = []
    controller.operation_finished.connect(results.append)
    qtbot.addWidget(LibraryBackupPanel(controller))

    assert controller.start_create(tmp_path / "package")
    assert results and results[-1].completed
    assert not controller.busy
    controller.close()


def test_panel_pairs_primary_actions_with_their_resume_controls(qtbot, tmp_path):
    controller = LibraryBackupController(_Main(tmp_path / "clips"))
    panel = LibraryBackupPanel(controller)
    qtbot.addWidget(panel)

    assert panel.create_btn.text() == "Create backup"
    assert panel.resume_backup_btn.text() == "Resume backup"
    assert panel.verify_btn.text() == "Verify backup"
    assert panel.restore_btn.text() == "Restore backup"
    assert panel.resume_restore_btn.text() == "Resume restore"
    assert panel.cancel_btn.text() == "Cancel"
    assert not panel.cancel_btn.isEnabled()
    panel.resize(350, 400)
    panel.show()
    qtbot.waitUntil(lambda: panel.create_btn.height() > panel.resume_backup_btn.height())
    assert not panel.create_btn.icon().isNull()
    assert not panel.restore_btn.icon().isNull()
    assert panel.create_btn.y() == panel.restore_btn.y()
    assert panel.create_btn.x() == panel.resume_backup_btn.x()
    assert panel.restore_btn.x() == panel.resume_restore_btn.x()
    assert panel.create_btn.y() < panel.resume_backup_btn.y()
    assert panel.restore_btn.y() < panel.resume_restore_btn.y()
    assert panel.include_imported_check.isChecked() is False
    assert panel.include_trash_check.isChecked() is True
    controller.close()


def test_controller_coalesces_one_physical_job_and_cancel_keeps_busy_until_done(
        qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _AsyncService)
    main = _AsyncMain(tmp_path / "clips")
    controller = LibraryBackupController(main)
    panel = LibraryBackupPanel(controller)
    qtbot.addWidget(panel)
    assert controller.start_create(tmp_path / "package")
    assert not controller.start_create(tmp_path / "second")
    assert controller.busy
    assert panel.cancel_btn.isEnabled()
    panel.cancel_btn.click()
    assert not panel.cancel_btn.isEnabled()
    assert not panel.create_btn.isEnabled()
    qtbot.waitUntil(lambda: not controller.busy, timeout=2000)
    controller.close()


def test_root_change_suppresses_stale_completion_and_dirty_flush_refuses_start(
        qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _AsyncService)
    main = _AsyncMain(tmp_path / "clips")
    controller = LibraryBackupController(main)
    finished = []
    controller.operation_finished.connect(finished.append)
    assert controller.start_create(tmp_path / "package")
    main.settings_manager.values["clips_directory"] = str(tmp_path / "changed")
    controller.root_changed()
    qtbot.waitUntil(lambda: not controller.busy, timeout=2000)
    assert not finished
    controller.close()

    refusing = _Main(tmp_path / "other")
    refusing._prepare_backup_admission = lambda: "dirty editor"
    blocked = LibraryBackupController(refusing)
    assert not blocked.start_create(tmp_path / "blocked")
    blocked.close()


def test_controller_releases_only_its_own_claim_after_worker_failure(qtbot, tmp_path,
                                                                       monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _ClaimingService)
    main = _Main(tmp_path / "clips")
    controller = LibraryBackupController(main)
    errors = []
    controller.error.connect(errors.append)
    assert controller.start_create(tmp_path / "package")
    assert errors
    assert len(main.released) == 1
    assert main.released[0] is main.jobs[0]
    controller.close()


def test_verify_refreshes_ram_admission_without_editor_flush(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _VerifyService)
    main = _Main(tmp_path / "clips")
    controller = LibraryBackupController(main)
    assert controller.start_verify(tmp_path / "package")
    assert main.capture_calls >= 1
    assert main.prepare_calls == 0
    controller.close()


def test_overlapping_claim_batches_use_one_owned_token(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService",
                        _OverlappingClaimingService)
    main = _Main(tmp_path / "clips")
    controller = LibraryBackupController(main)
    assert controller.start_create(tmp_path / "package")
    assert len(main.jobs) == 2
    assert len(main.released) == 2
    controller.close()


def test_cancel_during_reserve_releases_own_token_only(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _ClaimingService)
    main = _CancelDuringReserveMain(tmp_path / "clips")
    controller = LibraryBackupController(main)
    main.controller = controller
    main.jobs.append(main.foreign_token)

    assert controller.start_create(tmp_path / "package")
    assert len(main.released) == 1
    assert main.released[0] is main.jobs[1]
    assert main.foreign_token not in main.released
    controller.close()


def test_completed_result_survives_release_failure_and_refreshes(qtbot, tmp_path,
                                                                  monkeypatch):
    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _CompletedClaimService)
    main = _ReleaseErrorMain(tmp_path / "clips")
    refreshes = []
    main.clip_grid.refresh_after_backup_restore = lambda: refreshes.append(True)
    controller = LibraryBackupController(main)
    results = []
    errors = []
    controller.operation_finished.connect(results.append)
    controller.error.connect(errors.append)

    assert controller.start_restore(tmp_path / "package", tmp_path / "restored")
    assert results and results[-1].completed
    assert "Exclusions and notes (1)" in _result_text(results[-1])
    assert not errors
    assert refreshes == [True]
    controller.close()


def test_restore_preflight_confirms_counts_before_restore_write(qtbot, tmp_path,
                                                                  monkeypatch):
    order = []

    class _RestoreService(_FakeService):
        def verify_backup(self, package, *, cancel, progress):
            order.append("verify")
            return VerifiedBackup(str(package), "a" * 64,
                                  {"entries": [{}], "files": [], "excluded": []})

        def restore_backup(self, package, destination, *, cancel, progress,
                           expected_manifest_sha256=None):
            assert expected_manifest_sha256 == "a" * 64
            order.append(("restore", str(destination)))
            return BackupResult(str(package), True, 1, 0, (), destination_root=str(destination))

    monkeypatch.setattr("ui.library_backup.LibraryBackupService", _RestoreService)
    monkeypatch.setattr("ui.library_backup.FthrMessageDialog.question",
                        classmethod(lambda *_args, **_kwargs: True))
    main = _Main(tmp_path / "clips")
    controller = LibraryBackupController(main)
    finished = []
    controller.operation_finished.connect(finished.append)
    assert controller.start_restore(tmp_path / "package")
    assert order and order[0] == "verify"
    assert order[1][0] == "restore"
    assert finished and all(not isinstance(item, VerifiedBackup) for item in finished)
    controller.close()


def test_result_text_separates_exclusions_from_restore_notes():
    result = BackupResult(
        "package", False, 1, 1,
        ("clip: source unavailable", "restore metadata committed; manager reload pending",
         "restore cleanup pending"),
        restore_checkpoint_path="checkpoint.json")
    text = _result_text(result)
    assert "Exclusions and notes (3)" in text
    assert "clip: source unavailable" in text
    assert "restore metadata committed; manager reload pending" in text
    assert "restore cleanup pending" in text
    assert "Resume checkpoint: checkpoint.json" in text
