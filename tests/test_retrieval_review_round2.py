"""Second-pass review blockers for retrieval orchestration."""
from __future__ import annotations

import hashlib
import json
import sys
import threading
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
    from repository_scope import resolve_repository_scope

    sealed = []

    def fake_seal(catalog, manifest, artifact_names, **_kwargs):
        sealed.append(tuple(artifact_names))
        return ("seal", tuple(artifact_names))

    monkeypatch.setattr(search_memory, "_generation_consumption_seal", fake_seal)
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

        def get_active(self, **_kwargs):
            pytest.fail("repository retrieval must not invoke generic get_active repair")

        def get_active_for_repository(self, repository_scope, **_kwargs):
            assert repository_scope == resolve_repository_scope(search_memory.ROOT)
            return {
                "generation_id": "gen-v",
                "repository_scope": resolve_repository_scope(search_memory.ROOT).as_dict(),
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


@pytest.mark.parametrize("bind_generation", [True, False], ids=("foreign", "unbound"))
def test_wrong_repository_generation_cannot_contribute_any_signal(
    tmp_path, monkeypatch, bind_generation
):
    import evidence_graph
    import graph_neighbors
    import retrieval
    import search_memory
    from repository_scope import resolve_repository_scope

    repository_a = tmp_path / "first" / "same-name"
    repository_b = tmp_path / "second" / "same-name"
    repository_a.mkdir(parents=True)
    repository_b.mkdir(parents=True)
    monkeypatch.setattr(search_memory, "ROOT", repository_b)
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_seal",
        lambda *_args, **_kwargs: pytest.fail("wrong generation must not be sealed"),
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_unchanged",
        lambda *_args, **_kwargs: True,
    )

    monkeypatch.setattr(
        search_memory,
        "_generation_connection",
        lambda *_args, **_kwargs: pytest.fail("wrong generation FTS must not open"),
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_fts_search",
        lambda *_args, **_kwargs: pytest.fail("wrong generation lexical signal used"),
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_vectors_search",
        lambda *_args, **_kwargs: pytest.fail("wrong generation vector signal used"),
    )
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: [_hit("local", "local.md", 1.0)],
    )
    monkeypatch.setattr(
        search_memory,
        "_legacy_dense_hits",
        lambda *_args, **_kwargs: [_hit("local", "local.md", 0.9)],
    )
    monkeypatch.setattr(graph_neighbors, "boost_graph_neighbors", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        retrieval,
        "expand_evidence_graph",
        lambda *_args, **_kwargs: pytest.fail("wrong generation graph signal used"),
    )

    class FakeEvidenceGraph:
        def __init__(self, repository_scope):
            self.repository_scope = repository_scope

        @classmethod
        def open_active_for_repository(
            cls, catalog, repository_scope, *, deadline=None, cancelled=None
        ):
            manifest = catalog.get_active(deadline=deadline, cancelled=cancelled)
            if manifest.get("repository_scope") != repository_scope.as_dict():
                return None
            return cls(repository_scope)

        def close(self):
            return None

    monkeypatch.setattr(evidence_graph, "EvidenceGraph", FakeEvidenceGraph)

    class Catalog:
        generations_path = tmp_path / "generations"
        state_root = tmp_path / "state"

        def get_active_for_repository(
            self, _repository_scope, *, deadline=None, cancelled=None
        ):
            manifest = {
                "generation_id": "repository-a",
                "vector_state": "complete",
                "embedding_model_id": "model",
                "embedding_model_revision": "revision",
                "vector_dimensions": 2,
                "artifacts": [
                    {"path": "search.sqlite3", "size": 1, "sha256": "0" * 64},
                    {"path": "evidence.sqlite3", "size": 1, "sha256": "1" * 64},
                    {"path": "vectors.npy", "size": 1, "sha256": "2" * 64},
                    {"path": "vectors.json", "size": 1, "sha256": "3" * 64},
                ],
                "source_manifest_sha256": "a" * 64,
                "collector_version": "c",
                "extractor_version": "e",
                "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
                "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
                "schema_version": "corpus-generation/v1",
            }
            if bind_generation:
                manifest["repository_scope"] = resolve_repository_scope(
                    repository_a, deadline=deadline, cancelled=cancelled
                ).as_dict()
            return None

    rows = retrieval.retrieve_via_search_memory(
        "needle",
        catalog=Catalog(),
        semantic=True,
        generation_embedder=lambda _texts: [[1.0, 0.0]],
        generation_model_id="model",
        generation_model_revision="revision",
        graph=True,
        rerank=False,
        emit_telemetry=False,
        profile="GLOBAL",
    )

    assert [row["candidate_id"] for row in rows] == ["local"]
    assert rows[0]["generation"] == "legacy"
    assert rows[0]["fallback_reason"] == "generation_unavailable"


