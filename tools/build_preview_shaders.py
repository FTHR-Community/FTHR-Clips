"""Rebuild the bundled Qt Quick shader for D3D, Vulkan, Metal and OpenGL."""

from pathlib import Path
import subprocess

import PySide6


def main():
    qt_dir = Path(PySide6.__file__).parent
    qsb = qt_dir / 'qsb.exe'
    if not qsb.exists():
        qsb = qt_dir / 'qsb'
    qml = Path(__file__).resolve().parents[1] / 'FTHR_UI' / 'ui' / 'qml'
    subprocess.run([
        str(qsb), '--glsl', '100 es,120,150', '--hlsl', '50', '--msl', '12',
        '-o', str(qml / 'color.frag.qsb'), str(qml / 'color.frag'),
    ], check=True)


if __name__ == '__main__':
    main()
