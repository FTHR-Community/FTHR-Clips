from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8')
BRIDGE_SOURCE = (ROOT / 'FTHR_UI' / 'core' / 'capture_bridge.py').read_text(
    encoding='utf-8')
ENGINE_SOURCE = (ROOT / 'FTHRcapture' / 'FTHRclips' / 'src' / 'main.cpp').read_text(
    encoding='utf-8')
ENGINE_HEADER = (ROOT / 'FTHRcapture' / 'FTHRclips' / 'include' / 'shared_memory.h').read_text(
    encoding='utf-8')


def test_background_mode_defers_heavy_main_ui_but_starts_core_services():
    assert 'def __init__(self, *, background_start: bool = False)' in MAIN_SOURCE
    assert 'if not self._background_start:\n            self.ensure_main_ui()' in MAIN_SOURCE
    assert 'self._setup_hotkeys()\n        self._start_background_services()' in MAIN_SOURCE
    assert "background_start = '--background' in sys.argv" in MAIN_SOURCE


def test_close_to_tray_and_explicit_exit_have_separate_paths():
    assert 'WindowHiddenToTray' in MAIN_SOURCE
    assert "self.settings_manager.get('close_to_tray', True)" in MAIN_SOURCE
    assert "self.power_btn.setObjectName('powerBtn')" in MAIN_SOURCE
    assert 'self.power_btn.clicked.connect(self.request_full_exit)' in MAIN_SOURCE
    assert 'self.capture_card.show_background_capture(source)' in MAIN_SOURCE
    assert 'def request_full_exit(self)' in MAIN_SOURCE
    assert 'self._tray_icon.hide()' in MAIN_SOURCE
    assert 'def _perform_full_shutdown(self)' in MAIN_SOURCE
    assert '_FINALIZATION_GRACE_SECONDS = 1.5' in MAIN_SOURCE


def test_capture_status_is_top_level_and_not_duplicated_inside_popups():
    assert 'mc.addWidget(self.status_label)' in MAIN_SOURCE
    assert "self._set_status('APPLYING SETTINGS', status_idle_qss())" in MAIN_SOURCE
    assert 'apply_state_lbl' not in MAIN_SOURCE


def test_native_engine_has_a_graceful_shutdown_command_without_layout_change():
    assert 'SHUTDOWN = 11' in ENGINE_HEADER
    assert 'SHUTDOWN = 11' in BRIDGE_SOURCE
    assert 'def request_engine_shutdown(self)' in BRIDGE_SOURCE
    assert 'case fthr::CommandType::SHUTDOWN:' in ENGINE_SOURCE
    assert 'while (!shutdown_requested)' in ENGINE_SOURCE
    assert 'engine.Shutdown();' in ENGINE_SOURCE


def test_source_mode_prefers_the_fresh_direct_project_engine():
    cfr_release = (
        "project_root / 'FTHRclips' / 'x64' / 'CFRRelease' / 'FTHRclips.exe'"
    )
    direct_release = (
        "project_root / 'FTHRclips' / 'x64' / 'Release' / 'FTHRclips.exe'"
    )
    compatibility_release = (
        "project_root / 'x64' / 'Release' / 'FTHRClips.exe'"
    )

    assert cfr_release in MAIN_SOURCE
    assert direct_release in MAIN_SOURCE
    assert compatibility_release in MAIN_SOURCE
    assert MAIN_SOURCE.index(cfr_release) < MAIN_SOURCE.index(direct_release)
    assert MAIN_SOURCE.index(direct_release) < MAIN_SOURCE.index(compatibility_release)
    assert 'self.engine_path = max(' in MAIN_SOURCE
    assert 'print(f"Engine found: {self.engine_path.resolve()}")' in MAIN_SOURCE
