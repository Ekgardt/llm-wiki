"""Every store that only grew has an owner that bounds it.

Measured 2026-09-24: 23 557 transaction rows (55 MB), 55 MB of finished capture
intents, a 924 KB hook log, 1.5 GB of benchmark runs, lock probes and staged state
links in `run/`, and code generations for a throwaway worktree. See
docs/research/2026-09-24-every-store-has-a-bound.md.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import doctor
import ephemeral_paths
import maintenance_helpers
import markdown_transaction
import memory_state
import reclaim_runtime_state
import repository_index
import repository_retention
import repository_worktrees
import retire_benchmark_runs
import scheduled_weekly
from markdown_transaction import MarkdownCoordinator

ROOT = Path(__file__).resolve().parent.parent


# --- throwaway checkouts ----------------------------------------------------


def test_the_temporary_and_job_directories_are_ephemeral(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "host"))
    job = tmp_path / "host" / "jobs" / "abc" / "tmp" / "clean"

    roots = ephemeral_paths.ephemeral_roots()
    verdicts = (
        ephemeral_paths.is_under(Path(tempfile.gettempdir()) / "x", roots),
        ephemeral_paths.is_under(job, roots),
        ephemeral_paths.is_under(ROOT, roots),
    )

    assert verdicts == (True, True, False)


def _job_checkout(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "host"))
    checkout = tmp_path / "host" / "jobs" / "abc" / "tmp" / "clean"
    checkout.mkdir(parents=True)
    return checkout


def test_a_checkout_in_the_job_directory_is_retired_and_not_refreshed(tmp_path: Path, monkeypatch) -> None:
    checkout = _job_checkout(tmp_path, monkeypatch)

    verdict = repository_retention._verdict({"checkout_root": str(checkout)})
    refreshed = repository_index._refresh_row({"checkout_root": str(checkout)}, None, time.monotonic() + 5)

    assert (verdict, repository_retention._kept_count(verdict)) == ("throwaway_checkout", 0)
    assert refreshed["status"] == "throwaway_not_refreshed"


def test_a_worktree_in_the_job_directory_is_not_followed(tmp_path: Path, monkeypatch) -> None:
    worktree = repository_worktrees.Worktree(
        path=_job_checkout(tmp_path, monkeypatch), branch=None, bare=False, prunable=False
    )

    assert repository_worktrees._indexable(worktree) is False


# --- transaction history ----------------------------------------------------


def _append(coordinator: MarkdownCoordinator, day: str, family: str = "post-tool"):
    return markdown_transaction._append_until_committed(
        coordinator,
        f"{family}:{day}",
        f"knowledge/daily/{day}.md",
        f"# {day}\n".encode(),
        deadline=time.monotonic() + 60,
        cancelled=None,
    )


def _transaction_count(coordinator: MarkdownCoordinator) -> int:
    with sqlite3.connect(coordinator.database_path) as database:
        return database.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0]


def test_settled_rows_past_the_window_are_dropped_and_recent_ones_kept(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    _append(coordinator, "2026-01-01")
    _append(coordinator, "2026-01-02")
    later = datetime.now(timezone.utc) + timedelta(days=markdown_transaction.HISTORY_RETENTION_DAYS + 1)
    soon = datetime.now(timezone.utc) + timedelta(days=3)

    unpruned = coordinator.prune_history(now=later)
    coordinator.prune(now=soon)
    kept = coordinator.prune_history(now=soon)
    dropped = coordinator.prune_history(now=later)

    assert (unpruned["transactions"], kept["transactions"], dropped["transactions"]) == (0, 0, 2)
    assert _transaction_count(coordinator) == 0


def test_a_row_something_reads_back_is_never_pruned(tmp_path: Path) -> None:
    """Compile receipts and archives read their transaction back; only breadcrumbs go."""
    root = tmp_path / "vault"
    (root / "knowledge/daily").mkdir(parents=True)
    coordinator = MarkdownCoordinator(root, tmp_path / "state")
    _append(coordinator, "2026-01-01", family="compile")
    _append(coordinator, "2026-01-02", family="user-prompt")
    coordinator.prune(now=datetime.now(timezone.utc) + timedelta(days=3))

    later = datetime.now(timezone.utc) + timedelta(days=markdown_transaction.HISTORY_RETENTION_DAYS + 1)
    dropped = coordinator.prune_history(now=later)

    assert (dropped["transactions"], _transaction_count(coordinator)) == (1, 1)


def test_doctor_counts_every_row_by_state_in_one_aggregate(tmp_path: Path) -> None:
    path = tmp_path / "coordinator.sqlite3"
    with sqlite3.connect(path) as database:
        database.execute('CREATE TABLE "transaction" (state TEXT)')
        database.executemany('INSERT INTO "transaction" VALUES (?)', [("committed",)] * 3 + [("quarantined",)])

    assert doctor._transaction_state_totals(path) == {"committed": 3, "quarantined": 1}


# --- run/ debris and logs ---------------------------------------------------


def _aged(path: Path, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_old_lock_probes_are_swept_and_fresh_ones_kept(tmp_path: Path) -> None:
    old = tmp_path / ".llm-wiki-lock-probe-aaaa.sqlite3"
    journal = tmp_path / ".llm-wiki-lock-probe-aaaa.sqlite3-journal"
    fresh = tmp_path / ".llm-wiki-lock-probe-bbbb.sqlite3"
    for path in (old, journal, fresh):
        path.write_bytes(b"x")
    _aged(old, 7200)
    _aged(journal, 7200)

    swept = reclaim_runtime_state.sweep_orphan_temporaries(tmp_path)

    assert (swept["removed"], old.exists(), journal.exists(), fresh.exists()) == (2, False, False, True)


def test_empty_intent_shards_are_removed_and_full_ones_kept(tmp_path: Path) -> None:
    (tmp_path / "00").mkdir()
    (tmp_path / "01").mkdir()
    (tmp_path / "01" / "intent.json").write_text("{}", encoding="utf-8")

    removed = reclaim_runtime_state.remove_empty_intent_shards(tmp_path)

    assert (removed, (tmp_path / "00").exists(), (tmp_path / "01").exists()) == (1, False, True)


def test_keeping_the_previous_state_never_leaves_a_staged_link(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(memory_state, "STATE_FILE", state)

    memory_state._keep_previous(True)
    memory_state._keep_previous(True)  # `.previous` already is this inode

    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


def test_the_hook_error_log_is_bounded_with_the_scheduler_logs() -> None:
    assert "hook-errors.log" in maintenance_helpers.SCHEDULER_LOG_NAMES


# --- the queue --------------------------------------------------------------


def test_the_weekly_purges_finished_queue_work_into_the_private_archive() -> None:
    steps = {label: command for _message, label, command, _timeout in scheduled_weekly._script_steps()}
    command = steps["queue_purge"]
    before = datetime.fromisoformat(command[command.index("--terminal-before") + 1])
    export = Path(command[command.index("--export") + 1])

    assert (command[2], "--include-dead" in command) == ("purge", True)
    assert datetime.now(timezone.utc) - before > timedelta(days=29)
    assert export.parent == ROOT / "knowledge" / "raw" / "queue-archive"


# --- benchmark runs ---------------------------------------------------------


def _benchmark_directory(parent: Path, name: str, age_days: float) -> None:
    directory = parent / name
    directory.mkdir()
    (directory / "data.json").write_text("{}", encoding="utf-8")
    _aged(directory / "data.json", age_days * 86400)
    _aged(directory, age_days * 86400)


def test_old_runs_go_and_datasets_and_recent_runs_stay(tmp_path: Path) -> None:
    _benchmark_directory(tmp_path, "longmemeval", 40)
    _benchmark_directory(tmp_path, "full-2026-08-01", 40)
    _benchmark_directory(tmp_path, "full-2026-09-20", 1)
    now = time.time()

    removed = retire_benchmark_runs.retire(tmp_path, now)

    assert removed == ["full-2026-08-01"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["full-2026-09-20", "longmemeval"]


def test_the_dataset_names_match_the_benchmark_data_modules() -> None:
    pattern = re.compile(r'"cache"\s*/\s*"benchmarks"\s*/\s*"(\w+)"')
    declared = {
        match
        for path in (ROOT / "benchmark").glob("*_data.py")
        for match in pattern.findall(path.read_text(encoding="utf-8"))
    }

    assert declared == retire_benchmark_runs.DATASET_CACHES
