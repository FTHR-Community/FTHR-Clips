"""Source-checkout engine candidates; frozen apps use only their bundled engine."""
import sys
from pathlib import Path


def isolated_source_engines(workspace_root: Path) -> list[Path]:
    """Include isolated native build outputs without searching packaged apps."""
    try:
        return sorted(path for path in workspace_root.glob('build/*/engine/FTHRclips.exe')
                      if path.is_file())
    except OSError:
        return []


def find_source_engine(workspace_root: Path) -> Path | None:
    """Match the source UI's newest-build selection and tie-break order."""
    capture_root = workspace_root / 'FTHRcapture'
    direct_root = capture_root / 'FTHRclips' / 'x64'
    candidates = [
        *isolated_source_engines(workspace_root),
        *(direct_root / config / 'FTHRclips.exe' for config in (
            'HDRRelease', 'CFRReleasePause', 'CFRReleaseFinal', 'CFRRelease',
            'Release', 'Debug')),
        *(capture_root / 'x64' / config / 'FTHRClips.exe' for config in (
            'Release', 'Debug')),
        *(capture_root / config / 'FTHRClips.exe' for config in ('Release', 'Debug')),
    ]
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    try:
        return max(existing, key=lambda path: path.stat().st_mtime)
    except OSError:
        return existing[0]


if __name__ == '__main__':
    # The batch launcher runs this tiny script with redirected stdout.
    # Escaping non-representable characters keeps their path diagnostic visible
    # even if the launcher's console encoding cannot display an exact glyph.
    engine = find_source_engine(Path(__file__).resolve().parents[2])
    if engine is not None:
        sys.stdout.reconfigure(errors='backslashreplace')
        print(engine)
