#!/usr/bin/env python3
"""Restore bundled shared libraries that stripping made unloadable.

PyInstaller strips every collected binary. Wheel libraries that auditwheel
already rewrote with patchelf (numpy's OpenBLAS/gfortran, PySide6's bundled
FFmpeg) carry extra PT_LOAD segments; older binutils `strip` rewrites those
with file offsets that no longer match their virtual addresses modulo the
page size. The dynamic loader then refuses the file with "ELF load command
address/offset not page-aligned", so the import fails at runtime even though
the AppImage itself launches.

For every misaligned ELF under --bundle, copy the unstripped original from
the same relative path under --site-packages, then rescan. Exit 0 when the
bundle is clean, 1 when a damaged file has no original or stays misaligned.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path

PT_LOAD = 1


def misaligned_load_segment(path: Path) -> bool:
    """True when a PT_LOAD segment has offset % align != vaddr % align."""
    try:
        with path.open('rb') as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:4] != b'\x7fELF' or header[4] != 2:
                return False   # not a 64-bit ELF
            little = header[5] == 1
            order = '<' if little else '>'
            phoff, = struct.unpack_from(order + 'Q', header, 32)
            phentsize, phnum = struct.unpack_from(order + 'HH', header, 54)
            handle.seek(phoff)
            table = handle.read(phentsize * phnum)
    except OSError:
        return False
    if phentsize < 56 or len(table) < phentsize * phnum:
        return False
    for index in range(phnum):
        base = index * phentsize
        p_type, = struct.unpack_from(order + 'I', table, base)
        p_offset, p_vaddr = struct.unpack_from(order + 'QQ', table, base + 8)
        p_align, = struct.unpack_from(order + 'Q', table, base + 48)
        if p_type == PT_LOAD and p_align > 1 and p_offset % p_align != p_vaddr % p_align:
            return True
    return False


def find_misaligned(bundle: Path) -> list[Path]:
    return sorted(
        path for path in bundle.rglob('*')
        if path.is_file() and not path.is_symlink() and misaligned_load_segment(path))


def repair(bundle: Path, site_packages: list[Path]) -> int:
    damaged = find_misaligned(bundle)
    if not damaged:
        print('    No misaligned ELF segments in the bundle.')
        return 0

    failed = False
    for path in damaged:
        relative = path.relative_to(bundle)
        original = next(
            (root / relative for root in site_packages if (root / relative).is_file()),
            None)
        if original is None:
            print(f'    ERROR: {relative} is misaligned and has no unstripped '
                  f'original under the given site-packages', file=sys.stderr)
            failed = True
            continue
        if misaligned_load_segment(original):
            print(f'    ERROR: the original {original} is itself misaligned',
                  file=sys.stderr)
            failed = True
            continue
        shutil.copy2(original, path)
        print(f'    Restored unstripped {relative}')

    remaining = find_misaligned(bundle)
    for path in remaining:
        print(f'    ERROR: still misaligned: {path.relative_to(bundle)}',
              file=sys.stderr)
    return 1 if failed or remaining else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--bundle', type=Path, required=True,
                        help='PyInstaller _internal directory')
    parser.add_argument('--site-packages', type=Path, action='append',
                        required=True,
                        help='Directory holding the original wheels; repeatable')
    args = parser.parse_args()
    if not args.bundle.is_dir():
        print(f'ERROR: bundle directory not found: {args.bundle}', file=sys.stderr)
        return 1
    return repair(args.bundle, args.site_packages)


if __name__ == '__main__':
    sys.exit(main())
