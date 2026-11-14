"""Optional audio models remain bounded, inert feature-package assets."""
import io
import zipfile

import pytest

from core.plugin_manager import PluginError, _validate_feature


def feature(name, content):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('__init__.py', 'class Plugin: pass\n')
        archive.writestr(name, content)
    return output.getvalue()


@pytest.mark.parametrize('name,content', [
    ('models/std.rnnn', b'RNNoise model data\n'),
    ('LICENSE.txt', b'Copyright and redistribution terms\n'),
], ids=['model', 'license'])
def test_model_and_attribution_assets_allowed(name, content):
    _validate_feature(feature(name, content))


@pytest.mark.parametrize('name,content', [
    ('models/std.rnnn', b'\xff\xfe'),
    ('LICENSE.txt', b'text\x00binary'),
    ('models/std.rnnn', b'0' * (4 * 1024 * 1024 + 1)),
    ('LICENSE.txt', b'x' * (256 * 1024 + 1)),
    ('models/engine.dll', b'MZ'),
    ('../std.rnnn', b'RNNoise'),
], ids=['invalid-utf8', 'nul', 'large-model', 'large-license', 'dll', 'traversal'])
def test_bad_or_unbounded_feature_assets_rejected(name, content):
    with pytest.raises(PluginError):
        _validate_feature(feature(name, content))


def test_delivered_rnnoise_installs_and_uninstalls_in_isolated_host(tmp_path, qtbot, monkeypatch):
    from pathlib import Path
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtWidgets import QWidget
    from core.plugin_manager import PluginManager
    from core.plugin_runtime import PluginRuntime
    from core.playback_mix_model import PlaybackSource

    monkeypatch.setattr(QStandardPaths, 'writableLocation', lambda _: str(tmp_path / 'cache'))
    package = Path(__file__).resolve().parents[1] / 'examples/plugins/fthr-rnnoise.zip'
    manager = PluginManager(tmp_path / 'installed')
    manager.install(package)
    window = QWidget()
    qtbot.addWidget(window)
    window.settings_manager = {'rnnoise_enabled': True}
    runtime = PluginRuntime(window, manager)
    try:
        runtime.refresh()
        assert not runtime.errors
        assert 'fthr.rnnoise' in runtime.active
        source = PlaybackSource('mic', 'Microphone', 'microphone', 0, 0)
        assert 'arnndn=' in source.processing_filter
        model = runtime.active['fthr.rnnoise'][1].model
        manager.uninstall('fthr.rnnoise')
        runtime.refresh()
        assert not runtime.active
        assert model.is_file()
        assert 'arnndn=' in source.processing_filter
        assert PlaybackSource('mic', 'Microphone', 'microphone', 0, 0).processing_filter == 'anull'
    finally:
        runtime.close()
