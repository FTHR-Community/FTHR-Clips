"""F08-B1 editor regressions for transactional tag and description persistence.

These component tests use synthetic local media and isolated persistence; they
do not launch the application or perform capture.
"""
from __future__ import annotations

from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QCloseEvent, QPixmap
from PySide6.QtTest import QSignalSpy

pytest_plugins = ("test_editor_sessions",)


@pytest.fixture
def b1_editor_shell(editor_shell):
    """Use the existing component shell with a migrated explicit test catalog."""
    from main import MainWindow

    editor_shell.clip_metadata_manager.migrate(target_version=5)
    editor_shell.request_full_exit = MethodType(MainWindow.request_full_exit, editor_shell)
    editor_shell._shutdown_timer_started = None
    editor_shell._lifecycle_log = SimpleNamespace(info=lambda *_args, **_kwargs: None)
    editor_shell._tray_icon = None
    try:
        yield editor_shell
    finally:
        seen = set()
        for viewer in (editor_shell._active_clip_viewer,
                       getattr(editor_shell, "_parked_editor_shell", None)):
            if viewer is None or id(viewer) in seen:
                continue
            seen.add(id(viewer))
            # Some assertions deliberately leave invalid edits dirty. Clear only
            # synthetic UI flags so the shared fixture can release its widgets.
            viewer._tag_edit.setModified(False)
            viewer._desc_edit.setModified(False)


def open_editor(root, index=0):
    root._on_clip_opened(root.paths[index], QPixmap(), QRect())
    return root._active_clip_viewer


def make_selected_dirty(viewer, value="tag draft"):
    viewer._tag_edit.setText(value)
    viewer._tag_edit.setModified(True)


def stub_warning(monkeypatch):
    from ui.clip_viewer import FthrMessageDialog

    monkeypatch.setattr(FthrMessageDialog, "warning",
                        lambda *_args, **_kwargs: None)


def test_strict_csv_empty_and_second_quoted_field_round_trip():
    from core.library_tags import format_tags_text, parse_tags_text

    assert parse_tags_text("") == ()
    values = ("first", "second,with comma")
    assert parse_tags_text(format_tags_text(values)) == values


def test_description_only_editor_save_preserves_verbatim_legacy_tag(b1_editor_shell):
    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="  old, comma tag  ", description="before")
    viewer = open_editor(root)

    viewer._desc_edit.setText("description only")
    viewer._desc_edit.setModified(True)
    viewer._flush_clip_details()

    assert manager.get(str(path)) == {
        "tag": "  old, comma tag  ", "description": "description only"}
    assert not viewer._desc_edit.isModified()


def test_tag_save_uses_identity_captured_when_clip_loaded(b1_editor_shell):
    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"original identity")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="before", description="keep")
    viewer = open_editor(root)
    make_selected_dirty(viewer, "replacement must be rejected")

    # Simulate another process replacing the media after the editor's selection.
    replacement = path.with_name(path.stem + "-replacement.mp4")
    replacement.write_bytes(b"a different file identity")
    replacement.replace(path)
    with pytest.raises(OSError):
        viewer._flush_clip_details()

    assert manager.get(str(path)) == {"tag": "before", "description": "keep"}
    assert viewer.clip_path == str(path)
    assert viewer._tag_edit.text() == "replacement must be rejected"
    assert viewer._tag_edit.isModified()


def test_successful_noop_acknowledges_dirty_editor_and_emits_details_signal(b1_editor_shell):
    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="same", description="same description")
    viewer = open_editor(root)
    viewer._tag_edit.setText("same")
    viewer._tag_edit.setModified(True)
    spy = QSignalSpy(viewer.library_details_changed)

    viewer._flush_clip_details()

    assert not viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {"tag": "same", "description": "same description"}
    assert spy.count() == 1
    assert spy.at(0) == [str(path)]


def test_internal_details_flush_remains_strict_and_keeps_dirty_input(
        b1_editor_shell, monkeypatch):
    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b'synthetic local clip')
    manager = root.clip_metadata_manager
    manager.set(str(path), tag='stored', description='unchanged')
    viewer = open_editor(root)
    make_selected_dirty(viewer, 'valid replacement')
    attempts = []

    def fail_save(*_args, **_kwargs):
        attempts.append(True)
        raise OSError('synthetic metadata write failure')

    monkeypatch.setattr(manager, 'set_details', fail_save)

    with pytest.raises(OSError, match='synthetic metadata write failure'):
        viewer._flush_clip_details()

    assert len(attempts) == 1
    assert viewer._tag_edit.text() == 'valid replacement'
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {'tag': 'stored', 'description': 'unchanged'}


