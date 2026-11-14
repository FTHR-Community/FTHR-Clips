from __future__ import annotations

from pathlib import Path

from core.error_codes import (
    APP_FAILURE_CODES,
    APP_FAILURE_CODE_BY_TITLE,
    format_error_title,
)


ROOT = Path(__file__).resolve().parents[1]


def test_every_bottom_bar_failure_title_has_one_stable_code():
    import main

    assert set(main._ERROR_BAR_FAILURE_TITLES) == set(APP_FAILURE_CODE_BY_TITLE)
    assert len(APP_FAILURE_CODES) == len(APP_FAILURE_CODE_BY_TITLE) == 23
    assert len({item.code for item in APP_FAILURE_CODES}) == 23
    assert [item.code for item in APP_FAILURE_CODES] == [
        f'Error {index:03d}' for index in range(1, 24)]


def test_error_bar_title_format_includes_code():
    assert format_error_title('clip save failed') == (
        '[Error 014] CLIP SAVE FAILED')
    assert format_error_title('routine notice') == 'ROUTINE NOTICE'