def test_repository_retrieval_selects_scope_before_generic_active_repair(
    tmp_path, monkeypatch
):
    import retrieval
    import search_memory
    from repository_scope import resolve_repository_scope

    repository = tmp_path / "repository"
    repository.mkdir()
    scope = resolve_repository_scope(repository)
    monkeypatch.setattr(search_memory, "ROOT", repository)
    monkeypatch.setattr(search_memory, "_legacy_lexical_hits", lambda *_a, **_k: [])

    class Catalog:
        def get_active(self, **_kwargs):
            pytest.fail("repository retrieval must not invoke generic get_active repair")

        def get_active_for_repository(self, requested_scope, **_kwargs):
            assert requested_scope == scope
            return None

    assert retrieval.retrieve_via_search_memory(
        "needle",
        catalog=Catalog(),
        semantic=False,
        graph=False,
        rerank=False,
        emit_telemetry=False,
    ) == []


@pytest.mark.parametrize(
    "stage",
    ("catalog", "seal", "fts_open", "fts_search", "graph_open", "vector_search", "seal_recheck"),
)
def test_public_search_propagates_generation_stage_timeout(
    tmp_path, monkeypatch, stage
):
    import evidence_graph
    import retrieval
    import search_memory
    from repository_scope import resolve_repository_scope

    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(search_memory, "ROOT", root)
    deadline = time.monotonic() + 30

    def cancelled():
        return False

    seen: list[tuple[str, float | None, object]] = []

    def stop(name, received_deadline=None, received_cancelled=None):
        seen.append((name, received_deadline, received_cancelled))
        if name == stage:
            raise TimeoutError(f"{name} timed out")

    manifest = {
        "generation_id": "scoped",
        "repository_scope": resolve_repository_scope(root).as_dict(),
        "vector_state": "complete",
        "embedding_model_id": "model",
        "embedding_model_revision": "revision",
        "vector_dimensions": 2,
        "artifacts": [
            {"path": "search.sqlite3", "size": 1, "sha256": "0" * 64},
            {"path": "evidence.sqlite3", "size": 1, "sha256": "1" * 64},
            {"path": "vectors.npy", "size": 1, "sha256": "2" * 64},
            {"path": "vectors.json", "size": 1, "sha256": "3" * 64},
        ],
        "source_manifest_sha256": "a" * 64,
        "collector_version": "collector",
        "extractor_version": "extractor",
        "tokenizer_version": search_memory.GENERATION_TOKENIZER_VERSION,
        "tokenizer_config_sha256": search_memory.GENERATION_TOKENIZER_CONFIG_SHA256,
        "schema_version": "corpus-generation/v1",
    }

    class Catalog:
        generations_path = tmp_path / "generations"
        state_root = tmp_path / "state"

        def get_active_for_repository(
            self, _repository_scope, *, deadline=None, cancelled=None
        ):
            stop("catalog", deadline, cancelled)
            return manifest

    class Connection:
        def close(self):
            pass

    class Graph:
        generation_id = "scoped"

        def __init__(self, repository_scope):
            self.repository_scope = repository_scope

        @classmethod
        def open_active_for_repository(
            cls, _catalog, repository_scope, *, deadline=None, cancelled=None
        ):
            stop("graph_open", deadline, cancelled)
            assert repository_scope == resolve_repository_scope(root)
            return cls(repository_scope)

        def close(self):
            pass

    def seal(_catalog, _manifest, _names, *, deadline=None, cancelled=None):
        stop("seal", deadline, cancelled)
        return ("sealed",)

    def unchanged(
        _catalog, _manifest, _names, _seal, *, deadline=None, cancelled=None
    ):
        stop("seal_recheck", deadline, cancelled)
        return True

    def connection(_catalog, _manifest, *, deadline=None, cancelled=None):
        stop("fts_open", deadline, cancelled)
        return Connection()

    def fts(*_args, deadline=None, cancelled=None, **_kwargs):
        stop("fts_search", deadline, cancelled)
        return [_hit("local", "local.md", 1.0)]

    def vectors(*_args, deadline=None, cancelled=None, **_kwargs):
        stop("vector_search", deadline, cancelled)
        return [_hit("local", "local.md", 0.9)]

    monkeypatch.setattr(evidence_graph, "EvidenceGraph", Graph)
    monkeypatch.setattr(search_memory, "_generation_consumption_seal", seal)
    monkeypatch.setattr(search_memory, "_generation_consumption_unchanged", unchanged)
    monkeypatch.setattr(search_memory, "_generation_connection", connection)
    monkeypatch.setattr(search_memory, "_generation_fts_search", fts)
    monkeypatch.setattr(search_memory, "_generation_vectors_search", vectors)
    monkeypatch.setattr(retrieval, "expand_evidence_graph", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: pytest.fail("timeout must not become legacy fallback"),
    )

    search_options = dict(
        catalog=Catalog(),
        semantic=True,
        generation_embedder=lambda _texts: [[1.0, 0.0]],
        generation_model_id="model",
        generation_model_revision="revision",
        graph=True,
        rerank=False,
        emit_telemetry=False,
        profile="GLOBAL",
        deadline_monotonic=deadline,
        cancelled=cancelled,
    )
    with pytest.raises(TimeoutError, match=stage):
        search_memory.search("needle", **search_options)

    expected_stop = (
        (stage, None, None)
        if stage == "vector_search"
        else (stage, deadline, cancelled)
    )
    assert expected_stop in seen


