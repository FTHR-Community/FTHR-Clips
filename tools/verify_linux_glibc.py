#!/usr/bin/env python3
"""Verify the glibc symbol ceiling of a Linux release bundle."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "linux_release_baseline.json"
_ELF_MAGIC = b"\x7fELF"
_GLIBC_RE = re.compile(r"\bGLIBC_(\d+(?:\.\d+)*)\b")


class VerificationError(RuntimeError):
    """A release-bundle verification failure."""


@dataclass(frozen=True, order=True)
class GlibcVersion:
    parts: tuple[int, ...]

    @classmethod
    def parse(cls, value: str) -> "GlibcVersion":
        if not re.fullmatch(r"\d+(?:\.\d+)*", value):
            raise ValueError(f"invalid glibc version: {value!r}")
        parts = [int(part) for part in value.split(".")]
        while len(parts) > 1 and parts[-1] == 0:
            parts.pop()
        return cls(tuple(parts))

    def __str__(self) -> str:
        return ".".join(str(part) for part in self.parts)


@dataclass(frozen=True)
class Manifest:
    architecture: str
    build_runner: str
    max_glibc: GlibcVersion

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VerificationError(f"could not read manifest {path}: {exc}") from exc
        try:
            architecture = str(data["architecture"])
            build_runner = str(data["build_runner"])
            max_glibc = GlibcVersion.parse(str(data["max_glibc"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise VerificationError(
                f"invalid Linux release baseline manifest {path}: {exc}"
            ) from exc
        if not architecture or not build_runner:
            raise VerificationError(f"invalid Linux release baseline manifest {path}")
        return cls(architecture, build_runner, max_glibc)


@dataclass(frozen=True)
class ElfResult:
    path: Path
    versions: tuple[GlibcVersion, ...]

    @property
    def highest(self) -> GlibcVersion | None:
        return max(self.versions) if self.versions else None


@dataclass(frozen=True)
class VerificationReport:
    manifest: Manifest
    scanned: tuple[ElfResult, ...]
    errors: tuple[str, ...]

    @property
    def highest(self) -> GlibcVersion | None:
        versions = [item.highest for item in self.scanned if item.highest is not None]
        return max(versions) if versions else None

    @property
    def offending(self) -> tuple[ElfResult, ...]:
        return tuple(
            item
            for item in self.scanned
            if item.highest is not None and item.highest > self.manifest.max_glibc
        )

    @property
    def ok(self) -> bool:
        return bool(self.scanned) and not self.errors and not self.offending


ReadelfRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_readelf(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _is_elf(path: Path) -> bool:
    try:
        with path.open("rb") as stream:
            return stream.read(4) == _ELF_MAGIC
    except OSError:
        return False


def _iter_files(targets: Iterable[Path]) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    files: list[Path] = []
    errors: list[str] = []
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()

    def visit(path: Path, explicit: bool = False) -> None:
        try:
            if not path.exists():
                if explicit:
                    errors.append(f"target does not exist: {path}")
                return
            resolved = path.resolve(strict=True)
        except OSError as exc:
            errors.append(f"could not access target {path}: {exc}")
            return

        if resolved.is_dir():
            key = os.fspath(resolved)
            if key in seen_dirs:
                return
            seen_dirs.add(key)
            try:
                children = sorted(path.iterdir(), key=lambda item: os.fspath(item))
            except OSError as exc:
                errors.append(f"could not scan directory {path}: {exc}")
                return
            for child in children:
                visit(child)
            return

        key = os.fspath(resolved)
        if key in seen_files:
            return
        seen_files.add(key)
        if resolved.is_file() and _is_elf(resolved):
            files.append(path)

    for target in targets:
        visit(target, explicit=True)
    return tuple(files), tuple(errors)


def _parse_versions(output: str) -> tuple[GlibcVersion, ...]:
    return tuple(sorted({GlibcVersion.parse(match) for match in _GLIBC_RE.findall(output)}))


def verify_targets(
    targets: Iterable[Path],
    manifest: Manifest,
    *,
    readelf_runner: ReadelfRunner = _run_readelf,
) -> VerificationReport:
    elf_files, scan_errors = _iter_files(tuple(Path(item) for item in targets))
    results: list[ElfResult] = []
    errors = list(scan_errors)
    for path in elf_files:
        command = ("readelf", "--version-info", "--wide", os.fspath(path))
        try:
            completed = readelf_runner(command)
        except OSError as exc:
            errors.append(f"readelf failed for {path}: {exc}")
            continue
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            errors.append(f"readelf failed for {path} (exit {completed.returncode}){suffix}")
            continue
        results.append(ElfResult(path, _parse_versions(completed.stdout or "")))
    if not results:
        errors.append("no ELF files found in the supplied targets")
    return VerificationReport(manifest, tuple(results), tuple(errors))


def _format_report(report: VerificationReport) -> str:
    lines = [
        f"Linux release baseline: {report.manifest.architecture} / {report.manifest.build_runner}",
        f"Maximum supported glibc: GLIBC_{report.manifest.max_glibc}",
        f"ELF files scanned: {len(report.scanned)}",
        f"Highest required glibc: {('GLIBC_' + str(report.highest)) if report.highest else 'none'}",
    ]
    for error in report.errors:
        lines.append(f"ERROR: {error}")
    for item in report.offending:
        lines.append(
            f"ERROR: {item.path} requires GLIBC_{item.highest}, "
            f"which exceeds GLIBC_{report.manifest.max_glibc}"
        )
    lines.append("RESULT: PASS" if report.ok else "RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--target",
        type=Path,
        action="append",
        required=True,
        help="ELF file/AppImage or directory to scan; may be repeated",
    )
    args = parser.parse_args(argv)
    try:
        manifest = Manifest.load(args.manifest)
        report = verify_targets(args.target, manifest)
    except VerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(_format_report(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
