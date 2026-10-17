"""Render installer artwork from the current app assets at common Windows DPIs."""
from pathlib import Path
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'installer_assets' / 'polished'
SIZES = (54, 68, 81, 95, 108, 122, 135, 162, 216)


def main():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    font_id = QFontDatabase.addApplicationFont(str(ROOT / 'FTHR_UI/assets/fonts/Oswald-Bold.ttf'))
    if font_id < 0:
        raise RuntimeError('Cannot load the application display font')
    family = QFontDatabase.applicationFontFamilies(font_id)[0]
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        for state, color in [('active', '#D9DCD9'), ('done', '#A8B2AB'), ('pending', '#747E77')]:
            for step in range(1, 4):
                # Supersample, then filter to the actual display size. This retains
                # partial edge coverage instead of GDI's faceted ellipse outline.
                image = QImage(size * 4, size * 4, QImage.Format.Format_ARGB32_Premultiplied)
                image.fill(QColor('#131C17'))
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(QRectF(2, 2, size * 4 - 4, size * 4 - 4))
                font = QFont(family)
                font.setPixelSize(round(size * 4 * 32 / 54))
                font.setBold(True)
                painter.setFont(font)
                painter.setPen(QColor('#131C17'))
                bounds = QFontMetricsF(font).tightBoundingRect(str(step))
                painter.drawText((size * 4 - bounds.width()) / 2 - bounds.left(),
                                 (size * 4 - bounds.height()) / 2 - bounds.top(), str(step))
                painter.end()
                image = image.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
                if not image.save(str(OUTPUT / f'step-{step}-{state}-{size}.png')):
                    raise RuntimeError('Cannot save step artwork')
    renderer = QSvgRenderer(str(ROOT / 'FTHR_UI/assets/icons/close.svg'))
    if not renderer.isValid():
        raise RuntimeError('Cannot load the current application close icon')
    image = QImage(128, 128, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor('#131C17'))
    painter = QPainter(image)
    natural = renderer.defaultSize()
    fitted = natural.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio)
    renderer.render(painter, QRectF((128 - fitted.width()) / 2, (128 - fitted.height()) / 2,
                                   fitted.width(), fitted.height()))
    painter.end()
    if not image.save(str(OUTPUT / 'close-current.png')):
        raise RuntimeError('Cannot save close artwork')
    print(f'Rendered installer artwork from current app assets: {OUTPUT}')


if __name__ == '__main__':
    main()
