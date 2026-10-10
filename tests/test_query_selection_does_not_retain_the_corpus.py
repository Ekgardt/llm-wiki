"""A shortlist must not retain unrelated corpus chunk objects."""

import sys
import time
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import query_memory  # noqa: E402


class _Chunk:
    def __init__(self, number):
        self.id = str(number)
        self.source_path = f"page-{number}"
        self.parent_page = self.source_path
        self.heading_ancestry = ()
        self.byte_start = 0
        self.byte_end = 1


class _Chunks:
    def __iter__(self):
        refs = []
        for number in range(100):
            assert sum(ref() is not None for ref in refs) <= 2
            chunk = _Chunk(number)
            refs.append(weakref.ref(chunk))
            yield chunk


def test_id_shortlist_does_not_keep_unrelated_chunks():
    snapshot = SimpleNamespace(chunks=_Chunks())
    selected = query_memory._matching_chunks(snapshot, [{"chunk_id": "0"}])
    assert tuple(chunk.id for chunk in selected) == ("0",)


class _CitedChunks(_Chunks):
    def __iter__(self):
        for chunk in super().__iter__():
            chunk.source_path = "page"
            chunk.byte_start = int(chunk.id) * 2
            chunk.byte_end = chunk.byte_start + 2
            yield chunk


def test_cited_position_does_not_keep_every_chunk_of_the_page():
    snapshot = SimpleNamespace(chunks=_CitedChunks())
    selected = query_memory._matching_chunks(
        snapshot, [{"path": "page", "cited": True, "byte_start": 199}]
    )
    assert tuple(chunk.id for chunk in selected) == ("99",)


def test_cached_full_selection_does_not_expand_chunks_before_consumption(monkeypatch, tmp_path):
    snapshot = SimpleNamespace(sources=(), chunks=_Chunks())
    monkeypatch.setattr(query_memory, "_cached_full_index", lambda vault: b"")
    index, chunks = query_memory._cached_full_selection(snapshot, tmp_path)
    assert index == ""
    assert sum(1 for _chunk in chunks) == 100


def test_sibling_selection_does_not_keep_unrelated_entries():
    snapshot = SimpleNamespace(chunks=_Chunks())
    selected = (_Chunk(0),)
    assert query_memory._with_entry_siblings(snapshot, selected, whole=0, partner=False) == selected


def test_context_shedding_does_not_keep_unrequested_pages(monkeypatch):
    snapshot = SimpleNamespace(chunks=_Chunks())
    selected = (_Chunk(0),)

    def compiled(snapshot, chosen, budget, pages, deadline):
        assert set(pages) == {"page-0"}
        return ("page-0",), (), None

    monkeypatch.setattr(query_memory, "_compiled_for", compiled)
    assert query_memory._fitted_selection(snapshot, selected, object())[0] == ("page-0",)


class _SourceThatMustNotBeOpened:
    def __iter__(self):
        raise AssertionError("expired caller must not materialize a source")


def test_grounded_answer_stops_before_source_selection(tmp_path):
    snapshot = SimpleNamespace(chunks=_SourceThatMustNotBeOpened())
    with pytest.raises(TimeoutError, match="deadline"):
        query_memory.build_grounded_context(
            snapshot, [{"chunk_id": "wanted"}], vault=tmp_path,
            profile="BASE", deadline=time.monotonic() - 1,
        )
