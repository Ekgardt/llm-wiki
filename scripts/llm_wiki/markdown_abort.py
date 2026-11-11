"""Transaction abort: restore before-images and publish abort receipts."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import NamedTuple

from iso_time import utc_text
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.schema import validate_schema
from llm_wiki.markdown_records import (
    IntentFence,
    RefusedOperation,
    TransactionAbortReceipt,
    TransactionFailure,
)
from llm_wiki.markdown_store import SCHEMA_DIRECTORY, inverse_change_kind, stored_before_state
from llm_wiki.sqlite_transactions import begin_immediate
from reliable_memory import (
    publish_runtime_file,
    read_runtime_bytes,
)

_ABORT_FENCE_QUERY = """SELECT 1 FROM intent_fences AS fence
                       JOIN capture_binding_projections AS binding
                         ON binding.intent_id=fence.intent_id
                        AND binding.intent_fence_token=fence.token
                        AND binding.intent_fence_epoch=fence.fencing_epoch
                       WHERE fence.intent_id=? AND fence.token=?
                         AND fence.fencing_epoch=? AND fence.expires_at>?
                         AND binding.active_link_digest=?"""


def _require_operator_fence(intent_fence: object, active_link_digest: object) -> None:
    if not isinstance(intent_fence, IntentFence) or intent_fence.mode != "operator":
        raise ValueError("abort requires an operator intent fence")
    if re.fullmatch(r"[0-9a-f]{64}", str(active_link_digest)) is None:
        raise ValueError("active link digest must be lowercase 64-hex")


def _require_actor_identity(actor_identity: object) -> None:
    if not isinstance(actor_identity, str):
        raise ValueError("actor identity is invalid")
    if not 1 <= len(actor_identity.encode("utf-8")) <= 512:
        raise ValueError("actor identity is invalid")


def _require_abortable_state(row: sqlite3.Row, transaction_id: str) -> None:
    if row["state"] == "committed":
        raise TransactionFailure(
            "committed transaction cannot be aborted",
            "abort_committed",
            "committed",
        )
    if row["state"] in {"conflicted", "quarantined"}:
        raise TransactionFailure(
            "conflicted transaction cannot be aborted",
            "abort_state_refused",
            str(row["state"]),
        )


def _abort_identity(
    transaction_id: str,
    intent_fence: IntentFence,
    active_link_digest: str,
    manifest_sha256: str,
) -> str:
    identity = {
        "active_link_digest": active_link_digest,
        "before_manifest_sha256": manifest_sha256,
        "intent_fence_epoch": intent_fence.epoch,
        "intent_id": intent_fence.intent_id,
        "transaction_id": transaction_id,
    }
    return f"transaction-abort:{sha256_bytes(canonical_json_bytes(identity))}"


class _AbortDirection(NamedTuple):
    operation_id: str
    manifest_sha256: str
    chosen_at: str


class TransactionAbort:
    def _restore_inflight_operations(
        self, transaction_id: str, changed: Sequence[sqlite3.Row]
    ) -> None:
        for row in reversed(changed):
            if self._operation_hash(row) != row["after_hash"]:
                continue
            before_state = stored_before_state(self.transaction_root, transaction_id, row)
            inverse = dict(row)
            inverse.update(
                kind=inverse_change_kind(row["before_hash"], row["after_hash"]),
                before_hash=row["after_hash"],
                after_hash=row["before_hash"],
            )
            self._apply_operation(inverse, {"after": before_state})
            self._require_operation_state(inverse, row["before_hash"], "restored")

    def _abort_before_manifest(self, transaction_id: str) -> list[dict[str, object]]:
        manifest = [
            {
                "position": int(row["position"]),
                "kind": str(row["kind"]),
                "path": str(row["path"]),
                "before_hash": str(row["before_hash"]),
                "after_hash": str(row["after_hash"]),
            }
            for row in self._operation_rows(transaction_id)
        ]
        if not manifest:
            raise KeyError(transaction_id)
        return manifest

    def _require_live_abort_fence(
        self,
        database: sqlite3.Connection,
        intent_fence: IntentFence,
        active_link_digest: str,
        at: str,
        state: str,
        code: str,
        message: str,
    ) -> None:
        fence = database.execute(
            _ABORT_FENCE_QUERY,
            (
                intent_fence.intent_id,
                intent_fence.token,
                intent_fence.epoch,
                at,
                active_link_digest,
            ),
        ).fetchone()
        if fence is None:
            raise TransactionFailure(message, code, state)

    def _direct_abort(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        transaction_id: str,
        direction: _AbortDirection,
    ) -> _AbortDirection:
        """Move the transaction into aborting, or adopt the direction already there."""
        if row["state"] == "aborting":
            return _AbortDirection(
                str(row["abort_operation_id"]),
                str(row["abort_manifest_sha256"]),
                str(row["abort_chosen_at"]),
            )
        changed = database.execute(
            """UPDATE "transaction" SET state='aborting',error_code=NULL,
                   abort_operation_id=?,abort_manifest_sha256=?,
                   abort_chosen_at=?,updated_at=?
               WHERE id=? AND state IN ('preparing','prepared','applying')""",
            (
                direction.operation_id,
                direction.manifest_sha256,
                direction.chosen_at,
                direction.chosen_at,
                transaction_id,
            ),
        ).rowcount
        if changed != 1:
            raise TransactionFailure(
                "transaction cannot enter aborting",
                "abort_state_refused",
                str(row["state"]),
            )
        return direction

    def _enter_aborting(
        self,
        transaction_id: str,
        intent_fence: IntentFence,
        active_link_digest: str,
        direction: _AbortDirection,
    ) -> _AbortDirection:
        with self._connect() as database, begin_immediate(database):
            row = database.execute(
                'SELECT * FROM "transaction" WHERE id=?', (transaction_id,)
            ).fetchone()
            if row is None:
                raise KeyError(transaction_id)
            _require_abortable_state(row, transaction_id)
            self._require_live_abort_fence(
                database,
                intent_fence,
                active_link_digest,
                direction.chosen_at,
                str(row["state"]),
                "intent_fence_lost",
                "abort intent fence is stale",
            )
            return self._direct_abort(database, row, transaction_id, direction)

    def _restored_before_state(
        self, operation: sqlite3.Row, transaction_id: str
    ) -> object:
        try:
            return stored_before_state(self.transaction_root, transaction_id, operation)
        except (RefusedOperation, RuntimeError) as exc:
            raise TransactionFailure(
                "abort before-image is corrupt",
                "abort_before_image_corrupt",
                "aborting",
            ) from exc

    def _restore_one_target(self, operation: sqlite3.Row, transaction_id: str) -> None:
        current = self._operation_hash(operation)
        if current == operation["before_hash"]:
            return
        if current != operation["after_hash"]:
            raise TransactionFailure(
                "abort target has third-party bytes",
                "abort_target_conflict",
                "aborting",
            )
        before_state = self._restored_before_state(operation, transaction_id)
        inverse = dict(operation)
        inverse.update(
            kind=inverse_change_kind(operation["before_hash"], operation["after_hash"]),
            before_hash=operation["after_hash"],
            after_hash=operation["before_hash"],
        )
        self._apply_operation(inverse, {"after": before_state})
        self._require_operation_state(
            inverse, operation["before_hash"], "restored"
        )
        self._killpoint("after_each_abort_target")

    def _restore_abort_targets(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            for operation in reversed(rows):
                self._require_operation_active(deadline, cancelled)
                self._restore_one_target(operation, transaction_id)
        except TransactionFailure as exc:
            self._set_transaction_state(
                transaction_id, "aborting", error_code=exc.code
            )
            raise

    def _abort_receipt_record(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        intent_fence: IntentFence,
        active_link_digest: str,
        actor_identity: str,
        direction: _AbortDirection,
    ) -> dict[str, object]:
        restored = [
            {"path": str(row["path"]), "sha256": str(row["before_hash"])}
            for row in rows
        ]
        return {
            "schema_version": "transaction-abort/v1",
            "transaction_id": transaction_id,
            "intent_id": intent_fence.intent_id,
            "active_link_digest": active_link_digest,
            "intent_fence_token_sha256": sha256_bytes(
                intent_fence.token.encode("utf-8")
            ),
            "intent_fence_epoch": intent_fence.epoch,
            "abort_operation_id": direction.operation_id,
            "before_manifest_sha256": direction.manifest_sha256,
            "restored_target_count": len(rows),
            "restored_tree_sha256": sha256_bytes(canonical_json_bytes(restored)),
            "actor_identity": actor_identity,
            "aborted_at": direction.chosen_at,
        }

    def _publish_abort_receipt(
        self, transaction_id: str, receipt_record: dict[str, object]
    ) -> tuple[str, str]:
        validate_schema(
            receipt_record,
            SCHEMA_DIRECTORY / "transaction-abort-v1.json",
        )
        receipt_bytes = canonical_json_bytes(receipt_record)
        if len(receipt_bytes) > 64 * 1024:
            raise TransactionFailure(
                "abort receipt exceeds its bound",
                "abort_receipt_oversized",
                "aborting",
            )
        relative_path = f"run/transactions/{transaction_id}/abort-receipt.json"
        receipt_path = self.state_root / relative_path
        self._killpoint("before_abort_receipt")
        publish_runtime_file(
            receipt_path,
            receipt_bytes,
            state_root=self.state_root,
            create_only=True,
        )
        read_back = read_runtime_bytes(
            receipt_path, self.state_root, max_bytes=64 * 1024, owner_only=True
        )
        if read_back != receipt_bytes:
            raise TransactionFailure(
                "abort receipt read-back failed",
                "abort_receipt_conflict",
                "aborting",
            )
        self._killpoint("after_abort_receipt")
        return relative_path, sha256_bytes(read_back)

    def _finalize_aborted(
        self,
        transaction_id: str,
        rows: list[sqlite3.Row],
        intent_fence: IntentFence,
        active_link_digest: str,
        direction: _AbortDirection,
        receipt_sha256: str,
    ) -> None:
        with self._connect() as database, begin_immediate(database):
            self._require_live_abort_fence(
                database,
                intent_fence,
                active_link_digest,
                utc_text(datetime.now(UTC)),
                "aborting",
                "abort_final_verification_failed",
                "abort final verification failed",
            )
            if any(self._operation_hash(row) != row["before_hash"] for row in rows):
                raise TransactionFailure(
                    "abort final verification failed",
                    "abort_final_verification_failed",
                    "aborting",
                )
            self._killpoint("before_aborted")
            changed = database.execute(
                """UPDATE "transaction" SET state='aborted',error_code=NULL,
                       abort_receipt_sha256=?,aborted_at=?,updated_at=?
                   WHERE id=? AND state='aborting' AND abort_operation_id=?
                     AND abort_manifest_sha256=?""",
                (
                    receipt_sha256,
                    direction.chosen_at,
                    direction.chosen_at,
                    transaction_id,
                    direction.operation_id,
                    direction.manifest_sha256,
                ),
            ).rowcount
            if changed != 1:
                raise TransactionFailure(
                    "abort fence was lost", "abort_fence_lost", "aborting"
                )

    def abort_for_discard(
        self,
        transaction_id: str,
        *,
        intent_fence: IntentFence,
        active_link_digest: str,
        actor_identity: str,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> TransactionAbortReceipt:
        self._require_operation_active(deadline, cancelled)
        _require_operator_fence(intent_fence, active_link_digest)
        _require_actor_identity(actor_identity)
        before_manifest = self._abort_before_manifest(transaction_id)
        manifest_sha256 = sha256_bytes(canonical_json_bytes(before_manifest))
        chosen_at = (
            utc_text(datetime.now(UTC))
        )
        direction = _AbortDirection(
            _abort_identity(
                transaction_id, intent_fence, active_link_digest, manifest_sha256
            ),
            manifest_sha256,
            chosen_at,
        )
        with self.writer_gate():
            direction = self._enter_aborting(
                transaction_id, intent_fence, active_link_digest, direction
            )
            self._killpoint("after_aborting")
            rows = self._operation_rows(transaction_id)
            self._restore_abort_targets(transaction_id, rows, deadline, cancelled)
            relative_path, receipt_sha256 = self._publish_abort_receipt(
                transaction_id,
                self._abort_receipt_record(
                    transaction_id,
                    rows,
                    intent_fence,
                    active_link_digest,
                    actor_identity,
                    direction,
                ),
            )
            self._finalize_aborted(
                transaction_id,
                rows,
                intent_fence,
                active_link_digest,
                direction,
                receipt_sha256,
            )
        return TransactionAbortReceipt(
            transaction_id=transaction_id,
            intent_id=intent_fence.intent_id,
            abort_operation_id=direction.operation_id,
            receipt_path=relative_path,
            receipt_sha256=receipt_sha256,
            aborted_at=direction.chosen_at,
        )
