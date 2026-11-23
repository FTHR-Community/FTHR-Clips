"""Build the isolated Windows Minecraft Game Capture provider from OBS 32.1.2."""
import argparse
import hashlib
from importlib.metadata import PackageNotFoundError, distribution
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROVIDER = ROOT / "FTHRcapture/game_source"
COMMIT = "fb4d98bf88fae5fc85cb11fc57f7c5e309282194"
PYSIDE_FFMPEG_SHA256 = {
    "avcodec-61.dll": "a3127f36742b58827411a561489505e38a8ccbcc6a3bc2f1ee58abfe700ced15",
    "avformat-61.dll": "bbacff3524c192eaf129dd6f838c0eee65abe56fe5109b9bede839a0823f8fa3",
    "avutil-59.dll": "615df5bb9903887b6f56a47f17fc169cd42753b9b9db7f162c74731c45612df6",
    "swresample-5.dll": "878e0fbf647dc8e6becb32dc031701b19dac8c9867192d571f0d72d02842f7ed",
    "swscale-8.dll": "2f21c52fd2eb73484b448666903c5eee1b4948d28239cb18d93428a989ed3ecc",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--obs", type=Path, default=Path(os.environ["ProgramFiles"]) / "obs-studio")
    parser.add_argument("--source", type=Path, required=True, help="OBS Studio 32.1.2 source checkout")
    parser.add_argument("--out", type=Path, required=True, help="engine/game-source staging directory")
    args = parser.parse_args()
    src, out = args.source.resolve(), args.out.resolve()
    revision = subprocess.check_output(["git", "-C", src, "rev-parse", "HEAD"], text=True).strip()
    if revision != COMMIT:
        raise SystemExit("Expected OBS Studio 32.1.2 at " + COMMIT)
    try:
        pyside = distribution("PySide6")
    except PackageNotFoundError as exc:
        raise SystemExit("Build with the project's Windows Python environment (PySide6 6.11.1).") from exc
    ffmpeg = {name: pyside.locate_file(f"PySide6/{name}") for name in PYSIDE_FFMPEG_SHA256}
    bindir = args.obs / "bin/64bit"
    plugin = args.obs / "obs-plugins/64bit/win-capture.dll"
    with tempfile.TemporaryDirectory(prefix="fthr-game-source-") as work:
        def run(*cmd):
            return subprocess.check_output(cmd, cwd=work, text=True, errors="replace",
                                           stderr=subprocess.STDOUT)

        symbols = re.findall(r"^\s*\d+\s+[A-Fa-f\d]+\s+[A-Fa-f\d]+\s+(\w+)(?: = [^\r\n]+)?\s*$",
                             run("dumpbin", "/nologo", "/exports", bindir / "obs.dll"), re.M)
        (Path(work) / "obs.def").write_text("LIBRARY obs.dll\nEXPORTS\n" + "\n".join(symbols))
        (Path(work) / "obsconfig.h").write_text("#pragma once\n#define OBS_RELEASE_CANDIDATE 0\n#define OBS_BETA 0\n")
        run("lib", "/nologo", "/machine:x64", "/def:obs.def", "/out:obs.lib")

        # Follow the imports of the shipped DLLs. The LGPL FFmpeg build already
        # used by PySide6 provides the same versioned DLL names as libobs needs.
        pending = [plugin, bindir / "obs.dll", bindir / "libobs-d3d11.dll"]
        seen = set()
        files = {}
        while pending:
            dep = pending.pop()
            if dep.name.lower() in seen:
                continue
            seen.add(dep.name.lower())
            files[dep.name] = dep
            for name in re.findall(r"^\s+([\w.-]+\.dll)\s*$",
                                   run("dumpbin", "/nologo", "/dependents", dep), re.I | re.M):
                path = ffmpeg.get(name.lower(), bindir / name)
                if path.is_file():
                    pending.append(path)
        assets = args.obs / "data/obs-plugins/win-capture"
        for path in assets.iterdir():
            if path.suffix in {".exe", ".dll", ".json"}:
                files["data/win-capture/" + path.name] = path
        files["data/win-capture/locale/en-US.ini"] = assets / "locale/en-US.ini"
        for path in (args.obs / "data/libobs").glob("*.effect"):
            files["data/libobs/" + path.name] = path
        manifest = json.loads((PROVIDER / "obs-32.1.2-sha256.json").read_text())
        manifest.update(PYSIDE_FFMPEG_SHA256)
        actual = {relative: hashlib.sha256(path.read_bytes()).hexdigest() for relative, path in files.items()}
        if actual != manifest:
            mismatched = sorted(name for name in actual.keys() | manifest.keys()
                                if actual.get(name) != manifest.get(name))
            raise SystemExit("Game Capture dependencies differ from the pinned OBS and "
                             "PySide6 runtime: " + ", ".join(mismatched))

        exe = Path(work) / "FTHRGameSource.exe"
        files[exe.name] = exe
        files["OBS-COPYING.txt"] = src / "COPYING"
        files["OBS-deps-NOTICES.txt"] = PROVIDER / "OBS-deps-NOTICES.txt"
        files["LGPL-2.1.txt"] = src / "deps/w32-pthreads/COPYING.LIB"
        files["Pthreads-Win32-COPYING.txt"] = src / "deps/w32-pthreads/COPYING"
        files["THIRD_PARTY.md"] = PROVIDER / "THIRD_PARTY.md"
        unexpected = sorted(path.relative_to(out).as_posix() for path in out.rglob("*")
                            if path.is_file() and path.relative_to(out).as_posix() not in files)
        if unexpected:
            raise SystemExit("Choose a clean Game Capture staging directory. It contains "
                             "untracked files: " + ", ".join(unexpected))

        try:
            run("cl", "/nologo", "/std:c++17", "/EHsc", "/O2", "/MD", "/utf-8",
                "/I" + str(src / "libobs"), "/I" + str(src / "shared/obs-hook-config"),
                "/I" + work, PROVIDER / "main.cpp", "obs.lib", "User32.lib", "dxgi.lib",
                "/Fe:" + str(exe))
        except subprocess.CalledProcessError as exc:
            raise SystemExit(exc.output) from exc
        for relative, path in files.items():
            dest = out / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)


if __name__ == "__main__":
    main()
