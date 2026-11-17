from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import generation_maintenance
import pytest
import reliable_memory
from corpus_snapshot import collect_corpus
from llm_wiki.generation_catalog_store import GenerationCatalog

from tests.generation_catalog_helpers import NOW, _publish
from tests.slow_machine import LONG_TIMEOUT

NOTE = "---\ntype: concept\n---\n# {title}\n\nA page about {title}.\n"
PROJECT_STATE = "---\ntype: project-state\n---\n# Lantern\n\nThe lantern project.\n"


def _vault(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state = tmp_path / "state"
    (root / "knowledge" / "notes").mkdir(parents=True)
    (root / "knowledge" / "projects").mkdir(parents=True)
    (state / "cache").mkdir(parents=True)
    (state / "run").mkdir(parents=True)
    return root, state


def _note(root: Path, title: str) -> None:
    page = root / "knowledge" / "notes" / f"{title}.md"
    page.write_text(NOTE.format(title=title), encoding="utf-8")


def _maintain(root: Path, state: Path, **options: Any) -> dict[str, Any]:
    outcome: dict[str, Any] = generation_maintenance.run_generation_maintenance(
        root=root, state_root=state, **options
    )
    return outcome


def test_a_naive_time_is_read_as_utc() -> None:
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert generation_maintenance.as_utc(naive) == naive.replace(tzinfo=UTC)
    assert generation_maintenance.parse_utc("2026-01-02T03:04:05") == naive.replace(tzinfo=UTC)
    assert generation_maintenance.parse_utc("not a time") is None


def test_a_path_that_cannot_be_resolved_is_not_within_a_root(tmp_path: Path) -> None:
    assert not generation_maintenance.within(Path("bad\x00name"), tmp_path)


def test_each_kind_of_entry_is_named(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    fifo = root / "pipe"
    os.mkfifo(fifo)
    too_long = root / ("x" * 300)
    assert generation_maintenance.safe_kind(outside, root)[0] == "outside"
    assert generation_maintenance.safe_kind(fifo, root)[0] == "special"
    assert generation_maintenance.safe_kind(too_long, root) == ("unsafe", None)


def test_a_schema_read_past_its_deadline_stops() -> None:
    with (
        closing(sqlite3.connect(":memory:")) as database,
        pytest.raises(TimeoutError, match="database schema deadline"),
    ):
        generation_maintenance.database_table_columns(database, "t", time.monotonic() - 1)


def test_a_table_wider_than_the_column_limit_is_refused() -> None:
    columns = ", ".join(f"c{index}" for index in range(257))
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute(f"CREATE TABLE wide ({columns})")
        with pytest.raises(sqlite3.DatabaseError, match="exceeds column limit"):
            generation_maintenance.database_table_columns(database, "wide")


def test_a_catalog_without_its_state_row_has_no_active_pointer() -> None:
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE catalog_state (singleton, active_generation_id)")
        assert generation_maintenance.active_pointer(database) is None


def _raising(error: BaseException) -> Callable[..., object]:
    def raise_it(*_args: object, **_kwargs: object) -> object:
        raise error

    return raise_it


def test_an_unknown_filesystem_type_degrades_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reliable_memory, "known_network_path", _raising(OSError("mounts")))
    checked = generation_maintenance.filesystem_check(tmp_path)
    assert checked["status"] == "degraded"
    assert checked["details"]["read_error"] is True


def test_a_failed_locking_probe_leaves_locking_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(reliable_memory, "sqlite_lock_probe", _raising(sqlite3.Error("probe")))
    checked = generation_maintenance.filesystem_check(tmp_path)
    assert checked["status"] == "degraded"
    assert checked["details"]["locking"] == "unknown"


@pytest.mark.parametrize("deadline", [True, float("nan")])
def test_a_doctor_deadline_must_be_a_finite_number(deadline: float) -> None:
    with pytest.raises(ValueError, match="finite monotonic timestamp"):
        generation_maintenance.validated_doctor_deadline(deadline, 1.0)


@pytest.mark.parametrize("budget", [True, 0, float("inf"), "60"])
def test_a_maintenance_budget_must_be_positive_and_finite(tmp_path: Path, budget: Any) -> None:
    root, state = _vault(tmp_path)
    with pytest.raises(ValueError, match="time_budget_seconds must be positive"):
        _maintain(root, state, time_budget_seconds=budget)


@pytest.mark.parametrize("limit", [True, 0, "10"])
def test_a_source_limit_must_be_a_positive_integer(tmp_path: Path, limit: Any) -> None:
    root, state = _vault(tmp_path)
    with pytest.raises(ValueError, match="max_sources must be a positive integer"):
        _maintain(root, state, max_sources=limit)


def test_a_network_runtime_refuses_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state = _vault(tmp_path)
    monkeypatch.setattr(reliable_memory, "known_network_path", lambda _path: True)
    outcome = _maintain(root, state, max_sources=10)
    assert outcome["status"] == "error"
    assert outcome["reason"] == "unsupported_filesystem"


def test_a_budget_spent_before_the_filesystem_check_defers_maintenance(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    outcome = _maintain(root, state, time_budget_seconds=1e-9, max_sources=10)
    assert outcome["status"] == "deferred"
    assert outcome["reason"] == "time_limit"


def test_a_project_page_is_extracted_into_the_generation(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    _note(root, "lantern")
    project = root / "knowledge" / "projects" / "lantern"
    project.mkdir()
    (project / "state.md").write_text(PROJECT_STATE, encoding="utf-8")
    outcome = _maintain(root, state, time_budget_seconds=LONG_TIMEOUT, max_sources=10)
    assert outcome["status"] == "built", outcome
    assert outcome["sources"] == 2


def test_a_corpus_larger_than_its_source_limit_is_deferred(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    _note(root, "first")
    _note(root, "second")
    outcome = _maintain(root, state, time_budget_seconds=LONG_TIMEOUT, max_sources=1)
    assert outcome["status"] == "deferred"
    assert outcome["reason"] == "source_limit"


@pytest.mark.parametrize(
    ("error", "status", "reason"),
    [
        (PermissionError("denied"), "error", "PermissionError"),
        (ValueError("total byte limit reached"), "deferred", "corpus_byte_limit"),
        (ValueError("unexpected shape"), "error", "ValueError"),
    ],
)
def test_a_failed_refresh_names_its_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status: str,
    reason: str,
) -> None:
    root, state = _vault(tmp_path)
    monkeypatch.setattr(generation_maintenance, "refreshed_generation", _raising(error))
    outcome = _maintain(root, state, time_budget_seconds=LONG_TIMEOUT, max_sources=10)
    assert (outcome["status"], outcome["reason"]) == (status, reason)


@pytest.fixture
def held_maintenance(tmp_path: Path) -> Iterator[tuple[Path, Path, Any, dict[str, object]]]:
    root, state = _vault(tmp_path)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state, datetime.now(UTC))
    assert acquired is not None
    coordinator, lease = acquired
    yield root, state, coordinator, lease
    generation_maintenance.release_maintenance_owner(coordinator, lease)


def test_a_busy_maintenance_fence_names_its_holder(
    held_maintenance: tuple[Path, Path, Any, dict[str, object]],
) -> None:
    root, state, _coordinator, _lease = held_maintenance
    outcome = _maintain(root, state, time_budget_seconds=LONG_TIMEOUT, max_sources=10)
    assert outcome["reason"] == "maintenance_owner_busy"
    assert outcome["details"]["holder"]["present"] is True


def test_an_unreadable_fence_holder_is_reported_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import markdown_transaction

    root, state = _vault(tmp_path)
    monkeypatch.setattr(
        markdown_transaction, "active_or_legacy_coordinator", _raising(OSError("gone"))
    )
    assert generation_maintenance._maintenance_holder(root, state) == {"present": None}


def test_a_released_fence_cannot_be_released_again(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state, datetime.now(UTC))
    assert acquired is not None
    coordinator, lease = acquired
    generation_maintenance.release_maintenance_owner(coordinator, lease)
    with pytest.raises(RuntimeError, match="maintenance_owner_fence_lost"):
        generation_maintenance.release_maintenance_owner(coordinator, lease)


def test_a_heartbeat_past_its_deadline_refuses_to_start(
    held_maintenance: tuple[Path, Path, Any, dict[str, object]],
) -> None:
    _root, _state, coordinator, lease = held_maintenance
    heartbeat = generation_maintenance.MaintenanceHeartbeat(
        coordinator, lease, deadline=time.monotonic() - 1
    )
    with pytest.raises(TimeoutError, match="heartbeat fence reached"):
        heartbeat.check()


def test_a_heartbeat_cleanup_returns_what_the_cleanup_returned(tmp_path: Path) -> None:
    root, state = _vault(tmp_path)
    acquired = generation_maintenance.acquire_maintenance_owner(root, state, datetime.now(UTC))
    assert acquired is not None
    coordinator, lease = acquired
    heartbeat: Any = generation_maintenance.MaintenanceHeartbeat(
        coordinator, lease, deadline=time.monotonic() + LONG_TIMEOUT
    )
    with heartbeat as guard:
        assert guard.cleanup(lambda value: value * 2, 21) == 42


def test_a_missing_owner_row_is_not_ours() -> None:
    assert not generation_maintenance._owner_row_is_ours(None, {"token": "t", "epoch": 1})


def _catalog(tmp_path: Path) -> GenerationCatalog:
    state_root = tmp_path / "catalog-state"
    state_root.mkdir()
    return GenerationCatalog(state_root, clock=lambda: NOW)


def _is_removable(catalog: GenerationCatalog, path: Path, deadline: float) -> bool:
    return generation_maintenance._removable_generation_orphan(
        path, path.name, catalog.state_root, catalog, deadline, None
    )


def test_an_intact_or_linked_unregistered_generation_is_not_removable(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "g1")
    link = catalog.generations_path / "g2"
    link.symlink_to(directory, target_is_directory=True)
    assert not _is_removable(catalog, directory, time.monotonic() + LONG_TIMEOUT)
    assert not _is_removable(catalog, link, time.monotonic() + LONG_TIMEOUT)


def test_an_orphan_check_past_its_deadline_stops(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "g1")
    with pytest.raises(TimeoutError):
        _is_removable(catalog, directory, time.monotonic() - 1)


def _cancelled_after(checks: int) -> Callable[[], bool]:
    seen: list[bool] = []

    def cancelled() -> bool:
        seen.append(True)
        return len(seen) > checks

    return cancelled


def test_an_orphan_cleanup_cancelled_between_children_stops(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "g1")
    with pytest.raises(TimeoutError, match="generation cleanup deadline reached"):
        generation_maintenance._cleanup_generation_orphans(
            catalog,
            catalog.state_root,
            set(),
            time.monotonic() + LONG_TIMEOUT,
            _cancelled_after(2),
        )


def test_an_unowned_record_or_node_leaves_the_partition_unchanged() -> None:
    state = generation_maintenance._new_partition_state(("a",))
    generation_maintenance._reference_node(state, None, "a")
    assert state.node_references == {}
    dependency = {"source_id": None, "dependent_node_id": "node"}
    assert generation_maintenance._dependency_owners(dependency, state) == ()


def test_a_partition_past_its_deadline_stops() -> None:
    result = SimpleNamespace(occurrences=())
    with pytest.raises(TimeoutError, match="partition deadline reached"):
        generation_maintenance.partition_code_extraction(result, (), deadline=time.monotonic() - 1)


def test_a_lost_activation_race_defers_the_build() -> None:
    built = SimpleNamespace(activated=False, generation_id="g1")
    snapshot = SimpleNamespace(sources=("a", "b"))
    outcome = generation_maintenance._generation_build_result(built, snapshot)
    assert outcome["reason"] == "activation_race"
    assert outcome["sources"] == 2


def test_extraction_bytes_that_differ_from_the_snapshot_are_refused(tmp_path: Path) -> None:
    root, _state = _vault(tmp_path)
    _note(root, "lantern")
    snapshot = collect_corpus(root, max_files=10)
    source = snapshot.sources[0]
    extractor = generation_maintenance.generation_source_extractor(snapshot, "repository")
    with pytest.raises(ValueError, match="incremental extraction bytes differ"):
        extractor(
            {"source_id": source.record.logical_id},
            b"other bytes",
            sources=None,
            source_bytes={},
            deadline=None,
            cancelled=None,
        )
    with pytest.raises(ValueError, match="knowledge extraction bytes differ"):
        generation_maintenance._require_snapshot_bytes(
            (source,), {source.record.logical_id: b"other bytes"}, "knowledge"
        )


def test_an_orphan_cleanup_keeps_a_registered_generation(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    directory, _manifest = _publish(catalog, "g1")
    removed = generation_maintenance._cleanup_generation_orphans(
        catalog, catalog.state_root, {"g1"}, time.monotonic() + LONG_TIMEOUT, None
    )
    assert removed == 0
    assert directory.is_dir()
