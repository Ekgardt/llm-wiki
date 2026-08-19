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

PROFILE_EXPECTED_SIGNALS = {
    "DIRECT": ("lexical",),
    "EXACT": ("lexical",),
    "BASE": ("lexical",),
    "HYBRID": ("lexical", "dense"),
    "GRAPH": ("lexical", "graph"),
    "TEMPORAL": ("lexical",),
    "REPO_MAP": ("lexical", "graph"),
    "IMPACT": ("lexical", "graph"),
    "GLOBAL": ("lexical", "dense", "graph"),
    "CACHED_FULL": ("lexical",),
}


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
    **extra: object,
) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": candidate_id,
        "parent_id": parent_id or path,
        "relative_path": path,
        "heading_path": heading_path,
        "source_sha256": source_sha256 or ("a" * 64),
        "byte_start": byte_start,
        "byte_end": byte_end,
        "score": score,
    }
    row.update(extra)
    return row


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
        # RU / ZH / fullwidth question mark
        ("Что такое auth decision?", "question", "HYBRID"),
        ("什么是 auth decision？", "question", "HYBRID"),
        ("auth decision？", "question", "HYBRID"),
        # filename, sqlite path, snake_case
        ("auth-decision.md", "exact_identifier", "EXACT"),
        ("cache/evidence-graph/catalog.sqlite3", "exact_identifier", "EXACT"),
        ("search_memory_rebuild", "exact_identifier", "EXACT"),
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
        'Find "exact phrase" and scripts/search_memory.py plus CamelCaseSymbol '
        "and snake_case_id and notes/page.md and run/queue.sqlite3"
    )
    assert "exact phrase" in analysis.quoted_phrases
    assert "scripts/search_memory.py" in analysis.exact_identifiers
    assert "CamelCaseSymbol" in analysis.exact_identifiers
    assert "snake_case_id" in analysis.exact_identifiers
    assert "notes/page.md" in analysis.exact_identifiers
    assert "run/queue.sqlite3" in analysis.exact_identifiers
    assert "page.md" in analysis.exact_identifiers or any(
        item.endswith("page.md") for item in analysis.exact_identifiers
    )


def test_rrf_is_rank_only_and_keeps_raw_backend_fields_separate() -> None:
    import retrieval

    lexical = [
        _hit(candidate_id="c-a", path="a.md", score=12.5, bm25_score=12.5),
        _hit(candidate_id="c-b", path="b.md", score=4.0, bm25_score=4.0),
    ]
    dense = [
        _hit(candidate_id="c-b", path="b.md", score=0.91, vector_score=0.91),
        _hit(candidate_id="c-a", path="a.md", score=0.40, vector_score=0.40),
    ]
    fused, _meta = retrieval.fuse_rrf(lexical=lexical, dense=dense, graph=None)
    assert fused[0].candidate_id == "c-a"
    assert fused[0].bm25_rank == 1
    assert fused[0].bm25_score == 12.5
    assert fused[0].vector_rank == 2
    assert fused[0].vector_score == 0.40
    assert fused[0].rrf_score > fused[1].rrf_score
    assert fused[0].final_score == fused[0].rrf_score
    assert not hasattr(fused[0], "vector_distance")
    # Raw magnitudes must not change rank-only fusion vs pure ranks.
    lexical_huge = [
        _hit(candidate_id="c-a", path="a.md", score=9999.0),
        _hit(candidate_id="c-b", path="b.md", score=0.001),
    ]
    dense_tiny = [
        _hit(candidate_id="c-b", path="b.md", score=0.0001),
        _hit(candidate_id="c-a", path="a.md", score=0.00001),
    ]
    fused2, _ = retrieval.fuse_rrf(lexical=lexical_huge, dense=dense_tiny, graph=None)
    assert [item.candidate_id for item in fused2] == [item.candidate_id for item in fused]
    assert fused2[0].rrf_score == fused[0].rrf_score


