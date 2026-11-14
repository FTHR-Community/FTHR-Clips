"""Settings controls and worker bridge for library backup and restore."""

from __future__ import annotations

import os
import threading
from dataclasses import replace
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGridLayout, QLabel, QProgressBar,
    QPushButton, QVBoxLayout, QWidget,
)

from core.library_backup import (
    BackupOptions, BackupProgress, BackupResult, LibraryBackupService,
    VerifiedBackup,
)
from core.library_index import canonical_media_path
from core.settings_manager import clips_directory_lexical_from
from ui.dialogs import FthrMessageDialog
from ui.icons import IconButton
from ui.style import (
    Colors, Fonts, button_outline_qss, button_secondary_qss, checkbox_qss,
    label_body, set_theme_style,
)


def _path_text(value):
    return str(value) if value else ""


def _result_text(result):
    def details(items, label):
        if not items:
            return ""
        value = "; ".join(str(item) for item in items[:3])
        if len(items) > 3:
            value += "; ..."
        return f" {label} ({len(items)}): {value}."

    if isinstance(result, VerifiedBackup):
        text = (f"Backup verified: {result.clips} clips, {result.files} files; "
                f"{len(result.excluded)} excluded.")
        if result.package_path:
            text += f" Package: {result.package_path}."
        return text + details(result.excluded, "Excluded")
    if not isinstance(result, BackupResult):
        return str(result)
    state = "completed" if result.completed else "incomplete"
    extra = " (already imported)" if result.already_imported else ""
    text = (f"Backup/restore {state}{extra}: {result.clips} clips, "
            f"{result.files} files.")
    if result.package_path:
        text += f" Package: {result.package_path}."
    if result.destination_root:
        text += f" Destination: {result.destination_root}."
    text += details(result.excluded, "Exclusions and notes")
    if result.restore_checkpoint_path:
        text += f" Resume checkpoint: {result.restore_checkpoint_path}."
    return text


class _RestoreConfirmationDeclined(Exception):
    """Internal worker result for a user-declined restore confirmation."""