def test_editing_finished_warns_once_and_warning_reentry_does_not_retry_save(
        b1_editor_shell, monkeypatch):
    from ui.clip_viewer import FthrMessageDialog

    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b'synthetic local clip')
    manager = root.clip_metadata_manager
    manager.set(str(path), tag='stored', description='unchanged')
    viewer = open_editor(root)
    make_selected_dirty(viewer, 'valid replacement')
    attempts = []
    warnings = []

    def fail_save(*_args, **_kwargs):
        attempts.append(True)
        raise OSError('synthetic metadata write failure')

    def warning(*_args, **kwargs):
        warnings.append(kwargs)
        if len(warnings) == 1:
            viewer._tag_edit.editingFinished.emit()

    monkeypatch.setattr(manager, 'set_details', fail_save)
    monkeypatch.setattr(FthrMessageDialog, 'warning', warning)

    viewer._tag_edit.editingFinished.emit()

    assert len(attempts) == 1
    assert len(warnings) == 1
    assert warnings[0].get('plain_text') is True
    assert viewer._tag_edit.text() == 'valid replacement'
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {'tag': 'stored', 'description': 'unchanged'}


def test_invalid_metadata_blocks_export_close_before_cancel_or_hide(
        b1_editor_shell, monkeypatch):
    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b'synthetic local clip')
    manager = root.clip_metadata_manager
    manager.set(str(path), tag='stored', description='unchanged')
    viewer = open_editor(root)
    make_selected_dirty(viewer, 'invalid,\tvalue')
    stub_warning(monkeypatch)
    cancel_calls = []
    original_thread = viewer._export_thread
    original_job = viewer._export_job
    original_cancelled = viewer._export_cancel.is_set()
    try:
        viewer._export_thread = SimpleNamespace(is_alive=lambda: True)
        viewer._export_job = SimpleNamespace(
            request_cancel=lambda: cancel_calls.append(True))
        viewer._export_cancel.clear()

        assert viewer.close() is False

        assert viewer.isVisible()
        assert viewer._export_thread.is_alive()
        assert cancel_calls == []
        assert not viewer._export_cancel.is_set()
        assert viewer._tag_edit.isModified()
        assert manager.get(str(path)) == {'tag': 'stored', 'description': 'unchanged'}
    finally:
        viewer._export_thread = original_thread
        viewer._export_job = original_job
        if original_cancelled:
            viewer._export_cancel.set()
        else:
            viewer._export_cancel.clear()


@pytest.mark.parametrize("leave", ["close_event", "reject", "accept", "replace"])
def test_validation_failure_keeps_editor_and_dirty_text_on_leave_attempt(
        b1_editor_shell, monkeypatch, leave):

    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="valid", description="unchanged")
    viewer = open_editor(root)
    make_selected_dirty(viewer, "bad,\tvalue")
    stub_warning(monkeypatch)
    if leave == "close_event":
        event = QCloseEvent()
        viewer.closeEvent(event)
        assert event.isAccepted() is False
    elif leave == "replace":
        assert viewer.replace_clip(root.paths[1], autoplay=False) is False
    else:
        getattr(viewer, leave)()

    assert viewer.clip_path == str(path)
    assert viewer._tag_edit.text() == "bad,\tvalue"
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {"tag": "valid", "description": "unchanged"}


def test_failed_park_keeps_active_session_and_edit(b1_editor_shell, monkeypatch):

    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="valid", description="keep")
    viewer = open_editor(root)
    make_selected_dirty(viewer, "bad,\tvalue")
    stub_warning(monkeypatch)
    before_sessions = tuple(root._editor_sessions)

    root._minimize_active_editor(viewer)

    assert root._active_clip_viewer is viewer
    assert tuple(root._editor_sessions) == before_sessions
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {"tag": "valid", "description": "keep"}


