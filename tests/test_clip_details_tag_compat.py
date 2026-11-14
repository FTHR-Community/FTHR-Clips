"""Phase-A adapter checks for partial legacy editor detail flushes."""
from __future__ import annotations

from types import MethodType, SimpleNamespace

import pytest
from PySide6.QtWidgets import QLineEdit

from ui.clip_viewer import ClipViewer


def _viewer(qtbot, *, tag_modified=False, description_modified=False, save=None):
    tag = QLineEdit("legacy, tag")
    description = QLineEdit("edited description")
    qtbot.addWidget(tag)
    qtbot.addWidget(description)
    tag.setModified(tag_modified)
    description.setModified(description_modified)
    manager = SimpleNamespace(set=save)
    viewer = SimpleNamespace(
        _mm=manager,
        clip_path="synthetic/clip.mp4",
        _tag_edit=tag,
        _desc_edit=description,
    )
    viewer._flush_clip_details = MethodType(ClipViewer._flush_clip_details, viewer)
    return viewer


def test_description_only_flush_does_not_rewrite_legacy_tag(qtbot):
    calls = []
    viewer = _viewer(qtbot, description_modified=True,
                     save=lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    viewer._flush_clip_details()

    assert calls == [(("synthetic/clip.mp4",),
                      {"tag": None, "description": "edited description"})]
    assert not viewer._desc_edit.isModified()
    assert not viewer._tag_edit.isModified()


def test_tag_only_flush_does_not_overwrite_description(qtbot):
    calls = []
    viewer = _viewer(qtbot, tag_modified=True,
                     save=lambda *args, **kwargs: calls.append((args, kwargs)) or True)

    viewer._flush_clip_details()

    assert calls == [(("synthetic/clip.mp4",),
                      {"tag": "legacy, tag", "description": None})]
    assert not viewer._tag_edit.isModified()
    assert not viewer._desc_edit.isModified()


def test_detail_save_error_keeps_all_modified_editor_fields(qtbot):
    def fail(*_args, **_kwargs):
        raise OSError("synthetic persistence failure")

    viewer = _viewer(qtbot, tag_modified=True, description_modified=True, save=fail)

    with pytest.raises(OSError, match="synthetic persistence failure"):
        viewer._flush_clip_details()

    assert viewer._tag_edit.isModified()
    assert viewer._desc_edit.isModified()
