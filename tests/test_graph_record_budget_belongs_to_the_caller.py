"""A complete captured universe does not inherit a hidden graph record count."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from corpus_snapshot import canonical_captured_source
from knowledge_extractor import extract_knowledge
from project_extractor import extract_projects
from project_journal import JOURNAL_HEADER
from reliable_memory import canonical_json_bytes

from tests.test_project_extractor import _event


def _source(path: str, content: bytes):
    return canonical_captured_source(
        source_id=f"source:{path}", source_path=path,
        source_sha256=hashlib.sha256(content).hexdigest(), content=content,
    )


def _note(index: int):
    return _source(f"knowledge/notes/record-control-{index}.md", b"# Note\n")


def _project(index: int):
    slug = f"record-control-{index}"
    event = _event()
    event["project"] = slug
    event["provenance"]["session"] = f"session-{index}"
    event["evidence_event_ids"] = [f"event-{index}-a", f"event-{index}-b"]
    return _source(
        f"knowledge/projects/{slug}/journal.md",
        JOURNAL_HEADER.encode() + canonical_json_bytes(event) + b"\n",
    )


@pytest.fixture(scope="module")
def many_notes():
    return tuple(_note(index) for index in range(50001))


@pytest.mark.parametrize("budget", [None, 100002])
def test_50001_sources_preserve_every_page_and_physical_occurrence(monkeypatch, many_notes, budget):
    monkeypatch.setenv("LLM_WIKI_EXTRACTION_MAX_SOURCES", "80832")
    result = extract_knowledge(many_notes, max_records=budget)
    assert len(result.nodes) == 50001
    assert len(result.occurrences) == 50001
    assert {item["source_id"] for item in result.occurrences} == {
        source.record.logical_id for source in many_notes
    }


def test_project_records_do_not_inherit_the_same_hidden_ceiling():
    sources = tuple(_project(index) for index in range(7001))
    result = extract_projects(sources)
    count = sum(map(len, (result.nodes, result.occurrences, result.assertions, result.evidence)))
    assert count > 100000
    assert len(result.occurrences) == 7001
    assert {item["source_id"] for item in result.occurrences} == {
        source.record.logical_id for source in sources
    }


@pytest.mark.parametrize("extractor,factory", [(extract_knowledge, _note), (extract_projects, _project)])
def test_explicit_lower_budget_is_a_refusal(extractor, factory):
    with pytest.raises(ValueError, match="record ceiling"):
        extractor((factory(0),), max_records=1)


@pytest.mark.parametrize("budget", [False, True, 0, -1, 1.5, "100", float("inf")])
@pytest.mark.parametrize("extractor,factory", [(extract_knowledge, _note), (extract_projects, _project)])
def test_invalid_caller_budget_is_rejected(extractor, factory, budget):
    with pytest.raises(ValueError, match="positive"):
        extractor((factory(0),), max_records=budget)


@pytest.mark.parametrize("extractor,factory", [(extract_knowledge, _note), (extract_projects, _project)])
def test_default_budget_keeps_cancellation_and_expiry(extractor, factory):
    sources = (factory(0),)
    with pytest.raises(TimeoutError, match="deadline"):
        extractor(sources, deadline=0)
    with pytest.raises(TimeoutError, match="cancelled"):
        extractor(sources, cancelled=lambda: True)


@pytest.mark.parametrize("extractor,factory", [(extract_knowledge, _note), (extract_projects, _project)])
def test_default_budget_keeps_physical_byte_authority(extractor, factory):
    source = factory(0)
    with pytest.raises(ValueError, match="captured source"):
        extractor((replace(source, content=source.content + b"changed"),))
