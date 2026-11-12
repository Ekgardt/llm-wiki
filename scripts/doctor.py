"""Agent-readable local health checks and conservative repairs."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

# One source of truth for the window; it was 30 in four files. See
# `docs/research/2026-09-02-where-undo-belongs-and-for-how-long.md`.
import generation_maintenance  # noqa: E402
from llm_wiki import (
    doctor_base,
    doctor_deletion,
    doctor_lsp,
    doctor_queue,
    doctor_repair,
    doctor_transactions,
    monotonic_deadline,
)
from llm_wiki.doctor_base import QUEUE_STATES as QUEUE_STATES
from llm_wiki.doctor_base import SCHEMA_VERSION as SCHEMA_VERSION
from llm_wiki.doctor_base import SUMMARY_LIMIT as SUMMARY_LIMIT
from llm_wiki.doctor_base import TRANSACTION_STATES as TRANSACTION_STATES
from llm_wiki.doctor_base import VALID_STATUSES as VALID_STATUSES
from llm_wiki.doctor_base import environment_check as environment_check
from llm_wiki.doctor_base import readonly_database as readonly_database
from llm_wiki.doctor_base import runtime_check as runtime_check
from llm_wiki.doctor_capture import capture_check as capture_check
from llm_wiki.doctor_capture import checkpoint_check as checkpoint_check
from llm_wiki.doctor_capture import hook_error_check as hook_error_check
from llm_wiki.doctor_capture import models_check as models_check
from llm_wiki.doctor_capture import scheduler_check as scheduler_check
from llm_wiki.doctor_capture import settings_check as settings_check
from llm_wiki.doctor_capture import tool_failure_check as tool_failure_check
from llm_wiki.doctor_deletion import claim_check as claim_check
from llm_wiki.doctor_deletion import deletion_snapshot as deletion_snapshot
from llm_wiki.doctor_deletion import run_deletion_check as run_deletion_check
from llm_wiki.doctor_generation import generation_check as generation_check
from llm_wiki.doctor_integrations import DEFERRED_BY_ACTION as DEFERRED_BY_ACTION
from llm_wiki.doctor_integrations import RepairContext as RepairContext
from llm_wiki.doctor_integrations import integration_check as integration_check
from llm_wiki.doctor_lsp import derived_deletion_codes as derived_deletion_codes
from llm_wiki.doctor_lsp import lsp_runtime_check as lsp_runtime_check
from llm_wiki.doctor_lsp import pyright_check as pyright_check
from llm_wiki.doctor_queue import archive_check as archive_check
from llm_wiki.doctor_queue import queue_check as queue_check
from llm_wiki.doctor_repair import BACKUP_FRESH_SECONDS as BACKUP_FRESH_SECONDS
from llm_wiki.doctor_repair import BACKUP_GIT_TIMEOUT_SECONDS as BACKUP_GIT_TIMEOUT_SECONDS
from llm_wiki.doctor_repair import adoption_check as adoption_check
from llm_wiki.doctor_repair import resolved_doctor_paths as resolved_doctor_paths
from llm_wiki.doctor_repair import run_repairs as run_repairs
from llm_wiki.doctor_repair import unfinished_result as unfinished_result
from llm_wiki.doctor_repair import validated_repairs as validated_repairs
from llm_wiki.doctor_schedule import mcp_check as mcp_check
from llm_wiki.doctor_transactions import operational_database_path as operational_database_path
from llm_wiki.doctor_transactions import transaction_check as transaction_check
from llm_wiki.install_inspection import installed_at
from secret_redact import describe_error


def _last_snapshot_at(root: Path) -> datetime | None:
    """The time of the snapshot repository's newest commit, or None."""
    if not (root / ".git").exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=BACKUP_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stamp = completed.stdout.strip()
    if completed.returncode != 0 or not stamp.isdigit():
        return None
    return datetime.fromtimestamp(int(stamp), tz=UTC)


