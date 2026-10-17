from __future__ import annotations

from types import SimpleNamespace
import sys

import pytest

from core.encoder_capabilities import (
    EncoderCapability,
    available_codecs,
    load_cached_encoder_capabilities,
    probe_encoder_capabilities,
    save_encoder_capabilities_cache,
)


def _runner_for(working: set[str]):
    def _run(command, **_kwargs):
        encoder = command[command.index('-c:v') + 1]
        return SimpleNamespace(returncode=0 if encoder in working else 1)
    return _run


def test_windows_returns_only_usable_hardware_backends():
    capabilities = probe_encoder_capabilities(
        'ffmpeg',
        platform='win32',
        runner=_runner_for({'h264_nvenc', 'hevc_nvenc', 'h264_qsv'}),
    )

    assert [(item.key, item.codecs) for item in capabilities] == [
        ('nvenc', ('h264', 'hevc')),
        ('qsv', ('h264',)),
    ]
    assert capabilities[0].supports_presets
    assert available_codecs(capabilities, 'nvenc') == ('h264', 'hevc')
    assert available_codecs(capabilities, 'auto') == ('h264', 'hevc')


def test_linux_exposes_reviewed_software_fallbacks():
    capabilities = probe_encoder_capabilities(
        'ffmpeg',
        platform='linux',
        runner=_runner_for({'libopenh264', 'libkvazaar', 'libsvtav1'}),
    )

    assert [(item.key, item.codecs) for item in capabilities] == [
        ('software', ('h264', 'hevc', 'av1')),
    ]


def test_device_capabilities_are_reused_until_ffmpeg_changes(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'first build')
    cache = tmp_path / 'encoder-capabilities.json'
    capabilities = (
        EncoderCapability(
            'nvenc', 'NVIDIA NVENC', ('h264',),
            (('h264', 'h264_nvenc'),), True),
    )
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature',
        lambda: 'test-adapter|driver:stable')

    assert save_encoder_capabilities_cache(
        capabilities, str(ffmpeg), platform='win32', cache_file=cache)
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache) == capabilities

    ffmpeg.write_bytes(b'a different FFmpeg build')
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache) is None


def test_windows_cache_expires_when_adapter_or_driver_signature_changes(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'one build')
    cache = tmp_path / 'encoder-capabilities.json'
    capabilities = (EncoderCapability(
        'nvenc', 'NVIDIA NVENC', ('h264',),
        (('h264', 'h264_nvenc'),), True),)
    signature = {'value': 'adapter:pci-1|driver:1.0'}
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature',
        lambda: signature['value'])

    assert save_encoder_capabilities_cache(
        capabilities, str(ffmpeg), platform='win32', cache_file=cache)
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache) == capabilities

    signature['value'] = 'adapter:pci-1|driver:2.0'
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache) is None


def test_windows_cache_is_not_reused_without_adapter_identity(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'one build')
    cache = tmp_path / 'encoder-capabilities.json'
    capabilities = (EncoderCapability(
        'qsv', 'Intel Quick Sync', ('h264',),
        (('h264', 'h264_qsv'),), False),)
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature',
        lambda: 'adapter:pci-1|driver:1.0')
    assert save_encoder_capabilities_cache(
        capabilities, str(ffmpeg), platform='win32', cache_file=cache)

    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature', lambda: None)
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache) is None


def test_linux_cache_does_not_require_windows_adapter_identity(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg'
    ffmpeg.write_bytes(b'linux build')
    cache = tmp_path / 'encoder-capabilities.json'
    capabilities = (EncoderCapability(
        'software', 'Software', ('h264',),
        (('h264', 'libopenh264'),), False),)
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature', lambda: None)

    assert save_encoder_capabilities_cache(
        capabilities, str(ffmpeg), platform='linux', cache_file=cache)
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='linux', cache_file=cache) == capabilities


