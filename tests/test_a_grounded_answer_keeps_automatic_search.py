"""Automatic grounded retrieval must keep the planner's semantic lanes."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import query_memory  # noqa: E402
import retrieval  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

from tests.grounded_qa_helpers import _answer_for_prompt, _write_page  # noqa: E402


@pytest.mark.parametrize("question,profile,expected", [
    ("\u043f\u0440\u0438 \u043a\u0430\u043a\u0438\u0445 \u0443\u0441\u043b\u043e\u0432\u0438\u044f\u0445 \u0432\u043a\u043b\u044e\u0447\u0430\u0435\u0442\u0441\u044f \u0430\u043b\u044c\u0444\u0430", None, ("lexical", "dense")),
    ("\u043a\u0430\u043a \u0430\u043b\u044c\u0444\u0430 \u0441\u0432\u044f\u0437\u0430\u043d\u0430 \u0441 \u0434\u0440\u0443\u0433\u0438\u043c\u0438 \u043f\u043e\u043d\u044f\u0442\u0438\u044f\u043c\u0438", None, ("lexical", "dense", "graph")),
    ("\u043f\u0440\u0438 \u043a\u0430\u043a\u0438\u0445 \u0443\u0441\u043b\u043e\u0432\u0438\u044f\u0445 \u0432\u043a\u043b\u044e\u0447\u0430\u0435\u0442\u0441\u044f \u0430\u043b\u044c\u0444\u0430", "base", ("lexical",)),
    ("\u043a\u0430\u043a \u0430\u043b\u044c\u0444\u0430 \u0441\u0432\u044f\u0437\u0430\u043d\u0430 \u0441 \u0434\u0440\u0443\u0433\u0438\u043c\u0438 \u043f\u043e\u043d\u044f\u0442\u0438\u044f\u043c\u0438", "graph", ("lexical", "graph")),
    ("\u043f\u0440\u0438 \u043a\u0430\u043a\u0438\u0445 \u0443\u0441\u043b\u043e\u0432\u0438\u044f\u0445 \u0432\u043a\u043b\u044e\u0447\u0430\u0435\u0442\u0441\u044f \u0430\u043b\u044c\u0444\u0430", "hybrid", ("lexical", "dense")),
])
def test_grounded_retrieval_preserves_automatic_or_explicit_signals(
    tmp_path, monkeypatch, question, profile, expected,
):
    vault = tmp_path / "vault"
    _write_page(vault, "alpha.md", "Alpha is enabled.")
    snapshot = collect_corpus(vault)
    seen = []
    monkeypatch.setattr(query_memory, "sentence_encoder", lambda: None)

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
