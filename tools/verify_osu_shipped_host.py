"""Exercise the installable plugin against packaged 2.1.0 main bytecode.

Runs in a separate process with isolated settings. Native recording is stubbed;
this verifies host compatibility, not physical fullscreen capture.
"""
import argparse
import copy
import hashlib
import marshal
import os
from pathlib import Path
import sys
import tempfile
import types


def semantic_code(code):
    return (code.co_code, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_nlocals, code.co_stacksize,
            code.co_flags, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_exceptiontable,
            tuple(semantic_code(v) if isinstance(v, types.CodeType) else v
                  for v in code.co_consts))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--release-source', type=Path, required=True)
    parser.add_argument('--plugin', type=Path, required=True)
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PyInstaller.archive.readers import CArchiveReader
    archive = CArchiveReader(str(args.bundle))
    pyz = archive.open_embedded_archive(next(n for n in archive.toc if n.endswith('.pyz')))
    main_code = marshal.loads(archive.extract('main'))
    release_ui = args.release_source.resolve() / 'FTHR_UI'
    # Do not accidentally validate against the edited development host.
    for name in ('core.plugin_runtime', 'core.capture_compatibility',
                 'core.capture_settings', 'core.capture_session', 'core.plugin_manager'):
        source = release_ui / Path(*name.split('.')).with_suffix('.py')
        assert semantic_code(pyz.extract(name)) == semantic_code(
            compile(source.read_bytes(), str(source), 'exec')), name
    assert semantic_code(main_code) == semantic_code(compile(
        (release_ui / 'main.py').read_bytes(), 'main.py', 'exec'))
    sys.path.insert(0, str(release_ui))
    host_module = types.ModuleType('shipped_main')
    host_module.__file__ = str(release_ui / 'main.py')
    sys.modules[host_module.__name__] = host_module
    exec(main_code, host_module.__dict__)
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QWidget
    from core import settings_manager
    from core.capture_session import SourceSnapshot
    from core.plugin_manager import PluginManager
    from core.plugin_runtime import PluginContext, PluginRuntime
    assert not hasattr(PluginContext, 'register_wgc_capture')
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix='fthr-osu-shipped-test-') as directory:
        temp = Path(directory)
        old_home = Path.__dict__['home']
        Path.home = classmethod(lambda cls: temp)
        settings_manager._windows_videos_directory = lambda: temp / 'Videos'
        runtime = None
        try:
            window = QWidget()
            window.settings_manager = settings_manager.SettingsManager()
            window.settings_manager.set('capture_monitor', 'DISPLAY1')
            window.settings_manager.set('capture_cursor', False)
            saved = copy.deepcopy(window.settings_manager.settings)
            window._capture_request_token = 0
            window.engine_process = object()
            window._capture_settings_timer = QTimer(window)
            calls = []
            window._restart_capture_engine = lambda: calls.append('restart')
            for name in ('_capture_global_defaults_snapshot', '_capture_source_snapshot',
                         '_begin_capture_intent', '_resolve_capture_request', 'start_engine',
                         '_capture_resources_busy', '_on_capture_settings_restart_requested',
                         '_queue_capture_settings_restart'):
                setattr(window, name, getattr(host_module.MainWindow, name).__get__(window))
            host_module.enumerate_windows_monitors = lambda: []
            host_module.resolve_monitor_selection = lambda *a: types.SimpleNamespace(
                path='DISPLAY1', persist_required=False)
            original_builder = host_module.build_capture_config
            original_guard = host_module.capture_policy_is_current
            manager = PluginManager(temp / 'installed')
            manifest = manager.install(args.plugin.resolve())
            assert manifest['version'] == '1.1.0'
            runtime = PluginRuntime(window, manager)
            runtime.refresh()
            assert not runtime.errors, runtime.errors
            plugin = runtime.active['fthr.osu-capture'][1]
            sys.modules[type(plugin).__module__].osu_running = lambda: True
            window._manual_record_state = 'recording'
            plugin._poll()
            assert not plugin.registered and not calls
            window._manual_record_state = 'idle'
            plugin._poll()
            assert plugin.registered and calls == ['restart']
            for mode in ('desktop', 'window'):
                identity = {'hwnd': 4242, 'pid': 32, 'exe_path': r'C:\Games\osu!.exe',
                            'class_name': 'Game', 'process_created': 11}
                source = (SourceSnapshot('desktop') if mode == 'desktop' else
                          SourceSnapshot('window', r'C:\Games\osu!.exe', identity))
                request = window._resolve_capture_request(source_snapshot=source)
                assert request.capture_config.capture_backend == 'wgc'
                assert not request.capture_config.capture_cursor
                assert request.source == source
                assert window._desired_capture_request is request
                assert host_module.capture_policy_is_current(window, request)
                assert window._resolve_capture_request(prepared_request=request) is request
            assert window.settings_manager.settings == saved
            sys.modules[type(plugin).__module__].osu_running = lambda: False
            for _ in range(3):
                plugin._poll()
            assert not plugin.registered
            request = window._resolve_capture_request(source_snapshot=SourceSnapshot('desktop'))
            assert request.capture_config.capture_backend == 'auto'
            runtime.close()
            runtime = None
            assert host_module.build_capture_config is original_builder
            assert host_module.capture_policy_is_current is original_guard
            assert window.settings_manager.settings == saved
            app.processEvents()
        finally:
            if runtime is not None:
                runtime.close()
            Path.home = old_home
    print('PASS: shipped 2.1.0 bytecode, old plugin API, ZIP install/load, busy deferral, '
          'desktop/window WGC requests, prepared requests, restoration and unload')
    print('Host SHA256:', hashlib.sha256(args.bundle.read_bytes()).hexdigest())
    print('Plugin SHA256:', hashlib.sha256(args.plugin.read_bytes()).hexdigest())


if __name__ == '__main__':
    main()