@pytest.mark.parametrize("stop", ["deadline", "cancelled"])
def test_public_search_rejects_expired_or_cancelled_request(stop):
    import search_memory

    options = (
        {"deadline_monotonic": time.monotonic() - 1}
        if stop == "deadline"
        else {"cancelled": lambda: True}
    )
    with pytest.raises(TimeoutError, match=stop):
        search_memory.search("needle", emit_telemetry=False, **options)


@pytest.mark.parametrize("stage", ["lexical"])
def test_public_search_threads_deadline_and_cancel_into_legacy_backends(
    monkeypatch, stage
):
    import search_memory

    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    deadline = time.monotonic() + 30

    def cancelled():
        return False

    def lexical(*_args, deadline=None, cancelled=None, **_kwargs):
        assert deadline == test_deadline
        assert cancelled is test_cancelled
        if stage == "lexical":
            raise TimeoutError("legacy lexical deadline")
        return [_hit("local", "local.md", 1.0)]

    def dense(*_args, deadline=None, cancelled=None, **_kwargs):
        assert deadline == test_deadline
        assert cancelled is test_cancelled
        raise TimeoutError("legacy dense deadline")

    test_deadline = deadline
    test_cancelled = cancelled
    monkeypatch.setattr(search_memory, "_legacy_lexical_hits", lexical)
    monkeypatch.setattr(search_memory, "_legacy_dense_hits", dense)

    with pytest.raises(TimeoutError, match=f"legacy {stage} deadline"):
        search_memory.search(
            "needle",
            semantic=True,
            graph=False,
            rerank=False,
            emit_telemetry=False,
            profile="HYBRID",
            deadline_monotonic=deadline,
            cancelled=cancelled,
        )


