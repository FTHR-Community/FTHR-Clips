from core.windows_monitor import (
    DisplayDeviceRecord,
    MonitorSelectionError,
    MonitorSelectionState,
    build_monitor_choices,
    default_windows_monitor_path,
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
