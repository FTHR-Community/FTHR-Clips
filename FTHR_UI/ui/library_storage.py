"""Settings panel and GUI coordinator for root-scoped library storage limits."""

from __future__ import annotations

import os
import re
import shutil
import stat
import threading
import time
from decimal import Decimal, InvalidOperation

from PySide6.QtCore import QObject, QSignalBlocker, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QBoxLayout, QCheckBox, QHBoxLayout, QLabel, QLineEdit,
    QListView, QListWidget, QPushButton, QSlider, QVBoxLayout, QWidget,
)

from core.library_storage import (
    MAX_POLICY_BYTES, StorageBindingsSnapshot, StorageCandidate, StorageInventory, StoragePolicy,
    StoragePolicyError,
    StoragePreview, _walk_files, build_storage_inventory, canonical_root, make_preview,
    policy_for_root, root_identities_match, select_trash_candidates,
    storage_root_identity,
)
from core.settings_manager import clips_directory_lexical_from
from core.library_index import canonical_media_path
from ui.dialogs import FthrMessageDialog
from ui.style import (
    Colors, Fonts, button_outline_qss, checkbox_qss,
    label_body, lineedit_qss, set_theme_style, slider_qss,
)


_DECIMAL_UNITS = ((10 ** 12, "TB"), (10 ** 9, "GB"), (10 ** 6, "MB"),
                  (10 ** 3, "KB"), (1, "B"))
_BYTE_STOPS = (0, *(size * 10 ** 6 for size in (
    500, 1_000, 2_000, 5_000, 10_000, 20_000, 50_000, 100_000, 200_000, 500_000,
    1_000_000, 2_000_000, 5_000_000, 10_000_000,
)))
_DAY_STOPS = (0, 1, 3, 7, 14, 30, 60, 90, 180, 365)


def _bytes_text(value: int) -> str:
    value = max(0, int(value))
    for scale, unit in _DECIMAL_UNITS:
        if value >= scale or scale == 1:
            rounded = (value + scale // 2) // scale
            return f"{rounded} {unit}"


def _storage_limit_text(value: int) -> str:
    gb = 10 ** 9
    if value % (1000 * gb) and value % gb == 0:
        return f"{value // gb} GB"
    return _bytes_text(value) if value else "Off"


def _floor_bytes(value: int) -> int:
    for scale, _ in _DECIMAL_UNITS[1:]:
        if value >= scale or scale == 1:
            return value // scale * scale


def _parse_bytes_text(value: str) -> int:
    """Parse decimal or legacy binary sizes without rounding stored limits."""
    text = str(value).strip()
    if not text or text.casefold() == "off":
        return 0
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(B|[KMGT]i?B)?",
                         text, flags=re.IGNORECASE)
    if match is None:
        raise ValueError("Enter Off or a size such as 500 MB or 2 GB.")
    try:
        number = Decimal(match.group(1))
    except InvalidOperation as error:
        raise ValueError("Storage limits must resolve to whole non-negative bytes.") from error
    suffix = (match.group(2) or "B").casefold()
    power = ("b", "k", "m", "g", "t").index(suffix[0])
    multiplier = (1024 if "i" in suffix else 1000) ** power
    numerator, denominator = number.as_integer_ratio()
    result, remainder = divmod(numerator * multiplier, denominator)
    if remainder:
        raise ValueError("Storage limits must resolve to whole non-negative bytes.")
    return result


def _retention_days(value: str) -> int:
    text = value.strip()
    return 0 if not text or text.casefold() == "no auto delete" else int(text)


def _candidate_key(candidate):
    """Return a stable, hashable review key without relying on Mapping hash."""
    identity = tuple(int(candidate.identity[field]) for field in (
        "st_dev", "st_ino", "size", "mtime_ns"))
    return (candidate.action, candidate.trash_id, str(candidate.path), identity,
            candidate.file.clip_id)


