#!/usr/bin/env python3
"""Verify Python/Windows shared-memory layouts and enum values.

Model native alignment and compare field order, types, extents, offsets,
and size with ctypes. Also check retired command slots and mapping version.
This cannot identify the headers used by a running binary.
Exit 0 means agreement; 1 means drift.
"""

from __future__ import annotations

import ctypes
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'FTHR_UI'))

WIN_HEADER = ROOT / 'FTHRcapture' / 'FTHRclips' / 'include' / 'shared_memory.h'
BRIDGE = ROOT / 'FTHR_UI' / 'core' / 'capture_bridge.py'

# C++ scalar -> (size, alignment). Both MSVC x64 and GCC x86-64 use these.
# wchar_t is 2 bytes on Windows and only appears in the Windows header.
_SCALARS = {
    'uint32_t': (4, 4),
    'int32_t': (4, 4),
    'uint64_t': (8, 8),
    'int64_t': (8, 8),
    'float': (4, 4),
    'double': (8, 8),
    'bool': (1, 1),
    'char': (1, 1),
    'wchar_t': (2, 2),
    'CommandType': (4, 4),      # enum class : uint32_t
    'ResponseType': (4, 4),     # enum class : uint32_t
}

# ctypes type -> the C++ scalar it must correspond to.
_CTYPES_TO_CPP = {
    ctypes.c_uint32: 'uint32_t',
    ctypes.c_uint64: 'uint64_t',
    ctypes.c_float: 'float',
    ctypes.c_bool: 'bool',
    ctypes.c_char: 'char',
}
if hasattr(ctypes, 'c_wchar'):
    _CTYPES_TO_CPP[ctypes.c_wchar] = 'wchar_t'


class Report:
    def __init__(self) -> None:
        self.failed = False

    def ok(self, msg: str) -> None:
        print(f'  OK    {msg}')

    def fail(self, msg: str) -> None:
        self.failed = True
        print(f'  FAIL  {msg}')

    def info(self, msg: str) -> None:
        print(f'        {msg}')


# C++ header parsing

_FIELD_RE = re.compile(
    r'^\s*(?:volatile\s+)?'
    r'(?P<type>[A-Za-z_][A-Za-z0-9_]*)\s+'
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\s*\[\s*(?P<count>\d+)\s*\])?\s*;'
)


def _strip_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def parse_struct(path: Path, struct_name: str = 'SharedMemoryLayout'):
    """Return [(field_name, cpp_type, array_count_or_None), ...]."""
    src = _strip_comments(path.read_text(encoding='utf-8', errors='replace'))
    m = re.search(rf'struct\s+{struct_name}\s*\{{', src)
    if not m:
        raise ValueError(f'{path.name}: struct {struct_name} not found')
    # Walk braces to find the matching close.
    depth, i = 0, m.end() - 1
    while i < len(src):
        if src[i] == '{':
            depth += 1
        elif src[i] == '}':
            depth -= 1
            if depth == 0:
                break
        i += 1
    body = src[m.end():i]

    fields = []
    for line in body.splitlines():
        fm = _FIELD_RE.match(line)
        if not fm:
            continue
        t = fm.group('type')
        if t not in _SCALARS:
            continue  # method declarations, nested types — not data members
        count = fm.group('count')
        fields.append((fm.group('name'), t, int(count) if count else None))
    return fields


def parse_enum(path: Path, enum_name: str) -> dict[str, int]:
    src = _strip_comments(path.read_text(encoding='utf-8', errors='replace'))
    m = re.search(rf'enum\s+class\s+{enum_name}\s*(?::\s*\w+\s*)?\{{(.*?)\}}',
                  src, flags=re.S)
    if not m:
        raise ValueError(f'{path.name}: enum {enum_name} not found')
    out: dict[str, int] = {}
    for name, val in re.findall(r'([A-Z_][A-Z0-9_]*)\s*=\s*(\d+)', m.group(1)):
        out[name] = int(val)
    return out


def layout_of(fields) -> tuple[list[tuple[str, int, int]], int, int]:
    """Model natural alignment. Returns (offsets, total_size, struct_align)."""
    offset = 0
    struct_align = 1
    out = []
    for name, cpp_type, count in fields:
        size, align = _SCALARS[cpp_type]
        struct_align = max(struct_align, align)
        offset = (offset + align - 1) // align * align
        total = size * (count or 1)
        out.append((name, offset, total))
        offset += total
    padded = (offset + struct_align - 1) // struct_align * struct_align
    return out, padded, struct_align


# Python side

def python_fields(struct_cls):
    """Return [(name, cpp_type, count_or_None)] for a ctypes Structure."""
    out = []
    for name, ftype in struct_cls._fields_:
        count = None
        base = ftype
        if hasattr(ftype, '_length_') and hasattr(ftype, '_type_'):
            count = ftype._length_
            base = ftype._type_
        cpp = _CTYPES_TO_CPP.get(base)
        if cpp is None:
            raise ValueError(f'unmapped ctypes type for field {name}: {base}')
        out.append((name, cpp, count))
    return out


def load_bridge_struct(platform: str):
    """Import capture_bridge.SharedMemoryLayout as it would be on *platform*.

    capture_bridge picks its struct with `if sys.platform == 'win32'` at import
    time, so verifying the branch we are not running on means re-executing the
    module with sys.platform patched.
    """
    import types

    src = BRIDGE.read_text(encoding='utf-8')
    mod = types.ModuleType(f'_bridge_{platform}')
    mod.__dict__['__file__'] = str(BRIDGE)

    real_platform = sys.platform
    # Stop the module at the struct definitions: everything after is runtime
    # IPC code that would try to open a real mapping.
    cut = src.index('class CaptureBridge')
    try:
        sys.platform = platform
        exec(compile(src[:cut], str(BRIDGE), 'exec'), mod.__dict__)
    finally:
        sys.platform = real_platform
    return mod.SharedMemoryLayout


