"""Replay budget controls and preference round trips."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QSpinBox

from core.replay_storage import MIB, REPLAY_STORAGE_KEY, ReplayFolderStatus
from ui.replay_storage import ReplayStorageWidget


class _Settings(dict):
    def set(self, key, value):
        self[key] = value

    def save_settings(self):
        return True


def _show_budget(qtbot, settings):
    widget = ReplayStorageWidget(settings, {'source': {'medium': 25_000}},
                                 QComboBox, QSpinBox)
    qtbot.addWidget(widget)
    widget.show()
    return widget


def _choose(combo, choice):
    combo.setCurrentIndex(combo.findData(choice))


def test_manual_slider_keyboard_steps_and_legacy_budget_round_trip(qtbot):
    settings = _Settings(replay_storage={'memory_mib': 2049, 'disk_mib': 4097})
    widget = _show_budget(qtbot, settings)
    assert widget.budget_mode_combo.currentData() == 'auto'
    assert not widget.memory_budget.isVisible()
    assert 'budget_mode' not in settings[REPLAY_STORAGE_KEY]

    with qtbot.waitSignal(widget.applied):
        _choose(widget.budget_mode_combo, 'manual')
        assert widget.memory_budget.isVisible()
        assert not widget.disk_budget.isVisible()
        assert 'budget_mode' not in settings[REPLAY_STORAGE_KEY]
    assert settings[REPLAY_STORAGE_KEY]['budget_mode'] == 'manual'
    assert settings[REPLAY_STORAGE_KEY]['memory_mib'] == 2049

    slider = widget.memory_budget
    slider.setFocus()
    with qtbot.waitSignal(widget.applied):
        qtbot.keyClick(slider, Qt.Key.Key_Home)
        assert slider.accessibleDescription().startswith('100 MB.')
        qtbot.keyClick(slider, Qt.Key.Key_Right)
        assert slider.accessibleDescription().startswith('250 MB.')
        assert settings[REPLAY_STORAGE_KEY]['memory_mib'] == 2049
    assert settings[REPLAY_STORAGE_KEY]['memory_mib'] == 239
    assert settings[REPLAY_STORAGE_KEY]['disk_mib'] == 4097

    with qtbot.waitSignal(widget.applied):
        _choose(widget.budget_mode_combo, 'auto')
    assert settings[REPLAY_STORAGE_KEY]['budget_mode'] == 'auto'
    reopened = _show_budget(qtbot, settings)
    assert reopened.budget_mode_combo.currentData() == 'auto'
    assert not reopened.memory_budget.isVisible()
    with qtbot.waitSignal(reopened.applied):
        _choose(reopened.budget_mode_combo, 'manual')
    assert reopened.memory_budget.accessibleDescription().startswith('250 MB.')
    assert settings[REPLAY_STORAGE_KEY]['memory_mib'] == 239


def test_disk_budget_warning_only_compares_manual_limit(qtbot, monkeypatch):
    monkeypatch.setattr('ui.replay_storage.inspect_replay_folder',
                        lambda path: ReplayFolderStatus(20_000 * MIB, 'ssd'))
    settings = _Settings(replay_storage={'mode': 'disk', 'disk_mib': 64})
    widget = _show_budget(qtbot, settings)
    qtbot.waitUntil(lambda: 'free on this drive' in widget.space_label.text())
    assert not widget.memory_budget.isVisible()
    assert not widget.disk_budget.isVisible()
    assert not widget.capacity_warning.isVisible()

    _choose(widget.budget_mode_combo, 'manual')
    assert widget.memory_budget.isVisible()
    assert widget.disk_budget.isVisible()
    assert 'disk budget is below' in widget.capacity_warning.text()
    qtbot.keyClick(widget.disk_budget, Qt.Key.Key_End)
    assert widget.disk_budget.accessibleDescription().startswith('1,000 GB.')
    assert not widget.capacity_warning.isVisible()


def test_automatic_disk_estimate_includes_one_free_chunk(qtbot, monkeypatch):
    monkeypatch.setattr('ui.replay_storage.inspect_replay_folder',
                        lambda path: ReplayFolderStatus(110 * MIB, 'ssd'))
    monkeypatch.setattr(ReplayStorageWidget, '_estimated_bytes', lambda self: 100 * MIB)
    widget = _show_budget(qtbot, _Settings(replay_storage={'mode': 'disk'}))
    qtbot.waitUntil(lambda: 'free on this drive' in widget.space_label.text())
    assert '122 MB' in widget.space_label.text()
    assert widget.capacity_warning.isVisible()
    assert 'less free space' in widget.capacity_warning.text()

    _choose(widget.budget_mode_combo, 'manual')
    assert '105 MB' in widget.space_label.text()
    assert not widget.capacity_warning.isVisible()