def _no_snapshot_result(installed: datetime | None, now: datetime, details: dict) -> dict:
    """No snapshot yet: pending while the install is younger than the freshness
    bound (a vault that young has not missed one), a finding after it. See
    docs/research/2026-09-27-what-is-not-yet-due-is-not-a-warning.md.
    """
    if installed is None:
        return generation_maintenance.result("backup", "degraded", "No knowledge snapshot has been taken yet.", details)
    due_by = generation_maintenance.as_utc(installed) + timedelta(seconds=BACKUP_FRESH_SECONDS)
    details["first_snapshot_due_by"] = due_by.isoformat()
    if generation_maintenance.as_utc(now) <= due_by:
        message = f"No knowledge snapshot yet; the first is due by {due_by:%Y-%m-%d %H:%M} UTC."
        return generation_maintenance.result("backup", "ok", message, details)
    return generation_maintenance.result("backup", "degraded", "No knowledge snapshot has been taken yet.", details)


def _latest(*moments: datetime | None) -> datetime | None:
    known = [moment for moment in moments if moment is not None]
    return max(known, default=None)


def backup_check(home_path: Path, now: datetime, installed: datetime | None = None) -> dict:
    """Whether the memory's second copy was taken recently."""
    from snapshot_knowledge import last_checked_at, snapshot_root

    root = snapshot_root(home_path)
    taken = _latest(_last_snapshot_at(root), last_checked_at(root))
    details = {"last_snapshot_at": taken.isoformat() if taken else None}
    if taken is None:
        return _no_snapshot_result(installed, now, details)
    age_days = (generation_maintenance.as_utc(now) - taken).total_seconds() / 86400
    if age_days * 86400 > BACKUP_FRESH_SECONDS:
        return generation_maintenance.result("backup", "degraded", f"The last knowledge snapshot is {age_days:.1f} days old.", details)
    return generation_maintenance.result("backup", "ok", "The knowledge snapshot is current.", details)


def deferrable_checks(
    root_path: Path,
    state_path: Path,
    home_path: Path,
    generated_at: datetime,
) -> tuple[tuple[str, Callable[[float], dict]], ...]:
    """Each deferrable check, cheapest first, taking the deadline it may spend.

    Order is the budget policy. The generation check re-collects the whole
    corpus — 1.4 s on this vault on 2026-08-24, and it grows with the vault —
    while every other check reads one file or one table. Running it first meant
    it spent the entire budget and the six checks after it reported "not
    completed", which describes the clock rather than the vault. Last, it gets
    whatever the cheap checks did not need, and only it defers.
    """
    return (
        (
            "scheduler",
            lambda budget: scheduler_check(
                root_path, state_path, generated_at, budget, home_path
            ),
        ),
        ("capture", lambda budget: capture_check(root_path, state_path, budget)),
        ("tools", lambda budget: tool_failure_check(state_path, budget)),
        (
            "backup",
            lambda _budget: backup_check(home_path, generated_at, installed_at(state_path)),
        ),
        ("models", lambda _budget: models_check()),
        ("settings", lambda _budget: settings_check(root_path)),
        ("hooks", lambda _budget: hook_error_check(state_path, generated_at)),
        ("checkpoints", lambda _budget: checkpoint_check(state_path, generated_at)),
        ("mcp", lambda _budget: mcp_check(root_path)),
        (
            "integrations",
            lambda budget: integration_check(root_path, home_path, deadline=budget, state_root=state_path),
        ),
        (
            "pyright",
            lambda budget: doctor_lsp.pyright_check(root_path, state_path, deadline=budget),
        ),
        (
            "lsp",
            lambda budget: doctor_lsp.lsp_runtime_check(state_path, generated_at, deadline=budget),
        ),
        (
            "generation",
            lambda budget: generation_check(
                root_path, state_path, generated_at, budget
            ),
        ),
    )


