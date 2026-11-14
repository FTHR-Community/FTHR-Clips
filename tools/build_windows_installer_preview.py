"""Compile a small, non-installing native UI preview of the Windows installer.

The PreviewBuild define always blocks PrepareToInstall, including silent mode.
It requires no elevation and never installs, writes settings, or launches FTHR.
The real installer must be built separately without this define.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from build_windows_installer import find_iscc


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    output = ROOT / "build" / "guided-installer-preview"
    fixture = output / "fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    (fixture / "PREVIEW-ONLY.txt").write_text(
        "Visual preview only. Installation is disabled.\n", encoding="utf-8")
    subprocess.run([
        str(find_iscc(None)), "/Qp", "/DPreviewBuild",
        f"/DBundleDir={fixture}", f"/O{output}",
        "/FFTHRClips-Guided-Preview", "installer_windows.iss",
    ], cwd=ROOT, check=True)
    print(output / "FTHRClips-Guided-Preview.exe")


if __name__ == "__main__":
    main()
