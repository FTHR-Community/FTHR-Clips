"""Foreground detection follows the selected mode for manual and automatic sources."""
from functools import partial
from types import SimpleNamespace
import pytest


@pytest.fixture
def setup_auto(qtbot, monkeypatch, tmp_path):
    import main
    from core.settings_manager import SettingsManager
    from core.game_detector import ForegroundGameDetector, GameWindow
    from test_source_authority_and_save_reporting import _bind_real_admission_helpers
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(main.sys, 'platform', 'win32')
    monkeypatch.setattr(main.GameDetectionPopup, '_refresh_running_windows', lambda self: None)
    settings = SettingsManager()
    settings.update({'capture_mode': 'desktop', 'capture_source_authority': 'manual'})
    candidate = GameWindow(hwnd=88, pid=9, title='Better Minecraft',
        exe_path=r'C:\ATLauncher\runtime\bin\javaw.exe', exe_name='javaw.exe',
        class_name='GLFW30', process_created=1234, right=1920, bottom=1080)
    detector = ForegroundGameDetector(window_provider=lambda _pid: candidate)
    monkeypatch.setattr(detector, 'start', lambda: True)
    monkeypatch.setattr(main, 'is_capture_window_valid', lambda hwnd: hwnd == candidate.hwnd)
    monkeypatch.setattr(main, 'get_window_info', lambda hwnd, own_pid=None: candidate)
    popup = main.GameDetectionPopup(settings, SimpleNamespace(hotkeys={}))
    qtbot.addWidget(popup)
    restarts = []
    host = SimpleNamespace(settings_manager=settings, _game_detector=detector,
        _pending_game_window=None, _source_authority_revision=0, _source_intent_revision=0,
        game_detection_btn=SimpleNamespace(setToolTip=lambda text: None),
        capture_card=SimpleNamespace(show_capturing=lambda name: None),
        hotkey_manager=SimpleNamespace(hotkeys={}),
        _game_dismiss_timer=SimpleNamespace(stop=lambda: None, start=lambda _: None),
        _restart_capture_engine=lambda: restarts.append(settings.get('target_hwnd')),
        _active_game_hwnd=None, _active_game_window=None)
    _bind_real_admission_helpers(host, main)
    for name in ('_game_window_hwnd', '_game_window_name', '_game_candidate_authorized',
                 '_switch_capture_to_game', '_set_capture_window_source',
                 '_show_game_detection_prompt', '_on_confirm_game_detection'):
        setattr(host, name, partial(getattr(main.MainWindow, name), host))
    detector.game_detected.connect(lambda window: main.MainWindow._handle_game_candidate(host, window))
    popup.custom_games_changed.connect(lambda rules: main.MainWindow._on_custom_game_rules_changed(host, rules))
    popup.configuration_changed.connect(lambda mode:
        main.MainWindow._on_game_detection_configuration_changed(host, mode))
    callbacks = []
    monkeypatch.setattr(main.QTimer, 'singleShot', lambda delay, callback: callbacks.append(callback))
    yield main, settings, candidate, detector, popup, host, restarts, callbacks
    detector.stop()


def _add_exact_executable(popup, candidate):
    popup._custom_rules.append({'exe_path': candidate.exe_path, 'exe_name': candidate.exe_name})
    popup._save_rules(added=True)


