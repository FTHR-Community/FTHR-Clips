from core.windows_monitor import (
    DisplayDeviceRecord,
    MonitorSelectionError,
    MonitorSelectionState,
    build_monitor_choices,
    default_windows_monitor_path,
    format_monitor_choice_label,
    normalize_monitor_device_path,
    resolve_monitor_selection,
    validate_engine_monitor_argument,
)


def test_monitor_choices_use_normalized_stable_device_paths():
    records = [
        DisplayDeviceRecord(
            gdi_name=r"\\.\DISPLAY2",
            friendly_name="Secondary Panel",
            monitor_device_path=r"  \\?\DISPLAY#ABC/DEF  ",
            active=True,
            x=-2560,
            width=2560,
            height=1080,
        )
    ]

    choices = build_monitor_choices(records)

    assert choices[0].device_path == r"\\?\display#abc\def"
    assert choices[0].gdi_name == r"\\.\DISPLAY2"
    assert (choices[0].x, choices[0].y,
            choices[0].width, choices[0].height) == (-2560, 0, 2560, 1080)


def test_monitor_label_contains_display_number_resolution_refresh_and_primary():
    from core.windows_monitor import MonitorChoice

    label = format_monitor_choice_label(MonitorChoice(
        r'\\.\DISPLAY2', 'AOC 24G2', r'\\?\display#a', False,
        width=1920, height=1080, refresh_rate=59.94), 99)

    assert label == 'AOC 24G2 · Display 2 · 1920×1080 @ 59.94 Hz'


def test_monitor_label_uses_qt_fallback_only_for_missing_native_fields():
    from core.windows_monitor import MonitorChoice

    choice = MonitorChoice(
        r'\\.\DISPLAY1', 'Generic PnP Monitor', r'\\?\display#a', True,
        width=0, height=0, refresh_rate=0.0)
    assert format_monitor_choice_label(
        choice, 1, fallback_width=2560, fallback_height=1440,
        fallback_refresh_rate=144.0,
    ) == 'Generic PnP Monitor · Display 1 · 2560×1440 @ 144 Hz · Primary'


def test_display_config_refresh_requires_a_valid_rational():
    import core.windows_monitor as windows_monitor

    rational = windows_monitor._DisplayConfigRational(Numerator=5994,
                                                       Denominator=100)
    assert windows_monitor._valid_display_config_refresh_rate(rational) == 59.94
    rational.Denominator = 0
    assert windows_monitor._valid_display_config_refresh_rate(rational) == 0.0


def test_monitor_label_rejects_non_finite_native_rate_before_qt_fallback():
    from core.windows_monitor import MonitorChoice

    choice = MonitorChoice(
        r'\\.\DISPLAY1', 'Panel', r'\\?\display#a', False,
        width=1920, height=1080, refresh_rate=float('nan'))
    assert '59.94 Hz' in format_monitor_choice_label(
        choice, 1, fallback_refresh_rate=59.94)
    choice = MonitorChoice(
        r'\\.\DISPLAY1', 'Panel', r'\\?\display#a', False,
        width=1920, height=1080, refresh_rate=float('inf'))
    assert '59.94 Hz' in format_monitor_choice_label(
        choice, 1, fallback_refresh_rate=59.94)


class _DisplayConfigFakeFunction:
    def __init__(self, callback):
        self._callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._callback(*args)


def _display_config_path(source_id, target_id, numerator=14400,
                         denominator=100):
    import core.windows_monitor as windows_monitor

    path = windows_monitor._DisplayConfigPathInfo()
    path.sourceInfo.adapterId.LowPart = 7
    path.sourceInfo.adapterId.HighPart = 0
    path.sourceInfo.id = source_id
    path.targetInfo.adapterId.LowPart = 7
    path.targetInfo.adapterId.HighPart = 0
    path.targetInfo.id = target_id
    path.targetInfo.refreshRate.Numerator = numerator
    path.targetInfo.refreshRate.Denominator = denominator
    path.flags = 1
    return path


