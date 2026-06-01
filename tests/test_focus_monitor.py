import sys
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from core.focus_monitor import FocusMonitor


def test_title_matches_exact(qtbot):
    m = FocusMonitor(target_name='Counter-Strike 2')
    assert m._title_matches('Counter-Strike 2') is True


def test_title_matches_partial(qtbot):
    m = FocusMonitor(target_name='CS2')
    assert m._title_matches('CS2 - Valve') is True


def test_title_no_match(qtbot):
    m = FocusMonitor(target_name='Counter-Strike 2')
    assert m._title_matches('Discord') is False


def test_title_case_insensitive(qtbot):
    m = FocusMonitor(target_name='counter-strike 2')
    assert m._title_matches('Counter-Strike 2') is True
