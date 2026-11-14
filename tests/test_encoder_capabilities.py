from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
import json
import os
import subprocess
import sys

import pytest

from core.encoder_capabilities import (
    EncoderCapability,
    GpuProbeContext,
    _windows_adapter_signature,
    available_codecs,
    load_cached_encoder_capabilities,
    probe_encoder_capabilities,
    save_encoder_capabilities_cache,
)


def test_windows_cache_tracks_headless_gpu_and_stable_monitor_identity(tmp_path, monkeypatch):
    import ctypes

    display_class = '{4d36e968-e325-11ce-bfc1-08002be10318}'
    device_ids = (r'PCI\VEN_8086&DEV_9A60\INTEL', r'PCI\VEN_10DE&DEV_25A2\NVIDIA')
    device_list = '\0'.join(device_ids) + '\0\0'
    versions = ['32.0.101.6881', '32.0.16.1692']
    problems = [0, 0]

    class _FakeKey:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self.path

        def __exit__(self, *_args):
            return False

    class _FakeWinreg:
        HKEY_LOCAL_MACHINE = object()

        @staticmethod
        def OpenKey(_root, path):
            return _FakeKey(path)

        @staticmethod
        def QueryValueEx(key, name):
            if name == 'Driver':
                index = next(i for i, device in enumerate(device_ids) if key.endswith(device))
                return f'{display_class}\\{index:04}', None
            index = int(key[-4:])
            return (versions[index] if name == 'DriverVersion' else f'oem{index}.inf', None)

    class _FakeCfgMgr:
        @staticmethod
        def CM_Get_Device_ID_List_SizeW(size, device_class, flags):
            assert device_class == display_class and flags == 0x300
            size._obj.value = len(device_list)
            return 0

        @staticmethod
        def CM_Get_Device_ID_ListW(device_class, buffer, size, flags):
            assert device_class == display_class and size == len(device_list) and flags == 0x300
            buffer[:] = device_list
            return 0

        @staticmethod
        def CM_Locate_DevNodeW(devnode, identity, _flags):
            devnode._obj.value = device_ids.index(identity)
            return 0

        @staticmethod
        def CM_Get_DevNode_Status(status, problem, devnode, _flags):
            code = problems[devnode.value]
            status._obj.value = 0x400 if code else 0x8
            problem._obj.value = code
            return 0

    monkeypatch.setattr('core.encoder_capabilities.sys.platform', 'win32')
    monkeypatch.setitem(sys.modules, 'winreg', _FakeWinreg)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda _name: _FakeCfgMgr(), raising=False)
    records = [SimpleNamespace(
        monitor_device_path=r'\\?\display#one', gdi_name=r'\\.\DISPLAY1',
        x=0, y=0, width=1920, height=1080, primary=True,
        hmonitor=1234, active=True)]
    monkeypatch.setattr(
        'core.windows_monitor.enumerate_windows_monitor_records',
        lambda: records,
    )

    signature = json.loads(_windows_adapter_signature())
    assert [(item[0], item[2]) for item in signature['adapters']] == [
        (device_ids[1].casefold(), versions[1]),
        (device_ids[0].casefold(), versions[0]),
    ]
    assert signature['topology'] == [[
        r'\\?\display#one', r'\\.\display1', 0, 0, 1920, 1080, True,
    ]]

    ffmpeg, cache = tmp_path / 'ffmpeg.exe', tmp_path / 'cache.json'
    ffmpeg.write_bytes(b'ffmpeg')
    assert save_encoder_capabilities_cache((), str(ffmpeg), cache_file=cache)
    records[0].hmonitor = 5678
    assert load_cached_encoder_capabilities(str(ffmpeg), cache_file=cache) == ()
    versions[1] = '32.0.16.2000'
    assert load_cached_encoder_capabilities(str(ffmpeg), cache_file=cache) is None
    assert save_encoder_capabilities_cache((), str(ffmpeg), cache_file=cache)
    problems[1] = 22  # CM_PROB_DISABLED
    assert load_cached_encoder_capabilities(str(ffmpeg), cache_file=cache) is None


@pytest.fixture(autouse=True)
def native_runtime(tmp_path):
    engine = tmp_path / 'FTHRClips.exe'
    engine.write_bytes(b'PE fixture FTHR_GPU_ENCODER_PROBE_V1')
    for name in ('avcodec-62.dll', 'avutil-60.dll', 'swscale-9.dll', 'avformat-62.dll'):
        (tmp_path / name).write_bytes(name.encode())
    return engine


