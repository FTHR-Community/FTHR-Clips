"""Preview a local HTML or URL keyboard overlay with Qt WebEngine.

Run from the source checkout with .venv/Scripts/python tools/preview_keyboard_browser_source.py.
With no source, the preview shows WASD and Space while the window has focus.
Pass --snapshot output.png to capture a transparent frame without opening a window.
This is a stand-alone feasibility tool. FTHR's recorder does not import WebEngine.
"""
import argparse
import os
from pathlib import Path
import sys


_EXAMPLE = """<!doctype html><meta charset="utf-8"><style>
* { box-sizing: border-box }
html,body { margin:0; background:transparent; overflow:hidden }
body { display:flex; align-items:center; gap:8px; padding:16px; font: bold 20px system-ui }
kbd { display:grid; place-items:center; min-width:48px; height:44px; padding:0 12px;
      font:inherit; background:#202527; color:white; border:2px solid #596162; border-radius:6px }
kbd.down { background:#00eba0; color:#152621; border-color:#a1ffdd }
</style><kbd data-key="KeyW">W</kbd><kbd data-key="KeyA">A</kbd>
<kbd data-key="KeyS">S</kbd><kbd data-key="KeyD">D</kbd><kbd data-key="Space">Space</kbd>
<script>const keys=[...document.querySelectorAll('kbd')];
for(const type of ['keydown','keyup']) addEventListener(type,e=>{
  const key=keys.find(k=>k.dataset.key===e.code); if(key){
    key.classList.toggle('down',type==='keydown');e.preventDefault();
  }
});addEventListener('blur',()=>keys.forEach(k=>k.classList.remove('down')));</script>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', nargs='?', help='HTML file or HTTP(S) URL')
    parser.add_argument('--width', type=int, default=420)
    parser.add_argument('--height', type=int, default=100)
    parser.add_argument('--snapshot', type=Path, help='Write one transparent PNG and exit')
    args = parser.parse_args()
    if not (32 <= args.width <= 1920 and 32 <= args.height <= 1080):
        parser.error('Use a viewport between 32×32 and 1920×1080.')
    if args.snapshot:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        # Hidden GPU-backed WebEngine surfaces may return an all-transparent
        # grab on Windows. CPU compositing is sufficient for this small scene.
        os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = '--disable-gpu --disable-gpu-compositing'

    from PySide6.QtCore import Qt, QTimer, QUrl
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication([sys.argv[0]])
    view = QWebEngineView()
    view.resize(args.width, args.height)
    view.setWindowTitle('Keyboard visualizer · Browser source preview')
    view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    if args.snapshot:
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    profile = QWebEngineProfile(view)  # An off-the-record profile.
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    page.setAudioMuted(True)
    page.setBackgroundColor(QColor(0, 0, 0, 0))

    def capture():
        if view.grab().toImage().save(str(args.snapshot), 'PNG'):
            app.exit(0)
        else:
            print(f'Could not write {args.snapshot}', file=sys.stderr)
            app.exit(1)

    def loaded(ok):
        if not ok:
            print('The browser source could not be loaded.', file=sys.stderr)
            app.exit(1)
        elif args.snapshot:
            QTimer.singleShot(500, capture)
        else:
            view.setFocus()

    page.loadFinished.connect(loaded)
    view.show()
    if not args.source:
        page.setHtml(_EXAMPLE)
    elif args.source.lower().startswith(('http://', 'https://')):
        page.load(QUrl(args.source))
    else:
        source = Path(args.source).expanduser().resolve()
        if not source.is_file():
            parser.error('Choose an existing HTML file or an HTTP(S) URL.')
        page.load(QUrl.fromLocalFile(str(source)))
    if args.snapshot:
        QTimer.singleShot(15000, lambda: app.exit(1))
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
