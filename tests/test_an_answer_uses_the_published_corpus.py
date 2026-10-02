"""Answers use the verified captured corpus, then check selected live sources."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import corpus_snapshot  # noqa: E402
import query_memory  # noqa: E402
import search_memory  # noqa: E402
from doctor import _generation_source_rows  # noqa: E402
from evidence_graph_builder import build_full_generation  # noqa: E402
from generation_catalog import GenerationCatalog  # noqa: E402
from repository_scope import resolve_repository_scope  # noqa: E402


@pytest.fixture
def published(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    page = vault / "knowledge/notes/decision.md"
    page.parent.mkdir(parents=True)
    page.write_text("---\ntype: decision\nsource_authority: user\nconfidence: high\n---\n# Decision\nKeep the original evidence.\n")
    subprocess.run(["git", "init", "-q", str(vault)], check=True)
    snapshot = corpus_snapshot.collect_corpus(vault)
    state = tmp_path / "state"
    catalog = GenerationCatalog(state)
    build_full_generation(catalog, sources=_generation_source_rows(snapshot),
        source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
        nodes=[], occurrences=[], assertions=[], evidence=[], observations=[], dependencies=[],
        generation_id="gen-answer", snapshot=snapshot, publication_root=vault,
        repository_scope=resolve_repository_scope(vault))
    monkeypatch.setattr(search_memory, "STATE_ROOT", state)
    return vault, snapshot, catalog


def test_answer_uses_published_bytes_without_recapturing_every_live_source(published, monkeypatch):
    vault, original, _catalog = published
    monkeypatch.setattr(corpus_snapshot, "collect_corpus", lambda *a, **k: pytest.fail("live corpus was collected"))
    actual = query_memory._answer_corpus(vault, time.monotonic() + 30)
    assert actual.sources == original.sources
    assert actual.chunks == original.chunks
    assert actual.corpus_sha256 == original.corpus_sha256


def test_selected_source_changed_after_publication_is_still_rejected(published):
    vault, original, _catalog = published
    (vault / "knowledge/notes/decision.md").write_text("# Changed\nDifferent evidence.\n")
    actual = query_memory._answer_corpus(vault, time.monotonic() + 30)
    assert actual.sources == original.sources
    assert not query_memory._source_is_unchanged(actual.sources[0], vault)


def test_answer_without_a_generation_still_reads_live_markdown(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    page = vault / "knowledge/notes/page.md"
    page.parent.mkdir(parents=True)
    page.write_text("# Page\nLive evidence.\n")
    monkeypatch.setattr(search_memory, "STATE_ROOT", tmp_path / "state")
    actual = query_memory._answer_corpus(vault, time.monotonic() + 30)
    assert actual.sources[0].content == page.read_bytes()



def test_published_chunk_cannot_override_markdown_authority(published):
    _vault, original, _catalog = published
    row = list(search_memory._generation_chunk_row(original.chunks[0], 0))
    row[12:19] = ["untrusted-cache-type", None, "untrusted-cache-authority", "low", "superseded", None, None]
    actual = search_memory._published_chunk(row, original.sources[0], {"extractor_version": original.extractor_version})
    assert actual.type == original.sources[0].metadata.type
    assert actual.authority == original.sources[0].metadata.authority
    assert actual.confidence == original.sources[0].metadata.confidence
    assert actual.status == original.sources[0].metadata.status


@pytest.mark.parametrize("column,value", [(21, "forged text"), (8, 1000000)])
def test_published_chunk_must_match_actual_source_bytes(published, column, value):
    _vault, original, _catalog = published
    row = list(search_memory._generation_chunk_row(original.chunks[0], 0))
    row[column] = value
    with pytest.raises(ValueError, match="published chunk"):
        search_memory._published_chunk(row, original.sources[0], {"extractor_version": original.extractor_version})


def test_generation_changed_during_read_is_not_a_successful_live_fallback(published, monkeypatch):
    vault, _original, _catalog = published
    monkeypatch.setattr(search_memory, "_generation_consumption_unchanged", lambda *a, **k: False)
    with pytest.raises(corpus_snapshot.CorpusChanged, match="published corpus changed"):
        query_memory._answer_corpus(vault, time.monotonic() + 30)


def test_published_corpus_obeys_the_callers_expired_deadline(published):
    vault, _original, _catalog = published
    with pytest.raises(TimeoutError):
        query_memory._answer_corpus(vault, time.monotonic() - 1)



@pytest.mark.parametrize("link", ["source", "ancestor"])
def test_selected_source_rejects_a_symlink_even_when_bytes_match(published, tmp_path, link):
    vault, original, _catalog = published
    source = vault / original.sources[0].record.relative_path
    if link == "source":
        copy = tmp_path / "outside.md"
        copy.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(copy)
    else:
        parent = source.parent
        outside = tmp_path / "outside"
        parent.rename(outside)
        parent.symlink_to(outside, target_is_directory=True)
    assert not query_memory._source_is_unchanged(original.sources[0], vault)


def test_selected_source_does_not_read_an_unbounded_replacement(published, monkeypatch):
    vault, original, _catalog = published
    target = vault / original.sources[0].record.relative_path
    target.write_bytes(original.sources[0].content + b"changed")
    reader = Path.read_bytes
    def forbid_unbounded(path):
        if path == target:
            pytest.fail("changed selected source used an unbounded read")
        return reader(path)
    monkeypatch.setattr(Path, "read_bytes", forbid_unbounded)
    assert not query_memory._source_is_unchanged(original.sources[0], vault)



def test_selected_source_obeys_the_answer_deadline(published):
    vault, original, _catalog = published
    with pytest.raises(TimeoutError):
        query_memory._source_is_unchanged(original.sources[0], vault, deadline=time.monotonic() - 1)