def test_switch_to_parked_session_refuses_to_park_invalid_active_editor(
        b1_editor_shell, monkeypatch):

    root = b1_editor_shell
    first_path, second_path = Path(root.paths[0]), Path(root.paths[1])
    first_path.write_bytes(b"first local clip")
    second_path.write_bytes(b"second local clip")
    manager = root.clip_metadata_manager
    manager.set(str(first_path), tag="first", description="keep first")
    manager.set(str(second_path), tag="second", description="keep second")
    first = open_editor(root, 0)
    root._minimize_active_editor(first)
    first_session = next(session for session in root._editor_sessions
                         if session.clip_path == str(first_path))
    second = open_editor(root, 1)
    make_selected_dirty(second, "bad,\tvalue")
    stub_warning(monkeypatch)
    sessions_before = tuple(root._editor_sessions)

    root._restore_editor_session(first_session)

    assert root._active_clip_viewer is second
    assert tuple(root._editor_sessions) == sessions_before
    assert second.clip_path == str(second_path)
    assert second._tag_edit.isModified()
    assert manager.get(str(second_path)) == {
        "tag": "second", "description": "keep second"}


@pytest.mark.parametrize("destination", ["clips", "settings", "trash"])
def test_invalid_details_block_sidebar_navigation_without_qt_slot_exception(
        b1_editor_shell, monkeypatch, destination):
    import sys
    from ui.clip_viewer import FthrMessageDialog

    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="stored", description="unchanged")
    viewer = open_editor(root)
    session = root._active_editor_session
    assert session is not None
    make_selected_dirty(viewer, "invalid,\tvalue")
    slot_errors = []
    warnings = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: slot_errors.append(args))
    monkeypatch.setattr(
        FthrMessageDialog, "warning",
        lambda *args, **kwargs: warnings.append((args, kwargs)))
    starting_page = root.main_stack.currentIndex()
    destination_button = {
        "clips": root.close_settings_btn,
        "settings": root.settings_gear_btn,
        "trash": root.trash_sidebar_btn,
    }[destination]

    destination_button.click()

    assert slot_errors == []
    assert len(warnings) == 1
    assert warnings[0][1].get("plain_text") is True
    assert root.main_stack.currentIndex() == starting_page
    assert root._active_clip_viewer is viewer
    assert root._active_editor_session is session
    assert session.button.isChecked()
    assert viewer._tag_edit.text() == "invalid,\tvalue"
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {"tag": "stored", "description": "unchanged"}


@pytest.mark.parametrize("shell_state", ["active", "parked"])
def test_regular_exit_is_blocked_before_shutdown_flags_or_hide(
        b1_editor_shell, monkeypatch, shell_state):

    root = b1_editor_shell
    path = Path(root.paths[0])
    path.write_bytes(b"synthetic local clip")
    manager = root.clip_metadata_manager
    manager.set(str(path), tag="valid", description="keep")
    viewer = open_editor(root)
    if shell_state == "parked":
        assert root._park_active_editor()
    make_selected_dirty(viewer, "bad,\tvalue")
    stub_warning(monkeypatch)
    assert root.isVisible()

    root.request_full_exit()

    assert root._shutdown_requested is False
    assert root.isVisible()
    assert root._active_clip_viewer is (viewer if shell_state == "active" else None)
    assert getattr(root, "_parked_editor_shell", None) is (
        viewer if shell_state == "parked" else None)
    assert viewer._tag_edit.isModified()
    assert manager.get(str(path)) == {"tag": "valid", "description": "keep"}


def test_minimize_keeps_active_session_when_player_teardown_refuses(
        b1_editor_shell, monkeypatch):
    root = b1_editor_shell
    viewer = open_editor(root)
    session = root._active_editor_session
    assert session is not None
    previous_state = {"sentinel": "keep-existing-session-state"}
    session.state = previous_state
    captures = []
    capture = viewer.capture_session_state

    def capture_once():
        captures.append(True)
        return capture()

    def refuse_teardown(*_args, **_kwargs):
        raise OSError("synthetic player teardown refusal")

    monkeypatch.setattr(viewer, "capture_session_state", capture_once)
    monkeypatch.setattr(viewer, "_teardown_player", refuse_teardown)
    stub_warning(monkeypatch)

    root._minimize_active_editor(viewer)

    assert captures == [True]
    assert root._active_clip_viewer is viewer
    assert root._active_editor_session is session
    assert getattr(root, "_parked_editor_shell", None) is None
    assert session.state is previous_state
    assert viewer.isVisible()
    assert not viewer._teardown_complete


