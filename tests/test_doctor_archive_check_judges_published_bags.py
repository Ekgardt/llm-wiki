from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from archive_daily import DailyArchiver
from llm_wiki import doctor_queue, monotonic_deadline
from markdown_transaction import MarkdownCoordinator

ArchivedVault = tuple[Path, Path, Path]


def _vault_skeleton(root: Path) -> Path:
    for relative in ("knowledge/daily/receipts", "knowledge/notes"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Knowledge Index\n")
    (root / "knowledge/log.local.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"contract\n")
    daily = root / "knowledge/daily/2026-01-01.md"
    daily.write_bytes(b"# day\n## [evt-1] event\ncompiled evidence\n")
    return daily


def _compile_daily(root: Path, state_root: Path, daily: Path) -> None:
    from llm_wiki import compile_pipeline_apply, compile_pipeline_batches, compile_pipeline_sources

    inputs = compile_pipeline_sources.snapshot_compile_inputs([daily])
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    compile_pipeline_apply.apply_compile_plan(
        inputs,
        {"schema_version": "compile-plan/v2", "operations": []},
        action_key="b" * 64,
        trigger="manual",
        coordinator=MarkdownCoordinator(root, state_root),
        completed_at="2026-07-01T00:00:00Z",
        batch=batch,
        provider_budget={"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000},
    )
    database_path = state_root / "run/markdown-transactions.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            'UPDATE "transaction" SET updated_at="2026-05-01T00:00:00Z" '
            'WHERE operation_id LIKE "compile:%"'
        )


def _archiver(root: Path, state_root: Path) -> DailyArchiver:
    return DailyArchiver(root, state_root, clock=lambda: datetime(2026, 7, 14, 12, tzinfo=UTC))


@pytest.fixture
def archived_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ArchivedVault]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    daily = _vault_skeleton(root)
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    _compile_daily(root, state_root, daily)
    receipt = _archiver(root, state_root).archive(daily.stem)
    yield root, state_root, receipt.bag_path
    _make_writable(root)


def _make_writable(root: Path) -> None:
    for directory in (path for path in root.rglob("*") if path.is_dir()):
        directory.chmod(0o700)


def _check(root: Path, state_root: Path) -> dict[str, Any]:
    return doctor_queue.archive_check(root, state_root)


def _archive(root: Path) -> Path:
    return root / "knowledge/daily/archive"


def test_a_published_bag_with_its_derived_index_is_healthy(archived_vault: ArchivedVault) -> None:
    root, state_root, _ = archived_vault
    _archiver(root, state_root).rebuild_index()

    check = _check(root, state_root)

    assert (check["id"], check["status"], check["message"]) == (
        "archives",
        "ok",
        "Archive state is healthy.",
    )
    assert (check["details"]["bags"], check["details"]["index"]) == (1, "valid")
    assert check["details"]["deletion_codes"] == []


def test_a_bag_without_an_index_is_counted_and_the_index_named_missing(
    archived_vault: ArchivedVault,
) -> None:
    root, state_root, _ = archived_vault
    (_archive(root) / "archive-index.json").unlink()

    check = _check(root, state_root)

    assert (check["status"], check["details"]["bags"], check["details"]["index"]) == (
        "ok",
        1,
        "missing",
    )


def test_a_copied_bag_is_a_duplicate_the_index_no_longer_matches(
    archived_vault: ArchivedVault,
) -> None:
    root, state_root, bag = archived_vault
    _archiver(root, state_root).rebuild_index()
    bag.parent.chmod(0o700)
    shutil.copytree(bag, bag.parent / "bag-copy")

    check = _check(root, state_root)

    assert check["status"] == "degraded"
    assert check["message"] == "Archive state requires operator attention."
    assert (check["details"]["duplicates"], check["details"]["index"]) == (1, "invalid")


def test_a_bag_without_a_readable_manifest_is_an_archive_error(
    archived_vault: ArchivedVault,
) -> None:
    root, state_root, bag = archived_vault
    bag.parent.chmod(0o700)
    (bag.parent / "bag-empty").mkdir()

    check = _check(root, state_root)

    assert check["status"] == "error"
    assert check["details"]["codes"] == ["archive_manifest_invalid"]
    assert check["details"]["bags"] == 2


@pytest.mark.parametrize(
    "index_bytes",
    [
        b'{"schema_version": "archive-index/v0", "bags": []}',
        b"[1, 2]",
        b"not json",
    ],
)
def test_an_index_of_the_wrong_schema_or_shape_is_invalid(
    archived_vault: ArchivedVault, index_bytes: bytes
) -> None:
    root, state_root, _ = archived_vault
    (_archive(root) / "archive-index.json").write_bytes(index_bytes)

    check = _check(root, state_root)

    assert (check["status"], check["details"]["index"]) == ("degraded", "invalid")


def test_an_index_listing_a_bag_twice_is_invalid(archived_vault: ArchivedVault) -> None:
    root, state_root, bag = archived_vault
    entry = {"bag_path": bag.relative_to(root).as_posix()}
    index = {"schema_version": "archive-index/v1", "bags": [entry, entry]}
    (_archive(root) / "archive-index.json").write_text(json.dumps(index))

    assert _check(root, state_root)["details"]["index"] == "invalid"


def test_an_index_that_is_a_directory_is_invalid(archived_vault: ArchivedVault) -> None:
    root, state_root, _ = archived_vault
    (_archive(root) / "archive-index.json").unlink()
    (_archive(root) / "archive-index.json").mkdir()

    assert _check(root, state_root)["details"]["index"] == "invalid"


def test_no_archive_directory_is_healthy(tmp_path: Path) -> None:
    check = _check(tmp_path, tmp_path)

    assert (check["status"], check["message"]) == ("ok", "No archive exists.")


def test_an_archive_root_that_is_a_file_is_refused_as_unsafe(tmp_path: Path) -> None:
    _archive(tmp_path).parent.mkdir(parents=True)
    _archive(tmp_path).write_bytes(b"not a directory")

    check = _check(tmp_path, tmp_path)

    assert (check["status"], check["message"]) == ("error", "Archive root is unsafe.")
    assert check["details"]["read_error"] is True
    assert check["details"]["deletion_codes"] == ["archive_state_unreadable"]


def test_retained_quarantine_entries_keep_run_from_deletion(tmp_path: Path) -> None:
    quarantine = tmp_path / "run/archive-quarantine"
    quarantine.mkdir(parents=True)
    (quarantine / "bag-1.json").write_bytes(b"{}")

    check = _check(tmp_path, tmp_path)

    assert check["details"]["quarantined"] == 1
    assert check["details"]["deletion_codes"] == ["archive_quarantine_retained"]
    assert check["details"]["read_error"] is False


def test_a_quarantine_entry_that_is_not_a_file_leaves_the_state_unknown(tmp_path: Path) -> None:
    quarantine = tmp_path / "run/archive-quarantine"
    (quarantine / "nested").mkdir(parents=True)

    check = _check(tmp_path, tmp_path)

    assert check["details"]["read_error"] is True
    assert check["details"]["deletion_codes"] == [
        "archive_quarantine_retained",
        "archive_quarantine_state_unknown",
    ]


def _month_directories(root: Path, count: int) -> None:
    for number in range(count):
        (_archive(root) / f"{1900 + number // 12}-{number % 12 + 1:02d}").mkdir(parents=True)


def test_more_months_than_the_scan_reads_are_reported_truncated(tmp_path: Path) -> None:
    _month_directories(tmp_path, 122)
    (_archive(tmp_path) / "not-a-month").mkdir()

    check = _check(tmp_path, tmp_path)

    assert check["status"] == "error"
    assert check["details"]["codes"] == ["archive_scan_truncated"]
    assert check["details"]["deletion_codes"] == ["archive_state_unknown"]


def _bag_directories(month: Path, count: int) -> None:
    for number in range(count):
        (month / f"bag-{number}").mkdir(parents=True)


def test_bags_beyond_the_entry_bound_are_cut_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_queue, "MAX_RUNTIME_ENTRIES", 2)
    _bag_directories(_archive(tmp_path) / "2026-01", 3)

    details = _check(tmp_path, tmp_path)["details"]

    assert details["bags"] == 2
    assert "archive_scan_truncated" in details["codes"]
    assert "archive_state_unknown" in details["deletion_codes"]