def _gpu_response(command):
    return dict(schema_version=1, backend=command[2], codec=command[3],
                monitor=command[4], hardware=True, gpu_input=True,
                frames_submitted=48, frames_decoded=48, source_width=640, source_height=360,
                width=(int(command[5]) or 640) & ~1, height=(int(command[6]) or 360) & ~1,
                fps=int(command[7]), bitrate_kbps=int(command[8]), preset=int(command[9]),
                fit=int(command[10]), adapter_luid_high=0, adapter_luid_low=42, driver_version='1')


def _native_runner(working, mutate=lambda raw: None):
    def run(command, **_kwargs):
        if '-c:v' in command:
            return _runner_for(working)(command)
        name = f'{command[3]}_{command[2]}'
        raw = _gpu_response(command)
        mutate(raw)
        return SimpleNamespace(returncode=0 if name in working else 1,
                               stdout=('driver log\nFTHR_GPU_ENCODER_PROBE_V1 '
                                       + json.dumps(raw) + '\n').encode())
    return run


def _runner_for(working: set[str]):
    def _run(command, **_kwargs):
        encoder = command[command.index('-c:v') + 1]
        return SimpleNamespace(returncode=0 if encoder in working else 1)
    return _run


def test_windows_returns_only_usable_hardware_backends(tmp_path):
    capabilities = probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'),
        platform='win32',
        runner=_native_runner({'h264_amf', 'hevc_amf'}),
    )

    assert [(item.key, item.codecs) for item in capabilities] == [
        ('amf', ('h264', 'hevc')),
    ]
    assert not capabilities[0].supports_presets
    assert available_codecs(capabilities, 'amf') == ('h264', 'hevc')
    assert available_codecs(capabilities, 'auto') == ('h264', 'hevc')


@pytest.mark.parametrize(('field', 'value'), [
    ('schema_version', 0), ('schema_version', True), ('backend', 'qsv'),
    ('codec', 'hevc'), ('monitor', 'other monitor'), ('hardware', False), ('gpu_input', False),
    ('frames_decoded', 47), ('frames_submitted', 1), ('width', 638), ('height', 358),
    ('source_width', 0), ('fps', 30), ('bitrate_kbps', 8000), ('preset', 7), ('fit', 1),
    ('driver_version', ''), ('adapter_luid_low', '42'),
])
def test_native_probe_rejects_incomplete_or_mismatched_evidence(tmp_path, field, value):
    capabilities = probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32',
        runner=_native_runner({'h264_amf'}, lambda raw: raw.update({field: value})))
    assert not capabilities


def _cross_adapter_response(raw):
    raw.update(gpu_input=False, input_path='cross-adapter-cpu', adapter_vendor_id=0x10de,
               capture_adapter_luid_high=0, capture_adapter_luid_low=7)


@pytest.mark.parametrize('fit', [0, 1])
def test_windows_accepts_verified_nvenc_cross_adapter_capture(tmp_path, fit):
    def response(raw):
        _cross_adapter_response(raw)
        raw.update(source_width=1280, source_height=1024)

    capabilities = probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32',
        context=GpuProbeContext(width=1920, height=1080, fit=fit),
        runner=_native_runner({'h264_nvenc', 'hevc_nvenc'}, response))
    assert available_codecs(capabilities, 'nvenc') == ('h264', 'hevc')


@pytest.mark.parametrize(('backend', 'overrides'), [
    ('amf', {'adapter_vendor_id': 0x1002}),
    ('qsv', {'adapter_vendor_id': 0x8086}),
    ('nvenc', {'input_path': 'same-adapter-gpu', 'gpu_input': True}),
    ('nvenc', {'gpu_input': True}),
    ('nvenc', {'capture_adapter_luid_low': 42}),
    ('nvenc', {'capture_adapter_luid_low': 0}),
    ('nvenc', {'capture_adapter_luid_low': None}),
    ('nvenc', {'adapter_vendor_id': 0x8086}),
    ('nvenc', {'adapter_vendor_id': None}),
    ('nvenc', {'adapter_luid_high': 1 << 31}),
    ('nvenc', {'driver_version': '0'}),
    ('nvenc', {'frames_decoded': 47}),
])
def test_native_probe_rejects_invalid_cross_adapter_evidence(tmp_path, backend, overrides):
    def mutate(raw):
        _cross_adapter_response(raw)
        raw.update(overrides)

    assert not probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32',
        runner=_native_runner({f'h264_{backend}'}, mutate))


