"""Task 11: retrieval contract, query planner, and RRF fusion."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, validate_schema

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCHEMAS = SCRIPTS / "schemas"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PROFILES = (
    "DIRECT",
    "EXACT",
    "BASE",
    "HYBRID",
    "GRAPH",
    "TEMPORAL",
    "REPO_MAP",
    "IMPACT",
    "GLOBAL",
    "CACHED_FULL",
)


def _hit(
    *,
    candidate_id: str,
    path: str,
    score: float,
    source_sha256: str | None = None,
    heading_path: tuple[str, ...] = (),
    byte_start: int = 0,
    byte_end: int = 10,
    parent_id: str | None = None,
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "parent_id": parent_id or path,
        "relative_path": path,
        "heading_path": heading_path,
        "source_sha256": source_sha256 or ("a" * 64),
        "byte_start": byte_start,
        "byte_end": byte_end,
        "score": score,
    }


def test_profiles_are_closed_and_exported() -> None:
    import retrieval

    assert retrieval.PROFILES == PROFILES
    for name in PROFILES:
        assert name in retrieval.PROFILE_SIGNALS


@pytest.mark.parametrize(
    ("query", "intent", "profile"),
    [
        ('open "auth decision"', "quoted_phrase", "EXACT"),
        ("knowledge/notes/auth-decision.md", "exact_identifier", "EXACT"),
        ("hook-scripts-defense-in-depth", "exact_identifier", "EXACT"),
        ("What is the auth decision?", "question", "HYBRID"),
        ("decisions since 2025-01-01", "temporal", "TEMPORAL"),
        ("what depends on search_memory", "graph_relation", "GRAPH"),
        ("show repo map of scripts", "repo_map", "REPO_MAP"),
        ("impact of changing search_memory.py", "impact", "IMPACT"),
        ("synthesize architecture across all projects", "global_synthesis", "GLOBAL"),
        ("auth decision", None, "BASE"),
    ],
)
def test_analyze_query_is_deterministic(query: str, intent: str | None, profile: str) -> None:
    import retrieval

    first = retrieval.analyze_query(query)
    second = retrieval.analyze_query(query)
    assert first == second
    assert first.recommended_profile == profile
    if intent is not None:
        assert intent in first.intents
    assert first.normalized_query == second.normalized_query


def test_quoted_phrases_and_identifiers_are_extracted() -> None:
    import retrieval

    analysis = retrieval.analyze_query(
        'Find "exact phrase" and scripts/search_memory.py plus CamelCaseSymbol'
    )
    assert "exact phrase" in analysis.quoted_phrases
    assert "scripts/search_memory.py" in analysis.exact_identifiers
    assert "CamelCaseSymbol" in analysis.exact_identifiers


def test_rrf_keeps_raw_backend_scores_and_uses_larger_is_better() -> None:
    import retrieval

    lexical = [
        _hit(candidate_id="c-a", path="a.md", score=12.5),
        _hit(candidate_id="c-b", path="b.md", score=4.0),
    ]
    dense = [
        _hit(candidate_id="c-b", path="b.md", score=0.91),
        _hit(candidate_id="c-a", path="a.md", score=0.40),
    ]
    fused = retrieval.fuse_rrf(lexical=lexical, dense=dense, graph=None)
    assert fused[0].candidate_id == "c-a"
    assert fused[0].bm25_rank == 1
    assert fused[0].bm25_score == 12.5
    assert fused[0].vector_rank == 2
    assert fused[0].vector_score == 0.40
    assert fused[0].rrf_score > fused[1].rrf_score
    assert fused[0].final_score == fused[0].rrf_score
    assert fused[0].final_score > fused[1].final_score


def test_rrf_ties_are_broken_deterministically_by_candidate_id() -> None:
    import retrieval

    # Zero graph boosts yield equal RRF contributions; tie-break by candidate_id.
    graph = [
        {**_hit(candidate_id="c-z", path="z.md", score=0.0), "graph_boost": 0.0},
        {**_hit(candidate_id="c-a", path="a.md", score=0.0), "graph_boost": 0.0},
    ]
    fused = retrieval.fuse_rrf(lexical=None, dense=None, graph=graph)
    assert [item.candidate_id for item in fused] == ["c-a", "c-z"]
    assert fused[0].rrf_score == fused[1].rrf_score


def test_rrf_preserves_distance_field_separately_from_similarity() -> None:
    import retrieval

    dense = [
        {
            **_hit(candidate_id="c-a", path="a.md", score=0.8),
            "distance": 0.2,
        }
    ]
    fused = retrieval.fuse_rrf(lexical=[], dense=dense, graph=None)
    assert fused[0].vector_score == 0.8
    assert fused[0].vector_distance == 0.2


def test_retrieve_reports_requested_effective_signals_fallback_and_generation() -> None:
    import retrieval

    def lexical(**_kwargs):
        return [_hit(candidate_id="c-a", path="a.md", score=5.0)]

    def dense(**_kwargs):
        return None  # unavailable → fallback

    result = retrieval.retrieve(
        "auth decision",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=dense,
        graph_backend=None,
        corpus_generation="gen-test",
        graph_enabled=True,
        rerank_enabled=False,
    )
    assert result.trace.requested_mode == "HYBRID"
    assert result.trace.effective_mode == "BASE"
    assert result.trace.signals_used == ("lexical",)
    assert result.trace.fallback_reason == "dense_unavailable"
    assert result.trace.corpus_generation == "gen-test"
    assert result.trace.partial is False
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.candidate_id == "c-a"
    assert candidate.relative_path == "a.md"
    assert candidate.source_sha256 == "a" * 64
    assert candidate.bm25_score == 5.0
    assert candidate.vector_score is None
    assert candidate.final_score == candidate.rrf_score > 0


def test_retrieve_uses_identical_hard_filters_for_lexical_and_dense() -> None:
    import retrieval

    seen: list[dict[str, object]] = []

    def lexical(**kwargs):
        seen.append(dict(kwargs))
        return [_hit(candidate_id="c-a", path="a.md", score=2.0)]

    def dense(**kwargs):
        seen.append(dict(kwargs))
        return [_hit(candidate_id="c-a", path="a.md", score=0.9)]

    retrieval.retrieve(
        "needle",
        requested_profile="HYBRID",
        scope="wiki",
        limit=7,
        project="demo",
        since="2024-01-01",
        as_of="2025-06-01",
        lexical_backend=lexical,
        dense_backend=dense,
        corpus_generation="gen-filters",
    )
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0] == {
        "query": "needle",
        "scope": "wiki",
        "limit": 7,
        "project": "demo",
        "since": "2024-01-01",
        "as_of": "2025-06-01",
    }


def test_retrieve_honors_graph_disablement_even_for_graph_profile() -> None:
    import retrieval

    def lexical(**_kwargs):
        return [_hit(candidate_id="c-a", path="a.md", score=3.0)]

    def graph(**_kwargs):
        raise AssertionError("graph backend must not run when disabled")

    result = retrieval.retrieve(
        "what depends on auth",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        dense_backend=None,
        graph_backend=graph,
        graph_enabled=False,
        corpus_generation="gen-g",
    )
    assert result.trace.requested_mode == "GRAPH"
    assert result.trace.effective_mode == "BASE"
    assert result.trace.signals_used == ("lexical",)
    assert result.trace.fallback_reason == "graph_disabled"


def test_legacy_search_wrapper_preserves_dict_shape(tmp_path, monkeypatch) -> None:
    import search_memory

    vault = tmp_path / "vault"
    notes = vault / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "page.md").write_text("# Page\nAuth decision needle.\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", vault)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", tmp_path / "cache")
    monkeypatch.setattr(search_memory, "INDEX_FILE", tmp_path / "cache" / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", tmp_path / "cache" / ".paths-manifest")
    monkeypatch.setattr(search_memory, "VECTOR_NPY", tmp_path / "cache" / "vectors.npy")
    monkeypatch.setattr(search_memory, "VECTOR_META", tmp_path / "cache" / "vectors_meta.json")
    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)

    results = search_memory.search(
        "auth decision needle",
        limit=5,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert results
    row = results[0]
    for key in ("path", "title", "summary", "score", "project", "timestamp"):
        assert key in row
    assert row["requested_mode"] == "BASE"
    assert row["effective_mode"] == "BASE"
    assert "lexical" in row["signals_used"]
    assert row["generation"]
    assert row["score"] == row["final_score"]
    assert row["rrf_score"] == row["final_score"]


def test_cli_exposes_profile_and_disable_switches() -> None:
    import search_memory

    parser_src = Path(search_memory.__file__).read_text(encoding="utf-8")
    assert "--profile" in parser_src
    assert "--no-graph" in parser_src
    assert "--no-rerank" in parser_src


def test_retrieval_trace_schema_accepts_contract_payload() -> None:
    import retrieval

    payload = retrieval.trace_to_dict(
        retrieval.RetrievalTrace(
            requested_mode="HYBRID",
            effective_mode="BASE",
            signals_used=("lexical",),
            fallback_reason="dense_unavailable",
            corpus_generation="gen-1",
            partial=False,
        )
    )
    validate_schema(payload, SCHEMAS / "retrieval-trace-v1.json")
    schema = json.loads((SCHEMAS / "retrieval-trace-v1.json").read_text(encoding="utf-8"))
    assert schema["$id"].endswith("retrieval-trace-v1.json")
    assert "additionalProperties" in schema and schema["additionalProperties"] is False


def test_retrieval_trace_schema_rejects_unknown_fields() -> None:
    payload = {
        "schema_version": "retrieval-trace/v1",
        "requested_mode": "BASE",
        "effective_mode": "BASE",
        "signals_used": ["lexical"],
        "fallback_reason": None,
        "corpus_generation": "gen-1",
        "partial": False,
        "extra": True,
    }
    with pytest.raises(SchemaValidationError):
        validate_schema(payload, SCHEMAS / "retrieval-trace-v1.json")