def test_deadline_allows_hybrid_signals_when_dense_finishes_in_budget(monkeypatch):
    import search_memory

    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: [_hit("local", "local.md", 1.0)],
    )
    monkeypatch.setattr(
        search_memory,
        "_legacy_dense_hits",
        lambda *_args, **_kwargs: [_hit("dense", "dense.md", 0.9)],
    )

    rows = search_memory.search(
        "needle",
        semantic=True,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="HYBRID",
        deadline_monotonic=time.monotonic() + 30,
    )

    assert {row["candidate_id"] for row in rows} == {"local", "dense"}
    assert rows[0]["requested_mode"] == "HYBRID"
    assert rows[0]["effective_mode"] == "HYBRID"
    assert rows[0]["signals_used"] == ["lexical", "dense"]
    assert rows[0]["fallback_reason"] is None
    assert rows[0]["partial"] is False


def test_hanging_dense_backend_falls_back_to_partial_base(monkeypatch):
    import retrieval
    import search_memory

    release = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(retrieval, "OPTIONAL_STAGE_MAX_SECONDS", 0.02)
    monkeypatch.setattr(
        retrieval,
        "_OPTIONAL_STAGE_SLOTS",
        threading.BoundedSemaphore(retrieval.MAX_OPTIONAL_STRAGGLERS),
    )
    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: [_hit("local", "local.md", 1.0)],
    )

    def hanging(*_args, **_kwargs):
        started.set()
        release.wait()
        return [_hit("dense", "dense.md", 0.9)]

    monkeypatch.setattr(search_memory, "_legacy_dense_hits", hanging)
    try:
        rows = search_memory.search(
            "needle",
            semantic=True,
            graph=False,
            rerank=False,
            emit_telemetry=False,
            profile="HYBRID",
            deadline_monotonic=time.monotonic() + 1,
        )

        assert started.is_set()
        assert [row["candidate_id"] for row in rows] == ["local"]
        assert rows[0]["effective_mode"] == "BASE"
        assert rows[0]["fallback_reason"] == "optional_stage_timeout"
        assert rows[0]["partial"] is True
    finally:
        release.set()


def test_optional_boundary_caps_concurrent_stragglers(monkeypatch):
    import retrieval

    release = threading.Event()
    started = 0
    lock = threading.Lock()
    monkeypatch.setattr(retrieval, "OPTIONAL_STAGE_MAX_SECONDS", 0.01)
    monkeypatch.setattr(
        retrieval,
        "_OPTIONAL_STAGE_SLOTS",
        threading.BoundedSemaphore(retrieval.MAX_OPTIONAL_STRAGGLERS),
    )

    def hanging():
        nonlocal started
        with lock:
            started += 1
        release.wait()

    try:
        for _ in range(retrieval.MAX_OPTIONAL_STRAGGLERS + 1):
            with pytest.raises(retrieval.OptionalStageTimeout):
                retrieval._run_optional_bounded(
                    hanging, deadline=time.monotonic() + 1, cancelled=None
                )
        assert started == retrieval.MAX_OPTIONAL_STRAGGLERS
    finally:
        release.set()


def test_optional_boundary_releases_capacity_when_thread_start_fails(monkeypatch):
    import retrieval

    real_thread = retrieval.threading.Thread
    starts = 0

    class FailingThenRealThread:
        def __init__(self, *args, **kwargs):
            self._thread = real_thread(*args, **kwargs)

        def start(self):
            nonlocal starts
            starts += 1
            if starts <= retrieval.MAX_OPTIONAL_STRAGGLERS:
                raise RuntimeError("thread startup failed")
            self._thread.start()

    monkeypatch.setattr(retrieval.threading, "Thread", FailingThenRealThread)
    monkeypatch.setattr(
        retrieval,
        "_OPTIONAL_STAGE_SLOTS",
        threading.BoundedSemaphore(retrieval.MAX_OPTIONAL_STRAGGLERS),
    )

    for _ in range(retrieval.MAX_OPTIONAL_STRAGGLERS):
        with pytest.raises(RuntimeError, match="thread startup failed"):
            retrieval._run_optional_bounded(
                lambda: "unused", deadline=time.monotonic() + 1, cancelled=None
            )

    assert retrieval._run_optional_bounded(
        lambda: "started", deadline=time.monotonic() + 1, cancelled=None
    ) == "started"


