"""Verify ctypes/C++ layout agreement and reject deliberately mismatched layouts."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / 'tools'
sys.path.insert(0, str(TOOLS))

import verify_shared_memory_contract as vsmc  # noqa: E402


WIN_HEADER = ROOT / 'FTHRcapture' / 'FTHRclips' / 'include' / 'shared_memory.h'


# The contract itself

def test_contract_verifier_passes_on_current_tree():
    assert vsmc.main() == 0, 'shared-memory contract drifted — see output above'


@pytest.mark.parametrize('platform,header', [
    ('win32', WIN_HEADER),
])
def test_field_order_and_offsets_match(platform, header):
    cpp = vsmc.parse_struct(header)
    py = vsmc.python_fields(vsmc.load_bridge_struct(platform))

    assert [f[0] for f in cpp] == [f[0] for f in py], 'field order differs'

    cpp_layout, cpp_size, cpp_align = vsmc.layout_of(cpp)
    py_layout, py_size, py_align = vsmc.layout_of(py)
    assert cpp_layout == py_layout, 'field offsets/sizes differ'
    assert cpp_size == py_size
    assert cpp_align == py_align


@pytest.mark.parametrize('header', [WIN_HEADER])
def test_enums_match(header):
    from core.capture_bridge import CommandType, ResponseType

    assert vsmc.parse_enum(header, 'CommandType') == \
        {m.name: m.value for m in CommandType}
    assert vsmc.parse_enum(header, 'ResponseType') == \
        {m.name: m.value for m in ResponseType}


def test_reserved_command_slots_still_occupied():
    """Slots 4..9 are the retired SET_* commands.

    An engine built before they were retired still acts on those numbers, so
    handing one to a new command would make an old binary do the wrong thing
    instead of ignoring an unknown command.
    """
    from core.capture_bridge import CommandType

    values = {m.value for m in CommandType}
    assert {4, 5, 6, 7, 8, 9} <= values


def test_mapping_name_is_layout_versioned():
    from core.capture_bridge import CaptureBridge

    assert CaptureBridge.SHARED_MEM_NAME.rsplit('_', 1)[-1].startswith('v')


# Negative tests — prove the verifier can actually fail

def _cpp_fields():
    return vsmc.parse_struct(WIN_HEADER)


@pytest.mark.parametrize('mutate,expect', [
    # A field renamed on one side only.
    (lambda f: [('renamed', *f[0][1:])] + f[1:], 'order/name'),
    # An array shrunk — the classic buffer-size drift.
    (lambda f: [(n, t, (c - 1) if c else None) if n == 'ui_string' else (n, t, c)
                for n, t, c in f], 'array extent'),
    # A scalar widened, shifting every subsequent offset.
    (lambda f: [(n, 'uint64_t' if n == 'ui_param1' else t, c) for n, t, c in f],
     'offset/size'),
    # A field appended on one side only.
    (lambda f: f + [('trailing_field', 'uint32_t', None)], 'field count'),
])
def test_verifier_detects_drift(mutate, expect, capsys):
    rep = vsmc.Report()
    good = _cpp_fields()
    vsmc.compare(rep, 'synthetic', mutate(good), good)
    out = capsys.readouterr().out
    assert rep.failed, f'verifier accepted a mutated layout ({expect})'
    assert expect in out


def test_verifier_accepts_identical_layouts(capsys):
    rep = vsmc.Report()
    good = _cpp_fields()
    vsmc.compare(rep, 'synthetic', good, good)
    assert not rep.failed


def test_enum_typed_as_uint32_is_not_treated_as_drift():
    """`enum class CommandType : uint32_t` and c_uint32 are the same bytes.

    This equivalence is deliberate; if it is ever removed the verifier goes
    permanently red and people start ignoring it.
    """
    assert vsmc._normalise('CommandType') == vsmc._normalise('uint32_t')
    assert vsmc._normalise('ResponseType') == vsmc._normalise('uint32_t')
    assert vsmc._normalise('uint64_t') != vsmc._normalise('uint32_t')