class LibraryBackupController(QObject):
    """Own one cancellable backup worker and publish only current results."""

    busy_changed = Signal(bool)
    cancel_requested = Signal()
    progress_changed = Signal(object)
    status_changed = Signal(str)
    operation_finished = Signal(object)
    error = Signal(str)
    availability_changed = Signal(bool)

    def __init__(self, main_window, parent=None):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.settings_manager = getattr(main_window, "settings_manager", None)
        self._cancel = threading.Event()
        self._generation = 0
        self._busy = False
        self._closed = False
        self._operation = None
        self._chooser_operation = None
        self._operation_details = ()
        self._active_service = None
        self._context = None
        self._context_invalidated = False
        self._claims_lock = threading.Lock()
        self._claims = {}
        self._progress_lock = threading.Lock()
        self._latest_progress = None
        self._progress_generation = None
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(50)
        self._progress_timer.timeout.connect(self._flush_progress)

    @property
    def busy(self):
        return self._busy

    @property
    def cancellable(self):
        return (self._busy and self._operation != "chooser"
                and not self._closed and not self._cancel.is_set())

    @property
    def operation(self):
        """Return the logical operation, including an open file chooser."""
        if self._operation == "chooser":
            return self._chooser_operation
        return self._operation

    def _context_snapshot(self):
        if self.settings_manager is None:
            raise OSError("Backup settings are unavailable")
        root = str(clips_directory_lexical_from(self.settings_manager))
        imported = tuple(self.settings_manager.get("imported_clip_folders", ()) or ())
        return root, imported

    def _current_root(self):
        if self._context is None or self._context_invalidated:
            raise OSError("library context changed during backup")
        if self._closed:
            raise OSError("backup controller is closed")
        return self._context[0]

    def _make_service(self, root, imported, cancel, *, release_warnings=None):
        owned_keys = {}

        def release_token_ids(token_ids):
            releaser = getattr(self.main_window, "_release_clip_paths", None)
            if not callable(releaser):
                return ()
            failures = []
            for token_id in token_ids:
                with self._claims_lock:
                    token = self._claims.pop(token_id, None)
                if token is None:
                    continue
                try:
                    releaser(token)
                except Exception as error:  # Continue independent token release.
                    failures.append(error)
            if failures:
                if release_warnings is not None:
                    release_warnings.extend(str(error) for error in failures)
            return ()

        def path_map(paths):
            mapped = {}
            for value in paths:
                text = str(value)
                mapped.setdefault(canonical_media_path(text), text)
            return mapped

        def reserve(paths):
            paths = tuple(str(path) for path in paths)
            requested = path_map(paths)
            self._refresh_worker_admission(cancel)
            checker = getattr(self.main_window, "_backup_admission_reason", None)
            if callable(checker):
                reason = checker(paths)
                if reason:
                    raise OSError(str(reason))
            delta = [key for key in requested if key not in owned_keys]
            if not delta:
                return True
            reserve_method = getattr(self.main_window, "_reserve_clip_paths", None)
            if not callable(reserve_method):
                raise OSError("Backup admission is unavailable")
            if cancel.is_set() or self._closed or self._context_invalidated:
                raise InterruptedError("backup claim was cancelled")
            token_result = reserve_method(tuple(requested[key] for key in delta))
            token, reason = (token_result if isinstance(token_result, tuple)
                             and len(token_result) == 2 else (token_result, ""))
            if token is None:
                return False
            token_id = id(token)
            with self._claims_lock:
                self._claims[token_id] = token
            for key in delta:
                owned_keys[key] = token_id
            if reason:
                for key in delta:
                    owned_keys.pop(key, None)
                release_token_ids((token_id,))
                return False
            try:
                self._refresh_worker_admission(cancel)
                if callable(checker):
                    reason = checker(paths)
                    if reason:
                        raise OSError(str(reason))
            except Exception as claim_error:  # Release this run's token before propagating.
                for key in delta:
                    owned_keys.pop(key, None)
                release_token_ids((token_id,))
                raise claim_error
            return True

        def release(paths):
            requested = path_map(paths)
            token_ids = set()
            for key in requested:
                token_id = owned_keys.pop(key, None)
                if token_id is not None:
                    token_ids.add(token_id)
            still_owned = set(owned_keys.values())
            token_ids.difference_update(still_owned)
            with self._claims_lock:
                token_ids.intersection_update(self._claims)
            release_token_ids(tuple(token_ids))

        return LibraryBackupService(
            getattr(self.main_window, "clip_metadata_manager", None), root,
            imported_roots=imported, reserve=reserve, release=release,
            current_root=self._current_root,
        )

    def _release_owned_claims(self):
        with self._claims_lock:
            tokens = tuple(self._claims.values())
            self._claims.clear()
        releaser = getattr(self.main_window, "_release_clip_paths", None)
        failures = []
        if callable(releaser):
            for token in tokens:
                try:
                    releaser(token)
                except Exception as error:  # Continue releasing every owned token.
                    failures.append(error)
        return failures

    def _dispatch(self, callback):
        bridge = getattr(self.main_window, "_ui_call", None)
        emitter = getattr(bridge, "emit", None)
        if callable(emitter):
            try:
                emitter(callback)
                return True
            except RuntimeError:
                # Qt teardown owns the bridge; worker cleanup still runs below.
                return False
        return False

    def _refresh_worker_admission(self, cancel):
        """Refresh the GUI-owned RAM admission mirror before a worker claim."""
        capture = getattr(self.main_window, "_capture_backup_admission_snapshot", None)
        if not callable(capture):
            return
        if cancel.is_set() or self._closed or self._context_invalidated:
            raise InterruptedError("backup admission was cancelled")
        finished = threading.Event()
        outcome = {}

        def capture_on_gui():
            if cancel.is_set() or self._closed or self._context_invalidated:
                outcome["error"] = "backup admission was cancelled"
                finished.set()
                return
            try:
                if self._context_snapshot() != self._context:
                    outcome["error"] = "library context changed during backup"
                else:
                    outcome["reason"] = capture() or ""
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                outcome["error"] = str(error)
            except Exception as error:  # GUI admission failures must wake the worker.
                outcome["error"] = str(error)
            finally:
                finished.set()

        if not self._dispatch(capture_on_gui):
            raise InterruptedError("backup admission bridge unavailable")
        while not finished.wait(0.05):
            if cancel.is_set() or self._closed or self._context_invalidated:
                raise InterruptedError("backup admission was cancelled")
        if cancel.is_set() or self._closed or self._context_invalidated:
            raise InterruptedError("backup admission was cancelled")
        if outcome.get("error"):
            raise OSError(outcome["error"])
        if outcome.get("reason"):
            raise OSError(str(outcome["reason"]))

    def _publish_progress(self, generation, progress):
        with self._progress_lock:
            self._latest_progress = progress
            self._progress_generation = generation

    def _flush_progress(self):
        with self._progress_lock:
            progress = self._latest_progress
            generation = self._progress_generation
            self._latest_progress = None
        if (progress is not None and generation == self._generation
                and not self._closed and not self._context_invalidated):
            self.progress_changed.emit(progress)

    def _prepare_admission(self, operation="create", *, flush=True):
        source_operation = operation in {"create", "resume_backup"}
        prepare = (getattr(self.main_window, "_prepare_backup_admission", None)
                   if flush and source_operation else None)
        capture = getattr(self.main_window, "_capture_backup_admission_snapshot", None)
        callback = prepare if callable(prepare) else capture
        if not callable(callback):
            callback = getattr(self.main_window, "_prepare_backup_admission", None)
        if callable(callback):
            reason = callback()
            if reason:
                raise OSError(str(reason))

    def _confirm_restore(self, verified, cancel):
        """Ask on the GUI thread while the same worker retains its slot."""
        context = self._context
        if context is None:
            raise InterruptedError("library context is unavailable")
        destination = os.path.join(context[0], f"Restored-{uuid4()}")
        self._operation_details = (*self._operation_details,
                                   f"Destination: {destination}.")
        finished = threading.Event()
        decision = {}

        def ask():
            if cancel.is_set() or self._closed or self._context_invalidated:
                decision["cancelled"] = True
                finished.set()
                return
            try:
                if self._context_snapshot() != context:
                    decision["cancelled"] = True
                else:
                    decision["accepted"] = FthrMessageDialog.question(
                        self.main_window, "Restore backup",
                        f"Restore {verified.clips} clips and {verified.files} files "
                        f"into this new Clips folder?\n{destination}",
                        plain_text=True)
                    if (cancel.is_set() or self._closed
                            or self._context_invalidated
                            or self._context_snapshot() != context):
                        decision["cancelled"] = True
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                decision["error"] = str(error)
            except Exception as error:  # Dialog failures abort before restore writes.
                decision["error"] = str(error)
            finally:
                finished.set()

        if not self._dispatch(ask):
            raise InterruptedError("restore confirmation bridge unavailable")
        while not finished.wait(0.05):
            if cancel.is_set() or self._closed or self._context_invalidated:
                raise InterruptedError("restore confirmation was cancelled")
        if cancel.is_set() or self._closed or self._context_invalidated:
            raise InterruptedError("restore confirmation was cancelled")
        if decision.get("error"):
            raise OSError(decision["error"])
        if decision.get("cancelled"):
            raise InterruptedError("restore confirmation was cancelled")
        if not decision.get("accepted"):
            raise _RestoreConfirmationDeclined("restore was not confirmed")
        return destination

    def set_available(self, available):
        self.availability_changed.emit(bool(available))

    def _start(self, operation, runner, *, continuation=False, details=()):
        if self._closed or (self._busy and not continuation):
            return False
        self._operation_details = tuple(str(item) for item in details)
        try:
            context = self._context_snapshot()
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            message = " ".join((str(error), *self._operation_details))
            self._operation_details = ()
            self.error.emit(message)
            return False
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel = cancel
        self._active_service = None
        self._busy = True
        self._operation = operation
        self._chooser_operation = None
        self._context = context
        self._context_invalidated = False
        if not continuation:
            self.busy_changed.emit(True)
        self.status_changed.emit(f"Starting {operation}...")
        self._progress_timer.start()
        try:
            self._prepare_admission(operation)
            if (cancel.is_set() or self._closed
                    or self._context_invalidated
                    or self._context_snapshot() != context):
                raise InterruptedError("backup admission was cancelled")
        except InterruptedError as interrupted:
            self._busy = False
            self._operation = None
            self._progress_timer.stop()
            message = " ".join((str(interrupted), *self._operation_details))
            self._operation_details = ()
            self.status_changed.emit(message)
            self.busy_changed.emit(False)
            return False
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._busy = False
            self._operation = None
            self._progress_timer.stop()
            message = " ".join((str(error), *self._operation_details))
            self._operation_details = ()
            self.busy_changed.emit(False)
            self.error.emit(message)
            return False

        def work():
            result = None
            failure = None
            release_warnings = []
            try:
                root, imported = context
                service = self._make_service(
                    root, imported, cancel, release_warnings=release_warnings)
                self._active_service = service
                result = runner(service, cancel, generation)
            except _RestoreConfirmationDeclined as declined:
                failure = declined
            except InterruptedError as interrupted:
                failure = str(interrupted)
            except Exception as error:  # worker boundary publishes a visible failure
                failure = str(error)
            finally:
                release_failures = self._release_owned_claims()
            release_failures.extend(release_warnings)
            if release_failures:
                release_note = str(release_failures[0])
                if isinstance(result, BackupResult) and result.completed:
                    result = replace(
                        result,
                        excluded=tuple(result.excluded) + (
                            f"reservation release cleanup warning: {release_note}",),
                    )
                    failure = None
                elif failure:
                    failure = f"{failure}; reservation release failed: {release_note}"
                else:
                    failure = f"reservation release failed: {release_note}"
            self._dispatch(lambda: self._finished(generation, result, failure))

        try:
            self.main_window._launch_file_worker(work)
        except Exception as error:  # worker startup failure leaves the slot reusable
            self._busy = False
            self._operation = None
            self._chooser_operation = None
            self._progress_timer.stop()
            message = " ".join((str(error), *self._operation_details))
            self._operation_details = ()
            self.busy_changed.emit(False)
            self.error.emit(message)
            return False
        return True

    def _finished(self, generation, result, failure):
        if generation != self._generation:
            return
        self._flush_progress()
        self._progress_timer.stop()
        operation = self._operation
        self._busy = False
        self._operation = None
        if self._closed:
            return

        def release_presentation_slot():
            if self._generation == generation:
                self._operation_details = ()
                self.busy_changed.emit(False)

        context_stale = self._context_invalidated
        try:
            context_stale = context_stale or self._context_snapshot() != self._context
        except (OSError, RuntimeError, TypeError, ValueError):
            context_stale = True
        checkpoint = getattr(self._active_service, "pending_restore_checkpoint", None)
        details = " ".join(self._operation_details)
        if context_stale:
            message = "Backup operation cancelled after a library change."
            if details:
                message += f" {details}"
            if checkpoint:
                message += f" Resume checkpoint: {checkpoint}."
            self.status_changed.emit(message)
            release_presentation_slot()
            return
        if failure:
            if isinstance(failure, _RestoreConfirmationDeclined):
                message = "Restore cancelled before any files were written."
                if details:
                    message += f" {details}"
                self.status_changed.emit(message)
            elif self._cancel.is_set():
                message = "Backup operation cancelled."
                if details:
                    message += f" {details}"
                if checkpoint:
                    message += f" Resume checkpoint: {checkpoint}."
                elif failure:
                    message += f" {failure}"
                self.status_changed.emit(message)
            else:
                if checkpoint:
                    failure = f"{failure} Resume checkpoint: {checkpoint}"
                if details:
                    failure = f"{failure} {details}"
                self.error.emit(failure)
        elif result is not None:
            self.status_changed.emit(_result_text(result))
            self.operation_finished.emit(result)
            if operation in {"restore", "resume_restore"} and getattr(result, "completed", False):
                grid = getattr(self.main_window, "clip_grid", None)
                refresh = getattr(grid, "refresh_after_backup_restore", None)
                if callable(refresh):
                    refresh()
                storage = getattr(self.main_window, "library_storage_controller", None)
                if storage is not None:
                    storage.request_refresh(force=True)
                trash = getattr(self.main_window, "_library_trash_page", None)
                refresh_trash = getattr(trash, "refresh", None)
                if callable(refresh_trash):
                    refresh_trash()
        release_presentation_slot()

    def _run_simple(self, operation, method, *args, details=(), **kwargs):
        def run(service, cancel, generation):
            return getattr(service, method)(
                *args, cancel=cancel,
                progress=lambda value: self._publish_progress(generation, value),
                **kwargs)
        return self._start(operation, run, details=details)

    def _begin_chooser(self, operation="chooser"):
        if self._closed or self._busy:
            return None
        self._cancel = threading.Event()
        self._busy = True
        self._operation = "chooser"
        self._chooser_operation = operation
        self._context_invalidated = False
        self._operation_details = ()
        try:
            context = self._context_snapshot()
            self._context = context
            self._prepare_admission(operation, flush=False)
            if (self._cancel.is_set() or self._closed
                    or self._context_invalidated
                    or self._context_snapshot() != context):
                raise InterruptedError("backup chooser was cancelled")
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            self._busy = False
            self._operation = None
            self._chooser_operation = None
            self.busy_changed.emit(False)
            self.error.emit(str(error))
            return None
        self.busy_changed.emit(True)
        return context

    def _end_chooser(self, context):
        if self._operation != "chooser":
            return False
        cancelled = self._cancel.is_set() or self._closed or self._context_invalidated
        try:
            current = self._context_snapshot()
        except (OSError, RuntimeError, TypeError, ValueError):
            current = None
        if not cancelled and current == context:
            try:
                self._prepare_admission(self._chooser_operation, flush=False)
            except (OSError, RuntimeError, TypeError, ValueError):
                cancelled = True
        self._busy = False
        self._operation = None
        self._chooser_operation = None
        self.busy_changed.emit(False)
        if cancelled:
            self.status_changed.emit("Backup chooser cancelled.")
            return False
        if cancelled or current != context:
            self.error.emit("Library folders changed while the chooser was open.")
            return False
        return True

    def start_create(self, destination=None, *, include_imported=False, include_trash=True):
        if destination is None:
            context = self._begin_chooser("create")
            if context is None:
                return False
            destination = QFileDialog.getExistingDirectory(
                self.main_window, "Choose backup destination parent")
            if not destination:
                self._end_chooser(context)
                return False
            destination = os.path.join(destination, f"fthr-backup-{uuid4()}")
            if not self._end_chooser(context):
                return False
        options = BackupOptions(include_imported=bool(include_imported),
                                include_trash=bool(include_trash))
        return self._run_simple(
            "create", "create_backup", destination,
            details=(f"Package: {destination}.",), options=options)

    def start_resume_backup(self, package=None):
        if package is None:
            context = self._begin_chooser("resume_backup")
            if context is None:
                return False
            package = QFileDialog.getExistingDirectory(
                self.main_window, "Choose backup package to resume")
            if not self._end_chooser(context):
                return False
        return bool(package) and self._run_simple(
            "resume_backup", "resume_backup", package,
            details=(f"Package: {package}.",))

    def start_verify(self, package=None):
        if package is None:
            context = self._begin_chooser("verify")
            if context is None:
                return False
            package = QFileDialog.getExistingDirectory(
                self.main_window, "Choose backup package to verify")
            if not self._end_chooser(context):
                return False
        return bool(package) and self._run_simple(
            "verify", "verify_backup", package,
            details=(f"Package: {package}.",))

    def start_restore(self, package=None, destination=None):
        if package is None:
            context = self._begin_chooser("restore")
            if context is None:
                return False
            package = QFileDialog.getExistingDirectory(
                self.main_window, "Choose backup package to restore")
            if not self._end_chooser(context):
                return False
        if not package:
            return False
        if destination is None:
            package = str(package)
            def run_confirmed_restore(service, cancel, generation):
                verified = service.verify_backup(
                    package, cancel=cancel,
                    progress=lambda value: self._publish_progress(generation, value))
                destination_path = self._confirm_restore(verified, cancel)
                return service.restore_backup(
                    package, destination_path, cancel=cancel,
                    progress=lambda value: self._publish_progress(generation, value),
                    expected_manifest_sha256=verified.manifest_sha256)

            return self._start(
                "restore", run_confirmed_restore,
                details=(f"Package: {package}.",))
        return self._run_simple(
            "restore", "restore_backup", package, destination,
            details=(f"Package: {package}.",
                     f"Destination: {destination}."))

    def start_resume_restore(self, checkpoint=None):
        if checkpoint is None:
            context = self._begin_chooser("resume_restore")
            if context is None:
                return False
            checkpoint, _ = QFileDialog.getOpenFileName(
                self.main_window, "Choose restore checkpoint", "",
                "Restore checkpoint (*.json)")
            if not self._end_chooser(context):
                return False
        return bool(checkpoint) and self._run_simple(
            "resume_restore", "resume_restore", checkpoint,
            details=(f"Checkpoint: {checkpoint}.",))

    def cancel(self):
        if self._busy:
            self._cancel.set()
            self.cancel_requested.emit()
            self.status_changed.emit("Stopping backup operation...")
            return True
        return False

    def root_changed(self, *_args):
        if self._busy:
            self._context_invalidated = True
            self._cancel.set()
            self.cancel_requested.emit()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.cancel()
        self._progress_timer.stop()