def test_optional_boundary_releases_the_acquired_semaphore_after_rebind(monkeypatch):
    import retrieval

    for _ in range(5):
        acquired_slots = threading.BoundedSemaphore(1)
        rebound_slots = threading.BoundedSemaphore(1)
        release = threading.Event()
        finished = threading.Event()
        monkeypatch.setattr(retrieval, "OPTIONAL_STAGE_MAX_SECONDS", 0.005)
        monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_SLOTS", acquired_slots)

        def straggler():
            try:
                release.wait()
            finally:
                finished.set()

        with pytest.raises(retrieval.OptionalStageTimeout):
            retrieval._run_optional_bounded(
                straggler, deadline=time.monotonic() + 1, cancelled=None
            )
        monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_SLOTS", rebound_slots)
        release.set()
        assert finished.wait(1)
        assert acquired_slots.acquire(blocking=False)
        assert rebound_slots.acquire(blocking=False)


def test_optional_boundary_start_failure_releases_acquired_semaphore_after_rebind(
    monkeypatch,
):
    import retrieval

    acquired_slots = threading.BoundedSemaphore(1)
    rebound_slots = threading.BoundedSemaphore(1)

    class RebindingFailedThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_SLOTS", rebound_slots)
            raise RuntimeError("thread startup failed")

    monkeypatch.setattr(retrieval, "_OPTIONAL_STAGE_SLOTS", acquired_slots)
    monkeypatch.setattr(retrieval.threading, "Thread", RebindingFailedThread)

    with pytest.raises(RuntimeError, match="thread startup failed"):
        retrieval._run_optional_bounded(
            lambda: None, deadline=time.monotonic() + 1, cancelled=None
        )
    assert acquired_slots.acquire(blocking=False)
    assert rebound_slots.acquire(blocking=False)


def test_explicit_base_under_deadline_is_not_reported_as_fallback(monkeypatch):
    import search_memory

    monkeypatch.setattr(search_memory, "_active_generation_catalog", lambda: None)
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: [_hit("local", "local.md", 1.0)],
    )

    rows = search_memory.search(
        "needle",
        semantic=False,
        graph=False,
        rerank=False,
        emit_telemetry=False,
        profile="BASE",
        deadline_monotonic=time.monotonic() + 30,
    )

    assert rows[0]["requested_mode"] == "BASE"
    assert rows[0]["effective_mode"] == "BASE"
    assert rows[0]["fallback_reason"] is None
    assert rows[0]["partial"] is False


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


def test_exact_filename_stays_rank_one_after_dense_fusion():
    import retrieval

    exact = _hit(
        "exact",
        "knowledge/notes/Target_Contract.md",
        1.0,
        title="Unrelated heading",
    )
    distractor = _hit(
        "distractor",
        "knowledge/notes/distractor.md",
        0.9,
        title="Target Contract",
    )

    result = retrieval.retrieve(
        "target contract",
        requested_profile="HYBRID",
        lexical_backend=lambda **_kwargs: [exact, distractor],
        dense_backend=lambda **_kwargs: [distractor],
        graph_enabled=False,
        rerank_enabled=False,
    )

    assert result.candidates[0].relative_path == "knowledge/notes/Target_Contract.md"


