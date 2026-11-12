"""Containment refusals and archive fallback preserve failure semantics."""
import pytest
from llm_wiki import breadcrumb_reader as reader

from tests.modules.breadcrumb_reader.contract_helpers import _disk_source


@pytest.mark.parametrize('relative', ['notes/a.md', 'knowledge/raw/sessions/../a.md', '/knowledge/raw/sessions/a.md'])
def test_non_session_or_non_relative_path_is_refused(tmp_path, relative):
    with pytest.raises(ValueError):
        reader.source_target(tmp_path, relative)
    assert not list(tmp_path.iterdir())


def test_target_resolution_does_not_create_parent(tmp_path):
    relative = 'knowledge/raw/sessions/2026-11-04/event.md'
    assert reader.source_target(tmp_path, relative) == tmp_path / relative
    assert not list(tmp_path.iterdir())


def test_corrupted_current_head_does_not_fall_back_to_valid_archive(tmp_path):
    head = _disk_source(tmp_path, b'whole evidence', archived=True)
    current = tmp_path / head
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_bytes(b'corrupt head')
    with pytest.raises(ValueError):
        reader.read_permanent_source(tmp_path, head)


def test_permission_failure_does_not_attempt_archive(tmp_path, monkeypatch):
    attempted = []

    def refuse(vault, relative, *, deadline=None):
        attempted.append((vault, relative, deadline))
        raise PermissionError('denied')

    monkeypatch.setattr(reader, 'read_exact_source_document', refuse)
    with pytest.raises(PermissionError, match='denied'):
        reader.read_source_document(tmp_path, 'current', deadline=42.0)
    assert attempted == [(tmp_path, 'current', 42.0)]
