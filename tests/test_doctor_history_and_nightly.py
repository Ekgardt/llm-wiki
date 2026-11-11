"""Refused and quarantined transactions that later commits made history, and whether the nightly is current or stale."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.doctor_case_helpers import (
    give_doctor_a_generous_budget,
)
from tests.doctor_helpers import (
    _build_root,
    _check,
    _nightly_state,
    _qualified_pyright_check,
)

_budget_that_survives_a_slow_runner = pytest.fixture(autouse=True)(give_doctor_a_generous_budget)


def _transaction_database(
    state_root: Path,
    rows: list[tuple[str, str, str | None]],
    creates: dict[str, list[str]] | None = None,
) -> None:
    """A minimal coordinator database holding just the rows a check reads."""

    path = state_root / "run" / "markdown-transactions.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    database = sqlite3.connect(path)
    database.execute(
        'CREATE TABLE "transaction" (id TEXT PRIMARY KEY, operation_id TEXT, '
        "request_hash TEXT, state TEXT, preconditions_json TEXT, plan_hash TEXT, "
        "created_at TEXT, updated_at TEXT, parent_transaction_id TEXT, "
        "error_code TEXT, artifacts_pruned_at TEXT, owner_pid INTEGER)"
    )
    database.execute(
        "CREATE TABLE operation (transaction_id TEXT, position INTEGER, kind TEXT, "
        "path TEXT, before_hash TEXT, after_hash TEXT, parent_device INTEGER, "
        "parent_inode INTEGER, applied INTEGER)"
    )
    for identifier, state, parent in rows:
        database.execute(
            'INSERT INTO "transaction" (id, operation_id, request_hash, state, '
            "preconditions_json, plan_hash, created_at, updated_at, "
            "parent_transaction_id) VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?)",
            (
                identifier,
                f"compile:{identifier}",
                identifier * 2,
                state,
                identifier * 2,
                "2026-08-22T00:00:00Z",
                "2026-08-22T00:00:00Z",
                parent,
            ),
        )
        # Every state but preparing and discarded must carry its operations, or
        # the scan calls the row corrupt and the whole check turns error.
        database.execute(
            "INSERT INTO operation (transaction_id, position, kind, path, "
            "before_hash, after_hash, parent_device, parent_inode, applied) "
            "VALUES (?, 0, 'replace', 'knowledge/notes/page.md', ?, ?, 0, 0, 1)",
            (identifier, "c" * 64, "d" * 64),
        )
        for position, path in enumerate((creates or {}).get(identifier, ()), start=1):
            database.execute(
                "INSERT INTO operation (transaction_id, position, kind, path, "
                "before_hash, after_hash, parent_device, parent_inode, applied) "
                "VALUES (?, ?, 'create', ?, 'absent', ?, 0, 0, 1)",
                (identifier, position, path, "e" * 64),
            )
        (state_root / "run" / "transactions" / identifier).mkdir(parents=True, exist_ok=True)
    database.commit()
    database.close()


def test_a_quarantined_attempt_a_later_one_committed_is_history_not_a_problem(
    tmp_path, monkeypatch
):
    """Quarantine is retained evidence, so it must not be a permanent red light.

    Once a retry in the same chain commits, the refused attempt is history. The
    live vault would otherwise report `error` for as long as it keeps the
    evidence, which is exactly how an operator learns to stop reading it.
    """
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    _transaction_database(
        state_root,
        [("a" * 32, "quarantined", None), ("b" * 32, "committed", "a" * 32)],
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "transactions")

    assert check["details"]["quarantined_unresolved"] == 0, check["details"]
    assert check["details"]["states"]["quarantined"] == 1, check["details"]
    assert check["status"] == "ok", check["details"]


def test_a_refused_attempt_whose_pages_another_commit_wrote_is_history(
    tmp_path, monkeypatch
):
    """The ordinary recovery is a new attempt, not a retry of the same chain.

    Once the refusal is fixed the compile runs again with different inputs or
    dispositions, so it legitimately carries a different operation identity and
    the refused attempt gets no successor in its own chain — ever. What proves
    the work happened is the outcome: everything it meant to create exists,
    because a transaction that committed created it.
    """
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    receipt = "knowledge/daily/receipts/v3-written.md"
    _transaction_database(
        state_root,
        [("d" * 32, "quarantined", None), ("e" * 32, "committed", None)],
        creates={"d" * 32: [receipt], "e" * 32: [receipt]},
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "transactions")

    assert check["details"]["quarantined_unresolved"] == 0, check["details"]
    assert check["details"]["states"]["quarantined"] == 1, check["details"]
    assert check["status"] == "ok", check["details"]
    del home


def test_a_refused_attempt_whose_pages_nobody_wrote_still_needs_attention(
    tmp_path, monkeypatch
):
    """A page that exists nowhere is a real loss, and stays a finding."""
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    _transaction_database(
        state_root,
        [("f" * 32, "quarantined", None), ("a" * 32, "committed", None)],
        creates={"f" * 32: ["knowledge/notes/never-written.md"]},
    )

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "transactions")

    assert check["details"]["quarantined_unresolved"] == 1, check["details"]
    assert check["status"] == "error", check["details"]
    del home


_REFUSED_DAY = "knowledge/daily/2026-08-25.md"


def _refuse_day_receipts(coordinator, staged_day):
    from markdown_transaction import MarkdownChange, TransactionFailure

    record = json.dumps({"schema_version": "compile-receipt/v3", "source": {"logical_path": staged_day}})
    raw = b"---\ntype: compile-receipt\n---\n\n## Record\n```json\n" + record.encode() + b"\n```\n"
    changes = [MarkdownChange.create(f"knowledge/daily/receipts/v3-{digit * 64}.md", raw)
               for digit in ("1", "2")]
    transaction = coordinator.prepare(changes, operation_id="compile:refused-day",
                                      preconditions={"knowledge/notes/absent.md": "0" * 64})
    with pytest.raises(TransactionFailure):
        coordinator.apply(transaction.id)
    assert coordinator.transaction_state(transaction.id) == "quarantined"


def _publish_current_day(root, state_root, monkeypatch, coordinator):
    from llm_wiki import compile_pipeline_apply, compile_pipeline_batches, compile_pipeline_sources

    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    inputs = compile_pipeline_sources.snapshot_compile_inputs([root / _REFUSED_DAY])
    batch = compile_pipeline_batches.pack_compile_batches(inputs, model=None)[0]
    compile_pipeline_apply.apply_compile_plan(
        batch.inputs, {"schema_version": "compile-plan/v2", "operations": []},
        action_key="b" * 64, trigger="manual", coordinator=coordinator, batch=batch,
        provider_budget={"provider": "fake", "model": "test", "max_output_tokens": 4000},
    )


def _refused_compile_of_a_day(tmp_path, monkeypatch, *, compiled_now: bool, staged_day: str = _REFUSED_DAY):
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )
    from markdown_transaction import MarkdownCoordinator

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    day = b"# 2026-08-25\n\n## [10:00:00] event\nThe day as it stands now.\n"
    (root / "knowledge/daily/receipts").mkdir(parents=True)
    (root / _REFUSED_DAY).write_bytes(day)
    for relative in ("knowledge/index.md", "knowledge/log.md", "AGENTS.md"):
        (root / relative).write_bytes(b"# Synthetic test fixture\n")
    coordinator = MarkdownCoordinator(root, state_root)
    _refuse_day_receipts(coordinator, staged_day)
    if compiled_now:
        _publish_current_day(root, state_root, monkeypatch, coordinator)
    return _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "transactions")


def test_a_refused_compile_of_a_day_that_is_compiled_now_is_history(tmp_path, monkeypatch):
    """2026-08-25: a DLP refusal staged receipts for eight snapshots of one day.

    Those snapshots are gone and their receipts can never be written, but every
    part of the day as it stands now has a committed receipt: the work happened.
    """
    check = _refused_compile_of_a_day(tmp_path, monkeypatch, compiled_now=True)

    assert check["details"]["quarantined_unresolved"] == 0, check["details"]
    assert check["status"] == "ok", check["details"]


def test_a_refused_compile_of_a_day_still_uncompiled_needs_attention(tmp_path, monkeypatch):
    check = _refused_compile_of_a_day(tmp_path, monkeypatch, compiled_now=False)

    assert check["details"]["quarantined_unresolved"] == 1, check["details"]
    assert check["status"] == "error", check["details"]


def test_a_refused_attempt_staging_a_receipt_outside_the_days_needs_attention(tmp_path, monkeypatch):
    check = _refused_compile_of_a_day(
        tmp_path, monkeypatch, compiled_now=True, staged_day="knowledge/notes/not-a-day.md"
    )

    assert check["details"]["quarantined_unresolved"] == 1, check["details"]


def test_a_quarantined_attempt_with_no_successor_still_needs_attention(
    tmp_path, monkeypatch
):
    """The exemption is a successful retry, not the passage of time."""
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    _transaction_database(state_root, [("c" * 32, "quarantined", None)])

    check = _check(doctor.run_doctor(root=root, state_root=state_root, home=home), "transactions")

    assert check["details"]["quarantined_unresolved"] == 1, check["details"]
    assert check["status"] == "error", check["details"]


def test_a_nightly_that_ran_last_night_is_current_before_tonight_runs(
    tmp_path, monkeypatch
):
    """The nightly runs at 03:00, so midnight to three is not staleness.

    Measured on this machine at 2026-08-22T00:06Z: the timer had run 21 hours
    earlier and would run again in under three, `last_nightly_status` was
    success, and the check said degraded at that same moment.
    """
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    now = datetime(2026, 8, 22, 0, 6, tzinfo=UTC)
    _nightly_state(
        state_root,
        {
            "last_nightly_status": "success",
            "last_nightly_date": "2026-08-21",
            "last_nightly_at": "2026-08-21T03:00:40+00:00",
        },
    )

    check = _check(
        doctor.run_doctor(root=root, state_root=state_root, home=home, now=now),
        "scheduler",
    )

    assert check["status"] == "ok"


def test_a_nightly_that_missed_its_window_is_still_stale(tmp_path, monkeypatch):
    """The buffer is a day plus slack, not an amnesty."""
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    now = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    _nightly_state(
        state_root,
        {
            "last_nightly_status": "success",
            "last_nightly_date": "2026-08-21",
            "last_nightly_at": "2026-08-21T03:00:40+00:00",
        },
    )

    check = _check(
        doctor.run_doctor(root=root, state_root=state_root, home=home, now=now),
        "scheduler",
    )

    assert check["status"] == "degraded"


def test_state_without_a_nightly_timestamp_keeps_the_old_day_rule(
    tmp_path, monkeypatch
):
    """An upgraded vault must not get noisier for lacking a new field."""
    import doctor
    from llm_wiki import (
        doctor_lsp,
    )

    root, state_root, home = _build_root(tmp_path)
    monkeypatch.setattr(doctor_lsp, "pyright_check", _qualified_pyright_check)
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    _nightly_state(
        state_root,
        {"last_nightly_status": "success", "last_nightly_date": "2026-08-22"},
    )

    check = _check(
        doctor.run_doctor(root=root, state_root=state_root, home=home, now=now),
        "scheduler",
    )

    assert check["status"] == "ok"