def _fake_display_config_user32(paths, names, *, target_status=None,
                                source_status=None, query_statuses=None,
                                actual_counts=None):
    import ctypes
    import core.windows_monitor as windows_monitor

    target_status = dict(target_status or {})
    source_status = dict(source_status or {})
    query_statuses = list(query_statuses or [0])
    actual_paths, actual_modes = actual_counts or (len(paths), len(paths))
    query_calls = []
    source_names = {
        int(path.sourceInfo.id): names[int(path.targetInfo.id)][0]
        for path in paths
    }

    def get_sizes(_flags, path_count, mode_count):
        path_count._obj.value = len(paths)
        mode_count._obj.value = len(paths)
        return 0

    def query(_flags, path_count, path_array, mode_count, _mode_array,
              _topology):
        query_calls.append(True)
        status = query_statuses[min(len(query_calls) - 1,
                                    len(query_statuses) - 1)]
        path_count._obj.value = actual_paths
        mode_count._obj.value = actual_modes
        if status == 0:
            for index, path in enumerate(paths):
                if index < len(path_array):
                    path_array[index] = path
        return status

    def get_info(header_pointer):
        header = header_pointer._obj
        if header.type == windows_monitor._DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME:
            status = source_status.get(int(header.id), 0)
            if status:
                return status
            info = ctypes.cast(
                header_pointer,
                ctypes.POINTER(windows_monitor._DisplayConfigSourceDeviceName),
            ).contents
            info.viewGdiDeviceName = source_names[header.id]
            return 0
        if header.type == windows_monitor._DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME:
            info = ctypes.cast(
                header_pointer,
                ctypes.POINTER(windows_monitor._DisplayConfigTargetDeviceName),
            ).contents
            status = target_status.get(int(header.id), 0)
            if status:
                return status
            info.monitorFriendlyDeviceName = names[header.id][1]
            info.monitorDevicePath = names[header.id][2]
            return 0
        return 87

    return type('_FakeUser32', (), {
        'GetDisplayConfigBufferSizes': _DisplayConfigFakeFunction(get_sizes),
        'QueryDisplayConfig': _DisplayConfigFakeFunction(query),
        'DisplayConfigGetDeviceInfo': _DisplayConfigFakeFunction(get_info),
    })(), query_calls