class LibraryBackupPanel(QWidget):
    """Backup and restore controls for Clip Settings."""

    def __init__(self, controller: LibraryBackupController, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._available = False
        self.setObjectName("libraryBackupPanel")
        self._make_ui()
        controller.busy_changed.connect(self._set_busy)
        controller.cancel_requested.connect(self._disable_cancel)
        controller.progress_changed.connect(self._show_progress)
        controller.status_changed.connect(self._show_status)
        controller.operation_finished.connect(self._show_result)
        controller.error.connect(self._show_error)
        controller.availability_changed.connect(self._set_available)
        self._set_available(getattr(
            controller.main_window, "_file_operations_ready", False))
        self._set_busy(controller.busy)

    def _make_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        title = QLabel("Library backup and restore")
        set_theme_style(title, lambda: label_body(Colors.TEXT, Fonts.SIZE_H3))
        layout.addWidget(title)
        self.include_imported_check = QCheckBox("Include imported originals")
        self.include_trash_check = QCheckBox("Include recoverable Trash")
        self.include_trash_check.setChecked(True)
        set_theme_style(self.include_imported_check, checkbox_qss)
        set_theme_style(self.include_trash_check, checkbox_qss)
        layout.addWidget(self.include_imported_check)
        layout.addWidget(self.include_trash_check)
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(6)
        self.create_btn = IconButton("increase.png", text="Create backup", size=16)
        self.resume_backup_btn = QPushButton("Resume backup")
        self.restore_btn = IconButton("refresh.png", text="Restore backup", size=18)
        self.resume_restore_btn = QPushButton("Resume restore")
        self.verify_btn = QPushButton("Verify backup")
        self.cancel_btn = QPushButton("Cancel")
        for button in (self.create_btn, self.restore_btn):
            button.setMinimumHeight(54)
            set_theme_style(button, lambda: button_secondary_qss() + (
                f"QPushButton {{ font-size: {Fonts.SIZE_BODY_L}px; padding: 8px 10px; }}"
                f"QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; }}"
            ))
        for button in (self.resume_backup_btn, self.resume_restore_btn, self.verify_btn):
            set_theme_style(button, lambda: button_outline_qss() + (
                f"QPushButton {{ padding: 5px 10px; }}"
                f"QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}"
            ))
        set_theme_style(self.cancel_btn, self._cancel_button_qss)
        buttons.setColumnStretch(0, 1)
        buttons.setColumnStretch(1, 1)
        buttons.addWidget(self.create_btn, 0, 0)
        buttons.addWidget(self.restore_btn, 0, 1)
        buttons.addWidget(self.resume_backup_btn, 1, 0)
        buttons.addWidget(self.resume_restore_btn, 1, 1)
        buttons.addWidget(self.verify_btn, 2, 0)
        buttons.addWidget(self.cancel_btn, 2, 1)
        layout.addLayout(buttons)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.paths_label = QLabel("No backup operation running.")
        self.paths_label.setWordWrap(True)
        self.paths_label.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(self.paths_label, lambda: label_body(Colors.TEXT_MUTED, Fonts.SIZE_MICRO))
        layout.addWidget(self.paths_label)
        self.status_label = QLabel("Backup is ready.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        set_theme_style(self.status_label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        layout.addWidget(self.status_label)
        self.create_btn.clicked.connect(self._create)
        self.resume_backup_btn.clicked.connect(lambda: self.controller.start_resume_backup())
        self.verify_btn.clicked.connect(lambda: self.controller.start_verify())
        self.restore_btn.clicked.connect(lambda: self.controller.start_restore())
        self.resume_restore_btn.clicked.connect(lambda: self.controller.start_resume_restore())
        self.cancel_btn.clicked.connect(lambda: self.controller.cancel())

    def _create(self):
        self.controller.start_create(
            include_imported=self.include_imported_check.isChecked(),
            include_trash=self.include_trash_check.isChecked())

    @staticmethod
    def _cancel_button_qss():
        return button_outline_qss() + f"""
            QPushButton {{ padding: 5px 10px; }}
            QPushButton:enabled {{ color: {Colors.DELETE}; border-color: {Colors.DELETE}; }}
            QPushButton:enabled:hover {{ background: {Colors.ERROR_SOFT}; }}
            QPushButton:enabled:pressed {{ background: {Colors.DELETE}; color: {Colors.TEXT}; }}
            QPushButton:disabled {{ background: {Colors.SURFACE_2}; color: {Colors.TEXT_MUTED};
                border-color: {Colors.BORDER}; }}
        """

    def _set_busy(self, busy):
        for widget in (self.create_btn, self.resume_backup_btn, self.verify_btn,
                       self.restore_btn, self.resume_restore_btn,
                       self.include_imported_check, self.include_trash_check):
            widget.setEnabled(not busy and self._available)
        self.cancel_btn.setEnabled(self.controller.cancellable)

    def _disable_cancel(self):
        self.cancel_btn.setEnabled(False)

    def _set_available(self, available):
        self._available = bool(available)
        if self.controller.busy:
            return
        for widget in (self.create_btn, self.resume_backup_btn, self.verify_btn,
                       self.restore_btn, self.resume_restore_btn,
                       self.include_imported_check, self.include_trash_check):
            widget.setEnabled(self._available)

    def _show_progress(self, progress):
        if not isinstance(progress, BackupProgress):
            return
        self.progress_bar.setRange(0, max(1, progress.total))
        self.progress_bar.setValue(min(progress.completed, max(1, progress.total)))
        message = progress.message or progress.phase
        self.status_label.setText(f"{progress.phase}: {progress.completed}/{progress.total} {message}")

    def _show_status(self, message):
        text = str(message)
        self.status_label.setText(text)
        if any(marker in text for marker in ("Package:", "Destination:",
                                              "Checkpoint:", "checkpoint")):
            self.paths_label.setText(text)

    def _show_result(self, result):
        self.paths_label.setText(_result_text(result))

    def _show_error(self, message):
        self.paths_label.setText(str(message))
        self.status_label.setText("Backup operation failed")