# Comparison

# `enum class CommandType : uint32_t` has exactly the representation of a
# uint32_t, which is what the Python side declares. Treat them as one type so
# the comparison flags real drift rather than this deliberate difference.
_TYPE_EQUIVALENTS = {
    'CommandType': 'uint32_t',
    'ResponseType': 'uint32_t',
}


def _normalise(cpp_type: str) -> str:
    return _TYPE_EQUIVALENTS.get(cpp_type, cpp_type)


def compare(rep: Report, label: str, cpp_fields, py_fields) -> None:
    cpp_layout, cpp_size, cpp_align = layout_of(cpp_fields)
    py_layout, py_size, py_align = layout_of(py_fields)

    if len(cpp_fields) != len(py_fields):
        rep.fail(f'{label}: field count differs — C++ {len(cpp_fields)}, '
                 f'Python {len(py_fields)}')
        cpp_names = [f[0] for f in cpp_fields]
        py_names = [f[0] for f in py_fields]
        rep.info(f'only in C++:    {sorted(set(cpp_names) - set(py_names))}')
        rep.info(f'only in Python: {sorted(set(py_names) - set(cpp_names))}')
        return

    drift = False
    # Lengths were compared above, so strict=True can only fire on a bug here.
    for idx, ((cn, ct, cc), (pn, pt, pc)) in enumerate(
            zip(cpp_fields, py_fields, strict=True)):
        co, csz = cpp_layout[idx][1], cpp_layout[idx][2]
        po, psz = py_layout[idx][1], py_layout[idx][2]
        if cn != pn:
            rep.fail(f'{label}: field {idx} order/name — C++ {cn!r}, Python {pn!r}')
            drift = True
        elif _normalise(ct) != _normalise(pt):
            rep.fail(f'{label}: {cn}: type — C++ {ct}, Python {pt}')
            drift = True
        elif (cc or 1) != (pc or 1):
            rep.fail(f'{label}: {cn}: array extent — C++ {cc}, Python {pc}')
            drift = True
        elif co != po or csz != psz:
            rep.fail(f'{label}: {cn}: offset/size — C++ @{co}+{csz}, '
                     f'Python @{po}+{psz}')
            drift = True

    if cpp_size != py_size:
        rep.fail(f'{label}: total size — C++ {cpp_size} B, Python {py_size} B')
        drift = True
    if cpp_align != py_align:
        rep.fail(f'{label}: struct alignment — C++ {cpp_align}, Python {py_align}')
        drift = True

    if not drift:
        rep.ok(f'{label}: {len(cpp_fields)} fields, {cpp_size} B, '
               f'align {cpp_align} — identical')


def compare_enums(rep: Report, label: str, cpp: dict, py: dict) -> None:
    if cpp == py:
        rep.ok(f'{label}: {len(cpp)} values match')
        return
    for name in sorted(set(cpp) | set(py)):
        if cpp.get(name) != py.get(name):
            rep.fail(f'{label}: {name} — C++ {cpp.get(name)}, Python {py.get(name)}')


def main() -> int:
    rep = Report()
    print('Shared-memory contract verification')
    print()

    print('Windows layout (capture_bridge win32 branch vs shared_memory.h):')
    try:
        win_cpp = parse_struct(WIN_HEADER)
        win_py = python_fields(load_bridge_struct('win32'))
        compare(rep, 'win32', win_cpp, win_py)
    except Exception as exc:
        rep.fail(f'win32: {exc}')

    # Enums
    print()
    print('Command / response enums:')
    try:
        sys.path.insert(0, str(ROOT / 'FTHR_UI'))
        from core.capture_bridge import CommandType, ResponseType
        py_cmd = {m.name: m.value for m in CommandType}
        py_rsp = {m.name: m.value for m in ResponseType}
        for header, tag in ((WIN_HEADER, 'win32'),):
            compare_enums(rep, f'CommandType ({tag})',
                          parse_enum(header, 'CommandType'), py_cmd)
            compare_enums(rep, f'ResponseType ({tag})',
                          parse_enum(header, 'ResponseType'), py_rsp)

        # Slots 4..9 were the old SET_* runtime commands. They are dead but must
        # stay occupied: an engine built before the change still acts on them,
        # so reusing a number would make an old binary do the wrong thing.
        reserved = {4, 5, 6, 7, 8, 9}
        occupied = {v for v in py_cmd.values()}
        missing = reserved - occupied
        if missing:
            rep.fail(f'reserved command slots {sorted(missing)} are vacant — '
                     f'they must stay defined for ABI stability')
        else:
            rep.ok('reserved command slots 4..9 still occupied')
    except Exception as exc:
        rep.fail(f'enums: {exc}')

    # Mapping name
    print()
    print('Mapping name:')
    try:
        text = BRIDGE.read_text(encoding='utf-8')
        m = re.search(r"SHARED_MEM_NAME\s*=\s*'([^']+)'", text)
        if not m:
            rep.fail('SHARED_MEM_NAME not found in capture_bridge.py')
        elif not re.search(r'_v\d+$', m.group(1)):
            rep.fail(f'{m.group(1)!r} carries no _vN layout suffix — an old '
                     f'engine could map a new layout')
        else:
            rep.ok(f'{m.group(1)} — versioned, old and new cannot collide')
    except Exception as exc:
        rep.fail(f'mapping name: {exc}')

    print()
    if rep.failed:
        print('RESULT: shared-memory contract is BROKEN.')
        return 1
    print('RESULT: shared-memory contract intact.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