def _completed_or_deferred(
    check_id: str, operation: Callable[[float], dict], deadline: float, share: float
) -> dict:
    """The LSP check owns its own budget; the rest defer once time is up."""
    if check_id != "lsp" and time.monotonic() >= deadline:
        return unfinished_result(check_id)
    return operation(share)


def unfinished_when_late(check: dict, deadline: float) -> dict:
    """A read that failed once the budget ran out is an unfinished check, not a broken store.

    Every check catches its deadline together with real read errors, and a
    SQLite read the deadline interrupts raises `sqlite3.OperationalError`; so
    under the default budget `queue` and `claims` said `error` and advised
    `--repair` on a healthy vault (audit A-9,
    docs/research/2026-09-25-a-late-check-is-unfinished-not-broken.md).
    """
    details = check.get("details") or {}
    if check.get("status") != "error" or not details.get("read_error"):
        return check
    if not monotonic_deadline.deadline_reached(deadline):
        return check
    unfinished = unfinished_result(check["id"])
    unfinished["details"]["deletion_codes"] = derived_deletion_codes(check)
    return unfinished


def _validate_health_scope(scope, repair, rebuild_generation):
    if scope not in {"full", "operational"}:
        raise ValueError("doctor scope must be full or operational")
    if scope != "full" and (repair or rebuild_generation):
        raise ValueError("doctor repairs require full scope")


def _operational_adoption_check(root, state_root, deadline):
    from llm_wiki.adoption_admission import (
        require_reliability_v3_admission,
    )
    from markdown_transaction import reliability_v3_records_present

    if not reliability_v3_records_present(state_root):
        return generation_maintenance.result("adoption", "degraded", "Reliability V3 admission is absent.", {"adopted": False})
    require_reliability_v3_admission(root=root, state_root=state_root, deadline=deadline)
    return generation_maintenance.result("adoption", "ok", "Current writer admission verified; retained content not certified.",
                   {"adopted": True, "retained_history": "not_checked"})


_OPERATIONAL_STORES = {
    "queue": ("queue", "SELECT state, COUNT(*) FROM tasks GROUP BY state", QUEUE_STATES),
    "transactions": ("coordinator", 'SELECT state, COUNT(*) FROM "transaction" GROUP BY state', TRANSACTION_STATES),
}
_OPERATIONAL_EXCLUDED = ("archives", "claims", "backup", "checkpoints", "mcp", "integrations", "pyright", "generation")


def _operational_store_details(state_root, name, deadline):
    database_name, query, _states = _OPERATIONAL_STORES[name]
    path = operational_database_path(state_root, database_name)
    with closing(doctor_base.readonly_database(path, state_root, deadline=deadline)) as database:
        database.execute("BEGIN")
        details = {"states": dict(database.execute(query).fetchall()), "retained_history": "not_checked"}
        if name == "queue":
            details["source_failures"] = database.execute("SELECT COUNT(*) FROM source_failures").fetchone()[0]
            details["dead_unresolved"] = database.execute(
                "SELECT COUNT(*) FROM tasks AS dead WHERE dead.state='dead' AND NOT EXISTS "
                "(SELECT 1 FROM tasks AS retry WHERE retry.redrive_of=dead.id AND retry.state!='cancelled')"
            ).fetchone()[0]
        return details


def _operational_store_status(name, details):
    states = details["states"]
    if set(states) - set(_OPERATIONAL_STORES[name][2]):
        return "error"
    needs_attention = details.get("source_failures", 0) + details.get("dead_unresolved", 0)
    needs_attention += sum(states.get(state, 0) for state in ("blocked", "quarantined", "conflicted"))
    if needs_attention:
        return "degraded"
    return "ok"


def _operational_store_check(state_root, name, deadline):
    details = _operational_store_details(state_root, name, deadline)
    status = _operational_store_status(name, details)
    message = f"Current {name} counts read; retained artifacts and historical outcomes not verified."
    if status != "ok":
        message = f"{name.title()} records need attention; see current counts and run full diagnosis."
    return generation_maintenance.result(name, status, message, details)