def test_display_config_joins_two_targets_with_native_name_hz_and_path(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    paths = [_display_config_path(1, 101, 14400, 100),
             _display_config_path(2, 102, 5994, 100)]
    names = {
        101: (r'\\.\DISPLAY1', '27G2G8', r'\\?\DISPLAY#A'),
        102: (r'\\.\DISPLAY2', 'LG ULTRAWIDE', r'\\?\DISPLAY#B'),
    }
    fake, _calls = _fake_display_config_user32(paths, names)
    monitor_specs = {
        101: (r'\\.\DISPLAY1', 0, 0, 1920, 1080, True,
              r'\\?\DISPLAY#A'),
        102: (r'\\.\DISPLAY2', 1920, 0, 2560, 1080, False,
              r'\\?\DISPLAY#B'),
    }

    def get_monitor_info(monitor, info_pointer):
        info = info_pointer._obj
        gdi_name, x, y, width, height, primary, _path = monitor_specs[monitor]
        info.szDevice = gdi_name
        info.rcMonitor.left, info.rcMonitor.top = x, y
        info.rcMonitor.right, info.rcMonitor.bottom = x + width, y + height
        info.dwFlags = 1 if primary else 0
        return 1

    def enum_monitors(_dc, _clip, callback, _data):
        callback(101, None, None, 0)
        callback(102, None, None, 0)
        return 1

    def enum_devices(gdi_name, index, device_pointer, _flags):
        if index != 0:
            return 0
        device = device_pointer._obj
        for spec in monitor_specs.values():
            if spec[0].casefold() == str(gdi_name).casefold():
                device.DeviceString = 'Generic PnP Monitor'
                device.DeviceID = spec[6]
                device.StateFlags = 1
                return 1
        return 0

    fake.GetMonitorInfoW = _DisplayConfigFakeFunction(get_monitor_info)
    fake.EnumDisplayMonitors = _DisplayConfigFakeFunction(enum_monitors)
    fake.EnumDisplayDevicesW = _DisplayConfigFakeFunction(enum_devices)
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    monkeypatch.setattr(windows_monitor.ctypes, 'WINFUNCTYPE',
                        lambda *_a: (lambda callback: callback), raising=False)
    records = windows_monitor.enumerate_windows_monitor_records()
    metadata = windows_monitor._enumerate_display_config_metadata()

    assert [(item.gdi_name, item.friendly_name, item.device_path,
              item.refresh_rate) for item in metadata] == [
        (r'\\.\display1', '27G2G8', r'\\?\display#a', 144.0),
        (r'\\.\display2', 'LG ULTRAWIDE', r'\\?\display#b', 59.94),
    ]
    choices = windows_monitor.build_monitor_choices(records)
    assert [(choice.friendly_name, choice.refresh_rate,
             choice.width, choice.height) for choice in choices] == [
        ('27G2G8', 144.0, 1920, 1080),
        ('LG ULTRAWIDE', 59.94, 2560, 1080),
    ]
    assert [choice.device_path for choice in choices] == [
        r'\\?\display#a', r'\\?\display#b']
    assert choices[0].primary is True and choices[1].primary is False


def test_display_config_missing_api_or_target_name_returns_safe_fallback(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    class _MissingApi:
        pass

    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: _MissingApi(), raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []

    path = _display_config_path(1, 101)
    fake, _calls = _fake_display_config_user32(
        [path], {101: (r'\\.\DISPLAY1', 'Ignored', r'\\?\DISPLAY#A')},
        target_status={101: 5})
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []


def test_display_config_retries_resize_three_times_and_rejects_fourth(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    path = _display_config_path(1, 101)
    names = {101: (r'\\.\DISPLAY1', 'Panel', r'\\?\DISPLAY#A')}
    fake, calls = _fake_display_config_user32(
        [path], names, query_statuses=[122, 122, 0])
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    metadata = windows_monitor._enumerate_display_config_metadata()
    assert len(calls) == 3 and len(metadata) == 1

    fake, calls = _fake_display_config_user32(
        [path], names, query_statuses=[122, 122, 122, 0])
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []
    assert len(calls) == 3


def test_display_config_rejects_count_overruns(monkeypatch):
    import core.windows_monitor as windows_monitor

    path = _display_config_path(1, 101)
    names = {101: (r'\\.\DISPLAY1', 'Panel', r'\\?\DISPLAY#A')}
    fake, _calls = _fake_display_config_user32(
        [path], names, actual_counts=(2, 1))
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []

def test_display_config_mirror_with_failed_target_stays_ambiguous(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    paths = [_display_config_path(1, 101), _display_config_path(1, 102)]
    names = {
        101: (r'\\.\DISPLAY1', 'Panel A', r'\\?\DISPLAY#A'),
        102: (r'\\.\DISPLAY1', 'Panel B', r'\\?\DISPLAY#B'),
    }
    fake, _calls = _fake_display_config_user32(
        paths, names, target_status={102: 5})
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []


def test_display_config_mirror_with_failed_source_name_stays_ambiguous(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    paths = [_display_config_path(1, 101), _display_config_path(2, 102)]
    names = {
        101: (r'\\.\DISPLAY1', 'Panel A', r'\\?\DISPLAY#A'),
        102: (r'\\.\DISPLAY1', 'Panel B', r'\\?\DISPLAY#B'),
    }
    fake, _calls = _fake_display_config_user32(
        paths, names, source_status={2: 5})
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    assert windows_monitor._enumerate_display_config_metadata() == []


def test_display_config_mirror_with_blank_source_name_falls_back(monkeypatch):
    import core.windows_monitor as windows_monitor

    paths = [_display_config_path(1, 101), _display_config_path(1, 102)]
    for source_name in ('', '   '):
        names = {
            101: (source_name, 'Panel A', r'\\?\DISPLAY#A'),
            102: (source_name, 'Panel B', r'\\?\DISPLAY#B'),
        }
        fake, _calls = _fake_display_config_user32(paths, names)
        monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                            lambda *_a, _fake=fake, **_k: _fake, raising=False)
        assert windows_monitor._enumerate_display_config_metadata() == []


def test_display_config_mismatch_keeps_legacy_record_and_stable_path(
        monkeypatch):
    import core.windows_monitor as windows_monitor

    path = _display_config_path(1, 101)
    fake, _calls = _fake_display_config_user32(
        [path], {101: (r'\\.\DISPLAY1', 'Wrong target',
                       r'\\?\DISPLAY#OTHER')})

    def get_monitor_info(_monitor, info_pointer):
        info = info_pointer._obj
        info.szDevice = r'\\.\DISPLAY1'
        info.rcMonitor.left, info.rcMonitor.top = 0, 0
        info.rcMonitor.right, info.rcMonitor.bottom = 1920, 1080
        info.dwFlags = 1
        return 1

    def enum_monitors(_dc, _clip, callback, _data):
        callback(101, None, None, 0)
        return 1

    def enum_devices(gdi_name, index, device_pointer, _flags):
        if index != 0:
            return 0
        device = device_pointer._obj
        device.DeviceString = 'Generic PnP Monitor'
        device.DeviceID = r'\\?\DISPLAY#A'
        device.StateFlags = 1
        return 1

    fake.GetMonitorInfoW = _DisplayConfigFakeFunction(get_monitor_info)
    fake.EnumDisplayMonitors = _DisplayConfigFakeFunction(enum_monitors)
    fake.EnumDisplayDevicesW = _DisplayConfigFakeFunction(enum_devices)
    monkeypatch.setattr(windows_monitor.ctypes, 'WinDLL',
                        lambda *_a, **_k: fake, raising=False)
    monkeypatch.setattr(windows_monitor.ctypes, 'WINFUNCTYPE',
                        lambda *_a: (lambda callback: callback), raising=False)

    records = windows_monitor.enumerate_windows_monitor_records()

    assert len(records) == 1
    assert records[0].friendly_name == 'Generic PnP Monitor'
    assert records[0].monitor_device_path == r'\\?\DISPLAY#A'
    assert records[0].refresh_rate == 0.0


def test_monitor_choices_exclude_inactive_and_deduplicate_paths():
    records = [
        DisplayDeviceRecord("DISPLAY1", "One", r"\\?\DISPLAY#PATH-A", True),
        DisplayDeviceRecord("DISPLAY1", "Duplicate", r"\\?\display#path-a", True),
        DisplayDeviceRecord("DISPLAY2", "Inactive", r"\\?\DISPLAY#PATH-B", False),
    ]

    choices = build_monitor_choices(records)

    assert [(choice.gdi_name, choice.device_path) for choice in choices] == [
        ("DISPLAY1", r"\\?\display#path-a")
    ]


def test_normalize_monitor_device_path_rejects_blank_values():
    assert normalize_monitor_device_path("  ") == ""


def test_default_monitor_requires_unique_primary_or_sole_choice():
    choices = build_monitor_choices([
        DisplayDeviceRecord(
            'DISPLAY2', 'Secondary', r'\\?\DISPLAY#SECOND', True,
            primary=False),
        DisplayDeviceRecord(
            'DISPLAY1', 'Primary', r'\\?\DISPLAY#PRIMARY', True,
            primary=True),
    ])

    assert default_windows_monitor_path(choices) == r'\\?\display#primary'
    assert default_windows_monitor_path(choices[:1]) == r'\\?\display#second'
    assert default_windows_monitor_path([]) == ''


def _choice(name: str, *, primary: bool = False, gdi: str = 'DISPLAY1'):
    return DisplayDeviceRecord(
        gdi, name, rf"\\?\DISPLAY#{name}", True, primary=primary)


def test_first_run_resolves_unique_primary_and_requires_persistence():
    result = resolve_monitor_selection('', build_monitor_choices([
        _choice('PRIMARY', primary=True),
        _choice('SECONDARY'),
    ]))

    assert result.state is MonitorSelectionState.RESOLVED
    assert result.code == 'MONITOR_SELECTION_RESOLVED'
    assert result.path == r'\\?\display#primary'
    assert result.persist_required is True


def test_valid_non_primary_selection_is_preserved_without_persistence():
    choices = build_monitor_choices([
        _choice('PRIMARY', primary=True), _choice('SECONDARY')])
    result = resolve_monitor_selection(r'\\?\display#secondary', choices)

    assert result.state is MonitorSelectionState.RESOLVED
    assert result.path == r'\\?\display#secondary'
    assert result.persist_required is False


def test_stale_selection_recovers_to_unique_primary_not_first_item():
    choices = build_monitor_choices([
        _choice('SECONDARY'), _choice('PRIMARY', primary=True)])
    result = resolve_monitor_selection(r'\\?\display#removed', choices)

    assert result.state is MonitorSelectionState.RECOVERED
    assert result.previous_state is MonitorSelectionState.STALE
    assert result.path == r'\\?\display#primary'
    assert result.persist_required is True


def test_empty_and_diagnostic_sentinel_are_not_engine_values():
    choices = build_monitor_choices([_choice('PRIMARY', primary=True)])
    for saved in ('', 'unavailable:not_configured'):
        result = resolve_monitor_selection(saved, choices)
        assert result.path == r'\\?\display#primary'
        assert result.previous_state is MonitorSelectionState.NOT_CONFIGURED
    for invalid in ('', 'unavailable:not_configured', r'\\.\DISPLAY1'):
        try:
            validate_engine_monitor_argument(invalid)
        except MonitorSelectionError as error:
            assert error.result.code in {
                'MONITOR_SELECTION_NOT_CONFIGURED',
                'MONITOR_SELECTION_STALE',
            }
        else:
            raise AssertionError('unresolved monitor reached engine guard')


def test_no_displays_is_a_bounded_precise_failure():
    result = resolve_monitor_selection('', [])

    assert result.state is MonitorSelectionState.NO_DISPLAYS
    assert result.code == 'MONITOR_SELECTION_NO_DISPLAYS'
    assert result.path == ''
    assert result.persist_required is False


def test_ambiguous_topology_does_not_use_first_item():
    choices = build_monitor_choices([_choice('ONE'), _choice('TWO')])
    result = resolve_monitor_selection('', choices)

    assert result.state is MonitorSelectionState.AMBIGUOUS
    assert result.path == ''
    assert result.persist_required is False
    assert default_windows_monitor_path(choices) == ''


def test_legacy_gdi_name_migrates_only_when_unique():
    choices = build_monitor_choices([_choice('PRIMARY', primary=True)])
    result = resolve_monitor_selection(r'\\.\DISPLAY1', choices)

    assert result.state is MonitorSelectionState.RECOVERED
    assert result.path == r'\\?\display#primary'
    assert result.persist_required is True


def test_persisted_recovery_is_reusable_on_second_launch():
    choices = build_monitor_choices([_choice('PRIMARY', primary=True)])
    first = resolve_monitor_selection('', choices)
    second = resolve_monitor_selection(first.path, choices)

    assert first.persist_required is True
    assert second.state is MonitorSelectionState.RESOLVED
    assert second.path == first.path
    assert second.persist_required is False


def test_monitor_selection_is_independent_of_gpu_vendor_labels():
    for vendor in ('AMD Radeon(TM) Graphics', 'NVIDIA GeForce RTX'):
        choices = build_monitor_choices([
            DisplayDeviceRecord(
                r'\\.\DISPLAY1', vendor,
                r'\\?\DISPLAY#PRIMARY', True, primary=True),
        ])
        result = resolve_monitor_selection('', choices)

        assert result.state is MonitorSelectionState.RESOLVED
        assert result.path == r'\\?\display#primary'


def test_driver_missing_child_active_flag_is_inferred_only_for_one_child():
    import core.windows_monitor as windows_monitor

    child = windows_monitor._DisplayDeviceW()
    child.DeviceID = r'\\?\DISPLAY#DRIVER-ONLY'
    assert windows_monitor._active_monitor_children(
        [child], adapter_in_monitor_snapshot=True) == [child]

    second = windows_monitor._DisplayDeviceW()
    second.DeviceID = ''
    assert windows_monitor._active_monitor_children(
        [child, second], adapter_in_monitor_snapshot=True) == []


def test_driver_active_child_wins_even_when_other_children_are_inactive():
    import core.windows_monitor as windows_monitor

    active = windows_monitor._DisplayDeviceW()
    active.StateFlags = 0x00000001
    inactive = windows_monitor._DisplayDeviceW()
    inactive.DeviceID = r'\\?\DISPLAY#OTHER'
    assert windows_monitor._active_monitor_children(
        [inactive, active], adapter_in_monitor_snapshot=False) == [active]


def test_live_desktop_fallback_and_restored_pnp_path():
    from core.windows_monitor import desktop_monitor_identity, is_valid_monitor_device_path
    record = DisplayDeviceRecord(r"\\.\DISPLAY2", '', '', True,
                                 width=1280, height=1024, hmonitor=0x123)
    choice = build_monitor_choices([record])[0]
    assert choice.device_path == 'fthr-monitor:123:display2'
    assert desktop_monitor_identity(0x123, r"\\.\DISPLAY2") == choice.device_path
    assert validate_engine_monitor_argument(choice.device_path) == choice.device_path
    assert resolve_monitor_selection(choice.device_path, [choice]).resolved
    restored = build_monitor_choices([DisplayDeviceRecord(
        record.gdi_name, 'Panel', r"\\?\display#restored", True, hmonitor=record.hmonitor)])[0]
    assert is_valid_monitor_device_path(choice.device_path, [restored])
    assert resolve_monitor_selection(choice.device_path, [restored]).path == choice.device_path


def test_stale_desktop_fallback_never_redirects_to_primary():
    choices = build_monitor_choices([DisplayDeviceRecord(
        'DISPLAY2', '', '', True, primary=True, hmonitor=0x124)])
    result = resolve_monitor_selection('fthr-monitor:123:display2', choices)
    assert not result.resolved
    assert result.state is MonitorSelectionState.STALE


def test_invalid_live_desktop_identity_rejected():
    import pytest
    for value in ('fthr-monitor:0:display1', 'fthr-monitor:12:displayX',
                  'fthr-monitor:-1:display1', 'fthr-monitor:12:display1:extra'):
        with pytest.raises(MonitorSelectionError):
            validate_engine_monitor_argument(value)


def test_multiple_live_desktops_without_pnp_remain_distinct():
    choices = build_monitor_choices([
        DisplayDeviceRecord('DISPLAY1', '', '', True, hmonitor=11),
        DisplayDeviceRecord('DISPLAY2', '', '', True, hmonitor=12),
        DisplayDeviceRecord('DISPLAY3', '', '', False, hmonitor=13),
    ])
    assert len(choices) == 2
    assert choices[0].device_path != choices[1].device_path
    assert not resolve_monitor_selection('', choices).resolved


def test_missing_saved_pnp_path_requires_reselection_when_only_fallback_exists():
    choices = build_monitor_choices([DisplayDeviceRecord(
        'DISPLAY1', '', '', True, primary=True, hmonitor=42)])
    assert not resolve_monitor_selection(r"\\?\display#missing", choices).resolved


def test_winapi_monitorinfo_primary_flag_drives_first_run_selection(monkeypatch):
    """GetMonitorInfoW's MONITORINFOF_PRIMARY flag reaches selection policy."""
    import core.windows_monitor as windows_monitor

    class _FakeApiFunction:
        def __init__(self, callback):
            self._callback = callback
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self._callback(*args)

    def fake_get_monitor_info(monitor, info_pointer):
        info = info_pointer._obj
        if monitor == 101:
            info.szDevice = r"\\.\DISPLAY1"
            info.rcMonitor.left, info.rcMonitor.top = 0, 0
            info.rcMonitor.right, info.rcMonitor.bottom = 1920, 1080
            info.dwFlags = 0x00000001  # MONITORINFOF_PRIMARY
        else:
            info.szDevice = r"\\.\DISPLAY2"
            info.rcMonitor.left, info.rcMonitor.top = 1920, 0
            info.rcMonitor.right, info.rcMonitor.bottom = 3840, 1080
            info.dwFlags = 0
        return 1

    def fake_enum_display_monitors(_dc, _clip, callback, _data):
        callback(101, None, None, 0)
        callback(202, None, None, 0)
        return 1

    def fake_enum_display_devices(gdi_name, index, device_pointer, _flags):
        if index != 0:
            return 0
        device = device_pointer._obj
        device.DeviceString = gdi_name
        device.DeviceID = rf"\\?\DISPLAY#{gdi_name[-1:]}"
        device.StateFlags = 0x00000001  # DISPLAY_DEVICE_ACTIVE
        return 1

    class _FakeUser32:
        GetMonitorInfoW = _FakeApiFunction(fake_get_monitor_info)
        EnumDisplayMonitors = _FakeApiFunction(fake_enum_display_monitors)
        EnumDisplayDevicesW = _FakeApiFunction(fake_enum_display_devices)

    monkeypatch.setattr(
        windows_monitor.ctypes,
        'WinDLL',
        lambda *_args, **_kwargs: _FakeUser32(),
        raising=False,
    )
    # Keep this test focused on the WinAPI callback data while running on
    # non-Windows CI, where ctypes exposes CFUNCTYPE but not WINFUNCTYPE.
    monkeypatch.setattr(
        windows_monitor.ctypes,
        'WINFUNCTYPE',
        lambda *_args: (lambda callback: callback),
        raising=False,
    )

    records = windows_monitor.enumerate_windows_monitor_records()
    choices = build_monitor_choices(records)
    result = resolve_monitor_selection('', choices)

    assert [record.primary for record in records] == [True, False]
    assert result.state is MonitorSelectionState.RESOLVED
    assert result.path == r"\\?\display#1"


def test_real_windows_desktop_snapshot_has_valid_capture_arguments():
    import sys
    import pytest
    from core.windows_monitor import enumerate_windows_monitors
    if sys.platform != 'win32':
        pytest.skip('Windows desktop enumeration')
    for choice in enumerate_windows_monitors():
        assert choice.hmonitor > 0 and choice.width > 0 and choice.height > 0
        assert validate_engine_monitor_argument(choice.device_path)
        assert resolve_monitor_selection(choice.device_path, [choice]).resolved
