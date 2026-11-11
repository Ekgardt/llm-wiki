"""Recoverable, hash-checked transactions for authoritative Markdown files."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass as dataclass

import process_liveness
from bounded_io import VerifiedByteRange as VerifiedByteRange
from llm_wiki import runtime_roots
from llm_wiki.coordinator_invariants import (
    coordinator_migration_error as coordinator_migration_error,
)
from llm_wiki.coordinator_invariants import (
    coordinator_v3_base_cross_table_invariant as coordinator_v3_base_cross_table_invariant,
)
from llm_wiki.coordinator_invariants import (
    coordinator_v3_blackboard_invariant as coordinator_v3_blackboard_invariant,
)
from llm_wiki.coordinator_invariants import (
    coordinator_v3_cross_table_invariant as coordinator_v3_cross_table_invariant,
)
from llm_wiki.coordinator_invariants import coordinator_v3_row_counts as coordinator_v3_row_counts
from llm_wiki.coordinator_invariants import require_v3_integrity as require_v3_integrity
from llm_wiki.coordinator_invariants import require_v3_invariants as require_v3_invariants
from llm_wiki.coordinator_invariants import v3_operation_violations as v3_operation_violations
from llm_wiki.coordinator_schema import COORDINATOR_V3_CONTRACT as COORDINATOR_V3_CONTRACT
from llm_wiki.coordinator_schema import COORDINATOR_V3_INDEX_SQL as COORDINATOR_V3_INDEX_SQL
from llm_wiki.coordinator_schema import COORDINATOR_V3_INDEXES as COORDINATOR_V3_INDEXES
from llm_wiki.coordinator_schema import COORDINATOR_V3_SCHEMA_SHA256 as COORDINATOR_V3_SCHEMA_SHA256
from llm_wiki.coordinator_schema import COORDINATOR_V3_TABLE_SQL as COORDINATOR_V3_TABLE_SQL
from llm_wiki.coordinator_schema import COORDINATOR_V3_TABLES as COORDINATOR_V3_TABLES
from llm_wiki.coordinator_schema import (
    coordinator_v3_object_matches as coordinator_v3_object_matches,
)
from llm_wiki.coordinator_schema import (
    coordinator_v3_schema_complete as coordinator_v3_schema_complete,
)
from llm_wiki.coordinator_schema import coordinator_v3_statements as coordinator_v3_statements
from llm_wiki.coordinator_schema import coordinator_v3_tables_match as coordinator_v3_tables_match
from llm_wiki.coordinator_schema import (
    ensure_coordinator_v3_indexes as ensure_coordinator_v3_indexes,
)
from llm_wiki.coordinator_schema import known_coordinator_index as known_coordinator_index
from llm_wiki.coordinator_schema import normalized_coordinator_sql as normalized_coordinator_sql
from llm_wiki.coordinator_schema import only_known_indexes as only_known_indexes
from llm_wiki.coordinator_schema import schema_objects as schema_objects
from llm_wiki.coordinator_schema import tables_of as tables_of
from llm_wiki.coordinator_validation import (
    reliability_v3_records_present as reliability_v3_records_present,
)
from llm_wiki.coordinator_validation import (
    require_coordinator_v3_openable as require_coordinator_v3_openable,
)
from llm_wiki.coordinator_validation import require_inside_state_root as require_inside_state_root
from llm_wiki.coordinator_validation import (
    validate_coordinator_v3_database as validate_coordinator_v3_database,
)
from llm_wiki.formats.canonical import canonical_json_bytes
from llm_wiki.formats.canonical import sha256_bytes as sha256_bytes
from llm_wiki.markdown_abort import TransactionAbort as TransactionAbort
from llm_wiki.markdown_admission import ADOPTION_VALIDATION_LOCK as ADOPTION_VALIDATION_LOCK
from llm_wiki.markdown_admission import active_markdown_coordinator as active_markdown_coordinator
from llm_wiki.markdown_admission import active_or_legacy_coordinator as active_or_legacy_coordinator
from llm_wiki.markdown_admission import decoded_image_bytes as decoded_image_bytes
from llm_wiki.markdown_admission import default_coordinator as default_coordinator
from llm_wiki.markdown_admission import require_adopted_once as require_adopted_once
from llm_wiki.markdown_admission import (
    require_adopted_through_contention as require_adopted_through_contention,
)
from llm_wiki.markdown_admission import (
    retire_strays_before_validation as retire_strays_before_validation,
)
from llm_wiki.markdown_admission import retry_adoption_validation as retry_adoption_validation
from llm_wiki.markdown_admission import stable_operation_id as stable_operation_id
from llm_wiki.markdown_admission import (
    transient_adoption_contention as transient_adoption_contention,
)
from llm_wiki.markdown_append import append_candidate_id as append_candidate_id
from llm_wiki.markdown_append import append_captured_knowledge as append_captured_knowledge
from llm_wiki.markdown_append import append_knowledge as append_knowledge
from llm_wiki.markdown_append import append_owned_knowledge as append_owned_knowledge
from llm_wiki.markdown_append import append_preconditions as append_preconditions
from llm_wiki.markdown_append import append_until_committed as append_until_committed
from llm_wiki.markdown_append import (
    committed_append_content_matches as committed_append_content_matches,
)
from llm_wiki.markdown_append import refused_parent as refused_parent
from llm_wiki.markdown_append import run_append_candidate as run_append_candidate
from llm_wiki.markdown_apply import TransactionApplication as TransactionApplication
from llm_wiki.markdown_checkpoints import ProjectCheckpointOperations as ProjectCheckpointOperations
from llm_wiki.markdown_coordinator import MarkdownCoordinator as MarkdownCoordinator
from llm_wiki.markdown_coordinator_base import (
    MarkdownCoordinatorFoundation as MarkdownCoordinatorFoundation,
)
from llm_wiki.markdown_migration import coordinator_table_columns as coordinator_table_columns
from llm_wiki.markdown_migration import drop_objects_of_kind as drop_objects_of_kind
from llm_wiki.markdown_migration import expiry_is_live as expiry_is_live
from llm_wiki.markdown_migration import (
    initialize_coordinator_v3_candidate as initialize_coordinator_v3_candidate,
)
from llm_wiki.markdown_migration import require_rebuildable_schema as require_rebuildable_schema
from llm_wiki.markdown_migration import table_has_rows as table_has_rows
from llm_wiki.markdown_mutation import mutate_knowledge as mutate_knowledge
from llm_wiki.markdown_mutation import (
    mutate_knowledge_in_transaction as mutate_knowledge_in_transaction,
)
from llm_wiki.markdown_mutation import mutate_owned_knowledge as mutate_owned_knowledge
from llm_wiki.markdown_mutation import settle_operation as settle_operation
from llm_wiki.markdown_operations import OperationExecution as OperationExecution
from llm_wiki.markdown_preconditions import PreconditionChecks as PreconditionChecks
from llm_wiki.markdown_prepare import TransactionPreparation as TransactionPreparation
from llm_wiki.markdown_records import ChangeKind as ChangeKind
from llm_wiki.markdown_records import CommittedWriteAuthority as CommittedWriteAuthority
from llm_wiki.markdown_records import IntentFence as IntentFence
from llm_wiki.markdown_records import MarkdownChange as MarkdownChange
from llm_wiki.markdown_records import MarkdownOperation as MarkdownOperation
from llm_wiki.markdown_records import OperationBoundElsewhereError as OperationBoundElsewhereError
from llm_wiki.markdown_records import PreconditionChangedError as PreconditionChangedError
from llm_wiki.markdown_records import ProjectCheckpointReservation as ProjectCheckpointReservation
from llm_wiki.markdown_records import ProjectPendingPriorError as ProjectPendingPriorError
from llm_wiki.markdown_records import RefusedArgument as RefusedArgument
from llm_wiki.markdown_records import RefusedOperation as RefusedOperation
from llm_wiki.markdown_records import TargetBoundaryFailure as TargetBoundaryFailure
from llm_wiki.markdown_records import TargetPathBoundaryError as TargetPathBoundaryError
from llm_wiki.markdown_records import TargetStateMismatch as TargetStateMismatch
from llm_wiki.markdown_records import TargetTooLargeError as TargetTooLargeError
from llm_wiki.markdown_records import TransactionAbortReceipt as TransactionAbortReceipt
from llm_wiki.markdown_records import TransactionDriftError as TransactionDriftError
from llm_wiki.markdown_records import TransactionFailure as TransactionFailure
from llm_wiki.markdown_records import TransactionImageError as TransactionImageError
from llm_wiki.markdown_records import TransactionRecord as TransactionRecord
from llm_wiki.markdown_records import TransactionStateError as TransactionStateError
from llm_wiki.markdown_records import WriterAdmissionTimeout as WriterAdmissionTimeout
from llm_wiki.markdown_records import require_bytes as require_bytes
from llm_wiki.markdown_records_access import (
    TransactionRecordOperations as TransactionRecordOperations,
)
from llm_wiki.markdown_recovery import TransactionRecovery as TransactionRecovery
from llm_wiki.markdown_store import ABSENT as ABSENT
from llm_wiki.markdown_store import LZMA_MAGIC as LZMA_MAGIC
from llm_wiki.markdown_store import UNDO_RETENTION_DAYS as UNDO_RETENTION_DAYS
from llm_wiki.markdown_store import WRITER_RETRY_CAP_SECONDS as WRITER_RETRY_CAP_SECONDS
from llm_wiki.markdown_store import Validator as Validator
from llm_wiki.markdown_store import harden_owner_only as harden_owner_only
from llm_wiki.markdown_store import image_bytes as image_bytes
from llm_wiki.markdown_store import is_transient_writer_contention as is_transient_writer_contention
from llm_wiki.markdown_store import now as now
from llm_wiki.markdown_store import parse_stored_timestamp as parse_stored_timestamp
from llm_wiki.markdown_store import preparer_alive as preparer_alive
from llm_wiki.markdown_store import retry_writer_or_raise as retry_writer_or_raise
from llm_wiki.markdown_store import writer_retry_delay as writer_retry_delay
from llm_wiki.markdown_targets import TargetCaptureOperations as TargetCaptureOperations
from llm_wiki.markdown_undo import TransactionUndoAndPrune as TransactionUndoAndPrune
from llm_wiki.markdown_writer_gate import WriterGateOperations as WriterGateOperations
from llm_wiki.sqlite_contracts import (
    OperationalDatabaseContract as OperationalDatabaseContract,
)
from llm_wiki.sqlite_transactions import begin_immediate as begin_immediate
from llm_wiki.windows_acl import acl_entry_lines as acl_entry_lines
from llm_wiki.windows_acl import acl_failure as acl_failure
from llm_wiki.windows_acl import acl_line_names_owner as acl_line_names_owner
from llm_wiki.windows_acl import acl_lines_naming as acl_lines_naming
from llm_wiki.windows_acl import acl_output_encodings as acl_output_encodings
from llm_wiki.windows_acl import acl_output_text as acl_output_text
from llm_wiki.windows_acl import acl_permission as acl_permission
from llm_wiki.windows_acl import apply_windows_acl as apply_windows_acl
from llm_wiki.windows_acl import console_code_page as console_code_page
from llm_wiki.windows_acl import harden_windows_acl as harden_windows_acl
from llm_wiki.windows_acl import names_identity as names_identity
from llm_wiki.windows_acl import read_windows_acl as read_windows_acl
from llm_wiki.windows_acl import (
    require_verified_windows_owner_acl as require_verified_windows_owner_acl,
)
from llm_wiki.windows_acl import run_acl_command as run_acl_command
from llm_wiki.windows_acl import verified_owner_acl_line as verified_owner_acl_line
from llm_wiki.windows_acl import verify_no_other_acl as verify_no_other_acl
from llm_wiki.windows_acl import verify_windows_owner_acl as verify_windows_owner_acl
from llm_wiki.windows_acl import windows_acl_identity as windows_acl_identity
from llm_wiki.windows_native import (
    WINDOWS_KERNEL32_PROTOTYPES as WINDOWS_KERNEL32_PROTOTYPES,
)
from llm_wiki.windows_native import (
    WindowsDispositionInformation as WindowsDispositionInformation,
)
from llm_wiki.windows_native import (
    WindowsFileInformation as WindowsFileInformation,
)
from llm_wiki.windows_native import (
    windows_kernel32 as windows_kernel32,
)


def pid_alive(pid: int) -> bool:
    """One probe for every legacy lock (`process_liveness`); doubt is alive."""
    if pid <= 0:
        return False
    return process_liveness.pid_alive(pid)


def _redacted_record(record: TransactionRecord) -> dict[str, str | None]:
    return {
        "transaction_id": record.id,
        "state": record.state,
        "code": record.error_code,
    }


def _print_canonical_json(payload: object) -> None:
    sys.stdout.write(canonical_json_bytes(payload).decode("utf-8") + "\n")


def _bounded_transaction_id(value: object) -> str | None:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return None
    if any(character not in "0123456789abcdefghijklmnopqrstuvwxyz-_" for character in value):
        return None
    return value


def _cli_error_code(error: Exception) -> str:
    """Every refusal names its own code; the text is for the reader only."""
    if isinstance(error, (TransactionFailure, RefusedOperation, RefusedArgument)):
        return error.code
    if isinstance(error, KeyError):
        return "unknown_transaction"
    return _cli_type_code(error)


def _cli_type_code(error: Exception) -> str:
    if isinstance(error, TimeoutError):
        return "writer_busy"
    if isinstance(error, ValueError):
        return "invalid_argument"
    return "operation_failed"


def _main() -> int:
    args = _parse_cli_args()
    coordinator: MarkdownCoordinator | None = None
    try:
        coordinator = cli_coordinator()
        payload, exit_code = _run_cli_command(coordinator, args)
    except Exception as error:  # noqa: BLE001 - the CLI answers with one envelope
        _print_canonical_json(_cli_failure(coordinator, args, error))
        return 2
    _print_canonical_json(payload)
    return exit_code


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("recover", help="recover incomplete transactions")
    undo_parser = subparsers.add_parser("undo", help="undo a committed transaction")
    undo_parser.add_argument("transaction_id")
    prune_parser = subparsers.add_parser(
        "prune", help="prune expired transaction images"
    )
    prune_parser.add_argument(
        "--retention-days", type=int, default=UNDO_RETENTION_DAYS
    )
    return parser.parse_args()


def cli_coordinator() -> MarkdownCoordinator:
    """The coordinator the vault actually has, v3 or the legacy pair.

    Constructing `MarkdownCoordinator` on the raw paths has been dead since
    Reliability V3 adoption: adoption replaces the pre-adoption database with
    a JSON tombstone, so opening that path directly raises `file is not a
    database`. Every command here went with it — `prune`, so the undo trail
    grew to 5.5 GB with nothing enforcing the two-day retention that already
    existed, and `undo` and `recover`, which are the operator's only hands on
    a transaction. `reclaim_runtime_state.py` learned this on 2026-09-02 and
    the CLI never did.
    """
    return active_or_legacy_coordinator(runtime_roots.vault_root(), runtime_roots.state_root())


def _run_cli_command(
    coordinator: MarkdownCoordinator, args: argparse.Namespace
) -> tuple[object, int]:
    if args.command == "recover":
        return _cli_recover(coordinator)
    if args.command == "undo":
        return _cli_undo(coordinator, args.transaction_id), 0
    return {"pruned": coordinator.prune(retention_days=args.retention_days)}, 0


def _cli_recover(coordinator: MarkdownCoordinator) -> tuple[object, int]:
    """A transaction that needs attention is reported by the exit code."""
    records = coordinator.recover()
    payload = [_redacted_record(record) for record in records]
    needs_attention = any(
        record.state in {"conflicted", "quarantined"} for record in records
    )
    return payload, 2 if needs_attention else 0


def _cli_undo(
    coordinator: MarkdownCoordinator, transaction_id: str
) -> dict[str, object]:
    undo = coordinator.undo(transaction_id)
    committed = coordinator.apply(undo.id)
    return {
        **_redacted_record(committed),
        "parent_transaction_id": committed.parent_transaction_id,
    }


def _cli_failure(
    coordinator: MarkdownCoordinator | None,
    args: argparse.Namespace,
    error: Exception,
) -> dict[str, object]:
    transaction_id = _bounded_transaction_id(getattr(args, "transaction_id", None))
    return {
        "code": _cli_error_code(error),
        "state": _cli_transaction_state(coordinator, transaction_id),
        "transaction_id": transaction_id,
    }


def _cli_transaction_state(
    coordinator: MarkdownCoordinator | None, transaction_id: str | None
) -> str | None:
    """A lookup that fails must not replace the error being reported."""
    if coordinator is None or transaction_id is None:
        return None
    try:
        record = coordinator._record_if_present(transaction_id)
    except Exception:  # noqa: BLE001 - diagnosis is best effort here
        return None
    if record is None:
        return None
    return record.state


if __name__ == "__main__":
    raise SystemExit(_main())