def _operational_capture_check(root, state_root, deadline, adoption):
    state = "unknown"
    error = adoption["message"]
    if adoption.get("details", {}).get("adopted"):
        state = "adopted"
        error = None
    return capture_check(root, state_root, deadline, adoption=(state, error))


def _operational_runs(root, state, home, now, deadline, adoption):
    return (
        ("environment", lambda: doctor_base.environment_check(root, state)),
        ("runtime", lambda: doctor_base.runtime_check(state)),
        ("capture", lambda: _operational_capture_check(root, state, deadline, adoption)),
        ("tools", lambda: tool_failure_check(state, deadline)),
        ("scheduler", lambda: scheduler_check(root, state, now, deadline, home)),
        ("models", models_check),
        ("settings", lambda: settings_check(root)),
        ("hooks", lambda: hook_error_check(state, now)),
        ("lsp", lambda: doctor_lsp.lsp_runtime_check(state, now, deadline=deadline)),
        ("filesystem", lambda: generation_maintenance.filesystem_check(state, deadline)),
        ("queue", lambda: _operational_store_check(state, "queue", deadline)),
        ("transactions", lambda: _operational_store_check(state, "transactions", deadline)),
    )


def _collect_operational_checks(root, state, home, now, deadline):
    adoption = _operational_isolated("adoption", lambda: _operational_adoption_check(root, state, deadline), deadline)
    checks = [adoption]
    for name, run in _operational_runs(root, state, home, now, deadline, adoption):
        checks.append(_operational_isolated(name, run, deadline))
    return checks


def _operational_isolated(name, run, deadline):
    return isolated(name, lambda: _operational_check_before_deadline(name, run, deadline))


def _operational_deadline_error(error, deadline):
    timed = isinstance(error, TimeoutError) or getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_INTERRUPT
    return timed and monotonic_deadline.deadline_reached(deadline)


def _operational_check_before_deadline(name, run, deadline):
    if monotonic_deadline.deadline_reached(deadline):
        return unfinished_result(name)
    try:
        return run()
    except (TimeoutError, sqlite3.OperationalError) as error:
        if _operational_deadline_error(error, deadline):
            return unfinished_result(name)
        raise


def _checks_for_scope(scope, context, home):
    collect = {"full": collect_checks, "operational": _collect_operational_checks}[scope]
    return collect(context.root_path, context.state_path, home, context.generated_at, context.deadline)


def _deletion_for_scope(scope, context, checks):
    if scope == "operational":
        return deletion_snapshot(["not_checked_in_operational_scope"])
    return run_deletion_check(context.state_path, context.generated_at, root=context.root_path,
                               deadline=context.deadline, collected={check["id"]: check for check in checks})


def _health_coverage(scope, checks):
    return {
        "retained_history": {"full": "requested", "operational": "not_checked"}[scope],
        "excluded_checks": list(_OPERATIONAL_EXCLUDED) if scope == "operational" else [],
        "unfinished_checks": [check["id"] for check in checks if check.get("details", {}).get("budget_exhausted")],
    }


