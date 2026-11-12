"""The doctor tool: health status, queue and transaction operations."""
from __future__ import annotations

from pathlib import Path

from llm_wiki import mcp_server_workers, runtime_roots
from llm_wiki.mcp_server_limits import QUEUE_READ_BUSY_MS
from llm_wiki.mcp_server_workers import (
    bounded_operation_deadline,
)


def _operator_result(
    action: str,
    *,
    ids: list[str] | None = None,
    states: list[str] | None = None,
    codes: list[str] | None = None,
    counts: dict[str, int] | None = None,
    overall_status: str | None = None,
) -> dict:
    identifiers = list(ids or [])
    state_values = set(states or [])
    return {
        "action": action,
        "overall_status": overall_status or _default_overall_status(state_values),
        "ids": identifiers[:100],
        "counts": _operator_counts(counts, identifiers),
        "states": sorted(state_values),
        "codes": sorted(set(codes or [])),
    }


def _operator_counts(counts: dict | None, identifiers: list) -> dict:
    if counts is not None:
        return counts
    return {"items": len(identifiers)}


def _default_overall_status(state_values: set) -> str:
    if "error" in state_values:
        return "error"
    if "degraded" in state_values:
        return "degraded"
    return "ok"


_DOCTOR_MUTATIONS = {
    "queue-cancel",
    "queue-redrive",
    "transaction-recover",
    "transaction-undo",
}

_DOCTOR_FAILURE_CODES = {
    "KeyError": "unknown_target",
    "TimeoutError": "owner_busy",
    "MigrationBusy": "owner_busy",
}


def _doctor_failure_code(error: BaseException) -> str:
    return _DOCTOR_FAILURE_CODES.get(type(error).__name__, "operation_failed")


def _doctor_report_codes(report: dict) -> list:
    codes = [item["code"] for item in report["run_deletion"]["blockers"]]
    for check in report["checks"]:
        codes.extend(
            str(code) for code in check.get("details", {}).get("codes", [])
        )
    return codes


def _doctor_status(context: dict) -> dict:
    from doctor import run_doctor

    report = run_doctor(
        root=context["root"],
        state_root=context["state_root"],
        deadline=context["deadline"],
        scope="operational",
    )
    result = _operator_result(
        "status",
        ids=[str(check["id"]) for check in report["checks"]][: context["limit"]],
        states=[str(report["overall_status"])],
        codes=_doctor_report_codes(report),
        counts={key: int(value) for key, value in report["counts"].items()},
        overall_status=str(report["overall_status"]),
    )

    result["scope"] = report["scope"]
    result["coverage"] = report["coverage"]
    result["generated_at"] = report["generated_at"]
    return result


def _queue_database_path(context: dict) -> Path:
    """The queue database in force; adoption leaves a tombstone at the legacy path."""
    from llm_wiki.adoption_admission import (
        adopted_database_path,
    )

    return adopted_database_path(
        database_name="queue", state_root=context["state_root"]
    )


def _missing_queue_result(action: str) -> dict:
    if action == "queue-dead-list":
        return _operator_result(action, codes=["queue_missing"])
    return _operator_result(
        action, states=["error"], codes=["queue_missing"], overall_status="error"
    )


def _open_queue_reader(context: dict):
    from reliable_memory import open_readonly_operational_db

    return open_readonly_operational_db(
        _queue_database_path(context),
        context["state_root"],
        max_bytes=256 * 1024 * 1024,
        busy_ms=QUEUE_READ_BUSY_MS,
    )


def _row_error_codes(row) -> list:
    if row["error_code"]:
        return [str(row["error_code"])]
    return []


def _queue_inspect_result(connection, target_id) -> dict:
    row = connection.execute(
        "SELECT id,state,error_code FROM tasks WHERE id=?", (target_id,)
    ).fetchone()
    if row is None:
        return _operator_result(
            "queue-inspect",
            states=["error"],
            codes=["unknown_task"],
            overall_status="error",
        )
    return _operator_result(
        "queue-inspect",
        ids=[str(row["id"])],
        states=[str(row["state"])],
        codes=_row_error_codes(row),
    )


def _queue_dead_list_result(connection, limit: int) -> dict:
    rows = connection.execute(
        "SELECT id,state,error_code FROM tasks WHERE state='dead' "
        "ORDER BY updated_at,id LIMIT ?",
        (limit,),
    ).fetchall()
    return _operator_result(
        "queue-dead-list",
        ids=[str(row["id"]) for row in rows],
        states=[str(row["state"]) for row in rows],
        codes=[str(row["error_code"]) for row in rows if row["error_code"]],
    )


def _doctor_queue_read(context: dict) -> dict:
    action = context["action"]
    if not _queue_database_path(context).is_file():
        return _missing_queue_result(action)
    connection = _open_queue_reader(context)
    try:
        if action == "queue-inspect":
            return _queue_inspect_result(connection, context["target_id"])
        return _queue_dead_list_result(connection, context["limit"])
    finally:
        connection.close()


