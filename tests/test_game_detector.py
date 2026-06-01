import sys
sys.path.insert(0, '/home/tom/FTHR_Clips/FTHR_UI')

from unittest.mock import MagicMock
from core.game_detector import GameDetector


def _make_window(hwnd, is_game=True):
    return {'hwnd': hwnd, 'display_name': f'Game{hwnd}', 'is_game': is_game}


def test_game_appeared_emitted_for_new_game(qtbot):
    windows = []
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    windows.append(_make_window(1001))
    detector._poll()

    assert len(appeared) == 1
    assert appeared[0]['hwnd'] == 1001


def test_game_appeared_not_repeated(qtbot):
    windows = [_make_window(1001)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    detector._poll()
    detector._poll()

    assert len(appeared) == 1  # second poll: already known, no repeat


def test_non_game_window_ignored(qtbot):
    windows = [_make_window(2001, is_game=False)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    appeared = []
    detector.game_appeared.connect(lambda w: appeared.append(w))

    detector._poll()

    assert appeared == []


def test_game_closed_emitted_when_window_disappears(qtbot):
    windows = [_make_window(1001)]
    detector = GameDetector(enumerate_fn=lambda: windows)
    closed = []
    detector.game_closed.connect(lambda hwnd: closed.append(hwnd))

    detector._poll()       # registers 1001
    windows.clear()
    detector._poll()       # 1001 gone

    assert closed == [1001]
