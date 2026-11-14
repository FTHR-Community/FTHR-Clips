"""Lifecycle host for locally installed, trusted Python feature plugins."""
from dataclasses import dataclass
import hashlib
import importlib.util
import io
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import uuid
import zipfile

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QPushButton

from core.plugin_manager import PluginManager
from core.capture_compatibility import (
    register_border_free_capture, register_wgc_capture,
    unregister_border_free_capture, unregister_wgc_capture,
)
from core.audio_effects import (register_microphone_effect,
                               unregister_microphone_effect,
                               unregister_microphone_effects)
from ui.dialogs import FthrDialog
from ui.style import button_outline_qss, set_theme_style

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PluginContext:
    """API 1 services. Plugins run with the same permissions as the app."""
    window: object
    settings: object
    _effect_owner: object = None

    def register_microphone_effect(self, effect_id, filter_factory):
        """Register trusted DSP; changes apply to clips opened afterwards."""
        register_microphone_effect(effect_id, filter_factory, owner=self._effect_owner)

    def unregister_microphone_effect(self, effect_id):
        unregister_microphone_effect(effect_id, owner=self._effect_owner)

    def register_border_free_capture(self):
        """Opt into full selected-display DXGI capture, with the cursor."""
        register_border_free_capture(self.window, owner=self._effect_owner)

    def unregister_border_free_capture(self):
        """Release only this plugin's capture compatibility registration."""
        unregister_border_free_capture(self.window, owner=self._effect_owner)

    def register_wgc_capture(self):
        """Use Windows Graphics Capture for new requests, preserving their source and settings."""
        register_wgc_capture(self.window, owner=self._effect_owner)

    def unregister_wgc_capture(self):
        """Release only this plugin's Windows Graphics Capture registration."""
        unregister_wgc_capture(self.window, owner=self._effect_owner)

    @property
    def ready(self):
        return getattr(self.window, '_file_operations_ready', True)

    def microphone_device(self):
        return self.window._resolved_mic_device_index()

    def refresh_microphone_preview(self):
        """Rebind the visible preview after a feature changes its shared stream."""
        page = self.window.__dict__.get('_settings_page_widget')
        if page is not None:
            QTimer.singleShot(0, page, page._start_audio_preview_if_current)


class PluginRuntime:
    def __init__(self, window, manager=None):
        self.context = PluginContext(window, window.settings_manager)
        self.manager = manager if manager is not None else PluginManager()
        self.active = {}
        self.errors = {}
        self.dialogs = {}
        self.surfaces = None
        self.attach_surfaces()

    def attach_surfaces(self):
        """Attach deferred settings UI without starting feature plugins twice."""
        page = self.context.window.__dict__.get('_settings_page_widget')
        surfaces = page.get_plugin_surfaces() if page is not None else None
        if surfaces is None or surfaces is self.surfaces:
            return
        self.surfaces = surfaces
        for plugin_id, entry in list(self.active.items()):
            try:
                self.surfaces.register(plugin_id, entry[1])
            except Exception as exc:
                log.exception('Feature plugin %s failed to attach settings', plugin_id)
                self.errors[plugin_id] = str(exc)
                self._stop(plugin_id)

    def refresh(self):
        packages = {m['id']: (m, data) for m, data in self.manager.feature_packages()}
        for plugin_id, entry in list(self.active.items()):
            package = packages.get(plugin_id)
            if package is None or hashlib.sha256(package[1]).hexdigest() != entry[0]:
                self._stop(plugin_id)
        self.errors.clear()
        for plugin_id, (_manifest, data) in packages.items():
            if plugin_id in self.active:
                continue
            directory = TemporaryDirectory(prefix='fthr-feature-')
            namespace = '_fthr_plugin_' + uuid.uuid4().hex
            instance = None
            context = PluginContext(self.context.window, self.context.settings, namespace)
            try:
                # The manager has validated paths, entry types and size limits.
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    archive.extractall(directory.name)
                root = Path(directory.name)
                spec = importlib.util.spec_from_file_location(
                    namespace, root / '__init__.py', submodule_search_locations=[str(root)])
                module = importlib.util.module_from_spec(spec)
                sys.modules[namespace] = module
                spec.loader.exec_module(module)
                instance = module.Plugin(context)
                instance.start()
                if self.surfaces is not None:
                    self.surfaces.register(plugin_id, instance)
                self.active[plugin_id] = (hashlib.sha256(data).hexdigest(), instance, directory, namespace)
                if callable(getattr(instance, 'interface_host_ready', None)):
                    QTimer.singleShot(0, self.context.window,
                                      lambda pid=plugin_id: self._interface_host_ready(pid))
            except Exception as exc:
                log.exception('Feature plugin %s failed to start', plugin_id)
                self.errors[plugin_id] = str(exc)
                if self.surfaces is not None:
                    self.surfaces.unregister(plugin_id)
                if instance is not None:
                    try:
                        instance.stop()
                    except Exception:
                        log.exception('Feature plugin cleanup failed')
                unregister_microphone_effects(namespace)
                unregister_border_free_capture(self.context.window, owner=namespace)
                unregister_wgc_capture(self.context.window, owner=namespace)
                self._release(directory, namespace)

    @staticmethod
    def _release(directory, namespace):
        for name in list(sys.modules):
            if name == namespace or name.startswith(namespace + '.'):
                sys.modules.pop(name, None)
        directory.cleanup()

    def _interface_host_ready(self, plugin_id):
        """Notify optional interface plugins after the host finishes styling."""
        entry = self.active.get(plugin_id)
        if entry is None:
            return
        try:
            entry[1].interface_host_ready()
        except Exception as exc:
            log.exception('Feature plugin %s failed to initialize its interface', plugin_id)
            self.errors[plugin_id] = str(exc)
            self._stop(plugin_id)

    def _stop(self, plugin_id):
        if self.surfaces is not None:
            self.surfaces.unregister(plugin_id)
        dialog = self.dialogs.pop(plugin_id, None)
        if dialog is not None:
            dialog.close()
            dialog.deleteLater()
        _, instance, directory, namespace = self.active.pop(plugin_id)
        try:
            instance.stop()
        except Exception:
            log.exception('Feature plugin %s failed to stop', plugin_id)
        finally:
            unregister_microphone_effects(namespace)
            unregister_border_free_capture(self.context.window, owner=namespace)
            unregister_wgc_capture(self.context.window, owner=namespace)
            self._release(directory, namespace)

    def configure(self, plugin_id):
        if plugin_id not in self.active:
            return
        open_window = getattr(self.active[plugin_id][1], 'open_window', None)
        if callable(open_window):
            open_window()
            return
        if self.surfaces is not None and self.surfaces.show(plugin_id):
            return
        dialog = self.dialogs.get(plugin_id)
        if dialog is None:
            instance = self.active[plugin_id][1]
            name = next(m['name'] for m in self.manager.list_plugins() if m['id'] == plugin_id)
            dialog = FthrDialog(name, self.context.window, width=520)
            dialog.setModal(False)
            dialog.body_layout.addWidget(instance.settings_widget())
            close = QPushButton('Close')
            close.setCursor(Qt.CursorShape.PointingHandCursor)
            set_theme_style(close, lambda: button_outline_qss())
            close.clicked.connect(dialog.close)
            dialog.action_layout.addStretch()
            dialog.action_layout.addWidget(close)
            self.dialogs[plugin_id] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def close(self):
        for plugin_id in list(self.active):
            self._stop(plugin_id)
