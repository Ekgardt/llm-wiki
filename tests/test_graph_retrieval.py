"""Task 22: bounded, explainable graph expansion for retrieval."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from context_compiler import compile_context
from corpus_snapshot import (
    CapturedSource,
    CorpusSnapshot,
    SnapshotPolicy,
    SourceMetadata,
    SourceRecord,
)
from retrieval import candidates_to_legacy, expand_evidence_graph, retrieve


def _hit(candidate_id: str, path: str, score: float, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": candidate_id,
        "parent_id": path,
        "relative_path": path,
        "source_sha256": "a" * 64,
        "byte_start": 0,
        "byte_end": 10,
        "score": score,
        "title": Path(path).stem,
        "content": Path(path).stem.replace("-", " "),
    }
    row.update(extra)
    return row


def _expansion(
    candidate_id: str,
    *,
    seed_id: str = "seed",
    edge_type: str = "CALLS",
    direction: str = "out",
    content: str = "target implementation",
) -> dict[str, object]:
    assertion_id = f"assertion:{candidate_id}"
    evidence_id = f"evidence:{candidate_id}"
    return _hit(
        candidate_id,
        f"{candidate_id}.md",
        999_999.0,
        seed_id=seed_id,
        hop=1,
        edge_type=edge_type,
        direction=direction,
        content=content,
        assertion_path=(
            {
                "assertion_id": assertion_id,
                "source_node_id": seed_id,
                "target_node_id": candidate_id,
                "edge_type": edge_type,
                "direction": direction,
                "evidence_ids": (evidence_id,),
            },
        ),
        evidence_ids=(evidence_id,),
    )


def test_graph_expansion_uses_only_high_confidence_text_seeds_and_profile_policy() -> None:
    seen: dict[str, object] = {}

    def lexical(**_filters: object):
        return [
            _hit("seed", "seed.md", 4.0, retrieval_confidence="high"),
            _hit("low", "low.md", 3.9, retrieval_confidence="low"),
        ]

    def graph(**options: object):
        seen.update(options)
        return [_expansion("target")]

    result = retrieve(
        "what calls target implementation",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        graph_backend=graph,
        rerank_enabled=False,
    )

    assert [row["candidate_id"] for row in seen["seeds"]] == ["seed"]
    assert seen["max_hops"] == 1
    assert seen["directions"] == ("in", "out")
    assert seen["per_seed_limit"] > 0
    assert seen["global_limit"] >= seen["per_seed_limit"]
    assert "CALLS" in seen["edge_types"]
    assert {item.candidate_id for item in result.candidates} == {"seed", "low", "target"}


def test_graph_expansion_applies_deterministic_per_seed_global_caps_and_edge_decay() -> None:
    def lexical(**_filters: object):
        return [_hit("seed", "seed.md", 4.0, retrieval_confidence="high")]

    def graph(**_options: object):
        return [
            _expansion("z-link", edge_type="LINKS_TO", content="target"),
            _expansion("b-call", edge_type="CALLS", content="target"),
            _expansion("a-call", edge_type="CALLS", content="target"),
        ]

    result = retrieve(
        "target",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        graph_backend=graph,
        graph_per_seed_limit=2,
        graph_global_limit=2,
        rerank_enabled=False,
    )

    expanded = [item for item in result.candidates if item.graph_rank is not None]
    assert [item.candidate_id for item in expanded] == ["a-call", "b-call"]
    assert all(item.graph_score == pytest.approx(0.85) for item in expanded)


def test_every_expansion_preserves_exact_assertion_path_and_evidence() -> None:
    def lexical(**_filters: object):
        return [_hit("seed", "seed.md", 4.0, retrieval_confidence="high")]

    result = retrieve(
        "target implementation",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        graph_backend=lambda **_options: [_expansion("target")],
        rerank_enabled=False,
    )
    row = next(item for item in candidates_to_legacy(result) if item["candidate_id"] == "target")

    assert row["graph_seed_id"] == "seed"
    assert row["graph_direction"] == "out"
    assert row["graph_edge_type"] == "CALLS"
    assert row["assertion_path"] == [
        {
            "assertion_id": "assertion:target",
            "source_node_id": "seed",
            "target_node_id": "target",
            "edge_type": "CALLS",
            "direction": "out",
            "evidence_ids": ["evidence:target"],
        }
    ]
    assert row["evidence_ids"] == ["evidence:target"]


def test_expanded_nodes_are_reranked_against_original_query_before_fusion() -> None:
    def lexical(**_filters: object):
        return [_hit("seed", "seed.md", 4.0, retrieval_confidence="high")]

    def graph(**_options: object):
        return [
            _expansion("near", content="unrelated words"),
            _expansion("far", content="needle architecture"),
        ]

    result = retrieve(
        "needle architecture",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        graph_backend=graph,
        rerank_enabled=False,
    )

    expanded = [item for item in result.candidates if item.graph_rank is not None]
    assert [item.candidate_id for item in expanded] == ["far", "near"]


def test_graph_only_proximity_cannot_beat_weak_text_except_direct_graph_query() -> None:
    def lexical(**_filters: object):
        return [_hit("weak-text", "weak.md", 0.00001, content="needle")]

    def graph(**_options: object):
        return [_expansion("proximity", seed_id="weak-text", content="unrelated")]
    ordinary = retrieve(
        "needle",
        requested_profile="GLOBAL",
        lexical_backend=lexical,
        dense_backend=lambda **_filters: (),
        graph_backend=graph,
        rerank_enabled=False,
    )
    direct = retrieve(
        "what calls needle",
        requested_profile="GRAPH",
        lexical_backend=lexical,
        graph_backend=graph,
        rerank_enabled=False,
    )

    assert ordinary.candidates[0].candidate_id == "weak-text"
    assert all(item.candidate_id != "proximity" for item in ordinary.candidates)
    assert any(item.candidate_id == "proximity" for item in direct.candidates)


def test_edge_family_ablation_removes_disabled_family_before_fusion() -> None:
    seen: dict[str, object] = {}

    def graph(**options: object):
        seen.update(options)
        return [
            _expansion("helpful", edge_type="CALLS", content="target"),
            _expansion("harmful", edge_type="CO_CHANGED_WITH", content="target"),
        ]

    result = retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
        graph_backend=graph,
        graph_edge_families={"CO_CHANGED_WITH": False},
        rerank_enabled=False,
    )

    assert "CO_CHANGED_WITH" not in seen["edge_types"]
    assert {item.candidate_id for item in result.candidates} == {"seed", "helpful"}


def test_graph_backend_failure_falls_back_to_text_with_stable_generation() -> None:
    def broken_graph(**_options: object):
        raise OSError("graph unavailable")

    result = retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
        graph_backend=broken_graph,
        corpus_generation="gen-22",
        rerank_enabled=False,
    )

    assert [item.candidate_id for item in result.candidates] == ["seed"]
    assert result.trace.corpus_generation == "gen-22"
    assert result.trace.effective_mode == "BASE"
    assert result.trace.fallback_reason == "graph_error"


def test_context_trace_keeps_graph_assertion_paths_and_evidence() -> None:
    content = b"---\ntype: concept\n---\n# Target\n"
    digest = hashlib.sha256(content).hexdigest()
    source = CapturedSource(
        SourceRecord("source:target", "target.md", digest, len(content), "text/markdown", "en", None),
        SourceMetadata("concept", None, "user", "high", "active", None, None, "en"),
        content,
    )
    snapshot = CorpusSnapshot(
        (source,),
        (),
        digest,
        SnapshotPolicy((), (), False, None, 100, 1024, 1024, 100, 100, 8),
    )
    provenance = _expansion("target")["assertion_path"]

    compiled = compile_context(
        snapshot,
        graph_expansions=(
            {
                "candidate_id": "target",
                "seed_id": "seed",
                "assertion_path": provenance,
                "evidence_ids": ("evidence:target",),
            },
        ),
    )

    assert compiled.trace.graph_expansions[0].candidate_id == "target"
    assert compiled.trace.graph_expansions[0].seed_id == "seed"
    assert compiled.trace.graph_expansions[0].assertion_path[0]["assertion_id"] == "assertion:target"
    assert compiled.trace.graph_expansions[0].evidence_ids == ("evidence:target",)


def test_evidence_graph_adapter_reads_typed_directions_with_exact_evidence(tmp_path) -> None:
    from evidence_graph import EvidenceGraph, create_generation_database

    caller = b"def caller(): target()\n"
    target = b"def target(): pass\n"
    sources = [
        {
            "source_id": "src-caller",
            "relative_path": "caller.py",
            "sha256": hashlib.sha256(caller).hexdigest(),
            "size": len(caller),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        },
        {
            "source_id": "src-target",
            "relative_path": "target.py",
            "sha256": hashlib.sha256(target).hexdigest(),
            "size": len(target),
            "media_type": "text/x-python",
            "language": "python",
            "git_oid": None,
        },
    ]
    nodes = [
        {
            "node_id": name,
            "kind": "function",
            "identity_scheme": "test/v1",
            "identity_key": name,
            "metadata": {"name": name, "path": f"{name}.py"},
        }
        for name in ("caller", "target")
    ]
    occurrences = [
        {
            "occurrence_id": f"occ-{name}",
            "node_id": name,
            "source_id": f"src-{name}",
            "role": "definition",
            "byte_start": 0,
            "byte_end": len(content) - 1,
            "line_start": 1,
            "line_end": 1,
        }
        for name, content in (("caller", caller), ("target", target))
    ]
    start = caller.index(b"target()")
    assertions = [
        {
            "assertion_id": "call",
            "source_node_id": "caller",
            "edge_type": "CALLS",
            "target_node_id": "target",
            "literal": None,
            "confidence": "high",
            "authority": "ai-derived",
            "resolution": "resolved",
            "extractor": "test/v1",
        }
    ]
    evidence = [
        {
            "evidence_id": "ev-call",
            "assertion_id": "call",
            "observation_id": None,
            "source_id": "src-caller",
            "byte_start": start,
            "byte_end": start + len(b"target()"),
            "span_sha256": hashlib.sha256(b"target()").hexdigest(),
        }
    ]
    database = tmp_path / "evidence.sqlite3"
    create_generation_database(
        database,
        sources=sources,
        source_bytes={"src-caller": caller, "src-target": target},
        nodes=nodes,
        occurrences=occurrences,
        assertions=assertions,
        evidence=evidence,
        observations=(),
        dependencies=(),
    )

    with EvidenceGraph(database, state_root=tmp_path, generation_id="gen-22") as graph:
        outbound = expand_evidence_graph(
            graph,
            seeds=(_hit("caller", "caller.py", 1.0),),
            directions=("out",),
            edge_types=("CALLS",),
            per_seed_limit=2,
            global_limit=2,
        )
        inbound = expand_evidence_graph(
            graph,
            seeds=(_hit("target", "target.py", 1.0),),
            directions=("in",),
            edge_types=("CALLS",),
            per_seed_limit=2,
            global_limit=2,
        )

    assert outbound[0]["candidate_id"] == "target"
    assert outbound[0]["relative_path"] == "target.py"
    assert outbound[0]["assertion_path"][0]["evidence_ids"] == ("ev-call",)
    assert outbound[0]["assertion_path"][0]["evidence"][0] == {
        "evidence_id": "ev-call",
        "source_id": "src-caller",
        "relative_path": "caller.py",
        "byte_start": start,
        "byte_end": start + len(b"target()"),
        "span_sha256": hashlib.sha256(b"target()").hexdigest(),
    }
    assert inbound[0]["candidate_id"] == "caller"
    assert inbound[0]["direction"] == "in"


@pytest.mark.parametrize(
    "options",
    [
        {"graph_per_seed_limit": True},
        {"graph_per_seed_limit": 0},
        {"graph_global_limit": 1001},
        {"graph_edge_families": {"CALLS": "yes"}},
        {"graph_edge_families": {"UNKNOWN": False}},
    ],
)
def test_graph_policy_rejects_ambiguous_or_unbounded_configuration(options) -> None:
    with pytest.raises(ValueError):
        retrieve(
            "what calls target",
            requested_profile="GRAPH",
            lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
            graph_backend=lambda **_filters: (),
            rerank_enabled=False,
            **options,
        )


def test_graph_deadline_is_not_downgraded_to_backend_fallback() -> None:
    def timed_out(**_options: object):
        raise TimeoutError("deadline")

    with pytest.raises(TimeoutError, match="deadline"):
        retrieve(
            "what calls target",
            requested_profile="GRAPH",
            lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
            graph_backend=timed_out,
            rerank_enabled=False,
        )


def test_context_rejects_incomplete_graph_provenance() -> None:
    content = b"---\ntype: concept\n---\n# Target\n"
    digest = hashlib.sha256(content).hexdigest()
    source = CapturedSource(
        SourceRecord("source:target", "target.md", digest, len(content), "text/markdown", "en", None),
        SourceMetadata("concept", None, "user", "high", "active", None, None, "en"),
        content,
    )
    snapshot = CorpusSnapshot(
        (source,),
        (),
        digest,
        SnapshotPolicy((), (), False, None, 100, 1024, 1024, 100, 100, 8),
    )

    with pytest.raises(ValueError, match="provenance"):
        compile_context(
            snapshot,
            graph_expansions=(
                {
                    "candidate_id": "target",
                    "seed_id": "seed",
                    "assertion_path": (),
                    "evidence_ids": (),
                },
            ),
        )


def test_seed_cap_preserves_backend_rank_instead_of_identifier_order() -> None:
    seen: dict[str, object] = {}

    def graph(**options: object):
        seen.update(options)
        return ()

    retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [
            _hit("z-first", "z.md", 100.0),
            _hit("a-second", "a.md", 1.0),
        ],
        graph_backend=graph,
        rerank_enabled=False,
    )

    assert [item["candidate_id"] for item in seen["seeds"]] == ["z-first", "a-second"]


def test_graph_provenance_survives_when_expansion_is_also_a_text_hit() -> None:
    result = retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [
            _hit("seed", "seed.md", 2.0),
            _hit("target", "target.md", 1.0, content="target"),
        ],
        graph_backend=lambda **_filters: [_expansion("target")],
        rerank_enabled=False,
    )

    row = next(item for item in candidates_to_legacy(result) if item["candidate_id"] == "target")
    assert row["bm25_rank"] == 2
    assert row["graph_rank"] == 1
    assert row["assertion_path"][0]["assertion_id"] == "assertion:target"
    assert row["evidence_ids"] == ["evidence:target"]


def test_generation_search_seals_graph_artifact_and_uses_same_generation(
    tmp_path, monkeypatch
) -> None:
    import evidence_graph
    import retrieval
    import search_memory

    sealed: list[tuple[str, ...]] = []
    closed: list[str] = []

    class Connection:
        def close(self):
            closed.append("search")

    class Graph:
        generation_id = "gen-22"

        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            closed.append("graph")

    manifest = {
        "generation_id": "gen-22",
        "vector_state": "absent",
        "artifacts": [
            {"path": "search.sqlite3", "size": 1, "sha256": "0" * 64},
            {"path": "evidence.sqlite3", "size": 1, "sha256": "1" * 64},
        ],
    }

    class Catalog:
        generations_path = tmp_path / "generations"
        state_root = tmp_path

        def get_active(self):
            return manifest

    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_seal",
        lambda _catalog, _manifest, names: sealed.append(tuple(names)) or ("seal",),
    )
    monkeypatch.setattr(search_memory, "_generation_consumption_unchanged", lambda *_a: True)
    monkeypatch.setattr(search_memory, "_generation_connection", lambda *_a: Connection())
    monkeypatch.setattr(
        search_memory,
        "_generation_fts_search",
        lambda *_a, **_k: [
            _hit("seed", "seed.py", 1.0, retrieval_confidence="high", content="seed")
        ],
    )
    monkeypatch.setattr(evidence_graph, "EvidenceGraph", Graph)
    monkeypatch.setattr(
        retrieval,
        "expand_evidence_graph",
        lambda graph, **_options: (
            _expansion("target", seed_id="seed", content="target"),
        ),
    )

    rows = retrieval.retrieve_via_search_memory(
        "what calls target",
        catalog=Catalog(),
        semantic=True,
        graph=True,
        rerank=False,
        emit_telemetry=False,
        profile="GRAPH",
    )

    assert any("evidence.sqlite3" in names for names in sealed)
    assert next(row for row in rows if row["candidate_id"] == "target")["generation"] == "gen-22"
    assert closed == ["graph", "search"]


def test_multiple_paths_to_one_node_merge_evidence_without_multiple_rrf_votes() -> None:
    first = _expansion("target", content="target")
    second = _expansion("target", content="target")
    second["assertion_path"] = (
        {
            **second["assertion_path"][0],
            "assertion_id": "assertion:target:second",
            "evidence_ids": ("evidence:target:second",),
        },
    )
    second["evidence_ids"] = ("evidence:target:second",)

    result = retrieve(
        "what calls target",
        requested_profile="GRAPH",
        lexical_backend=lambda **_filters: [_hit("seed", "seed.md", 1.0)],
        graph_backend=lambda **_filters: (first, second),
        rerank_enabled=False,
    )
    target = next(item for item in result.candidates if item.candidate_id == "target")
    row = next(item for item in candidates_to_legacy(result) if item["candidate_id"] == "target")

    assert target.rrf_score == round(0.5 / 61, 6)
    assert [step["assertion_id"] for step in row["assertion_path"]] == [
        "assertion:target",
        "assertion:target:second",
    ]
    assert row["evidence_ids"] == ["evidence:target", "evidence:target:second"]