class LibraryStorageController(QObject):
    """Own the off-GUI inventory worker and sequential cleanup lifecycle."""

    inventory_changed = Signal(object)
    preview_ready = Signal(object)
    context_changed = Signal()
    status_changed = Signal(str)
    operation_finished = Signal(object)
    error = Signal(str)

    _REOPEN_FRESHNESS_SECONDS = 10.0

    def __init__(self, main_window, parent=None):
        super().__init__(parent or main_window)
        self.main_window = main_window
        self.settings_manager = getattr(main_window, "settings_manager", None)
        self._cancel = threading.Event()
        self._generation = 0
        self._inventory_running = False
        self._inventory_generation = None
        self._inventory_cancel = None
        self._pending_refresh = False
        self._visible = False
        self._scan_suspended = False
        self._closed = False
        self._inventory: StorageInventory | None = None
        self._reopen_inventory_context = None
        self._preview: StoragePreview | None = None
        self._after_inventory = None
        self._operation_busy = False
        self._cleanup_queue: tuple[StorageCandidate, ...] = ()
        self._cleanup_index = 0
        self._reviewed_keys = frozenset()
        self._automatic_run = False
        self._automatic_cooldown_until = 0.0
        self._cleanup_run_id = 0
        self._cleanup_stage = None
        self._cleanup_cancelled = False
        self._cleanup_root_identity = None
        self._cleanup_context = None
        self._observed_clip_keys = set()
        self._observing_clip_keys = set()
        self._cadence = QTimer(self)
        self._cadence.setInterval(60_000)
        self._cadence.timeout.connect(self.request_refresh)
        # A configured policy must warm its admission snapshot even while the
        # settings page is hidden.  request_refresh remains a no-op for the
        # all-default policy, so this timer performs no hidden filesystem I/O.
        self._cadence.start()
        self.inventory_changed.connect(self._publish_inventory)

    @property
    def inventory(self):
        return self._inventory

    @property
    def preview(self):
        return self._preview

    def _policy_context(self):
        if self.settings_manager is None:
            raise OSError("Storage settings are unavailable")
        root = str(clips_directory_lexical_from(self.settings_manager))
        policy = policy_for_root(self.settings_manager, root, strict=False)
        imported = tuple(self.settings_manager.get("imported_clip_folders", ()) or ())
        return root, policy, imported

    @staticmethod
    def _has_storage_rule(policy):
        return bool(policy.max_library_bytes or policy.trash_max_bytes
                    or policy.trash_retention_days)

    def _busy_paths_snapshot(self):
        """Capture GUI-owned busy paths without filesystem inspection."""
        main = self.main_window
        paths = set()

        def add_path(value):
            if not isinstance(value, (str, bytes, os.PathLike)):
                return
            raw = os.fspath(value)
            if isinstance(raw, bytes):
                raw = os.fsdecode(raw)
            if raw:
                paths.add(str(raw))

        def add_many(values):
            if isinstance(values, (str, bytes, os.PathLike)):
                add_path(values)
                return
            for value in values or ():
                add_path(value)

        for shell in (getattr(main, "_active_clip_viewer", None),
                      getattr(main, "_parked_editor_shell", None)):
            add_path(getattr(shell, "clip_path", None))
        for session in getattr(main, "_editor_sessions", ()) or ():
            add_path(getattr(session, "clip_path", None))
        add_path(getattr(main, "_manual_record_path", None))
        add_path(getattr(main, "_pending_manual_record_path", None))

        grid = getattr(main, "clip_grid", None)
        try:
            from ui.clip_cover_dialog import ClipCoverDialog
        except (ImportError, AttributeError):
            # Lightweight hosts may omit the optional cover dialog module.
            cover_dialog_type = None
        else:
            cover_dialog_type = ClipCoverDialog
        if cover_dialog_type is not None:
            for parent in (main, grid):
                finder = getattr(parent, "findChildren", None)
                if callable(finder):
                    for dialog in finder(cover_dialog_type) or ():
                        add_path(getattr(dialog, "_source_path", None))

        upload = getattr(main, "upload_manager", None)
        upload_lock = getattr(upload, "_in_flight_lock", None)
        if upload is not None and upload_lock is not None:
            with upload_lock:
                add_many(tuple(getattr(upload, "_in_flight", ())))
                add_many(tuple(getattr(upload, "_local_mutations", ())))

        try:
            from core import export_queue
            export_tasks = export_queue.tasks()
        except (ImportError, AttributeError):
            # A host without export support has no export paths to reserve.
            export_tasks = ()
        for task in export_tasks or ():
            if not getattr(task, "done", False):
                add_path(getattr(task, "source", None))
                add_path(getattr(task, "output", None))

        readiness = getattr(main, "_clip_readiness", None)
        readiness_lock = getattr(readiness, "_lock", None)
        if readiness is not None and readiness_lock is not None:
            with readiness_lock:
                handles = dict(getattr(readiness, "_handles", {}))
                blocked = tuple(getattr(readiness, "_blocked", {}).keys())
                for path, handle in handles.items():
                    state = getattr(getattr(handle, "state", None), "name",
                                    getattr(handle, "state", None))
                    if state not in {"READY", "READY_WITH_WARNING"}:
                        add_path(path)
                add_many(blocked)
        return tuple(paths)

    def _storage_busy_hint(self):
        """Return only global GUI states that stop storage admission.

        MainWindow's file-operation admission remains authoritative; this hint
        does not block unrelated clips because their paths are passed to the
        inventory worker as a fresh RAM-only busy-path snapshot.
        """
        main = self.main_window
        if not getattr(main, "_file_operations_ready", True):
            return True
        if (getattr(main, "_shutdown_requested", False)
                or getattr(main, "_shutdown_complete", False)):
            return True
        readiness = getattr(main, "_clip_readiness", None)
        readiness_lock = getattr(readiness, "_lock", None)
        if readiness is not None and readiness_lock is not None:
            with readiness_lock:
                if getattr(readiness, "_initializing", False):
                    return True
        return False

    def set_scan_suspended(self, suspended: bool) -> None:
        if self._closed or suspended == self._scan_suspended:
            return
        self._scan_suspended = suspended
        if suspended:
            self._cadence.stop()
            self.cancel()
        else:
            self._cadence.start()
            self.request_refresh()

    def set_visible(self, visible: bool):
        visible = bool(visible)
        if self._closed or visible == self._visible:
            return
        self._visible = visible
        if self._scan_suspended:
            return
        if self._visible:
            self._cadence.start()
            if self._inventory_running and self._inventory_generation == self._generation:
                return
            if not self._can_reuse_inventory_on_open():
                self.request_refresh()
        else:
            self._cadence.stop()
            try:
                _, policy, _ = self._policy_context()
            except (OSError, TypeError, ValueError):
                policy = StoragePolicy()
            if policy.cleanup_enabled or self._has_storage_rule(policy):
                # A configured quota still warms the admission cache while the
                # panel is hidden; only the all-default policy is scan-free.
                self._cadence.start()
            elif self._inventory_running or self._operation_busy:
                self.cancel()

    def _can_reuse_inventory_on_open(self):
        if self._reopen_inventory_context is None or self._inventory is None:
            return False
        age = time.time() - self._inventory.scanned_at
        if not 0 <= age < self._REOPEN_FRESHNESS_SECONDS:
            return False
        try:
            root, policy, imported = self._policy_context()
            return (canonical_root(root), policy, imported) == self._reopen_inventory_context
        except (OSError, TypeError, ValueError):
            return False

    def root_changed(self, *_args):
        self.cancel()
        self._reopen_inventory_context = None
        self._inventory = None
        self._preview = None
        self.context_changed.emit()
        if self._scan_suspended:
            return
        try:
            _, policy, _ = self._policy_context()
        except (OSError, TypeError, ValueError):
            policy = StoragePolicy()
        if self._visible or policy.cleanup_enabled or self._has_storage_rule(policy):
            self._cadence.start()
        if self._visible:
            self.request_refresh(force=True)

    def request_quota_refresh(self) -> bool:
        """An explicit save can still request its required quota accounting."""
        return self.request_refresh(force=True, quota_check=True)

    def request_refresh(self, *, force=False, quota_check=False):
        if self._closed or (self._scan_suspended and not quota_check):
            return False
        try:
            root, policy, imported = self._policy_context()
        except (OSError, TypeError, ValueError) as error:
            self.error.emit(str(error))
            return False
        if (not self._visible and not force
                and not (policy.cleanup_enabled or self._has_storage_rule(policy))):
            return False
        self._reopen_inventory_context = None
        if self._inventory_running:
            self._pending_refresh = True
            return False
        self._generation += 1
        generation = self._generation
        cancel = threading.Event()
        self._cancel = cancel
        busy_paths = self._busy_paths_snapshot()
        self._inventory_running = True
        self._inventory_generation = generation
        self._inventory_cancel = cancel
        self._pending_refresh = False
        self.status_changed.emit("Checking storage…")

        def work():
            try:
                initial_root_identity = storage_root_identity(root)
                snapshot = self.main_window.clip_metadata_manager.storage_bindings_snapshot()
                trash_items = self.main_window.clip_metadata_manager.list_trash()
                snapshot = self._snapshot_with_trash_paths(snapshot, trash_items)
                trash_created = {
                    str(item.trash_id): str(item.created_utc)
                    for item in trash_items
                    if (getattr(item, "trash_id", None)
                        and getattr(item, "created_utc", None)
                        and getattr(getattr(item, "status", None), "value",
                                    getattr(item, "status", None)) == "TRASHED")
                }
                inventory = build_storage_inventory(
                    root, metadata_snapshot=snapshot, imported_roots=imported,
                    cancel=cancel,
                    cover_root=self._cover_store_root(),
                    cover_bytes=self._cover_bytes_hint(root),
                    trash_created_utc=trash_created,
                    busy_paths=busy_paths,
                )
                if not root_identities_match(
                        inventory.root_identity, storage_root_identity(root)):
                    raise OSError("The Clips folder changed during the storage check")
                if not root_identities_match(initial_root_identity, inventory.root_identity):
                    raise OSError("The Clips folder changed during the storage check")
                result = (generation, root, policy, imported, inventory, None)
            except InterruptedError:
                result = (generation, root, policy, imported, None, "Storage check cancelled")
            except Exception as error:  # worker boundary; publish a visible error
                result = (generation, root, policy, imported, None, str(error))
            try:
                self.main_window._ui_call.emit(lambda value=result: self._inventory_done(value))
            except RuntimeError:
                # The GUI bridge can disappear during shutdown after the
                # worker has finished; release the plain-Python ownership slot.
                self._inventory_running = False

        try:
            self.main_window._launch_file_worker(work)
        except Exception as error:
            self._inventory_running = False
            self._inventory_generation = None
            self._inventory_cancel = None
            self.error.emit(str(error))
            return False
        return True

    def _cover_store_root(self):
        store = getattr(getattr(self.main_window, "clip_metadata_manager", None),
                        "_cover_store", None)
        return getattr(store, "root", None)

    def _cover_bytes_hint(self, owned_root=None):
        # Cover assets live in the dedicated store and are displayed separately;
        # this bounded no-follow walk runs only inside the inventory worker.
        cover_root = self._cover_store_root()
        if cover_root is None:
            return 0
        if owned_root is not None and self._path_is_under_root(cover_root, owned_root):
            # The exact ``<DB>.covers`` namespace is already counted once as
            # technical ``other`` data by the owned-root walk.
            return 0
        try:
            total = 0
            for _path, info, _relative in _walk_files(cover_root, self._cancel):
                if info is None:
                    return 0
                total += int(info.st_size)
            return total
        except FileNotFoundError:
            # A cover tree removed during refresh contributes no bytes.
            return 0
        except InterruptedError:
            raise
        except OSError:
            # A cover tree that cannot be read cannot contribute accounted bytes.
            return 0
        return total

    @staticmethod
    def _path_is_under_root(path, root):
        try:
            path = canonical_root(path)
            root = canonical_root(root)
            return os.path.commonpath((path, root)) == root
        except (OSError, ValueError):
            # An unrepresentable path cannot be proven to belong to the root.
            return False

    @staticmethod
    def _snapshot_with_trash_paths(snapshot, trash_items):
        """Attach moved Trash paths to the one immutable metadata snapshot."""
        if not isinstance(snapshot, StorageBindingsSnapshot):
            return snapshot
        entries_by_clip = {}
        for raw_entry in snapshot.files:
            try:
                entry = dict(raw_entry)
            except (TypeError, ValueError):
                # A malformed snapshot row cannot safely be remapped.
                continue
            role = getattr(entry.get("role", "PRIMARY"), "value",
                           entry.get("role", "PRIMARY"))
            if entry.get("clip_id") is not None:
                entries_by_clip.setdefault(str(entry["clip_id"]), []).append(entry)
        by_path = dict(snapshot.by_path)
        for item in trash_items:
            status = getattr(getattr(item, "status", None), "value",
                             getattr(item, "status", None))
            if status != "TRASHED":
                # PURGING/recovery rows are not reviewed Trash candidates;
                # leaving their physical members unbound makes the inventory
                # reject the whole bundle conservatively.
                continue
            clip_id = getattr(item, "clip_id", None)
            stored_path = getattr(item, "stored_path", None)
            entries = entries_by_clip.get(str(clip_id), ()) if clip_id is not None else ()
            if not entries or not stored_path:
                continue
            stored_path = str(stored_path)
            for source in entries:
                mapped = dict(source)
                role = getattr(mapped.get("role", "PRIMARY"), "value",
                               mapped.get("role", "PRIMARY"))
                target = (stored_path if str(role) == "PRIMARY" else
                          os.path.join(os.path.dirname(stored_path),
                                       os.path.basename(str(source["path"]))))
                mapped["path"] = target
                mapped["path_key"] = canonical_media_path(target)
                # A successfully moved member is complete media even though
                # its active SQL binding state is TRASHED.
                mapped["complete"] = True
                by_path[mapped["path_key"]] = mapped
        return StorageBindingsSnapshot(
            files=snapshot.files, by_path=by_path,
            trashed_ids=snapshot.trashed_ids)

    def observe_completed_clip(self, path, expected_identity, *, publish_ui=True):
        """Register exactly one finalized, identity-bound capture candidate.

        This is deliberately an optional worker-side observation.  A normal
        save stays untouched when no storage rule is configured, and a later
        same-path replacement cannot be adopted because the captured stat
        tuple is checked both before inspection and by the metadata writer.
        """
        if not publish_ui or self._closed or expected_identity is None:
            return False
        try:
            root, policy, _ = self._policy_context()
            if not self._has_storage_rule(policy):
                return False
            target = canonical_root(path)
            if os.path.commonpath((canonical_root(root), target)) != canonical_root(root):
                return False
            identity = dict(expected_identity)
            fields = ("st_dev", "st_ino", "size", "mtime_ns")
            if (set(identity) != set(fields)
                    or any(type(identity[field]) is not int for field in fields)):
                return False
            key = (target, tuple(identity[field] for field in fields))
            if key in self._observed_clip_keys or key in self._observing_clip_keys:
                return False
            self._observing_clip_keys.add(key)
        except (OSError, TypeError, ValueError):
            # A malformed or changed policy context rejects this observation.
            return False

        def work():
            result = False
            try:
                current_root, current_policy, _ = self._policy_context()
                if (not self._has_storage_rule(current_policy)
                        or canonical_root(current_root) != canonical_root(root)):
                    raise OSError("storage policy changed before finalized clip registration")
                current_target = canonical_root(path)
                if os.path.commonpath((canonical_root(root), current_target)) != canonical_root(root):
                    raise OSError("finalized clip moved outside the owned root")
                info = os.stat(path, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode)
                        or dict(identity) != {
                            "st_dev": int(info.st_dev), "st_ino": int(info.st_ino),
                            "size": int(info.st_size), "mtime_ns": int(info.st_mtime_ns)}):
                    raise OSError("finalized clip identity changed")
                evidence = self.main_window.clip_metadata_manager.inspect_media(path)
                if dict(evidence.stat) != identity:
                    raise OSError("finalized clip identity changed during inspection")
                after = os.stat(path, follow_symlinks=False)
                if (not stat.S_ISREG(after.st_mode)
                        or dict(identity) != {
                            "st_dev": int(after.st_dev), "st_ino": int(after.st_ino),
                            "size": int(after.st_size), "mtime_ns": int(after.st_mtime_ns)}):
                    raise OSError("finalized clip identity changed after inspection")
                # A finalized capture is allowed to register its own new
                # primary, but an old legacy/unverified row must never be
                # silently adopted by an automatic observation.
                binding = self.main_window.clip_metadata_manager.register_media(
                    evidence, adopt_legacy=False)
                binding_state = getattr(getattr(binding, "state", None), "value",
                                        getattr(binding, "state", None))
                result = (binding is not None and binding.clip_id is not None
                          and binding_state in {"STAT_BOUND", "CONTENT_VERIFIED"})
            except (OSError, RuntimeError, TypeError, ValueError):
                result = False
            payload = (key, result)
            try:
                self.main_window._ui_call.emit(
                    lambda value=payload: self._completed_clip_observed(value))
            except RuntimeError:
                # No receiver remains to release this observer key after
                # teardown, so do that cleanup at the worker boundary.
                self._observing_clip_keys.discard(key)

        try:
            self.main_window._launch_file_worker(work)
        except (OSError, RuntimeError, TypeError, ValueError):
            self._observing_clip_keys.discard(key)
            return False
        return True

    def _completed_clip_observed(self, payload):
        key, registered = payload
        self._observing_clip_keys.discard(key)
        if self._closed:
            return
        if registered:
            self._observed_clip_keys.add(key)
            self.request_refresh(force=True)

    def _inventory_done(self, result):
        generation, root, policy, imported, inventory, error = result
        if generation != self._generation:
            # A cancelled worker may finish its read-only walk; release its
            # ownership slot without ever publishing stale accounting.
            current_job = self._inventory_generation == generation
            if current_job:
                self._inventory_running = False
                self._inventory_generation = None
                self._inventory_cancel = None
            if current_job and self._cleanup_cancelled and self._cleanup_stage == "inventory":
                self._finish_cleanup_cancelled()
            if current_job and self._pending_refresh and not self._closed:
                self._pending_refresh = False
                QTimer.singleShot(0, self.request_refresh)
            return
        if self._closed:
            if self._inventory_generation == generation:
                self._inventory_running = False
                self._inventory_generation = None
                self._inventory_cancel = None
            return
        if self._inventory_generation == generation:
            self._inventory_running = False
            self._inventory_generation = None
            self._inventory_cancel = None
        inventory_accepted = False
        if error is not None:
            # An errored scan cannot authorize cleanup or quota admission;
            # discard the previous accounting snapshot until a fresh scan
            # succeeds.
            self._inventory = None
            self._preview = None
            self.error.emit(error)
            if self._operation_busy:
                self._finish_cleanup_cancelled()
        elif inventory is not None:
            try:
                current_root, current_policy, _ = self._policy_context()
            except (OSError, TypeError, ValueError):
                current_root, current_policy = "", StoragePolicy()
            current_imported = tuple(self.settings_manager.get(
                "imported_clip_folders", ()) or ())
            if (canonical_root(current_root) != canonical_root(root)
                    or current_policy != policy
                    or current_imported != tuple(imported)):
                # Do not use accounting captured for a different root or
                # policy while the replacement scan is pending.
                self._inventory = None
                self._preview = None
                self._pending_refresh = True
            else:
                inventory_accepted = True
                self._inventory = inventory
                # Reuse only for quick navigation without any quota or cleanup
                # rule. Admission and cleanup still request fresh inventories.
                if not self._pending_refresh and policy == StoragePolicy():
                    self._reopen_inventory_context = (
                        canonical_root(root), policy, tuple(imported))
                self.inventory_changed.emit(inventory)
                self._preview = make_preview(inventory, policy)
                self.preview_ready.emit(self._preview)
        callback = self._after_inventory if inventory_accepted else None
        if inventory_accepted:
            self._after_inventory = None
        if callback is not None:
            callback(inventory)
        if self._pending_refresh and not self._closed:
            self._pending_refresh = False
            QTimer.singleShot(0, self.request_refresh)
        elif error is None and not self._operation_busy:
            self.status_changed.emit("Storage ready")
        if (inventory_accepted and not self._visible and not self._scan_suspended
                and not self._operation_busy and self._preview is not None):
            try:
                now = time.monotonic()
                auto_policy = StoragePolicy(
                    max_library_bytes=(policy.max_library_bytes
                                       if policy.automatic_cleanup else 0),
                    automatic_cleanup=policy.automatic_cleanup,
                    trash_max_bytes=(policy.trash_max_bytes
                                     if policy.automatic_trash_cleanup else 0),
                    trash_retention_days=(policy.trash_retention_days
                                          if policy.automatic_trash_cleanup else 0),
                    automatic_trash_cleanup=policy.automatic_trash_cleanup,
                )
                auto_preview = make_preview(inventory, auto_policy, automatic=True)
                if (policy.cleanup_enabled and auto_preview.candidates
                        and now >= self._automatic_cooldown_until
                        and not self._storage_busy_hint()):
                    self._automatic_cooldown_until = now + 60.0
                    self.start_manual_cleanup(
                        auto_preview, confirmed=True, automatic=True)
            except (OSError, TypeError, ValueError) as error:
                self.error.emit(str(error))

    def _publish_inventory(self, inventory):
        try:
            _, policy, _ = self._policy_context()
        except (OSError, TypeError, ValueError):
            # A failed policy read leaves the prior inventory unpublished.
            return
        recording_path = (getattr(self.main_window, '_manual_record_path', None)
                          or getattr(self.main_window, '_pending_manual_record_path', None))
        owned_recording = True
        ownership = getattr(self.main_window, '_storage_path_is_owned', None)
        if recording_path is not None and callable(ownership):
            owned_recording = bool(ownership(recording_path))
        if (owned_recording and policy.max_library_bytes
                and inventory.total_bytes > policy.max_library_bytes
                and getattr(self.main_window, '_manual_record_state', 'idle') == 'recording'):
            stop = getattr(self.main_window, '_stop_manual_recording', None)
            if callable(stop):
                stop()

    def preview_now(self):
        if self._inventory is None:
            self._after_inventory = lambda _inventory: self.preview_now()
            return self.request_refresh(force=True)
        try:
            _, policy, _ = self._policy_context()
            self._preview = make_preview(self._inventory, policy)
        except (OSError, TypeError, ValueError) as error:
            self.error.emit(str(error))
            return False
        self.preview_ready.emit(self._preview)
        return True

    def start_manual_cleanup(self, preview=None, *, confirmed=False, automatic=False):
        if self._operation_busy:
            return False
        preview = preview or self._preview
        if preview is None or not preview.complete:
            self.error.emit("Storage must be checked before cleanup can start.")
            return False
        if not confirmed:
            self.error.emit("Confirm permanent cleanup before continuing.")
            return False
        if self._storage_busy_hint():
            self.error.emit("Storage cleanup is waiting for active clip work to finish.")
            return False
        self._cleanup_run_id += 1
        self._cleanup_cancelled = False
        self._cleanup_stage = None
        self._operation_busy = True
        self._automatic_run = bool(automatic)
        self._cleanup_queue = tuple(preview.candidates[:20])
        self._cleanup_index = 0
        self._cleanup_root_identity = (
            dict(self._inventory.root_identity) if self._inventory is not None else None)
        try:
            root, policy, imported = self._policy_context()
            self._cleanup_context = (canonical_root(root), policy, tuple(imported))
        except (OSError, TypeError, ValueError):
            self._cleanup_context = None
        self._reviewed_keys = frozenset(
            _candidate_key(candidate)
            for candidate in self._cleanup_queue)
        self.status_changed.emit("Cleaning up storage…")
        self._cleanup_next()
        return True

    def _cleanup_next(self):
        if self._closed or not self._operation_busy:
            return
        if self._cleanup_cancelled:
            if self._cleanup_stage is None:
                self._finish_cleanup_cancelled()
            return
        if self._cleanup_index >= len(self._cleanup_queue):
            self._operation_busy = False
            self._automatic_run = False
            self._cleanup_stage = None
            self._cleanup_root_identity = None
            self._cleanup_context = None
            self.status_changed.emit("Cleanup finished")
            self.operation_finished.emit(self._inventory)
            self.request_refresh(force=True)
            return
        candidate = self._cleanup_queue[self._cleanup_index]
        run_id = self._cleanup_run_id
        self._cleanup_stage = "inventory"
        self._after_inventory = lambda inventory, value=candidate, rid=run_id: self._admit_cleanup_candidate(
            value, inventory, rid)
        if not self.request_refresh(force=True) and not self._inventory_running:
            self._finish_cleanup_cancelled()

    def _finish_cleanup_cancelled(self):
        self._operation_busy = False
        self._cleanup_stage = None
        self._after_inventory = None
        self._automatic_run = False
        self._cleanup_queue = ()
        self._cleanup_index = 0
        self._cleanup_root_identity = None
        self._cleanup_context = None
        if not self._closed:
            self.status_changed.emit("Cleanup cancelled")
            self.operation_finished.emit(None)

    def _admit_cleanup_candidate(self, candidate, inventory, run_id):
        """Recheck one reviewed candidate immediately before its operation."""
        if (self._closed or not self._operation_busy
                or run_id != self._cleanup_run_id):
            return
        if self._cleanup_cancelled:
            self._finish_cleanup_cancelled()
            return
        key = _candidate_key(candidate)
        try:
            root, policy, imported = self._policy_context()
            context = self._cleanup_context
            context_matches = (
                context is not None
                and context == (canonical_root(root), policy, tuple(imported)))
            if (not inventory.complete
                    or canonical_root(inventory.root) != canonical_root(root)
                    or not context_matches
                    or (self._cleanup_root_identity is not None
                        and not root_identities_match(
                            inventory.root_identity, self._cleanup_root_identity))):
                # A changed root, policy, import boundary, or ownership
                # identity invalidates the whole reviewed run.  Do not advance
                # into another candidate from a stale preview.
                self.error.emit("Storage settings or the Clips folder changed during cleanup.")
                self._finish_cleanup_cancelled()
                return
        except (OSError, TypeError, ValueError):
            self.error.emit("Storage settings or the Clips folder changed during cleanup.")
            self._finish_cleanup_cancelled()
            return
        if key not in self._reviewed_keys:
            self._cleanup_stage = None
            self._cleanup_index += 1
            QTimer.singleShot(0, self._cleanup_next)
            return
        try:
            fresh = make_preview(
                inventory, policy, max_items=20,
                automatic=self._automatic_run)
            current = next((item for item in fresh.candidates
                            if _candidate_key(item) == key), None)
        except (OSError, TypeError, ValueError):
            current = None
        if current is None:
            self._cleanup_stage = None
            self._cleanup_index += 1
            QTimer.singleShot(0, self._cleanup_next)
            return
        self._cleanup_index += 1
        self._cleanup_stage = "operation"
        if current.action == "purge":
            self._purge_candidate(current, run_id)
        else:
            self._trash_candidate(current, run_id)

    def _trash_candidate(self, candidate, run_id=None):
        run_id = self._cleanup_run_id if run_id is None else run_id
        def done(result, item=None, cleanup_done=False):
            if self._closed:
                return
            if run_id != self._cleanup_run_id or not self._operation_busy:
                return
            if self._cleanup_cancelled:
                self._finish_cleanup_cancelled()
                return
            if not result.succeeded or item is None:
                self.status_changed.emit("Cleanup skipped; the clip was retained.")
                self._cleanup_stage = None
                QTimer.singleShot(0, self._cleanup_next)
                return
            self._purge_after_trash(item, run_id, source_candidate=candidate)

        request = getattr(self.main_window, "_request_clip_trash", None)
        if not callable(request):
            self._finish_cleanup_cancelled()
            self.error.emit("Storage cleanup is unavailable while the library is starting.")
            return
        request(candidate.path, dict(candidate.identity), done,
                automatic=True, preserve_favorites=True, detailed=True,
                captured_clip_id=candidate.file.clip_id,
                explicit_review=not self._automatic_run,
                adopt_legacy=not self._automatic_run)

    def _purge_after_trash(self, item, run_id, *, source_candidate=None):
        """Re-inventory and re-plan the exact committed Trash bundle."""
        if self._closed or not self._operation_busy or run_id != self._cleanup_run_id:
            return
        if getattr(item, "trash_id", None) is None:
            self._finish_cleanup_cancelled()
            return
        self._cleanup_stage = "inventory"
        self._after_inventory = (
            lambda inventory, value=item, rid=run_id, source=source_candidate:
            self._admit_purge_after_trash(value, inventory, rid, source))
        if not self.request_refresh(force=True) and not self._inventory_running:
            self._finish_cleanup_cancelled()

    def _admit_purge_after_trash(self, item, inventory, run_id, source_candidate=None):
        if (self._closed or not self._operation_busy
                or run_id != self._cleanup_run_id):
            return
        if self._cleanup_cancelled:
            self._finish_cleanup_cancelled()
            return
        try:
            if (source_candidate is None
                    or source_candidate.action != "trash"
                    or source_candidate.file.category != "active"
                    or _candidate_key(source_candidate) not in self._reviewed_keys):
                raise OSError("The committed Trash bundle has no reviewed library candidate")
            item_status = getattr(getattr(item, "status", None), "value",
                                  getattr(item, "status", None))
            if item_status != "TRASHED":
                raise OSError("The committed Trash bundle is not recoverable")
            root, policy, imported = self._policy_context()
            context = self._cleanup_context
            if (not inventory.complete or context is None
                    or context != (canonical_root(root), policy, tuple(imported))
                    or (self._cleanup_root_identity is not None
                        and not root_identities_match(
                            inventory.root_identity, self._cleanup_root_identity))):
                raise OSError("Storage context changed before purge")
            library_need = (max(0, inventory.total_bytes - policy.max_library_bytes)
                            if policy.max_library_bytes else 0)
            if library_need <= 0:
                raise OSError("The library quota is already satisfied")
            committed = select_trash_candidates(
                inventory, policy, needed_bytes=library_need, max_items=1,
                manual_whitelist=(str(item.trash_id),))
            current = next((candidate for candidate in committed
                            if candidate.trash_id == str(item.trash_id)), None)
            if current is None:
                raise OSError("The committed Trash bundle is no longer eligible")
        except (OSError, TypeError, ValueError):
            self._cleanup_stage = None
            self._operation_busy = False
            self._automatic_run = False
            self._cleanup_context = None
            self.operation_finished.emit(None)
            return
        self._cleanup_stage = "operation"
        self._purge_candidate(current, run_id)

    def _purge_candidate(self, candidate, run_id=None):
        run_id = self._cleanup_run_id if run_id is None else run_id
        trash_id = getattr(candidate, "trash_id", None)
        if trash_id is None:
            self._finish_cleanup_cancelled()
            self.error.emit("The reviewed Trash item is no longer available.")
            return

        def done(result, _item=None, cleanup_done=False):
            if self._closed:
                return
            if run_id != self._cleanup_run_id or not self._operation_busy:
                return
            self._cleanup_stage = None
            if self._cleanup_cancelled:
                self._finish_cleanup_cancelled()
                return
            item_status = getattr(getattr(_item, "status", None), "value",
                                  getattr(_item, "status", None))
            if (not result.succeeded or not cleanup_done
                    or item_status != "PURGED"):
                self.status_changed.emit("Cleanup skipped; the item remains recoverable.")
                QTimer.singleShot(0, self._cleanup_next)
                return
            self._cleanup_stage = None
            self._cleanup_next()

        request = getattr(self.main_window, "_request_trash_purge", None)
        if not callable(request):
            self._finish_cleanup_cancelled()
            self.error.emit("Storage cleanup is unavailable while the library is starting.")
            return
        request(trash_id, done, automatic=True,
                preserve_favorites=True, detailed=True)

    def cancel(self):
        if self._inventory_cancel is not None:
            self._inventory_cancel.set()
        self._generation += 1
        self._pending_refresh = False
        self._after_inventory = None
        if self._operation_busy and self._cleanup_stage in ("inventory", "operation"):
            self._cleanup_cancelled = True
            self.status_changed.emit("Stopping storage cleanup...")
            return
        self._operation_busy = False
        self._automatic_run = False
        self._cleanup_queue = ()
        self._cleanup_stage = None
        self._cleanup_root_identity = None
        self._cleanup_context = None
        self.status_changed.emit("Storage check cancelled")

    def close(self):
        if self._closed:
            return
        self._cadence.stop()
        # Mark the bridge closed before cancellation so shutdown cannot emit a
        # user-facing "cancelled" status while QObject teardown is in progress.
        self._closed = True
        self.cancel()


