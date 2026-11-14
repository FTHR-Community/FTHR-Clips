"""Source-checkout engine candidates; frozen apps use only their bundled engine."""
from pathlib import Path


def isolated_source_engines(workspace_root: Path) -> list[Path]:
    """Include isolated native build outputs without searching packaged apps."""
    try:
        return sorted(path for path in workspace_root.glob('build/*/engine/FTHRclips.exe')
                      if path.is_file())
    except OSError:
        return []
