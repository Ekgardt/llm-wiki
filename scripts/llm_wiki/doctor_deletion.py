"""Doctor's claims check and its verdict on deleting the runtime directory."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import generation_maintenance  # noqa: E402
from llm_wiki import doctor_base, monotonic_deadline
from llm_wiki.doctor_lsp import validated_observation_codes
from llm_wiki.doctor_queue import claim_status, count_claims

# Issue #29.5: "requires operator attention" named neither the cause nor the
# repair. One cause per code, in the words the operator needs.
CLAIM_CODE_CAUSES = {
    "evidence_unresolved": (
        "cite daily bytes that no longer resolve under the recorded digest "
        "(the daily was appended or rewritten after the page was compiled)"
    ),
    "evidence_ambiguous": "cite bytes that match more than one slice of the daily",
    "evidence_literal_mismatch": "quote text that differs from the bytes at the cited place",
    "claim_scan_truncated": "were not scanned: the bounded scan stopped early",
}
CLAIM_REPAIR = (
    "Repair: `uv run python scripts/doctor.py --repair` rebuilds the claim index "
    "against the current dailies; a claim that still does not resolve stays "
    "listed by page in details, and that page's evidence line must be re-bound "
    "by hand or by recompiling its daily with `compile_memory.py --file`."
)


def _claim_cause(code: str, count: int) -> str:
    cause = CLAIM_CODE_CAUSES.get(code, f"are flagged `{code}`")
    return f"{count} claim(s) {cause}"


def _claim_message(status: str, details: dict) -> str:
    if status == "ok":
        return "Claim index is healthy."
    if details["index"] == "invalid":
        return "Claim index is unreadable; " + CLAIM_REPAIR
    causes = "; ".join(
        _claim_cause(code, count) for code, count in sorted(details["by_code"].items())
    )
    pages = ", ".join(details["pages"])
    return f"Claim index: {causes}. Pages: {pages}. {CLAIM_REPAIR}"


def claim_result(details: dict) -> dict:
    status = claim_status(details)
    return generation_maintenance.result("claims", status, _claim_message(status, details), details)


def claim_check(root: Path, state_root: Path, deadline: float = float("inf")) -> dict:
    path = state_root / "cache" / "claims.sqlite3"
    details = {
        "index": "missing",
        "claims": 0,
        "diagnostics": 0,
        "codes": [],
        "by_code": {},
        "pages": [],
        "read_error": False,
        "deletion_codes": [],
    }
    kind = generation_maintenance.safe_kind(path, state_root)[0]
    if kind == "missing":
        # Nothing has recorded a claim yet, which is what a new vault looks
        # like. The generation check says the same about a generation that has
        # not been built; calling this degraded left every fresh install
        # permanently unhealthy for having done nothing wrong.
        return generation_maintenance.result(
            "claims", "ok", "No claim has been recorded yet.", details
        )
    if kind != "regular":
        details.update(index="invalid", read_error=True)
        return generation_maintenance.result("claims", "error", "Claim index is unsafe.", details)
    _read_claim_index(path, state_root, deadline, details)
    return claim_result(details)


def _record_claim_schema(index_class, database, details: dict) -> None:
    compatible = index_class._schema_compatible(database)  # noqa: SLF001
    details["index"] = "valid" if compatible else "invalid"
    if compatible:
        count_claims(database, details)


def _read_claim_index(
    path: Path, state_root: Path, deadline: float, details: dict
) -> None:
    try:
        _read_claim_schema(path, state_root, deadline, details)
    except (OSError, PermissionError, sqlite3.Error, TimeoutError, ValueError):
        details["index"] = "invalid"
        details["read_error"] = True


def _read_claim_schema(
    path: Path, state_root: Path, deadline: float, details: dict
) -> None:
    from claims import ClaimIndex

    with doctor_base.readonly_database(path, state_root, deadline=deadline) as database:
        if monotonic_deadline.deadline_reached(deadline):
            raise TimeoutError("claim check deadline")
        _record_claim_schema(ClaimIndex, database, details)


def deletion_snapshot(codes: list[str]) -> dict[str, object]:
    blockers = [{"code": code} for code in sorted(set(codes))]
    return {
        "schema_version": "run-deletion-snapshot/v1",
        "quiescent": not blockers,
        "permit": False,
        "offline_action_required": True,
        "blockers": blockers,
    }


def _deletion_root(root: Path | None, state_path: Path) -> Path:
    if root is None:
        return state_path
    return Path(root)


def run_deletion_check(
    state_root: Path,
    now: datetime,
    *,
    root: Path | None = None,
    deadline: float = float("inf"),
    collected: dict[str, dict] | None = None,
) -> dict:
    """Return an immediate, non-permitting observation of adopted runtime state."""
    if monotonic_deadline.deadline_reached(deadline):
        return deletion_snapshot(["run_deletion_state_unknown"])
    del collected
    from llm_wiki.adoption_admission import (
        ReliabilityV3ValidationError,
        require_reliability_v3_adopted,
    )
    from operational_ownership import OperationalOwnershipError, OwnershipRegistry
    from reliability_v3_validation import (
        validate_reliability_v3_runtime,
    )

    state_path = Path(state_root)
    root_path = _deletion_root(root, state_path)
    try:
        require_reliability_v3_adopted(root=root_path, state_root=state_path, deadline=deadline)
    except (ReliabilityV3ValidationError, TimeoutError) as exc:
        return deletion_snapshot([getattr(exc, "code", "run_deletion_state_unknown")])

    snapshot_deadline = deadline
    registry = OwnershipRegistry.from_adopted_database(  # noqa: SLF001
        state_path,
        state_path / "run" / "markdown-transactions-v3.sqlite3",
    )
    try:
        owner = registry.acquire("doctor", scope="global")
    except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError) as exc:
        code = getattr(exc, "code", "runtime_deletion_check_unavailable")
        code = {"owner_busy": "runtime_deletion_check_requires_quiescence"}.get(code, code)
        return deletion_snapshot([str(code)])

    diagnostics: dict[str, str] = {}
    codes = _deletion_codes_with_owner(
        registry, owner, root_path, state_path, now, snapshot_deadline,
        validate_reliability_v3_runtime, diagnostics=diagnostics,
    )
    return {**deletion_snapshot(codes), **diagnostics}


def _release_deletion_owner(registry, owner, codes: list[str]) -> None:
    from operational_ownership import OperationalOwnershipError

    try:
        registry.release(owner)
    except (OperationalOwnershipError, OSError, sqlite3.Error, ValueError):
        codes.append("runtime_deletion_check_release_failed")


def _deletion_codes_with_owner(
    registry,
    owner,
    root_path: Path,
    state_path: Path,
    now: datetime,
    snapshot_deadline: float,
    validate,
    *,
    diagnostics: dict[str, str] | None = None,
) -> list[str]:
    from operational_ownership import OperationalOwnershipError, heartbeat_owner
    from secret_redact import describe_error

    codes: list[str] = []
    try:
        with heartbeat_owner(owner, registry=registry):
            codes = validated_observation_codes(
                registry, root_path, state_path, now, snapshot_deadline, owner, validate
            )
    except (OperationalOwnershipError, OSError, sqlite3.Error, TimeoutError, ValueError) as error:
        codes = ["run_deletion_state_unknown"]
        if diagnostics is not None:
            diagnostics["observation_error"] = describe_error(error)
    finally:
        _release_deletion_owner(registry, owner, codes)
    return codes
