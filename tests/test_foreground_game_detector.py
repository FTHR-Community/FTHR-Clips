import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'FTHR_UI'))

from core.game_detector import (  # noqa: E402
    ForegroundGameDetector,
    GameWindow,
    crop_profile_for_window,
    is_probable_game,
    normalise_custom_game_rules,
)


def _window(**overrides):
    values = {
        'hwnd': 101,
        'pid': 202,
        'title': 'Example Game',
        'exe_path': r'C:\Standalone\game.exe',
        'exe_name': 'game.exe',
        'class_name': 'GameWindow',
        'right': 1920,
        'bottom': 1080,
    }
    values.update(overrides)
    return GameWindow(**values)


def test_store_path_is_detected_even_when_windowed():
    assert is_probable_game(_window(
        exe_path=r'D:\SteamLibrary\steamapps\common\Example\game.exe'))


def test_browser_fullscreen_is_not_detected():
    assert not is_probable_game(_window(
        exe_name='chrome.exe', class_name='Chrome_WidgetWin_1',
        is_borderless=True, is_fullscreen=True))


def test_windows_input_experience_is_not_detected_when_metadata_is_incomplete():
    assert not is_probable_game(_window(
        title='Windows Input Experience',
        exe_path='',
        exe_name='',
        class_name='',
        is_borderless=True,
        is_fullscreen=True,
    ))


def test_large_borderless_window_without_process_metadata_is_not_detected():
    assert not is_probable_game(_window(
        exe_path='',
        exe_name='',
        class_name='UnknownWindow',
        is_borderless=True,
        is_fullscreen=True,
    ))


def test_manual_java_rule_requires_the_title_match():
    rules = normalise_custom_game_rules([{
        'exe_path': r'C:\Games\Minecraft\runtime\javaw.exe',
        'title_contains': 'Minecraft',
    }])
    assert is_probable_game(_window(
        title='Minecraft 1.21', exe_path=r'D:\Runtime\javaw.exe',
        exe_name='javaw.exe', right=800, bottom=600), rules)
    assert not is_probable_game(_window(
        title='Mod Development IDE', exe_path=r'D:\Runtime\javaw.exe',
        exe_name='javaw.exe', right=800, bottom=600), rules)


def test_game_crop_profile_is_normalized_persisted_and_matched():
    rules = normalise_custom_game_rules([{
        'exe_path': r'C:\Standalone\game.exe',
        'crop_profile': {
            'enabled': True,
            'x': 0.1,
            'y': 0.125,
            'w': 0.8,
            'h': 0.75,
        },
    }])

    profile = crop_profile_for_window(_window(), rules)
    assert profile == {
        'enabled': True,
        'x': 0.1,
        'y': 0.125,
        'w': 0.8,
        'h': 0.75,
    }


def test_disabled_game_crop_profile_is_not_applied():
    rules = normalise_custom_game_rules([{
        'exe_path': r'C:\Standalone\game.exe',
        'crop_profile': {
            'enabled': False, 'x': 0.2, 'y': 0.2, 'w': 0.6, 'h': 0.6,
        },
    }])

    assert crop_profile_for_window(_window(), rules) is None


def test_detector_requires_two_stable_observations_before_emitting():
    detector = ForegroundGameDetector(
        stable_polls=2, window_provider=lambda _pid: None)
    detected = []
    detector.game_detected.connect(detected.append)
    candidate = _window()

    detector._observe(candidate)
    assert detected == []
    detector._observe(candidate)
    assert detected == [candidate]


def test_detector_reemits_same_game_after_monitor_resolution_change():
    detector = ForegroundGameDetector(
        stable_polls=2, window_provider=lambda _pid: None)
    detected = []
    detector.game_detected.connect(detected.append)
    original = _window(
        right=1920, bottom=1080, monitor_width=1920, monitor_height=1080)
    resized = _window(
        right=2560, bottom=1440, monitor_width=2560, monitor_height=1440)

    detector._observe(original)
    detector._observe(original)
    assert detected == [original]

    # A display-mode transition is debounced just like a new HWND, so a
    # transient intermediate size does not restart the capture engine.
    detector._observe(resized)
    assert detected == [original]
    detector._observe(resized)

    assert detected == [original, resized]


def test_detector_tracks_monitor_resize_when_window_bounds_stay_constant():
    detector = ForegroundGameDetector(
        stable_polls=1, window_provider=lambda _pid: None)
    detected = []
    detector.game_detected.connect(detected.append)
    first = _window(
        right=1280, bottom=720, monitor_width=1920, monitor_height=1080)
    second = _window(
        right=1280, bottom=720, monitor_width=2560, monitor_height=1440)

    detector._observe(first)
    detector._observe(second)

    assert detected == [first, second]


def test_detector_emits_loss_after_two_empty_observations():
    detector = ForegroundGameDetector(
        stable_polls=2, window_provider=lambda _pid: None)
    lost = []
    detector.game_lost.connect(lambda: lost.append(True))
    candidate = _window()

    detector._observe(candidate)
    detector._observe(candidate)
    detector._observe(None)
    assert lost == []
    detector._observe(None)
    assert lost == [True]


def test_blacklist_takes_precedence_over_manual_and_automatic_detection(monkeypatch):
    candidate = _window(is_known_game=True)
    rule = {'exe_path': candidate.exe_path}
    detector = ForegroundGameDetector(stable_polls=1, custom_game_rules=[rule],
                                      blacklist_rules=[rule], window_provider=lambda pid: candidate)
    detected = []
    detector.game_detected.connect(detected.append)
    monkeypatch.setattr(detector._stop_event, 'wait', lambda timeout: detector._stop_event.set())
    detector._run()
    assert detected == []
    detector.set_blacklist_rules([])
    detector._stop_event.clear()
    detector._run()
    assert detected == [candidate]
