"""Shared pytest setup for the uninstalled FTHR_UI source tree.

Add the UI directory to sys.path centrally so test imports do not depend
on collection order.
"""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Standalone plugin sources and sample packages are maintained separately from
# this app checkout. Keep their integration tests available for combined trees,
# while continuing to run the plugin host's self-contained contract tests here.
if not (ROOT / 'plugins').is_dir():
    collect_ignore = [
        'test_border_free_capture_plugin.py',
        'test_capture_startup_doctor_plugin.py',
        'test_gary_overlay.py',
        'test_interface_assets.py',
        'test_interface_content_layers.py',
        'test_interface_content_model.py',
        'test_interface_content_ui.py',
        'test_interface_fills_ui.py',
        'test_interface_host_integration.py',
        'test_interface_interactions.py',
        'test_interface_media.py',
        'test_interface_rendering.py',
        'test_interface_studio.py',
        'test_interface_theme_library.py',
        'test_rnnoise_plugin.py',
        'test_stream_deck_bridge.py',
        'test_stream_deck_integration.py',
    ]


def pytest_collection_modifyitems(items):
    sources_available = (ROOT / 'plugins').is_dir()
    examples_available = (ROOT / 'examples' / 'plugins').is_dir()
    package_tests = {
        ('test_gary_plugin.py', 'test_audio_off_hides_overlay_and_releases_microphone'),
        ('test_gary_plugin.py', 'test_delivered_package_install_configure_disable_reenable_remove'),
        ('test_gary_plugin.py', 'test_failed_update_unloads_old_plugin_and_is_reported'),
        ('test_gary_plugin.py', 'test_gary_mounts_in_audio_and_restores_normal_meter_on_unload'),
        ('test_gary_plugin.py', 'test_plugin_does_not_stop_a_stream_owned_by_the_app'),
        ('test_plugin_manager.py', 'test_example_packs_install'),
        ('test_plugin_model_assets.py',
         'test_delivered_rnnoise_installs_and_uninstalls_in_isolated_host'),
        ('test_plugin_settings.py', 'test_drop_test_zip_previews_before_installing'),
        ('test_noop_plugin.py', 'test_sample_noop_lifecycle'),
    }
    source_tests = {
        ('test_audio_effect_plugins.py',
         'test_native_rnnoise_preview_matches_render_and_preserves_dry_file'),
        ('test_gary_plugin.py', 'test_reject_invalid_features_without_execution'),
    }
    for item in items:
        filename = Path(str(item.path)).name
        name = getattr(item, 'originalname', item.name)
        if not examples_available and (filename, name) in package_tests:
            item.add_marker(pytest.mark.skip(
                reason='Standalone plugin packages are not in this checkout'))
        elif not sources_available and (filename, name) in source_tests:
            item.add_marker(pytest.mark.skip(
                reason='Standalone plugin source is not in this checkout'))

# Widget tests construct real QWidgets. On a headless CI runner Qt aborts
# without a platform plugin; offscreen is the supported answer and is harmless
# on a desktop. setdefault so a developer can still force xcb/windows to watch
# a test render.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

for _p in (ROOT / 'FTHR_UI', ROOT / 'tools'):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)


@pytest.fixture(autouse=True)
def _isolate_windows_videos_storage(monkeypatch, tmp_path):
    """Keep Windows known-folder tests away from a developer's real Videos."""
    from core import settings_manager

    monkeypatch.setattr(
        settings_manager, '_windows_videos_directory',
        lambda: tmp_path / 'Videos', raising=False)


@pytest.fixture(autouse=True)
def _reset_ui_runtime_state():
    """Do not leak application QSS state between tests."""
    yield
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    app.setStyleSheet('')
