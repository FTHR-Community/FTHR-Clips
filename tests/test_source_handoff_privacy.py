"""Internal continuation notes must not enter a colleague/public source export."""
import package_source


def test_continuation_note_is_local_but_project_docs_and_notices_remain(tmp_path, monkeypatch):
    for relative in ('docs/CODEX-CONTINUATION.md', 'docs/USER-GUIDE.md',
                     'README.md', 'THIRD_PARTY_NOTICES.md', 'licenses/NOTICE.txt'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('Owned test fixture', encoding='utf-8')
    monkeypatch.setattr(package_source, 'ROOT', tmp_path)
    selected = {path.relative_to(tmp_path).as_posix() for path in package_source.source_paths()}
    assert 'docs/CODEX-CONTINUATION.md' not in selected
    assert selected == {'docs/USER-GUIDE.md', 'README.md',
                        'THIRD_PARTY_NOTICES.md', 'licenses/NOTICE.txt'}
