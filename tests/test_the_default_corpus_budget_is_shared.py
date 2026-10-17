"""Physical source count alone does not exhaust the wider existing walk budget."""
import time

import pytest
from corpus_snapshot import CorpusCapacityExceeded, collect_corpus
from knowledge_extractor import extract_knowledge

from tests.slow_machine import LONG_TIMEOUT


@pytest.fixture(scope="module")
def larger_corpus(tmp_path_factory):
    root = tmp_path_factory.mktemp("larger-default-corpus")
    notes = root / "knowledge" / "notes"
    notes.mkdir(parents=True)
    for index in range(10_001):
        (notes / f"page-{index}.md").write_bytes(b"---\ntype: concept\n---\n# A page\nA fact.\n")
    snapshot = collect_corpus(root, max_files=10_001, deadline_seconds=LONG_TIMEOUT)
    return root, snapshot


def test_default_collection_accepts_a_corpus_with_more_than_ten_thousand_sources(larger_corpus):
    root, _snapshot = larger_corpus
    snapshot = collect_corpus(root, deadline_seconds=LONG_TIMEOUT)
    assert len(snapshot.sources) == 10_001
    assert len({source.record.relative_path for source in snapshot.sources}) == 10_001


def test_default_extraction_accepts_every_admitted_source(larger_corpus, monkeypatch):
    root, snapshot = larger_corpus
    import memory_state

    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    result = extract_knowledge(snapshot.sources, deadline=time.monotonic() + LONG_TIMEOUT)
    observed = {occurrence["source_id"] for occurrence in result.occurrences}
    assert observed == {source.record.logical_id for source in snapshot.sources}


def test_explicit_lower_file_and_extraction_limits_still_refuse(larger_corpus, monkeypatch):
    root, snapshot = larger_corpus
    (root / "llm-wiki.toml").write_bytes(b"[corpus]\nmax_files = 1\n[extraction]\nmax_sources = 1\n")
    import memory_state

    monkeypatch.setattr(memory_state, "ROOT", root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    with pytest.raises(CorpusCapacityExceeded, match="corpus file limit exceeded"):
        collect_corpus(root, max_entries=snapshot.policy.max_entries, deadline_seconds=LONG_TIMEOUT)
    with pytest.raises(ValueError, match="knowledge extraction source ceiling exceeded"):
        extract_knowledge(snapshot.sources[:2], deadline=time.monotonic() + LONG_TIMEOUT)
