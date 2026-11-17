"""Doctor's explicit repairs and the context they run in."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import generation_maintenance  # noqa: E402
from llm_wiki import doctor_deletion, monotonic_deadline, runtime_roots
from llm_wiki.doctor_base import MAX_OPERATIONAL_ROWS, archive_path
from llm_wiki.doctor_integrations import DEFERRED_BY_ACTION, RepairContext, repair_runtime
from llm_wiki.doctor_queue import archive_check
from secret_redact import describe_error, describe_error_chain
from settings import (
    setting_value,
)

VALID_REPAIR_ACTIONS = frozenset(
    {"runtime", "transactions", "queue", "indexes", "archives", "generations"}
)


def validated_repairs(repair_actions: set[str] | frozenset[str] | None) -> set[str]:
    selected = set(VALID_REPAIR_ACTIONS if repair_actions is None else repair_actions)
    unknown = selected - VALID_REPAIR_ACTIONS
    if unknown:
        raise ValueError(f"unknown doctor repair actions: {sorted(unknown)}")
    return selected


def resolved_doctor_paths(
    root: Path | str | None, state_root: Path | str | None, home: Path | str | None
) -> tuple[Path, Path, Path]:
    root_path = Path(root or runtime_roots.vault_root()).resolve()
    state_path = Path(os.path.abspath(state_root or runtime_roots.state_root_for(root_path)))
    home_path = Path(home).resolve() if home is not None else Path.home().resolve()
    return root_path, state_path, home_path


def _defer_all_repairs(context: RepairContext) -> None:
    for action in context.selected_repairs:
        context.repair_deferred.update(DEFERRED_BY_ACTION[action])


def _record_generation_rebuild(result: dict, context: RepairContext) -> None:
    """Record a completed rebuild, or note that the rebuild was deferred."""
    if result["status"] == "built":
        context.repaired.append(
            {
                "action": "rebuild_generation",
                "generation_id": result["generation_id"],
            }
        )
        return
    if result["status"] == "deferred":
        context.repair_deferred.add("generation")


def repair_generations_action(guard: Any, context: RepairContext) -> None:
    guard.run(
        generation_maintenance.repair_generation_catalog,
        context.root_path,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
        repaired=context.repaired,
    )
    if not context.rebuild_generation:
        return
    result = guard.run(
        generation_maintenance.build_or_refresh_generation,
        context.root_path,
        context.state_path,
        deadline=context.deadline,
        cancelled=guard.cancelled,
        max_sources=setting_value("corpus.max_files", context.root_path),
        force_rebuild=True,
        coordinator=guard.coordinator,
    )
    _record_generation_rebuild(result, context)


def _repair_transactions_action(
    guard: Any, coordinator: Any, context: RepairContext
) -> None:
    recovered = guard.run(
        coordinator.recover,
        writer_wait_seconds=0,
        max_transactions=MAX_OPERATIONAL_ROWS,
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    if recovered:
        context.repaired.append(
            {"action": "recover_transactions", "count": len(recovered)}
        )


def _repair_queue_action(guard: Any, context: RepairContext) -> None:
    """Open the queue this vault has, so a queue that cannot open is an error.

    The JSON queue lease repair and the v2 migration went on 2026-09-23.
    """
    from queue_core import (
        active_or_legacy_memory_queue,
    )

    # Not `MemoryQueue(state_path)`. Adoption replaces the pre-adoption
    # `run/queue.sqlite3` with a JSON tombstone, so constructing the legacy queue
    # directly raises `queue_tombstoned_by_adoption` — and because this is the
    # first action in the repair chain, the whole runtime repair aborted on it.
    # Found in an audit 2026-09-05: `doctor --repair` had been reporting
    # "Runtime repair failed" with nothing repaired, on a vault where adoption
    # is in force, and the message named the fix.
    guard.run(active_or_legacy_memory_queue, context.root_path, context.state_path)


def _repair_archives_action(guard: Any, context: RepairContext) -> None:
    archive_before = archive_check(
        context.root_path, context.state_path, context.deadline
    )
    archive_root = archive_path(context.root_path)
    if generation_maintenance.safe_kind(archive_root, context.root_path)[0] != "directory":
        return
    if archive_before["status"] == "ok":
        return
    record_recovered_archives(_recovered_archives(guard, context), context)


def _recovered_archives(guard: Any, context: RepairContext) -> list:
    from archive_daily import DailyArchiver

    return guard.run(
        lambda: DailyArchiver(context.root_path, context.state_path).recover(
            deadline=context.deadline,
            cancelled=guard.cancelled,
        )
    )


def record_recovered_archives(recovered: list, context: RepairContext) -> None:
    """A repair that recovered nothing is not recorded as work that was done."""
    if not recovered:
        return
    context.repaired.append({"action": "recover_archives", "count": len(recovered)})


def _claim_sources(root_path: Path) -> list[Path]:
    sources = [root_path / "knowledge" / "notes"]
    projects = root_path / "knowledge" / "projects"
    if generation_maintenance.safe_kind(projects, root_path)[0] == "directory":
        sources.append(projects)
    return sources


def _repair_claims_action(guard: Any, context: RepairContext) -> None:
    claim_before = doctor_deletion.claim_check(
        context.root_path, context.state_path, context.deadline
    )
    if claim_before["status"] == "ok":
        return
    from claims import ClaimIndex

    claim_index = ClaimIndex(context.state_path, vault=context.root_path)
    guard.run(
        claim_index.rebuild,
        _claim_sources(context.root_path),
        deadline=context.deadline,
        cancelled=guard.cancelled,
    )
    context.repaired.append({"action": "rebuild_claim_index"})


def run_selected_repairs(
    selected: set[str], ordered: tuple, context: RepairContext
) -> None:
    """Run each named repair the caller selected, in the order given.

    The repairs are independent, so one that raises is recorded under its own
    name and the rest still run: a `ValueError` from the generation catalog used
    to skip the transactions, the queue, the index, the archives and the claims,
    and the report called all of that "Runtime repair failed". A lost fence and a
    reached deadline are the exception and end the pass — no repair may work
    without the fence. See
    `docs/research/2026-09-17-one-failed-repair-does-not-cancel-the-others.md`.
    """
    for name, action in ordered:
        if name in selected:
            repair_or_record(name, action, context)


def repair_or_record(name: str, action, context: RepairContext) -> None:
    try:
        action()
    except (generation_maintenance.MaintenanceFenceLost, TimeoutError):
        raise
    except Exception as exc:  # noqa: BLE001 - one repair's fault is its own
        context.repair_errors.setdefault(name, []).append(
            f"{name} repair failed: {describe_error(exc)}"
        )
        context.repair_deferred.update(DEFERRED_BY_ACTION[name])


def _repair_state_actions(
    guard: Any, coordinator: Any, context: RepairContext
) -> None:
    ordered = (
        ("runtime", lambda: guard.run(
            repair_runtime, context.state_path, context.repaired
        )),
        ("generations", lambda: repair_generations_action(guard, context)),
        ("transactions", lambda: _repair_transactions_action(
            guard, coordinator, context
        )),
    )
    run_selected_repairs(context.selected_repairs, ordered, context)
    if "queue" not in context.selected_repairs:
        return
    repair_or_record("queue", lambda: _repair_queue_action(guard, context), context)


def _repair_derived_actions(guard: Any, context: RepairContext) -> None:
    # No worker runs here: within a repair's budget it could only claim a task and
    # kill it, costing an attempt. See
    # `docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
    ordered = (
        ("archives", lambda: _repair_archives_action(guard, context)),
        ("indexes", lambda: _repair_claims_action(guard, context)),
    )
    run_selected_repairs(context.selected_repairs, ordered, context)


def _release_unentered_maintenance(
    maintenance: tuple | None, guard_entered: bool, context: RepairContext
) -> None:
    if maintenance is None or guard_entered:
        return
    try:
        generation_maintenance.release_maintenance_owner(*maintenance)
    except Exception as exc:  # noqa: BLE001
        context.repair_errors.setdefault("runtime", []).append(
            f"Maintenance owner release failed: {describe_error(exc)}"
        )


def stray_candidates(state_root: Path) -> list[str]:
    """The pre-adoption candidate paths that still exist, by their plain names."""
    from llm_wiki.adoption_admission import (
        STRAY_CANDIDATE_NAMES,
    )

    return [
        f"run/{name}"
        for name in STRAY_CANDIDATE_NAMES
        if generation_maintenance.safe_kind(state_root / "run" / name, state_root)[0] != "missing"
    ]


def quarantined_candidates(state_root: Path) -> int:
    """How many stray candidates have been moved aside so far."""
    from installed_memory_repair import (
        QUARANTINE_DIRECTORIES,
    )
    from llm_wiki.adoption_admission import (
        STRAY_CANDIDATE_NAMES,
    )

    run = state_root / "run"
    return sum(_candidates_in(run / name, STRAY_CANDIDATE_NAMES) for name in QUARANTINE_DIRECTORIES)


def _candidates_in(directory: Path, names: tuple[str, ...]) -> int:
    if not directory.is_dir():
        return 0
    return len([entry for entry in directory.iterdir() if entry.name.endswith(names)])


def _adoption_refusal_message(code: str, cause: str, strays: list[str]) -> str:
    message = f"Complete adoption certification failed: {cause}."
    if strays:
        message += " Stray candidate: " + ", ".join(strays) + "."
    return message + " Repair: `uv run python scripts/doctor.py --repair`."


def adoption_check(root: Path, state_root: Path, *, deadline: float | None = None) -> dict:
    """Certify the adopted pair and name the cause of any diagnostic failure.

    Writers verify the adoption evidence and both database contracts before
    admission. Doctor additionally certifies the complete retained database
    history through `require_adopted_through_contention`; a normal writer open
    does not substitute for this deeper diagnostic.
    """
    from llm_wiki.adoption_admission import (
        ReliabilityV3ValidationError,
    )
    from markdown_transaction import (
        reliability_v3_records_present,
        require_adopted_through_contention,
    )

    if not reliability_v3_records_present(state_root):
        message = "Reliability V3 is not adopted here; writers use the legacy path."
        return generation_maintenance.result("adoption", "ok", message, {"adopted": False})
    strays = stray_candidates(state_root)
    details: dict[str, Any] = {
        "adopted": True,
        "stray_candidates": strays,
        "quarantined_candidates": quarantined_candidates(state_root),
    }
    try:
        require_adopted_through_contention(Path(root), state_root, deadline=deadline)
    except ReliabilityV3ValidationError as exc:
        details.update(code=exc.code, cause=describe_error_chain(exc))
        message = _adoption_refusal_message(exc.code, details["cause"], strays)
        return generation_maintenance.result("adoption", "error", message, details)
    except TimeoutError as exc:
        return _adoption_timeout_result(exc, deadline, details)
    return generation_maintenance.result("adoption", "ok", "The adoption record admits writers.", details)


def _adoption_timeout_result(error: TimeoutError, deadline: float | None, details: dict) -> dict:
    if deadline is None or not monotonic_deadline.deadline_reached(deadline):
        raise error
    result = unfinished_result("adoption")
    result["details"].update(details)
    return result


def _retire_stray_candidate(context: RepairContext) -> None:
    """Move every provably stray pre-adoption candidate out of the way.

    It runs before the maintenance owner is taken because while a stray exists
    no owner can be taken at all. Writers apply the same rule where they are
    refused (`markdown_transaction.retire_strays_before_validation`).
    """
    from llm_wiki.adoption_admission import (
        retire_stray_candidates,
    )

    outcome = retire_stray_candidates(context.state_path, context.generated_at)
    for path in outcome.retired:
        context.repaired.append({"action": "retire_stray_candidate", "path": path})
    for reason in outcome.kept:
        context.repair_errors.setdefault("runtime", []).append(f"Stray candidate kept: {reason}")


def run_repairs(context: RepairContext) -> None:
    """Run every selected repair under one maintenance owner."""
    maintenance: tuple[Any, dict[str, object]] | None = None
    guard_entered = False
    try:
        if "runtime" in context.selected_repairs:
            _retire_stray_candidate(context)
        maintenance = generation_maintenance.acquire_maintenance_owner(
            context.root_path, context.state_path, context.generated_at
        )
        if maintenance is None:
            _defer_all_repairs(context)
            return
        coordinator, lease = maintenance
        with generation_maintenance.MaintenanceHeartbeat(
            coordinator, lease, deadline=context.deadline
        ) as guard:
            guard_entered = True
            _repair_state_actions(guard, coordinator, context)
            _repair_derived_actions(guard, context)
    except Exception as exc:  # noqa: BLE001
        context.repair_errors.setdefault("runtime", []).append(
            f"Repair failed: {describe_error(exc)}"
        )
    finally:
        _release_unentered_maintenance(maintenance, guard_entered, context)


# The nightly snapshot of `knowledge/` is the memory's second copy
# (`snapshot_knowledge.py`); a nightly period plus a day of grace. See
# `docs/research/2026-09-24-the-documents-say-what-the-code-does.md`.
BACKUP_FRESH_SECONDS = 2 * 24 * 3600


# `git log -1` in the snapshot repository, a local read that answers in milliseconds;
# past 5 s the check has no snapshot time to judge. Basis unknown beyond that.
BACKUP_GIT_TIMEOUT_SECONDS = 5


def unfinished_result(check_id: str) -> dict:
    return generation_maintenance.result(
        check_id,
        "degraded",
        "Check not completed because the doctor time budget was exhausted.",
        {"budget_exhausted": True},
    )
