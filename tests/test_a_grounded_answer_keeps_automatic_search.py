"""Automatic grounded retrieval must keep the planner's semantic lanes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import query_memory  # noqa: E402
import retrieval  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

from tests.test_grounded_qa import _answer_for_prompt, _write_page  # noqa: E402


@pytest.mark.parametrize("question,profile,expected", [
    ("при каких условиях включается альфа", None, ("lexical", "dense")),
    ("как альфа связана с другими понятиями", None, ("lexical", "dense", "graph")),
    ("при каких условиях включается альфа", "base", ("lexical",)),
    ("как альфа связана с другими понятиями", "graph", ("lexical", "graph")),
    ("при каких условиях включается альфа", "hybrid", ("lexical", "dense")),
])
def test_grounded_retrieval_preserves_automatic_or_explicit_signals(
    tmp_path, monkeypatch, question, profile, expected,
):
    vault = tmp_path / "vault"
    _write_page(vault, "alpha.md", "Alpha is enabled.")
    snapshot = collect_corpus(vault)
    seen = []
    monkeypatch.setattr(query_memory, "_sentence_encoder", lambda: None)

    def backend(query, **options):
        _chosen, signals = retrieval.planned_request(options["profile"],
            retrieval.analyze_query(query), semantic=options["semantic"])
        seen.append(signals)
        return snapshot.chunks

    monkeypatch.setattr(retrieval, "retrieve_via_search_memory", backend)
    result = query_memory.grounded_qa(question, vault=vault, snapshot=snapshot,
        profile=profile, generator=lambda prompt, *args: _answer_for_prompt(prompt))
    assert result["status"] == "answered"
    assert seen == [expected]
