"""The real page is visible immediately and keeps its controls and state."""

import pytest
from PySide6.QtCore import QAbstractAnimation
from PySide6.QtWidgets import QLineEdit, QStackedWidget, QWidget

from main import SlidingStackedWidget


@pytest.mark.parametrize('stack_type', [QStackedWidget, SlidingStackedWidget])
def test_navigation_is_immediate_and_preserves_edit_state(qtbot, monkeypatch, stack_type):
    stack = stack_type()
    qtbot.addWidget(stack)
    pages = [QWidget() for _ in range(3)]
    fields = [QLineEdit('Keep me', page) for page in pages]
    for page in pages:
        stack.addWidget(page)
        monkeypatch.setattr(page, 'grab', lambda: pytest.fail('page was snapshotted'))
    monkeypatch.setattr(stack, 'grab', lambda: pytest.fail('stack was snapshotted'))
    stack.resize(400, 300)
    stack.show()
    for index in (1, 2, 0):
        stack.setCurrentIndex(index)
        assert stack.currentWidget() is pages[index]
        assert pages[index].isVisible()
        assert pages[index].geometry() == stack.rect()
        assert all(page.isHidden() for i, page in enumerate(pages) if i != index)
    stack.resize(500, 350)
    stack.hide()
    stack.show()
    assert pages[0].geometry() == stack.rect()
    assert not stack.findChildren(QAbstractAnimation)
    fields[0].setFocus()
    qtbot.keyClicks(fields[0], '!')
    assert fields[0].text() == 'Keep me!'
    assert [field.text() for field in fields[1:]] == ['Keep me', 'Keep me']
