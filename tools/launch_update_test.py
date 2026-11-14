"""Launch the local development app using its actual source version."""
from pathlib import Path
import runpy
import sys

UI_DIR = Path(__file__).resolve().parents[1] / 'FTHR_UI'

if __name__ == '__main__':
    sys.path.insert(0, str(UI_DIR))
    runpy.run_path(str(UI_DIR / 'main.py'), run_name='__main__')