def test_windows_accepts_same_adapter_proof_with_explicit_path(tmp_path):
    def mutate(raw):
        raw.update(input_path='same-adapter-gpu', adapter_vendor_id=0x8086,
                   capture_adapter_luid_high=0, capture_adapter_luid_low=42)

    capabilities = probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32',
        runner=_native_runner({'h264_qsv'}, mutate))
    assert available_codecs(capabilities, 'qsv') == ('h264',)


def test_windows_does_not_launch_old_engines_or_use_cpu_hardware_probe(tmp_path, native_runtime):
    native_runtime.write_bytes(b'old engine without probe support')
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return _runner_for({'h264_amf', 'libopenh264'})(command, **kwargs)

    result = probe_encoder_capabilities(str(tmp_path / 'ffmpeg.exe'), platform='win32', runner=run)
    assert [(item.key, item.codecs) for item in result] == [('software', ('h264',))]
    assert len(commands) == 1 and commands[0][commands[0].index('-c:v') + 1] == 'libopenh264'


def test_native_probe_crash_timeout_and_malformed_output_are_unverified(tmp_path):
    for failure in (OSError('crash'), subprocess.TimeoutExpired('probe', 15)):
        def run(*_args, _failure=failure, **_kwargs):
            raise _failure
        assert not probe_encoder_capabilities(
            str(tmp_path / 'ffmpeg.exe'), platform='win32', runner=run)
    assert not probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32',
        runner=lambda command, **_kwargs: SimpleNamespace(
            returncode=1 if '-c:v' in command else 0, stdout=b'{}'))


@pytest.mark.parametrize('context', [
    GpuProbeContext(monitor='secondary'), GpuProbeContext(width=1920, height=1080),
    GpuProbeContext(fps=120), GpuProbeContext(bitrate_kbps=50000),
    GpuProbeContext(preset=6), GpuProbeContext(fit=1),
])
def test_windows_cache_is_bound_to_monitor_and_capture_parameters(tmp_path, monkeypatch, context):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'ffmpeg')
    cache = tmp_path / 'cache.json'
    monkeypatch.setattr('core.encoder_capabilities._windows_adapter_signature', lambda: 'driver')
    assert save_encoder_capabilities_cache((), str(ffmpeg), platform='win32', cache_file=cache)
    assert load_cached_encoder_capabilities(
        str(ffmpeg), platform='win32', cache_file=cache, context=context) is None


@pytest.mark.parametrize('name', [
    'FTHRClips.exe', 'avcodec-62.dll', 'avutil-60.dll', 'swscale-9.dll',
    'avformat-62.dll',
])
def test_windows_cache_is_bound_to_native_engine_and_codec_runtime(tmp_path, monkeypatch, name):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'ffmpeg')
    cache = tmp_path / 'cache.json'
    monkeypatch.setattr('core.encoder_capabilities._windows_adapter_signature', lambda: 'driver')
    assert save_encoder_capabilities_cache((), str(ffmpeg), platform='win32', cache_file=cache)
    with (tmp_path / name).open('ab') as changed:
        changed.write(b'rebuilt')
    assert load_cached_encoder_capabilities(str(ffmpeg), platform='win32', cache_file=cache) is None


def test_gpu_probe_passes_selected_geometry_rate_and_monitor(tmp_path):
    context = GpuProbeContext(monitor=r'\\?\display#second', width=1920, height=1080,
                              fps=120, bitrate_kbps=35000, preset=6, fit=1)
    result = probe_encoder_capabilities(
        str(tmp_path / 'ffmpeg.exe'), platform='win32', context=context,
        runner=_native_runner({'h264_amf'}))
    assert available_codecs(result, 'amf') == ('h264',)
    assert replace(context, fps=60) != context


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


