"""Lazy, removable extension points within the application's settings pages."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QVBoxLayout, QWidget


class PluginSettingsSurfaces:
    def __init__(self, settings_page):
        self.page = settings_page
        self.providers = {}
        self.widgets = {}
        self.container = None
        self.meter_row = None
        self.normal_meter = None

    def register(self, plugin_id, instance):
        if getattr(instance, 'settings_location', None) != 'audio.microphone':
            return False
        self.providers[plugin_id] = instance
        if self.container is not None:
            self._mount(plugin_id)
        return True

    def attach_audio(self, layout, meter_row, normal_meter):
        self.container = QWidget()
        self.container.setObjectName('audioPluginSettings')
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(0, 18, 0, 0)
        self.container_layout.setSpacing(18)
        self.meter_row = meter_row
        self.normal_meter = normal_meter
        layout.addWidget(self.container)
        for plugin_id in self.providers:
            self._mount(plugin_id)
        self._sync_visibility()

    def _mount(self, plugin_id):
        if plugin_id in self.widgets:
            return
        instance = self.providers[plugin_id]
        panel = instance.settings_widget()
        meter_factory = getattr(instance, 'settings_meter', None)
        meter = meter_factory() if callable(meter_factory) else None
        self.container_layout.addWidget(panel)
        panel.show()
        if meter is not None:
            self.meter_row.addWidget(meter, 1)
            self.normal_meter._level_changed.connect(meter.set_level)
            meter.show()
        self.widgets[plugin_id] = (panel, meter)
        self._sync_visibility()

    def _sync_visibility(self):
        if self.container is not None:
            self.container.setVisible(bool(self.widgets))
            self.normal_meter.setVisible(not any(meter for _, meter in self.widgets.values()))

    def unregister(self, plugin_id):
        self.providers.pop(plugin_id, None)
        mounted = self.widgets.pop(plugin_id, None)
        if mounted is not None:
            panel, meter = mounted
            panel.hide()
            self.container_layout.removeWidget(panel)
            if meter is not None:
                self.normal_meter._level_changed.disconnect(meter.set_level)
                meter.hide()
                self.meter_row.removeWidget(meter)
                meter.setParent(panel)  # Return ownership before plugin.stop().
            panel.setParent(None)
        self._sync_visibility()

    def show(self, plugin_id):
        if plugin_id not in self.providers:
            return False
        self.page._ensure_category(2)
        self.page.navigate_category_to(2)
        panel, _ = self.widgets[plugin_id]
        panel.setFocus(Qt.FocusReason.OtherFocusReason)
        return True
