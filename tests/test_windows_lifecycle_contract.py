from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8')
BRIDGE_SOURCE = (ROOT / 'FTHR_UI' / 'core' / 'capture_bridge.py').read_text(
    encoding='utf-8')
ENGINE_SOURCE = (ROOT / 'FTHRcapture' / 'FTHRclips' / 'src' / 'main.cpp').read_text(
    encoding='utf-8')
ENGINE_HEADER = (ROOT / 'FTHRcapture' / 'FTHRclips' / 'include' / 'shared_memory.h').read_text(
    encoding='utf-8')
LAUNCHER = (ROOT / 'launch_windows.bat').read_text(encoding='utf-8')


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
    from main import MainWindow

    assert 'mc.addWidget(self.status_label)' in MAIN_SOURCE
    assert 'self._set_status(self._capture_pending_text(), status_idle_qss())' in MAIN_SOURCE
    assert MainWindow._capture_pending_text(SimpleNamespace()) == 'APPLYING SETTINGS'
    restarting = SimpleNamespace(_engine_restart_requested=True)
    assert MainWindow._capture_pending_text(restarting) == 'RESTARTING ENGINE'
    assert 'apply_state_lbl' not in MAIN_SOURCE


def test_native_engine_has_a_graceful_shutdown_command_without_layout_change():
    assert 'SHUTDOWN = 11' in ENGINE_HEADER
    assert 'SHUTDOWN = 11' in BRIDGE_SOURCE
    assert 'def request_engine_shutdown(self)' in BRIDGE_SOURCE
    assert 'case fthr::CommandType::SHUTDOWN:' in ENGINE_SOURCE
    assert 'while (!shutdown_requested)' in ENGINE_SOURCE
    assert 'engine.Shutdown();' in ENGINE_SOURCE


def test_source_engine_discovery_is_shared_and_frozen_uses_its_bundle():
    assert 'from core.engine_discovery import find_source_engine' in MAIN_SOURCE
    assert 'self.engine_path = find_source_engine(Path(__file__).parent.parent)' in MAIN_SOURCE
    assert "bundled_engine = Path(sys._MEIPASS) / 'engine' / 'FTHRClips.exe'" in MAIN_SOURCE
    assert 'self.engine_path = bundled_engine if bundled_engine.is_file() else None' in MAIN_SOURCE
    assert 'print(f"Engine found: {self.engine_path.resolve()}")' in MAIN_SOURCE
    assert 'FTHR_UI\\core\\engine_discovery.py' in LAUNCHER