def test_windows_cache_expires_when_ffmpeg_content_changes_without_stat_change(
        tmp_path, monkeypatch):
    ffmpeg = tmp_path / 'ffmpeg.exe'
    ffmpeg.write_bytes(b'first build')
    cache = tmp_path / 'encoder-capabilities.json'
    capabilities = (EncoderCapability(
        'nvenc', 'NVIDIA NVENC', ('h264',),
        (('h264', 'h264_nvenc'),), True),)
    monkeypatch.setattr(
        'core.encoder_capabilities._windows_adapter_signature',
        lambda: 'test-adapter|driver:stable')

    assert save_encoder_capabilities_cache(
        capabilities, str(ffmpeg), platform='win32', cache_file=cache)
    stat = ffmpeg.stat()
    ffmpeg.write_bytes(b'second text')
    os.utime(ffmpeg, ns=(stat.st_atime_ns, stat.st_mtime_ns))
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
    assert page.performance_audio_check.text() == 'Enable audio capture'
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


def test_settings_cache_and_hashing_are_deferred_to_worker(tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    import main
    page_type = main._SettingsPage
    start_probe = page_type._start_encoder_probe
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(page_type, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    sm.set('framerate', 120)
    sm.set('capture_monitor', 'saved-monitor')
    page = page_type(sm)
    workers, calls = [], []
    monkeypatch.setattr(main.threading, 'Thread', lambda **kwargs: SimpleNamespace(
        start=lambda: workers.append(kwargs['target'])))
    monkeypatch.setattr(main, 'enumerate_windows_monitors', lambda: [])
    monkeypatch.setattr(main, 'resolve_monitor_selection', lambda *_args: SimpleNamespace(path='resolved-monitor'))

    def load(**kwargs):
        calls.append(kwargs)
        return (EncoderCapability('amf', 'AMD AMF', ('h264',), (('h264', 'h264_amf'),)),)

    monkeypatch.setattr(main, 'load_cached_encoder_capabilities', load)
    monkeypatch.setattr(main, 'probe_encoder_capabilities', lambda **_kwargs: pytest.fail('cache hit must not probe'))
    start_probe(page)
    assert len(workers) == 1 and not calls
    workers[0]()
    assert calls[0]['context'].fps == 120
    assert calls[0]['context'].monitor == ('resolved-monitor' if sys.platform == 'win32' else 'saved-monitor')
    assert page._encoder_probe_from_cache and page.encoder_refresh_btn.isEnabled()
    page.deleteLater()
    app.processEvents()


def test_settings_discard_probe_result_after_parameters_change(tmp_path, monkeypatch):
    qt_widgets = pytest.importorskip('PySide6.QtWidgets')
    from core.settings_manager import SettingsManager
    from main import _SettingsPage
    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    sm = SettingsManager()
    page = _SettingsPage(sm)
    page._encoder_probe_epoch = 2
    previous = page._encoder_probe_context()
    sm.set('framerate', previous.fps + 30)
    restarted = []
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self, **kwargs: restarted.append(kwargs))
    page._on_encoder_capabilities_ready((1, previous, (), False))
    assert not restarted
    page._on_encoder_capabilities_ready((2, previous, (), False))
    assert restarted == [{'force': True}]
    assert not page._encoder_inventory_ready
    page.deleteLater()
    app.processEvents()


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


def test_encoder_preferences_restore_after_failed_auto_save(qtbot, tmp_path, monkeypatch):
    from core.settings_manager import SettingsManager
    from main import _SettingsPage

    monkeypatch.setattr('pathlib.Path.home', lambda: tmp_path)
    monkeypatch.setattr(_SettingsPage, '_start_encoder_probe', lambda _self: None)
    sm = SettingsManager()
    save_attempts = []
    def reject_save():
        save_attempts.append(True)
        return False

    monkeypatch.setattr(sm, 'save_settings', reject_save)
    page = _SettingsPage(sm)
    qtbot.addWidget(page)
    _ = page.encoder_combo
    capability = EncoderCapability('nvenc', 'NVIDIA NVENC', ('h264',),
                                   (('h264', 'h264_nvenc'),), True)
    page._on_encoder_capabilities_ready((capability,))
    assert save_attempts == []

    with qtbot.assertNotEmitted(page.encoder_config_changed):
        page.encoder_combo.setCurrentIndex(page.encoder_combo.findData('nvenc'))
    assert sm.get('encoder_pref') == 'auto'
    assert page.encoder_combo.currentData() == 'auto'
    assert not page.encoder_save_error.isHidden()
    assert save_attempts == [True]