def test_rrf_ties_are_broken_deterministically_by_candidate_id() -> None:
    import retrieval

    # Rank-only ignores raw boost magnitudes.
    graph = [
        {**_hit(candidate_id="c-z", path="z.md", score=0.0), "graph_boost": 99.0},
        {**_hit(candidate_id="c-a", path="a.md", score=0.0), "graph_boost": 0.01},
    ]
    by_rank, _ = retrieval.fuse_rrf(lexical=None, dense=None, graph=graph)
    assert [item.candidate_id for item in by_rank] == ["c-z", "c-a"]
    assert by_rank[0].graph_score == 99.0
    assert by_rank[1].graph_score == 0.01

    # Symmetric ranks + equal weights → equal RRF; tie-break by candidate_id.
    original_bm25 = retrieval.BM25_WEIGHT
    original_dense = retrieval.DENSE_WEIGHT
    try:
        retrieval.BM25_WEIGHT = 1.0
        retrieval.DENSE_WEIGHT = 1.0
        fused, _ = retrieval.fuse_rrf(
            lexical=[
                _hit(candidate_id="c-z", path="z.md", score=1.0),
                _hit(candidate_id="c-a", path="a.md", score=1.0),
            ],
            dense=[
                _hit(candidate_id="c-a", path="a.md", score=1.0),
                _hit(candidate_id="c-z", path="z.md", score=1.0),
            ],
            graph=None,
        )
    finally:
        retrieval.BM25_WEIGHT = original_bm25
        retrieval.DENSE_WEIGHT = original_dense
    assert fused[0].rrf_score == fused[1].rrf_score
    assert [item.candidate_id for item in fused] == ["c-a", "c-z"]


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


def test_retrieve_runs_lexical_and_dense_separately() -> None:
    import retrieval

    calls: list[str] = []

    def lexical(**_kwargs):
        calls.append("lexical")
        return [_hit(candidate_id="c-a", path="a.md", score=2.0)]

    def dense(**_kwargs):
        calls.append("dense")
        return [_hit(candidate_id="c-b", path="b.md", score=0.9)]

    def fused_backend(**_kwargs):
        raise AssertionError("must not call a pre-fused backend")

    result = retrieval.retrieve(
        "needle",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=dense,
        graph_backend=None,
        corpus_generation="gen-sep",
        rerank_enabled=False,
    )
    assert calls == ["lexical", "dense"]
    assert set(result.trace.signals_used) == {"lexical", "dense"}
    assert result.trace.effective_mode == "HYBRID"
    assert result.trace.fallback_reason is None
    assert {c.candidate_id for c in result.candidates} == {"c-a", "c-b"}


@pytest.mark.parametrize("profile", PROFILES)
def test_all_profiles_request_declared_signals_behaviorally(profile: str) -> None:
    import retrieval

    calls: list[str] = []

    def lexical(**_kwargs):
        calls.append("lexical")
        return [_hit(candidate_id="c-lex", path="lex.md", score=3.0)]

    def dense(**_kwargs):
        calls.append("dense")
        return [_hit(candidate_id="c-den", path="den.md", score=0.8)]

    def graph(**_kwargs):
        calls.append("graph")
        return [
            {
                **_hit(candidate_id="c-g", path="g.md", score=0.0),
                "graph_boost": 0.2,
            }
        ]

    result = retrieval.retrieve(
        "probe",
        requested_profile=profile,
        lexical_backend=lexical,
        dense_backend=dense,
        graph_backend=graph,
        corpus_generation="gen-profiles",
        graph_enabled=True,
        rerank_enabled=False,
    )
    expected = PROFILE_EXPECTED_SIGNALS[profile]
    assert result.trace.requested_mode == profile
    assert result.trace.effective_mode == profile
    assert result.trace.signals_used == expected
    assert result.trace.fallback_reason is None
    assert result.trace.corpus_generation == "gen-profiles"
    assert tuple(calls) == expected
    assert result.candidates


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


def test_public_search_goes_through_retrieve(tmp_path, monkeypatch) -> None:
    import retrieval
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

    seen: dict[str, object] = {}
    real_retrieve = retrieval.retrieve

    def wrapped_retrieve(*args, **kwargs):
        seen["called"] = True
        seen["kwargs"] = kwargs
        return real_retrieve(*args, **kwargs)

    monkeypatch.setattr(retrieval, "retrieve", wrapped_retrieve)

    results = search_memory.search(
        "auth decision needle",
        limit=5,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
    )
    assert seen.get("called") is True
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
    assert "vector_distance" not in row