def collect_checks(
    root_path: Path,
    state_path: Path,
    home_path: Path,
    generated_at: datetime,
    deadline: float,
) -> list[dict]:
    """Every check, each judged the moment it returns.

    Judging all of them after the last one called a corrupt store found early
    "not completed" whenever a later check ran past the deadline (audit
    2026-09-26 B-19, docs/research/2026-09-26-a-check-is-judged-when-it-ends.md).
    """
    runs = [
        ("environment", lambda: doctor_base.environment_check(root_path, state_path)),
        ("runtime", lambda: doctor_base.runtime_check(state_path)),
        ("adoption", lambda: doctor_repair.adoption_check(root_path, state_path, deadline=deadline)),
        ("filesystem", lambda: generation_maintenance.filesystem_check(state_path, deadline)),
        (
            "transactions",
            lambda: doctor_transactions.transaction_check(state_path, generated_at, deadline, vault_root=root_path),
        ),
        ("queue", lambda: doctor_queue.queue_check(state_path, generated_at, deadline)),
        ("archives", lambda: doctor_queue.archive_check(root_path, state_path, deadline)),
        ("claims", lambda: doctor_deletion.claim_check(root_path, state_path, deadline)),
    ]
    runs.extend(
        (check_id, _deferred_run(check_id, operation, deadline))
        for check_id, operation in deferrable_checks(root_path, state_path, home_path, generated_at)
    )
    return [unfinished_when_late(isolated(check_id, run), deadline) for check_id, run in runs]


def isolated(check_id: str, run) -> dict:
    """A check that raised is that check's error, named; the others still report.

    One unguarded check raised IndexError on a v3 queue owner and doctor printed
    nothing at all, so the installer's smoke saw no report and stopped the update
    (2026-09-29).
    """
    try:
        return run()
    except Exception as error:  # noqa: BLE001 - reported as the check's own error, with its cause
        return generation_maintenance.result(
            check_id,
            "error",
            f"The {check_id} check could not finish: {describe_error(error)}",
            {"exception": type(error).__name__},
        )


def _deferred_run(check_id: str, operation, deadline: float):
    return lambda: _completed_or_deferred(check_id, operation, deadline, deadline)


def _mark_repair_deferred(check: dict) -> None:
    check["status"] = "degraded"
    check["message"] = (
        f"{check['id'].title()} repair deferred because another owner "
        "holds the repair lock."
    )
    check["details"]["repair_deferred"] = True


def _mark_repair_failed(check: dict, errors: list[str]) -> None:
    check["status"] = "error"
    check["message"] = f"{check['id'].title()} repair failed."
    check["details"].pop("repair_deferred", None)
    check["details"]["repair_errors"] = errors


def _repair_errors_for_check(check_id: str, errors: dict[str, list[str]]) -> list[str]:
    """Repair action names and health check names are not always identical."""
    matched = []
    for action, reasons in errors.items():
        if check_id in DEFERRED_BY_ACTION.get(action, {action}):
            matched.extend(reasons)
    return matched


def apply_repair_outcomes(checks: list[dict], context: RepairContext) -> None:
    for check in checks:
        if check["id"] in context.repair_deferred:
            _mark_repair_deferred(check)
        errors = _repair_errors_for_check(check["id"], context.repair_errors)
        if errors:
            _mark_repair_failed(check, errors)


def _status_counts(checks: list[dict]) -> dict[str, int]:
    return {
        status: sum(check["status"] == status for check in checks)
        for status in VALID_STATUSES
    }


def _overall_status(counts: dict[str, int]) -> str:
    if counts["error"]:
        return "error"
    if counts["degraded"]:
        return "degraded"
    return "ok"


def _run_deletion_result(run_deletion: dict) -> dict:
    message = "Runtime history must be retained."
    if run_deletion["quiescent"]:
        message = (
            "Runtime state was observed quiescent; offline action is still required."
        )
    return generation_maintenance.result("run_deletion", "ok", message, run_deletion)


