from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / 'FTHR_UI' / 'main.py').read_text(encoding='utf-8')
TARGET_SOURCE = (ROOT / 'FTHR_UI' / 'core' / 'screenshot_target.py').read_text(
    encoding='utf-8')


def test_selected_monitor_is_resolved_fresh_without_cross_monitor_fallback():
    assert 'if not selected_monitor:' in TARGET_SOURCE
    assert "return None" in TARGET_SOURCE
    assert 'expected_geometry' in TARGET_SOURCE
    assert "getattr(matched_monitor, 'primary', False)" in TARGET_SOURCE
    assert 'enumerate_windows_monitors()' in MAIN_SOURCE
    assert 'primary=QApplication.primaryScreen()' in MAIN_SOURCE


def test_screenshot_capture_is_side_band_and_transactional():
    assert 'ScreenshotPngSaveWorker' in MAIN_SOURCE
    assert 'reserve_screenshot_paths' in MAIN_SOURCE
    assert 'pixmap.save(str(raw_path))' not in MAIN_SOURCE
    assert 'self.capture_card.show_screenshot()' in MAIN_SOURCE
    assert 'self._screenshot_inflight' in MAIN_SOURCE
    assert 'self._screenshot_pending_requests' in MAIN_SOURCE
