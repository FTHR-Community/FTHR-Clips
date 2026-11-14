"""Optional standalone plugin windows preserve the normal plugin lifecycle."""
from types import SimpleNamespace
from unittest.mock import Mock

from core.plugin_runtime import PluginRuntime


def runtime_with(instance):
    runtime = object.__new__(PluginRuntime)
    runtime.active = {'studio': ('digest', instance, None, 'namespace')}
    runtime.errors = {}
    runtime.dialogs = {}
    runtime.surfaces = None
    return runtime


def test_configure_uses_standalone_window_without_wrapper():
    instance = SimpleNamespace(open_window=Mock())
    runtime = runtime_with(instance)
    runtime.configure('studio')
    runtime.configure('studio')
    assert instance.open_window.call_count == 2
    assert runtime.dialogs == {}


def test_deferred_host_ready_ignores_removed_plugin():
    instance = SimpleNamespace(interface_host_ready=Mock())
    runtime = runtime_with(instance)
    runtime.active.clear()
    runtime._interface_host_ready('studio')
    instance.interface_host_ready.assert_not_called()


def test_host_ready_failure_stops_plugin_and_records_error():
    instance = SimpleNamespace(interface_host_ready=Mock(side_effect=ValueError('bad design')))
    runtime = runtime_with(instance)
    runtime._stop = Mock()
    runtime._interface_host_ready('studio')
    runtime._stop.assert_called_once_with('studio')
    assert runtime.errors['studio'] == 'bad design'
