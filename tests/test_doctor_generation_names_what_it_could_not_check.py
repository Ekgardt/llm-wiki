from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import generation_maintenance
import pytest
from llm_wiki import doctor_generation
from llm_wiki.generation_catalog_store import GenerationCatalog

from tests.slow_machine import LONG_TIMEOUT

GENERATIONS = Path("cache") / "evidence-graph" / "generations"


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache" / "evidence-graph").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _catalog_path(state: Path) -> Path:
    return state / "cache" / "evidence-graph" / "catalog.sqlite3"


def _check(root: Path, state: Path, deadline: float) -> dict[str, Any]:
    result: dict[str, Any] = doctor_generation.generation_check(
        root, state, datetime.now(UTC), deadline=deadline
    )
    return result


def _generous() -> float:
    return time.monotonic() + LONG_TIMEOUT


def _built_generation(root: Path, state: Path) -> Path:
    note = root / "knowledge" / "notes" / "first.md"
    note.write_text("---\ntype: concept\n---\n# First\n\nA first page.\n", encoding="utf-8")
    built = generation_maintenance.run_generation_maintenance(
        root=root, state_root=state, time_budget_seconds=LONG_TIMEOUT, max_sources=10
    )
    assert built.get("generation_id"), built
    return state / GENERATIONS / str(built["generation_id"])


def test_a_linked_catalog_is_reported_unsafe(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    real = tmp_path / "elsewhere.sqlite3"
    real.write_bytes(b"")
    _catalog_path(state).symlink_to(real)
    result = _check(root, state, _generous())
    assert result["status"] == "error"
    assert result["message"] == "Evidence generation catalog is unsafe."
    assert result["details"]["catalog"] == "invalid"


def test_a_catalog_without_its_tables_is_invalid(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    database = sqlite3.connect(_catalog_path(state))
    database.execute("CREATE TABLE unrelated (value TEXT)")
    database.commit()
    database.close()
    result = _check(root, state, _generous())
    assert result["status"] == "error"
    assert result["details"]["catalog"] == "invalid"
    assert result["details"]["catalog_schema"] == "unknown"
    assert result["details"]["active_generation"] is None


def test_a_catalog_in_write_ahead_mode_breaks_its_durability_contract(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    GenerationCatalog(state)
    database = sqlite3.connect(_catalog_path(state))
    assert database.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    database.close()
    result = _check(root, state, _generous())
    assert result["status"] == "error"
    assert result["message"] == "Evidence generation catalog or active artifacts are invalid."


def test_a_check_out_of_time_without_an_active_generation_reports_no_vectors(
    tmp_path: Path,
) -> None:
    root, state = _vault(tmp_path)
    GenerationCatalog(state)
    result = _check(root, state, time.monotonic() - 1.0)
    assert result["status"] == "degraded"
    assert result["message"] == "Evidence generation check was deferred by its time bound."
    assert result["details"]["budget_exhausted"] is True
    assert result["details"]["vector_state"] == "unknown"
    assert result["details"]["vector_model"] is None


def test_a_check_out_of_time_still_says_the_active_vectors_are_absent(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    generation = _built_generation(root, state)
    result = _check(root, state, time.monotonic() - 1.0)
    assert result["status"] == "degraded"
    assert "Vectors are absent" in result["message"]
    assert result["details"]["budget_exhausted"] is False
    assert result["details"]["vector_state"] == "absent"
    assert generation.is_dir()


def _break_evidence(generation: Path) -> None:
    evidence = generation / "evidence.sqlite3"
    evidence.chmod(0o600)
    with evidence.open("r+b") as output:
        output.seek(200)
        output.write(b"corrupted evidence bytes")


def test_an_invalid_generation_still_reports_its_intact_search_index(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    generation = _built_generation(root, state)
    _break_evidence(generation)
    result = _check(root, state, _generous())
    assert result["status"] == "error"
    assert result["details"]["search_schema"] == "corpus-search"
    assert result["details"]["search_index"] == "valid"
    assert result["details"]["search_integrity"] == "valid"


def test_an_invalid_generation_whose_search_index_is_a_directory_is_corrupt(
    tmp_path: Path,
) -> None:
    root, state = _vault(tmp_path)
    generation = _built_generation(root, state)
    _break_evidence(generation)
    search = generation / "search.sqlite3"
    search.chmod(0o600)
    search.unlink()
    search.mkdir()
    result = _check(root, state, _generous())
    assert result["status"] == "error"
    assert result["details"]["search_index"] == "corrupt"
    assert result["details"]["search_integrity"] == "invalid"


def test_an_invalid_generation_of_another_schema_is_not_searched(tmp_path: Path) -> None:
    generation = tmp_path / "g1"
    state: dict[str, Any] = {
        "generation_path": generation,
        "diagnostic_manifest": {"schema_version": "corpus-generation/v1"},
        "invalid_details": {},
    }
    doctor_generation._diagnose_invalid_generation(state, tmp_path, _generous())
    assert state["invalid_details"] == {}


def test_a_changed_collector_or_extractor_makes_the_corpus_extraction_stale() -> None:
    manifest = {"collector_version": "c1", "extractor_version": "e1"}
    assert doctor_generation._corpus_extraction_state(manifest, "c2", "e1") == "stale"
    assert doctor_generation._corpus_extraction_state(manifest, "c1", "e2") == "stale"
    assert doctor_generation._corpus_extraction_state(manifest, "c1", "e1") == "current"


def test_a_generation_without_a_manifest_seal_is_aged_by_its_catalog() -> None:
    seal = (SimpleNamespace(path="search.sqlite3", mtime_ns=1),)
    catalog_info = SimpleNamespace(st_mtime_ns=42)
    assert doctor_generation._generation_age_source(seal, catalog_info) == (
        42,
        "catalog_mtime",
    )


def test_a_generation_with_no_timestamp_cannot_be_aged() -> None:
    with pytest.raises(OSError, match="age timestamp is unavailable"):
        doctor_generation._generation_age_source((), None)


def test_a_result_without_capacity_usage_keeps_its_status() -> None:
    result: dict[str, Any] = {"status": "ok", "message": "fine.", "details": {}}
    doctor: Any = doctor_generation
    returned = doctor._with_generation_capacity(result, None)
    assert returned["status"] == "ok"
    assert returned["details"] == {"capacity_usage": None}
