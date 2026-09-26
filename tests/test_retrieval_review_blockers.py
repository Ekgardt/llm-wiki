"""Review-blocker regressions for Task 11–13 retrieval contract."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCHEMAS = SCRIPTS / "schemas"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _hit(candidate_id: str, path: str, score: float, **extra):
    row = {
        "candidate_id": candidate_id,
        "parent_id": path,
        "relative_path": path,
        "path": path,
        "heading_path": (),
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": score,
        "title": Path(path).stem.title(),
        "summary": f"body for {candidate_id}",
    }
    row.update(extra)
    return row


def test_corrupt_generation_still_goes_through_retrieve(tmp_path, monkeypatch):
    import retrieval
    import search_memory

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nCorrupt needle.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)

    class Catalog:
        generations_path = tmp_path / "generations"

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return None

    seen = {"retrieve": 0}
    real = retrieval.retrieve

    def wrap(*a, **k):
        seen["retrieve"] += 1
        return real(*a, **k)

    monkeypatch.setattr(retrieval, "retrieve", wrap)

    results = search_memory.search(
        "corrupt needle",
        catalog=Catalog(),
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert seen["retrieve"] == 1
    assert results
    assert results[0]["fallback_reason"] in {
        "generation_unavailable",
        "generation_corrupt",
        "generation_seal_invalid",
    }
    assert results[0]["effective_mode"] in {"BASE", "EXACT"}
    assert "lexical" in results[0]["signals_used"]


def test_seal_change_refuses_dense_and_falls_back_base(tmp_path, monkeypatch):
    import search_memory
    from repository_scope import resolve_repository_scope

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "a.md").write_text("# A\nSeal needle.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)

    # Force generation open path with seal flip after first check.
    seals = {"n": 0}

    def seal(*_a, **_k):
        seals["n"] += 1
        return ("seal-a",) if seals["n"] == 1 else ("seal-b",)

    monkeypatch.setattr(search_memory, "_generation_consumption_seal", seal)
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_unchanged",
        lambda *_a, **_k: False,
    )

    class Catalog:
        generations_path = tmp_path / "gens"

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return {
                "generation_id": "gen-1",
                "repository_scope": resolve_repository_scope(vault).as_dict(),
                "vector_state": "complete",
                "embedding_model_id": "m",
                "embedding_model_revision": "r",
                "artifacts": [{"path": "search.sqlite3", "size": 1, "sha256": "0" * 64}],
                "source_manifest_sha256": "1" * 64,
                "collector_version": "c",
                "extractor_version": "e",
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "schema_version": "corpus-generation/v1",
            }

    # Open generation fails connection → generation_unavailable path still retrieve.
    monkeypatch.setattr(search_memory, "_generation_connection", lambda *_a, **_k: None)

    results = search_memory.search(
        "seal needle",
        catalog=Catalog(),
        semantic=True,
        generation_embedder=lambda texts: [[1.0, 0.0]],
        generation_model_id="m",
        generation_model_revision="r",
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )
    assert results
    assert results[0]["effective_mode"] == "BASE"
    assert results[0]["fallback_reason"] in {
        "generation_unavailable",
        "generation_seal_changed",
        "generation_vectors_unavailable",
        "dense_unavailable",
    }


def test_trace_includes_reranker_diagnostics(monkeypatch):
    import mcp_server
    import reranker
    import retrieval
    from reliable_memory import validate_schema

    def lexical(**_k):
        return [
            _hit("c-a", "a.md", 5.0),
            _hit("c-b", "b.md", 4.0),
        ]

    def dense(**_k):
        return [
            _hit("c-b", "b.md", 0.9),
            _hit("c-a", "a.md", 0.1),
        ]

    def fake_rerank(query, documents, limit=10, **kwargs):
        out = []
        for index, doc in enumerate(documents):
            item = dict(doc)
            item["reranker_applied"] = True
            item["rerank_score"] = float(len(documents) - index)
            item["final_score"] = float(item.get("rrf_score") or 0) + item["rerank_score"]
            item["score"] = item["final_score"]
            item["reranker_model_id"] = "fake/m"
            item["reranker_model_revision"] = "rev"
            item["reranker_depth"] = 20
            item["reranker_duration_ms"] = 3
            item["reranker_fallback_reason"] = None
            out.append(item)
        out.sort(key=lambda d: (-float(d["final_score"]), str(d.get("candidate_id"))))
        return out[:limit]

    monkeypatch.setattr(reranker, "should_rerank", lambda **_k: (True, None))
    monkeypatch.setattr(reranker, "rerank", fake_rerank)

    result = retrieval.retrieve(
        "What is different?",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=dense,
        graph_backend=None,
        corpus_generation="gen",
        rerank_enabled=True,
    )
    assert result.trace.reranker_applied is True
    assert result.trace.reranker_model_id == "fake/m"
    assert result.trace.reranker_model_revision == "rev"
    assert result.trace.reranker_depth == 20
    assert result.trace.reranker_duration_ms == 3
    payload = mcp_server._reported_trace(dataclasses.asdict(result.trace))
    validate_schema(payload, SCHEMAS / "retrieval-trace-v1.json")
    rows = retrieval.candidates_to_legacy(result, display_meta=result.display_meta)
    assert rows[0]["reranker_applied"] is True
    assert rows[0]["reranker_model_id"] == "fake/m"


def test_reranker_receives_real_title_and_content_not_stem_only():
    import retrieval

    seen = []

    def lexical(**_k):
        return [
            _hit("c-a", "knowledge/notes/a.md", 5.0, title="Alpha Title", summary="alpha body"),
            _hit("c-b", "knowledge/notes/b.md", 4.9, title="Beta Title", summary="beta body"),
        ]

    def dense(**_k):
        return [
            _hit("c-b", "knowledge/notes/b.md", 0.99, title="Beta Title", summary="beta body"),
            _hit("c-a", "knowledge/notes/a.md", 0.1, title="Alpha Title", summary="alpha body"),
        ]

    def fake_rerank(query, documents, limit=10, **kwargs):
        seen.extend([(d.get("title"), d.get("summary") or d.get("content")) for d in documents])
        for d in documents:
            d["reranker_applied"] = True
            d["rerank_score"] = 1.0
            d["final_score"] = d.get("rrf_score", 0)
            d["reranker_model_id"] = "m"
            d["reranker_model_revision"] = "r"
            d["reranker_depth"] = 20
            d["reranker_duration_ms"] = 1
            d["reranker_fallback_reason"] = None
        return documents[:limit]

    with patch("reranker.should_rerank", return_value=(True, None)), patch(
        "reranker.rerank", side_effect=fake_rerank
    ):
        retrieval.retrieve(
            "What is alpha?",
            requested_profile="HYBRID",
            lexical_backend=lexical,
            dense_backend=dense,
            rerank_enabled=True,
            corpus_generation="g",
        )
    assert ("Alpha Title", "alpha body") in seen
    assert all(t != "a" for t, _ in seen)


def test_should_rerank_refuses_only_for_a_named_reason(monkeypatch):
    """Every question in a rerank profile is reranked; a refusal names why."""
    import reranker

    # The reranker is installed here; its absence is its own refusal (audit 2026-09-26 B-16).
    monkeypatch.setattr(reranker, "reranker_installed", lambda: True)

    docs = [
        {"rrf_score": 1.0, "bm25_rank": 1, "vector_rank": 1},
        {"rrf_score": 0.2, "bm25_rank": 2, "vector_rank": 2},
    ]
    assert reranker.should_rerank(profile="HYBRID", candidates=docs) == (True, None)
    assert reranker.should_rerank(
        profile="HYBRID", candidates=docs, analysis_intents=("quoted_phrase",)
    ) == (False, "exact_match_bypass")
    assert reranker.should_rerank(profile="EXACT", candidates=docs) == (
        False,
        "profile_bypass",
    )
    assert reranker.should_rerank(profile="HYBRID", candidates=docs[:1]) == (
        False,
        "tiny_result_set",
    )


@pytest.mark.parametrize(
    ("query", "intent", "profile"),
    [
        ("Что зависит от search_memory?", "graph_relation", "GRAPH"),
        ("Покажи карту репозитория scripts", "repo_map", "REPO_MAP"),
        ("Влияние изменения search_memory.py", "impact", "IMPACT"),
        ("Синтез архитектуры across all projects", "global_synthesis", "GLOBAL"),
        ("решения с 2025-01-01", "temporal", "TEMPORAL"),
        ("什么依赖 search_memory", "graph_relation", "GRAPH"),
        ("显示仓库地图 scripts", "repo_map", "REPO_MAP"),
        ("更改 search_memory.py 的影响", "impact", "IMPACT"),
        ("跨项目综合架构", "global_synthesis", "GLOBAL"),
        ("自 2025-01-01 以来的决策", "temporal", "TEMPORAL"),
        ("为什么选择 auth？", "question", "HYBRID"),
    ],
)
def test_ru_zh_analyzer_covers_all_intents(query, intent, profile):
    import retrieval

    analysis = retrieval.analyze_query(query)
    assert intent in analysis.intents
    assert analysis.recommended_profile == profile


def test_hard_filter_contract_parity_across_backends():
    import retrieval

    filters_seen = []

    def lexical(**kwargs):
        filters_seen.append(("lexical", dict(kwargs)))
        return [_hit("c1", "a.md", 1.0, project="demo", status="active")]

    def dense(**kwargs):
        filters_seen.append(("dense", dict(kwargs)))
        return [_hit("c1", "a.md", 0.9, project="demo", status="active")]

    retrieval.retrieve(
        "needle",
        requested_profile="HYBRID",
        scope="wiki",
        limit=3,
        project="demo",
        since="2024-01-01",
        as_of="2026-01-01",
        lexical_backend=lexical,
        dense_backend=dense,
        rerank_enabled=False,
        corpus_generation="g",
    )
    assert len(filters_seen) == 2
    assert filters_seen[0][1] == filters_seen[1][1]
    for key in ("scope", "project", "since", "as_of", "limit", "query"):
        assert key in filters_seen[0][1]


def test_no_process_global_display_meta_leakage():
    import retrieval

    r1 = retrieval.retrieve(
        "one",
        requested_profile="BASE",
        lexical_backend=lambda **k: [_hit("x", "x.md", 1.0, title="X")],
        rerank_enabled=False,
        corpus_generation="g1",
    )
    r2 = retrieval.retrieve(
        "two",
        requested_profile="BASE",
        lexical_backend=lambda **k: [_hit("y", "y.md", 1.0, title="Y")],
        rerank_enabled=False,
        corpus_generation="g2",
    )
    assert r1.display_meta is not None
    assert r2.display_meta is not None
    assert "x" in r1.display_meta
    assert "y" in r2.display_meta
    assert getattr(retrieval.retrieve, "_last_display_meta", None) in (None, {})
    assert getattr(retrieval.fuse_rrf, "_last_meta", None) in (None, {})


def test_semantic_false_keeps_base_even_when_dense_backend_present():
    import retrieval

    called = {"dense": 0}

    def dense(**_k):
        called["dense"] += 1
        return [_hit("d", "d.md", 0.9)]

    result = retrieval.retrieve(
        "plain query",
        requested_profile="BASE",
        lexical_backend=lambda **k: [_hit("a", "a.md", 1.0)],
        dense_backend=dense,
        rerank_enabled=False,
        corpus_generation="g",
    )
    assert called["dense"] == 0
    assert result.trace.effective_mode == "BASE"
    assert result.trace.signals_used == ("lexical",)


