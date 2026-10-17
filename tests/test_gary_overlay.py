from pathlib import Path

from PySide6.QtGui import QColor, QPixmap

from ui import gary_overlay


def test_cat_has_one_in_ten_chance_per_appearance(qtbot, monkeypatch, tmp_path):
    default = tmp_path / 'gary.png'
    cat = tmp_path / 'gatze.png'
    for path, color in ((default, 'blue'), (cat, 'red')):
        pixmap = QPixmap(20, 20)
        pixmap.fill(QColor(color))
        pixmap.save(str(path))
    rolls = iter(range(10))
    calls = []

    def roll(n):
        calls.append(n)
        return next(rolls)

    monkeypatch.setattr(gary_overlay.random, 'randrange', roll)
    overlay = gary_overlay.GaryOverlay(default)
    qtbot.addWidget(overlay)
    shown = []
    for _ in range(10):
        overlay.set_intensity(.1)
        selected = overlay.image_path
        shown.append(selected)
        for intensity in (.5, 1., .3, .01):
            overlay.set_intensity(intensity)
            assert overlay.image_path == selected
        overlay.set_intensity(0)
    assert shown.count(str(cat)) == 1
    assert shown.count(str(default)) == 9
    assert calls == [10] * 10


def test_missing_cat_falls_back_to_configured_image(qtbot, monkeypatch, tmp_path):
    custom = tmp_path / 'custom.png'
    pixmap = QPixmap(20, 20)
    pixmap.fill(QColor('green'))
    pixmap.save(str(custom))
    monkeypatch.setattr(gary_overlay.random, 'randrange', lambda _: 0)
    overlay = gary_overlay.GaryOverlay(tmp_path / 'gary.png')
    qtbot.addWidget(overlay)
    assert overlay.set_image(str(custom))
    overlay.set_intensity(.5)
    assert overlay.image_path == str(custom)
    assert not overlay._pixmap.isNull()


def test_bundled_cat_image_is_available_and_transparent(qapp):
    cat = Path(gary_overlay.__file__).parents[1] / 'assets' / 'gatze.png'
    pixmap = QPixmap(str(cat))
    assert not pixmap.isNull()
    assert pixmap.hasAlphaChannel()
