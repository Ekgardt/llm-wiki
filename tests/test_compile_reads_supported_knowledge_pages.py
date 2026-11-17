"""Compilation must read the same note size that publication permits."""
import hashlib
import zipfile

import compile_controller as controller
import pytest
from bounded_io import MAX_KNOWLEDGE_PAGE_BYTES
from llm_wiki import compile_pipeline_sources

from tests.compile_ceiling_counts_only_compile_inputs_helpers import _bind_compiler


@pytest.mark.parametrize('size', [4 * 1024 * 1024 + 1, MAX_KNOWLEDGE_PAGE_BYTES])
def test_streamed_capture_preserves_supported_note_whole(tmp_path, monkeypatch, size):
    _bind_compiler(monkeypatch, tmp_path)
    note = tmp_path / 'knowledge/notes/supported.md'
    note.parent.mkdir(parents=True)
    content = b'x' * size
    note.write_bytes(content)
    records = list(compile_pipeline_sources.iter_compile_records([]))
    assert [group for group, _ in records] == ['sources', 'targets']
    for _, record in records:
        assert record.logical_path == 'knowledge/notes/supported.md'
        assert record.content == content
        assert record.sha256 == hashlib.sha256(content).hexdigest()
    capture = tmp_path / 'capture.zip'
    with capture.open('wb') as output:
        digest = controller.write_compile_capture(output, [])
    with capture.open('rb') as saved:
        controller.validate_capture_archive(digest, None, saved)
    with zipfile.ZipFile(capture) as archive:
        reader = controller.CompileInputArchive(archive, digest)
        restored = list(reader.records('targets'))
        assert len(restored) == 1
        assert restored[0].content == content
        assert restored[0].sha256 == hashlib.sha256(content).hexdigest()


def test_oversized_note_is_refused_without_partial_capture(tmp_path, monkeypatch):
    _bind_compiler(monkeypatch, tmp_path)
    note = tmp_path / 'knowledge/notes/oversized.md'
    note.parent.mkdir(parents=True)
    note.write_bytes(b'x' * (MAX_KNOWLEDGE_PAGE_BYTES + 1))
    records = compile_pipeline_sources.iter_compile_records([])
    with pytest.raises(ValueError, match='knowledge page'):
        next(records)