class _LimitSlider(QSlider):
    """Select discrete limits while keeping typed values independent of the stops."""

    edited = Signal()

    def __init__(self, label, edit, parse, format_value, stops, hint):
        super().__init__(Qt.Orientation.Horizontal)
        self._edit = edit
        self._parse = parse
        self._format_value = format_value
        self._exact_amount = None
        self.setMinimumHeight(28)
        self.setMaximumWidth(164)
        self.setAccessibleName(f"{label} slider")
        self.setPageStep(1)
        self.setTracking(False)
        set_theme_style(self, lambda: slider_qss() + (
            f"QSlider::handle:horizontal:focus {{ border: 2px solid {Colors.TEXT}; }}"
            f"QSlider::handle:horizontal:disabled {{ background: {Colors.TEXT_MUTED}; }}"
        ))
        self.set_stops(stops, hint)
        self._edit.textChanged.connect(self._on_edit)
        self._edit.textEdited.connect(self._on_edit)
        self.valueChanged.connect(self._select_stop)

    def load_amount(self, amount):
        self._edit.setText(self._format_value(amount))
        # Retain legacy values that cannot be represented by the rounded display.
        self._exact_amount = amount if self._parse(self._edit.text()) != amount else None
        self._sync_amount()

    def edit_state(self):
        return self._edit.text(), self._exact_amount

    def amount(self):
        if self._exact_amount is not None:
            return self._exact_amount
        return self._parse(self._edit.text())

    def set_stops(self, stops, hint):
        self._stops = stops
        with QSignalBlocker(self):
            self.setRange(0, len(stops) - 1)
            self._sync_amount()
        self.setEnabled(len(stops) > 1)
        self.setAccessibleDescription(hint)
        self.setToolTip(hint)

    def _select_stop(self, index):
        self._exact_amount = None
        self._edit.setText(self._format_value(self._stops[index]))
        self.edited.emit()

    def _on_edit(self):
        self._exact_amount = None
        self._sync_amount()
        self.edited.emit()

    def _sync_amount(self):
        try:
            amount = self.amount()
        except ValueError:
            return
        # A positive custom value should never appear to be switched off.
        first = int(amount > 0 and len(self._stops) > 1)
        closest = min(range(first, len(self._stops)),
                      key=lambda index: abs(self._stops[index] - amount))
        with QSignalBlocker(self):
            self.setValue(closest)


