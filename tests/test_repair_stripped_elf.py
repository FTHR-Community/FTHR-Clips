"""Tests for restoring wheel libraries that strip left unloadable."""

from __future__ import annotations

import struct
from pathlib import Path

from tools.repair_stripped_elf import find_misaligned, misaligned_load_segment, repair

PT_LOAD = 1


def _elf(load_segments: list[tuple[int, int]], align: int = 0x1000) -> bytes:
    """Minimal 64-bit little-endian ELF: header plus PT_LOAD (offset, vaddr)."""
    phoff, phentsize = 64, 56
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<Q', header, 32, phoff)
    struct.pack_into('<HH', header, 54, phentsize, len(load_segments))
    table = b''.join(
        struct.pack('<IIQQQQQQ', PT_LOAD, 0, offset, vaddr, vaddr, 0, 0, align)
        for offset, vaddr in load_segments)
    return bytes(header) + table


GOOD = _elf([(0x0, 0x0), (0x288800, 0x289800), (0x16d2000, 0x15ed000)])
# The layout strip produced for numpy's OpenBLAS in the 1.1.2-alpha CI build.
STRIPPED = _elf([(0x0, 0x0), (0x1621098, 0x166b000)])


def test_detects_the_offset_vaddr_mismatch_strip_produced(tmp_path: Path) -> None:
    good, bad = tmp_path / 'good.so', tmp_path / 'bad.so'
    good.write_bytes(GOOD)
    bad.write_bytes(STRIPPED)
    assert not misaligned_load_segment(good)
    assert misaligned_load_segment(bad)


def test_ignores_non_elf_and_truncated_files(tmp_path: Path) -> None:
    (tmp_path / 'text.py').write_text('print("hi")\n')
    (tmp_path / 'short.so').write_bytes(b'\x7fELF\x02')
    assert find_misaligned(tmp_path) == []


def test_restores_original_from_matching_site_packages_path(tmp_path: Path) -> None:
    bundle, site = tmp_path / '_internal', tmp_path / 'site-packages'
    damaged = bundle / 'numpy.libs' / 'libscipy_openblas64_.so'
    original = site / 'numpy.libs' / 'libscipy_openblas64_.so'
    damaged.parent.mkdir(parents=True)
    original.parent.mkdir(parents=True)
    damaged.write_bytes(STRIPPED)
    original.write_bytes(GOOD)
    # A symlink to the damaged file must not be treated as a second copy.
    (bundle / 'libscipy_openblas64_.so').symlink_to('numpy.libs/libscipy_openblas64_.so')

    assert repair(bundle, [site]) == 0
    assert damaged.read_bytes() == GOOD
    assert find_misaligned(bundle) == []


def test_fails_when_no_original_exists(tmp_path: Path) -> None:
    bundle, site = tmp_path / '_internal', tmp_path / 'site-packages'
    bundle.mkdir()
    site.mkdir()
    (bundle / 'libbroken.so').write_bytes(STRIPPED)
    assert repair(bundle, [site]) == 1


def test_clean_bundle_is_left_untouched(tmp_path: Path) -> None:
    bundle = tmp_path / '_internal'
    bundle.mkdir()
    (bundle / 'libfine.so').write_bytes(GOOD)
    assert repair(bundle, [tmp_path]) == 0
    assert (bundle / 'libfine.so').read_bytes() == GOOD