def run_doctor(
    root: Path | str | None = None,
    state_root: Path | str | None = None,
    home: Path | str | None = None,
    repair: bool = False,
    rebuild_generation: bool = False,
    repair_actions: set[str] | frozenset[str] | None = None,
    now: datetime | None = None,
    time_budget_seconds: float = generation_maintenance.DEFAULT_TIME_BUDGET_SECONDS,
    deadline: float | None = None,
    scope: str = "full",
) -> dict:
    """Return a JSON-safe local health report; mutate only with ``repair=True``.

    Without ``repair`` the one file doctor touches is the locking probe: it creates
    one temporary SQLite file in the state root and removes it, because whether two
    connections really exclude each other can only be learned by trying (audit
    2026-09-27 C-1, docs/research/2026-09-27-a-read-only-doctor-says-what-it-touches.md).
    Every other file, and the state it describes, is left as it was.
    """
    started = time.monotonic()
    _validate_health_scope(scope, repair, rebuild_generation)
    root_path, state_path, home_path = resolved_doctor_paths(root, state_root, home)
    generated_at = generation_maintenance.as_utc(now)
    context = RepairContext(
        root_path=root_path,
        state_path=state_path,
        generated_at=generated_at,
        deadline=generation_maintenance.validated_doctor_deadline(deadline, time_budget_seconds),
        rebuild_generation=rebuild_generation,
        selected_repairs=validated_repairs(repair_actions),
        repaired=[],
        repair_errors={},
        repair_deferred=set(),
    )
    if repair:
        run_repairs(context)

    checks = _checks_for_scope(scope, context, home_path)
    apply_repair_outcomes(checks, context)
    run_deletion = _deletion_for_scope(scope, context, checks)
    checks.append(_run_deletion_result(run_deletion))
    counts = _status_counts(checks)
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "coverage": _health_coverage(scope, checks),
        "duration_seconds": time.monotonic() - started,
        "generated_at": generated_at.isoformat(),
        "overall_status": _overall_status(counts),
        "repaired": context.repaired,
        "checks": checks,
        "counts": counts,
        "run_deletion": run_deletion,
    }


def degraded_summary(report: dict) -> str:
    """Return a compact bounded summary containing only actionable checks."""
    if report.get("overall_status") == "ok":
        return ""
    return _bounded_summary("; ".join(_actionable_entries(report)))


def _actionable_entries(report: dict) -> list[str]:
    actionable = [
        check for check in report.get("checks", []) if check.get("status") in {"degraded", "error"}
    ]
    return [
        f"{check.get('id', 'unknown')} ({check['status']}): {check.get('message', '')}"
        for check in actionable
    ]


def _bounded_summary(text: str) -> str:
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[: SUMMARY_LIMIT - 3].rstrip() + "..."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check local LLM-Wiki health.")
    parser.add_argument("--repair", action="store_true", help="Apply safe idempotent repairs.")
    parser.add_argument(
        "--rebuild-generation",
        action="store_true",
        help="Explicitly rebuild the immutable evidence generation under the repair fence.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the structured report as JSON.")
    parser.add_argument(
        "--time-budget",
        type=float,
        default=generation_maintenance.DEFAULT_TIME_BUDGET_SECONDS,
        help=(
            "Seconds this run may spend before unfinished checks report "
            f"a budget exhaustion (default {generation_maintenance.DEFAULT_TIME_BUDGET_SECONDS:g})."
        ),
    )
    parser.add_argument("--scope", choices=("full", "operational"), default="full",
                        help="Operational observations or full retained-history diagnosis (default full).")
    args = parser.parse_args(argv)
    if not math.isfinite(args.time_budget) or args.time_budget <= 0:
        parser.error("--time-budget must be a positive number of seconds")
    report = run_doctor(
        repair=args.repair or args.rebuild_generation,
        rebuild_generation=args.rebuild_generation,
        time_budget_seconds=args.time_budget,
        scope=args.scope,
    )
    _print_report(report, args.json)
    return {"ok": 0, "degraded": 1, "error": 2}[report["overall_status"]]


def _print_text_details(report: dict) -> None:
    """The human-readable lines that follow the headline status."""
    summary = degraded_summary(report)
    if summary:
        print(summary)
    if report["repaired"]:
        print(f"Repairs applied: {len(report['repaired'])}")


def _print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        return
    print(f"LLM-Wiki doctor ({report['scope']}): {report['overall_status']}")
    _print_text_details(report)


if __name__ == "__main__":
    raise SystemExit(main())
