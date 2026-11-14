"""Focused behavioural checks for the passive capture-admission editor."""

from __future__ import annotations

from html import unescape

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QWidget

from core.capture_admission import (
    AdmissionDataError,
    AdmissionPolicy,
    AdmissionValidationError,
    add_executable,
    empty_policy,
)
from ui.capture_admission import CaptureAdmissionWidget


class _Manager:
    def __init__(self, policy=None):
        self.settings = {}
        self.policy = empty_policy() if policy is None else policy
        self.raw = None
        self.save_calls = []
        self.save_result = True
        self.save_error = None

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def read_capture_admission_policy(self):
        if self.raw is not None:
            error = AdmissionDataError('future capture admission policy')
            error.raw = self.raw
            raise error
        return self.policy

    def save_capture_admission_policy(self, policy, *, expected_revision):
        self.save_calls.append((policy, expected_revision))
        if self.save_error is not None:
            raise self.save_error
        if self.save_result is not True:
            return self.save_result
        if expected_revision != self.policy.revision:
            return False
        self.policy = policy
        return True


def test_widget_exposes_passive_controls_and_refresh_only_signal(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    refreshes = []
    widget.refresh_windows_requested.connect(lambda: refreshes.append(True))

    for name in (
            'enabled_checkbox', 'executable_list', 'add_file_button',
            'refresh_windows_button', 'running_windows_combo',
            'add_window_button', 'remove_button', 'reload_button'):
        assert getattr(widget, name).accessibleName()
    assert refreshes == []
    widget.refresh_windows_button.click()
    assert refreshes == [True]
    assert manager.save_calls == []


def test_window_records_are_detached_and_full_identity_is_in_tooltip(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    path = r'C:\Games\<literal>\very-long-game-executable-name.exe'
    records = [{'exe_path': path, 'title': '<Window & title>'}]

    widget.set_available_windows(records)
    records[0]['exe_path'] = r'C:\Games\changed.exe'

    assert widget._available_windows == [
        {'exe_path': path, 'display_name': '<Window & title>'}]
    assert widget.running_windows_combo.itemText(0) == '<Window & title>'
    tooltip = widget.running_windows_combo.itemData(
        0, Qt.ItemDataRole.ToolTipRole)
    assert unescape(tooltip.removeprefix('<qt>').removesuffix('</qt>')) == path
    assert '&lt;literal&gt;' in tooltip
    assert '&amp;' not in widget.running_windows_combo.itemText(0)


def test_add_selected_window_uses_one_cas_and_one_revision_signal(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    revisions = []
    widget.policy_changed.connect(revisions.append)

    widget.add_window_button.click()

    assert len(manager.save_calls) == 1
    assert manager.save_calls[0][1] == 0
    assert revisions == [1]
    assert widget.executable_list.count() == 1
    assert widget.executable_list.item(0).data(Qt.ItemDataRole.UserRole) == \
        r'c:\games\game.exe'


def test_idempotent_add_is_noop_without_save_or_signal(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    widget.add_window_button.click()
    manager.save_calls.clear()
    revisions = []
    widget.policy_changed.connect(revisions.append)

    widget.add_window_button.click()

    assert manager.save_calls == []
    assert revisions == []
    assert widget.executable_list.count() == 1


def test_failed_cas_keeps_visible_policy_and_reports_error(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    manager.save_result = False
    revisions = []
    widget.policy_changed.connect(revisions.append)

    widget.add_window_button.click()

    assert len(manager.save_calls) == 1
    assert manager.policy.executable_paths == ()
    assert widget.executable_list.count() == 0
    assert revisions == []
    assert 'could not add executable' in widget.error_label.text().lower()
    assert 'list changed' in widget.error_label.text().lower()
    assert 'compare-and-swap' not in widget.error_label.text().lower()


def test_failed_toggle_write_restores_previous_toggle_state(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    manager.save_result = False

    widget.enabled_checkbox.click()

    assert widget.enabled_checkbox.isChecked() is False
    assert manager.policy.enabled is False
    assert len(manager.save_calls) == 1


def test_invalid_file_dialog_input_keeps_previous_state_without_save(
        qapp, qtbot, monkeypatch):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    monkeypatch.setattr(
        'ui.capture_admission.QFileDialog.getOpenFileName',
        lambda *_args, **_kwargs: (r'C:\Games\bad.txt', ''))

    def reject_path(_policy, _path):
        raise AdmissionValidationError('executable path must end in .exe')

    monkeypatch.setattr('ui.capture_admission.add_executable', reject_path)
    widget.add_file_button.click()

    assert manager.save_calls == []
    assert widget.executable_list.count() == 0
    assert 'no changes were saved' in widget.error_label.text().lower()


def test_maximum_approved_executables_rejects_129th_without_save(
        qapp, qtbot, monkeypatch):
    manager = _Manager()
    policy = empty_policy()
    for index in range(128):
        policy = add_executable(policy, f'C:\\Games\\Game{index}.exe')
    manager.policy = policy
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.reload_policy()
    monkeypatch.setattr(
        'ui.capture_admission.QFileDialog.getOpenFileName',
        lambda *_args, **_kwargs: (r'C:\Games\Game128.exe', ''))

    widget.add_file_button.click()

    assert manager.save_calls == []
    assert widget.executable_list.count() == 128
    assert 'no changes were saved' in widget.error_label.text().lower()


def test_disk_failure_preserves_list_toggle_and_signal(qapp, qtbot):
    manager = _Manager()
    manager.policy = add_executable(manager.policy, r'C:\Games\Existing.exe')
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.reload_policy()
    manager.save_error = OSError('replace failed')
    revisions = []
    widget.policy_changed.connect(revisions.append)

    widget.enabled_checkbox.click()

    assert widget.enabled_checkbox.isChecked() is False
    assert widget.executable_list.count() == 1
    assert revisions == []
    assert 'save failed' in widget.error_label.text().lower()
    assert 'reload and retry' in widget.error_label.text().lower()


def test_reload_after_conflict_clears_failure_and_keeps_policy(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    manager.save_result = False
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    widget.add_window_button.click()
    assert 'save failed' in widget.error_label.text().lower()

    manager.save_result = True
    widget.reload_button.click()

    assert widget.error_label.text() == ''
    assert widget.status_label.text() == ''
    assert widget.status_label.isHidden()
    assert widget.executable_list.count() == 0


def test_large_revision_is_emitted_as_exact_python_int(qapp, qtbot):
    revision = 2 ** 31 + 17
    manager = _Manager(AdmissionPolicy(
        version=1, revision=revision, enabled=False, executable_paths=()))
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    revisions = []
    widget.policy_changed.connect(revisions.append)

    widget.add_window_button.click()

    assert revisions == [revision + 1]
    assert type(revisions[0]) is int


def test_invalid_raw_policy_is_unavailable_and_reload_only(qapp, qtbot):
    manager = _Manager()
    manager.raw = {'version': 99, 'revision': 4, 'enabled': True}
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)

    assert widget._unavailable is True
    assert widget.reload_button.isEnabled()
    assert widget.enabled_checkbox.isEnabled() is False
    assert widget.add_file_button.isEnabled() is False
    assert widget.refresh_windows_button.isEnabled() is False
    assert 'unavailable' in widget.status_label.text().lower()
    widget.set_capture_status('Blocked by host')
    assert 'unavailable' in widget.status_label.text().lower()
    assert 'restart' in widget.error_label.text().lower()
    assert manager.raw['version'] == 99


def test_windows_unavailable_keeps_policy_read_only_with_explicit_info(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=False)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])

    assert 'unavailable' in widget.platform_label.text().lower()
    assert widget.enabled_checkbox.isEnabled() is False
    assert widget.add_window_button.isEnabled() is False
    assert widget.running_windows_combo.count() == 1
    assert manager.save_calls == []


def test_keyboard_controls_remain_focusable_and_long_paths_are_elided(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    path = 'C:\\Games\\' + ('nested-folder\\' * 12) + 'game.exe'
    manager.policy = add_executable(manager.policy, path)
    widget.reload_policy()

    canonical_path = manager.policy.executable_paths[0]
    assert widget.executable_list.item(0).text() != path
    tooltip = widget.executable_list.item(0).toolTip()
    assert unescape(tooltip.removeprefix('<qt>').removesuffix('</qt>')) == canonical_path
    for control in (
            widget.enabled_checkbox, widget.add_file_button,
            widget.refresh_windows_button, widget.running_windows_combo,
            widget.add_window_button, widget.remove_button, widget.reload_button):
        assert control.focusPolicy() != Qt.FocusPolicy.NoFocus


def test_keyboard_space_and_selection_perform_real_actions(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([
        {'exe_path': r'C:\Games\One.exe', 'display_name': 'One'},
        {'exe_path': r'C:\Games\Two.exe', 'display_name': 'Two'},
    ])
    widget.running_windows_combo.setFocus()
    qtbot.keyClick(widget.running_windows_combo, Qt.Key_Down)
    assert widget.running_windows_combo.currentIndex() == 1

    widget.add_window_button.setFocus()
    qtbot.keyClick(widget.add_window_button, Qt.Key_Space)
    assert manager.policy.executable_paths == (r'c:\games\two.exe',)

    widget.enabled_checkbox.setFocus()
    qtbot.keyClick(widget.enabled_checkbox, Qt.Key_Space)

    assert widget.enabled_checkbox.isChecked()
    assert manager.policy.enabled is True
    assert len(manager.save_calls) == 2


def test_long_window_names_do_not_force_horizontal_overflow(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': f'C:\\Games\\Game{index}.exe',
        'display_name': 'Window ' + ('long-title-' * 80),
    } for index in range(6)])
    widget.show()

    def fits():
        if widget.width() <= 0 or widget.running_windows_combo.width() <= 0:
            return False
        for child in widget.findChildren(QWidget):
            if not child.isVisible() or child.width() <= 0:
                continue
            origin = child.mapTo(widget, QPoint(0, 0))
            if origin.x() < 0 or origin.x() + child.width() > widget.width():
                return False
        return True

    for width in (1100, 1440):
        widget.resize(width, 720)
        qtbot.waitUntil(lambda width=width: widget.width() == width and fits(),
                        timeout=3000)


def test_durable_save_keeps_new_revision_when_render_refresh_fails(
        qapp, qtbot, monkeypatch):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    revisions = []
    widget.policy_changed.connect(revisions.append)
    original_render = widget._render_policy

    def fail_render():
        raise ValueError('test render failure')

    monkeypatch.setattr(widget, '_render_policy', fail_render)
    widget.add_window_button.click()

    assert manager.policy.revision == 1
    assert widget._policy == manager.policy
    assert revisions == [1]
    assert type(revisions[0]) is int
    assert widget._unavailable is True
    assert widget.add_file_button.isEnabled() is False
    assert widget.reload_button.isEnabled() is True
    assert 'save succeeded' in widget.error_label.text().lower()
    assert 'could not refresh' in widget.error_label.text().lower()
    assert 'save failed' not in widget.error_label.text().lower()

    monkeypatch.setattr(widget, '_render_policy', original_render)
    widget.reload_button.click()
    assert widget._unavailable is False
    assert widget.executable_list.count() == 1


def test_revision_signal_precedes_render_and_sees_committed_policy(
        qapp, qtbot, monkeypatch):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])
    observations = []

    def observe_revision(revision):
        observations.append(('signal', revision, widget._policy == manager.policy))

    def fail_render():
        observations.append(('render', widget._policy == manager.policy))
        raise ValueError('test render failure')

    widget.policy_changed.connect(observe_revision)
    monkeypatch.setattr(widget, '_render_policy', fail_render)
    widget.add_window_button.click()

    assert observations == [('signal', 1, True), ('render', True)]
    assert widget._policy == manager.policy
    assert 'save succeeded' in widget.error_label.text().lower()


def test_host_status_survives_commit_render_and_clears_to_policy_fallback(qapp, qtbot):
    manager = _Manager()
    widget = CaptureAdmissionWidget(manager, windows_available=True)
    qtbot.addWidget(widget)
    widget.set_available_windows([{
        'exe_path': r'C:\Games\Game.exe', 'display_name': 'Game'}])

    def publish_host_status(_revision):
        widget.set_capture_status('Blocked <host>')

    widget.policy_changed.connect(publish_host_status)
    widget.add_window_button.click()

    assert widget.status_label.text() == 'Blocked <host>'
    assert '&lt;host&gt;' in widget.status_label.toolTip()
    widget.reload_button.click()
    assert widget.status_label.text() == 'Blocked <host>'

    widget.set_capture_status('')
    assert widget.status_label.text() == ''
    assert widget.status_label.isHidden()