@pytest.mark.parametrize("replace_result", ["raise", "false"])
def test_parked_shell_survives_replace_failure_before_reloading_page(
        b1_editor_shell, monkeypatch, replace_result):
    from main import MainWindow

    root = b1_editor_shell
    viewer = open_editor(root)
    session = root._active_editor_session
    assert session is not None
    assert root._park_active_editor()
    parked_state = session.state
    old_page, old_path = viewer.clip_page, viewer.clip_path
    assert root._parked_editor_shell is viewer
    assert not viewer.isVisible()
    teardown_calls = []

    def refuse_replace(*_args, **_kwargs):
        if replace_result == "raise":
            raise OSError("synthetic parked-shell replacement refusal")
        return False

    def unexpected_teardown(*_args, **_kwargs):
        teardown_calls.append(True)

    monkeypatch.setattr(viewer, "replace_clip", refuse_replace)
    monkeypatch.setattr(viewer, "_teardown_player", unexpected_teardown)
    stub_warning(monkeypatch)
    root._editor_upload_coordinator = object()

    MainWindow._show_editor_session(root, session, QPixmap())

    assert root._parked_editor_shell is viewer
    assert root._active_clip_viewer is None
    assert root._active_editor_session is None
    assert root._editor_sessions == [session]
    assert session.state is parked_state
    assert viewer.clip_page is old_page and viewer.clip_path == old_path
    assert not viewer.isVisible()
    assert teardown_calls == []


def test_forced_main_finalizer_logs_save_failure_and_releases_resources(
        qtbot, monkeypatch, tmp_path, caplog, capsys):
    import main
    from core import export_queue
    from PySide6.QtWidgets import QApplication
    from test_pyside_ui_smoke import _CaptureBridgeStub, _CaptureCardStub
    from ui.clip_viewer import ClipViewer

    previous_export_initializing = export_queue._initializing
    window = None
    viewer = None
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(main, "CaptureCardClient", _CaptureCardStub)
    monkeypatch.setattr(main, "CaptureBridge", _CaptureBridgeStub)
    for name in ("_setup_hotkeys", "_start_mic_recorder", "start_engine", "stop_engine",
                 "_start_library_recovery", "_start_background_services"):
        monkeypatch.setattr(main.MainWindow, name, lambda _self: None)
    try:
        window = main.MainWindow()
        qtbot.addWidget(window)
        window.clip_metadata_manager.migrate(target_version=5)
        path = tmp_path / "finalizer-local.mp4"
        path.write_bytes(b"synthetic finalizer media")
        manager = window.clip_metadata_manager
        manager.set(str(path), tag="committed", description="keep this")

        for method in ("_set_media_source", "_prepare_playback_source",
                       "_discover_audio_sources_async", "_load_timeline_thumbnails_if_idle"):
            monkeypatch.setattr(ClipViewer, method, lambda *_args, **_kwargs: None)
        viewer = ClipViewer(str(path), None, settings_manager=window.settings_manager,
                            metadata_manager=manager)
        qtbot.addWidget(viewer)
        make_selected_dirty(viewer, "unsaved valid edit")
        writes = []

        def fail_details(*_args, **_kwargs):
            writes.append(True)
            raise OSError("synthetic detail persistence failure")

        monkeypatch.setattr(manager, "set_details", fail_details)
        # The old process must remain usable after shutdown cleanup; do not quit
        # pytest's QApplication from this bounded integration test.
        monkeypatch.setattr(QApplication, "instance", staticmethod(lambda: None))
        window._active_clip_viewer = viewer

        window._perform_full_shutdown()

        assert len(writes) == 1  # finalizer does not retry failed detail persistence
        assert window.capture_card.closed
        assert viewer._teardown_complete
        assert manager.get(str(path)) == {"tag": "committed", "description": "keep this"}
        captured = capsys.readouterr()
        messages = caplog.text + captured.out + captured.err
        assert "synthetic detail persistence failure" in messages
    finally:
        try:
            # The test checks the failed-save UI state before reaching cleanup.
            # Reset only synthetic dirty markers so QtBot can destroy widgets.
            if viewer is not None:
                viewer._tag_edit.setModified(False)
                viewer._desc_edit.setModified(False)
                if not viewer._teardown_complete:
                    viewer._teardown_player(wait_for_workers=False)
            if window is not None:
                window._active_clip_viewer = None
        finally:
            export_queue.set_initializing(previous_export_initializing)