def _doctor_queue_cancel(context: dict) -> dict:
    from queue_core import (
        active_or_legacy_memory_queue,
    )

    queue = active_or_legacy_memory_queue(
        context["root"], context["state_root"], deadline=context["deadline"]
    )
    changed = queue.cancel(
        str(context["target_id"]),
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    if not changed:
        return _operator_result(
            "queue-cancel",
            states=["error"],
            codes=["unknown_or_terminal_task"],
            overall_status="error",
        )
    return _operator_result(
        "queue-cancel", ids=[str(context["target_id"])], states=["cancelled"]
    )


def _redrive_error_code(error) -> str:
    if str(error) == "redrive_requires_dead":
        return str(error)
    return "redrive_invalid"


def _doctor_queue_redrive(context: dict) -> dict:
    from llm_wiki.queue_contracts import QueueOperationError
    from queue_core import (
        active_or_legacy_memory_queue,
    )

    queue = active_or_legacy_memory_queue(
        context["root"], context["state_root"], deadline=context["deadline"]
    )
    try:
        replacement = queue.redrive(
            str(context["target_id"]),
            deadline=context["deadline"],
            cancelled=context["cancelled"],
        )
    except KeyError:
        return _operator_result(
            "queue-redrive",
            states=["error"],
            codes=["unknown_task"],
            overall_status="error",
        )
    except QueueOperationError as error:
        return _operator_result(
            "queue-redrive",
            states=["error"],
            codes=[_redrive_error_code(error)],
            overall_status="error",
        )
    return _operator_result("queue-redrive", ids=[replacement], states=["ready"])


def _transaction_result(action: str, records) -> dict:
    return _operator_result(
        action,
        ids=[record.id for record in records],
        states=[record.state for record in records],
        codes=[record.error_code for record in records if record.error_code],
    )


def _transaction_coordinator(context: dict):
    from markdown_transaction import active_or_legacy_coordinator

    return active_or_legacy_coordinator(
        context["root"], context["state_root"], deadline=context["deadline"]
    )


def _doctor_transaction_recover(context: dict) -> dict:
    records = _transaction_coordinator(context).recover(
        max_transactions=context["limit"],
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    return _transaction_result("transaction-recover", records)


def _doctor_transaction_undo(context: dict) -> dict:
    coordinator = _transaction_coordinator(context)
    prepared = coordinator.undo(
        str(context["target_id"]),
        deadline=context["deadline"],
        cancelled=context["cancelled"],
    )
    record = coordinator.apply(
        prepared.id, deadline=context["deadline"], cancelled=context["cancelled"]
    )
    return _transaction_result("transaction-undo", [record])


def _check_counts(details: dict) -> dict:
    return {
        key: value
        for key, value in details.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _check_result(action: str, check: dict) -> dict:
    details = check["details"]
    return _operator_result(
        action,
        states=[str(check["status"])],
        codes=[str(code) for code in details.get("codes", [])],
        counts=_check_counts(details),
    )


def _doctor_archive_status(context: dict) -> dict:
    from llm_wiki.doctor_queue import (
        archive_check,
    )

    check = archive_check(context["root"], context["state_root"], context["deadline"])
    return _check_result("archive-status", check)


def _doctor_claim_status(context: dict) -> dict:
    from llm_wiki.doctor_deletion import (
        claim_check,
    )

    check = claim_check(context["root"], context["state_root"], context["deadline"])
    return _check_result("claim-status", check)


_DOCTOR_ACTIONS = {
    "status": _doctor_status,
    "queue-inspect": _doctor_queue_read,
    "queue-dead-list": _doctor_queue_read,
    "queue-cancel": _doctor_queue_cancel,
    "queue-redrive": _doctor_queue_redrive,
    "transaction-recover": _doctor_transaction_recover,
    "transaction-undo": _doctor_transaction_undo,
    "archive-status": _doctor_archive_status,
    "claim-status": _doctor_claim_status,
}


def doctor(
    *,
    action: str,
    target_id: str | None = None,
    limit: int = 20,
    repair: bool = False,
    deadline: float | None = None,
) -> dict:
    """Dispatch bounded health and recovery actions with redacted output."""

    if action in _DOCTOR_MUTATIONS and repair is not True:
        return _operator_result(
            action, states=["error"], codes=["repair_required"], overall_status="error"
        )
    handler = _DOCTOR_ACTIONS.get(action)
    if handler is None:
        return _operator_result(
            action, states=["error"], codes=["unknown_action"], overall_status="error"
        )
    context = {
        "action": action,
        "target_id": target_id,
        "limit": limit,
        "root": runtime_roots.vault_root(),
        "state_root": runtime_roots.state_root(),
        "deadline": bounded_operation_deadline(deadline),
        "cancelled": mcp_server_workers.operation_cancelled(),
    }
    try:
        return handler(context)
    except Exception as error:  # noqa: BLE001 - stable operator failure boundary
        return _operator_result(
            action,
            states=["error"],
            codes=[_doctor_failure_code(error)],
            overall_status="error",
        )
