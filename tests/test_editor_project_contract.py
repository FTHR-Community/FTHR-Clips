"""Editor DTO compatibility, separate from library persistence and UI."""
from __future__ import annotations

import copy
from uuid import uuid4

import pytest

from core.clip_metadata_types import EditorProject


def test_project_v2_roundtrips_multiple_sources_and_unknown_fields_defensively():
    body = {'version': 2, 'sources': [str(uuid4()), str(uuid4())],
            'state': {'segments': [[0, 1, False]], 'future_effect': {'value': 7}},
            'future_project_field': {'nested': ['preserve', None]}}
    original = copy.deepcopy(body)
    project = EditorProject.from_dict(body)
    body['state']['future_effect']['value'] = 'caller mutation'
    body['future_project_field']['nested'].append('caller mutation')
    assert project.to_dict() == original
    exported = project.to_dict()
    exported['state']['segments'].clear()
    exported['future_project_field']['nested'].clear()
    assert project.to_dict() == original
    assert EditorProject.from_dict(project.to_dict()).to_dict() == original


def test_legacy_draft_adapter_preserves_file_binding_unknown_fields_and_state():
    draft = {'version': 1, 'file': {'size': 100, 'mtime_ns': 2}, 'updated_at': 10,
             'state': {'unknown_edit': [1, {'preserve': True}]},
             'future_draft_field': {'preserve': None}}
    original = copy.deepcopy(draft)
    file_id = str(uuid4())
    project = EditorProject.from_legacy_draft(draft, file_id=file_id)
    draft['state']['unknown_edit'].clear()
    assert project.version == 2
    assert project.sources == (file_id,)
    assert project.to_legacy_draft() == original
    assert EditorProject.from_dict(project.to_dict()).to_legacy_draft() == original
    assert EditorProject.from_legacy_draft(original).sources == ()


@pytest.mark.parametrize('version', [1, 3, True, '2'])
def test_project_refuses_unsupported_versions_instead_of_rewriting_them(version):
    body = {'version': version, 'sources': [], 'state': {'preserve': [1, 2]}}
    original = copy.deepcopy(body)
    with pytest.raises(ValueError):
        EditorProject.from_dict(body)
    assert body == original