def test_exact_filename_stays_rank_one_after_reranking(monkeypatch):
    import reranker
    import retrieval

    exact = _hit(
        "exact",
        "knowledge/notes/Target_Contract.md",
        1.0,
        title="Unrelated heading",
    )
    distractor = _hit(
        "distractor",
        "knowledge/notes/distractor.md",
        0.9,
        title="Other heading",
    )

    def force_distractor_first(_query, documents, **_kwargs):
        by_id = {row["candidate_id"]: dict(row) for row in documents}
        rows = [by_id["distractor"], by_id["exact"]]
        for index, row in enumerate(rows):
            row.update(
                {
                    "final_score": 2.0 - index,
                    "rerank_score": 2.0 - index,
                    "reranker_applied": True,
                    "reranker_model_id": "test-reranker",
                    "reranker_model_revision": "1",
                    "reranker_depth": 2,
                    "reranker_duration_ms": 1,
                }
            )
        return rows

    monkeypatch.setattr(reranker, "should_rerank", lambda **_kwargs: (True, None))
    monkeypatch.setattr(reranker, "rerank", force_distractor_first)

    result = retrieval.retrieve(
        "target contract",
        requested_profile="HYBRID",
        lexical_backend=lambda **_kwargs: [exact, distractor],
        dense_backend=lambda **_kwargs: [exact, distractor],
        graph_enabled=False,
        rerank_enabled=True,
    )

    assert result.candidates[0].relative_path == "knowledge/notes/Target_Contract.md"


def test_reranker_receives_full_chunk_content(monkeypatch):
    import reranker
    import retrieval

    seen = []
    options = []

    def fake_rerank(query, documents, limit=10, **kwargs):
        seen.extend([d.get("content") for d in documents])
        options.append(kwargs)
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
    assert options == [{"text_field": "content"}]


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


def _paths_of(result) -> list[str]:
    return [candidate.relative_path for candidate in result.candidates]


def _label_of(result) -> tuple:
    """The four fields a caller reads together to know an answer is short."""
    return (
        result.trace.partial,
        result.trace.fallback_reason,
        result.trace.signals_used,
        result.trace.effective_mode,
    )


def _noting_leg(calls: list[str], name: str, cid: str, path: str):
    def leg(**_kwargs):
        calls.append(name)
        return [_hit(cid, path, 1.0)]

    return leg


def _slow_lexical_leg(calls: list[str]):
    """Finishes, and leaves no budget behind it."""
    inner = _noting_leg(calls, "lexical", "a", "a.md")

    def leg(**kwargs):
        time.sleep(0.05)
        return inner(**kwargs)

    return leg


def test_retrieve_respects_deadline(monkeypatch):
    """The deadline still binds -- and the finished leg is handed back, labelled.

    This used to assert `TimeoutError`. The deadline is enforced in exactly the
    same place: the check after the lexical leg fires, and nothing further is
    started. What changed is what happens to the leg that already finished --
    it is returned as a partial answer instead of being discarded. Measured on
    this vault, discarding it was the whole reason a loaded MCP call returned
    nothing at all.
    """
    import retrieval

    calls: list[str] = []
    result = retrieval.retrieve(
        "x",
        requested_profile="HYBRID",
        lexical_backend=_slow_lexical_leg(calls),
        dense_backend=_noting_leg(calls, "dense", "b", "b.md"),
        rerank_enabled=False,
        corpus_generation="g",
        deadline_monotonic=time.monotonic() + 0.001,
    )

    assert calls == ["lexical"]
    assert _paths_of(result) == ["a.md"]
    assert _label_of(result) == (
        True,
        "deadline_expired_partial_result",
        ("lexical",),
        "BASE",
    )


