import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))

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


def test_set_enabled_starts_and_stops_polling(qtbot):
    detector = GameDetector(enumerate_fn=lambda: [])

    assert detector.set_enabled(True) is True
    assert detector._timer.isActive()

    assert detector.set_enabled(False) is True
    assert not detector._timer.isActive()


def test_set_enabled_reports_missing_linux_tools(qtbot, monkeypatch):
    from core import game_detector

    monkeypatch.setattr(game_detector, '_linux_enumeration_available', lambda: False)
    detector = GameDetector(enumerate_fn=game_detector._enumerate_linux_windows)

    assert detector.set_enabled(True) is False
    assert not detector._timer.isActive()


def test_xdotool_enumeration_requires_xprop(monkeypatch):
    from core import game_detector

    monkeypatch.setattr(game_detector.linux_tools, 'available',
                        lambda name: name == 'xdotool')

    def fail(*_args, **_kwargs):
        raise AssertionError('must not shell out without xprop')

    monkeypatch.setattr(game_detector.subprocess, 'run', fail)
    assert game_detector._enumerate_via_xdotool() == []
