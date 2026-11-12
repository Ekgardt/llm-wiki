"""Precondition checks for claims, captures, leases and targets."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from bounded_io import read_stable_bytes
from claim_tree_manifest import (
    snapshot_claim_tree,
    snapshot_guardrail_sources,
)
from iso_time import utc_text
from llm_wiki.claim_ledger import MAX_CLAIM_PAGE_BYTES, parse_claim_ledger
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.markdown_records import (
    TransactionFailure,
)
from llm_wiki.markdown_store import ABSENT, parse_stored_timestamp

_PRECONDITION_METHODS = {
    "claim_targets": "_check_claim_targets_precondition",
    "claim_tree_manifest": "_check_claim_tree_precondition",
    "guardrails_source_manifest": "_check_guardrails_precondition",
    "project_lease": "_check_lease_precondition",
    "intent_fence": "_check_capture_precondition",
    "capture_binding": "_check_capture_precondition",
}


def _reconciled_target(
    operation_state: tuple[str, str] | None, expected: object, current: str
) -> bool:
    """The target may already hold the after state this transaction will write."""
    if operation_state is None:
        return False
    return expected == operation_state[0] and current == operation_state[1]


def _guardrail_sources_or_fail(vault: Path) -> object:
    try:
        return snapshot_guardrail_sources(vault)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TransactionFailure(
            "persisted guardrails source manifest precondition failed",
            "precondition_failed",
            "quarantined",
        ) from exc


def _sole_claim_record(
    ledger: Mapping[str, object] | None, claim_id: object
) -> Mapping[str, object]:
    records = (
        []
        if ledger is None
        else [
            record for record in ledger["claims"] if str(record["id"]) == claim_id
        ]
    )
    if len(records) != 1:
        raise ValueError("claim target is missing or ambiguous")
    return records[0]


def _claim_record_matches(
    record: Mapping[str, object], item: Mapping[str, object]
) -> bool:
    if record["fingerprint"] != item["fingerprint"]:
        return False
    if sha256_bytes(canonical_json_bytes(record)) != item["record_hash"]:
        return False
    return record["evidence"]["sha256"] == item["evidence_hash"]


def _manifest_entries(manifest: Mapping[str, object]) -> dict[str, str]:
    return {str(item["path"]): str(item["sha256"]) for item in manifest["entries"]}


def _entry_matches(
    before: str, now: str, operation: tuple[str, str] | None
) -> bool:
    if operation is None:
        return now == before
    return before == operation[0] and now in operation


def _lease_row_matches(row: object, expected: Mapping[str, object]) -> bool:
    if row is None:
        return False
    if (
        row["lease_token"] != expected["lease_token"]
        or row["fencing_epoch"] != expected["fencing_epoch"]
    ):
        return False
    now = datetime.now(UTC)
    return (
        parse_stored_timestamp(row["expires_at"]) > now
        and parse_stored_timestamp(str(expected["expires_at"])) > now
    )


class PreconditionChecks:
    def _check_preconditions(
        self,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        *,
        database: sqlite3.Connection | None = None,
    ) -> None:
        for path, expected in preconditions.items():
            self._check_one_precondition(
                path, expected, preconditions, operation_states, database
            )

    def _check_one_precondition(
        self,
        path: str,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        """A named precondition has its own checker; anything else is a target."""
        checker = getattr(self, _PRECONDITION_METHODS.get(path, ""), None)
        if checker is not None:
            checker(expected, preconditions, operation_states, database)
            return
        self._check_target_precondition(path, expected, operation_states)

    def _check_claim_targets_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Sequence)
        self._check_claim_targets(expected, operation_states)

    def _check_claim_tree_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if not self._claim_tree_matches(
            expected, snapshot_claim_tree(self.vault), operation_states
        ):
            raise TransactionFailure(
                "persisted claim tree manifest precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_guardrails_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if expected != _guardrail_sources_or_fail(self.vault):
            raise TransactionFailure(
                "persisted guardrails source manifest precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_lease_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        assert isinstance(expected, Mapping)
        if database is not None:
            self._check_project_lease(database, expected)
            return
        with self._connect() as lease_database:
            self._check_project_lease(lease_database, expected)

    def _check_capture_precondition(
        self,
        expected: object,
        preconditions: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
        database: sqlite3.Connection | None,
    ) -> None:
        if database is not None:
            self._check_capture_preconditions(database, preconditions)
            return
        with self._connect() as precondition_database:
            self._check_capture_preconditions(precondition_database, preconditions)

    def _check_target_precondition(
        self,
        path: str,
        expected: object,
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        current = self._current_hash(path)
        if current == expected:
            return
        if _reconciled_target(operation_states.get(path), expected, current):
            return
        raise TransactionFailure(
            f"persisted precondition failed for {path}",
            "precondition_failed",
            "quarantined",
        )

    @staticmethod
    def _check_capture_preconditions(
        database: sqlite3.Connection, preconditions: Mapping[str, object]
    ) -> None:
        fence = preconditions.get("intent_fence")
        binding = preconditions.get("capture_binding")
        if not isinstance(fence, Mapping) or not isinstance(binding, Mapping):
            raise TransactionFailure(
                "capture preconditions are incomplete",
                "precondition_failed",
                "quarantined",
            )
        now = utc_text(datetime.now(UTC))
        row = database.execute(
            """SELECT 1 FROM intent_fences AS fence
               JOIN capture_binding_projections AS binding
                 ON binding.intent_id=fence.intent_id
                AND binding.intent_fence_token=fence.token
                AND binding.intent_fence_epoch=fence.fencing_epoch
               WHERE fence.intent_id=? AND fence.mode=? AND fence.token=?
                 AND fence.fencing_epoch=? AND fence.expires_at>?
                 AND binding.task_id=? AND binding.active_link_digest=?
                 AND binding.seal_digest=?""",
            (
                fence["intent_id"],
                fence["mode"],
                fence["token"],
                fence["fencing_epoch"],
                now,
                binding["task_id"],
                binding["active_link_digest"],
                binding["seal_digest"],
            ),
        ).fetchone()
        if row is None:
            raise TransactionFailure(
                "persisted capture precondition failed",
                "precondition_failed",
                "quarantined",
            )

    def _check_claim_targets(
        self,
        expected: Sequence[object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        for item in expected:
            assert isinstance(item, Mapping)
            self._check_one_claim_target(item, operation_states)

    def _check_one_claim_target(
        self,
        item: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> None:
        path = str(item["page"])
        operation = operation_states.get(path)
        if operation is not None and self._current_hash(path) == operation[1]:
            return
        if not self._claim_target_matches(path, item):
            raise TransactionFailure(
                f"persisted claim target precondition failed for {path}#{item['claim_id']}",
                "precondition_failed",
                "quarantined",
            )

    def _claim_target_matches(self, path: str, item: Mapping[str, object]) -> bool:
        """Anything that cannot be read or parsed is a failed precondition."""
        try:
            content = read_stable_bytes(
                self._target(path),
                MAX_CLAIM_PAGE_BYTES,
                label="claim target precondition page",
            )
            record = _sole_claim_record(parse_claim_ledger(content), item["claim_id"])
            return _claim_record_matches(record, item)
        except (OSError, TypeError, ValueError):
            return False

    @staticmethod
    def _claim_tree_matches(
        expected: Mapping[str, object],
        current: Mapping[str, object],
        operation_states: Mapping[str, tuple[str, str]],
    ) -> bool:
        expected_entries = _manifest_entries(expected)
        current_entries = _manifest_entries(current)
        return all(
            _entry_matches(
                expected_entries.get(path, ABSENT),
                current_entries.get(path, ABSENT),
                operation_states.get(path),
            )
            for path in set(expected_entries) | set(current_entries)
        )

    @staticmethod
    def _check_project_lease(
        database: sqlite3.Connection, expected: Mapping[str, object]
    ) -> None:
        row = database.execute(
            "SELECT * FROM project_leases WHERE project = ?",
            (expected["project"],),
        ).fetchone()
        if not _lease_row_matches(row, expected):
            raise TransactionFailure(
                "persisted project lease precondition failed",
                "precondition_failed",
                "quarantined",
            )
