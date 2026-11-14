"""Install the delivered package and exercise its actual Qt/runtime boundary."""
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest
from PySide6.QtWidgets import QWidget

from core.plugin_manager import PluginError, PluginManager
from core.plugin_runtime import PluginRuntime
from ui.plugin_settings import PluginSettingsWidget

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'examples/plugins/fthr-gary-mode.zip'
PLUGIN_ID = 'fthr.gary-mode'


class Settings(dict):
    def save_settings(self):
        pass


class Recorder:
    running = False
    stops = 0
    starts = 0
    level = 0.7

    def is_available(self):
        return True

    def is_running(self):
        return self.running

    def start(self, device, gain):
        self.starts += 1
        self.running = True
        return True

    def stop(self):
        self.stops += 1
        self.running = False

    def latest_level(self):
        return self.level


@pytest.fixture
def host(qtbot, monkeypatch, tmp_path):
    # Import app services before replacing the recorder used by package modules.
    import main  # noqa: F401
    from core import mic_recorder
    recorder = Recorder()
    monkeypatch.setattr(mic_recorder, 'MicRecorder', lambda: recorder)
    window = QWidget()
    qtbot.addWidget(window)
    window.settings_manager = Settings(gary_min_level=60, gary_max_level=90)
    window._resolved_mic_device_index = lambda: None
    runtime = PluginRuntime(window, PluginManager(tmp_path / 'installed'))
    yield runtime, recorder
    runtime.close()


def test_delivered_package_install_configure_disable_reenable_remove(host, qtbot):
    runtime, recorder = host
    runtime.refresh()
    assert not runtime.active and recorder.starts == 0
    widget = PluginSettingsWidget(manager=runtime.manager, runtime=runtime)
    qtbot.addWidget(widget)
    widget._review_archive(str(PACKAGE))
    assert 'Runs Python code' in widget._preview_details.text()
    assert recorder.starts == 0  # inspection never executes a plugin
    widget._install()
    assert not runtime.errors
    instance = runtime.active[PLUGIN_ID][1]
    namespace = runtime.active[PLUGIN_ID][3]
    directory = Path(runtime.active[PLUGIN_ID][2].name)
    assert recorder.starts == 1 and instance.current > 0
    assert not instance.overlay._base_pixmap.isNull()
    runtime.configure(PLUGIN_ID)
    panel = instance.panel
    assert panel._gary_min_level == 60 and panel._gary_max_level == 90
    panel._on_gary_thresholds_dragged(50, 80)
    assert runtime.context.settings['gary_min_level'] == 50
    panel.gary_enabled_check.setChecked(False)
    assert not recorder.running and instance.overlay.intensity == 0
    panel.gary_enabled_check.setChecked(True)
    instance.retry_ticks = 0
    instance.tick()
    assert recorder.running
    widget._toggle(runtime.manager.list_plugins()[0])
    assert not runtime.active and not recorder.running
    assert namespace not in sys.modules and not directory.exists()
    widget._toggle(runtime.manager.list_plugins()[0])
    assert PLUGIN_ID in runtime.active
    assert runtime.context.settings['gary_min_level'] == 50
    runtime.manager.uninstall(PLUGIN_ID)
    runtime.refresh()
    assert not runtime.active and not recorder.running and not runtime.dialogs


def test_plugin_does_not_stop_a_stream_owned_by_the_app(host):
    runtime, recorder = host
    recorder.running = True
    runtime.manager.install(PACKAGE)
    runtime.refresh()
    assert recorder.starts == 0
    runtime.close()
    assert recorder.running and recorder.stops == 0


def test_audio_off_hides_overlay_and_releases_microphone(host):
    runtime, recorder = host
    runtime.manager.install(PACKAGE)
    runtime.refresh()
    plugin = runtime.active[PLUGIN_ID][1]
    runtime.context.settings['audio_capture_enabled'] = False
    plugin.tick()
    assert plugin.overlay.intensity == 0 and not recorder.running
    runtime.close()
    assert recorder.stops == 1