def test_retrieve_raises_when_the_deadline_leaves_nothing_in_hand():
    """Nothing finished is a refusal, and it must keep looking like one."""
    import retrieval

    with pytest.raises(TimeoutError, match="deadline exceeded"):
        retrieval.retrieve(
            "x",
            requested_profile="BASE",
            lexical_backend=lambda **_k: [_hit("a", "a.md", 1.0)],
            rerank_enabled=False,
            corpus_generation="g",
            deadline_monotonic=time.monotonic() - 1,
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


def test_resource_limit_is_global_before_rerank(monkeypatch):
    import reranker
    import retrieval

    rerank_sizes = []

    def backend(prefix):
        return lambda **kwargs: [
            _hit(f"{prefix}-{index}", f"{prefix}-{index}.md", 10.0 - index)
            for index in range(kwargs["limit"])
        ]

    def fake_rerank(_query, documents, **_kwargs):
        rerank_sizes.append(len(documents))
        return documents

    monkeypatch.setattr(reranker, "should_rerank", lambda **_kwargs: (True, None))
    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    result = retrieval.retrieve(
        "x",
        requested_profile="GLOBAL",
        limit=10,
        max_candidates=3,
        lexical_backend=backend("lex"),
        dense_backend=backend("dense"),
        graph_backend=backend("graph"),
        rerank_enabled=True,
        corpus_generation="g",
    )

    assert rerank_sizes == [3]
    assert len(result.candidates) == 3


def test_public_search_propagates_resource_controls(monkeypatch):
    import retrieval
    import search_memory

    seen = {}

    def cancelled():
        return False

    deadline = time.monotonic() + 30

    def fake_retrieve(_query, **kwargs):
        seen.update(kwargs)
        return retrieval.RetrievalResult(
            candidates=(),
            trace=retrieval.RetrievalTrace(
                requested_mode="BASE",
                effective_mode="BASE",
                signals_used=("lexical",),
                fallback_reason=None,
                corpus_generation="legacy",
                partial=False,
            ),
            analysis=retrieval.analyze_query("needle"),
        )

    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)
    search_memory.search(
        "needle",
        deadline_monotonic=deadline,
        max_candidates=7,
        cancelled=cancelled,
        emit_telemetry=False,
    )

    assert seen["deadline_monotonic"] == deadline
    assert seen["max_candidates"] == 7
    assert seen["cancelled"] is cancelled


def test_cancellation_is_checked_after_fusion_before_rerank(monkeypatch):
    import retrieval

    stopped = False
    real_fuse = retrieval.fuse_rrf

    def fuse_then_cancel(**kwargs):
        nonlocal stopped
        result = real_fuse(**kwargs)
        stopped = True
        return result

    reranked = []

    monkeypatch.setattr(retrieval, "fuse_rrf", fuse_then_cancel)
    monkeypatch.setattr(
        retrieval, "_maybe_rerank", lambda *a, **k: reranked.append(True)
    )
    result = retrieval.retrieve(
        "needle",
        requested_profile="BASE",
        lexical_backend=lambda **_kwargs: [_hit("a", "a.md", 1.0)],
        rerank_enabled=True,
        cancelled=lambda: stopped,
    )

    # The cancel is still checked between fusion and rerank -- the reranker
    # never runs. What changed is that the fused set is returned rather than
    # thrown away, labelled with the cancel that stopped the plan.
    assert reranked == []
    assert [candidate.relative_path for candidate in result.candidates] == ["a.md"]
    assert result.trace.partial is True
    assert result.trace.fallback_reason == "cancelled_partial_result"


