"""Task 26: bounded Evidence Graph generation maintenance."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _empty_generation(state: Path, generation_id: str, *, parent: str | None = None):
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog

    catalog = GenerationCatalog(state)
    return build_full_generation(
        catalog,
        sources=(),
        source_bytes={},
        nodes=(),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        generation_id=generation_id,
        parent_generation_id=parent,
        expected_active=parent,
    )


def test_generation_check_validates_active_artifacts_and_reports_source_delta(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    _empty_generation(state, "gen-1")
    now = datetime.now(timezone.utc)

    healthy = doctor._generation_check(
        root, state, now, deadline=time.monotonic() + 5, max_sources=10
    )

    assert healthy["status"] == "ok"
    assert (
        healthy["details"]
        | {
            "catalog": "valid",
            "active_generation": "gen-1",
            "generation_schema": "evidence-graph/v1",
            "source_manifest": "valid",
            "evidence_integrity": "valid",
            "vector_state": "absent",
            "vector_model": None,
            "vector_dimensions": None,
            "unindexed_delta": 0,
            "unresolved_observations": 0,
        }
        == healthy["details"]
    )
    assert healthy["details"]["age_seconds"] >= 0
    assert healthy["details"]["age_source"] == "manifest_mtime"

    (root / "knowledge" / "notes" / "new.md").write_text(
        "---\ntype: concept\n---\n# New\n", encoding="utf-8"
    )
    stale = doctor._generation_check(
        root, state, now, deadline=time.monotonic() + 5, max_sources=10
    )

    assert stale["status"] == "degraded"
    assert stale["details"]["freshness"] == "stale"
    assert stale["details"]["unindexed_delta"] == 1


def test_generation_check_distinguishes_not_built_from_invalid_active(tmp_path):
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    before = {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")}

    missing = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
        max_sources=10,
    )

    assert missing["status"] == "ok"
    assert missing["details"]["catalog"] == "missing"
    assert missing["details"]["freshness"] == "missing"
    assert missing["details"]["repairable"] is True
    assert missing["details"]["recommended_action"] == "rebuild_generation"
    assert missing["details"]["age_seconds"] is None
    assert missing["details"]["age_source"] is None
    assert {path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")} == before

    catalog = GenerationCatalog(state)
    empty = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
        max_sources=10,
    )

    assert empty["status"] == "ok"
    assert empty["details"]["catalog"] == "valid"
    assert empty["details"]["active_generation"] is None
    assert empty["details"]["repairable"] is True
    assert empty["details"]["recommended_action"] == "rebuild_generation"

    with sqlite3.connect(catalog.catalog_path) as database:
        database.execute(
            "UPDATE catalog_state SET active_generation_id = ? WHERE singleton = 1",
            ("missing-generation",),
        )

    invalid = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
        max_sources=10,
    )

    assert invalid["status"] == "error"
    assert invalid["details"]["catalog"] == "invalid"


def test_generation_repair_recovers_valid_orphan_cleans_partial_and_falls_back(tmp_path):
    import doctor
    from evidence_graph_builder import KillPointError, build_full_generation
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    first = _empty_generation(state, "gen-1")
    catalog = GenerationCatalog(state)
    try:
        build_full_generation(
            catalog,
            sources=(),
            source_bytes={},
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="valid-orphan",
            parent_generation_id="gen-1",
            expected_active="gen-1",
            kill_point="after_validation",
        )
    except KillPointError:
        pass
    partial = catalog.generations_path / "partial-orphan"
    partial.mkdir()
    (partial / "partial.tmp").write_text("incomplete", encoding="utf-8")

    second = _empty_generation(state, "gen-2", parent="gen-1")
    (second.generation_path / "evidence.sqlite3").write_bytes(b"corrupt")
    repaired: list[dict] = []

    doctor._repair_generation_catalog(
        root,
        state,
        deadline=time.monotonic() + 5,
        cancelled=lambda: False,
        repaired=repaired,
    )

    assert catalog.get_active()["generation_id"] == "gen-1"
    assert first.generation_path.exists()
    assert (catalog.generations_path / "valid-orphan").exists()
    assert not partial.exists()
    assert {item["action"] for item in repaired} >= {
        "recover_generation_orphans",
        "cleanup_generation_orphans",
        "fallback_generation",
    }


def test_generation_repair_does_not_create_an_empty_catalog(tmp_path):
    import doctor

    root, state = _vault(tmp_path)

    doctor._repair_generation_catalog(
        root,
        state,
        deadline=time.monotonic() + 5,
        cancelled=lambda: False,
        repaired=[],
    )

    assert not (state / "cache/evidence-graph/catalog.sqlite3").exists()


def test_bounded_builder_builds_then_defers_when_source_limit_is_exceeded(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    before_knowledge = list((root / "knowledge").rglob("*"))

    built = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=5,
        max_sources=10,
    )

    assert built["status"] == "built"
    assert built["partial"] is False
    assert before_knowledge == list((root / "knowledge").rglob("*"))

    current = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=5,
        max_sources=10,
    )

    assert current["status"] == "current"
    assert current["generation_id"] == built["generation_id"]

    for name in ("one.md", "two.md"):
        (root / "knowledge" / "notes" / name).write_text(
            f"---\ntype: concept\n---\n# {name}\n", encoding="utf-8"
        )
    deferred = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=5,
        max_sources=1,
    )

    assert deferred["status"] == "deferred"
    assert deferred["partial"] is True
    assert deferred["reason"] == "source_limit"
    assert not any(path.name.endswith(".lock") for path in state.rglob("*"))


def test_generation_catalog_remains_rollback_journal_full_sync(tmp_path):
    root, state = _vault(tmp_path)
    del root
    _empty_generation(state, "gen-1")

    with sqlite3.connect(state / "cache/evidence-graph/catalog.sqlite3") as database:
        assert database.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "delete"
        assert database.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_nightly_uses_shared_generation_builder_not_process_local_graphs(monkeypatch):
    import scheduled_nightly

    calls = []
    monkeypatch.setattr(
        scheduled_nightly,
        "run_generation_maintenance",
        lambda **kwargs: (
            calls.append(kwargs) or {"status": "built", "generation_id": "gen-1", "partial": False}
        ),
    )

    result = scheduled_nightly._refresh_generation(lambda message: calls.append(message))

    assert result == 0
    assert any(isinstance(item, dict) and item["max_sources"] > 0 for item in calls)
    source = Path(scheduled_nightly.__file__).read_text(encoding="utf-8")
    assert "rebuild_graph_cache" not in source
    assert "index_directory(ROOT" not in source


def test_sync_check_reports_stale_generation_and_apply_uses_shared_builder(tmp_path, monkeypatch):
    import sync_memory

    calls = []
    generation = {
        "id": "generation",
        "status": "degraded",
        "message": "Generation is stale.",
        "details": {"freshness": "stale", "repairable": True},
    }
    index = {
        "id": "index",
        "status": "ok",
        "message": "Index is fresh.",
        "details": {"freshness": "fresh", "repairable": False},
    }
    report = {
        "overall_status": "degraded",
        "repaired": [],
        "checks": [
            {"id": name, "status": "ok", "message": "ok", "details": {}}
            for name in (
                "environment",
                "runtime",
                "filesystem",
                "integrations",
                "transactions",
                "queue",
            )
        ]
        + [generation, index],
    }
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(
        sync_memory,
        "_dependency_action",
        lambda **kwargs: {
            "id": "dependencies",
            "status": "ok",
            "message": "ok",
            "details": {},
        },
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_generation_builder",
        lambda **kwargs: (
            calls.append(kwargs)
            or {
                "id": "indexes",
                "status": "changed",
                "message": "Generation refreshed.",
                "details": {"generation": "gen-2", "partial": False},
            }
        ),
    )

    checked = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=False)
    applied = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    checked_index = next(item for item in checked["actions"] if item["id"] == "indexes")
    applied_index = next(item for item in applied["actions"] if item["id"] == "indexes")
    assert checked_index["status"] == "skipped"
    assert checked_index["details"]["checks"]["generation"] == "degraded"
    assert applied_index["status"] == "changed"
    assert calls and calls[0]["max_sources"] > 0


def test_sync_apply_refreshes_generation_and_legacy_index_when_both_are_stale(
    tmp_path, monkeypatch
):
    import sync_memory

    calls = []
    report = {
        "overall_status": "degraded",
        "repaired": [],
        "checks": [
            {
                "id": name,
                "status": "ok",
                "message": "ok",
                "details": {},
            }
            for name in (
                "environment",
                "runtime",
                "filesystem",
                "integrations",
                "transactions",
                "queue",
            )
        ]
        + [
            {
                "id": "generation",
                "status": "degraded",
                "message": "stale",
                "details": {"freshness": "stale", "repairable": True},
            },
            {
                "id": "index",
                "status": "degraded",
                "message": "stale",
                "details": {"freshness": "stale", "repairable": True},
            },
        ],
    }
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(
        sync_memory,
        "_dependency_action",
        lambda **kwargs: sync_memory._result("dependencies", "ok", "ok", {}),
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_generation_builder",
        lambda **kwargs: (
            calls.append("generation")
            or sync_memory._result("indexes", "changed", "generation", {})
        ),
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_index_builder",
        lambda **kwargs: (
            calls.append("index") or sync_memory._result("indexes", "changed", "index", {})
        ),
    )

    applied = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    action = next(item for item in applied["actions"] if item["id"] == "indexes")
    assert calls == ["generation", "index"]
    assert action["status"] == "changed"
