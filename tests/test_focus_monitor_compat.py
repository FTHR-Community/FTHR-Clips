import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))
from unittest.mock import patch
from core.focus_monitor import FocusMonitor










def test_focus_monitor_emits_focus_lost():
    mon = FocusMonitor('Minecraft')
    lost = []
    mon.focus_lost.connect(lambda: lost.append(True))
    with patch('core.focus_monitor._get_active_window_title', return_value='Discord'):
        mon._poll()
    assert lost == [True]


def test_focus_monitor_emits_focus_regained():
    mon = FocusMonitor('Minecraft')
    mon._focused = False
    regained = []
    mon.focus_regained.connect(lambda: regained.append(True))
    with patch('core.focus_monitor._get_active_window_title', return_value='Minecraft'):
        mon._poll()
    assert regained == [True]


def test_focus_monitor_none_title_stays_quiet():
    mon = FocusMonitor('Minecraft')
    signals = []
    mon.focus_lost.connect(lambda: signals.append('lost'))
    mon.focus_regained.connect(lambda: signals.append('regained'))
    with patch('core.focus_monitor._get_active_window_title', return_value=None):
        mon._poll()
    assert signals == []