@pytest.mark.parametrize("vectors_returned", [True, False])
def test_late_vector_seal_change_discards_generation_lexical(
    monkeypatch, vectors_returned
):
    import retrieval
    import search_memory
    from repository_scope import resolve_repository_scope

    checks = iter((True, True, True, False))
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_unchanged",
        lambda *_args, **_kwargs: next(checks),
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_consumption_seal",
        lambda *_args, **_kwargs: ("sealed",),
    )

    class Connection:
        def close(self):
            return None

    monkeypatch.setattr(
        search_memory, "_generation_connection", lambda *_args, **_kwargs: Connection()
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_fts_search",
        lambda *_args, **_kwargs: [_hit("generation", "generation.md", 10.0)],
    )
    monkeypatch.setattr(
        search_memory,
        "_generation_vectors_search",
        lambda *_args, **_kwargs: (
            [_hit("generation", "generation.md", 0.9)] if vectors_returned else None
        ),
    )
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *_args, **_kwargs: [_hit("legacy", "legacy.md", 1.0)],
    )
    monkeypatch.setattr(search_memory, "_legacy_dense_hits", lambda *_args, **_kwargs: None)

    class Catalog:
        generations_path = Path(".")

        def get_active_for_repository(self, _repository_scope, **_kwargs):
            return {
                "generation_id": "gen-v",
                "repository_scope": resolve_repository_scope(search_memory.ROOT).as_dict(),
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
        "needle",
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

    assert [row["candidate_id"] for row in rows] == ["legacy"]
    assert rows[0]["generation"] == "legacy"
    assert rows[0]["fallback_reason"] == "generation_seal_changed"


def test_legacy_numpy_writer_and_loader_share_closed_contract(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    import search_memory

    page = tmp_path / "knowledge" / "notes" / "p.md"
    page.parent.mkdir(parents=True)
    page.write_text("# P\nbody\n", encoding="utf-8")
    cache = tmp_path / "cache"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", cache)
    monkeypatch.setattr(search_memory, "VECTOR_NPY", cache / "vectors.npy")
    monkeypatch.setattr(search_memory, "VECTOR_META", cache / "vectors_meta.json")
    monkeypatch.setattr(search_memory, "EMBEDDING_DIM", 2)
    monkeypatch.setattr(search_memory, "EMBEDDING_MODEL_REVISION", "revision-1")

    class Embedder:
        def encode(self, texts, **_kwargs):
            return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setattr(search_memory, "_get_embedder", lambda: Embedder())
    built = search_memory._build_vectors([page])
    metadata = json.loads((cache / "vectors_meta.json").read_text(encoding="utf-8"))

    assert isinstance(built["vectors"], np.ndarray)
    assert metadata["model_id"] == search_memory.EMBEDDING_MODEL
    assert metadata["model_revision"] == "revision-1"
    assert metadata["dimensions"] == 2
    assert metadata["source_paths"] == ["knowledge/notes/p.md"]
    assert metadata["source_sha256"] == [hashlib.sha256(page.read_bytes()).hexdigest()]
    assert metadata["dtype"] == "float32"
    assert metadata["shape"] == [1, 2]
    assert metadata["finite"] is True
    assert metadata["artifact_sha256"] == hashlib.sha256(
        (cache / "vectors.npy").read_bytes()
    ).hexdigest()

    loaded = search_memory._load_or_build_vectors([page])
    assert isinstance(loaded["vectors"], np.memmap)


def test_legacy_numpy_loader_rejects_changed_live_source(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    import search_memory

    page = tmp_path / "knowledge" / "notes" / "p.md"
    page.parent.mkdir(parents=True)
    page.write_text("# P\nbefore\n", encoding="utf-8")
    cache = tmp_path / "cache"
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    monkeypatch.setattr(search_memory, "INDEX_DIR", cache)
    monkeypatch.setattr(search_memory, "VECTOR_NPY", cache / "vectors.npy")
    monkeypatch.setattr(search_memory, "VECTOR_META", cache / "vectors_meta.json")
    monkeypatch.setattr(search_memory, "EMBEDDING_DIM", 2)
    monkeypatch.setattr(search_memory, "EMBEDDING_MODEL_REVISION", "revision-1")

    class Embedder:
        def encode(self, texts, **_kwargs):
            return np.ones((len(texts), 2), dtype=np.float32)

    monkeypatch.setattr(search_memory, "_get_embedder", lambda: Embedder())
    assert search_memory._build_vectors([page]) is not None
    page.write_text("# P\nafter with preserved metadata\n", encoding="utf-8")
    monkeypatch.setattr(
        search_memory,
        "_build_vectors",
        lambda _pages: pytest.fail("invalid cache must be unavailable, not silently rebuilt"),
    )

    assert search_memory._load_or_build_vectors([page]) is None
