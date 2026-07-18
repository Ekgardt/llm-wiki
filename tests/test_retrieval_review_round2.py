"""Second-pass review blockers for retrieval orchestration."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _hit(cid: str, path: str, score: float, **extra):
    row = {
        "candidate_id": cid,
        "chunk_id": cid,
        "path": path,
        "relative_path": path,
        "parent_id": path,
        "score": score,
        "title": extra.pop("title", Path(path).stem),
        "summary": extra.pop("summary", f"summary {cid}"),
        "content": extra.pop("content", f"full chunk body for {cid} " * 3),
        "source_sha256": extra.pop("source_sha256", "a" * 64),
        "project": extra.pop("project", "demo"),
        "status": extra.pop("status", "active"),
        "authority": extra.pop("authority", "user"),
        "valid_from": extra.pop("valid_from", "2024-01-01"),
        "valid_to": extra.pop("valid_to", ""),
        "timestamp": extra.pop("timestamp", "2024-06-01"),
        "byte_start": 0,
        "byte_end": 20,
    }
    row.update(extra)
    return row


def test_semantic_false_forces_base_lexical_even_for_hybrid_profile(monkeypatch):
    import retrieval
    import search_memory

    dense_calls = {"n": 0}

    def dense(**_k):
        dense_calls["n"] += 1
        return [_hit("d", "d.md", 0.9)]

    # Direct retrieve_via_search_memory path: semantic=False must force BASE.
    analysis_profile = "HYBRID"
    monkeypatch.setattr(
        retrieval,
        "analyze_query",
        lambda q: retrieval.QueryAnalysis(
            query=q,
            normalized_query=q,
            intents=("question",),
            exact_identifiers=(),
            quoted_phrases=(),
            recommended_profile=analysis_profile,
        ),
    )
    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *a, **k: [_hit("a", "a.md", 1.0)],
    )
    monkeypatch.setattr(search_memory, "_legacy_dense_hits", lambda *a, **k: dense())

    rows = retrieval.retrieve_via_search_memory(
        "what is auth?",
        semantic=False,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    )
    assert dense_calls["n"] == 0
    assert rows
    assert rows[0]["effective_mode"] == "BASE"
    assert rows[0]["signals_used"] == ["lexical"]


def test_vector_inclusive_seal_used_when_vectors_complete(monkeypatch):
    import retrieval
    import search_memory

    sealed = []

    def fake_seal(catalog, manifest, artifact_names):
        sealed.append(tuple(artifact_names))
        return ("seal", tuple(artifact_names))

    monkeypatch.setattr(search_memory, "_generation_consumption_seal", fake_seal)
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_unchanged",
        lambda *a, **k: True,
    )

    class Conn:
        def close(self):
            return None

    monkeypatch.setattr(search_memory, "_generation_connection", lambda *a, **k: Conn())
    monkeypatch.setattr(
        search_memory,
        "_generation_fts_search",
        lambda *a, **k: [_hit("c1", "a.md", 1.0, chunk_id="c1")],
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_vectors_search",
        lambda *a, **k: [_hit("c1", "a.md", 0.9, chunk_id="c1")],
    )
    monkeypatch.setattr(search_memory, "_generation_artifact", lambda m, n: True)
    monkeypatch.setattr(search_memory, "_legacy_lexical_hits", lambda *a, **k: [])
    monkeypatch.setattr(search_memory, "_legacy_dense_hits", lambda *a, **k: None)

    class Catalog:
        generations_path = Path(".")

        def get_active(self):
            return {
                "generation_id": "gen-v",
                "vector_state": "complete",
                "embedding_model_id": "m",
                "embedding_model_revision": "r",
                "artifacts": [
                    {"path": "search.sqlite3", "size": 1, "sha256": "0" * 64},
                    {"path": "vectors.npy", "size": 1, "sha256": "1" * 64},
                    {"path": "vectors.json", "size": 1, "sha256": "2" * 64},
                ],
                "source_manifest_sha256": "a" * 64,
                "collector_version": "c",
                "extractor_version": "e",
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "schema_version": "corpus-generation/v1",
            }

    rows = retrieval.retrieve_via_search_memory(
        "auth",
        catalog=Catalog(),
        semantic=True,
        generation_embedder=lambda texts: [[1.0, 0.0]],
        generation_model_id="m",
        generation_model_revision="r",
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="HYBRID",
    )
    assert rows
    assert sealed
    # At least one seal call must include vector artifacts.
    assert any("vectors.npy" in names for names in sealed)


def test_hard_filter_candidate_id_parity_across_backends():
    from search_memory import apply_hard_filters

    rows = [
        _hit("keep", "a.md", 1.0, project="demo", status="active", timestamp="2025-06-01", valid_from="2025-01-01", valid_to="", authority="user"),
        _hit("drop-proj", "b.md", 1.0, project="other", status="active", timestamp="2025-06-01", valid_from="2025-01-01", valid_to=""),
        _hit("drop-status", "c.md", 1.0, project="demo", status="superseded", timestamp="2025-06-01", valid_from="2025-01-01", valid_to=""),
        _hit("drop-since", "d.md", 1.0, project="demo", status="active", timestamp="2020-01-01", valid_from="2020-01-01", valid_to=""),
        _hit("drop-asof", "e.md", 1.0, project="demo", status="active", timestamp="2026-12-01", valid_from="2026-12-01", valid_to=""),
        _hit("drop-validity", "f.md", 1.0, project="demo", status="active", timestamp="2025-03-01", valid_from="2025-01-01", valid_to="2025-02-01"),
    ]
    # Status filter applies when as_of is absent (superseded dropped).
    status_filt = {"project": "demo", "since": "2024-01-01", "scope": "all"}
    status_ids = {r["candidate_id"] for r in apply_hard_filters(rows, **status_filt)}
    assert "keep" in status_ids
    assert "drop-status" not in status_ids
    assert "drop-proj" not in status_ids
    assert "drop-since" not in status_ids
    # as_of uses validity window; future/expired validity dropped.
    asof_filt = {
        "project": "demo",
        "since": "2024-01-01",
        "as_of": "2025-07-01",
        "scope": "all",
    }
    lexical = apply_hard_filters(rows, **asof_filt)
    dense = apply_hard_filters(list(reversed(rows)), **asof_filt)
    assert {r["candidate_id"] for r in lexical} == {r["candidate_id"] for r in dense}
    ids = {r["candidate_id"] for r in lexical}
    assert "keep" in ids
    assert "drop-proj" not in ids
    assert "drop-asof" not in ids
    assert "drop-validity" not in ids


def test_exact_title_bypass_before_rerank(monkeypatch):
    import reranker
    import retrieval

    called = {"rerank": 0}

    def lexical(**_k):
        return [
            _hit("t1", "a.md", 10.0, title="Auth Decision", summary="s", content="full"),
            _hit("t2", "b.md", 1.0, title="Other", summary="o", content="x"),
        ]

    monkeypatch.setattr(reranker, "rerank", lambda *a, **k: called.__setitem__("rerank", called["rerank"] + 1) or a[1])
    result = retrieval.retrieve(
        "Auth Decision",
        requested_profile="HYBRID",
        lexical_backend=lexical,
        dense_backend=lambda **k: [
            _hit("t2", "b.md", 0.99, title="Other"),
            _hit("t1", "a.md", 0.1, title="Auth Decision"),
        ],
        rerank_enabled=True,
        corpus_generation="g",
    )
    assert called["rerank"] == 0
    assert result.trace.reranker_fallback_reason == "exact_title_bypass"
    assert result.candidates[0].candidate_id == "t1"


def test_reranker_receives_full_chunk_content(monkeypatch):
    import reranker
    import retrieval

    seen = []

    def fake_rerank(query, documents, limit=10, **kwargs):
        seen.extend([d.get("content") for d in documents])
        for d in documents:
            d["reranker_applied"] = True
            d["rerank_score"] = 1.0
            d["final_score"] = d.get("rrf_score", 0)
            d["reranker_model_id"] = "m"
            d["reranker_model_revision"] = "r"
            d["reranker_depth"] = 2
            d["reranker_duration_ms"] = 1
            d["reranker_fallback_reason"] = None
        return documents[:limit]

    monkeypatch.setattr(reranker, "should_rerank", lambda **k: (True, None))
    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    body = "COMPLETE_CHUNK_BODY_XYZ " * 5
    retrieval.retrieve(
        "What differs?",
        requested_profile="HYBRID",
        lexical_backend=lambda **k: [
            _hit("a", "a.md", 5.0, content=body),
            _hit("b", "b.md", 4.0, content="other full body"),
        ],
        dense_backend=lambda **k: [
            _hit("b", "b.md", 0.99, content="other full body"),
            _hit("a", "a.md", 0.1, content=body),
        ],
        rerank_enabled=True,
        corpus_generation="g",
    )
    assert any(body in (c or "") for c in seen)


def test_lance_distance_kept_separate_from_similarity():
    import lance_store

    rows = lance_store._rows_from_lance_hits(
        [{"path": "a.md", "title": "A", "summary": "s", "project": "", "timestamp": "", "_distance": 0.5}]
    )
    assert rows[0]["lance_distance"] == 0.5
    assert rows[0]["vector_score"] == pytest.approx(1.0 / 1.5)
    assert rows[0]["score"] == rows[0]["vector_score"]
    assert rows[0]["lance_distance"] != rows[0]["vector_score"]


def test_upsert_vectors_refuses_destructive_live_drop():
    import lance_store

    with pytest.raises(RuntimeError, match="immutable generation"):
        lance_store.upsert_vectors(
            ["a.md"], ["A"], ["s"], [""], [""], [[0.1] * lance_store.EMBEDDING_DIM]
        )


def test_retrieve_respects_deadline(monkeypatch):
    import retrieval

    def slow_lexical(**_k):
        time.sleep(0.05)
        return [_hit("a", "a.md", 1.0)]

    with pytest.raises(TimeoutError):
        retrieval.retrieve(
            "x",
            requested_profile="BASE",
            lexical_backend=slow_lexical,
            rerank_enabled=False,
            corpus_generation="g",
            deadline_monotonic=time.monotonic() + 0.001,
        )


def test_legacy_numpy_requires_model_revision_and_source_hashes(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    import search_memory

    cache = tmp_path / "cache"
    cache.mkdir()
    notes = tmp_path / "knowledge" / "notes"
    notes.mkdir(parents=True)
    (notes / "p.md").write_text("# P\nbody\n", encoding="utf-8")
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "KNOWLEDGE_DIR", notes)
    monkeypatch.setattr(search_memory, "WIKI_DIR", notes)
    monkeypatch.setattr(search_memory, "INDEX_DIR", cache)
    monkeypatch.setattr(search_memory, "INDEX_FILE", cache / "index.sqlite")
    monkeypatch.setattr(search_memory, "INDEX_MANIFEST", cache / ".paths-manifest")
    monkeypatch.setattr(search_memory, "VECTOR_NPY", cache / "vectors.npy")
    monkeypatch.setattr(search_memory, "VECTOR_META", cache / "vectors_meta.json")
    monkeypatch.setattr(search_memory, "_have_sentence_transformers", lambda: True)
    np.save(cache / "vectors.npy", np.ones((1, 2), dtype=np.float32))
    (cache / "vectors_meta.json").write_text(
        json.dumps(
            {
                "paths": ["knowledge/notes/p.md"],
                "titles": ["P"],
                "summaries": ["body"],
                "projects": [""],
                "timestamps": [""],
                "model": "m",
                # missing model_revision and source_sha256
                "dimensions": 2,
            }
        ),
        encoding="utf-8",
    )
    assert (
        search_memory._legacy_dense_hits(
            "body", scope="all", limit=5, project=None, since=None, as_of=None
        )
        is None
    )


@pytest.mark.parametrize(
    ("query", "profile"),
    [
        ("Что такое auth?", "HYBRID"),
        ("что зависит от search_memory", "GRAPH"),
        ("покажи карту репозитория", "REPO_MAP"),
        ("влияние search_memory.py", "IMPACT"),
        ("синтез across all projects", "GLOBAL"),
        ("решения с 2025-01-01", "TEMPORAL"),
        ('открой "auth decision"', "EXACT"),
        ("什么是 auth？", "HYBRID"),
        ("什么依赖 search_memory", "GRAPH"),
        ("显示仓库地图", "REPO_MAP"),
        ("更改的影响 search_memory.py", "IMPACT"),
        ("跨项目综合架构", "GLOBAL"),
        ("自 2025-01-01 以来的决策", "TEMPORAL"),
    ],
)
def test_ru_zh_profiles_behavioral(query, profile):
    import retrieval

    assert retrieval.analyze_query(query).recommended_profile == profile


def test_resource_limit_caps_backend_limit():
    import retrieval

    seen = []

    def lexical(**kwargs):
        seen.append(kwargs["limit"])
        return [_hit("a", "a.md", 1.0)]

    retrieval.retrieve(
        "x",
        requested_profile="BASE",
        limit=5000,
        max_candidates=25,
        lexical_backend=lexical,
        rerank_enabled=False,
        corpus_generation="g",
    )
    assert seen == [25]
