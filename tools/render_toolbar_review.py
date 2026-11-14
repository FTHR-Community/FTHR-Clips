"""Render toolbar popup pairs using isolated settings, without starting capture."""
import os
import sys
from pathlib import Path
from runpy import run_path
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'FTHR_UI'))
sys.path.insert(0, str(ROOT))
from PySide6.QtCore import QPoint
from PySide6.QtGui import QPixmap, QPainter, QColor
from PySide6.QtWidgets import QApplication, QWidget
from PySide6.QtTest import QTest
import main
from ui.app_style import apply_app_style
app = QApplication([])
main._load_fonts()
apply_app_style(app)
settings = run_path(str(ROOT / 'tests/test_main_popup_layout.py'))['_SettingsStub']()
manager = run_path(str(ROOT / 'tests/test_hotkey_visibility_persistence.py'))['_PopupHotkeyManager']()
out = ROOT / 'ui-preview/toolbar-pairs'
out.mkdir(parents=True, exist_ok=True)
for name, panel in [('quality', main.CaptureSettingsPopup(settings)),
                    ('source', main.SourcePopup(settings)),
                    ('games', main.GameDetectionPopup(settings, manager)),
                    ('hotkeys', main.HotkeyPopup(manager))]:
    host = QWidget()
    host.resize(650, 760)
    host.setStyleSheet('background: #141619;')
    button = main.TopBarButton(name.capitalize(), host)
    button.move(24, 24)
    button.bindPopup(panel)
    host.show()
    panel.show_below(button)
    QTest.qWait(60)
    canvas = QPixmap(host.size())
    canvas.fill(QColor('#141619'))
    painter = QPainter(canvas)
    painter.drawPixmap(0, 0, host.grab())
    painter.drawPixmap(host.mapFromGlobal(panel.mapToGlobal(QPoint())), panel.grab())
    painter.end()
    canvas.save(str(out / (name + '.png')))
    print('edges', button.mapToGlobal(QPoint()).x(), panel.mapToGlobal(QPoint()).x(), panel.pos().x(), panel.frameGeometry().x())
    print(name, 'button', button.width(), 'panel', panel.width(), 'height', panel.height())
    panel.close()
    host.close()