class LibraryStoragePanel(QWidget):
    """Compact storage controls embedded in Clip Settings."""

    SPLIT_WIDTH = 560

    def __init__(self, settings_manager, controller: LibraryStorageController, parent=None):
        super().__init__(parent)
        self.settings_manager = settings_manager
        self.controller = controller
        self.setObjectName("libraryStoragePanel")
        self._root = ""
        self._loading_policy = False
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(450)
        self._save_timer.timeout.connect(self._save_policy)
        self._make_ui()
        controller.inventory_changed.connect(self._show_inventory)
        controller.preview_ready.connect(self._show_preview)
        controller.status_changed.connect(self.status_label.setText)
        controller.error.connect(self._show_error)
        controller.context_changed.connect(self._load_policy)
        self._load_policy()

    def _make_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        title = QLabel("Storage limits")
        set_theme_style(title, lambda: label_body(Colors.TEXT, Fonts.SIZE_H3))
        layout.addWidget(title)
        self._columns = QBoxLayout(QBoxLayout.Direction.TopToBottom)
        self._columns.setSpacing(16)
        layout.addLayout(self._columns)
        self._limits = QWidget()
        self._unconstrained_limits_width = self._limits.maximumWidth()
        limits_layout = QVBoxLayout(self._limits)
        limits_layout.setContentsMargins(0, 0, 0, 0)
        limits_layout.setSpacing(6)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(8)
        self._columns.addWidget(self._limits, 1, Qt.AlignmentFlag.AlignTop)
        self._columns.addWidget(review, 1, Qt.AlignmentFlag.AlignTop)
        layout.addStretch()

        self.max_library_bytes_edit, self.max_library_bytes_slider = self._add_limit(
            limits_layout, "Library limit", "Off", _parse_bytes_text, _storage_limit_text,
            "Enter Off or an exact size such as 500 MB or 2 GB.")
        self.trash_max_bytes_edit, self.trash_max_bytes_slider = self._add_limit(
            limits_layout, "Trash limit", "Off", _parse_bytes_text, _storage_limit_text,
            "Enter Off or an exact size such as 500 MB or 2 GB.")
        self.trash_retention_days_edit, self.trash_retention_days_slider = self._add_limit(
            limits_layout, "Trash retention (days)", "No auto delete", _retention_days,
            lambda days: str(days) if days else "No auto delete",
            "No auto delete turns off age-based deletion. The Trash size limit may still apply.",
            stops=_DAY_STOPS, field_width=142)

        limits_layout.addSpacing(6)
        self.automatic_cleanup_check = QCheckBox("Allow automatic library cleanup")
        self.automatic_trash_cleanup_check = QCheckBox("Allow automatic Trash cleanup")
        set_theme_style(self.automatic_cleanup_check, checkbox_qss)
        set_theme_style(self.automatic_trash_cleanup_check, checkbox_qss)
        self.automatic_cleanup_check.setToolTip("Automatic cleanup is off until you opt in.")
        self.automatic_trash_cleanup_check.setToolTip("Automatic permanent deletion is off until you opt in.")
        limits_layout.addWidget(self.automatic_cleanup_check)
        limits_layout.addWidget(self.automatic_trash_cleanup_check)
        usage_title = QLabel("Storage usage")
        set_theme_style(usage_title, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY_L))
        review_layout.addWidget(usage_title)
        self.usage_label = QLabel("Waiting for a storage check.")
        self.usage_label.setWordWrap(True)
        set_theme_style(self.usage_label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        review_layout.addWidget(self.usage_label)
        self.refresh_btn = QPushButton("Refresh usage")
        self.preview_btn = QPushButton("Preview cleanup")
        for button in (self.refresh_btn, self.preview_btn):
            set_theme_style(button, self._review_button_qss)
        self.refresh_btn.clicked.connect(lambda: self.controller.request_refresh(force=True))
        self.preview_btn.clicked.connect(self.controller.preview_now)
        review_actions = QHBoxLayout()
        review_actions.addWidget(self.refresh_btn)
        review_actions.addWidget(self.preview_btn)
        review_actions.addStretch()
        review_layout.addLayout(review_actions)
        review_layout.addSpacing(4)

        preview_title = QLabel("Cleanup review")
        set_theme_style(preview_title, lambda: label_body(Colors.TEXT, Fonts.SIZE_BODY_L))
        review_layout.addWidget(preview_title)
        self.preview_summary_label = QLabel("No cleanup preview.")
        self.preview_summary_label.setWordWrap(True)
        set_theme_style(self.preview_summary_label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_BODY))
        review_layout.addWidget(self.preview_summary_label)
        self.preview_list = QListWidget()
        self.preview_list.setAccessibleName("Cleanup candidates")
        self.preview_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.preview_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.preview_list.setWordWrap(True)
        self.preview_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.preview_list.setMaximumHeight(132)
        self.preview_list.setMinimumWidth(0)
        self.preview_list.hide()
        set_theme_style(self.preview_list, lambda: (
            f"QListWidget {{ color: {Colors.TEXT}; background: {Colors.SURFACE_1}; "
            f"border: 1px solid {Colors.BORDER}; font-size: {Fonts.SIZE_LABEL}px; "
            f"font-family: {Fonts.BODY}; }}"
        ))
        review_layout.addWidget(self.preview_list)

        self.cleanup_btn = QPushButton("Clean up preview…")
        self.stop_btn = QPushButton("Stop")
        for button in (self.cleanup_btn, self.stop_btn):
            set_theme_style(button, self._review_button_qss)
        self.cleanup_btn.clicked.connect(self._confirm_cleanup)
        self.stop_btn.clicked.connect(self.controller.cancel)
        cleanup_actions = QHBoxLayout()
        cleanup_actions.addWidget(self.cleanup_btn)
        cleanup_actions.addWidget(self.stop_btn)
        cleanup_actions.addStretch()
        review_layout.addLayout(cleanup_actions)

        self.status_label = QLabel("Storage check is off until this panel is opened.")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        set_theme_style(self.status_label, lambda: label_body(Colors.TEXT_DIM, Fonts.SIZE_LABEL))
        review_layout.addWidget(self.status_label)

        self._saved_limits = self._limit_state()
        for slider in (self.max_library_bytes_slider, self.trash_max_bytes_slider,
                       self.trash_retention_days_slider):
            slider.edited.connect(self._schedule_policy_save)
        for check in (self.automatic_cleanup_check, self.automatic_trash_cleanup_check):
            check.toggled.connect(self._schedule_policy_save)
        self._limit_edits = (self.max_library_bytes_edit, self.trash_max_bytes_edit,
                             self.trash_retention_days_edit)
        for edit in self._limit_edits:
            edit.editingFinished.connect(
                lambda field=edit: self._finish_limit_edit(field))

    @staticmethod
    def _review_button_qss():
        return button_outline_qss() + (
            f"QPushButton:disabled {{ color: {Colors.TEXT_MUTED}; border-color: {Colors.BORDER}; }}"
        )

    @staticmethod
    def _add_limit(layout, label_text, placeholder, parse, format_value, hint, *,
                   stops=(0,), field_width=128):
        label = QLabel(label_text)
        set_theme_style(label, lambda: label_body(Colors.TEXT, Fonts.SIZE_LABEL))
        layout.addWidget(label)
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setAccessibleName(label_text)
        edit.setAccessibleDescription(hint)
        edit.setPlaceholderText(placeholder)
        edit.setToolTip(hint)
        edit.setMinimumWidth(96)
        edit.setMaximumWidth(field_width)
        label.setBuddy(edit)
        set_theme_style(edit, lineedit_qss)

        slider = _LimitSlider(label_text, edit, parse, format_value, stops, hint)
        row.addWidget(slider, 1)
        row.addWidget(edit)
        row.addStretch(0)
        layout.addLayout(row)
        QWidget.setTabOrder(slider, edit)
        return edit, slider

    def _set_byte_slider_cap(self, free_bytes):
        cap = _floor_bytes(min(MAX_POLICY_BYTES, max(0, free_bytes or 0)))
        stops = tuple(stop for stop in _BYTE_STOPS if stop < cap) + (cap,)
        if free_bytes is None:
            hint = "Refresh usage to check available space."
        else:
            hint = f"Drive slider limit: {_storage_limit_text(cap) if cap else '0 B'}."
        for slider in (self.max_library_bytes_slider, self.trash_max_bytes_slider):
            slider.set_stops(stops, hint)

    def resizeEvent(self, event):
        split = self.width() >= self.SPLIT_WIDTH
        direction = (QBoxLayout.Direction.LeftToRight if split
                     else QBoxLayout.Direction.TopToBottom)
        if direction != self._columns.direction():
            self._limits.setMaximumWidth(310 if split else self._unconstrained_limits_width)
            self._columns.setDirection(direction)
            self.updateGeometry()
        super().resizeEvent(event)

    def _limit_state(self):
        return (
            self.max_library_bytes_slider.edit_state(), self.trash_max_bytes_slider.edit_state(),
            self.trash_retention_days_slider.edit_state(), self.automatic_cleanup_check.isChecked(),
            self.automatic_trash_cleanup_check.isChecked(),
        )

    def _update_review(self):
        dirty = self._limit_state() != self._saved_limits
        preview = self.controller.preview
        candidates = preview is not None and bool(preview.candidates)
        self.preview_btn.setEnabled(not dirty)
        self.cleanup_btn.setEnabled(not dirty and candidates and preview.complete)
        self.preview_list.setVisible(not dirty and candidates)
        if dirty:
            self.preview_summary_label.setText("Review available after the storage limits are saved.")
        elif preview is None:
            self.preview_summary_label.setText("No cleanup preview.")
        else:
            self.preview_summary_label.setText(self._preview_summary(preview))

    def _schedule_policy_save(self):
        if self._loading_policy:
            return
        self._update_review()
        self._save_timer.stop()
        if (self._limit_state() != self._saved_limits
                and not any(edit.isModified() for edit in self._limit_edits)):
            self._save_timer.start()

    def _finish_limit_edit(self, edit):
        edit.setModified(False)
        self._schedule_policy_save()

    def _load_policy(self):
        self._save_timer.stop()
        try:
            self._root = str(clips_directory_lexical_from(self.settings_manager))
            policy = policy_for_root(self.settings_manager, self._root, strict=False)
        except (OSError, TypeError, ValueError) as error:
            self._show_error(str(error))
            policy = StoragePolicy()
        self._saved_policy = policy
        self._loading_policy = True
        try:
            self.max_library_bytes_slider.load_amount(policy.max_library_bytes)
            self.trash_max_bytes_slider.load_amount(policy.trash_max_bytes)
            self.trash_retention_days_slider.load_amount(policy.trash_retention_days)
            self.automatic_cleanup_check.setChecked(policy.automatic_cleanup)
            self.automatic_trash_cleanup_check.setChecked(policy.automatic_trash_cleanup)
        finally:
            self._loading_policy = False
        self._saved_limits = self._limit_state()
        inventory = self.controller.inventory
        if inventory is None:
            self._set_byte_slider_cap(None)
            self.usage_label.setText("Waiting for a storage check.")
        else:
            self._show_inventory(inventory)
        self._update_review()

    def _save_policy(self):
        if self._limit_state() == self._saved_limits:
            return
        try:
            current_root = str(clips_directory_lexical_from(self.settings_manager))
            if canonical_root(current_root) != canonical_root(self._root):
                self._load_policy()
                self.status_label.setText(
                    "The Clips folder changed. Review its storage rules before editing them.")
                return
            policy = StoragePolicy.from_mapping({
                "version": 1,
                "max_library_bytes": self.max_library_bytes_slider.amount(),
                "trash_max_bytes": self.trash_max_bytes_slider.amount(),
                "trash_retention_days": self.trash_retention_days_slider.amount(),
                "automatic_cleanup": self.automatic_cleanup_check.isChecked(),
                "automatic_trash_cleanup": self.automatic_trash_cleanup_check.isChecked(),
            })
            if policy == self._saved_policy:
                self._load_policy()
                return
            self._validate_free_space(policy)
            if not self.settings_manager.save_storage_policy(self._root, policy):
                raise OSError("The storage policy could not be saved.")
        except (OSError, TypeError, ValueError, StoragePolicyError) as error:
            self._show_error(str(error))
            return
        self.controller.root_changed()
        self.status_label.setText("Storage rules saved")

    def _validate_free_space(self, policy):
        changed = (
            (label, getattr(policy, field))
            for label, field in (("Library limit", "max_library_bytes"),
                                 ("Trash limit", "trash_max_bytes"))
            if getattr(policy, field) != getattr(self._saved_policy, field)
            and getattr(policy, field) > 0
        )
        changed = tuple(changed)
        if not changed:
            return
        try:
            free = shutil.disk_usage(self._root).free
        except OSError as error:
            self._set_byte_slider_cap(None)
            raise OSError(
                "Available space could not be checked. Check the Clips folder and try again."
            ) from error
        self._set_byte_slider_cap(free)
        for label, amount in changed:
            if amount > free:
                available = _storage_limit_text(_floor_bytes(free)) if free else "0 B"
                raise ValueError(f"{label} exceeds available space. Try {available} or less.")

    def _confirm_cleanup(self):
        if self._limit_state() != self._saved_limits:
            return
        preview = self.controller.preview
        if preview is None or not preview.complete or not preview.candidates:
            self.status_label.setText("There are no reviewed cleanup candidates.")
            return
        reviewed = preview.candidates[:20]
        reclaim = sum(candidate.estimated_reclaimable_bytes for candidate in reviewed)
        answer = FthrMessageDialog.question(
            self, "Permanent cleanup",
            f"Permanently delete {len(reviewed)} listed clips? "
            f"Estimated reclaim: {_bytes_text(reclaim)}.",
            plain_text=True,
        )
        if (answer and self.controller.preview is preview
                and self._limit_state() == self._saved_limits):
            self.controller.start_manual_cleanup(preview, confirmed=True)

    def _show_inventory(self, inventory):
        self._set_byte_slider_cap(inventory.free_bytes)
        usage = (
            ("Total", inventory.total_bytes),
            ("Active", inventory.active_bytes),
            ("Trash", inventory.trash_bytes),
            ("Companions", inventory.companion_bytes),
            ("Other", inventory.other_bytes),
            ("Imported", inventory.imported_bytes),
            ("Covers", inventory.cover_bytes),
            ("Free", inventory.free_bytes),
        )
        self.usage_label.setText(" · ".join(
            f"{name} {_bytes_text(amount) if amount is not None else 'unavailable'}"
            .replace(" ", "\N{NO-BREAK SPACE}")
            for name, amount in usage
        ))

    def _show_preview(self, preview):
        self.preview_list.clear()
        for candidate in preview.candidates[:20]:
            action = ("Trash then delete permanently"
                      if candidate.action == "trash" else "Delete permanently")
            self.preview_list.addItem(
                f"{action}: {candidate.path} ({_bytes_text(candidate.estimated_reclaimable_bytes)})")
        self._update_review()

    @staticmethod
    def _preview_summary(preview):
        summary = (
            f"Cleanup preview: {len(preview.candidates)} clips; "
            f"estimated reclaim {_bytes_text(preview.estimated_reclaimable_bytes)}; "
            f"remaining {_bytes_text(preview.remaining_bytes)}."
        )
        if not preview.complete:
            summary += " Inventory incomplete; cleanup unavailable."
        elif len(preview.candidates) > 20:
            summary += " This cleanup covers the first 20 listed clips."
        return summary

    def _show_error(self, message):
        self.status_label.setText(str(message))
        self._update_review()

    def showEvent(self, event):
        super().showEvent(event)
        self.controller.set_visible(True)

    def hideEvent(self, event):
        self.controller.set_visible(False)
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_policy()
        super().hideEvent(event)


class StorageBackupRow(QWidget):
    """Keep both library settings panels in view when the content area is wide."""

    def __init__(self, storage, backup, parent=None):
        super().__init__(parent)
        self._backup = backup
        self._panels = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self._panels.setContentsMargins(0, 0, 0, 0)
        self._panels.setSpacing(24)
        self._panels.addWidget(storage, 0, Qt.AlignmentFlag.AlignTop)
        self._panels.addWidget(backup, 0, Qt.AlignmentFlag.AlignTop)
        self._panels.addStretch(1)

    def resizeEvent(self, event):
        wide = self.width() >= max(
            880, LibraryStoragePanel.SPLIT_WIDTH
            + self._backup.minimumSizeHint().width() + self._panels.spacing()
        )
        direction = (QBoxLayout.Direction.LeftToRight if wide
                     else QBoxLayout.Direction.TopToBottom)
        if direction != self._panels.direction():
            self._panels.setDirection(direction)
            self._panels.setStretch(0, 2 if wide else 0)
            self._panels.setStretch(1, 1 if wide else 0)
            self._panels.setStretch(2, 0 if wide else 1)
            self.updateGeometry()
        super().resizeEvent(event)
