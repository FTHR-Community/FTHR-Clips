import sys
import os
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / 'FTHR_UI'))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest


@pytest.fixture
def error_bar(qtbot):
    from ui.error_bar import ErrorBar
    bar = ErrorBar()
    qtbot.addWidget(bar)
    return bar


def test_hidden_on_init(error_bar):
    assert not error_bar.isVisible()


def test_visible_after_push(error_bar):
    error_bar.push('TEST TITLE', 'Some detail.')
    assert error_bar.isVisible()


def test_dismiss_hides_when_queue_empty(error_bar):
    error_bar.push('ONLY', 'One error.')
    error_bar.dismiss_current()
    assert not error_bar.isVisible()


def test_dismiss_shows_next_when_queued(error_bar):
    error_bar.push('FIRST', 'First error.')
    error_bar.push('SECOND', 'Second error.')
    error_bar.dismiss_current()
    assert error_bar.isVisible()


def test_clear_hides_bar(error_bar):
    error_bar.push('A', 'Err A.')
    error_bar.push('B', 'Err B.')
    error_bar.clear()
    assert not error_bar.isVisible()


def test_action_fires_callback_and_dismisses(error_bar):
    fired = []
    error_bar.push('TITLE', 'Detail.',
                   actions=[('DO IT', lambda: fired.append(True))])
    error_bar._fire_action(lambda: fired.append(True))
    assert fired == [True]
    assert not error_bar.isVisible()


def test_action_fires_only_the_given_callback(error_bar):
    log = []
    error_bar.push('TITLE', 'Detail.',
                   actions=[('ACT', lambda: log.append('cb'))])
    error_bar._fire_action(lambda: log.append('direct'))
    assert 'direct' in log
    assert not error_bar.isVisible()


def test_action_does_not_dismiss_next_entry_when_callback_dismisses_current(
        error_bar):
    error_bar.push('FIRST', 'first')
    error_bar.push('SECOND', 'second')
    first = error_bar._current

    error_bar._fire_action(error_bar.dismiss_current, first)

    assert error_bar._current['title'] == 'SECOND'


def test_stale_action_from_removed_entry_does_not_run(error_bar):
    fired = []
    error_bar.push('FIRST', 'first')
    first = error_bar._current
    error_bar.push('SECOND', 'second')
    error_bar.dismiss_current()

    error_bar._fire_action(lambda: fired.append('stale'), first)

    assert fired == []
    assert error_bar._current['title'] == 'SECOND'


def test_restart_action_can_leave_recoverable_incident_visible(error_bar):
    calls = []
    error_bar.push(
        'CAPTURE FAILED', 'backend unavailable',
        actions=[('RESTART ENGINE', lambda: calls.append('restart'))],
        key='capture:4:7', dismiss_on_action=False)
    entry = error_bar._current

    error_bar._fire_action(lambda: calls.append('restart'), entry)

    assert calls == ['restart']
    assert error_bar._current is entry
    assert error_bar.isVisible()


def test_keyed_upsert_and_remove_preserve_unrelated_same_title_errors(error_bar):
    error_bar.push('CAPTURE FAILED', 'first incident', key='capture:1')
    error_bar.push('CAPTURE FAILED', 'second incident', key='capture:2')
    error_bar.push('OTHER', 'unrelated')
    error_bar.push('CAPTURE FAILED', 'updated second', key='capture:2')

    assert len(error_bar._queue) == 2
    assert error_bar._queue[0]['detail'] == 'updated second'
    assert error_bar.remove_by_key('capture:2') is True
    assert error_bar._current['key'] == 'capture:1'
    assert [entry['title'] for entry in error_bar._queue] == ['OTHER']

    error_bar.remove_by_key('capture:1')
    assert error_bar._current['title'] == 'OTHER'


def test_keyless_push_keeps_legacy_four_positional_argument_api(error_bar):
    error_bar.push('TITLE', 'detail', 'warning', [])

    assert error_bar._current['key'] is None
    assert error_bar._current['dismiss_on_action'] is True