def test_a_month_listing_past_the_bound_counts_only_bag_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_queue, "MAX_RUNTIME_ENTRIES", 3)
    month = _archive(tmp_path) / "2026-01"
    _bag_directories(month, 2)
    for name in ("a.txt", "b.txt", "c.txt"):
        (month / name).write_bytes(b"x")

    details = _check(tmp_path, tmp_path)["details"]

    assert details["deletion_codes"] == ["archive_state_unknown"]
    assert details["bags"] <= 2


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
@pytest.mark.parametrize("locked", ["archive", "month"])
def test_an_unlistable_archive_or_month_is_unreadable(tmp_path: Path, locked: str) -> None:
    month = _archive(tmp_path) / "2026-01"
    month.mkdir(parents=True)
    target = {"archive": _archive(tmp_path), "month": month}[locked]
    target.chmod(0)
    try:
        check = _check(tmp_path, tmp_path)
    finally:
        target.chmod(0o700)

    assert check["status"] == "error"
    assert "archive_unreadable" in check["details"]["codes"]
    assert check["details"]["deletion_codes"] == ["archive_state_unreadable"]


def test_a_deadline_that_passes_among_the_bags_stops_the_scan_as_unreadable(
    archived_vault: ArchivedVault, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, state_root, _ = archived_vault
    monkeypatch.setattr(monotonic_deadline, "deadline_reached", _reached_on_bag_reads())

    check = _check(root, state_root)

    assert check["details"]["codes"] == ["archive_unreadable"]
    assert check["details"]["read_error"] is True


def _reached_on_bag_reads() -> Callable[[float], bool]:
    def reached(deadline: float) -> bool:
        return sys._getframe(1).f_code.co_name == "_scan_archive_manifests"

    return reached