def test_retrieve_conditional_rerank_blends_and_reports_signal() -> None:
    import retrieval

    def lexical(**_kwargs):
        return [
            _hit(candidate_id="c-a", path="a.md", score=5.0),
            _hit(candidate_id="c-b", path="b.md", score=4.9),
        ]

    def dense(**_kwargs):
        return [
            _hit(candidate_id="c-b", path="b.md", score=0.99),
            _hit(candidate_id="c-a", path="a.md", score=0.10),
        ]

    def fake_scorer(pairs):
        # Prefer the second pair document strongly.
        return [0.0, 8.0]

    import reranker

    real_rerank = reranker.rerank

    def wrapped(query, documents, limit=10, **kwargs):
        kwargs.setdefault("scorer", fake_scorer)
        return real_rerank(query, documents, limit=limit, **kwargs)

    import sys
    from unittest.mock import patch

    with patch.object(sys.modules.setdefault("reranker", reranker), "rerank", wrapped):
        # Patch via retrieval's import path
        with patch("reranker.rerank", wrapped):
            result = retrieval.retrieve(
                "What is the difference?",
                requested_profile="HYBRID",
                lexical_backend=lexical,
                dense_backend=dense,
                graph_backend=None,
                corpus_generation="gen-rr",
                rerank_enabled=True,
            )
    assert "reranker" in result.trace.signals_used
    assert result.candidates[0].rerank_score is not None
    assert result.candidates[0].final_score != result.candidates[0].rrf_score


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
            reranker_applied=False,
            reranker_model_id=None,
            reranker_model_revision=None,
            reranker_depth=None,
            reranker_duration_ms=None,
            reranker_fallback_reason="conditions_unmet",
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
        "reranker_applied": False,
        "reranker_model_id": None,
        "reranker_model_revision": None,
        "reranker_depth": None,
        "reranker_duration_ms": None,
        "reranker_fallback_reason": None,
        "extra": True,
    }
    with pytest.raises(SchemaValidationError):
        validate_schema(payload, SCHEMAS / "retrieval-trace-v1.json")


def test_typed_provenance_weighs_on_every_path_from_one_table():
    """A stated fact outranks a guess in the hybrid path, not only in BM25.

    The weights lived in `search_memory` and reached the lexical paths only, so
    the fused path ranked `source_authority: user` and `inferred` alike while
    the README promised typed-provenance ranking.
    """
    import provenance
    import search_memory
    from retrieval import fuse_rrf

    # One table, imported by both paths: the lexical scorer and the fusion.
    assert search_memory.authority_weight is provenance.authority_weight

    lexical = [
        {"path": "knowledge/notes/guess.md", "authority": "inferred", "score": 9.0},
        {"path": "knowledge/notes/stated.md", "authority": "user", "score": 9.0},
    ]
    fused, meta = fuse_rrf(lexical=lexical, dense=None, graph=None)

    by_path = {candidate.relative_path: candidate for candidate in fused}
    stated = by_path["knowledge/notes/stated.md"]
    guess = by_path["knowledge/notes/guess.md"]

    assert stated.authority_weight == provenance.AUTHORITY_WEIGHTS["user"]
    assert guess.authority_weight == provenance.AUTHORITY_WEIGHTS["inferred"]
    # The guess is one rank ahead lexically and still loses on trust.
    assert fused[0].relative_path == "knowledge/notes/stated.md"
    assert stated.final_score > guess.final_score
    assert stated.rrf_score < guess.rrf_score
    assert meta[stated.candidate_id]["authority_weight"] == stated.authority_weight


def test_an_unweighted_candidate_keeps_its_fused_score_unchanged():
    from retrieval import fuse_rrf

    fused, _meta = fuse_rrf(
        lexical=[{"path": "knowledge/notes/plain.md", "score": 1.0}],
        dense=None,
        graph=None,
    )

    assert fused[0].authority_weight == 1.0
    assert fused[0].final_score == fused[0].rrf_score


def test_the_reranker_keeps_the_trust_weight_on_its_blended_score():
    """A reranked list must not drop the prior fusion applied."""
    import reranker

    documents = [
        {
            "candidate_id": "a",
            "path": "knowledge/notes/a.md",
            "content": "alpha",
            "rrf_score": 0.03,
            "authority_weight": 0.8,
        },
        {
            "candidate_id": "b",
            "path": "knowledge/notes/b.md",
            "content": "beta",
            "rrf_score": 0.03,
            "authority_weight": 1.35,
        },
    ]
    ranked = reranker.rerank(
        "query",
        documents,
        depth=2,
        limit=2,
        text_field="content",
        scorer=lambda pairs: [0.5 for _pair in pairs],
    )

    weighted = {item["candidate_id"]: item["final_score"] for item in ranked}
    assert weighted["b"] > weighted["a"]