def test_windows_cache_save_is_bypassed_without_adapter_identity(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'one build')
    cache = tmp_path / 'encoder-capabilities.json'
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature', lambda: None)

    assert not save_encoder_capabilities_cache(
        (), str(ffmpeg), platform='win32', cache_file=cache)
    assert not cache.exists()


def test_settings_bind_device_encoder_codec_preset_and_audio(
        tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    sm.set('encoder_pref', 'nvenc')
    sm.set('codec_pref', 'hevc')
    sm.set('encoder_preset', 6)
    sm.save_settings()

    page = _SettingsPage(sm)
    page._on_encoder_capabilities_ready((
        EncoderCapability(
            'nvenc', 'NVIDIA NVENC', ('h264', 'hevc'),
            (('h264', 'h264_nvenc'), ('hevc', 'hevc_nvenc')), True),
        EncoderCapability(
            'qsv', 'Intel Quick Sync', ('h264',),
            (('h264', 'h264_qsv'),), False),
        EncoderCapability('software', 'Software', ('h264',),
                          (('h264', 'libopenh264'),), False),
    ))

    assert 'Version & Updates' not in page._cat_titles
    assert page.encoder_combo.currentData() == 'nvenc'
    assert page.codec_combo.currentData() == 'hevc'
    assert page.encoder_preset_combo.currentData() == 6
    assert not page.encoder_preset_row.isHidden()
    assert page.performance_audio_check.text() == 'Enable audio capture · Medium impact'
    assert page.audio_capture_check.text() == 'Enable audio capture'
    if sys.platform == 'win32':
        assert page.capture_card_mode_combo.currentData() == 'all'
        assert not hasattr(page, 'performance_capture_card_check')
    else:
        assert page.performance_capture_card_check.text() == 'Enable capture card'
        assert page.performance_capture_card_check.isChecked()
    assert page.performance_encoder_preset_combo.currentData() == 6
    assert not page.performance_encoder_preset_row.isHidden()

    page.encoder_combo.setCurrentIndex(page.encoder_combo.findData('qsv'))
    assert page.encoder_preset_row.isHidden()
    assert page.performance_encoder_preset_row.isHidden()
    assert page.performance_encoder_section.isHidden()
    # Windows keeps the prior explicit codec as unverified when the backend
    # changes; selecting Automatic or another supported codec is deliberate.
    expected_qsv_codecs = (['auto', 'h264', 'hevc'] if sys.platform == 'win32'
                           else ['auto', 'h264'])
    assert [page.codec_combo.itemData(i)
            for i in range(page.codec_combo.count())] == expected_qsv_codecs

    page.encoder_combo.setCurrentIndex(page.encoder_combo.findData('nvenc'))
    page.encoder_preset_combo.setCurrentIndex(4)
    assert page.performance_encoder_preset_combo.currentData() == 5
    page.performance_encoder_preset_combo.setCurrentIndex(2)
    assert page.encoder_preset_combo.currentData() == 3
    assert not page.performance_encoder_apply_btn.isHidden()
    page.performance_encoder_apply_btn.click()
    assert sm.get('encoder_preset') == 3

    page.performance_audio_check.setChecked(False)
    assert not page.audio_capture_check.isChecked()
    assert not hasattr(page, 'multiband_check')
    assert not sm.get('audio_capture_enabled')
    if sys.platform == 'win32':
        page.capture_card_mode_combo.setCurrentIndex(page.capture_card_mode_combo.findData('off'))
    else:
        page.performance_capture_card_check.setChecked(False)
    assert not sm.get('capture_card_enabled')
    page.encoder_combo.setCurrentIndex(page.encoder_combo.findData('software'))
    assert page.encoder_combo.currentData() == 'software'
    expected_software_codecs = (
        ['auto', 'h264', 'hevc'] if sys.platform == 'win32'
        else ['auto', 'h264'])
    assert [page.codec_combo.itemData(i)
            for i in range(page.codec_combo.count())] == expected_software_codecs
    assert page.encoder_preset_row.isHidden()
    page._on_encoder_apply()
    assert sm.get('encoder_pref') == 'software'
    page.deleteLater()
    app.processEvents()


def test_windows_software_is_explicit_and_h264_only():
    capabilities = probe_encoder_capabilities(
        'ffmpeg', platform='win32',
        runner=_runner_for({'libopenh264', 'libkvazaar', 'libsvtav1'}))
    assert [(item.key, item.codecs) for item in capabilities] == [('software', ('h264',))]
    assert available_codecs(capabilities, 'software') == ('h264',)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows encoder profile')
def test_stale_windows_backend_and_codec_are_preserved_until_explicit_change(
        tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    sm.set('encoder_pref', 'amf')
    sm.set('codec_pref', 'av1')
    sm.save_settings()

    page = _SettingsPage(sm)
    page._on_encoder_capabilities_ready((
        EncoderCapability('amf', 'AMD AMF', ('h264',),
                          (('h264', 'h264_amf'),), False),
    ))
    page.stack.setCurrentIndex(1)

    assert page.encoder_combo.currentData() == 'amf'
    assert page.codec_combo.currentData() == 'av1'
    assert 'not verified for selected monitor' in page.encoder_detection_lbl.text()

    # Applying an unrelated setting must not rewrite the stale profile to auto
    # or H.264 while initialization has not validated it.
    page.stack.setCurrentIndex(5)
    page.performance_audio_check.setChecked(False)
    page.encoder_preset_combo.setCurrentIndex(0)
    page._on_encoder_apply()
    assert sm.get('encoder_pref') == 'amf'
    assert sm.get('codec_pref') == 'av1'

    # A deliberate supported-codec choice is still an ordinary Apply.
    page.codec_combo.setCurrentIndex(page.codec_combo.findData('h264'))
    page._on_encoder_apply()
    assert sm.get('encoder_pref') == 'amf'
    assert sm.get('codec_pref') == 'h264'
    page.deleteLater()
    app.processEvents()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows encoder profile')
def test_stale_windows_backend_without_capability_remains_visible_and_unchanged(
        tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    sm.set('encoder_pref', 'amf')
    sm.set('codec_pref', 'h264')
    sm.save_settings()

    page = _SettingsPage(sm)
    page._on_encoder_capabilities_ready((
        EncoderCapability('qsv', 'Intel Quick Sync', ('h264',),
                          (('h264', 'h264_qsv'),), False),
    ))
    page.stack.setCurrentIndex(1)

    assert page.encoder_combo.currentData() == 'amf'
    assert page.codec_combo.currentData() == 'h264'
    assert 'amf' in page.encoder_combo.currentText().lower()
    page._on_encoder_apply()
    assert sm.get('encoder_pref') == 'amf'
    assert sm.get('codec_pref') == 'h264'
    page.deleteLater()
    app.processEvents()


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows encoder profile')
@pytest.mark.parametrize('target_encoder', ['nvenc', 'qsv'])
def test_switching_windows_backend_keeps_stale_codec_until_explicit_choice(
        tmp_path, monkeypatch, target_encoder):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    sm.set('encoder_pref', 'amf')
    sm.set('codec_pref', 'av1')
    sm.save_settings()

    page = _SettingsPage(sm)
    page._on_encoder_capabilities_ready((
        EncoderCapability('amf', 'AMD AMF', ('h264',),
                          (('h264', 'h264_amf'),), False),
        EncoderCapability('nvenc', 'NVIDIA NVENC', ('h264',),
                          (('h264', 'h264_nvenc'),), True),
        EncoderCapability('qsv', 'Intel Quick Sync', ('h264',),
                          (('h264', 'h264_qsv'),), False),
    ))
    page.stack.setCurrentIndex(1)
    page.encoder_combo.setCurrentIndex(page.encoder_combo.findData(target_encoder))

    assert page.encoder_combo.currentData() == target_encoder
    assert page.codec_combo.currentData() == 'av1'
    page._on_encoder_apply()
    assert sm.get('encoder_pref') == target_encoder
    assert sm.get('codec_pref') == 'av1'
    page.deleteLater()
    app.processEvents()
