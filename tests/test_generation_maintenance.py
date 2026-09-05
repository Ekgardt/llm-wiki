"""Task 26: bounded Evidence Graph generation maintenance."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _generation_directory(state_root: Path, result: dict) -> Path:
    """The built generation, naming the whole result when there is none."""
    assert result.get("generation_id"), result
    return (
        state_root / "cache" / "evidence-graph" / "generations" / result["generation_id"]
    )


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _graph_tables(state: Path, generation_id: str) -> dict[str, list[tuple]]:
    database_path = (
        state / "cache" / "evidence-graph" / "generations" / generation_id
        / "evidence.sqlite3"
    )
    with sqlite3.connect(database_path) as database:
        return {
            table: database.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "source", "node", "occurrence", "assertion", "evidence",
                "observation", "dependency",
            )
        }


def _write_python_workspace(root: Path, files: dict[str, str]) -> None:
    scripts = root / "scripts"
    scripts.mkdir(exist_ok=True)
    for path in scripts.glob("*.py"):
        path.unlink()
    for name, content in files.items():
        (scripts / name).write_text(content, encoding="utf-8")


def test_production_opencode_plugin_progresses_through_generation_validation(tmp_path):
    pytest.importorskip("tree_sitter_javascript")
    import doctor
    from evidence_graph import validate_generation_artifact
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    scripts = root / "scripts"
    scripts.mkdir()
    production_plugin = (
        Path(__file__).resolve().parent.parent / "scripts" / "llm-wiki-memory-opencode.js"
    )
    (scripts / production_plugin.name).write_bytes(production_plugin.read_bytes())

    result = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert result["status"] == "built"
    generation = state / "cache/evidence-graph/generations" / result["generation_id"]
    validate_generation_artifact(
        generation,
        GenerationCatalog(state).get_active(),
        state_root=state,
    )
    with sqlite3.connect(generation / "evidence.sqlite3") as database:
        targets = [
            row[0]
            for row in database.execute(
                "SELECT target_text FROM observation WHERE edge_type = 'CALLS'"
            )
        ]
    assert targets
    assert all(target and "\r" not in target and "\n" not in target for target in targets)


def _empty_generation(
    state: Path,
    generation_id: str,
    *,
    parent: str | None = None,
    root: Path | None = None,
    extractor_version: str | None = None,
):
    import doctor
    from evidence_graph_builder import build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    catalog = GenerationCatalog(state)
    if root is not None and extractor_version is None:
        extractor_version = doctor._maintenance_extractor_identity()
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
        repository_scope=None if root is None else resolve_repository_scope(root),
        **({"extractor_version": extractor_version} if extractor_version else {}),
    )


def test_generation_check_reports_graph_only_v1_as_degraded_and_repairable(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    _empty_generation(state, "gen-1", root=root)
    now = datetime.now(timezone.utc)

    healthy = doctor._generation_check(
        root, state, now, deadline=time.monotonic() + 5, max_sources=10
    )

    assert healthy["status"] == "degraded"
    assert (
        healthy["details"]
        | {
            "catalog": "valid",
            "active_generation": "gen-1",
            "generation_schema": "evidence-graph/v2",
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
    assert healthy["details"]["search_index"] == "missing"
    assert healthy["details"]["repairable"] is True

    (root / "knowledge" / "notes" / "new.md").write_text(
        "---\ntype: concept\n---\n# New\n", encoding="utf-8"
    )
    stale = doctor._generation_check(
        root, state, now, deadline=time.monotonic() + 5, max_sources=10
    )

    assert stale["status"] == "degraded"
    assert stale["details"]["freshness"] == "stale"
    assert stale["details"]["unindexed_delta"] == 1


def test_generation_check_reports_complete_v2_as_healthy(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )

    result = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 5,
        max_sources=10,
    )

    assert built["status"] == "built"
    assert result["status"] == "ok"
    assert result["details"]["search_index"] == "valid"
    assert result["details"]["search_schema"] == "corpus-search/v1"
    assert result["details"]["search_integrity"] == "valid"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_generation_check_reports_invalid_v2_search_index_as_error(tmp_path, damage):
    import doctor

    root, state = _vault(tmp_path)
    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    search = (
        state
        / "cache/evidence-graph/generations"
        / built["generation_id"]
        / "search.sqlite3"
    )
    if damage == "missing":
        search.unlink()
    else:
        search.write_bytes(b"corrupt")

    result = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 5,
        max_sources=10,
    )

    assert result["status"] == "error"
    assert result["details"]["catalog"] == "valid"
    assert result["details"]["generation_schema"] == "evidence-graph/v2"
    assert result["details"]["search_index"] == damage
    assert result["details"]["search_schema"] == "corpus-search/v1"
    assert result["details"]["search_integrity"] == (
        "missing" if damage == "missing" else "invalid"
    )


@pytest.mark.parametrize("finding", ["unscoped", "mismatched_scope", "stale_extractor"])
def test_generation_check_degrades_noncurrent_scope_or_extraction_identity(tmp_path, finding):
    import doctor

    root, state = _vault(tmp_path)
    if finding == "unscoped":
        _empty_generation(
            state,
            "gen-1",
            extractor_version=doctor._maintenance_extractor_identity(),
        )
    elif finding == "mismatched_scope":
        other_root, _other_state = _vault(tmp_path / "other")
        _empty_generation(state, "gen-1", root=other_root)
    else:
        _empty_generation(
            state,
            "gen-1",
            root=root,
            extractor_version="maintenance-extractors/stale",
        )

    result = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 5,
        max_sources=10,
    )

    assert result["status"] == "degraded"
    assert result["details"]["freshness"] == "stale"
    assert result["details"]["repairable"] is True


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
    replacement = catalog.catalog_path.with_suffix(".replacement")
    replacement.write_bytes(catalog.catalog_path.read_bytes())
    os.replace(replacement, catalog.catalog_path)


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
        time_budget_seconds=60,
        max_sources=10,
    )

    assert built["status"] == "built"
    assert built["partial"] is False
    assert before_knowledge == list((root / "knowledge").rglob("*"))

    current = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
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
        time_budget_seconds=60,
        max_sources=1,
    )

    assert deferred["status"] == "deferred"
    assert deferred["partial"] is True
    assert deferred["reason"] == "source_limit"
    assert not any(path.name.endswith(".lock") for path in state.rglob("*"))


def test_maintenance_publishes_consumable_v2_search_without_legacy(tmp_path, monkeypatch):
    import doctor
    import search_memory
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    unique = "phaseoneuniqueterm"
    (root / "knowledge/notes/searchable.md").write_text(
        f"---\ntype: concept\n---\n# Searchable\n{unique}\n", encoding="utf-8"
    )

    built = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    active = GenerationCatalog(state).get_active()
    monkeypatch.setattr(search_memory, "ROOT", root)
    monkeypatch.setattr(
        search_memory,
        "_legacy_lexical_hits",
        lambda *args, **kwargs: pytest.fail("legacy search must not run"),
    )
    results = search_memory.search(unique, catalog=GenerationCatalog(state), graph=False)

    assert built["status"] == "built"
    assert active["schema_version"] == "corpus-generation/v2"
    assert {item["path"] for item in active["artifacts"]} >= {
        "source-manifest.json",
        "evidence.sqlite3",
        "search.sqlite3",
    }
    assert results and results[0]["generation"] == built["generation_id"]


def test_matching_hash_v2_with_missing_search_is_rebuilt(tmp_path):
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    first = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    first_path = (
        state
        / "cache/evidence-graph/generations"
        / first["generation_id"]
    )
    (first_path / "search.sqlite3").unlink()

    rebuilt = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )

    assert rebuilt["status"] == "built"
    assert rebuilt["generation_id"] != first["generation_id"]
    assert GenerationCatalog(state).get_active()["generation_id"] == rebuilt["generation_id"]
    assert (
        state
        / "cache/evidence-graph/generations"
        / rebuilt["generation_id"]
        / "search.sqlite3"
    ).is_file()


def _generation_directories(path) -> set[str]:
    return {item.name for item in path.iterdir() if item.is_dir()}


def _ok_checks(names) -> list[dict]:
    return [
        {"id": name, "status": "ok", "message": "ok", "details": {}} for name in names
    ]


_SYNC_CHECK_NAMES = (
    "environment",
    "runtime",
    "filesystem",
    "integrations",
    "transactions",
    "queue",
)


def test_fts_failure_preserves_prior_and_removes_candidate(tmp_path, monkeypatch):
    import doctor
    import search_memory
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    prior = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    (root / "knowledge/notes/new.md").write_text(
        "---\ntype: concept\n---\n# New\nnew term\n", encoding="utf-8"
    )
    before = _generation_directories(state / "cache/evidence-graph/generations")
    monkeypatch.setattr(
        search_memory,
        "build_generation_fts",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected FTS failure")),
    )

    with pytest.raises(RuntimeError, match="injected FTS failure"):
        doctor._build_or_refresh_generation(
            root,
            state,
            deadline=time.monotonic() + 5,
            cancelled=lambda: False,
            max_sources=10,
            force_rebuild=False,
        )

    catalog = GenerationCatalog(state)
    assert catalog.get_active()["generation_id"] == prior["generation_id"]
    assert _generation_directories(catalog.generations_path) == before


def test_incremental_search_contains_new_chunks_and_no_old_text(tmp_path):
    import doctor

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/changing.md"
    page.write_text(
        "---\ntype: concept\n---\n# Changing\noldonlyterm\n", encoding="utf-8"
    )
    first = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    page.write_text(
        "---\ntype: concept\n---\n# Changing\nnewonlyterm\n", encoding="utf-8"
    )
    second = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )

    assert first["status"] == second["status"] == "built"
    search = state / "cache/evidence-graph/generations" / second["generation_id"] / "search.sqlite3"
    with sqlite3.connect(search) as database:
        text = "\n".join(row[0] for row in database.execute("SELECT content FROM chunks"))
        assert database.execute(
            "SELECT COUNT(*) FROM chunks WHERE chunks MATCH 'newonlyterm'"
        ).fetchone()[0] == 1
    assert "newonlyterm" in text
    assert "oldonlyterm" not in text


def test_source_drift_before_publication_preserves_prior_and_removes_candidate(
    tmp_path, monkeypatch
):
    import doctor
    import search_memory
    from corpus_snapshot import CorpusChanged
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/drift.md"
    page.write_text("---\ntype: concept\n---\n# Drift\nfirst\n", encoding="utf-8")
    prior = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    page.write_text("---\ntype: concept\n---\n# Drift\nsecond\n", encoding="utf-8")
    before = {path.name for path in GenerationCatalog(state).generations_path.iterdir()}
    real_build = search_memory.build_generation_fts

    def build_then_drift(*args, **kwargs):
        descriptor = real_build(*args, **kwargs)
        page.write_text("---\ntype: concept\n---\n# Drift\nthird!\n", encoding="utf-8")
        return descriptor

    monkeypatch.setattr(search_memory, "build_generation_fts", build_then_drift)

    # Until 2026-09-05 a page edited during the build refused the publication and
    # left the prior generation active. Measured on the live vault, that is why
    # nothing was activated from 2026-08-30 onward. The build now finishes; the
    # page that moved is dropped at query time before its text can be quoted.
    doctor._build_or_refresh_generation(
        root,
        state,
        deadline=time.monotonic() + 5,
        cancelled=lambda: False,
        max_sources=10,
        force_rebuild=False,
    )

    catalog = GenerationCatalog(state)
    assert catalog.get_active()["generation_id"] != prior["generation_id"]
    assert {path.name for path in catalog.generations_path.iterdir()} > before


def test_cancellation_after_registration_leaves_only_an_unregistered_orphan(
    tmp_path, monkeypatch
):
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/cancel.md"
    page.write_text("---\ntype: concept\n---\n# Cancel\nfirst\n", encoding="utf-8")
    prior = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    page.write_text("---\ntype: concept\n---\n# Cancel\nsecond\n", encoding="utf-8")
    cancelled = False
    real_register = GenerationCatalog._register_validated

    def register_then_cancel(self, *args, **kwargs):
        nonlocal cancelled
        result = real_register(self, *args, **kwargs)
        cancelled = True
        return result

    monkeypatch.setattr(
        GenerationCatalog, "_register_validated", register_then_cancel
    )
    with pytest.raises(TimeoutError, match="cancel"):
        doctor._build_or_refresh_generation(
            root,
            state,
            deadline=time.monotonic() + 5,
            cancelled=lambda: cancelled,
            max_sources=10,
            force_rebuild=False,
        )

    catalog = GenerationCatalog(state)
    assert catalog.get_active()["generation_id"] == prior["generation_id"]
    with sqlite3.connect(catalog.catalog_path) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
    generation_directories = {
        path.name for path in catalog.generations_path.iterdir() if path.is_dir()
    }
    assert prior["generation_id"] in generation_directories
    assert len(generation_directories) == 2


def test_expired_deadline_after_registration_leaves_only_an_unregistered_orphan(
    tmp_path, monkeypatch
):
    import doctor
    import generation_catalog
    import search_memory
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/deadline.md"
    page.write_text("---\ntype: concept\n---\n# Deadline\nfirst\n", encoding="utf-8")
    prior = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    page.write_text("---\ntype: concept\n---\n# Deadline\nsecond\n", encoding="utf-8")

    deadline = time.monotonic() + 100.0
    clock = [deadline - 1.0]
    monkeypatch.setattr(generation_catalog.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(search_memory.time, "monotonic", lambda: clock[0])
    real_register = GenerationCatalog._register_validated

    def register_then_expire(self, *args, **kwargs):
        result = real_register(self, *args, **kwargs)
        clock[0] = deadline + 1.0
        return result

    monkeypatch.setattr(GenerationCatalog, "_register_validated", register_then_expire)

    with pytest.raises(TimeoutError, match="deadline"):
        doctor._build_or_refresh_generation(
            root,
            state,
            deadline=deadline,
            cancelled=lambda: False,
            max_sources=10,
            force_rebuild=False,
        )

    catalog = GenerationCatalog(state)
    assert catalog.get_active()["generation_id"] == prior["generation_id"]
    with sqlite3.connect(catalog.catalog_path) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1
    generation_directories = {
        path.name for path in catalog.generations_path.iterdir() if path.is_dir()
    }
    assert prior["generation_id"] in generation_directories
    assert len(generation_directories) == 2


def test_complete_v2_orphan_is_recoverable_without_activation(tmp_path):
    import corpus_snapshot
    from evidence_graph_builder import KillPointError, build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/recover.md"
    page.write_text("---\ntype: concept\n---\n# Recover\nrecoverable\n", encoding="utf-8")
    snapshot = corpus_snapshot.collect_corpus(root)
    sources = [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]
    catalog = GenerationCatalog(state)

    with pytest.raises(KillPointError, match="after_validation"):
        build_full_generation(
            catalog,
            sources=sources,
            source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="recoverable-v2",
            snapshot=snapshot,
            publication_root=root,
            repository_scope=resolve_repository_scope(root),
            kill_point="after_validation",
        )

    assert catalog.get_active() is None
    assert catalog.recover_orphans() == ["recoverable-v2"]
    assert catalog.get_active() is None
    assert catalog.activate("recoverable-v2", expected_active=None) is True


def test_incomplete_v2_orphan_is_not_registered_or_activated(tmp_path):
    import corpus_snapshot
    import doctor
    from evidence_graph_builder import KillPointError, build_full_generation
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    root, state = _vault(tmp_path)
    page = root / "knowledge/notes/incomplete.md"
    page.write_text("---\ntype: concept\n---\n# Incomplete\n", encoding="utf-8")
    snapshot = corpus_snapshot.collect_corpus(root)
    sources = [
        {
            "source_id": source.record.logical_id,
            "relative_path": source.record.relative_path,
            "sha256": source.record.sha256,
            "size": source.record.size,
            "media_type": source.record.media_type,
            "language": source.record.language,
            "git_oid": source.record.git_oid,
        }
        for source in snapshot.sources
    ]
    catalog = GenerationCatalog(state)
    with pytest.raises(KillPointError, match="after_validation"):
        build_full_generation(
            catalog,
            sources=sources,
            source_bytes={source.record.logical_id: source.content for source in snapshot.sources},
            nodes=(),
            occurrences=(),
            assertions=(),
            evidence=(),
            observations=(),
            dependencies=(),
            generation_id="incomplete-v2",
            snapshot=snapshot,
            publication_root=root,
            repository_scope=resolve_repository_scope(root),
            kill_point="after_validation",
        )
    (catalog.generations_path / "incomplete-v2/search.sqlite3").unlink()

    repaired = []
    doctor._repair_generation_catalog(
        root,
        state,
        deadline=time.monotonic() + 5,
        cancelled=lambda: False,
        repaired=repaired,
    )

    assert catalog.get_active() is None
    assert catalog.recover_orphans() == []
    with sqlite3.connect(catalog.catalog_path) as database:
        assert database.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 0


def test_maintenance_scopes_same_basename_roots_and_repository_nodes(tmp_path):
    import doctor
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    first_root, state = _vault(tmp_path / "first")
    second_root, _unused_state = _vault(tmp_path / "second")
    for root in (first_root, second_root):
        (root / "scripts").mkdir()
        (root / "scripts" / "app.py").write_text("def shared():\n    return 1\n", encoding="utf-8")

    first = doctor.run_generation_maintenance(
        root=first_root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )
    second = doctor.run_generation_maintenance(
        root=second_root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    first_scope = resolve_repository_scope(first_root)
    second_scope = resolve_repository_scope(second_root)
    assert first_root.name == second_root.name
    assert first_scope.repository_id != second_scope.repository_id
    assert first["status"] == "built"
    assert second["status"] == "built"
    assert second["generation_id"] != first["generation_id"]
    active = GenerationCatalog(state).get_active()
    assert active["repository_scope"] == second_scope.as_dict()
    database_path = (
        state / "cache" / "evidence-graph" / "generations" / second["generation_id"]
        / "evidence.sqlite3"
    )
    with sqlite3.connect(database_path) as database:
        assert database.execute(
            "SELECT identity_key FROM node WHERE kind = 'repository'"
        ).fetchall() == [(second_scope.repository_id,)]


def test_maintenance_extracts_python_and_records_current_extractor_inputs(tmp_path):
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "worker.py").write_text(
        "def production_worker():\n    return 42\n", encoding="utf-8"
    )

    result = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert result["status"] == "built"
    active = GenerationCatalog(state).get_active()
    import corpus_snapshot

    assert active["extractor_version"] == corpus_snapshot.EXTRACTOR_VERSION
    assert active["graph_extractor_version"] == doctor._maintenance_extractor_identity()
    database_path = (
        state / "cache" / "evidence-graph" / "generations" / result["generation_id"]
        / "evidence.sqlite3"
    )
    with sqlite3.connect(database_path) as database:
        function_names = {
            json.loads(row[0])["name"]
            for row in database.execute("SELECT metadata_json FROM node WHERE kind = 'function'")
        }
        bad_parse_observations = database.execute(
            "SELECT COUNT(*) FROM observation "
            "WHERE edge_type = 'PARSES' AND target_text = 'en' "
            "AND reason = 'unsupported_semantics'"
        ).fetchone()[0]
    assert "production_worker" in function_names
    assert bad_parse_observations == 0


def test_maintenance_extracts_workspace_once_and_partitions_cross_file_ownership(
    tmp_path, monkeypatch
):
    import code_extractor
    import doctor

    root, state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from dep import helper\n\ndef app():\n    return helper()\n",
            "dep.py": "def helper():\n    return 1\n",
        },
    )
    calls = []
    real_extract = code_extractor.extract_code

    def counted_extract(sources, **kwargs):
        captured = tuple(sources)
        calls.append(tuple(source.record.relative_path for source in captured))
        return real_extract(captured, **kwargs)

    monkeypatch.setattr(code_extractor, "extract_code", counted_extract)

    result = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert result["status"] == "built"
    assert calls == [("scripts/app.py", "scripts/dep.py")]
    generation = state / "cache/evidence-graph/generations" / result["generation_id"]
    manifest = json.loads((generation / "incremental-manifest.json").read_bytes())
    entries = {entry["relative_path"]: entry for entry in manifest["sources"]}
    app = entries["scripts/app.py"]
    dep = entries["scripts/dep.py"]
    assert app["source_dependencies"] == [dep["source_id"]]
    assert set(app["records"]["occurrences"]).isdisjoint(dep["records"]["occurrences"])
    assert set(app["records"]["evidence"]).isdisjoint(dep["records"]["evidence"])
    with sqlite3.connect(generation / "evidence.sqlite3") as database:
        edges = database.execute(
            "SELECT edge_type, resolution FROM assertion "
            "WHERE edge_type IN ('IMPORTS', 'CALLS') ORDER BY edge_type"
        ).fetchall()
    assert edges == [("CALLS", "resolved"), ("IMPORTS", "resolved")]


@pytest.mark.parametrize(
    ("initial", "updated"),
    [
        (
            {"app.py": "from dep import helper\nhelper()\n"},
            {
                "app.py": "from dep import helper\nhelper()\n",
                "dep.py": "def helper():\n    return 1\n",
            },
        ),
        (
            {
                "app.py": "from dep import helper\nhelper()\n",
                "dep.py": "def helper():\n    return 1\n",
            },
            {"app.py": "from dep import helper\nhelper()\n"},
        ),
        (
            {
                "app.py": "from dep import helper\nhelper()\n",
                "dep.py": "def helper():\n    return 1\n",
            },
            {
                "app.py": "from dep import helper\nhelper()\n",
                "renamed.py": "def helper():\n    return 1\n",
            },
        ),
        (
            {
                "app.py": "from dep import helper\nhelper()\n",
                "dep.py": "def helper():\n    return 1\n",
            },
            {
                "app.py": "from dep import helper\nhelper()\n",
                "dep.py": "def helper(value: int = 1):\n    return value\n",
            },
        ),
    ],
    ids=("add-resolves", "delete-unresolves", "rename-unresolves", "signature-change"),
)
def test_maintenance_cross_file_incremental_equals_clean_rebuild(tmp_path, initial, updated):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(root, initial)
    first = doctor.run_generation_maintenance(
        root=root, state_root=incremental_state, time_budget_seconds=60, max_sources=10
    )
    _write_python_workspace(root, updated)
    incremental = doctor.run_generation_maintenance(
        root=root, state_root=incremental_state, time_budget_seconds=60, max_sources=10
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root, state_root=clean_state, time_budget_seconds=60, max_sources=10
    )

    assert first["status"] == "built"
    assert incremental["status"] == "built"
    assert clean["status"] == "built"
    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    unresolved = {
        (row[2], row[4]) for row in tables["observation"]
        if row[2] in {"IMPORTS", "CALLS"}
    }
    if "dep.py" in updated:
        assert not unresolved
    else:
        assert {("IMPORTS", "missing_dependency"), ("CALLS", "missing_dependency")} <= unresolved


def test_package_init_relative_import_incremental_equals_clean_rebuild(tmp_path):
    import doctor

    root, incremental_state = _vault(tmp_path)
    package = root / "scripts" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .dep import helper\nhelper()\n", encoding="utf-8"
    )
    doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    (package / "dep.py").write_text("def helper():\n    return 1\n", encoding="utf-8")

    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert {row[2] for row in tables["assertion"]} >= {"IMPORTS", "CALLS"}
    generation = (
        incremental_state
        / "cache/evidence-graph/generations"
        / incremental["generation_id"]
    )
    manifest = json.loads((generation / "incremental-manifest.json").read_bytes())
    entries = {entry["relative_path"]: entry for entry in manifest["sources"]}
    assert entries["scripts/pkg/__init__.py"]["source_dependencies"] == [
        entries["scripts/pkg/dep.py"]["source_id"]
    ]


def _entries_by_path(manifest: dict) -> dict:
    return {entry["relative_path"]: entry for entry in manifest["sources"]}


def _apply_dependency_change(change: str, dependency, package) -> None:
    if change == "rename":
        dependency.rename(package / "renamed.py")
        return
    dependency.unlink()


def _import_assertions(tables: dict) -> list:
    return [row for row in tables["assertion"] if row[2] == "IMPORTS"]


def _has_missing_dependency_observation(tables: dict) -> bool:
    return any(
        row[2] == "IMPORTS" and row[4] == "missing_dependency"
        for row in tables["observation"]
    )


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_from_dot_import_dependency_invalidates_and_matches_clean_rebuild(
    tmp_path, change
):
    import doctor

    root, incremental_state = _vault(tmp_path)
    package = root / "scripts" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from . import dep\n", encoding="utf-8")
    dependency = package / "dep.py"
    dependency.write_text("VALUE = 1\n", encoding="utf-8")
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    first_generation = _generation_directory(incremental_state, first)
    first_manifest = json.loads(
        (first_generation / "incremental-manifest.json").read_bytes()
    )
    first_entries = _entries_by_path(first_manifest)
    assert first_entries["scripts/pkg/__init__.py"]["source_dependencies"] == [
        first_entries["scripts/pkg/dep.py"]["source_id"]
    ]

    _apply_dependency_change(change, dependency, package)
    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert not _import_assertions(tables)
    assert _has_missing_dependency_observation(tables)


def test_maintenance_ambiguous_candidates_invalidate_referencing_source(tmp_path):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from dep import helper\nhelper()\n",
            "dep.py": "def helper():\n    return 1\n",
        },
    )
    (root / "tests").mkdir()
    alternate = root / "tests" / "dep.py"
    alternate.write_text("def helper():\n    return 2\n", encoding="utf-8")

    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    first_generation = _generation_directory(incremental_state, first)
    first_manifest = json.loads(
        (first_generation / "incremental-manifest.json").read_bytes()
    )
    entries = {entry["relative_path"]: entry for entry in first_manifest["sources"]}
    app_dependencies = entries["scripts/app.py"]["source_dependencies"]

    assert app_dependencies == sorted(
        [entries["scripts/dep.py"]["source_id"], entries["tests/dep.py"]["source_id"]]
    )
    with sqlite3.connect(first_generation / "evidence.sqlite3") as database:
        assert database.execute(
            "SELECT COUNT(*) FROM assertion WHERE edge_type = 'CALLS'"
        ).fetchone()[0] == 0
        assert database.execute(
            "SELECT reason FROM observation WHERE edge_type = 'CALLS'"
        ).fetchall() == [("ambiguous_target",)]

    alternate.write_text("def other():\n    return 2\n", encoding="utf-8")
    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert any(row[2] == "CALLS" and row[7] == "resolved" for row in tables["assertion"])


def test_module_addition_rebuilds_unique_reference_into_ambiguity_and_matches_clean(
    tmp_path,
):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from dep import helper\nhelper()\n",
            "dep.py": "def helper():\n    return 1\n",
        },
    )
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    tests_root = root / "tests"
    tests_root.mkdir()
    (tests_root / "dep.py").write_text(
        "def helper():\n    return 2\n", encoding="utf-8"
    )

    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert first["status"] == "built"
    assert incremental["rebuilt_sources"] == incremental["sources"]
    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert not [row for row in tables["assertion"] if row[2] == "CALLS"]
    assert any(
        row[2] == "CALLS" and row[4] == "ambiguous_target"
        for row in tables["observation"]
    )


def test_module_removal_rebuilds_ambiguous_reference_to_unique_and_matches_clean(
    tmp_path,
):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from dep import helper\nhelper()\n",
            "dep.py": "def helper():\n    return 1\n",
        },
    )
    tests_root = root / "tests"
    tests_root.mkdir()
    alternate = tests_root / "dep.py"
    alternate.write_text("def helper():\n    return 2\n", encoding="utf-8")
    doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    alternate.unlink()

    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert incremental["rebuilt_sources"] == incremental["sources"]
    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert any(row[2] == "CALLS" and row[7] == "resolved" for row in tables["assertion"])


def test_module_file_package_collision_is_ambiguous_then_resolves_incrementally(tmp_path):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from foo import helper\nhelper()\n",
            "foo.py": "def helper():\n    return 'module'\n",
        },
    )
    package = root / "scripts/foo"
    package.mkdir()
    (package / "__init__.py").write_text(
        "def helper():\n    return 'package'\n", encoding="utf-8"
    )

    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    first_generation = _generation_directory(incremental_state, first)
    first_manifest = json.loads(
        (first_generation / "incremental-manifest.json").read_bytes()
    )
    entries = {entry["relative_path"]: entry for entry in first_manifest["sources"]}

    assert entries["scripts/app.py"]["source_dependencies"] == sorted(
        [entries["scripts/foo.py"]["source_id"], entries["scripts/foo/__init__.py"]["source_id"]]
    )
    with sqlite3.connect(first_generation / "evidence.sqlite3") as database:
        assert database.execute(
            "SELECT edge_type, reason FROM observation "
            "WHERE edge_type IN ('IMPORTS', 'CALLS') ORDER BY edge_type"
        ).fetchall() == [
            ("CALLS", "ambiguous_target"),
            ("IMPORTS", "ambiguous_target"),
        ]
        assert database.execute(
            "SELECT COUNT(*) FROM assertion WHERE edge_type IN ('IMPORTS', 'CALLS')"
        ).fetchone()[0] == 0

    (root / "scripts/foo.py").unlink()
    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert {row[2] for row in tables["assertion"]} >= {"IMPORTS", "CALLS"}


def test_duplicate_tables_are_ambiguous_and_incremental_matches_clean_rebuild(tmp_path):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "model_a.py": "class First:\n    __tablename__ = 'Users'\n",
            "model_b.py": "class Second:\n    __tablename__ = 'users'\n",
            "reader.py": (
                "import sqlite3\n"
                "def load(connection: sqlite3.Connection):\n"
                "    return connection.execute('SELECT * FROM USERS')\n"
            ),
        },
    )

    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    first_generation = _generation_directory(incremental_state, first)
    first_manifest = json.loads(
        (first_generation / "incremental-manifest.json").read_bytes()
    )
    entries = {entry["relative_path"]: entry for entry in first_manifest["sources"]}

    assert entries["scripts/reader.py"]["source_dependencies"] == sorted(
        [
            entries["scripts/model_a.py"]["source_id"],
            entries["scripts/model_b.py"]["source_id"],
        ]
    )
    with sqlite3.connect(first_generation / "evidence.sqlite3") as database:
        assert database.execute(
            "SELECT COUNT(*) FROM assertion WHERE edge_type = 'READS'"
        ).fetchone()[0] == 0
        assert database.execute(
            "SELECT target_text, reason FROM observation WHERE edge_type = 'READS'"
        ).fetchall() == [("USERS", "ambiguous_target")]

    (root / "scripts/model_b.py").write_text(
        "class Second:\n    __tablename__ = 'archived_users'\n",
        encoding="utf-8",
    )
    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert any(row[2] == "READS" and row[7] == "resolved" for row in tables["assertion"])


def test_missing_table_rechecks_when_existing_code_source_adds_definition(tmp_path):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "models.py": "class Archived:\n    __tablename__ = 'archived_users'\n",
            "reader.py": (
                "import sqlite3\n"
                "def load(connection: sqlite3.Connection):\n"
                "    return connection.execute('SELECT * FROM users')\n"
            ),
        },
    )
    doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    (root / "scripts/models.py").write_text(
        "class User:\n    __tablename__ = 'users'\n", encoding="utf-8"
    )

    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert any(row[2] == "READS" and row[7] == "resolved" for row in tables["assertion"])


def test_missing_python_symbol_rechecks_all_code_sources_on_module_addition(
    tmp_path,
):
    import doctor

    root, incremental_state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "from future_dep import helper\nhelper()\n",
            "ordinary.py": "def stable():\n    return 1\n",
        },
    )
    doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    (root / "scripts/future_dep.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )

    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert incremental["reused_sources"] == 0
    assert incremental["rebuilt_sources"] == incremental["sources"]
    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    tables = _graph_tables(incremental_state, incremental["generation_id"])
    assert {row[2] for row in tables["assertion"]} >= {"IMPORTS", "CALLS"}


@pytest.mark.parametrize("change", ["delete", "rename"])
def test_shared_structural_nodes_survive_owner_removal_and_match_clean_rebuild(
    tmp_path, change
):
    import doctor
    import evidence_graph
    from generation_catalog import GenerationCatalog

    root, incremental_state = _vault(tmp_path)
    shared = root / "scripts" / "shared"
    shared.mkdir(parents=True)
    first_file = shared / "a.py"
    first_file.write_text("def first():\n    return 1\n", encoding="utf-8")
    (shared / "b.py").write_text("def second():\n    return 2\n", encoding="utf-8")
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    first_generation = _generation_directory(incremental_state, first)
    first_manifest = json.loads(
        (first_generation / "incremental-manifest.json").read_bytes()
    )
    first_entries = _entries_by_path(first_manifest)

    if change == "rename":
        first_file.rename(shared / "z.py")
    else:
        first_file.unlink()
    incremental = doctor.run_generation_maintenance(
        root=root,
        state_root=incremental_state,
        time_budget_seconds=60,
        max_sources=10,
    )
    clean_state = tmp_path / "clean-state"
    clean = doctor.run_generation_maintenance(
        root=root,
        state_root=clean_state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert incremental["status"] == "built"
    assert _graph_tables(incremental_state, incremental["generation_id"]) == _graph_tables(
        clean_state, clean["generation_id"]
    )
    shared_nodes = set(
        first_entries["scripts/shared/a.py"]["records"]["nodes"]
    ) & set(first_entries["scripts/shared/b.py"]["records"]["nodes"])
    assert shared_nodes
    for state, result in (
        (incremental_state, incremental),
        (clean_state, clean),
    ):
        catalog = GenerationCatalog(state)
        manifest = catalog.get_active()
        generation = (
            state / "cache/evidence-graph/generations" / result["generation_id"]
        )
        evidence_graph.validate_generation_artifact(
            generation,
            manifest,
            state_root=state,
        )


def _partition_nodes(source_ids) -> list[dict]:
    return [
        {"node_id": "node:shared"},
        *({"node_id": f"node:{index}"} for index in range(len(source_ids))),
    ]


def _partition_occurrences(source_ids) -> list[dict]:
    return [
        {
            "occurrence_id": f"occurrence:{index}",
            "node_id": f"node:{index}",
            "source_id": source_id,
        }
        for index, source_id in enumerate(source_ids)
    ]


def _partition_assertions(source_ids) -> list[dict]:
    return [
        {
            "assertion_id": f"assertion:{index}",
            "source_node_id": "node:shared",
            "target_node_id": f"node:{index}",
        }
        for index in range(len(source_ids))
    ]


def _partition_evidence(source_ids) -> list[dict]:
    return [
        {
            "evidence_id": f"evidence:{index}",
            "assertion_id": f"assertion:{index}",
            "observation_id": None,
            "source_id": source_id,
        }
        for index, source_id in enumerate(source_ids)
    ]


def _partition_dependencies(source_ids) -> list[dict]:
    return [
        {"dependency_id": f"dependency:{index}", "source_id": source_id}
        for index, source_id in enumerate(source_ids)
    ]


def _assert_partition_of(partition, index: int) -> None:
    assert {node["node_id"] for node in partition.nodes} == {
        "node:shared",
        f"node:{index}",
    }
    assert partition.dependencies[0]["dependency_id"] == f"dependency:{index}"


def test_workspace_partition_indexes_each_record_collection_once():
    from types import SimpleNamespace

    import doctor

    class CountingRecords:
        def __init__(self, records):
            self.records = tuple(records)
            self.iterations = 0

        def __iter__(self):
            self.iterations += 1
            return iter(self.records)

    source_ids = tuple(f"source:{index}" for index in range(25))
    sources = tuple(
        SimpleNamespace(record=SimpleNamespace(logical_id=source_id))
        for source_id in source_ids
    )
    collections = {
        "nodes": CountingRecords(_partition_nodes(source_ids)),
        "occurrences": CountingRecords(_partition_occurrences(source_ids)),
        "assertions": CountingRecords(_partition_assertions(source_ids)),
        "evidence": CountingRecords(_partition_evidence(source_ids)),
        "observations": CountingRecords(()),
        "dependencies": CountingRecords(_partition_dependencies(source_ids)),
    }
    result = SimpleNamespace(
        **collections,
        observation_source_dependencies={},
    )

    partitions = doctor._partition_code_extraction(result, sources)

    assert all(records.iterations == 1 for records in collections.values())
    assert tuple(partitions) == source_ids
    for index, source_id in enumerate(source_ids):
        _assert_partition_of(partitions[source_id], index)


def test_workspace_partition_receives_generation_deadline_and_cancellation(
    tmp_path, monkeypatch
):
    import doctor

    root, state = _vault(tmp_path)
    _write_python_workspace(root, {"app.py": "def app():\n    return 1\n"})
    captured = {}
    real_partition = doctor._partition_code_extraction

    def capture_partition(result, sources, **kwargs):
        captured.update(kwargs)
        return real_partition(result, sources, **kwargs)

    monkeypatch.setattr(doctor, "_partition_code_extraction", capture_partition)

    built = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert built["status"] == "built"
    assert captured["deadline"] > time.monotonic()
    assert callable(captured["cancelled"])


def test_workspace_partition_checks_cancellation_inside_record_loops():
    from types import SimpleNamespace

    import doctor

    source = SimpleNamespace(record=SimpleNamespace(logical_id="source:app"))
    result = SimpleNamespace(
        nodes=tuple({"node_id": f"node:{index}"} for index in range(20)),
        occurrences=(),
        assertions=(),
        evidence=(),
        observations=(),
        dependencies=(),
        observation_source_dependencies={},
    )
    checks = 0

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 4

    with pytest.raises(TimeoutError, match="partition cancelled"):
        doctor._partition_code_extraction(result, (source,), cancelled=cancelled)

    assert checks == 4


def test_maintenance_language_membership_change_forces_workspace_reresolution(
    tmp_path, monkeypatch
):
    import corpus_snapshot
    import doctor

    root, state = _vault(tmp_path)
    _write_python_workspace(
        root,
        {
            "app.py": "def app():\n    return 1\n",
            "ordinary.py": "def ordinary():\n    return 2\n",
        },
    )
    first = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )
    real_collect = corpus_snapshot.collect_corpus

    def reclassified(*args, **kwargs):
        snapshot = real_collect(*args, **kwargs)
        source = next(item for item in snapshot.sources if item.record.relative_path == "scripts/app.py")
        object.__setattr__(source.record, "language", "typescript")
        return snapshot

    monkeypatch.setattr(corpus_snapshot, "collect_corpus", reclassified)
    second = doctor.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=60, max_sources=10
    )

    assert first["status"] == "built"
    assert second["status"] == "built"
    assert second["rebuilt_sources"] == second["sources"]
    assert second["reused_sources"] == 0


def test_maintenance_rebuilds_when_code_extractor_version_changes(tmp_path, monkeypatch):
    import code_extractor
    import doctor

    root, state = _vault(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )
    monkeypatch.setattr(code_extractor, "EXTRACTOR_VERSION", "code-extractor/freshness-test")

    second = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert first["status"] == "built"
    assert second["status"] == "built"
    assert second["generation_id"] != first["generation_id"]


def test_maintenance_rebuilds_when_classifier_identity_changes(tmp_path, monkeypatch):
    import code_languages
    import doctor

    root, state = _vault(tmp_path)
    (root / "scripts").mkdir()
    (root / "scripts" / "app.py").write_text("def app():\n    return 1\n", encoding="utf-8")
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )
    monkeypatch.setattr(
        code_languages,
        "CLASSIFIER_IDENTITY",
        "code-language-classifier/freshness-test+sha256:" + "0" * 64,
    )

    second = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert first["status"] == "built"
    assert second["status"] == "built"
    assert second["generation_id"] != first["generation_id"]


def test_maintenance_passes_budget_to_scope_resolution_and_defers(tmp_path, monkeypatch):
    import doctor
    import repository_scope

    root, state = _vault(tmp_path)
    captured = {}

    def exhausted(directory, *, deadline, cancelled):
        captured.update(directory=directory, deadline=deadline, cancelled=cancelled)
        raise TimeoutError("repository scope deadline reached")

    monkeypatch.setattr(repository_scope, "resolve_repository_scope", exhausted)

    result = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert result["status"] == "deferred"
    assert result["reason"] == "time_limit"
    assert captured["directory"] == root.resolve()
    assert captured["deadline"] > time.monotonic()
    assert callable(captured["cancelled"])


def test_workspace_extraction_deadline_defers_without_replacing_prior_generation(
    tmp_path, monkeypatch
):
    import code_extractor
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    _write_python_workspace(root, {"app.py": "def app():\n    return 1\n"})
    first = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )
    _write_python_workspace(root, {"app.py": "def app():\n    return 2\n"})
    captured = {}

    def expired(sources, *, deadline, cancelled, **kwargs):
        captured.update(
            sources=tuple(sources),
            deadline=deadline,
            cancelled=cancelled,
            kwargs=kwargs,
        )
        raise TimeoutError("pathological code extraction deadline")

    monkeypatch.setattr(code_extractor, "extract_code", expired)

    second = doctor.run_generation_maintenance(
        root=root,
        state_root=state,
        time_budget_seconds=60,
        max_sources=10,
    )

    assert first["status"] == "built"
    assert second["status"] == "deferred"
    assert second["reason"] == "time_limit"
    assert captured["deadline"] > time.monotonic()
    assert callable(captured["cancelled"])
    assert GenerationCatalog(state).get_active()["generation_id"] == first["generation_id"]


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


def test_edited_wikilink_is_visible_after_nightly_in_fresh_processes(
    tmp_path, monkeypatch
):
    import scheduled_nightly

    root, state = _vault(tmp_path)
    notes = root / "knowledge" / "notes"
    source = notes / "source.md"
    source.write_text(
        "---\ntype: concept\n---\n# Source\n[[target-a]]\n",
        encoding="utf-8",
    )
    for target in ("a", "b"):
        (notes / f"target-{target}.md").write_text(
            f"---\ntype: concept\n---\n# Target {target.upper()}\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(scheduled_nightly, "ROOT", root)
    monkeypatch.setattr(scheduled_nightly, "STATE_ROOT", state)
    logs = []

    def fresh_neighbors() -> list[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "LLM_WIKI_ROOT": str(root),
                "LLM_WIKI_STATE_ROOT": str(state),
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "scripts"),
            }
        )
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json, graph_neighbors; "
                    "graph_neighbors._build_link_graph = lambda *args, **kwargs: "
                    "(_ for _ in ()).throw(AssertionError('live source fallback')); "
                    "print(json.dumps(graph_neighbors.get_neighbors("
                    "'knowledge/notes/source.md')))"
                ),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return json.loads(completed.stdout)

    assert scheduled_nightly._refresh_generation(logs.append) == 0
    assert fresh_neighbors() == ["knowledge/notes/target-a.md"]

    source.write_text(
        "---\ntype: concept\n---\n# Source\n[[target-b]]\n",
        encoding="utf-8",
    )

    assert scheduled_nightly._refresh_generation(logs.append) == 0
    assert fresh_neighbors() == ["knowledge/notes/target-b.md"]


def _recording_kwargs(calls, value):
    """A stub that remembers the call it was made with."""

    def stub(**kwargs):
        calls.append(kwargs)
        return value

    return stub


def _recording_name(calls, name, value):
    """A stub that remembers only that it ran, in order."""

    def stub(**kwargs):
        calls.append(name)
        return value

    return stub


def _action_of(result: dict, action_id: str) -> dict:
    return next(item for item in result["actions"] if item["id"] == action_id)


def _stub_sync_report(monkeypatch, sync_memory, report, dependency) -> None:
    monkeypatch.setattr(sync_memory.doctor, "run_doctor", lambda **kwargs: report)
    monkeypatch.setattr(sync_memory, "_dependency_action", lambda **kwargs: dependency)


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
        "checks": _ok_checks(_SYNC_CHECK_NAMES) + [generation, index],
    }
    dependency = {"id": "dependencies", "status": "ok", "message": "ok", "details": {}}
    refreshed = {
        "id": "indexes",
        "status": "changed",
        "message": "Generation refreshed.",
        "details": {"generation": "gen-2", "partial": False},
    }
    _stub_sync_report(monkeypatch, sync_memory, report, dependency)
    monkeypatch.setattr(
        sync_memory, "_run_generation_builder", _recording_kwargs(calls, refreshed)
    )

    checked = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=False)
    applied = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    checked_index = _action_of(checked, "indexes")
    applied_index = _action_of(applied, "indexes")
    assert checked_index["status"] == "skipped"
    assert checked_index["details"]["checks"]["generation"] == "degraded"
    assert applied_index["status"] == "changed"
    assert calls, "apply must reach the shared generation builder"
    assert calls[0]["max_sources"] > 0


def test_sync_apply_refreshes_generation_and_legacy_index_when_both_are_stale(
    tmp_path, monkeypatch
):
    import sync_memory

    calls = []
    stale = {"freshness": "stale", "repairable": True}
    report = {
        "overall_status": "degraded",
        "repaired": [],
        "checks": _ok_checks(_SYNC_CHECK_NAMES)
        + [
            {"id": "generation", "status": "degraded", "message": "stale", "details": stale},
            {"id": "index", "status": "degraded", "message": "stale", "details": stale},
        ],
    }
    _stub_sync_report(
        monkeypatch,
        sync_memory,
        report,
        sync_memory._result("dependencies", "ok", "ok", {}),
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_generation_builder",
        _recording_name(
            calls, "generation", sync_memory._result("indexes", "changed", "generation", {})
        ),
    )
    monkeypatch.setattr(
        sync_memory,
        "_run_index_builder",
        _recording_name(
            calls, "index", sync_memory._result("indexes", "changed", "index", {})
        ),
    )

    applied = sync_memory.run_sync(root=tmp_path, state_root=tmp_path, home=tmp_path, apply=True)

    action = _action_of(applied, "indexes")
    assert calls == ["generation", "index"]
    assert action["status"] == "changed"


def test_a_vault_with_generations_but_none_active_is_degraded(tmp_path):
    """Built and then deactivated is an outage; never built is just young.

    Measured on 2026-08-24: a corpus rule change invalidated every candidate, the
    catalog cleared the active pointer, search returned zero rows — and the report
    still said `ok, legacy retrieval remains available`.
    """
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    catalog = GenerationCatalog(state)
    with sqlite3.connect(catalog.catalog_path) as database:
        database.execute(
            "INSERT INTO generations("
            "generation_id, parent_generation_id, manifest_json, manifest_sha256,"
            " registered_at) VALUES (?, NULL, ?, ?, ?)",
            ("generation-1111111111111111-22222222", b"{}", "a" * 64, "2026-08-24T00:00:00Z"),
        )

    report = doctor._generation_check(
        root,
        state,
        datetime.now(timezone.utc),
        deadline=time.monotonic() + 1,
        max_sources=10,
    )

    assert report["status"] == "degraded"
    assert report["details"]["active_generation"] is None
    assert report["details"]["recommended_action"] == "rebuild_generation"
    assert "1 are registered" in report["message"]


def _git_scope(tmp_path: Path, name: str, commit: str):
    """A Git-flavoured scope for a temporary checkout, at a chosen commit."""
    from repository_scope import (
        SCHEMA_VERSION,
        RepositoryScope,
        derive_checkout_id,
        derive_repository_id,
        resolve_repository_scope,
    )

    repository = tmp_path / name
    repository.mkdir(exist_ok=True)
    checkout_root = resolve_repository_scope(repository).checkout_root
    git_common_dir = f"{checkout_root}/.git"
    repository_id = derive_repository_id(
        checkout_root=checkout_root, git_common_dir=git_common_dir
    )
    return RepositoryScope(
        SCHEMA_VERSION,
        repository_id,
        derive_checkout_id(repository_id, checkout_root),
        checkout_root,
        git_common_dir,
        commit,
    )


def test_a_build_that_spans_a_commit_still_publishes(tmp_path, monkeypatch):
    """This vault commits itself; a four-minute build must survive that."""
    import repository_scope
    import search_memory

    expected = _git_scope(tmp_path, "repository", "a" * 40)
    moved = _git_scope(tmp_path, "repository", "b" * 40)
    monkeypatch.setattr(
        repository_scope, "resolve_repository_scope", lambda *a, **k: moved
    )

    search_memory._require_matching_repository(
        tmp_path, expected, deadline=None, cancelled=None
    )


def test_publishing_into_another_checkout_is_still_refused(tmp_path, monkeypatch):
    import repository_scope
    import search_memory

    expected = _git_scope(tmp_path, "repository", "a" * 40)
    elsewhere = _git_scope(tmp_path, "elsewhere", "a" * 40)
    monkeypatch.setattr(
        repository_scope, "resolve_repository_scope", lambda *a, **k: elsewhere
    )

    with pytest.raises(ValueError, match="repository scope"):
        search_memory._require_matching_repository(
            tmp_path, expected, deadline=None, cancelled=None
        )