def test_add_exact_executable_from_desktop_rearms_stale_candidate_then_switches(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    # Ignore an already queued observation from the previous detection mode.
    popup._set_mode('prompt')
    host.capture_card.show_prompt = lambda _: None
    detector._observe(candidate)
    detector._observe(candidate)
    main.MainWindow._on_manual_capture_source_intent(host)
    old_token = detector._stop_event
    assert settings.get('capture_mode') == 'desktop'
    _add_exact_executable(popup, candidate)
    assert settings.get('capture_source_authority') == 'manual'
    assert popup.get_summary() == 'Game detection: Prompt First'
    from core.game_detector import is_probable_game
    assert is_probable_game(candidate, detector._custom_game_rules)
    assert detector._stop_event is not old_token
    detector._publish_observation((old_token, candidate, False))
    assert settings.get('capture_mode') == 'desktop'
    popup._set_mode('auto')
    detector._observe(candidate)
    assert settings.get('capture_mode') == 'desktop'
    detector._observe(candidate)
    assert settings.get('capture_mode') == 'window'
    assert settings.get('target_hwnd') == candidate.hwnd
    assert settings.get('target_window_identity')['process_created'] == 1234
    assert not callbacks
    assert restarts == [candidate.hwnd]


def test_auto_switch_detects_a_new_game_while_manual_window_owns_capture(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    settings.update({'capture_mode': 'window', 'target_hwnd': 777,
                     'capture_source_authority': 'manual'})
    _add_exact_executable(popup, candidate)
    assert settings.get('capture_source_authority') == 'manual'
    assert popup.get_summary() == 'Game detection: Auto-Switch'
    assert popup.auto_switch_btn.isChecked()
    detector._observe(candidate)
    detector._observe(candidate)
    assert settings.get('capture_source_authority') == 'automatic'
    assert settings.get('target_hwnd') == candidate.hwnd
    assert not callbacks
    assert restarts == [candidate.hwnd]


@pytest.mark.parametrize('change', ['metadata', 'remove', 'add'])
def test_rule_edits_keep_current_source_authority(setup_auto, change):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    popup._custom_rules = [] if change == 'remove' else [{'exe_path': candidate.exe_path}]
    popup._save_rules(added=change == 'add')
    assert settings.get('capture_source_authority') == 'manual'
    assert not callbacks


def test_prompt_first_waits_for_confirmation_after_manual_selection(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    prompts = []
    host.capture_card.show_prompt = prompts.append
    popup.prompt_switch_btn.click()
    main.MainWindow._on_manual_capture_source_intent(host)

    detector._observe(candidate)
    detector._observe(candidate)

    assert prompts and candidate.display_name in prompts[-1]
    assert settings.get('capture_mode') == 'desktop'
    assert not restarts
    host._on_confirm_game_detection()
    assert settings.get('target_hwnd') == candidate.hwnd
    assert restarts == [candidate.hwnd]


def test_manual_selection_reconsiders_an_already_observed_game(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    detector._observe(candidate)
    detector._observe(candidate)
    assert restarts == [candidate.hwnd]
    previous_observation = detector._stop_event

    settings.update({'capture_mode': 'desktop', 'target_hwnd': 0})
    main.MainWindow._on_manual_capture_source_intent(host)
    detector._publish_observation((previous_observation, candidate, False))
    assert settings.get('capture_mode') == 'desktop'

    detector._observe(candidate)
    detector._observe(candidate)

    assert restarts == [candidate.hwnd, candidate.hwnd]
    assert popup.get_summary() == 'Game detection: Auto-Switch'


def test_auto_setup_does_not_bypass_capture_admission(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    from core.capture_admission import AdmissionPolicy
    settings.set('capture_admission_policy', AdmissionPolicy(enabled=True).to_dict())
    host._set_status = lambda *a: None
    host.push_error = lambda *a, **k: None
    _add_exact_executable(popup, candidate)
    detector._observe(candidate)
    detector._observe(candidate)
    assert settings.get('capture_mode') == 'desktop'
    assert not callbacks
    assert not restarts

@pytest.mark.parametrize('process_state', ['absent', 'exited'])
def test_restored_same_selected_game_recovers_after_failed_startup(setup_auto, process_state):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    from core.source_authority import window_identity
    settings.update({'capture_mode': 'window', 'target_hwnd': candidate.hwnd,
        'target_window_identity': window_identity(candidate),
        'capture_source_authority': 'automatic'})
    host.engine_process = None if process_state == 'absent' else SimpleNamespace(poll=lambda: 1)
    # Choosing Auto-Switch after failure must reconsider the existing identity.
    popup._set_mode('auto')
    detector._observe(candidate)
    detector._observe(candidate)
    assert restarts == [candidate.hwnd]
    assert not callbacks
    assert host._active_game_hwnd == candidate.hwnd
    assert host._auto_capture_hwnd == candidate.hwnd
    assert host._auto_capture_identity == window_identity(candidate)


@pytest.mark.parametrize('gate', [
    'manual', 'prompt', 'running', 'restart', 'applying', 'launch',
    'handshake', 'queued', 'terminal',
])
def test_same_selected_candidate_never_duplicates_owned_start_or_overrides_pin(setup_auto, gate):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    from core.source_authority import window_identity
    settings.update({'capture_mode': 'window', 'target_hwnd': candidate.hwnd,
        'target_window_identity': window_identity(candidate),
        'capture_source_authority': 'automatic'})
    host.engine_process = None
    if gate == 'manual': settings.set('capture_source_authority', 'manual')
    elif gate == 'prompt': settings.set('game_detection_mode', 'prompt')
    elif gate == 'running': host.engine_process = SimpleNamespace(poll=lambda: None)
    elif gate == 'restart': host._restart_pending = True
    elif gate == 'applying': host._capture_settings_applying = True
    elif gate == 'launch': host._pending_launch_config = object()
    elif gate == 'handshake': host._engine_handshake_generation = 4
    elif gate in {'queued', 'terminal'}:
        host.engine_process = SimpleNamespace(poll=lambda: None)
        host._accepted_capture_session = SimpleNamespace(
            request=SimpleNamespace(source=SimpleNamespace(source_mode='desktop')))
        host._latest_capture_intent_state = gate
        host._latest_capture_intent_consumed = gate == 'terminal'
    main.MainWindow._handle_game_candidate(host, candidate)
    assert not restarts
    assert not callbacks


def test_same_selected_candidate_recovers_while_desktop_engine_is_alive(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    from core.source_authority import window_identity
    identity = window_identity(candidate)
    settings.update({'capture_mode': 'window', 'target_hwnd': candidate.hwnd,
        'target_window_identity': identity, 'capture_source_authority': 'automatic'})
    host.engine_process = SimpleNamespace(poll=lambda: None)
    host._accepted_capture_session = SimpleNamespace(
        request=SimpleNamespace(source=SimpleNamespace(source_mode='desktop')))

    main.MainWindow._handle_game_candidate(host, candidate)

    assert restarts == [candidate.hwnd]
    assert not callbacks

    host._accepted_capture_session.request.source = SimpleNamespace(
        source_mode='window', live_identity=identity)
    main.MainWindow._handle_game_candidate(host, candidate)

    assert restarts == [candidate.hwnd]


def test_game_capture_card_waits_for_accepted_window_source(setup_auto):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    settings.set('capture_source_authority', 'automatic')
    notifications = []
    host.capture_card.show_capturing = notifications.append

    assert main.MainWindow._switch_capture_to_game(host, candidate)
    assert restarts == [candidate.hwnd]
    assert notifications == []

    source = SimpleNamespace(source_mode='window', authority='automatic',
        live_identity=host._auto_capture_identity, display_name=candidate.display_name)
    request = SimpleNamespace(source=source,
        source_intent_revision=host._source_intent_revision - 1)
    main.MainWindow._notify_accepted_automatic_game_capture(host, request)
    assert notifications == []

    request.source_intent_revision = host._source_intent_revision
    main.MainWindow._notify_accepted_automatic_game_capture(host, request)
    assert notifications == [candidate.display_name]


@pytest.mark.parametrize('source', ['window', 'desktop'])
def test_auto_source_commit_keeps_detection_mode_after_picker_refresh(setup_auto, source):
    main, settings, candidate, detector, popup, host, restarts, callbacks = setup_auto
    host.game_detection_popup = popup
    summaries = []
    popup.summary_changed.connect(summaries.append)
    def picker_refresh(*args, automatic=False):
        # Source picker control refreshes can synchronously claim manual
        # authority before the automatic setter writes its final state.
        main.MainWindow._on_manual_capture_source_intent(host)
        assert popup.get_summary() == 'Game detection: Auto-Switch'
        settings.set('capture_source_authority', 'automatic' if automatic else 'manual')
        return True
    host.source_popup = SimpleNamespace(set_capture_window=picker_refresh,
                                        set_capture_desktop=picker_refresh)
    if source == 'window':
        assert main.MainWindow._set_capture_window_source(host, candidate.as_capture_window(), automatic=True)
    else:
        assert main.MainWindow._set_capture_desktop_source(host, automatic=True)
    assert not summaries
    assert popup.get_summary() == 'Game detection: Auto-Switch'
    assert popup.auto_switch_btn.isChecked()