def feature_archive(tmp_path, files):
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    path = tmp_path / 'feature.zip'
    manifest = json.loads((ROOT / 'plugins/gary-mode-manifest.json').read_text())
    manifest.pop('cover', None)
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest))
        archive.writestr('feature.zip', inner.getvalue())
    return path


@pytest.mark.parametrize('files', [
    {'../escape.py': ''}, {'__init__.py': '', 'x.exe': b'executable'},
    {'__init__.py': 'syntax error!'}, {'__init__.py': '', 'image.png': b'bad'},
])
def test_reject_invalid_features_without_execution(tmp_path, files):
    with pytest.raises(PluginError):
        PluginManager(tmp_path / 'installed').install(feature_archive(tmp_path, files))


def test_failed_update_unloads_old_plugin_and_is_reported(host, tmp_path):
    runtime, recorder = host
    runtime.manager.install(PACKAGE)
    runtime.refresh()
    namespace = runtime.active[PLUGIN_ID][3]
    runtime.manager.install(feature_archive(tmp_path, {'__init__.py': 'raise RuntimeError("broken plugin")'}))
    runtime.refresh()
    assert not runtime.active and not recorder.running
    assert namespace not in sys.modules
    assert runtime.errors[PLUGIN_ID] == 'broken plugin'


def test_app_does_not_ship_gary_code_or_assets():
    assert 'gary' not in (ROOT / 'FTHR_UI/main.py').read_text(encoding='utf-8').lower()
    assert not (ROOT / 'FTHR_UI/assets/gary.png').exists()
    for filename in ('FTHR.spec',):
        source = (ROOT / filename).read_text(encoding='utf-8')
        assert 'gary.png' not in source and 'gatze.png' not in source


@pytest.mark.parametrize('audio_loaded', [False, True])
def test_gary_mounts_in_audio_and_restores_normal_meter_on_unload(
        host, qtbot, monkeypatch, tmp_path, audio_loaded):
    from core.settings_manager import SettingsManager
    from main import _SettingsPage
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid

    runtime, _ = host
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _: None)
    monkeypatch.setattr(_SettingsPage, '_start_audio_preview_if_current', lambda _: None)
    page = _SettingsPage(SettingsManager())
    qtbot.addWidget(page)
    runtime.context.window._settings_page_widget = page
    runtime.surfaces = page.get_plugin_surfaces()
    if audio_loaded:
        page._ensure_category(2)
    runtime.manager.install(PACKAGE)
    runtime.refresh()
    plugin = runtime.active[PLUGIN_ID][1]
    if not audio_loaded:
        assert plugin.panel is None  # Installation must not force lazy Audio UI open.
    runtime.configure(PLUGIN_ID)
    assert runtime.dialogs == {}
    panel = plugin.panel
    meter = panel.mic_level_meter
    assert panel.parentWidget().objectName() == 'audioPluginSettings'
    assert panel.gary_enabled_check.text() == 'Enable Gary Mode'
    assert page.mic_level_meter.isHidden()
    assert meter.parentWidget() != panel
    page.mic_level_meter._level_changed.emit(.75)
    assert meter._level == pytest.approx(.69)
    runtime.refresh()
    assert runtime.surfaces.container_layout.count() == 1
    runtime.manager.set_enabled(PLUGIN_ID, False)
    runtime.refresh()
    assert not page.mic_level_meter.isHidden()
    assert runtime.surfaces.container.isHidden()
    assert not runtime.surfaces.providers
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(panel) and not isValid(meter)
    runtime.manager.set_enabled(PLUGIN_ID, True)
    runtime.refresh()
    assert runtime.surfaces.container_layout.count() == 1
    runtime.manager.uninstall(PLUGIN_ID)
    runtime.refresh()
    assert not page.mic_level_meter.isHidden()
