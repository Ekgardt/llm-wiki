"""Fenced SQLite queue for deferred memory-pipeline work."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import re
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, NoReturn

from llm_wiki.queue_artifacts import _artifact_chunks as _artifact_chunks
from llm_wiki.queue_artifacts import _ArtifactOutput as _ArtifactOutput
from llm_wiki.queue_artifacts import _check_owner_file_metadata as _check_owner_file_metadata
from llm_wiki.queue_artifacts import _cleanup_export_staging as _cleanup_export_staging
from llm_wiki.queue_artifacts import _copy_artifact_chunks as _copy_artifact_chunks
from llm_wiki.queue_artifacts import _digest_stable_owner_file as _digest_stable_owner_file
from llm_wiki.queue_artifacts import _discard_temporary as _discard_temporary
from llm_wiki.queue_artifacts import _file_identity as _file_identity
from llm_wiki.queue_artifacts import _harden_owner_only as _harden_owner_only
from llm_wiki.queue_artifacts import _is_owner_only as _is_owner_only
from llm_wiki.queue_artifacts import _is_owner_only_posix as _is_owner_only_posix
from llm_wiki.queue_artifacts import _link_result as _link_result
from llm_wiki.queue_artifacts import _publish_stream_file as _publish_stream_file
from llm_wiki.queue_artifacts import _read_stable_descriptor as _read_stable_descriptor
from llm_wiki.queue_artifacts import _read_stable_owner_file as _read_stable_owner_file
from llm_wiki.queue_artifacts import _remove_export_staging as _remove_export_staging
from llm_wiki.queue_artifacts import _require_matching_stream_file as _require_matching_stream_file
from llm_wiki.queue_artifacts import _require_stream_identity as _require_stream_identity
from llm_wiki.queue_artifacts import _stream_digest as _stream_digest
from llm_wiki.queue_artifacts import _write_durable_file as _write_durable_file
from llm_wiki.queue_artifacts import _write_durable_producer as _write_durable_producer
from llm_wiki.queue_artifacts import _write_durable_stream as _write_durable_stream
from llm_wiki.queue_artifacts import _write_result_file as _write_result_file
from llm_wiki.queue_attempts import _PERMANENT_CODES as _PERMANENT_CODES
from llm_wiki.queue_attempts import _apply_failure_state as _apply_failure_state
from llm_wiki.queue_attempts import _attempt_histories as _attempt_histories
from llm_wiki.queue_attempts import _failure_is_terminal as _failure_is_terminal
from llm_wiki.queue_attempts import _failure_state as _failure_state
from llm_wiki.queue_attempts import _lease_row_holds as _lease_row_holds
from llm_wiki.queue_attempts import _record_acknowledged_attempt as _record_acknowledged_attempt
from llm_wiki.queue_attempts import _record_cancelled_attempt as _record_cancelled_attempt
from llm_wiki.queue_attempts import _record_failed_attempt as _record_failed_attempt
from llm_wiki.queue_attempts import _require_lease_input as _require_lease_input
from llm_wiki.queue_attempts import _require_leased_row as _require_leased_row
from llm_wiki.queue_attempts import _retire_exhausted_history as _retire_exhausted_history
from llm_wiki.queue_attempts import _retire_exhausted_ready as _retire_exhausted_ready
from llm_wiki.queue_attempts import _take_task_lease as _take_task_lease
from llm_wiki.queue_attempts import _take_v3_task_lease as _take_v3_task_lease
from llm_wiki.queue_child_runtime import (
    _await_child_message as _await_child_message,
)
from llm_wiki.queue_child_runtime import (
    _child_ready_handshake as _child_ready_handshake,
)
from llm_wiki.queue_child_runtime import (
    _ChildRun as _ChildRun,
)
from llm_wiki.queue_child_runtime import (
    _decode_processor_frame as _decode_processor_frame,
)
from llm_wiki.queue_child_runtime import (
    _encode_processor_outcome,
    _send_processor_frame,
)
from llm_wiki.queue_child_runtime import (
    _settle_child_exit as _settle_child_exit,
)
from llm_wiki.queue_child_runtime import (
    run_processor_child as _run_child_runtime,
)
from llm_wiki.queue_contracts import _MAX_RESULT_BYTES
from llm_wiki.queue_contracts import _MAX_RUNTIME_ATTEMPTS as _MAX_RUNTIME_ATTEMPTS
from llm_wiki.queue_contracts import _STATES as _STATES
from llm_wiki.queue_evidence_records import _capture_resolution_record as _capture_resolution_record
from llm_wiki.queue_evidence_records import _capture_seal_digest as _capture_seal_digest
from llm_wiki.queue_evidence_records import _corrupt_disposition_key as _corrupt_disposition_key
from llm_wiki.queue_evidence_records import (
    _corrupt_disposition_record as _corrupt_disposition_record,
)
from llm_wiki.queue_evidence_records import _corrupt_manifest as _corrupt_manifest
from llm_wiki.queue_evidence_records import _corrupt_metadata as _corrupt_metadata
from llm_wiki.queue_evidence_records import _corrupt_records_match as _corrupt_records_match
from llm_wiki.queue_evidence_records import _CorruptEvidence as _CorruptEvidence
from llm_wiki.queue_evidence_records import _purge_receipt_record as _purge_receipt_record
from llm_wiki.queue_evidence_records import _require_identical_seal as _require_identical_seal
from llm_wiki.queue_migration_plan import _QUEUE_V2_FAILURE_COLUMNS as _QUEUE_V2_FAILURE_COLUMNS
from llm_wiki.queue_migration_plan import _QUEUE_V2_HISTORY_COLUMNS as _QUEUE_V2_HISTORY_COLUMNS
from llm_wiki.queue_migration_plan import _QUEUE_V2_MIGRATED_TABLES as _QUEUE_V2_MIGRATED_TABLES
from llm_wiki.queue_migration_plan import (
    _QUEUE_V2_REQUIRED_TASK_COLUMNS as _QUEUE_V2_REQUIRED_TASK_COLUMNS,
)
from llm_wiki.queue_migration_plan import _QUEUE_V2_SCHEMA_OBJECTS as _QUEUE_V2_SCHEMA_OBJECTS
from llm_wiki.queue_migration_plan import _QUEUE_V3_ONLY_TABLES as _QUEUE_V3_ONLY_TABLES
from llm_wiki.queue_migration_plan import _QUEUE_V3_TASK_COLUMNS as _QUEUE_V3_TASK_COLUMNS
from llm_wiki.queue_migration_plan import _already_ordered as _already_ordered
from llm_wiki.queue_migration_plan import _attempt_history_statements as _attempt_history_statements
from llm_wiki.queue_migration_plan import _migrated_attempts as _migrated_attempts
from llm_wiki.queue_migration_plan import _migrated_payload_bytes as _migrated_payload_bytes
from llm_wiki.queue_migration_plan import _migrated_task_values as _migrated_task_values
from llm_wiki.queue_migration_plan import _ordered_queue_v2_tasks as _ordered_queue_v2_tasks
from llm_wiki.queue_migration_plan import _parents_before_children as _parents_before_children
from llm_wiki.queue_migration_plan import _place_with_parents as _place_with_parents
from llm_wiki.queue_migration_plan import _queue_v2_child_counts as _queue_v2_child_counts
from llm_wiki.queue_migration_plan import _queue_v2_columns as _queue_v2_columns
from llm_wiki.queue_migration_plan import (
    _queue_v2_migration_statements as _queue_v2_migration_statements,
)
from llm_wiki.queue_migration_plan import (
    _queue_v2_reconciliation_complete as _queue_v2_reconciliation_complete,
)
from llm_wiki.queue_migration_plan import _queue_v2_tables as _queue_v2_tables
from llm_wiki.queue_migration_plan import _queue_v3_insert_statement as _queue_v3_insert_statement
from llm_wiki.queue_migration_plan import _queue_v3_row_matches as _queue_v3_row_matches
from llm_wiki.queue_migration_plan import _redrive_chain as _redrive_chain
from llm_wiki.queue_migration_plan import (
    _require_queue_v2_tasks_schema as _require_queue_v2_tasks_schema,
)
from llm_wiki.queue_migration_plan import _source_failure_migrated as _source_failure_migrated
from llm_wiki.queue_migration_plan import _source_failure_statement as _source_failure_statement
from llm_wiki.queue_migration_plan import _source_failure_statements as _source_failure_statements
from llm_wiki.queue_migration_plan import _tables_have_counts as _tables_have_counts
from llm_wiki.queue_migration_plan import _task_migration_statements as _task_migration_statements
from llm_wiki.queue_migration_plan import (
    _task_source_link_statements as _task_source_link_statements,
)
from llm_wiki.queue_migration_plan import (
    _validate_queue_v2_schema_objects as _validate_queue_v2_schema_objects,
)
from llm_wiki.queue_process_cleanup import (
    _pid_is_alive as _pid_is_alive,
)
from llm_wiki.queue_process_cleanup import (
    _taskkill as _taskkill,
)
from llm_wiki.queue_process_cleanup import (
    _terminate_processor_child as _terminate_processor_child,
)
from llm_wiki.queue_process_cleanup import (
    _tracked_descendant_pids as _tracked_descendant_pids,
)
from llm_wiki.queue_task_records import _attempt_history_entry as _attempt_history_entry
from llm_wiki.queue_task_records import _export_task_record as _export_task_record
from llm_wiki.queue_task_records import _exported_task_record as _exported_task_record
from llm_wiki.queue_task_records import _ExportedRows as _ExportedRows
from llm_wiki.queue_task_records import _task_record as _task_record
from llm_wiki.queue_validation import _capture_cursor_text as _capture_cursor_text
from llm_wiki.queue_validation import (
    _capture_decision_relative_path as _capture_decision_relative_path,
)
from llm_wiki.queue_validation import _capture_scan_cursor as _capture_scan_cursor
from llm_wiki.queue_validation import (
    _capture_terminal_disposition_fields as _capture_terminal_disposition_fields,
)
from llm_wiki.queue_validation import (
    _check_capture_intent_descriptor as _check_capture_intent_descriptor,
)
from llm_wiki.queue_validation import _check_capture_intent_digest as _check_capture_intent_digest
from llm_wiki.queue_validation import (
    _check_capture_intent_relative_path as _check_capture_intent_relative_path,
)
from llm_wiki.queue_validation import _check_capture_intent_size as _check_capture_intent_size
from llm_wiki.queue_validation import _check_consumer_id as _check_consumer_id
from llm_wiki.queue_validation import _check_consumer_kind as _check_consumer_kind
from llm_wiki.queue_validation import _check_policy_integer as _check_policy_integer
from llm_wiki.queue_validation import _check_quarantine_names as _check_quarantine_names
from llm_wiki.queue_validation import _check_ready_intent_identity as _check_ready_intent_identity
from llm_wiki.queue_validation import _check_resolution_reason as _check_resolution_reason
from llm_wiki.queue_validation import _check_result_bytes as _check_result_bytes
from llm_wiki.queue_validation import _check_result_operation as _check_result_operation
from llm_wiki.queue_validation import _check_result_request as _check_result_request
from llm_wiki.queue_validation import _check_retry_bounds as _check_retry_bounds
from llm_wiki.queue_validation import _check_retry_window as _check_retry_window
from llm_wiki.queue_validation import _is_positive_int as _is_positive_int
from llm_wiki.queue_validation import _require_bounded_int as _require_bounded_int
from llm_wiki.queue_validation import _require_bounded_intent_path as _require_bounded_intent_path
from llm_wiki.queue_validation import (
    _require_capture_decision_path as _require_capture_decision_path,
)
from llm_wiki.queue_validation import _require_capture_fence as _require_capture_fence
from llm_wiki.queue_validation import _require_capture_identity as _require_capture_identity
from llm_wiki.queue_validation import (
    _require_capture_intent_descriptor as _require_capture_intent_descriptor,
)
from llm_wiki.queue_validation import _require_capture_intent_path as _require_capture_intent_path
from llm_wiki.queue_validation import _require_capture_intent_size as _require_capture_intent_size
from llm_wiki.queue_validation import (
    _require_capture_terminal_path as _require_capture_terminal_path,
)
from llm_wiki.queue_validation import (
    _require_claim_owner_and_lease as _require_claim_owner_and_lease,
)
from llm_wiki.queue_validation import _require_dedupe_key as _require_dedupe_key
from llm_wiki.queue_validation import _require_enqueue_arguments as _require_enqueue_arguments
from llm_wiki.queue_validation import _require_error_code as _require_error_code
from llm_wiki.queue_validation import _require_known_states as _require_known_states
from llm_wiki.queue_validation import (
    _require_legacy_enqueue_arguments as _require_legacy_enqueue_arguments,
)
from llm_wiki.queue_validation import _require_lower_sha256 as _require_lower_sha256
from llm_wiki.queue_validation import (
    _require_matching_decision_owner as _require_matching_decision_owner,
)
from llm_wiki.queue_validation import _require_non_empty_string as _require_non_empty_string
from llm_wiki.queue_validation import _require_positive_lease as _require_positive_lease
from llm_wiki.queue_validation import _require_positive_task_lease as _require_positive_task_lease
from llm_wiki.queue_validation import _require_repair_owner as _require_repair_owner
from llm_wiki.queue_validation import (
    _require_semantic_decision_shape as _require_semantic_decision_shape,
)
from llm_wiki.queue_validation import _require_task_identifier as _require_task_identifier
from llm_wiki.queue_validation import _require_task_kind as _require_task_kind
from llm_wiki.queue_validation import _require_worker_intent_fence as _require_worker_intent_fence
from llm_wiki.queue_validation import _valid_heartbeat_interval as _valid_heartbeat_interval
from llm_wiki.queue_validation import _validate_retry_policy as _validate_retry_policy
from llm_wiki.queue_validation import _validate_worker_policy as _validate_worker_policy
from llm_wiki.queue_worker_state import PROCESSOR_ERROR_CODE_BYTES as PROCESSOR_ERROR_CODE_BYTES
from llm_wiki.queue_worker_state import QueueRetryPolicy as QueueRetryPolicy
from llm_wiki.queue_worker_state import _ProcessorOutcome as _ProcessorOutcome
from llm_wiki.queue_worker_state import _WorkerProgress as _WorkerProgress
from llm_wiki.queue_worker_state import processor_error_code as processor_error_code

if TYPE_CHECKING:
    from markdown_transaction import IntentFence
    from operational_ownership import OwnerLease

import compile_progress
from llm_wiki.formats.canonical import canonical_json_bytes, sha256_bytes
from llm_wiki.formats.queue_payload import (
    _MAX_QUEUE_PAYLOAD_BYTES,
)
from llm_wiki.formats.queue_payload import (
    PayloadValidation as PayloadValidation,
)
from llm_wiki.formats.queue_payload import (
    validate_payload_blob as validate_payload_blob,
)
from llm_wiki.formats.schema import validate_schema
from llm_wiki.queue_contracts import AttemptRecord as AttemptRecord
from llm_wiki.queue_contracts import CaptureTaskBinding as CaptureTaskBinding
from llm_wiki.queue_contracts import CompileCompletionError as CompileCompletionError
from llm_wiki.queue_contracts import CorruptExportProgress as CorruptExportProgress
from llm_wiki.queue_contracts import CorruptPurgeProgress as CorruptPurgeProgress
from llm_wiki.queue_contracts import DeadCaptureRedrive as DeadCaptureRedrive
from llm_wiki.queue_contracts import DeferredResult as DeferredResult
from llm_wiki.queue_contracts import LeaseFenceError as LeaseFenceError
from llm_wiki.queue_contracts import LeaseProcessor as LeaseProcessor
from llm_wiki.queue_contracts import MigrationBusy as MigrationBusy
from llm_wiki.queue_contracts import PurgeReceipt as PurgeReceipt
from llm_wiki.queue_contracts import QueueFailure as QueueFailure
from llm_wiki.queue_contracts import QueueLease as QueueLease
from llm_wiki.queue_contracts import QueueOperationError as QueueOperationError
from llm_wiki.queue_contracts import QueueTask as QueueTask
from llm_wiki.queue_contracts import RestoreReceipt as RestoreReceipt
from llm_wiki.queue_contracts import ResultConflictError as ResultConflictError
from llm_wiki.queue_contracts import SemanticDecision as SemanticDecision
from llm_wiki.queue_contracts import SourceFence as SourceFence
from llm_wiki.queue_contracts import WorkerSummary as WorkerSummary
from llm_wiki.queue_heartbeat import (
    _LeaseHeartbeat as _LeaseHeartbeat,
)
from llm_wiki.queue_heartbeat import (
    _SourceFenceHeartbeat as _SourceFenceHeartbeat,
)
from llm_wiki.queue_schema import (
    _QUEUE_V3_INDEX_SQL as _QUEUE_V3_INDEX_SQL,
)
from llm_wiki.queue_schema import (
    _QUEUE_V3_TABLE_SQL as _QUEUE_V3_TABLE_SQL,
)
from llm_wiki.queue_schema import (
    _QUEUE_V3_TRIGGER_SQL as _QUEUE_V3_TRIGGER_SQL,
)
from llm_wiki.queue_schema_operations import _QUEUE_V3_DROP_KEYWORD as _QUEUE_V3_DROP_KEYWORD
from llm_wiki.queue_schema_operations import _QUEUE_V3_DROP_ORDER as _QUEUE_V3_DROP_ORDER
from llm_wiki.queue_schema_operations import (
    _check_no_populated_rebuild as _check_no_populated_rebuild,
)
from llm_wiki.queue_schema_operations import _drop_order_key as _drop_order_key
from llm_wiki.queue_schema_operations import _drop_schema_object as _drop_schema_object
from llm_wiki.queue_schema_operations import _drop_unexpected_objects as _drop_unexpected_objects
from llm_wiki.queue_schema_operations import _expected_queue_v3_schema as _expected_queue_v3_schema
from llm_wiki.queue_schema_operations import (
    _malformed_queue_v3_objects as _malformed_queue_v3_objects,
)
from llm_wiki.queue_schema_operations import _normalized_sql as _normalized_sql
from llm_wiki.queue_schema_operations import _queue_schema_rows as _queue_schema_rows
from llm_wiki.queue_schema_operations import _queue_v3_object_matches as _queue_v3_object_matches
from llm_wiki.queue_schema_operations import _queue_v3_schema_complete as _queue_v3_schema_complete
from llm_wiki.queue_schema_operations import _queue_v3_statements as _queue_v3_statements
from llm_wiki.queue_schema_operations import (
    _queue_v3_trigger_statements as _queue_v3_trigger_statements,
)
from llm_wiki.queue_schema_operations import (
    _repair_partial_queue_v3_schema as _repair_partial_queue_v3_schema,
)
from llm_wiki.queue_schema_operations import _table_is_populated as _table_is_populated
from llm_wiki.queue_sources import _SOURCE_IDENTITY_KEYS as _SOURCE_IDENTITY_KEYS
from llm_wiki.queue_sources import _collect_identity_from_mapping as _collect_identity_from_mapping
from llm_wiki.queue_sources import (
    _collect_source_identity_strings as _collect_source_identity_strings,
)
from llm_wiki.queue_sources import _distinct_source_identities as _distinct_source_identities
from llm_wiki.queue_sources import _identity_paths_or_daily as _identity_paths_or_daily
from llm_wiki.queue_sources import _link_pair as _link_pair
from llm_wiki.queue_sources import _normalized_source_link as _normalized_source_link
from llm_wiki.queue_sources import _normalized_source_links as _normalized_source_links
from llm_wiki.queue_sources import _payload_identity_fields as _payload_identity_fields
from llm_wiki.queue_sources import _queue_v3_source_links as _queue_v3_source_links
from llm_wiki.queue_sources import _require_canonical_daily_id as _require_canonical_daily_id
from llm_wiki.queue_sources import (
    _require_single_source_identity as _require_single_source_identity,
)
from llm_wiki.queue_sources import _source_identity_from_payload as _source_identity_from_payload
from llm_wiki.queue_sources import _source_identity_strings as _source_identity_strings
from llm_wiki.queue_sources import _unsafe_link_path as _unsafe_link_path
from llm_wiki.queue_sources import _unsafe_link_shape as _unsafe_link_shape
from llm_wiki.queue_sources import _validate_failure_identity as _validate_failure_identity
from llm_wiki.queue_sources import _validate_source_identity as _validate_source_identity
from llm_wiki.queue_sources import _validated_failure_identity as _validated_failure_identity
from llm_wiki.queue_timing import _as_utc as _as_utc
from llm_wiki.queue_timing import _bounded_retry_number as _bounded_retry_number
from llm_wiki.queue_timing import _bounded_retry_until as _bounded_retry_until
from llm_wiki.queue_timing import _no_retry_after as _no_retry_after
from llm_wiki.queue_timing import _parse_timestamp as _parse_timestamp
from llm_wiki.queue_timing import _retry_after_deadline as _retry_after_deadline
from llm_wiki.queue_timing import _retry_after_from_text as _retry_after_from_text
from llm_wiki.queue_timing import _retry_after_number as _retry_after_number
from llm_wiki.queue_timing import _retry_after_seconds as _retry_after_seconds
from llm_wiki.queue_timing import _retry_after_text as _retry_after_text
from llm_wiki.queue_timing import _retry_available_at as _retry_available_at
from llm_wiki.queue_timing import _timestamp as _timestamp
from llm_wiki.reliability_defaults import DEFAULTS
from llm_wiki.restricted_paths import restricted_relative_path
from llm_wiki.sqlite_contracts import (
    MigrationStatement,
    OperationalDatabaseContract,
    OperationalDatabaseContractError,
    quote_sqlite_identifier,
)
from llm_wiki.sqlite_contracts import _migration_error as _migration_error
from llm_wiki.sqlite_deadlines import (
    configure_operational_deadline,
    operational_deadline_active,
    refresh_operational_deadline,
)
from llm_wiki.sqlite_migrations import run_resumable_migration
from llm_wiki.sqlite_pragmas import _validate_or_initialize_operational_contract
from llm_wiki.sqlite_transactions import begin_immediate
from llm_wiki.windows_acl import _acl_is_owner_only as _acl_is_owner_only
from llm_wiki.windows_acl import _acl_owner_lines as _acl_owner_lines
from llm_wiki.windows_acl import _is_owner_only_windows as _is_owner_only_windows
from llm_wiki.windows_acl import _windows_acl_lines as _windows_acl_lines
from reliable_memory import (
    MAX_CAPTURE_DECISION_BYTES,
    fsync_directory,
    fsync_file,
    open_operational_db,
    open_readonly_operational_db,
    read_runtime_bytes,
)
from secret_redact import redact_secrets, redact_structure

# A task fence's own length; a holder doing slow work renews it
# (`heartbeat_task_fence`).
TASK_FENCE_SECONDS = 120
_TERMINAL_STATES = ("succeeded", "dead", "cancelled")
_MAX_EXPORT_METADATA_BYTES = 64 * 1024 * 1024
# One trail line per failed processor. The trail is read by a person at
# session start, so the reason has to fit a line, not carry a stack.
MAX_PROCESSOR_REASON_CHARS = 300
_MAX_CLI_DETAIL_CHARS = 240
_QUEUE_V3_CONTRACT = OperationalDatabaseContract(application_id=0x4C575133)

QUEUE_V3_SCHEMA_SHA256 = sha256_bytes(
    canonical_json_bytes(
        {
            "tables": [list(item) for item in _QUEUE_V3_TABLE_SQL],
            "indexes": [list(item) for item in _QUEUE_V3_INDEX_SQL],
            "triggers": [list(item) for item in _QUEUE_V3_TRIGGER_SQL],
            "application_id": _QUEUE_V3_CONTRACT.application_id,
            "user_version": _QUEUE_V3_CONTRACT.user_version,
        }
    )
)

QUEUE_COMPILE_SCHEMA_SHA256 = sha256_bytes(canonical_json_bytes({
    "base": QUEUE_V3_SCHEMA_SHA256,
    "version": compile_progress.QUEUE_SCHEMA_VERSION,
    "journal": list(compile_progress.SCHEMA_STATEMENTS),
}))
_QUEUE_COMPILE_CONTRACT = OperationalDatabaseContract(
    application_id=_QUEUE_V3_CONTRACT.application_id,
    user_version=compile_progress.QUEUE_SCHEMA_VERSION,
)


def _ordinary_purge_operation_id(manifest_sha256: str) -> str:
    return f"ordinary-purge:{manifest_sha256}"


def _capture_purge_archive_path(task_id: str, source_path: str) -> str:
    key = sha256_bytes(
        canonical_json_bytes({"source_path": source_path, "task_id": task_id})
    )
    return f"capture-artifacts/{key}.artifact"


def _cleanable_result_reference(
    record: Mapping[str, object], retained_terminals: set[str]
) -> str | None:
    """The result reference this record may drop, or None when it keeps it."""
    result = record["result"]
    if not isinstance(result, dict):
        return None
    reference = str(result["reference"])
    return None if reference in retained_terminals else reference


def _ordinary_purge_result_archive_path(task_id: str) -> str:
    value = f"results/{task_id}.result"
    try:
        restricted_relative_path(value, ("results",))
    except ValueError as exc:
        raise QueueOperationError("export_verification_failed") from exc
    return value


def _ordinary_purge_authorization_digest(
    task_id: str, operation_id: str, manifest_sha256: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "manifest_sha256": manifest_sha256,
                "operation_id": operation_id,
                "task_id": task_id,
            }
        )
    )


def _ordinary_purge_manifest_digest(operation_id: object) -> str | None:
    prefix = "ordinary-purge:"
    if not isinstance(operation_id, str) or not operation_id.startswith(prefix):
        return None
    digest = operation_id.removeprefix(prefix)
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        return None
    return digest


def _queue_v3_purge_authorization_valid(row: sqlite3.Row) -> bool:
    manifest_sha256 = _ordinary_purge_manifest_digest(row["operation_id"])
    if row["mode"] != "ordinary" or row["task_present"]:
        return False
    if manifest_sha256 is None:
        return False
    expected = _ordinary_purge_authorization_digest(
        str(row["task_id"]), str(row["operation_id"]), manifest_sha256
    )
    return row["authorization_digest"] == expected


def _queue_v3_purge_authorizations_valid(database: sqlite3.Connection) -> bool:
    rows = database.execute(
        """SELECT authorization.*,
                  EXISTS(SELECT 1 FROM tasks WHERE id=authorization.task_id)
                    AS task_present
           FROM task_purge_authorizations AS authorization"""
    ).fetchall()
    return all(_queue_v3_purge_authorization_valid(row) for row in rows)


# Which bucket each payload key contributes a source-identity string to.

def _still_held(source: sqlite3.Connection, table: str, where: str) -> bool:
    """Whether any such row is inside its lease; a null expiry cannot say, so it is.

    A fence row outlives the process that took it — the live v2 queue sweeps
    those on every open — and refusing adoption for a leftover locks a vault out
    for good. See `docs/research/
    2026-09-18-an-expired-fence-is-not-an-obstacle-to-adoption.md`.
    """
    now = _timestamp(_utc_now())
    rows = source.execute(
        f"SELECT expires_at FROM {table} WHERE {where}"  # noqa: S608 - fixed names
    ).fetchall()
    return any(row["expires_at"] is None or str(row["expires_at"]) > now for row in rows)


def _require_unambiguous_v2_owners(
    source: sqlite3.Connection, tables: set[str]
) -> None:
    """A v2 fence still held predates the identity v3 needs to fence with."""
    if "source_fences" in tables and _still_held(source, "source_fences", "1=1"):
        raise _migration_error(
            "queue_v2_source_fence_ambiguous",
            "queue v2 source fences lack process-start identity",
        )
    if "queue_ownership" in tables and _still_held(
        source, "queue_ownership", "token IS NOT NULL"
    ):
        raise _migration_error(
            "queue_v2_owner_ambiguous",
            "queue v2 ownership lacks canonical process identity",
        )


# The tables a queue v2 migration fills, and the ones it must leave empty.

def _queue_v3_row_counts(database: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(database.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table, _sql in _QUEUE_V3_TABLE_SQL
    }


def _names_the_same_file(candidate: Path, source: Path) -> bool:
    """Whether an existing candidate resolves to the very same file."""
    if not candidate.exists() and not candidate.is_symlink():
        return False
    return os.path.samefile(candidate, source)


def _reject_queue_source_alias(candidate: Path, source: Path) -> None:
    if candidate.absolute() == source.absolute():
        raise ValueError("queue v3 candidate and v2 source are the same file")
    try:
        aliased = _names_the_same_file(candidate, source)
    except OSError as exc:
        raise PermissionError(
            "queue v3 candidate identity could not be verified"
        ) from exc
    if aliased:
        raise ValueError("queue v3 candidate and v2 source are the same file")


def _queue_v3_payloads_valid(database: sqlite3.Connection) -> bool:
    for row in database.execute(
        "SELECT payload_blob, input_hash, state, error_code FROM tasks"
    ):
        payload_bytes = bytes(row["payload_blob"])
        validation = validate_payload_blob(
            payload_bytes, row["input_hash"], parse=True
        )
        if validation.code is not None and (
            row["state"] != "dead" or row["error_code"] != "payload_hash_mismatch"
        ):
            return False
    return True


_EMPTY_QUEUE_V3_SUMMARY = MappingProxyType(
    {
        "attempt_history": 0,
        "payload_hash_mismatches": 0,
        "source_failures": 0,
        "source_fences": 0,
        "task_source_links": 0,
        "tasks": 0,
    }
)


def _open_queue_v2_source(
    stack: ExitStack, candidate: Path, source_path: Path
) -> sqlite3.Connection:
    """Open the v2 source read-only, refusing one that aliases the candidate."""
    _reject_queue_source_alias(candidate, source_path)
    source = stack.enter_context(
        closing(
            open_readonly_operational_db(
                source_path,
                source_path.parent.parent,
                max_bytes=1 << 50,
                owner_only=True,
            )
        )
    )
    source.row_factory = sqlite3.Row
    source.execute("BEGIN")
    return source


def _queue_v2_migration_plan(
    stack: ExitStack, candidate: Path, source_path: Path | None
) -> tuple[tuple[MigrationStatement, ...], dict[str, int]]:
    """What the v2 source contributes, or nothing when there is no source."""
    if source_path is None:
        return (), dict(_EMPTY_QUEUE_V3_SUMMARY)
    source = _open_queue_v2_source(stack, candidate, source_path)
    statements, summary = _queue_v2_migration_statements(
        source, check_owners=_require_unambiguous_v2_owners
    )
    _reject_queue_source_alias(candidate, source_path)
    return statements, summary


def _require_empty_queue_v3(database: sqlite3.Connection) -> None:
    """A fresh initialization may not find rows already there."""
    if any(_queue_v3_row_counts(database).values()):
        raise _migration_error(
            "queue_v3_source_conflict",
            "fresh queue v3 initialization found existing rows",
        )


def _queue_v3_candidate_consistent(database: sqlite3.Connection) -> bool:
    """Whether the candidate agrees with itself: pages, keys and payloads."""
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        return False
    if database.execute("PRAGMA foreign_key_check").fetchall():
        return False
    return _queue_v3_payloads_valid(database)


def _validate_queue_v3_candidate(database: sqlite3.Connection) -> None:
    """The candidate has to be internally consistent before it is published."""
    if not _queue_v3_candidate_consistent(database):
        raise _migration_error(
            "queue_v3_validation_failed", "queue v3 candidate validation failed"
        )


def _build_queue_v3_candidate(
    stack: ExitStack,
    candidate: Path,
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> dict[str, int]:
    """Create or resume the candidate's schema and content, then validate it."""
    migration_statements, summary = _queue_v2_migration_plan(
        stack, candidate, source_path
    )
    database = stack.enter_context(
        closing(open_operational_db(candidate, busy_ms=DEFAULTS.queue_busy_ms))
    )
    if source_path is not None:
        _reject_queue_source_alias(candidate, source_path)
    _repair_partial_queue_v3_schema(
        database, allow_populated_rebuild=source_path is not None
    )
    run_resumable_migration(
        database,
        _queue_v3_statements(include_triggers=False),
        final_invariant=lambda current: _queue_v3_schema_complete(
            current, include_triggers=False
        ),
        killpoint=killpoint,
    )
    _apply_queue_v2_content(
        database, migration_statements, summary, source_path, killpoint
    )
    run_resumable_migration(
        database,
        _queue_v3_trigger_statements(),
        final_invariant=_queue_v3_schema_complete,
        killpoint=killpoint,
    )
    _validate_queue_v3_candidate(database)
    return summary


def _apply_queue_v2_content(
    database: sqlite3.Connection,
    migration_statements: tuple[MigrationStatement, ...],
    summary: Mapping[str, int],
    source_path: Path | None,
    killpoint: Callable[[str], None] | None,
) -> None:
    """Copy the v2 rows in, or prove the fresh candidate has none."""
    if source_path is None:
        _require_empty_queue_v3(database)
        return
    run_resumable_migration(
        database,
        migration_statements,
        final_invariant=lambda current: _queue_v2_reconciliation_complete(
            current, migration_statements, summary
        ),
        killpoint=killpoint,
    )


def _stamp_queue_v3_contract(candidate: Path) -> None:
    """Write the contract row that marks the candidate as a v3 database."""
    with closing(
        open_operational_db(
            candidate,
            busy_ms=DEFAULTS.queue_busy_ms,
            contract=_QUEUE_V3_CONTRACT,
            initialize_contract=True,
        )
    ):
        pass


def initialize_queue_v3_candidate(
    path: Path,
    *,
    source_v2: Path | None,
    killpoint: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Create or resume an unpublished queue v3 candidate database."""
    candidate = Path(path)
    source_path = Path(source_v2) if source_v2 is not None else None
    with ExitStack() as stack:
        summary = _build_queue_v3_candidate(
            stack, candidate, source_path, killpoint
        )
    _stamp_queue_v3_contract(candidate)
    _harden_owner_only(candidate, 0o600)
    validate_queue_v3_database(candidate, state_root=candidate.parent.parent)
    return summary


def _require_inside_state_root(path: Path, state_root: Path) -> None:
    """Refuse a database that does not live under the runtime root."""
    try:
        path.resolve(strict=True).relative_to(state_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PermissionError("queue v3 database is outside the state root") from exc


def _queue_contract_for_version(version: int) -> OperationalDatabaseContract:
    contracts = {_QUEUE_V3_CONTRACT.user_version: _QUEUE_V3_CONTRACT,
                 compile_progress.QUEUE_SCHEMA_VERSION: _QUEUE_COMPILE_CONTRACT}
    if version not in contracts:
        raise OperationalDatabaseContractError("unsupported queue database version")
    return contracts[version]


def _require_supported_queue_contract(database: sqlite3.Connection) -> None:
    version = database.execute("PRAGMA user_version").fetchone()[0]
    contract = _queue_contract_for_version(version)
    _validate_or_initialize_operational_contract(database, contract, initialize=False)
    if version == compile_progress.QUEUE_SCHEMA_VERSION:
        compile_progress._require_schema(database)


def _checked_queue_connection(database: sqlite3.Connection, expected_contract=None) -> sqlite3.Connection:
    try:
        _require_supported_queue_contract(database)
        if expected_contract is not None:
            _validate_or_initialize_operational_contract(database, expected_contract, initialize=False)
        return database
    except BaseException:
        database.close()
        raise


def _open_queue_v3_readonly(path: Path, state_root: Path, *, deadline: float | None = None, expected_contract=None) -> sqlite3.Connection:
    """Read a supported queue version; adoption still binds the exact active one."""
    return _checked_queue_connection(open_readonly_operational_db(
        path, state_root, max_bytes=None, owner_only=True,
        busy_ms=DEFAULTS.queue_busy_ms, deadline=deadline,
    ), expected_contract)


def _open_queue_operational(path: Path, *, busy_ms: int, deadline=None, expected_contract=None) -> sqlite3.Connection:
    return _checked_queue_connection(open_operational_db(
        path, busy_ms=busy_ms, deadline=deadline,
    ), expected_contract)


def _queue_v3_payload_violations(database: sqlite3.Connection) -> int:
    """How many task rows hold a payload or attempt count out of contract."""
    return database.execute(
        """SELECT COUNT(*) FROM tasks
           WHERE typeof(payload_blob) != 'blob'
              OR length(payload_blob) > ?
              OR attempts NOT BETWEEN 0 AND 100""",
        (_MAX_QUEUE_PAYLOAD_BYTES,),
    ).fetchone()[0]


def _queue_v3_structure_sound(database: sqlite3.Connection) -> bool:
    """Whether SQLite itself reports the file and its references intact."""
    integrity = database.execute("PRAGMA integrity_check").fetchall()
    if len(integrity) != 1 or integrity[0][0] != "ok":
        return False
    return not database.execute("PRAGMA foreign_key_check").fetchall()


def _queue_v3_invariants_hold(database: sqlite3.Connection) -> bool:
    """Whether every invariant a queue v3 database has to keep still holds."""
    return (
        _queue_v3_structure_sound(database)
        and not _queue_v3_payload_violations(database)
        and _queue_v3_purge_authorizations_valid(database)
        and _queue_v3_payloads_valid(database)
    )


def _queue_v3_report(database: sqlite3.Connection) -> dict[str, object]:
    """The health record a validated queue v3 database answers with."""
    return {
        "application_id": database.execute("PRAGMA application_id").fetchone()[0],
        "foreign_key_check": [],
        "integrity_check": "ok",
        "journal_mode": database.execute("PRAGMA journal_mode").fetchone()[0],
        "row_counts": _queue_v3_row_counts(database),
        "synchronous": database.execute("PRAGMA synchronous").fetchone()[0],
        "trusted_schema": database.execute("PRAGMA trusted_schema").fetchone()[0],
        "user_version": database.execute("PRAGMA user_version").fetchone()[0],
    }


def require_queue_v3_openable(path: Path, *, state_root: Path, deadline: float | None = None, expected_contract=None) -> None:
    """What every open of the adopted queue checks: place, contract, schema.

    The whole-file check (`validate_queue_v3_database`) reads every retained row,
    so it belongs where a file is certified — adoption, backup, a candidate —
    and not on each hook's open. A bad payload is a fact about one task; the
    transition that touches that task demotes it, which a veto on open prevents.
    """
    _require_inside_state_root(Path(path), Path(state_root))
    with closing(_open_queue_v3_readonly(
        Path(path), Path(state_root), deadline=deadline, expected_contract=expected_contract,
    )) as database:
        if not _queue_v3_schema_complete(database):
            raise _migration_error(
                "queue_v3_schema_incomplete", "queue v3 schema is incomplete"
            )


def validate_queue_v3_database(
    path: Path, *, state_root: Path
) -> dict[str, object]:
    """Validate one unpublished or active queue v3 database fail-closed."""
    _require_inside_state_root(Path(path), Path(state_root))
    with closing(_open_queue_v3_readonly(Path(path), Path(state_root))) as database:
        if not _queue_v3_schema_complete(database):
            raise _migration_error(
                "queue_v3_schema_incomplete", "queue v3 schema is incomplete"
            )
        if not _queue_v3_invariants_hold(database):
            raise _migration_error(
                "queue_v3_validation_failed", "queue v3 database invariant failed"
            )
        report = _queue_v3_report(database)
        report["compile_progress"] = audit_compile_progress(database, state_root)
        return report


def audit_compile_progress(database, state_root: Path, *, deadline=None):
    """Share intermediate-artifact certification with doctor and staged backup."""
    from compile_progress import audit_events

    queue = _QueueV3CandidateReader(Path(state_root) / "run" / "queue-v3.sqlite3")
    return audit_events(database, partial(queue.verify_compile_artifact, deadline=deadline), deadline=deadline)

def _require_active(
    deadline: float, cancelled: Callable[[], bool] | None = None
) -> None:
    if time.monotonic() >= deadline or bool(cancelled and cancelled()):
        raise TimeoutError("queue mutation deadline or cancellation reached")


def _source_failure_commit_active(database, deadline):
    _require_active(deadline)
    refresh_operational_deadline(database, DEFAULTS.queue_busy_ms, deadline)


@contextmanager
def _source_failure_connection(queue, deadline):
    _require_active(deadline)
    connection = queue._connect(deadline=deadline)
    with _queue_connection_context(connection) as database:
        with _source_failure_deadline(database, deadline):
            yield database


def _queue_connection_context(connection):
    if isinstance(connection, sqlite3.Connection):
        return closing(connection)
    return connection


@contextmanager
def _source_failure_deadline(database, deadline):
    _require_active(deadline)
    if deadline == float("inf"):
        yield database
        return
    configure_operational_deadline(database, deadline)
    try:
        yield database
        _require_active(deadline)
    except sqlite3.OperationalError:
        _require_active(deadline)
        raise
    finally:
        database.set_progress_handler(None, 0)


class _InvalidArguments(Exception):
    pass


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InvalidArguments

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            del message
            raise _InvalidArguments
        super().exit(status, message)


def _in_redrive_family(database: sqlite3.Connection, task_id: str) -> bool:
    """The task was redriven, or is itself a redrive."""
    row = database.execute(
        "SELECT 1 FROM tasks WHERE (id=? AND redrive_of IS NOT NULL) OR redrive_of=? LIMIT 1",
        (task_id, task_id),
    ).fetchone()
    return row is not None


def _capture_semantic_seal_digest(
    task_id: str, intent_id: str, stage: str, active_link_digest: str
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "active_digest": active_link_digest,
                "consumer_id": f"{intent_id}:{stage}",
                "consumer_kind": "semantic-decision",
                "task_id": task_id,
            }
        )
    )


def _require_indexed_capture_decision(
    row: sqlite3.Row,
    active: CaptureTaskBinding,
    *,
    intent_id: str,
    stage: str,
    active_link_digest: str,
) -> None:
    decision_path = _capture_decision_relative_path(intent_id, stage)
    seal_digest = _capture_semantic_seal_digest(
        active.task_id, intent_id, stage, active_link_digest
    )
    actual = (
        active.intent_id,
        active.active_digest,
        active.seal_digest,
        row["decision_path"],
        row["active_link_digest"],
        row["publication_state"],
    )
    expected = (
        intent_id,
        active_link_digest,
        seal_digest,
        decision_path,
        active_link_digest,
        "published",
    )
    if actual != expected:
        raise QueueOperationError("semantic_decision_conflict")


def _decision_sealer(
    database: sqlite3.Connection, row: sqlite3.Row, intent_id: str, stage: str
) -> sqlite3.Row:
    """The seal of the task that published this decision, proven by its own digest."""
    seal = database.execute(
        """SELECT * FROM capture_task_link_seals
           WHERE consumer_kind='semantic-decision' AND consumer_id=? AND active_digest=?""",
        (f"{intent_id}:{stage}", row["active_link_digest"]),
    ).fetchone()
    if seal is None:
        raise QueueOperationError("semantic_decision_conflict")
    expected = _capture_semantic_seal_digest(
        str(seal["task_id"]), intent_id, stage, str(row["active_link_digest"])
    )
    if seal["seal_digest"] != expected:
        raise QueueOperationError("semantic_decision_conflict")
    return seal


def _redrive_lineage(database: sqlite3.Connection, task_id: str) -> tuple[str, ...]:
    """Current task through origin; corruption never means a fresh execution."""
    seen, lineage = set(), []
    current: str | None = task_id
    while current is not None:
        if current in seen:
            raise QueueOperationError("redrive_lineage_cycle")
        seen.add(current)
        lineage.append(current)
        current = _redrive_parent(database, current)
    return tuple(lineage)


def _redrive_ancestors(database: sqlite3.Connection, task_id: str) -> set[str]:
    """Every ancestor after rejecting cycles and missing rows; no depth cap."""
    return set(_redrive_lineage(database, task_id)[1:])


def _required_redrive_row(database, task_id):
    row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise QueueOperationError("redrive_lineage_missing")
    return row


def _redrive_parent(database: sqlite3.Connection, task_id: str) -> str | None:
    row = _required_redrive_row(database, task_id)
    if row["redrive_of"] is None:
        return None
    return str(row["redrive_of"])


def _compile_lineage_signature(row):
    return tuple(row[name] for name in ('kind', 'handler_version', 'input_hash'))


def _require_compile_lineage_row(database, task_id, expected):
    row = _required_redrive_row(database, task_id)
    validation = validate_payload_blob(bytes(row['payload_blob']), row['input_hash'], parse=True)
    if validation.code is not None or _compile_lineage_signature(row) != expected:
        raise QueueOperationError('redrive_lineage_payload_mismatch')


def _compile_lineage_origin(database, row):
    if row['kind'] != 'compile':
        raise compile_progress.ProgressConflict('only compile tasks have compile work')
    lineage = _redrive_lineage(database, row['id'])
    expected = _compile_lineage_signature(row)
    for task_id in lineage:
        _require_compile_lineage_row(database, task_id, expected)
    return lineage[-1]


def _decision_seal_of(
    database: sqlite3.Connection, task_id: str, intent_id: str, stage: str
) -> sqlite3.Row | None:
    """This task's seal to the decision, when its digest proves it."""
    seal = database.execute(
        """SELECT * FROM capture_task_link_seals WHERE task_id=?
           AND consumer_kind='semantic-decision' AND consumer_id=?""",
        (task_id, f"{intent_id}:{stage}"),
    ).fetchone()
    if seal is None:
        return None
    expected = _capture_semantic_seal_digest(task_id, intent_id, stage, str(seal["active_digest"]))
    return seal if seal["seal_digest"] == expected else None


def _sealed_binding(seal: sqlite3.Row, intent_id: str) -> dict[str, str]:
    """The capture binding a Markdown transaction records for this seal."""
    return {
        "intent_id": intent_id,
        "task_id": str(seal["task_id"]),
        "active_link_digest": str(seal["active_digest"]),
        "seal_digest": str(seal["seal_digest"]),
    }


def _require_inherited_capture_decision(
    database: sqlite3.Connection,
    sealer: sqlite3.Row,
    active: CaptureTaskBinding,
    *,
    intent_id: str,
    stage: str,
    active_link_digest: str,
) -> None:
    """A decision an ancestor of this redrive sealed for the same intent.

    The decision is one per intent and stage, but its seal and link belong to the
    task that made it; a redrive re-signs the link for the child, so the child
    could never match its parent's seal (audit 2026-09-27 B-14). A redrive keeps
    what its ancestor already decided, as a workflow redrive keeps completed steps
    (docs/research/2026-09-27-a-redrive-keeps-the-decision-its-parent-sealed.md).
    """
    own_seal = _capture_semantic_seal_digest(active.task_id, intent_id, stage, active_link_digest)
    actual = (active.intent_id, active.active_digest, active.seal_digest in (None, own_seal))
    if actual != (intent_id, active_link_digest, True):
        raise QueueOperationError("semantic_decision_conflict")
    if str(sealer["task_id"]) not in _redrive_ancestors(database, active.task_id):
        raise QueueOperationError("semantic_decision_conflict")


def _indexed_capture_decision_published_at(row: sqlite3.Row) -> datetime:
    published_at = _parse_timestamp(str(row["published_at"]))
    if published_at is None:
        raise QueueOperationError("semantic_decision_conflict")
    return published_at


def _read_indexed_capture_decision(
    state_root: Path, row: sqlite3.Row
) -> tuple[str, str]:
    decision_path = str(row["decision_path"])
    decision_sha256 = str(row["decision_sha256"])
    _require_lower_sha256(decision_sha256, "decision_sha256")
    data = read_runtime_bytes(
        state_root / decision_path,
        state_root,
        max_bytes=MAX_CAPTURE_DECISION_BYTES,
        owner_only=True,
    )
    if sha256_bytes(data) != decision_sha256:
        raise QueueOperationError("semantic_decision_conflict")
    return decision_path, decision_sha256


def _existing_capture_publication_state(
    row: sqlite3.Row | None,
    *,
    pending_path: str,
    ready_path: str,
    intent_sha256: str,
    byte_size: int,
) -> str:
    if row is None:
        raise QueueOperationError("capture_intent_conflict")
    state = str(row["publication_state"])
    expected_paths = {"pending": pending_path, "ready": ready_path}
    matches = (
        expected_paths.get(state) == row["relative_path"]
        and row["intent_sha256"] == intent_sha256
        and row["byte_size"] == byte_size
    )
    if not matches:
        raise QueueOperationError("capture_intent_conflict")
    return state


def _require_capture_replay_task(
    row: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    dedupe_key: str,
) -> None:
    stored = bytes(row["payload_blob"])
    validation = validate_payload_blob(stored, str(row["input_hash"]), parse=True)
    actual = (
        validation.code,
        row["kind"],
        row["handler_version"],
        row["input_hash"],
        row["dedupe_key"],
        stored,
    )
    expected = (None, kind, handler_version, sha256_bytes(payload_bytes), dedupe_key, payload_bytes)
    if actual != expected:
        raise QueueOperationError("capture_replay_conflict")


@dataclass(frozen=True)
class TaskFence:
    task_id: str
    mode: Literal["worker", "queue-operator"]
    token: str
    epoch: int
    owner: OwnerLease
    expires_at: datetime


# How many dead captures one nightly pass redrives: each becomes a model call.
DEAD_CAPTURE_REDRIVES_PER_PASS = 50


@dataclass(frozen=True)
class _CapturePurgeArtifact:
    source_path: str
    archive_path: str
    sha256: str


@dataclass(frozen=True)
class _CapturePurgeEvidence:
    task_id: str
    intent_id: str
    intent: _CapturePurgeArtifact
    decisions: tuple[_CapturePurgeArtifact, ...]
    terminal_path: str
    sources: tuple[_CapturePurgeArtifact, ...] = ()


@dataclass(frozen=True)
class _OrdinaryPurgePlan:
    cutoff: str
    export: Path
    task_ids: tuple[str, ...]
    records: tuple[dict[str, object], ...]
    records_bytes: bytes
    capture_evidence: tuple[_CapturePurgeEvidence, ...]
    manifest_bytes: bytes | None
    retained: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validated_payload_bytes(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    validation = validate_payload_blob(payload_bytes, input_hash, parse=True)
    if validation.code is not None:
        raise ValueError(validation.code)
    return payload_bytes, input_hash


def _same_enqueued_task(
    existing: sqlite3.Row,
    existing_bytes: bytes,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> bool:
    if existing["kind"] != kind or existing["handler_version"] != handler_version:
        return False
    if existing["input_hash"] != input_hash:
        return False
    return existing_bytes == payload_bytes


def _v3_dedupe_match(
    existing: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> str:
    """The existing id when it is the same task; a dedupe conflict otherwise."""
    existing_bytes = bytes(existing["payload_blob"])
    current = validate_payload_blob(
        existing_bytes, existing["input_hash"], parse=True
    )
    if current.code is None and _same_enqueued_task(
        existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
    ):
        return str(existing["id"])
    raise QueueOperationError("dedupe_conflict")


def _unfinished_compile_request(database, payload_bytes, input_hash):
    existing = database.execute(
        """SELECT id, kind, handler_version, payload_blob, input_hash FROM tasks
           WHERE kind='compile' AND handler_version=1
             AND state IN ('ready','leased','blocked','dead')
             AND (input_hash=? OR payload_blob=?)
             AND NOT EXISTS (SELECT 1 FROM tasks AS child WHERE child.redrive_of=tasks.id)
           ORDER BY created_at, id LIMIT 1""", (input_hash, payload_bytes)).fetchone()
    if existing is None:
        return None
    return _v3_dedupe_match(existing, 'compile', 1, payload_bytes, input_hash)


def _legacy_dedupe_match(
    existing: sqlite3.Row,
    kind: str,
    handler_version: int,
    payload_bytes: bytes,
    input_hash: str,
) -> str:
    """The existing id when it is the same task; a dedupe conflict otherwise."""
    existing_bytes = _legacy_payload_bytes(existing)
    validation = validate_payload_blob(
        existing_bytes, existing["input_hash"], parse=True
    )
    if validation.code is None and _same_enqueued_task(
        existing, existing_bytes, kind, handler_version, payload_bytes, input_hash
    ):
        return str(existing["id"])
    raise QueueOperationError("dedupe_conflict")


def _legacy_payload_bytes(row: sqlite3.Row) -> bytes:
    try:
        return str(row["payload_json"]).encode("utf-8", errors="strict")
    except UnicodeError:
        return b""


def _capture_link_record(
    task_id: str, intent_id: str, intent_sha256: str, handler_version: int
) -> dict[str, object]:
    return {
        "schema_version": "capture-task-link/v1",
        "task_id": task_id,
        "intent_id": intent_id,
        "intent_sha256": intent_sha256,
        "handler_version": handler_version,
    }


def _matching_capture_intent(
    intent: sqlite3.Row | None, intent_path: str, intent_sha256: str
) -> bool:
    return (
        intent is not None
        and intent["relative_path"] == intent_path
        and intent["intent_sha256"] == intent_sha256
        and intent["publication_state"] == "ready"
    )


def _validated_capture_payload(payload: Mapping[str, object]) -> tuple[bytes, str]:
    payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
    input_hash = sha256_bytes(payload_bytes)
    if validate_payload_blob(payload_bytes, input_hash, parse=True).code is not None:
        raise ValueError("payload_hash_mismatch")
    return payload_bytes, input_hash


def _purge_states(include_dead: bool) -> tuple[str, ...]:
    if include_dead:
        return ("succeeded", "cancelled", "dead")
    return ("succeeded", "cancelled")


def _create_export_parent(parent: Path) -> None:
    if not parent.exists():
        parent.mkdir(parents=True)
        _harden_owner_only(parent, 0o700)


def _require_export_parent(parent: Path) -> None:
    _create_export_parent(parent)
    if parent.is_symlink() or not parent.is_dir() or not _is_owner_only(parent):
        raise QueueOperationError("export_parent_permissions_invalid")


def _purge_selection_changed(
    current: list, task_ids: tuple[str, ...], states: tuple[str, ...], cutoff_stamp: str
) -> bool:
    if len(current) != len(task_ids):
        return True
    return any(
        row["state"] not in states or row["updated_at"] >= cutoff_stamp
        for row in current
    )


def _require_semantic_decision_fences(
    task_id: str,
    intent_id: str,
    stage: str,
    decision_sha256: str,
    task_fence: object,
    intent_fence: object,
    owner: object,
    fence_type: type,
    owner_type: type,
) -> None:
    _require_semantic_decision_shape(stage, decision_sha256)
    if not isinstance(task_fence, TaskFence) or task_fence.task_id != task_id:
        raise ValueError("task fence does not match")
    _require_worker_intent_fence(intent_fence, intent_id, fence_type)
    _require_matching_decision_owner(owner, task_fence, intent_fence, owner_type)


# The stored columns a semantic decision has to agree with, in argument order.
_SEMANTIC_DECISION_FIELDS = (
    "seal_digest",
    "decision_path",
    "decision_sha256",
    "active_link_digest",
)


def _semantic_decision_matches(
    existing: sqlite3.Row,
    intent_id: str,
    stage: str,
    seal_digest: str,
    decision_path: str,
    decision_sha256: str,
    active_link_digest: str,
) -> bool:
    if existing["consumer_kind"] != "semantic-decision":
        return False
    if existing["consumer_id"] != f"{intent_id}:{stage}":
        return False
    expected = (seal_digest, decision_path, decision_sha256, active_link_digest)
    return tuple(existing[field] for field in _SEMANTIC_DECISION_FIELDS) == expected


def _purge_progress(
    task_id: str,
    operation_id: str,
    pages_written: int,
    links_deleted: int,
    *,
    state: str,
    code: str | None = None,
) -> CorruptPurgeProgress:
    return CorruptPurgeProgress(
        task_id=task_id,
        operation_id=operation_id,
        state=state,
        pages_written=pages_written,
        links_deleted=links_deleted,
        complete=False,
        code=code,
    )


def _blocked_or_pending(blocker_code: str | None) -> str:
    if blocker_code is None:
        return "purge_pending"
    return "blocked"


# Receipt field, and the disposition column it has to agree with.
_RECEIPT_DISPOSITION_FIELDS = (
    ("package_key", "disposition_key"),
    ("manifest_sha256", "manifest_sha256"),
    ("disposition_sha256", "disposition_sha256"),
    ("original_frozen_root", "original_frozen_root"),
)


def _receipt_matches_disposition(
    receipt: dict,
    disposition: sqlite3.Row,
    operation: sqlite3.Row,
    operation_id: str,
    task_id: str,
) -> bool:
    if (receipt["operation_id"], receipt["task_id"]) != (operation_id, task_id):
        return False
    if any(
        receipt[field] != disposition[column]
        for field, column in _RECEIPT_DISPOSITION_FIELDS
    ):
        return False
    return _receipt_matches_operation(receipt, operation)


def _receipt_matches_operation(receipt: dict, operation: sqlite3.Row) -> bool:
    if receipt["purge_page_count"] != operation["page_count"]:
        return False
    if receipt["final_rolling_root"] != operation["rolling_root"]:
        return False
    return receipt["final_generation"] == operation["expected_generation"]


def _parent_state_ready_for_delete(
    operation: sqlite3.Row, task: sqlite3.Row
) -> bool:
    if operation["state"] != "receipt-published":
        return False
    if task["state"] != "purge_pending":
        return False
    return int(task["lineage_generation"]) == int(operation["expected_generation"])


def _exported_bytes_match(
    raw: bytes,
    history: bytes,
    current_history: bytes,
    task: sqlite3.Row,
    disposition: sqlite3.Row,
) -> bool:
    return (
        sha256_bytes(raw) == disposition["raw_sha256"]
        and sha256_bytes(bytes(task["payload_blob"])) == disposition["raw_sha256"]
        and sha256_bytes(history) == disposition["history_sha256"]
        and sha256_bytes(current_history) == disposition["history_sha256"]
    )


def _require_matching_purge_operation(
    existing_operation: sqlite3.Row, task: sqlite3.Row, operation_id: str
) -> None:
    if existing_operation["operation_id"] != operation_id:
        raise QueueOperationError("corrupt_purge_conflict")
    if task["state"] != "purge_pending":
        raise QueueOperationError("corrupt_purge_conflict")


def _blocked_purge(task_id: str, code: str) -> CorruptPurgeProgress:
    return _purge_progress(task_id, "", 0, 0, state="blocked", code=code)


def _corrupt_child_leaf_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child something else was redriven from is not a leaf."""
    row = database.execute(
        "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_not_leaf" if row is not None else None


def _corrupt_child_state_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child still running, or one whose kind is kept forever."""
    if child["state"] not in _TERMINAL_STATES:
        return "corrupt_child_nonterminal"
    if child["state"] == "dead" and DEFAULTS.dead_task_retention_days is None:
        return "corrupt_child_retained"
    return None


def _child_retention_days(child: sqlite3.Row) -> int | None:
    if child["state"] == "dead":
        return DEFAULTS.dead_task_retention_days
    return DEFAULTS.queue_result_retention_days


def _corrupt_child_retention_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose retention window has not run out yet."""
    updated_at = _parse_timestamp(str(child["updated_at"]))
    retention_days = _child_retention_days(child)
    if updated_at is None or retention_days is None:
        return "corrupt_child_retention_active"
    if updated_at + timedelta(days=retention_days) > now:
        return "corrupt_child_retention_active"
    return None


def _corrupt_child_fence_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=? LIMIT 1", (str(child["id"]),)
    ).fetchone()
    return "corrupt_child_fenced" if row is not None else None


def _active_binding_or_block(
    queue: MemoryQueue, database: sqlite3.Connection, child_id: str
) -> tuple[object | None, str | None]:
    """The child's active capture binding, or why it cannot be read."""
    link_exists = (
        database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (child_id,)
        ).fetchone()
        is not None
    )
    try:
        return queue.active_capture_binding(database, child_id), None
    except QueueOperationError:
        if link_exists:
            return None, "corrupt_child_intent_unresolved"
        return None, None


def _corrupt_child_intent_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    """A child whose capture intent has no terminal record yet."""
    child_id = str(child["id"])
    binding, blocker = _active_binding_or_block(queue, database, child_id)
    if blocker is not None:
        return blocker
    return _unresolved_child_intent(queue, database, child_id, binding)


def _unresolved_child_intent(
    queue: MemoryQueue, database: sqlite3.Connection, child_id: str, binding
) -> str | None:
    """Whether this child's capture intent is still waiting for a terminal record."""
    if binding is None or binding.intent_id is None:
        return None
    if queue._capture_terminal_blocker(database, child_id, binding) is None:
        return None
    return "corrupt_child_intent_unresolved"


def _corrupt_child_decision_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    row = database.execute(
        """SELECT 1 FROM capture_task_link_seals
           WHERE task_id=? AND consumer_kind='semantic-decision' LIMIT 1""",
        (str(child["id"]),),
    ).fetchone()
    return "corrupt_child_decision_retained" if row is not None else None


def _corrupt_child_result_blocker(
    queue: MemoryQueue, database: sqlite3.Connection, child: sqlite3.Row, now: datetime
) -> str | None:
    fields = ("result_reference", "result_sha256", "result_operation_id")
    if any(child[field] is not None for field in fields):
        return "corrupt_child_result_retained"
    return None


_CORRUPT_CHILD_CHECKS = (
    _corrupt_child_leaf_blocker,
    _corrupt_child_state_blocker,
    _corrupt_child_retention_blocker,
    _corrupt_child_fence_blocker,
    _corrupt_child_intent_blocker,
    _corrupt_child_decision_blocker,
    _corrupt_child_result_blocker,
)


def _owner_only_directory(package: Path) -> bool:
    """A real, owner-only directory — not a link, a reparse point, or a file."""
    metadata = package.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return (
        not stat.S_ISLNK(metadata.st_mode)
        and not (getattr(metadata, "st_file_attributes", 0) & reparse_flag)
        and stat.S_ISDIR(metadata.st_mode)
        and _is_owner_only(package)
    )


def _validated_corrupt_records(
    package: Path,
) -> tuple[bytes, bytes, dict[str, object], dict[str, object]]:
    """The manifest and disposition bytes, and the objects they parse into."""
    schemas = Path(__file__).with_name("schemas")
    manifest_bytes = _read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = _read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    record = json.loads(disposition_bytes.decode("utf-8", errors="strict"))
    validate_schema(manifest, schemas / "corrupt-task-manifest-v1.json")
    validate_schema(record, schemas / "corrupt-task-disposition-v1.json")
    return manifest_bytes, disposition_bytes, manifest, record


def _corrupt_package_trustworthy(package: Path, disposition: sqlite3.Row) -> bool:
    """Whether the exported package still proves what the disposition recorded."""
    if not _owner_only_directory(package):
        return False
    records = _validated_corrupt_records(package)
    return _corrupt_records_match(
        *records, disposition
    ) and _corrupt_payload_matches(package, records[2], disposition)


def _corrupt_payload_matches(
    package: Path, manifest: Mapping[str, object], disposition: sqlite3.Row
) -> bool:
    """The exported bytes still hash to what the manifest recorded."""
    raw = _read_stable_owner_file(package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES)
    history = _read_stable_owner_file(
        package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
    )
    metadata = _read_stable_owner_file(
        package / "task-metadata.json", _MAX_EXPORT_METADATA_BYTES
    )
    digests = (sha256_bytes(raw), sha256_bytes(history), sha256_bytes(metadata))
    expected = (
        manifest["raw_sha256"],
        manifest["history_sha256"],
        manifest["metadata_sha256"],
    )
    if digests != expected:
        return False
    return manifest["rolling_root"] == disposition["original_frozen_root"]


def _source_fence_row_matches(row: sqlite3.Row, fence: SourceFence) -> bool:
    """The stored fence names the same source, token and owning process."""
    stored = (
        str(row["daily_id"]),
        str(row["source_digest"]),
        str(row["token"]),
        int(row["owner_pid"]),
        str(row["acquired_at"]),
    )
    expected = (
        fence.daily_id,
        fence.source_digest,
        fence.token,
        os.getpid(),
        _timestamp(fence.acquired_at),
    )
    return stored == expected and fence.owner_pid == os.getpid()


_CaptureLeaf = tuple[str, str | None, str | None, int]


def _superseded_digests(resolutions: Sequence[sqlite3.Row]) -> set[str]:
    """Every digest a later resolution replaced."""
    return {
        str(row["supersedes_digest"])
        for row in resolutions
        if row["supersedes_digest"] is not None
    }


def _resolution_leaf(
    database: sqlite3.Connection, link: sqlite3.Row, resolution: sqlite3.Row
) -> _CaptureLeaf:
    """One unsuperseded resolution, with the intent it selected if it named one."""
    digest = str(resolution["resolution_digest"])
    selected = resolution["selected_intent_id"]
    if selected is None:
        return digest, None, None, int(link["handler_version"])
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected,)
    ).fetchone()
    if intent is None:
        raise QueueOperationError("capture_link_conflicted")
    return (
        digest,
        str(intent["intent_id"]),
        str(intent["intent_sha256"]),
        int(link["handler_version"]),
    )


def _capture_link_leaves(
    database: sqlite3.Connection,
    link: sqlite3.Row,
    resolutions: Sequence[sqlite3.Row],
) -> list[_CaptureLeaf]:
    """Everything in this link's history that nothing else superseded."""
    superseded = _superseded_digests(resolutions)
    leaves: list[_CaptureLeaf] = []
    if str(link["link_digest"]) not in superseded:
        leaves.append(
            (
                str(link["link_digest"]),
                str(link["intent_id"]),
                str(link["intent_sha256"]),
                int(link["handler_version"]),
            )
        )
    for resolution in resolutions:
        if str(resolution["resolution_digest"]) in superseded:
            continue
        leaves.append(_resolution_leaf(database, link, resolution))
    return leaves


def _capture_binding_of(
    database: sqlite3.Connection, task_id: str
) -> CaptureTaskBinding:
    """The one active binding this task's link history leaves behind."""
    link = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
    ).fetchone()
    if link is None:
        raise QueueOperationError("capture_link_conflicted")
    resolutions = database.execute(
        """SELECT * FROM capture_task_link_resolutions
           WHERE task_id=? ORDER BY created_at,resolution_digest""",
        (task_id,),
    ).fetchall()
    leaves = _capture_link_leaves(database, link, resolutions)
    if len(leaves) != 1:
        raise QueueOperationError("capture_link_conflicted")
    active_digest, intent_id, intent_sha256, handler_version = leaves[0]
    seal = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    return CaptureTaskBinding(
        task_id=task_id,
        intent_id=intent_id,
        intent_sha256=intent_sha256,
        handler_version=handler_version,
        active_digest=active_digest,
        seal_digest=None if seal is None else str(seal["seal_digest"]),
    )


def _verify_corrupt_package_bytes(
    package: Path,
    *,
    raw: bytes,
    history: bytes,
    metadata: bytes,
    disposition_bytes: bytes,
) -> None:
    """Every file in the package still holds exactly what we wrote."""
    observed = (
        _read_stable_owner_file(package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES),
        _read_stable_owner_file(
            package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
        ),
        _read_stable_owner_file(
            package / "task-metadata.json", _MAX_EXPORT_METADATA_BYTES
        ),
        _read_stable_owner_file(package / "disposition.json", 64 * 1024),
    )
    if observed != (raw, history, metadata, disposition_bytes):
        raise QueueOperationError("corrupt_export_verification_failed")


def _require_disposable_rows(
    current_task: sqlite3.Row, current_operation: sqlite3.Row, incoming_count: int
) -> None:
    """The task and its operation are in the exact states disposal needs."""
    observed = (
        current_task["state"],
        current_operation["state"],
        current_operation["lineage_generation"],
        current_operation["link_count"],
    )
    expected = (
        "quarantine_pending",
        "manifested",
        current_task["lineage_generation"],
        incoming_count,
    )
    if observed != expected:
        raise QueueOperationError("corrupt_export_verification_failed")


def _blocked_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="blocked",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=False,
        code="capture_link_conflicted",
    )


def _quarantined_corrupt_progress(
    operation: sqlite3.Row, *, task_id: str, operation_id: str
) -> CorruptExportProgress:
    return CorruptExportProgress(
        task_id=task_id,
        operation_id=operation_id,
        state="quarantined",
        pages_written=int(operation["page_count"]),
        links_exported=int(operation["link_count"]),
        complete=True,
        code=None,
    )


def _insert_link_resolution(
    database: sqlite3.Connection,
    *,
    resolution_digest: str,
    task_id: str,
    supersedes_digest: str | None,
    observed_json: bytes,
    selected_id: str | None,
    actor: str,
    reason: str,
    now: datetime,
) -> None:
    inserted = database.execute(
        """INSERT INTO capture_task_link_resolutions(
               resolution_digest,task_id,supersedes_digest,observed_json,
               selected_intent_id,actor_identity,reason,created_at
           ) VALUES (?,?,?,?,?,?,?,?)""",
        (
            resolution_digest,
            task_id,
            supersedes_digest,
            observed_json,
            selected_id,
            actor,
            reason,
            _timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_resolution_failed")


def _require_repair_projection(
    database: sqlite3.Connection, owner: OwnerLease, now: datetime
) -> None:
    """The queue still projects this repair owner at this exact epoch."""
    projection = database.execute(
        """SELECT 1 FROM queue_ownership WHERE owner_token=?
           AND fencing_epoch=? AND canonical_role='repair' AND expires_at>?""",
        (owner.token, owner.epoch, _timestamp(now)),
    ).fetchone()
    if projection is None:
        raise QueueOperationError("queue_owner_fence_lost")


def _require_unfenced_unsealed_task(
    database: sqlite3.Connection, task_id: str
) -> None:
    """A fenced or sealed task's links may not be resolved."""
    if database.execute(
        "SELECT 1 FROM task_fences WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("task_fenced")
    if database.execute(
        "SELECT 1 FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone() is not None:
        raise QueueOperationError("capture_link_sealed")


def _selected_intent_id(
    database: sqlite3.Connection, selected_intent: Mapping[str, object] | None
) -> str | None:
    """The intent this resolution selects, when it names one that matches."""
    if selected_intent is None:
        return None
    selected_id = selected_intent["intent_id"]
    intent = database.execute(
        "SELECT * FROM capture_intents WHERE intent_id=?", (selected_id,)
    ).fetchone()
    if intent is None or intent["intent_sha256"] != selected_intent["intent_sha256"]:
        raise QueueOperationError("capture_link_conflicted")
    return selected_id


def _read_purge_receipt(package: Path) -> dict[str, object]:
    """The schema-valid purge receipt the package holds."""
    try:
        receipt_bytes = _read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt,
            Path(__file__).with_name("schemas") / "corrupt-purge-v1.json",
        )
    except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
        raise QueueOperationError("corrupt_purge_completion_invalid") from exc
    return receipt


def _require_matching_purge_receipt(
    receipt: Mapping[str, object],
    operation: sqlite3.Row,
    disposition: sqlite3.Row,
    task_id: str,
) -> None:
    """The receipt has to name exactly the purge the rows describe."""
    observed = (
        receipt["operation_id"],
        receipt["task_id"],
        receipt["package_key"],
        receipt["manifest_sha256"],
        receipt["disposition_sha256"],
        receipt["purge_page_count"],
        receipt["final_rolling_root"],
        receipt["final_generation"],
    )
    expected = (
        operation["operation_id"],
        task_id,
        disposition["disposition_key"],
        disposition["manifest_sha256"],
        disposition["disposition_sha256"],
        operation["page_count"],
        operation["rolling_root"],
        operation["expected_generation"],
    )
    if observed != expected:
        raise QueueOperationError("corrupt_purge_completion_invalid")


def _require_package_digests(package: Path, disposition: sqlite3.Row) -> None:
    """The manifest and disposition still hash to what the row recorded."""
    manifest_bytes = _read_stable_owner_file(package / "manifest.json", 64 * 1024)
    disposition_bytes = _read_stable_owner_file(
        package / "disposition.json", 64 * 1024
    )
    observed = (sha256_bytes(manifest_bytes), sha256_bytes(disposition_bytes))
    expected = (disposition["manifest_sha256"], disposition["disposition_sha256"])
    if observed != expected:
        raise QueueOperationError("corrupt_package_invalid")


def _write_purge_receipt(package: Path, receipt: Mapping[str, object]) -> bytes:
    """Write the receipt and read it back to prove it landed."""
    receipt_bytes = canonical_json_bytes(receipt)
    if len(receipt_bytes) > 64 * 1024:
        raise QueueOperationError("corrupt_purge_receipt_too_large")
    _write_durable_file(package / "purge-receipt.json", receipt_bytes)
    written = _read_stable_owner_file(package / "purge-receipt.json", 64 * 1024)
    if written != receipt_bytes:
        raise QueueOperationError("corrupt_purge_receipt_invalid")
    return receipt_bytes


def _record_capture_seal(
    database: sqlite3.Connection,
    *,
    task_id: str,
    consumer_kind: str,
    consumer_id: str,
    active_link_digest: str,
    seal_digest: str,
    now: datetime,
) -> None:
    """Seal the binding, accepting a repeat of the identical seal."""
    existing = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    if existing is not None:
        _require_identical_seal(
            existing,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
        )
        return
    inserted = database.execute(
        """INSERT INTO capture_task_link_seals(
               task_id,active_digest,consumer_kind,consumer_id,
               seal_digest,sealed_at
           ) VALUES (?,?,?,?,?,?)""",
        (
            task_id,
            active_link_digest,
            consumer_kind,
            consumer_id,
            seal_digest,
            _timestamp(now),
        ),
    ).rowcount
    if inserted != 1:
        raise QueueOperationError("capture_link_seal_failed")


def _descriptor_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _read_stable_result(descriptor: int) -> bytes | None:
    """The file's bytes, when it neither grew nor moved while we read it."""
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_size > _MAX_RESULT_BYTES:
        return None
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        data = handle.read(_MAX_RESULT_BYTES + 1)
    if len(data) > _MAX_RESULT_BYTES or _descriptor_identity(
        opened
    ) != _descriptor_identity(os.fstat(descriptor)):
        return None
    return data


def _stable_result_digest(path: Path) -> str | None:
    """The digest of a result file read without following a link or a swap."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        data = _read_stable_result(descriptor)
    finally:
        os.close(descriptor)
    return None if data is None else sha256_bytes(data)


def _validated_listed_states(states: tuple[str, ...] | None) -> tuple[str, ...]:
    """The states to list; every one of them has to be a real queue state."""
    listed = states or _STATES
    if not listed or any(state not in _STATES for state in listed):
        raise ValueError("invalid queue state")
    return listed


def _age_filter_clause(
    max_age_days: int | None, parameters: list[object], now: datetime
) -> str:
    """The age filter, appending its parameter when there is one."""
    if max_age_days is None:
        return ""
    parameters.append(_timestamp(now - timedelta(days=max_age_days)))
    return " AND created_at >= ?"


def _v3_task_record(
    row: sqlite3.Row, history: Sequence[sqlite3.Row]
) -> QueueTask:
    """One adopted-queue task record; v3 keeps the payload in a blob column."""
    return _task_record(row, history, json.loads(bytes(row["payload_blob"])))


def _refuse_legacy_only_api(name: str) -> NoReturn:
    """Refuse by name a legacy behaviour the adopted queue does not implement."""
    raise QueueOperationError(
        "queue_api_not_adopted",
        f"{name} has no implementation on the adopted V3 queue; it exists only on "
        "the pre-adoption queue, so it is refused here rather than ignored",
    )


def _require_adopted_retry_policy(
    max_attempts: int, retry_base_seconds: int, retry_cap_seconds: int
) -> None:
    """The adopted queue settles retries by its own contract, not by caller policy."""
    offered = (max_attempts, retry_base_seconds, retry_cap_seconds)
    settled = (
        DEFAULTS.queue_max_attempts,
        DEFAULTS.retry_base_seconds,
        DEFAULTS.retry_cap_seconds,
    )
    if offered != settled:
        _refuse_legacy_only_api("fail(retry_policy_override)")


_ORDINARY_PURGEABLE_STATES = frozenset({"succeeded", "cancelled", "dead"})


def _ordinary_purge_selection(include_dead: bool) -> str:
    """Which finished rows an ordinary purge takes; one query for both readers.

    Attempts-exhausted work is retained by default and leaves only when asked
    for with `--include-dead`, which the weekly pass does after 30 days; a dead
    capture whose capture has no terminal record stays even then
    (`_rows_with_releasable_evidence`). A row demoted to
    `dead / payload_hash_mismatch` is never taken here whatever the caller asks:
    its payload no longer hashes to its record, so it cannot be exported, and
    `quarantine-corrupt` is the route the product has for it. See
    `docs/research/2026-09-17-the-adopted-queue-settles-its-own-attempts-and-can-let-the-dead-go.md`.
    """
    if not include_dead:
        return "SELECT * FROM tasks WHERE state IN ('succeeded','cancelled') AND updated_at<?"
    return (
        "SELECT * FROM tasks WHERE (state IN ('succeeded','cancelled') "
        "OR (state='dead' AND error_code IS NOT 'payload_hash_mismatch')) "
        "AND updated_at<?"
    )


def _require_adopted_attempt_limit(max_attempts: int) -> None:
    """An attempt limit belongs to the queue, so a caller's own number is refused.

    `claim` used to take the caller's number while `fail` refused it and the
    terminal test settled by the contract's: one option with two answers, and a
    worker that could claim a task it could not then fail. See
    `docs/research/2026-09-17-the-adopted-queue-settles-its-own-attempts-and-can-let-the-dead-go.md`.
    """
    if max_attempts != DEFAULTS.queue_max_attempts:
        _refuse_legacy_only_api("claim(max_attempts_override)")


def _delete_ordinary_purge_authorizations(
    database: sqlite3.Connection, task_ids: Sequence[str], placeholders: str
) -> None:
    deleted = database.execute(
        f"""DELETE FROM task_purge_authorizations
            WHERE task_id IN ({placeholders})""",  # noqa: S608
        task_ids,
    ).rowcount
    if deleted != len(task_ids):
        raise QueueOperationError("purge_authorization_failed")


def _published_purge_operation(
    database: sqlite3.Connection, task_id: str
) -> sqlite3.Row | None:
    """The purge operation of a task that is gone; None while the task is still here."""
    task_exists = database.execute(
        "SELECT 1 FROM tasks WHERE id=?", (task_id,)
    ).fetchone()
    operation = database.execute(
        "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if task_exists is not None or operation is None:
        return None
    if operation["state"] != "receipt-published":
        raise QueueOperationError("corrupt_purge_completion_invalid")
    return operation


def _require_semantic_seal_matches(existing: sqlite3.Row, *expected: str) -> None:
    """An existing seal is idempotent only for the same decision."""
    if not _semantic_decision_matches(existing, *expected):
        raise QueueOperationError("semantic_decision_conflict")


def _require_exported_results(
    results_export: Path, result_manifest: list[dict[str, str]]
) -> None:
    """Every exported result reads back with the digest its manifest names."""
    for item in result_manifest:
        exported = results_export / f"{item['id']}.result"
        data = _read_stable_owner_file(exported, _MAX_RESULT_BYTES)
        if sha256_bytes(data) != item["sha256"]:
            raise QueueOperationError("export_verification_failed")


def _check_claim_arguments(
    owner: str, lease_seconds: int, max_attempts: int
) -> None:
    _require_claim_owner_and_lease(owner, lease_seconds)
    _validate_retry_policy(
        max_attempts, DEFAULTS.retry_base_seconds, DEFAULTS.retry_cap_seconds
    )
    _require_adopted_attempt_limit(max_attempts)


# What the legacy queue's claim may take, and therefore what its count names:
# one predicate, as `_V3_WORK_CLAIMABLE` is for the adopted queue. Parameters:
# max_attempts, now.
_V2_WORK_CLAIMABLE = """state = 'ready' AND attempts < ? AND available_at <= ?
    AND NOT EXISTS (
        SELECT 1 FROM source_fences f
        WHERE payload_references_source(tasks.payload_json, f.daily_id, f.source_digest)
    )"""


def _register_source_reference(connection: sqlite3.Connection) -> None:
    """Give this connection the queue's one source-reference rule as SQL.

    The claim and the eligible count select with SQL, and they matched a fence by
    `instr` over the payload text: a task that merely carried a timestamp of the
    fenced day was skipped and not counted, while the enqueue and the fence check
    already read identity fields only. They now call the same rule the enqueue
    does. See `docs/research/2026-09-28-a-check-names-its-cause.md`.
    """
    connection.create_function(
        "payload_references_source", 3, _stored_payload_references_source, deterministic=True
    )


def _stored_payload_references_source(
    payload: object, daily_id: object, source_digest: object
) -> bool:
    """`MemoryQueue._payload_references_source` for a stored payload and one fence."""
    return MemoryQueue._payload_references_source(
        _stored_payload_text(payload), str(daily_id), str(source_digest)
    )


def _stored_payload_text(payload: object) -> str:
    """A payload column as text; undecodable blob bytes are replaced (`_task_payload_text`)."""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", errors="replace")
    return str(payload)


def _next_unfenced_ready_task(
    connection: sqlite3.Connection, now: datetime, attempt_limit: int
) -> sqlite3.Row | None:
    """The next ready task whose source nobody is finalizing."""
    _register_source_reference(connection)
    return connection.execute(
        f"""SELECT * FROM tasks WHERE {_V2_WORK_CLAIMABLE}
           ORDER BY priority DESC, available_at, created_at, rowid LIMIT 1""",
        (attempt_limit, _timestamp(now)),
    ).fetchone()


def _read_exported_rows(
    database: sqlite3.Connection, task_id: str
) -> _ExportedRows:
    """The history, links and seal an export carries beside the task row."""
    history = database.execute(
        """SELECT attempt,started_at,finished_at,outcome,error_code
           FROM attempt_history WHERE task_id=? ORDER BY sequence""",
        (task_id,),
    ).fetchall()
    source_links = database.execute(
        """SELECT logical_path,source_digest FROM task_source_links
           WHERE task_id=? ORDER BY logical_path,source_digest""",
        (task_id,),
    ).fetchall()
    link = database.execute(
        "SELECT * FROM capture_task_links WHERE task_id=?", (task_id,)
    ).fetchone()
    seal = database.execute(
        "SELECT * FROM capture_task_link_seals WHERE task_id=?", (task_id,)
    ).fetchone()
    return _ExportedRows(history, source_links, link, seal)


# The attempt outcome, task state, and error code for a lease that expired after
# its result was published, keyed by whether that result was still intact.
_EXPIRED_PUBLISHED_OUTCOMES = {
    True: ("succeeded", "succeeded", None),
    False: ("failed", "dead", "result_corrupt"),
}

# The task state and error code for an expired lease, keyed by whether the task
# has now used up every attempt it is allowed.
_EXPIRED_LEASE_OUTCOMES = {
    True: ("dead", "attempts_exhausted"),
    False: ("ready", "lease_expired"),
}

# Which owner roles may project each queue role.
_QUEUE_OWNER_PARENT_ROLES = {
    "queue-worker": frozenset(
        {"queue-worker", "compile", "doctor", "nightly", "weekly"}
    ),
    "queue-operator": frozenset({"queue-operator", "repair"}),
}


def _reclaim_dead_queue_projections(
    database: sqlite3.Connection, registry: Any, lease: OwnerLease
) -> None:
    """Take over the projections a dead owner left in this database.

    The projection lives here, not in the coordinator, so no reclaim on that
    side can ever reach it: it needs the same consultation, on the same proof
    — expired lease plus a process the OS says is gone — which is why the
    answer comes from the registry rather than from a second rule written
    here. A live or unprovable owner still refuses by name. The registry is
    resolved by the caller, before this database's write transaction opens, so
    the two databases are never locked in the reverse of the usual order.
    """
    rows = database.execute(
        """SELECT * FROM queue_ownership
           WHERE actor_id=? OR owner_token=?
              OR (canonical_role=? AND canonical_scope=?)""",
        (lease.actor_id, lease.token, lease.role, lease.scope),
    ).fetchall()
    for row in rows:
        _require_dead_queue_owner(registry, row)
        _delete_queue_projection_row(database, row)


def _require_dead_queue_owner(registry: Any, row: sqlite3.Row) -> None:
    """A projection is reclaimable exactly when its canonical owner is."""
    if registry.reclaimable_dead_owner(row):
        return
    raise QueueOperationError(
        "queue_owner_busy",
        f"{row['canonical_role']} {row['canonical_scope']} is already projected",
    )


# A task fence exists only while its canonical owner does, and it names that
# owner by the same tuple, so it is reclaimed by the same proof, in the same
# transaction. Without this the ownership reclaim succeeds and the very next
# step still refuses `task_fenced` for ever.
_DEAD_OWNER_TASK_FENCES = """DELETE FROM task_fences
   WHERE canonical_role=? AND canonical_scope=? AND canonical_actor_id=?
     AND canonical_owner_token=? AND canonical_fencing_epoch=?"""


def _delete_queue_projection_row(
    database: sqlite3.Connection, row: sqlite3.Row
) -> None:
    database.execute(
        _DEAD_OWNER_TASK_FENCES,
        (
            row["canonical_role"],
            row["canonical_scope"],
            row["actor_id"],
            row["owner_token"],
            row["fencing_epoch"],
        ),
    )
    deleted = database.execute(
        """DELETE FROM queue_ownership
           WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
             AND owner_token=? AND fencing_epoch=? AND process_id=?
             AND process_start_identity=?""",
        (
            row["actor_id"],
            row["canonical_role"],
            row["canonical_scope"],
            row["owner_token"],
            row["fencing_epoch"],
            row["process_id"],
            row["process_start_identity"],
        ),
    ).rowcount
    if deleted != 1:
        raise QueueOperationError("queue_owner_fence_lost")


def _require_projectable_parent(registry: Any, parent: object, role: str) -> None:
    """Refuse a parent lease that cannot project the requested queue role."""
    from operational_ownership import OwnerLease

    if not isinstance(parent, OwnerLease):
        raise TypeError("parent must be an OwnerLease")
    if parent.role not in _QUEUE_OWNER_PARENT_ROLES[role]:
        raise ValueError("parent role cannot project the requested queue role")
    with closing(registry._connect()) as database:
        registry.require(database, parent)


# The queue tables whose rows keep the run directory alive.
_RETAINING_QUEUE_QUERIES = (
    "SELECT 1 FROM tasks LIMIT 1",
    "SELECT 1 FROM source_fences LIMIT 1",
    "SELECT 1 FROM source_failures LIMIT 1",
)


def _directory_holds_anything(path: Path) -> bool:
    """Whether the directory has an entry; one that cannot be read counts as held."""
    try:
        return path.exists() and any(path.iterdir())
    except OSError:
        return True


def _insert_source_fence(
    connection: sqlite3.Connection, values: tuple[object, ...]
) -> None:
    """Record the fence, refusing when another one already holds the source."""
    try:
        connection.execute(
            "INSERT INTO source_fences "
            "(daily_id, source_digest, token, owner_pid, acquired_at, "
            "heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise QueueOperationError("source_fenced") from exc


def _daily_logical_path(daily_id: str) -> str:
    """The vault-relative path a canonical daily id names."""
    return f"knowledge/daily/{daily_id}.md"


def _adopted_fence_row_matches(row: sqlite3.Row, fence: SourceFence) -> bool:
    """The stored adopted fence names the same source, token and owning process."""
    stored = (
        str(row["logical_path"]),
        str(row["source_digest"]),
        str(row["token"]),
        int(row["owner_pid"]),
        str(row["acquired_at"]),
    )
    expected = (
        _daily_logical_path(fence.daily_id),
        fence.source_digest,
        fence.token,
        os.getpid(),
        _timestamp(fence.acquired_at),
    )
    return stored == expected and fence.owner_pid == os.getpid()


def _insert_adopted_source_fence(
    database: sqlite3.Connection, values: tuple[object, ...]
) -> None:
    """Record the fence, refusing when another one already holds the source."""
    try:
        database.execute(
            "INSERT INTO source_fences "
            "(logical_path, source_digest, token, owner_pid, owner_start_identity, "
            "acquired_at, heartbeat_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
    except sqlite3.IntegrityError as exc:
        raise QueueOperationError("source_fenced") from exc


def _task_payload_text(row: sqlite3.Row) -> str:
    """The task payload as text for the source-reference check.

    Undecodable bytes are replaced, never dropped: the replacement makes the
    payload unreadable as JSON, and an unreadable payload counts as referencing
    every fenced source, which is the conservative answer at both call sites.
    """
    return _stored_payload_text(bytes(row["payload_blob"]))


# What the v3 `source_failures.error_code` CHECK stores: bytes, not characters.
MAX_SOURCE_FAILURE_CODE_BYTES = 64


def _within_stored_code_bytes(error_code: str) -> bool:
    return 1 <= len(error_code.encode("utf-8")) <= MAX_SOURCE_FAILURE_CODE_BYTES


def _valid_source_failure_fields(error_code: object, producer: object) -> bool:
    """Whether a source failure's own fields are inside contract.

    The bound is the one the schema enforces — 1 to 64 bytes of UTF-8 — and not
    200 characters, which let a code through validation and broke the insert. See
    `docs/research/2026-09-18-a-refusal-is-cheaper-than-a-crash.md`.
    """
    if not isinstance(error_code, str) or not _within_stored_code_bytes(error_code):
        return False
    if any(char in error_code for char in "\r\n"):
        return False
    return producer in {"compile", "queue"}


# How many times one task may be redriven. Current practice bounds nothing here
# because a redrive is everywhere an operator's decision, and an operator does
# not redrive the same message forever. Nobody here is that operator, so the
# bound has to exist, and it has to be small: a task that died with its eight
# attempts spent is already saying the trouble is not bad luck. One more round
# of eight is sixteen; there is no third.
# See `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`.
MAX_REDRIVE_GENERATIONS = 1


def _require_dead_task(database: sqlite3.Connection, task_id: str) -> sqlite3.Row:
    """The dead task row this redrive is allowed to copy."""
    row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    _require_redrivable(row)
    _require_lineage_budget(database, row)
    _require_unspent_redrive(database, row)
    return row


def _unblock_in(database: sqlite3.Connection, task_id: str, now: datetime) -> None:
    """Put one blocked task back to ready: the operator says its capability is back.

    A task blocked on `process_cleanup` had no way back but `cancel`, which loses
    the work. See `docs/research/2026-09-17-a-failed-start-is-not-a-failed-cleanup.md`.
    """
    row = database.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    if row["state"] != "blocked":
        raise QueueOperationError("unblock_requires_blocked")
    database.execute(
        """UPDATE tasks SET state='ready',blocked_capability=NULL,error_code=NULL,
               updated_at=?,available_at=? WHERE id=? AND state='blocked'""",
        (_timestamp(now), _timestamp(now), task_id),
    )


def _require_redrivable(row: sqlite3.Row) -> None:
    if row["state"] != "dead":
        raise QueueOperationError("redrive_requires_dead")


# A redrive that could reach the worker: its child carries the parent's capture
# link, or the parent was not a capture. `lineage_generation` counts every change
# to a lineage and is the version its compare-and-set reads, not a count of
# chances: a child created on 2026-09-06, before a redrive carried the link, never
# reached the capture worker and still raised it.
_SPENT_REDRIVES = (
    "SELECT COUNT(*) FROM tasks AS child WHERE child.redrive_of=? AND ("
    "NOT EXISTS (SELECT 1 FROM capture_task_links WHERE task_id=?) "
    "OR EXISTS (SELECT 1 FROM capture_task_links WHERE task_id=child.id))"
)


def _require_unspent_redrive(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """The dead row gets one redrive, and only one the worker could reach counts.

    See `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
    """
    task_id = str(row["id"])
    spent = database.execute(_SPENT_REDRIVES, (task_id, task_id)).fetchone()[0]
    if int(spent) >= MAX_REDRIVE_GENERATIONS:
        raise QueueOperationError("redrive_generations_exhausted")


def _redrive_depth(database: sqlite3.Connection, task_id: str) -> int:
    """How many redrives deep this task is, counted to the bound and no further."""
    row = database.execute(
        """WITH RECURSIVE lineage(id, depth) AS (
               SELECT redrive_of, 1 FROM tasks WHERE id=? AND redrive_of IS NOT NULL
               UNION ALL
               SELECT tasks.redrive_of, lineage.depth + 1 FROM tasks
               JOIN lineage ON tasks.id=lineage.id
               WHERE tasks.redrive_of IS NOT NULL AND lineage.depth < ?
           )
           SELECT COALESCE(MAX(depth), 0) FROM lineage""",
        (task_id, MAX_REDRIVE_GENERATIONS),
    ).fetchone()
    return int(row[0])


def _require_lineage_budget(database: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A copy does not reset the bound: the lineage, not the dead row, gets one redrive.

    See `docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
    """
    if _redrive_depth(database, str(row["id"])) >= MAX_REDRIVE_GENERATIONS:
        raise QueueOperationError("redrive_generations_exhausted")


_SQLITE_FILE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite_file(path: Path) -> bool:
    """Whether the file begins with the SQLite header, without opening a database."""
    try:
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_FILE_MAGIC)) == _SQLITE_FILE_MAGIC
    except OSError:
        return False


def _state_relative_name(path: Path, state_root: Path) -> str:
    """The path as the operator knows it: relative to the state root, POSIX form.

    The refusal detail below travels through the CLI's 240-character bound.
    Composed with absolute paths it failed its own purpose exactly where paths
    are long -- macOS `/private/var/folders/...` and Windows `D:\\a\\...` temp
    roots pushed the adopted path past the cut. The relative form is short and
    stable everywhere, and the operator already knows the root.
    """
    try:
        return path.relative_to(state_root).as_posix()
    except ValueError:
        return str(path)


def _require_no_adoption_tombstone(db_path: Path, state_root: Path) -> None:
    """Refuse a legacy queue path by name once adoption has replaced it.

    Adoption leaves a JSON tombstone where the pre-adoption database was, so a
    reader that opens it anyway gets `file is not a database` -- a refusal that
    names neither the boundary that broke nor the path that broke it. The
    tombstone already names its replacement, and one reader follows it, so ask
    that reader and say what it found.
    """
    if _is_sqlite_file(db_path):
        return
    from installed_memory_repair import adopted_database_path

    active = adopted_database_path(database_name="queue", state_root=state_root)
    if active == db_path:
        return
    raise QueueOperationError(
        "queue_tombstoned_by_adoption",
        f"{_state_relative_name(db_path, state_root)} is a Reliability V3 "
        f"adoption tombstone naming {_state_relative_name(active, state_root)}; "
        "open the adopted queue through active_or_legacy_memory_queue()",
    )


class MemoryQueue:
    """Durable priority queue with lease-token fencing and at-least-once delivery."""

    @classmethod
    def _from_v3_candidate(
        cls, path: Path, *, state_root: Path, vault: Path | None = None
    ) -> _QueueV3CandidateReader:
        report = validate_queue_v3_database(path, state_root=state_root)
        return _QueueV3CandidateReader(
            Path(path), vault=state_root if vault is None else vault,
            expected_contract=_queue_contract_for_version(report["user_version"]),
        )

    def __init__(
        self,
        state_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        rng: random.Random | random.SystemRandom | None = None,
        heartbeat_wait: Callable[[threading.Event, float], bool] | None = None,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        retry_base_seconds: int = DEFAULTS.retry_base_seconds,
        retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
        deadline: float | None = None,
    ) -> None:
        operational_deadline_active(deadline)
        _validate_retry_policy(max_attempts, retry_base_seconds, retry_cap_seconds)
        self.state_root = Path(state_root).resolve()
        self.run_dir = self.state_root / "run"
        self.db_path = self.run_dir / "queue.sqlite3"
        _require_no_adoption_tombstone(self.db_path, self.state_root)
        self.results_dir = self.run_dir / "queue-results"
        self._clock = clock or _utc_now
        self._rng = rng or random.SystemRandom()
        self._heartbeat_wait = heartbeat_wait or (
            lambda stop, interval: stop.wait(interval)
        )
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._db_hardened = False
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.run_dir, 0o700)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        with self._connect(deadline=deadline) as connection:
            self._create_schema(connection)
            self._retire_exhausted_on_open(connection, deadline=deadline)

    def _retire_exhausted_on_open(self, connection: sqlite3.Connection, *, deadline: float | None = None) -> None:
        """Take the write lock only when a ready task is out of attempts.

        Every construction opened `BEGIN IMMEDIATE`, so opening a queue while a
        writer held it failed with `database is locked` although there was
        nothing to retire (docs/research/2026-09-25-opening-the-legacy-queue-takes-no-write-lock.md).
        """
        # fetchall steps the statement to its end, so no read lock outlives it.
        exhausted = connection.execute(
            "SELECT 1 FROM tasks WHERE state='ready' AND attempts >= ? LIMIT 1",
            (self._max_attempts,),
        ).fetchall()
        if not exhausted:
            return
        with begin_immediate(connection):
            self._retire_exhausted_ready(connection, _as_utc(self._clock()), self._max_attempts)
            refresh_operational_deadline(connection, DEFAULTS.queue_busy_ms, deadline)

    @contextmanager
    def _connect(self, *, busy_ms: int | None = None, deadline: float | None = None) -> Iterator[sqlite3.Connection]:
        connection = open_operational_db(self.db_path, busy_ms=DEFAULTS.queue_busy_ms if busy_ms is None else busy_ms, deadline=deadline)
        try:
            # Preserve this legacy context manager's implicit commit/rollback API.
            connection.isolation_level = "DEFERRED"
            if not self._db_hardened:
                _harden_owner_only(self.db_path, 0o600)
                self._db_hardened = True
            with connection:
                yield connection
                refresh_operational_deadline(connection, DEFAULTS.queue_busy_ms, deadline)
        finally:
            connection.close()
            operational_deadline_active(deadline)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One write connection; the seam both queue backends offer alike."""
        with self._connect() as database:
            yield database

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        MemoryQueue._upgrade_legacy_attempt_constraint(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                handler_version INTEGER NOT NULL CHECK (handler_version >= 1),
                payload_json TEXT NOT NULL,
                input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
                dedupe_key TEXT UNIQUE,
                state TEXT NOT NULL CHECK (state IN ('ready','leased','blocked','succeeded','dead','cancelled')),
                priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                last_attempt_at TEXT,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                lease_heartbeat_at TEXT,
                attempt_started_at TEXT,
                error_code TEXT,
                blocked_capability TEXT,
                result_reference TEXT,
                result_sha256 TEXT,
                result_operation_id TEXT,
                redrive_of TEXT REFERENCES tasks(id)
            );
            CREATE INDEX IF NOT EXISTS queue_claim_order
                ON tasks(state, priority DESC, available_at, created_at);
            CREATE TABLE IF NOT EXISTS source_fences (
                daily_id TEXT PRIMARY KEY,
                source_digest TEXT NOT NULL UNIQUE,
                token TEXT NOT NULL UNIQUE,
                owner_pid INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                heartbeat_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_failures (
                logical_path TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                error_code TEXT NOT NULL,
                producer TEXT NOT NULL CHECK (producer IN ('compile','queue')),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (logical_path, source_digest)
            );
            CREATE TABLE IF NOT EXISTS attempt_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','blocked','cancelled','lease_expired')),
                error_code TEXT
            );
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_update
                BEFORE UPDATE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS attempt_history_immutable_delete
                BEFORE DELETE ON attempt_history BEGIN SELECT RAISE(ABORT, 'attempt history is immutable'); END;
            """
        )
        _add_missing_queue_columns(connection)
    @staticmethod
    def _upgrade_legacy_attempt_constraint(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row is None or "attempts BETWEEN 0 AND 8" not in str(row["sql"]):
            return
        old_sql = str(row["sql"])
        new_sql = re.sub(
            r"^CREATE TABLE\s+tasks\b",
            "CREATE TABLE tasks_unbounded",
            old_sql,
            count=1,
            flags=re.IGNORECASE,
        ).replace("attempts BETWEEN 0 AND 8", "attempts >= 0")
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with begin_immediate(connection):
                connection.execute(new_sql)
                connection.execute("INSERT INTO tasks_unbounded SELECT * FROM tasks")
                connection.execute("DROP TABLE tasks")
                connection.execute("ALTER TABLE tasks_unbounded RENAME TO tasks")
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def _retire_exhausted_ready(
        self,
        connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='ready' AND attempts >= ?",
            (max_attempts,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """UPDATE tasks SET state='dead', error_code='attempts_exhausted',
                       updated_at=?, last_attempt_at=COALESCE(last_attempt_at, ?)
                   WHERE id=? AND state='ready' AND attempts >= ?""",
                (
                    _timestamp(now),
                    _timestamp(now),
                    row["id"],
                    max_attempts,
                ),
            )
            self._record_payload_failure(
                connection,
                str(row["payload_json"]),
                "attempts_exhausted",
                "queue",
                now,
            )

    def _legacy_deduplicated_task_id(
        self,
        connection: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = connection.execute(
            """SELECT id, kind, handler_version, payload_json, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        return _legacy_dedupe_match(
            existing, kind, handler_version, payload_bytes, input_hash
        )

    def _insert_legacy_task_row(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_json: str,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
        inserted = connection.execute(
            """INSERT INTO tasks(
                   id, kind, handler_version, payload_json, input_hash, dedupe_key,
                   state, priority, created_at, updated_at, available_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> str:
        _require_legacy_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
        payload_json = payload_bytes.decode("utf-8")
        input_hash = sha256_bytes(payload_bytes)
        now = _as_utc(self._clock())
        ready_at = _as_utc(available_at or now)
        task_id = uuid.UUID(int=self._rng.getrandbits(128)).hex
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, payload_json)
            deduplicated = self._legacy_deduplicated_task_id(
                connection, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_legacy_task_row(
                connection,
                task_id,
                kind,
                handler_version,
                payload_json,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
        return task_id

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int | None = None,
    ) -> QueueLease | None:
        _require_claim_owner_and_lease(owner, lease_seconds)
        attempt_limit, _base, _cap = self._retry_policy(max_attempts, None, None)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            self._expire_leases(connection, now, attempt_limit)
            self._delete_stale_source_fences(connection)
            row = _next_unfenced_ready_task(connection, now, attempt_limit)
            if row is None:
                return None
            token = f"{self._rng.getrandbits(256):064x}"
            expires = now + timedelta(seconds=lease_seconds)
            if not _take_v3_task_lease(
                connection,
                row,
                owner=owner,
                token=token,
                now=now,
                expires=expires,
                attempt_limit=attempt_limit,
            ):
                return None
            attempt = int(row["attempts"]) + 1
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=json.loads(row["payload_json"]),
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires,
            attempt=attempt,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def _settle_published_expiry(
        self, connection: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        """Close a lease that expired after its result had already been published."""
        outcome, state, error_code = _EXPIRED_PUBLISHED_OUTCOMES[
            self._stored_result_is_valid(row)
        ]
        self._record_attempt(connection, row, now, outcome, error_code)
        self._finish_lease(
            connection, row["id"], now, state, error_code, None, last_attempt_at=now
        )

    def _retire_expired_lease(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        max_attempts: int,
    ) -> None:
        """Return an expired lease to the queue, or bury it if it is out of tries."""
        state, error_code = _EXPIRED_LEASE_OUTCOMES[
            int(row["attempts"]) >= max_attempts
        ]
        self._record_attempt(connection, row, now, "lease_expired", error_code)
        connection.execute(
            """UPDATE tasks SET state=?, available_at=?, updated_at=?, last_attempt_at=?,
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?
               WHERE id=? AND state='leased' AND lease_token=?""",
            (
                state,
                _timestamp(now),
                _timestamp(now),
                _timestamp(now),
                error_code,
                row["id"],
                row["lease_token"],
            ),
        )

    def _expire_one_lease(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        max_attempts: int,
    ) -> None:
        """Settle one lease whose expiry has passed."""
        if row["result_reference"] is not None or row["result_sha256"] is not None:
            self._settle_published_expiry(connection, row, now)
            return
        self._retire_expired_lease(connection, row, now, max_attempts)

    def _expire_leases(
        self, connection: sqlite3.Connection, now: datetime, max_attempts: int
    ) -> int:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at <= ?",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            self._expire_one_lease(connection, row, now, max_attempts)
        return len(rows)

    def _stored_result_is_valid(self, row: sqlite3.Row) -> bool:
        reference = row["result_reference"]
        digest = row["result_sha256"]
        if not isinstance(reference, str) or not isinstance(digest, str):
            return False
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return False
        return self._validated_result_digest(reference) == digest

    def _result_path_usable(self, path: Path) -> bool:
        """The reference names a real, owner-only file directly in results."""
        resolved = path.resolve(strict=True)
        resolved.relative_to(self.results_dir.resolve())
        if path.parent.resolve() != self.results_dir.resolve() or path.is_symlink():
            return False
        metadata = path.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_size <= _MAX_RESULT_BYTES
            and _is_owner_only(path)
        )

    def _validated_result_digest(self, reference: str) -> str | None:
        try:
            path = self.state_root / reference
            if not self._result_path_usable(path):
                return None
            return _stable_result_digest(path)
        except (OSError, ValueError):
            return None

    def _read_result_for_export(self, reference: str, digest: str) -> bytes:
        try:
            path = self.state_root / reference
            resolved = path.resolve(strict=True)
            resolved.relative_to(self.results_dir.resolve())
            if path.parent.resolve() != self.results_dir.resolve():
                raise QueueOperationError("result_verification_failed")
            data = _read_stable_owner_file(path, _MAX_RESULT_BYTES)
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            return data
        except QueueOperationError:
            raise
        except (OSError, ValueError):
            raise QueueOperationError("result_verification_failed") from None

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        """Adopt an owner-only stable result left before its DB reference committed."""
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        if not target.exists() and not target.is_symlink():
            return None
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            digest = self._validated_result_digest(relative)
            if digest is None:
                return self._bury_corrupt_publication(connection, row, lease, now)
            connection.execute(
                """UPDATE tasks SET result_reference=?, result_sha256=?,
                       result_operation_id=?, updated_at=? WHERE id=?""",
                (relative, digest, operation_id, _timestamp(now), lease.id),
            )
            return "adopted"

    def _bury_corrupt_publication(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        lease: QueueLease,
        now: datetime,
    ) -> str:
        """Kill the leased task whose published result no longer verifies."""
        self._record_attempt(connection, row, now, "failed", "result_corrupt")
        self._finish_lease(
            connection, lease.id, now, "dead", "result_corrupt", None, last_attempt_at=now
        )
        return "corrupt"

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        _require_positive_task_lease(lease_seconds)
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._require_lease(connection, lease, now)
            connection.execute(
                "UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?, updated_at=? WHERE id=?",
                (_timestamp(expires), _timestamp(now), _timestamp(now), lease.id),
            )
        return replace(lease, expires_at=expires)

    def _fail_corrupt_result(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
    ) -> None:
        """A result we cannot read again kills the lease that published it."""
        self._record_attempt(connection, row, now, "failed", "result_corrupt")
        self._finish_lease(
            connection,
            lease.id,
            now,
            "dead",
            "result_corrupt",
            None,
            last_attempt_at=now,
        )

    def _existing_result_conflict(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, object],
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
    ) -> ResultConflictError | None:
        """Why the result already in place cannot stand for this publication."""
        existing_digest = self._validated_result_digest(relative)
        if existing_digest is None:
            self._fail_corrupt_result(connection, row, lease, now)
            return ResultConflictError("result_corrupt")
        if existing_digest != digest:
            return ResultConflictError(
                "operation ID already has different result bytes"
            )
        return None

    def _record_published_result(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        now: datetime,
        relative: str,
        digest: str,
        operation_id: str,
        target: Path,
        linked: bool,
    ) -> None:
        """Make the published bytes, and the row that names them, durable."""
        if linked:
            _harden_owner_only(target, 0o600)
            fsync_file(target)
        fsync_directory(self.results_dir)
        connection.execute(
            """UPDATE tasks SET result_reference=?, result_sha256=?,
                   result_operation_id=?, updated_at=?
               WHERE id=?""",
            (relative, digest, operation_id, _timestamp(now), lease.id),
        )

    def _commit_result(
        self,
        lease: QueueLease,
        operation_id: str,
        digest: str,
        relative: str,
        target: Path,
        temporary: Path,
    ) -> ResultConflictError | None:
        """Publish the staged bytes under the lease, or say why it cannot be."""
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            _check_result_operation(row, operation_id)
            linked = _link_result(temporary, target)
            if not linked:
                conflict = self._existing_result_conflict(
                    connection, row, lease, now, relative, digest
                )
                if conflict is not None:
                    return conflict
            self._record_published_result(
                connection, lease, now, relative, digest, operation_id, target, linked
            )
        return None

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        _check_result_request(operation_id, result)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        target = self.results_dir / result_name
        temporary = self.results_dir / f".{result_name}.{uuid.uuid4().hex}.tmp"
        try:
            _write_result_file(temporary, result)
            conflict = self._commit_result(
                lease,
                operation_id,
                sha256_bytes(result),
                relative,
                target,
                temporary,
            )
        finally:
            _discard_temporary(temporary)
        if conflict is not None:
            raise conflict
        return relative

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if not self._stored_result_is_valid(row):
                self._record_attempt(
                    connection, row, now, "failed", "result_corrupt"
                )
                self._finish_lease(
                    connection,
                    lease.id,
                    now,
                    "dead",
                    "result_corrupt",
                    None,
                    last_attempt_at=now,
                )
            else:
                self._record_attempt(connection, row, now, "succeeded", None)
                self._finish_lease(
                    connection, lease.id, now, "succeeded", None, None
                )
        return self.get(lease.id)

    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int | None = None,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> None:
        _require_error_code(failure)
        attempt_limit, retry_base, retry_cap = self._retry_policy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        )
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            row = self._require_lease(connection, lease, now)
            if failure.blocked_capability:
                self._block_leased_task(connection, lease, row, failure, now)
                return
            self._record_attempt(connection, row, now, "failed", failure.error_code)
            if _failure_is_terminal(row, failure, attempt_limit):
                self._retire_failed_task(connection, lease, failure, now)
                return
            self._reschedule_failed_task(
                connection,
                lease,
                row,
                failure,
                now,
                retry_base=retry_base,
                retry_cap=retry_cap,
            )

    def _retry_policy(
        self,
        max_attempts: int | None,
        retry_base_seconds: int | None,
        retry_cap_seconds: int | None,
    ) -> tuple[int, int, int]:
        """This call's retry policy: the caller's numbers, or the queue's."""
        attempt_limit = self._max_attempts if max_attempts is None else max_attempts
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds
            if retry_cap_seconds is None
            else retry_cap_seconds
        )
        _validate_retry_policy(attempt_limit, retry_base, retry_cap)
        return attempt_limit, retry_base, retry_cap

    def _block_leased_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        row: sqlite3.Row,
        failure: QueueFailure,
        now: datetime,
    ) -> None:
        """A missing capability blocks the task without spending an attempt."""
        self._record_attempt(connection, row, now, "blocked", failure.error_code)
        self._record_payload_failure(
            connection,
            str(row["payload_json"]),
            failure.error_code,
            "queue",
            now,
        )
        connection.execute(
            """UPDATE tasks SET state='blocked', attempts=attempts-1, updated_at=?,
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=? WHERE id=?""",
            (
                _timestamp(now),
                failure.error_code,
                failure.blocked_capability,
                lease.id,
            ),
        )

    def _retire_failed_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        failure: QueueFailure,
        now: datetime,
    ) -> None:
        self._finish_lease(
            connection,
            lease.id,
            now,
            "dead",
            failure.error_code,
            None,
            last_attempt_at=now,
        )

    def _reschedule_failed_task(
        self,
        connection: sqlite3.Connection,
        lease: QueueLease,
        row: sqlite3.Row,
        failure: QueueFailure,
        now: datetime,
        *,
        retry_base: int,
        retry_cap: int,
    ) -> None:
        """Put the task back, no sooner than the backoff and the Retry-After."""
        delay = self._retry_delay(
            int(row["attempts"]),
            retry_base_seconds=retry_base,
            retry_cap_seconds=retry_cap,
        )
        retry_after = self._retry_after_seconds(failure.retry_after, now)
        if retry_after is not None:
            delay = max(delay, retry_after)
        self._finish_lease(
            connection,
            lease.id,
            now,
            "ready",
            failure.error_code,
            now + timedelta(seconds=delay),
            last_attempt_at=now,
        )

    def _retry_delay(
        self,
        attempts: int,
        *,
        retry_base_seconds: int | None = None,
        retry_cap_seconds: int | None = None,
    ) -> float:
        retry_base = (
            self._retry_base_seconds
            if retry_base_seconds is None
            else retry_base_seconds
        )
        retry_cap = (
            self._retry_cap_seconds if retry_cap_seconds is None else retry_cap_seconds
        )
        jitter_cap = min(
            retry_cap,
            retry_base * (2 ** (attempts - 1)),
        )
        return self._rng.uniform(0, jitter_cap)

    @staticmethod
    def _retry_after_seconds(
        value: float | datetime | str | None, now: datetime
    ) -> float | None:
        return _retry_after_seconds(value, now)

    @staticmethod
    def _record_attempt(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        outcome: str,
        error_code: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO attempt_history(
                   task_id, attempt, started_at, finished_at, outcome, error_code
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                row["id"],
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
                outcome,
                error_code,
            ),
        )

    def _settle_payload_failure(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
    ) -> None:
        """Record or clear the source failure this lease outcome implies."""
        row = connection.execute(
            "SELECT payload_json FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            return
        self._apply_payload_failure(
            connection, str(row["payload_json"]), now, state, error_code
        )

    def _apply_payload_failure(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        now: datetime,
        state: str,
        error_code: str | None,
    ) -> None:
        """Record or clear the source failure this settled state implies."""
        if error_code is not None and state in {"ready", "dead"}:
            self._record_payload_failure(
                connection, payload_json, error_code, "queue", now
            )
            return
        if state in {"succeeded", "cancelled"}:
            self._clear_payload_failure(connection, payload_json)

    def _finish_lease(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: datetime,
        state: str,
        error_code: str | None,
        available_at: datetime | None,
        *,
        last_attempt_at: datetime | None = None,
    ) -> None:
        self._settle_payload_failure(connection, task_id, now, state, error_code)
        connection.execute(
            """UPDATE tasks SET state=?, updated_at=?, available_at=COALESCE(?, available_at),
                   last_attempt_at=COALESCE(?, last_attempt_at),
                   lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                   lease_heartbeat_at=NULL, attempt_started_at=NULL, error_code=?,
                   blocked_capability=NULL WHERE id=?""",
            (
                state,
                _timestamp(now),
                _timestamp(available_at) if available_at else None,
                _timestamp(last_attempt_at) if last_attempt_at else None,
                error_code,
                task_id,
            ),
        )

    @staticmethod
    def _require_lease(
        connection: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        return _require_leased_row(connection, lease, now)

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            if row["state"] == "leased":
                self._record_attempt(connection, row, now, "cancelled", "cancelled")
            self._clear_payload_failure(connection, str(row["payload_json"]))
            _require_active(deadline, cancelled)
            connection.execute(
                """UPDATE tasks SET state='cancelled', updated_at=?, lease_owner=NULL,
                       lease_token=NULL, lease_expires_at=NULL, lease_heartbeat_at=NULL,
                       attempt_started_at=NULL, error_code='cancelled' WHERE id=?""",
                (_timestamp(now), task_id),
            )
            return True

    def unblock(self, task_id: str) -> None:
        """A blocked task goes back to ready; any other state is refused."""
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            _unblock_in(connection, task_id, now)

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        _require_active(deadline, cancelled)
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            row = connection.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            if row["state"] != "dead":
                raise QueueOperationError("redrive_requires_dead")
            _require_lineage_budget(connection, row)
            self._delete_stale_source_fences(connection)
            self._assert_payload_not_fenced(connection, str(row["payload_json"]))
            payload_bytes = str(row["payload_json"]).encode("utf-8")
            replacement = uuid.UUID(int=self._rng.getrandbits(128)).hex
            _require_active(deadline, cancelled)
            connection.execute(
                """INSERT INTO tasks(
                       id, kind, handler_version, payload_json, input_hash, state,
                       priority, created_at, updated_at, available_at, redrive_of
                   ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
                (
                    replacement,
                    row["kind"],
                    row["handler_version"],
                    payload_bytes.decode("utf-8"),
                    sha256_bytes(payload_bytes),
                    row["priority"],
                    _timestamp(now),
                    _timestamp(now),
                    _timestamp(now),
                    task_id,
                ),
            )
        return replacement

    _validate_source_identity = staticmethod(_validate_source_identity)

    _validate_failure_identity = staticmethod(_validate_failure_identity)

    _source_identity_from_payload = staticmethod(_source_identity_from_payload)

    @staticmethod
    def _record_source_failure_row(
        connection: sqlite3.Connection,
        logical_path: str,
        source_digest: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """INSERT INTO source_failures(
                   logical_path, source_digest, error_code, producer, updated_at
               ) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(logical_path, source_digest) DO UPDATE SET
                   error_code=excluded.error_code,
                   producer=excluded.producer,
                   updated_at=excluded.updated_at""",
            (logical_path, source_digest, error_code, producer, _timestamp(now)),
        )

    def _record_payload_failure(
        self,
        connection: sqlite3.Connection,
        payload_json: str,
        error_code: str,
        producer: str,
        now: datetime,
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            self._record_source_failure_row(
                connection, *identity, error_code, producer, now
            )

    def _clear_payload_failure(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        identity = self._source_identity_from_payload(payload_json)
        if identity is not None:
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                identity,
            )

    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
        deadline: float = float("inf"),
    ) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        if not _valid_source_failure_fields(error_code, producer):
            raise ValueError("source failure fields are invalid")
        with _source_failure_connection(self, deadline) as connection, begin_immediate(connection):
            self._record_source_failure_row(
                connection,
                logical_path,
                source_digest,
                error_code,
                producer,
                _as_utc(self._clock()),
            )
            _source_failure_commit_active(connection, deadline)

    def clear_source_failure(self, logical_path: str, source_digest: str, *, deadline: float = float("inf")) -> None:
        self._validate_failure_identity(logical_path, source_digest)
        with _source_failure_connection(self, deadline) as connection, begin_immediate(connection):
            connection.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )
            _source_failure_commit_active(connection, deadline)

    def source_failure_keys(self, *, deadline: float = float("inf")) -> list[tuple[str, str]]:
        """Every recorded `(logical_path, source_digest)`, so a caller can retire stale ones.

        A row keyed by a digest the file no longer has is about content that is
        gone (audit B-15, docs/research/2026-09-25-a-failure-of-content-that-is-gone-is-retired.md).
        """
        with _source_failure_connection(self, deadline) as connection:
            rows = connection.execute(_SOURCE_FAILURE_KEYS).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def source_failure(
        self, logical_path: str, source_digest: str
    ) -> dict[str, str] | None:
        self._validate_failure_identity(logical_path, source_digest)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT logical_path, source_digest, error_code, producer "
                "FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    @staticmethod
    def _payload_references_source(
        payload_json: str, daily_id: str, source_digest: str
    ) -> bool:
        """A reference is an identity field of the payload, not a date in its text.

        `daily_id` is a date, and every timestamp this product writes starts with
        one, so the substring test this replaces fenced the whole queue while a
        day was being archived. See `docs/research/
        2026-09-18-a-source-is-referenced-by-a-field-not-by-a-date-in-the-text.md`.
        """
        found = _payload_identity_fields(payload_json)
        if found is None:
            return True
        return (
            daily_id in found["daily_ids"]
            or source_digest in found["digests"]
            or _daily_logical_path(daily_id) in found["paths"]
        )

    def _delete_stale_source_fences(self, connection: sqlite3.Connection) -> None:
        import process_liveness

        now = _as_utc(self._clock())
        rows = connection.execute(
            "SELECT token, owner_pid, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = _parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not process_liveness.owner_alive(int(row["owner_pid"])):
                connection.execute(
                    "DELETE FROM source_fences WHERE token=?", (row["token"],)
                )

    def _assert_payload_not_fenced(
        self, connection: sqlite3.Connection, payload_json: str
    ) -> None:
        for row in connection.execute(
            "SELECT daily_id, source_digest FROM source_fences"
        ):
            if self._payload_references_source(
                payload_json, str(row["daily_id"]), str(row["source_digest"])
            ):
                raise QueueOperationError("source_fenced")

    def referencing_source_tasks(
        self,
        daily_id: str,
        source_digest: str,
        *,
        states: tuple[str, ...] = ("ready", "leased", "blocked", "dead"),
    ) -> tuple[str, ...]:
        self._validate_source_identity(daily_id, source_digest)
        _require_known_states(states)
        placeholders = ",".join("?" for _ in states)
        with self._connect() as connection:
            cursor = connection.execute(
                f"SELECT id, payload_json FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            return tuple(
                str(row["id"])
                for row in cursor
                if self._payload_references_source(
                    str(row["payload_json"]), daily_id, source_digest
                )
            )

    def _require_source_unreferenced(
        self, connection: sqlite3.Connection, daily_id: str, source_digest: str
    ) -> None:
        """Refuse a fence over a source that some live task still points at."""
        rows = connection.execute(
            "SELECT id, payload_json FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        )
        if any(
            self._payload_references_source(
                str(row["payload_json"]), daily_id, source_digest
            )
            for row in rows
        ):
            raise QueueOperationError("source_referenced")

    def acquire_source_fence(
        self,
        daily_id: str,
        source_digest: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        self._validate_source_identity(daily_id, source_digest)
        _require_positive_lease(lease_seconds)
        token = f"{self._rng.getrandbits(256):064x}"
        acquired = _as_utc(self._clock())
        expires = acquired + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._require_source_unreferenced(connection, daily_id, source_digest)
            _insert_source_fence(
                connection,
                (
                    daily_id,
                    source_digest,
                    token,
                    os.getpid(),
                    _timestamp(acquired),
                    _timestamp(acquired),
                    _timestamp(expires),
                ),
            )
        return SourceFence(
            daily_id,
            source_digest,
            token,
            os.getpid(),
            acquired,
            acquired,
            expires,
        )

    def heartbeat_source_fence(
        self,
        fence: SourceFence,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        if not isinstance(fence, SourceFence):
            raise TypeError("source heartbeat requires a SourceFence")
        _require_positive_lease(lease_seconds)
        now = _as_utc(self._clock())
        expires = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            changed = connection.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE daily_id=? AND source_digest=? AND token=? AND owner_pid=?
                     AND acquired_at=? AND heartbeat_at=? AND expires_at=?""",
                (
                    _timestamp(now),
                    _timestamp(expires),
                    fence.daily_id,
                    fence.source_digest,
                    fence.token,
                    fence.owner_pid,
                    _timestamp(fence.acquired_at),
                    _timestamp(fence.heartbeat_at),
                    _timestamp(fence.expires_at),
                ),
            ).rowcount
            if changed != 1 or now >= fence.expires_at:
                raise QueueOperationError("source_fence_lost")
        return replace(fence, heartbeat_at=now, expires_at=expires)

    @contextmanager
    def source_fence_heartbeat(
        self,
        fence: SourceFence,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ):
        if not _valid_heartbeat_interval(heartbeat_seconds, lease_seconds):
            raise ValueError("source heartbeat interval must be shorter than its lease")
        heartbeat = _SourceFenceHeartbeat(
            self,
            fence,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            heartbeat.stop()

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with self._connect() as connection, begin_immediate(connection):
            self._delete_stale_source_fences(connection)
            self._require_live_source_fence(connection, fence)
            self._require_no_source_failure(connection, fence)
            self._require_no_source_reference(connection, fence)
            yield

    def _require_live_source_fence(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """The fence row still matches this holder, and has not expired."""
        row = connection.execute(
            "SELECT daily_id, source_digest, token, owner_pid, acquired_at, expires_at "
            "FROM source_fences WHERE token=?",
            (fence.token,),
        ).fetchone()
        if row is None or not _source_fence_row_matches(row, fence):
            raise QueueOperationError("source_fence_lost")
        expires_at = _parse_timestamp(str(row["expires_at"]))
        if expires_at is None or _as_utc(self._clock()) >= expires_at:
            raise QueueOperationError("source_fence_lost")

    @staticmethod
    def _require_no_source_failure(
        connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """A source that already failed must not be finalized."""
        logical_path = f"knowledge/daily/{fence.daily_id}.md"
        row = connection.execute(
            "SELECT 1 FROM source_failures "
            "WHERE logical_path=? AND source_digest=?",
            (logical_path, fence.source_digest),
        ).fetchone()
        if row is not None:
            raise QueueOperationError("source_failure")

    def _require_no_source_reference(
        self, connection: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """No retained task may still point at the source being finalized."""
        for task in connection.execute(
            "SELECT payload_json FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        ):
            if self._payload_references_source(
                str(task["payload_json"]), fence.daily_id, fence.source_digest
            ):
                raise QueueOperationError("source_referenced")

    def release_source_fence(self, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("source fence token is invalid")
        with self._connect() as connection, begin_immediate(connection):
            changed = connection.execute(
                "DELETE FROM source_fences WHERE token=? AND owner_pid=?",
                (token, os.getpid()),
            ).rowcount
            if changed != 1:
                raise QueueOperationError("source_fence_lost")

    def _has_any_retaining_row(self) -> bool:
        """Whether any queue table still holds a row."""
        with self._connect() as connection:
            return any(
                connection.execute(query).fetchone() is not None
                for query in _RETAINING_QUEUE_QUERIES
            )

    def retains_run_directory(self) -> bool:
        if self._has_any_retaining_row():
            return True
        if _directory_holds_anything(self.run_dir / "queue-quarantine"):
            return True
        return _directory_holds_anything(self.results_dir)

    def _purge_selection(
        self, states: tuple[str, ...], cutoff: datetime
    ) -> tuple[list, dict[str, list]]:
        with self._connect() as connection:
            state_places = ",".join("?" for _ in states)
            rows = connection.execute(
                f"""SELECT * FROM tasks
                    WHERE state IN ({state_places}) AND updated_at < ?
                    ORDER BY created_at, rowid""",  # noqa: S608
                (*states, _timestamp(cutoff)),
            ).fetchall()
            histories = {
                str(row["id"]): connection.execute(
                    "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                    (row["id"],),
                ).fetchall()
                for row in rows
            }
        return rows, histories

    def _export_results(
        self,
        rows: list,
        results_export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for row in rows:
            _require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            digest = row["result_sha256"]
            if not isinstance(digest, str):
                raise QueueOperationError("result_verification_failed")
            result = self._read_result_for_export(str(reference), digest)
            _write_durable_file(results_export / f"{row['id']}.result", result)
            result_manifest.append({"id": str(row["id"]), "sha256": digest})
        return result_manifest

    def _verify_export(
        self,
        staging: Path,
        results_export: Path,
        records_bytes: bytes,
        manifest_bytes: bytes,
        result_manifest: list[dict[str, str]],
    ) -> None:
        """Read every exported byte back before anything is deleted."""
        records = _read_stable_owner_file(
            staging / "records.json", _MAX_EXPORT_METADATA_BYTES
        )
        if records != records_bytes:
            raise QueueOperationError("export_verification_failed")
        _require_exported_results(results_export, result_manifest)
        manifest = _read_stable_owner_file(
            staging / "manifest.json", _MAX_EXPORT_METADATA_BYTES
        )
        if manifest != manifest_bytes:
            raise QueueOperationError("export_verification_failed")

    def _write_export_package(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        staging: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        results_export = staging / "results"
        results_export.mkdir()
        _harden_owner_only(results_export, 0o700)
        result_manifest = self._export_results(
            rows, results_export, deadline, cancelled
        )
        _write_durable_file(staging / "records.json", records_bytes)
        manifest_bytes = canonical_json_bytes(
            {
                "records_sha256": sha256_bytes(records_bytes),
                "results": result_manifest,
                "task_ids": list(task_ids),
            }
        )
        _write_durable_file(staging / "manifest.json", manifest_bytes)
        fsync_directory(results_export)
        fsync_directory(staging)
        self._verify_export(
            staging, results_export, records_bytes, manifest_bytes, result_manifest
        )

    def _publish_export(
        self,
        rows: list,
        records_bytes: bytes,
        task_ids: tuple[str, ...],
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        parent = export.parent
        _require_export_parent(parent)
        _cleanup_export_staging(parent, export.name)
        staging = parent / f".{export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        try:
            self._write_export_package(
                rows, records_bytes, task_ids, staging, deadline, cancelled
            )
            _require_active(deadline, cancelled)
            staging.replace(export)
            fsync_directory(parent)
        except Exception:
            _remove_export_staging(staging)
            raise

    def _delete_purged_tasks(
        self,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        _require_active(deadline, cancelled)
        placeholders = ",".join("?" for _ in task_ids)
        with self._connect() as connection, begin_immediate(
            connection,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            current = connection.execute(
                f"""SELECT id, state, updated_at FROM tasks
                    WHERE id IN ({placeholders})""",  # noqa: S608
                task_ids,
            ).fetchall()
            if _purge_selection_changed(
                current, task_ids, states, _timestamp(cutoff)
            ):
                raise QueueOperationError("purge_selection_changed")
            _require_active(deadline, cancelled)
            connection.execute("DROP TRIGGER attempt_history_immutable_delete")
            connection.execute(
                f"DELETE FROM attempt_history WHERE task_id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                f"DELETE FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
                task_ids,
            )
            connection.execute(
                """CREATE TRIGGER attempt_history_immutable_delete
                   BEFORE DELETE ON attempt_history BEGIN
                   SELECT RAISE(ABORT, 'attempt history is immutable'); END"""
            )

    def _drop_unreferenced_results(
        self, rows: list, deadline: float, cancelled: Callable[[], bool] | None
    ) -> None:
        for row in rows:
            _require_active(deadline, cancelled)
            reference = row["result_reference"]
            if reference is None:
                continue
            with self._connect() as connection:
                retained = connection.execute(
                    "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1",
                    (reference,),
                ).fetchone()
            if retained is None:
                (self.state_root / str(reference)).unlink(missing_ok=True)
        fsync_directory(self.results_dir)

    def _retire_purged_tasks(
        self,
        rows: list,
        task_ids: tuple[str, ...],
        states: tuple[str, ...],
        cutoff: datetime,
        export: Path,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> None:
        try:
            self._delete_purged_tasks(task_ids, states, cutoff, deadline, cancelled)
        except BaseException:
            _remove_export_staging(export)
            raise
        self._drop_unreferenced_results(rows, deadline, cancelled)

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        include_dead: bool = False,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        """Export, verify, then delete terminal tasks older than the cutoff.

        `include_dead` extends the selection to attempts-exhausted tasks. It is
        off by default: a dead task is evidence that work never happened, so
        retiring it stays an explicit operator action.
        """
        _require_active(deadline, cancelled)
        states = _purge_states(include_dead)
        retention_cutoff = _as_utc(self._clock()) - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = min(_as_utc(terminal_before), retention_cutoff)
        export = Path(export_path).absolute()
        if export.exists():
            raise QueueOperationError("export_exists")
        rows, histories = self._purge_selection(states, cutoff)
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(
            [
                _export_task_record(
                    self._task_from_row(row, histories[str(row["id"])])
                )
                for row in rows
            ]
        )
        self._publish_export(
            rows, records_bytes, task_ids, export, deadline, cancelled
        )
        if task_ids:
            self._retire_purged_tasks(
                rows, task_ids, states, cutoff, export, deadline, cancelled
            )
        return PurgeReceipt(len(task_ids), task_ids)

    def restore(
        self,
        *,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreReceipt:
        """Re-enqueue the work in one verified purge export.

        A purged task leaves the queue with its history, so restoring it means
        the work runs again: each record returns as a new ready task and the
        receipt maps the exported id to the new one. Nothing is restored unless
        the manifest and every digest verify first.
        """
        return _restore_from_export(
            self, export_path=export_path, deadline=deadline, cancelled=cancelled
        )

    def recover_expired_leases(self) -> int:
        now = _as_utc(self._clock())
        with self._connect() as connection, begin_immediate(connection):
            return self._expire_leases(connection, now, self._max_attempts)

    def get(self, task_id: str) -> QueueTask:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            history = connection.execute(
                "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence", (task_id,)
            ).fetchall()
        return self._task_from_row(row, history)

    def list_tasks(
        self, *, states: tuple[str, ...] | None = None, max_age_days: int | None = None
    ) -> list[QueueTask]:
        states = _validated_listed_states(states)
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = self._age_clause(max_age_days, parameters)
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories = _attempt_histories(connection, rows)
        return [
            self._task_from_row(row, histories.get(str(row["id"]), []))
            for row in rows
        ]

    def _age_clause(
        self, max_age_days: int | None, parameters: list[object]
    ) -> str:
        """The age filter, appending its parameter when there is one."""
        return _age_filter_clause(max_age_days, parameters, _as_utc(self._clock()))

    def count_eligible(self, *, max_attempts: int = DEFAULTS.queue_max_attempts) -> int:
        _validate_retry_policy(
            max_attempts, self._retry_base_seconds, self._retry_cap_seconds
        )
        now = _as_utc(self._clock())
        with self._connect() as connection:
            _register_source_reference(connection)
            row = connection.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {_V2_WORK_CLAIMABLE}",
                (max_attempts, _timestamp(now)),
            ).fetchone()
        return int(row[0])

    @staticmethod
    def _task_from_row(row: sqlite3.Row, history: list[sqlite3.Row]) -> QueueTask:
        return _task_record(row, history, json.loads(row["payload_json"]))


# state, error_code, and attempt outcome for an acknowledgement, by whether the
# published result was still intact when the lease was acknowledged.
_ACKNOWLEDGED_OUTCOMES = {
    True: ("succeeded", None, "succeeded"),
    False: ("dead", "result_corrupt", "failed"),
}


_LeaseAction = Callable[[sqlite3.Connection, sqlite3.Row, datetime], object]


def _proved_lease_only(
    database: sqlite3.Connection, row: sqlite3.Row, now: datetime
) -> None:
    """The action of a pass that only proves the lease and its payload hold."""


# What `work` may claim from the adopted queue, and therefore what it counts as
# remaining: one predicate, so the count can never name a task the claim skips.
# The two were written apart and drifted twice — a ready capture task (the capture
# worker's) and a task under a source fence were both counted and never claimed,
# and the nightly's work step failed on `remaining_eligible` with nothing to do.
# Parameters: max_attempts, now. See docs/research/2026-09-28-a-check-names-its-cause.md.
# substr(..., 17, 10) is the daily id inside the canonical
# 'knowledge/daily/YYYY-MM-DD.md'; acquire_source_fence validates that form
# before any fence row exists, so the slice is always the date.
_V3_WORK_CLAIMABLE = """state='ready' AND attempts < ? AND available_at <= ?
    AND NOT EXISTS (
        SELECT 1 FROM capture_task_links link WHERE link.task_id=tasks.id
    )
    AND NOT EXISTS (
        SELECT 1 FROM source_fences fence
        WHERE payload_references_source(
            tasks.payload_blob, substr(fence.logical_path, 17, 10), fence.source_digest
        )
    )"""


def _claim_task_filter(task_id):
    if task_id is None:
        return '', ()
    _require_task_identifier(task_id)
    return ' AND tasks.id=?', (task_id,)


def _require_compile_task_family(database, row, checkpoint):
    family = _redrive_ancestors(database, row['id']) | {row['id']}
    if row['kind'] != 'compile' or checkpoint.task_id not in family:
        raise compile_progress.ProgressConflict('compile checkpoint belongs to another task family')


def _require_uncertain_compile_dispatch(database, task_id, work_id, dispatch_digest):
    row = database.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    if (row['state'], row['blocked_capability']) not in {
            ('blocked', 'provider_outcome_unknown'), ('ready', None)}:
        raise QueueOperationError('compile_recovery_requires_unknown_outcome')
    event = compile_progress.latest_dispatch(database, work_id)
    _require_dispatch_identity(event, dispatch_digest)
    _require_compile_task_family(database, row, event)
    current = compile_progress.latest_event(database, work_id)
    _require_compile_task_family(database, row, current)
    _require_recovery_position(event, current)
    return row


def _require_recovery_position(dispatch, current):
    if current.sequence not in (dispatch.sequence, dispatch.sequence + 1):
        raise QueueOperationError('compile_recovery_progress_advanced')


def _require_dispatch_identity(event, dispatch_digest):
    if event is None:
        raise compile_progress.ProgressConflict('compile dispatch is absent')
    if (event.details.get('model_call', {}).get('phase') != 'dispatch'
            or compile_progress._event_digest(event) != dispatch_digest):
        raise compile_progress.ProgressConflict('compile dispatch changed')


def _is_compile_recovery(row):
    return (row['state'] == 'leased' and row['kind'] == 'compile'
            and row['blocked_capability'] == 'provider_outcome_unknown')


def _require_compile_recovery(row):
    if not _is_compile_recovery(row) or row['attempts'] < 1:
        raise QueueOperationError('compile_recovery_lease_required')


def _require_dispatch_execution(row, event):
    if _is_compile_recovery(row) and 'model_call' in event.details:
        compile_progress._require_model_phase(event)
        if event.details['model_call']['phase'] == 'dispatch':
            raise QueueOperationError('compile_recovery_is_not_execution')


def _mark_compile_recovery(database, lease):
    changed = database.execute(
        """UPDATE tasks SET blocked_capability='provider_outcome_unknown',
           error_code='provider_outcome_unknown'
           WHERE id=? AND state='leased' AND lease_token=?""", (lease.id, lease.token),
    ).rowcount
    if changed != 1:
        raise LeaseFenceError('compile recovery claim lost its lease')


def _settle_compile_recovery(database, row, now, *, state, outcome, code):
    _require_compile_recovery(row)
    database.execute(
        """INSERT INTO attempt_history(task_id,attempt,started_at,finished_at,outcome,error_code)
           VALUES (?,?,?,?,?,?)""",
        (row['id'], row['attempts'], row['attempt_started_at'], _timestamp(now), outcome, code),
    )
    error, capability = {
        'ready': (None, None),
        'blocked': ('provider_outcome_unknown', 'provider_outcome_unknown'),
    }[state]
    changed = database.execute(
        """UPDATE tasks SET state=?,attempts=attempts-1,error_code=?,blocked_capability=?,
           updated_at=?,available_at=?,lease_owner=NULL,lease_token=NULL,
           lease_expires_at=NULL,lease_heartbeat_at=NULL,attempt_started_at=NULL
           WHERE id=? AND state='leased' AND lease_token=?""",
        (state, error, capability, _timestamp(now), _timestamp(now), row['id'], row['lease_token']),
    ).rowcount
    if changed != 1:
        raise LeaseFenceError('compile recovery settlement lost its lease')


def _require_compile_work_family(database, row, event):
    previous = compile_progress.latest_event(database, event.work_id)
    if previous is not None:
        _require_compile_task_family(database, row, previous)


def _compile_operator_progress(database, task_id):
    row = database.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise KeyError(task_id)
    origin = _compile_lineage_origin(database, row)
    works = database.execute(
        """WITH RECURSIVE family(id,redrive_of) AS (
             SELECT id,redrive_of FROM tasks WHERE id=?
             UNION ALL SELECT t.id,t.redrive_of FROM tasks t
             JOIN family f ON t.id=f.redrive_of
           )
           SELECT DISTINCT e.work_id FROM compile_work_events e
           JOIN family f ON e.task_id=f.id ORDER BY e.work_id""", (task_id,),
    ).fetchall()
    return {
        'schema': 'compile-operator-progress/v1', 'task_id': task_id,
        'state': row['state'], 'answer_artifacts_checked': False,
        'history_audited': False,
        'works': [_compile_operator_work(database, item['work_id'], origin) for item in works],
    }


def _compile_operator_work(database, work_id, origin):
    event = compile_progress.latest_event(database, work_id)
    if event is None:
        raise compile_progress.ProgressCorrupt('compile work has no checkpoint')
    row = _required_redrive_row(database, event.task_id)
    if _compile_lineage_origin(database, row) != origin:
        raise compile_progress.ProgressCorrupt('compile work changed task family')
    return {**compile_progress.operator_summary(event),
            'recovery_dispatch': compile_progress.dispatch_summary(
                compile_progress.latest_dispatch(database, work_id))}


def _require_compile_root_work(database, row, event):
    if event.sequence != 0 or 'compile_work' not in event.details:
        return
    origin = _compile_lineage_origin(database, row)
    compile_progress.validate_root_event(event, origin)


def _completed_compile_attestation(database, row):
    completed = compile_progress.completed_work(database, _compile_lineage_origin(database, row))
    if completed is not None:
        _require_compile_task_family(database, row, completed)
    return completed


def _require_complete_compile_attestation(database, row):
    try:
        completed = _completed_compile_attestation(database, row)
    except QueueOperationError as error:
        raise CompileCompletionError(error.code) from error
    except (ValueError, TypeError, KeyError) as error:
        raise CompileCompletionError('compile_completion_corrupt') from error
    if completed is None:
        raise CompileCompletionError('compile_work_incomplete')


def _require_initial_capture_root(root, lease, origin):
    expected = (lease.id, 0, 'pending', 'ready')
    if (root.task_id, root.sequence, root.stage, root.outcome) != expected:
        raise compile_progress.ProgressConflict('initial capture root identity changed')
    if set(root.details) != {'compile_work'}:
        raise compile_progress.ProgressConflict('initial capture requires a whole-work root')
    compile_progress.validate_root_event(root, origin)


class _QueueV3CandidateReader:
    """The queue-v3 backend: an unpublished candidate, or the adopted database.

    It began as a claim seam for tests against a candidate. Adoption made it the
    only queue an adopted vault has, so it is the backend
    `active_or_legacy_memory_queue` hands every caller once adoption is in force.
    """

    def __init__(self, path: Path, *, coordinator_path: Path | None = None, vault: Path | None = None,
                 expected_contract: OperationalDatabaseContract = _QUEUE_V3_CONTRACT) -> None:
        self.expected_contract = expected_contract
        self.db_path = Path(path)
        self.state_root = self.db_path.parent.parent
        self.results_dir = self.state_root / "run" / "queue-results"
        self.coordinator_path = coordinator_path
        self.vault = None if vault is None else Path(vault).resolve(strict=True)

    def ownership_registry(self):
        from operational_ownership import OwnershipRegistry

        if self.coordinator_path is None:
            return OwnershipRegistry(self.state_root)
        return OwnershipRegistry._from_adopted_database(
            self.state_root, self.coordinator_path
        )

    def _connect(self, *, busy_ms: int | None = None, deadline: float | None = None) -> sqlite3.Connection:
        return _open_queue_operational(
            self.db_path,
            busy_ms=DEFAULTS.queue_busy_ms if busy_ms is None else busy_ms,
            deadline=deadline,
            expected_contract=self.expected_contract,
        )

    def _heartbeat_wait(self, stop: threading.Event, interval: float) -> bool:
        """The heartbeat thread's wait; the adopted backend injects no seam."""
        return stop.wait(interval)

    @property
    def run_dir(self) -> Path:
        """Where this queue's runtime files live, named as the legacy queue names it."""
        return self.state_root / "run"

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """One write connection; the seam both queue backends offer alike."""
        with closing(self._connect()) as database, database:
            yield database

    def get(self, task_id: str) -> QueueTask:
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            history = database.execute(
                "SELECT * FROM attempt_history WHERE task_id=? ORDER BY sequence",
                (task_id,),
            ).fetchall()
        return _v3_task_record(row, history)

    def list_tasks(
        self, *, states: tuple[str, ...] | None = None, max_age_days: int | None = None
    ) -> list[QueueTask]:
        states = _validated_listed_states(states)
        placeholders = ",".join("?" for _ in states)
        parameters: list[object] = list(states)
        age_clause = _age_filter_clause(max_age_days, parameters, _utc_now())
        with closing(self._connect()) as database:
            rows = database.execute(
                f"""SELECT * FROM tasks WHERE state IN ({placeholders}){age_clause}
                    ORDER BY created_at, rowid""",  # noqa: S608 - generated placeholders
                parameters,
            ).fetchall()
            histories = _attempt_histories(database, rows)
        return [
            _v3_task_record(row, histories.get(str(row["id"]), [])) for row in rows
        ]

    def count_eligible(self, *, max_attempts: int = DEFAULTS.queue_max_attempts) -> int:
        """Ready tasks `work` could claim, by the claim's own predicate.

        A capture task is the capture worker's (`claim_capture`) and a fenced source
        waits for its fence; `work` claims neither, so neither is counted here.
        """
        with closing(self._connect()) as database:
            _register_source_reference(database)
            row = database.execute(
                f"SELECT COUNT(*) FROM tasks WHERE {_V3_WORK_CLAIMABLE}",
                (max_attempts, _timestamp(_utc_now())),
            ).fetchone()
        return int(row[0])

    def retains_run_directory(self) -> bool:
        """Whether queue rows, quarantine or results still hold `run/` open."""
        with closing(self._connect()) as database:
            held = any(
                database.execute(query).fetchone() is not None
                for query in _RETAINING_QUEUE_QUERIES
            )
        if held or _directory_holds_anything(self.run_dir / "queue-quarantine"):
            return True
        return _directory_holds_anything(self.results_dir)

    def record_source_failure(
        self,
        logical_path: str,
        source_digest: str,
        *,
        error_code: str,
        producer: str,
        deadline: float = float("inf"),
    ) -> None:
        MemoryQueue._validate_failure_identity(logical_path, source_digest)
        if not _valid_source_failure_fields(error_code, producer):
            raise ValueError("source failure fields are invalid")
        with _source_failure_connection(self, deadline) as database, begin_immediate(database):
            MemoryQueue._record_source_failure_row(
                database,
                logical_path,
                source_digest,
                error_code,
                producer,
                _utc_now(),
            )
            _source_failure_commit_active(database, deadline)

    def clear_source_failure(self, logical_path: str, source_digest: str, *, deadline: float = float("inf")) -> None:
        MemoryQueue._validate_failure_identity(logical_path, source_digest)
        with _source_failure_connection(self, deadline) as database, begin_immediate(database):
            database.execute(
                "DELETE FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            )
            _source_failure_commit_active(database, deadline)

    def source_failure_keys(self, *, deadline: float = float("inf")) -> list[tuple[str, str]]:
        """Every recorded `(logical_path, source_digest)`; see `MemoryQueue.source_failure_keys`."""
        with _source_failure_connection(self, deadline) as database:
            rows = database.execute(_SOURCE_FAILURE_KEYS).fetchall()
        return [(str(row[0]), str(row[1])) for row in rows]

    def source_failure(
        self, logical_path: str, source_digest: str
    ) -> dict[str, str] | None:
        MemoryQueue._validate_failure_identity(logical_path, source_digest)
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT logical_path, source_digest, error_code, producer "
                "FROM source_failures WHERE logical_path=? AND source_digest=?",
                (logical_path, source_digest),
            ).fetchone()
        return None if row is None else {key: str(row[key]) for key in row.keys()}

    def restore(
        self,
        *,
        export_path: Path,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreReceipt:
        """Re-enqueue the work in one verified purge export."""
        return _restore_from_export(
            self, export_path=export_path, deadline=deadline, cancelled=cancelled
        )

    # The source-fence family, adopted (2026-08-27). The rows live in the V3
    # `source_fences` table -- keyed by `logical_path` instead of the legacy
    # `daily_id`, and carrying `owner_start_identity` -- while every
    # caller-visible contract (arguments, refusal codes, ordering) stays
    # exactly the legacy one, so `archive_daily` runs unchanged.
    def _delete_stale_source_fences(self, database: sqlite3.Connection) -> None:
        """Drop fences whose lease expired or whose owning process died."""
        now = _utc_now()
        rows = database.execute(
            "SELECT token, owner_pid, owner_start_identity, expires_at FROM source_fences"
        ).fetchall()
        for row in rows:
            expires_at = _parse_timestamp(str(row["expires_at"]))
            expired = expires_at is None or expires_at <= now
            if expired or not _fence_owner_alive(row):
                database.execute(
                    "DELETE FROM source_fences WHERE token=?", (row["token"],)
                )

    @staticmethod
    def _fenced_sources(database: sqlite3.Connection) -> list[tuple[str, str]]:
        """Every held fence as the (daily_id, source_digest) it protects."""
        return [
            (Path(str(row["logical_path"])).stem, str(row["source_digest"]))
            for row in database.execute(
                "SELECT logical_path, source_digest FROM source_fences"
            )
        ]

    def _assert_payload_not_fenced(
        self, database: sqlite3.Connection, payload_json: str
    ) -> None:
        for daily_id, source_digest in self._fenced_sources(database):
            if MemoryQueue._payload_references_source(
                payload_json, daily_id, source_digest
            ):
                raise QueueOperationError("source_fenced")

    def referencing_source_tasks(
        self,
        daily_id: str,
        source_digest: str,
        *,
        states: tuple[str, ...] = ("ready", "leased", "blocked", "dead"),
    ) -> tuple[str, ...]:
        MemoryQueue._validate_source_identity(daily_id, source_digest)
        _require_known_states(states)
        placeholders = ",".join("?" for _ in states)
        with closing(self._connect()) as database:
            cursor = database.execute(
                f"SELECT id, payload_blob FROM tasks WHERE state IN ({placeholders}) "
                "ORDER BY created_at, rowid",  # noqa: S608 - generated placeholders
                states,
            )
            return tuple(
                str(row["id"])
                for row in cursor
                if MemoryQueue._payload_references_source(
                    _task_payload_text(row), daily_id, source_digest
                )
            )

    def _require_source_unreferenced(
        self, database: sqlite3.Connection, daily_id: str, source_digest: str
    ) -> None:
        """Refuse a fence over a source that some live task still points at."""
        rows = database.execute(
            "SELECT payload_blob FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        )
        if any(
            MemoryQueue._payload_references_source(
                _task_payload_text(row), daily_id, source_digest
            )
            for row in rows
        ):
            raise QueueOperationError("source_referenced")

    def acquire_source_fence(
        self,
        daily_id: str,
        source_digest: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        from operational_ownership import current_process_identity

        MemoryQueue._validate_source_identity(daily_id, source_digest)
        _require_positive_lease(lease_seconds)
        process = current_process_identity()
        token = sha256_bytes(os.urandom(32))
        acquired = _utc_now()
        expires = acquired + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as database, begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._require_source_unreferenced(database, daily_id, source_digest)
            _insert_adopted_source_fence(
                database,
                (
                    _daily_logical_path(daily_id),
                    source_digest,
                    token,
                    process.pid,
                    process.start_identity,
                    _timestamp(acquired),
                    _timestamp(acquired),
                    _timestamp(expires),
                ),
            )
        return SourceFence(
            daily_id,
            source_digest,
            token,
            process.pid,
            acquired,
            acquired,
            expires,
        )

    def heartbeat_source_fence(
        self,
        fence: SourceFence,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> SourceFence:
        if not isinstance(fence, SourceFence):
            raise TypeError("source heartbeat requires a SourceFence")
        _require_positive_lease(lease_seconds)
        now = _utc_now()
        expires = now + timedelta(seconds=lease_seconds)
        with closing(self._connect()) as database, begin_immediate(database):
            self._delete_stale_source_fences(database)
            changed = database.execute(
                """UPDATE source_fences SET heartbeat_at=?, expires_at=?
                   WHERE logical_path=? AND source_digest=? AND token=?
                     AND owner_pid=? AND acquired_at=? AND heartbeat_at=?
                     AND expires_at=?""",
                (
                    _timestamp(now),
                    _timestamp(expires),
                    _daily_logical_path(fence.daily_id),
                    fence.source_digest,
                    fence.token,
                    fence.owner_pid,
                    _timestamp(fence.acquired_at),
                    _timestamp(fence.heartbeat_at),
                    _timestamp(fence.expires_at),
                ),
            ).rowcount
            if changed != 1 or now >= fence.expires_at:
                raise QueueOperationError("source_fence_lost")
        return replace(fence, heartbeat_at=now, expires_at=expires)

    def source_fence_heartbeat(
        self,
        fence: SourceFence,
        *,
        heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ):
        """A held fence kept renewed by the same heartbeat the legacy queue runs."""
        return MemoryQueue.source_fence_heartbeat(
            self,
            fence,
            heartbeat_seconds=heartbeat_seconds,
            lease_seconds=lease_seconds,
        )

    @contextmanager
    def source_finalization(self, fence: SourceFence):
        if not isinstance(fence, SourceFence):
            raise TypeError("source finalization requires a SourceFence")
        with closing(self._connect()) as database, begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._require_live_source_fence(database, fence)
            MemoryQueue._require_no_source_failure(database, fence)
            self._require_no_source_reference(database, fence)
            yield

    def _require_live_source_fence(
        self, database: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """The fence row still matches this holder, and has not expired."""
        row = database.execute(
            "SELECT logical_path, source_digest, token, owner_pid, "
            "acquired_at, expires_at FROM source_fences WHERE token=?",
            (fence.token,),
        ).fetchone()
        if row is None or not _adopted_fence_row_matches(row, fence):
            raise QueueOperationError("source_fence_lost")
        expires_at = _parse_timestamp(str(row["expires_at"]))
        if expires_at is None or _utc_now() >= expires_at:
            raise QueueOperationError("source_fence_lost")

    def _require_no_source_reference(
        self, database: sqlite3.Connection, fence: SourceFence
    ) -> None:
        """No retained task may still point at the source being finalized."""
        for task in database.execute(
            "SELECT payload_blob FROM tasks "
            "WHERE state IN ('ready','leased','blocked','dead')"
        ):
            if MemoryQueue._payload_references_source(
                _task_payload_text(task), fence.daily_id, fence.source_digest
            ):
                raise QueueOperationError("source_referenced")

    def release_source_fence(self, token: str) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("source fence token is invalid")
        with closing(self._connect()) as database, begin_immediate(database):
            changed = database.execute(
                "DELETE FROM source_fences WHERE token=? AND owner_pid=?",
                (token, os.getpid()),
            ).rowcount
            if changed != 1:
                raise QueueOperationError("source_fence_lost")

    @staticmethod
    def _demote_payload_mismatch(
        database: sqlite3.Connection, row: sqlite3.Row, *, now: datetime
    ) -> None:
        changed = database.execute(
            """UPDATE tasks SET state='dead', error_code='payload_hash_mismatch',
                   updated_at=?, lease_owner=NULL, lease_token=NULL,
                   lease_expires_at=NULL, lease_heartbeat_at=NULL,
                   attempt_started_at=NULL, blocked_capability=NULL
               WHERE id=? AND state=?""",
            (_timestamp(now), row["id"], row["state"]),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("payload_demotion_failed")

    @staticmethod
    def _require_valid_task_payload(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: datetime,
        parse: bool,
    ) -> PayloadValidation | None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=parse
        )
        if validation.code is not None:
            _QueueV3CandidateReader._demote_payload_mismatch(database, row, now=now)
            return None
        return validation

    @staticmethod
    def _require_lease_row(
        database: sqlite3.Connection, lease: QueueLease, now: datetime
    ) -> sqlite3.Row:
        return _require_leased_row(database, lease, now)

    @staticmethod
    def _raise_payload_mismatch() -> None:
        raise QueueOperationError("payload_hash_mismatch")

    def _deduplicated_task_id(
        self,
        database: sqlite3.Connection,
        dedupe_key: str | None,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
    ) -> str | None:
        """The id of an identical task already enqueued under this dedupe key."""
        if dedupe_key is None:
            return None
        existing = database.execute(
            """SELECT id, kind, handler_version, payload_blob, input_hash
               FROM tasks WHERE dedupe_key=?""",
            (dedupe_key,),
        ).fetchone()
        if existing is None:
            return None
        return _v3_dedupe_match(
            existing, kind, handler_version, payload_bytes, input_hash
        )

    def _insert_task_row(
        self,
        database: sqlite3.Connection,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,dedupe_key,
                   state,priority,created_at,updated_at,available_at
               ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")

    def _insert_source_links(
        self,
        database: sqlite3.Connection,
        task_id: str,
        normalized_links: list[tuple[str, str]],
    ) -> None:
        for logical_path, source_digest in sorted(normalized_links):
            linked = database.execute(
                """INSERT INTO task_source_links(
                       task_id,logical_path,source_digest
                   ) VALUES (?,?,?)""",
                (task_id, logical_path, source_digest),
            ).rowcount
            if linked != 1:
                raise QueueOperationError("source_link_insert_failed")

    def enqueue(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
        source_links: tuple[tuple[str, str], ...] | list[tuple[str, str]] = (),
    ) -> str:
        _require_enqueue_arguments(kind, handler_version, priority, dedupe_key)
        normalized_links = _normalized_source_links(source_links)
        payload_bytes, input_hash = _validated_payload_bytes(payload)
        now = _utc_now()
        ready_at = _as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            self._delete_stale_source_fences(database)
            self._assert_payload_not_fenced(database, payload_bytes.decode("utf-8"))
            deduplicated = self._deduplicated_task_id(
                database, dedupe_key, kind, handler_version, payload_bytes, input_hash
            )
            if deduplicated is not None:
                return deduplicated
            self._insert_task_row(
                database,
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                now,
                ready_at,
            )
            self._insert_source_links(database, task_id, normalized_links)
        return task_id

    def claim_compile_request(self, payload, *, owner,
                              lease_seconds=DEFAULTS.queue_lease_seconds,
                              max_attempts=DEFAULTS.queue_max_attempts,
                              deadline=float('inf'), cancelled=None):
        """Select/create and claim one intent atomically, without a steal window."""
        _require_active(deadline, cancelled)
        _check_claim_arguments(owner, lease_seconds, max_attempts)
        payload_bytes, input_hash = _validated_payload_bytes(payload)
        with closing(self._connect(deadline=deadline)) as database, begin_immediate(database):
            task_id = self._compile_request_in(database, payload_bytes, input_hash)
            _require_active(deadline, cancelled)
            lease = self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                   max_attempts=max_attempts, task_filter=_claim_task_filter(task_id))
            _require_active(deadline, cancelled)
            return task_id, lease

    def _compile_request_in(self, database, payload_bytes, input_hash):
        self._delete_stale_source_fences(database)
        self._assert_payload_not_fenced(database, payload_bytes.decode('utf-8'))
        existing = _unfinished_compile_request(database, payload_bytes, input_hash)
        if existing is not None:
            return existing
        task_id = uuid.uuid4().hex
        now = _utc_now()
        self._insert_task_row(database, task_id, 'compile', 1, payload_bytes,
                              input_hash, None, 0, now, now)
        return task_id

    def ready_capture_intents_without_task(
        self, limit: int, *, after: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Ready intents that no task was ever created for, oldest first.

        This is the outbox query: a row that committed but was never
        dispatched. `recover_expired_leases` cannot see these because it
        recovers a task whose lease expired, and these have no task at all.
        Read-only by design — adoption decides what to do with the answer.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        cursor = _capture_scan_cursor(after)
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT intent.intent_id,intent.relative_path,
                          intent.intent_sha256,intent.byte_size,intent.updated_at
                   FROM capture_intents AS intent
                   LEFT JOIN capture_task_links AS link
                     ON link.intent_id=intent.intent_id
                   WHERE intent.publication_state='ready'
                     AND link.intent_id IS NULL
                     AND (intent.updated_at, intent.intent_id) > (?, ?)
                   ORDER BY intent.updated_at ASC, intent.intent_id ASC
                   LIMIT ?""",
                (*cursor, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_capture_intents(
        self, limit: int, older_than: str | None = None, *, after: tuple[str, str] | None = None,
    ) -> list[dict[str, object]]:
        """Intents still half-published, oldest first; read-only like the query above.

        A publisher killed between the pending write and `mark_capture_intent_ready`
        leaves one of these, and the ready-state query cannot see it. `older_than` is
        an ISO stamp: rows touched more recently belong to a publisher that may still
        be running. See
        `docs/research/2026-09-17-a-publication-that-stopped-half-way-is-finished.md`.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        cursor = _capture_scan_cursor(after)
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT intent_id,relative_path,intent_sha256,byte_size,updated_at
                   FROM capture_intents
                   WHERE publication_state='pending'
                     AND (? IS NULL OR updated_at < ?)
                     AND (updated_at, intent_id) > (?, ?)
                   ORDER BY updated_at ASC, intent_id ASC
                   LIMIT ?""",
                (older_than, older_than, *cursor, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def publish_capture_intent(
        self,
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        _check_capture_intent_descriptor(
            intent_id, intent_path, intent_sha256, byte_size
        )
        now = _timestamp(_utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                _check_ready_intent_identity(
                    existing, intent_path, intent_sha256, byte_size
                )
                return
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'ready',?)""",
                (intent_id, intent_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")

    def index_capture_intent_pending(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> str:
        _require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        _require_capture_intent_path(ready_path, "ready")
        now = _timestamp(_utc_now())
        with closing(self._connect()) as database, begin_immediate(database):
            existing = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            if existing is not None:
                return _existing_capture_publication_state(
                    existing,
                    pending_path=pending_path,
                    ready_path=ready_path,
                    intent_sha256=intent_sha256,
                    byte_size=byte_size,
                )
            inserted = database.execute(
                """INSERT INTO capture_intents(
                       intent_id,relative_path,intent_sha256,byte_size,
                       publication_state,updated_at
                   ) VALUES (?,?,?,?,'pending',?)""",
                (intent_id, pending_path, intent_sha256, byte_size, now),
            ).rowcount
            if inserted != 1:
                raise QueueOperationError("capture_intent_insert_failed")
        return "pending"

    def mark_capture_intent_ready(
        self,
        *,
        intent_id: str,
        pending_path: str,
        ready_path: str,
        intent_sha256: str,
        byte_size: int,
    ) -> None:
        _require_capture_intent_descriptor(
            intent_id, pending_path, intent_sha256, byte_size
        )
        _require_capture_intent_path(ready_path, "ready")
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute(
                "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
            state = _existing_capture_publication_state(
                row,
                pending_path=pending_path,
                ready_path=ready_path,
                intent_sha256=intent_sha256,
                byte_size=byte_size,
            )
            if state == "ready":
                return
            updated = database.execute(
                """UPDATE capture_intents
                   SET relative_path=?,publication_state='ready',updated_at=?
                   WHERE intent_id=? AND relative_path=?
                     AND publication_state='pending'""",
                (ready_path, _timestamp(_utc_now()), intent_id, pending_path),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("capture_intent_conflict")

    def enqueue_capture_task_replay_safe(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
    ) -> CaptureTaskBinding:
        payload_bytes = canonical_json_bytes(redact_structure(dict(payload)))
        dedupe_key = f"capture:{intent_id}:{handler_version}"
        existing = self._capture_replay_binding(
            intent_id=intent_id,
            kind=kind,
            handler_version=handler_version,
            payload_bytes=payload_bytes,
            dedupe_key=dedupe_key,
        )
        if existing is not None:
            return existing
        return self.enqueue_capture_task(
            kind,
            handler_version,
            payload,
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            capture_fence=capture_fence,
            owner=owner,
            dedupe_key=dedupe_key,
        )

    def _capture_replay_binding(
        self,
        *,
        intent_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        dedupe_key: str,
    ) -> CaptureTaskBinding | None:
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT task.*,link.intent_sha256
                   FROM capture_task_links AS link
                   JOIN tasks AS task ON task.id=link.task_id
                   WHERE link.intent_id=?""",
                (intent_id,),
            ).fetchall()
            if not rows:
                return None
            if len(rows) != 1:
                raise QueueOperationError("capture_link_conflicted")
            _require_capture_replay_task(
                rows[0], kind, handler_version, payload_bytes, dedupe_key
            )
            return self.active_capture_binding(database, str(rows[0]["id"]))

    @staticmethod
    def _insert_capture_link(
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        intent_sha256: str,
        handler_version: int,
        link_digest: str,
        created_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO capture_task_links(
                   task_id,intent_id,intent_sha256,handler_version,
                   link_digest,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                task_id,
                intent_id,
                intent_sha256,
                handler_version,
                link_digest,
                created_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_insert_failed")

    def _require_live_capture_fence(
        self, intent_id: str, capture_fence: object, owner: OwnerLease
    ) -> None:
        """The capture fence must still be the one this owner holds."""
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            try:
                registry.require(coordinator_database, owner)
            except Exception as exc:
                raise QueueOperationError("intent_fence_lost") from exc
            fence_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences
                   WHERE intent_id=? AND mode='capture' AND token=?
                     AND fencing_epoch=? AND canonical_role=?
                     AND canonical_scope=? AND canonical_actor_id=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    intent_id,
                    capture_fence.token,
                    capture_fence.epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(_utc_now()),
                ),
            ).fetchone()
        if fence_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _insert_capture_task_row(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        kind: str,
        handler_version: int,
        payload_bytes: bytes,
        input_hash: str,
        dedupe_key: str | None,
        priority: int,
        now: datetime,
        ready_at: datetime,
        intent_path: str,
        intent_id: str,
        intent_sha256: str,
        link_digest: str,
    ) -> None:
        self._require_capture_insert_allowed(
            database,
            intent_id=intent_id,
            intent_path=intent_path,
            intent_sha256=intent_sha256,
            dedupe_key=dedupe_key,
        )
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,dedupe_key,
                   state,priority,created_at,updated_at,available_at
               ) VALUES (?,?,?,?,?,?,'ready',?,?,?,?)""",
            (
                task_id,
                kind,
                handler_version,
                payload_bytes,
                input_hash,
                dedupe_key,
                priority,
                _timestamp(now),
                _timestamp(now),
                _timestamp(ready_at),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("enqueue_failed")
        self._insert_capture_link(
            database,
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            link_digest=link_digest,
            created_at=_timestamp(now),
        )

    def enqueue_capture_task(
        self,
        kind: str,
        handler_version: int,
        payload: Mapping[str, object],
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        capture_fence: object,
        owner: OwnerLease,
        priority: int = 0,
        available_at: datetime | None = None,
        dedupe_key: str | None = None,
    ) -> CaptureTaskBinding:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_capture_identity(intent_id, intent_sha256, intent_path)
        if not isinstance(owner, OwnerLease) or owner.role != "capture":
            raise ValueError("capture enqueue requires a capture owner")
        _require_capture_fence(capture_fence, intent_id, owner, IntentFence)
        self._require_live_capture_fence(intent_id, capture_fence, owner)
        _require_enqueue_arguments(kind, handler_version, priority, None)
        payload_bytes, input_hash = _validated_capture_payload(payload)
        now = _utc_now()
        ready_at = _as_utc(available_at or now)
        task_id = uuid.uuid4().hex
        link_digest = sha256_bytes(
            canonical_json_bytes(
                _capture_link_record(
                    task_id, intent_id, intent_sha256, handler_version
                )
            )
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._insert_capture_task_row(
                database,
                task_id=task_id,
                kind=kind,
                handler_version=handler_version,
                payload_bytes=payload_bytes,
                input_hash=input_hash,
                dedupe_key=dedupe_key,
                priority=priority,
                now=now,
                ready_at=ready_at,
                intent_path=intent_path,
                intent_id=intent_id,
                intent_sha256=intent_sha256,
                link_digest=link_digest,
            )
        return CaptureTaskBinding(
            task_id=task_id,
            intent_id=intent_id,
            intent_sha256=intent_sha256,
            handler_version=handler_version,
            active_digest=link_digest,
            seal_digest=None,
        )

    @staticmethod
    def _validate_task_fence_owner(
        owner: OwnerLease, mode: Literal["worker", "queue-operator"]
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(owner, OwnerLease):
            raise TypeError("owner must be an OwnerLease")
        allowed = {
            "worker": {"queue-worker", "compile", "doctor", "nightly", "weekly"},
            "queue-operator": {"queue-operator", "repair"},
        }
        if mode not in allowed or owner.role not in allowed[mode]:
            raise ValueError("owner cannot acquire the requested task fence")

    @staticmethod
    def _require_owner_projection(
        database: sqlite3.Connection, owner: OwnerLease, now: datetime
    ) -> None:
        """Refuse a fence whose owner is no longer projected into the queue."""
        projection = database.execute(
            """SELECT 1 FROM queue_ownership
               WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                 AND owner_token=? AND fencing_epoch=? AND process_id=?
                 AND process_start_identity=? AND expires_at>?""",
            (
                owner.actor_id,
                owner.role,
                owner.scope,
                owner.token,
                owner.epoch,
                owner.process.pid,
                owner.process.start_identity,
                _timestamp(now),
            ),
        ).fetchone()
        if projection is None:
            raise QueueOperationError("queue_owner_fence_lost")

    @staticmethod
    def _require_fenceable_task(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        """Refuse a fence over a task that is missing or already fenced."""
        if database.execute(
            "SELECT 1 FROM tasks WHERE id=?", (task_id,)
        ).fetchone() is None:
            raise KeyError(task_id)
        if database.execute(
            "SELECT 1 FROM task_fences WHERE task_id=?", (task_id,)
        ).fetchone() is not None:
            raise QueueOperationError("task_fenced")

    @staticmethod
    def _next_task_fence_epoch(database: sqlite3.Connection, task_id: str) -> int:
        """The next fencing epoch for this task, taken atomically."""
        return int(
            database.execute(
                """INSERT INTO task_fence_epochs(task_id,last_epoch) VALUES (?,1)
                   ON CONFLICT(task_id) DO UPDATE
                   SET last_epoch=task_fence_epochs.last_epoch+1
                   RETURNING last_epoch""",
                (task_id,),
            ).fetchone()[0]
        )

    @staticmethod
    def _insert_task_fence(
        database: sqlite3.Connection, values: tuple[object, ...]
    ) -> None:
        """Record the fence row, refusing when it did not land."""
        inserted = database.execute(
            """INSERT INTO task_fences(
                   task_id,mode,token,fencing_epoch,canonical_role,
                   canonical_scope,canonical_actor_id,canonical_owner_token,
                   canonical_fencing_epoch,process_id,process_start_identity,
                   heartbeat_at,expires_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("task_fence_failed")

    def acquire_task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> TaskFence:
        self._validate_task_fence_owner(owner, mode)
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be non-empty")
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        token = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = _utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=TASK_FENCE_SECONDS))
            self._require_owner_projection(database, owner, now)
            self._require_fenceable_task(database, task_id)
            epoch = self._next_task_fence_epoch(database, task_id)
            self._insert_task_fence(
                database,
                (
                    task_id,
                    mode,
                    token,
                    epoch,
                    owner.role,
                    owner.scope,
                    owner.actor_id,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(now),
                    _timestamp(expires_at),
                ),
            )
        return TaskFence(task_id, mode, token, epoch, owner, expires_at)

    def release_task_fence(self, fence: TaskFence) -> None:
        if not isinstance(fence, TaskFence):
            raise TypeError("fence must be a TaskFence")
        with closing(self._connect()) as database, begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM task_fences
                   WHERE task_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_role=? AND canonical_scope=?
                     AND canonical_actor_id=? AND canonical_owner_token=?
                     AND canonical_fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    fence.task_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    fence.owner.role,
                    fence.owner.scope,
                    fence.owner.actor_id,
                    fence.owner.token,
                    fence.owner.epoch,
                    fence.owner.process.pid,
                    fence.owner.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("task_fence_lost")

    def heartbeat_task_fence(self, fence: TaskFence, owner: OwnerLease) -> None:
        """Push a live task fence out by its own length, capped by its owner's lease.

        `owner` is the fence's owner as last renewed. See
        `docs/research/2026-09-14-a-capture-keeps-its-claim-while-it-asks.md`.
        """
        if not isinstance(fence, TaskFence):
            raise TypeError("fence must be a TaskFence")
        with closing(self._connect()) as database, begin_immediate(database):
            # Judge authority after SQLite has admitted this write transaction.
            now = _utc_now()
            expires_at = min(owner.expires_at, now + timedelta(seconds=TASK_FENCE_SECONDS))
            self._require_owner_projection(database, owner, now)
            renewed = database.execute(
                """UPDATE task_fences SET heartbeat_at=?, expires_at=?
                   WHERE task_id=? AND mode=? AND token=? AND fencing_epoch=?
                     AND canonical_owner_token=? AND canonical_fencing_epoch=?
                     AND process_id=? AND process_start_identity=? AND expires_at>?""",
                (
                    _timestamp(now),
                    _timestamp(expires_at),
                    fence.task_id,
                    fence.mode,
                    fence.token,
                    fence.epoch,
                    owner.token,
                    owner.epoch,
                    owner.process.pid,
                    owner.process.start_identity,
                    _timestamp(now),
                ),
            ).rowcount
            if renewed != 1:
                raise QueueOperationError("task_fence_lost")

    @contextmanager
    def task_fence(
        self,
        task_id: str,
        *,
        mode: Literal["worker", "queue-operator"],
        owner: OwnerLease,
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode=mode, owner=owner)
        try:
            yield fence
        finally:
            self.release_task_fence(fence)

    def _intent_coordinator(self):
        """The coordinator that pairs with this queue: adopted, or the candidate."""
        from markdown_transaction import MarkdownCoordinator, active_markdown_coordinator

        path = self.coordinator_path
        if path is None:
            path = self.state_root / "run" / "markdown-transactions-v3.candidate.sqlite3"
        if self.vault is not None and path == self.state_root / "run" / "markdown-transactions-v3.sqlite3":
            return active_markdown_coordinator(self.vault, self.state_root)
        return MarkdownCoordinator._from_v3_candidate(path, state_root=self.state_root)

    @contextmanager
    def _corrupt_intent_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[tuple[CaptureTaskBinding | None, object | None]]:
        try:
            binding = self.active_capture_binding(None, task_id)
        except QueueOperationError:
            yield None, None
            return
        if binding.intent_id is None:
            yield binding, None
            return
        coordinator = self._intent_coordinator()
        fence = coordinator.acquire_intent_fence(
            binding.intent_id, mode="operator", owner=owner
        )
        try:
            yield binding, fence
        finally:
            coordinator.release_intent_fence(fence)

    def active_capture_binding(
        self, connection: sqlite3.Connection | None, task_id: str
    ) -> CaptureTaskBinding:
        owned = connection is None
        database = self._connect() if owned else connection
        try:
            return _capture_binding_of(database, task_id)
        finally:
            if owned:
                database.close()

    def append_capture_link_resolution(
        self,
        task_id: str,
        *,
        supersedes_digest: str | None,
        observed: Mapping[str, object],
        selected_intent: Mapping[str, object] | None,
        owner: OwnerLease,
        reason: str,
    ) -> CaptureTaskBinding:
        from operational_ownership import OwnerLease, current_actor_identity

        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("capture link resolution requires a repair owner")
        _check_resolution_reason(reason)
        actor = current_actor_identity()
        now = _utc_now()
        record = _capture_resolution_record(
            task_id=task_id,
            supersedes_digest=supersedes_digest,
            observed=observed,
            selected_intent=selected_intent,
            actor=actor,
            reason=reason,
            now=now,
        )
        resolution_digest = sha256_bytes(canonical_json_bytes(record))
        observed_json = canonical_json_bytes(record["observed"])
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with closing(self._connect()) as database, begin_immediate(database):
            _require_repair_projection(database, owner, now)
            _require_unfenced_unsealed_task(database, task_id)
            active = self.active_capture_binding(database, task_id)
            if supersedes_digest != active.active_digest:
                raise QueueOperationError("capture_link_conflicted")
            selected_id = _selected_intent_id(database, selected_intent)
            _insert_link_resolution(
                database,
                resolution_digest=resolution_digest,
                task_id=task_id,
                supersedes_digest=supersedes_digest,
                observed_json=observed_json,
                selected_id=selected_id,
                actor=actor,
                reason=reason,
                now=now,
            )
            return self.active_capture_binding(database, task_id)

    def seal_capture_binding(
        self,
        task_id: str,
        *,
        consumer_kind: Literal["transaction", "terminal", "corrupt-disposition"],
        consumer_id: str,
        active_link_digest: str,
        before_side_effect: Callable[[], None] | None = None,
    ) -> CaptureTaskBinding:
        _check_consumer_kind(consumer_kind)
        _check_consumer_id(consumer_id)
        now = _utc_now()
        seal_digest = _capture_seal_digest(
            task_id=task_id,
            consumer_kind=consumer_kind,
            consumer_id=consumer_id,
            active_link_digest=active_link_digest,
        )
        with closing(self._connect()) as database, begin_immediate(database):
            active = self.active_capture_binding(database, task_id)
            if active.active_digest != active_link_digest:
                raise QueueOperationError("capture_link_conflicted")
            _record_capture_seal(
                database,
                task_id=task_id,
                consumer_kind=consumer_kind,
                consumer_id=consumer_id,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
        if before_side_effect is not None:
            before_side_effect()
        return CaptureTaskBinding(
            task_id=active.task_id,
            intent_id=active.intent_id,
            intent_sha256=active.intent_sha256,
            handler_version=active.handler_version,
            active_digest=active.active_digest,
            seal_digest=seal_digest,
        )

    @staticmethod
    def _insert_semantic_decision(
        database: sqlite3.Connection,
        *,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        published_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO semantic_decisions(
                   intent_id,stage,decision_path,decision_sha256,
                   active_link_digest,publication_state,published_at
               ) VALUES (?,?,?,?,?,'published',?)""",
            (
                intent_id,
                stage,
                decision_path,
                decision_sha256,
                active_link_digest,
                published_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("semantic_decision_insert_failed")

    def indexed_capture_decision(
        self,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        active_link_digest: str,
    ) -> SemanticDecision | None:
        _require_capture_decision_path(
            _capture_decision_relative_path(intent_id, stage), intent_id, stage
        )
        with closing(self._connect()) as database:
            row = database.execute(
                "SELECT * FROM semantic_decisions WHERE intent_id=? AND stage=?",
                (intent_id, stage),
            ).fetchone()
            if row is None:
                return None
            sealer = self._require_decision_for_task(
                database, row, task_id, intent_id=intent_id, stage=stage,
                active_link_digest=active_link_digest,
            )
            published_at = _indexed_capture_decision_published_at(row)
        decision_path, decision_sha256 = _read_indexed_capture_decision(
            self.state_root, row
        )
        return SemanticDecision(
            task_id=str(sealer["task_id"]),
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=str(row["active_link_digest"]),
            seal_digest=str(sealer["seal_digest"]),
            published_at=published_at,
        )

    def _require_decision_for_task(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        task_id: str,
        *,
        intent_id: str,
        stage: str,
        active_link_digest: str,
    ) -> sqlite3.Row:
        """The seal behind this decision, which this task made or inherited by redrive."""
        active = self.active_capture_binding(database, task_id)
        sealer = _decision_sealer(database, row, intent_id, stage)
        if sealer["task_id"] == task_id:
            _require_indexed_capture_decision(
                row, active, intent_id=intent_id, stage=stage, active_link_digest=active_link_digest
            )
            return sealer
        _require_inherited_capture_decision(
            database, sealer, active, intent_id=intent_id, stage=stage,
            active_link_digest=active_link_digest,
        )
        return sealer

    def _read_semantic_decision_bytes(
        self, decision_path: str, decision_sha256: str, intent_id: str, stage: str
    ) -> None:
        _require_capture_decision_path(decision_path, intent_id, stage)
        data = read_runtime_bytes(
            self.state_root / decision_path,
            self.state_root,
            max_bytes=MAX_CAPTURE_DECISION_BYTES,
            owner_only=True,
        )
        if sha256_bytes(data) != decision_sha256:
            raise QueueOperationError("semantic_decision_conflict")

    def _require_live_worker_intent(
        self, intent_id: str, intent_fence: object, owner: OwnerLease, now: datetime
    ) -> None:
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            intent_row = coordinator_database.execute(
                """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
                   AND token=? AND fencing_epoch=? AND canonical_owner_token=?
                   AND canonical_fencing_epoch=? AND expires_at>?""",
                (
                    intent_id,
                    intent_fence.token,
                    intent_fence.epoch,
                    owner.token,
                    owner.epoch,
                    _timestamp(now),
                ),
            ).fetchone()
        if intent_row is None:
            raise QueueOperationError("intent_fence_lost")

    def _require_live_task_fence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        projection = database.execute(
            """SELECT 1 FROM queue_ownership WHERE owner_token=?
               AND fencing_epoch=? AND expires_at>?""",
            (owner.token, owner.epoch, _timestamp(now)),
        ).fetchone()
        fence_row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if projection is None or fence_row is None:
            raise QueueOperationError("task_fence_lost")

    def _require_capture_insert_allowed(
        self,
        database: sqlite3.Connection,
        *,
        intent_id: str,
        intent_path: str,
        intent_sha256: str,
        dedupe_key: str | None,
    ) -> None:
        """The intent this task claims is ready, and the dedupe key is free."""
        intent = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (intent_id,)
        ).fetchone()
        if not _matching_capture_intent(intent, intent_path, intent_sha256):
            raise QueueOperationError("capture_intent_conflict")
        if dedupe_key is not None and database.execute(
            "SELECT 1 FROM tasks WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone() is not None:
            raise QueueOperationError("dedupe_conflict")

    def _require_unsealed_capture_link(
        self,
        database: sqlite3.Connection,
        task_id: str,
        intent_id: str,
        active_link_digest: str,
    ) -> None:
        active = self.active_capture_binding(database, task_id)
        if (
            active.intent_id != intent_id
            or active.active_digest != active_link_digest
            or active.seal_digest is not None
        ):
            raise QueueOperationError("capture_link_conflicted")

    def _seal_semantic_decision(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        intent_id: str,
        stage: str,
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        seal_digest: str,
        now: datetime,
    ) -> None:
        existing = database.execute(
            """SELECT seal.*, decision.decision_path,
                      decision.decision_sha256,decision.active_link_digest
               FROM capture_task_link_seals AS seal
               LEFT JOIN semantic_decisions AS decision
                 ON decision.intent_id=? AND decision.stage=?
               WHERE seal.task_id=?""",
            (intent_id, stage, task_id),
        ).fetchone()
        if existing is not None:
            _require_semantic_seal_matches(
                existing,
                intent_id,
                stage,
                seal_digest,
                decision_path,
                decision_sha256,
                active_link_digest,
            )
            return
        inserted = database.execute(
            """INSERT INTO capture_task_link_seals(
                   task_id,active_digest,consumer_kind,consumer_id,
                   seal_digest,sealed_at
               ) VALUES (?,?,'semantic-decision',?,?,?)""",
            (
                task_id,
                active_link_digest,
                f"{intent_id}:{stage}",
                seal_digest,
                _timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("capture_link_seal_failed")
        self._insert_semantic_decision(
            database,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            published_at=_timestamp(now),
        )

    def _published_decision_time(
        self, database: sqlite3.Connection, intent_id: str, stage: str
    ) -> datetime:
        published = database.execute(
            """SELECT published_at FROM semantic_decisions
               WHERE intent_id=? AND stage=?""",
            (intent_id, stage),
        ).fetchone()
        if published is None:
            raise QueueOperationError("semantic_decision_conflict")
        published_at = _parse_timestamp(str(published["published_at"]))
        if published_at is None:
            raise QueueOperationError("semantic_decision_conflict")
        return published_at

    def publish_semantic_decision(
        self,
        coordinator: object,
        *,
        task_id: str,
        intent_id: str,
        stage: Literal["flush", "feedback", "feedback-verify"],
        decision_path: str,
        decision_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> SemanticDecision:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_semantic_decision_fences(
            task_id,
            intent_id,
            stage,
            decision_sha256,
            task_fence,
            intent_fence,
            owner,
            IntentFence,
            OwnerLease,
        )
        self._read_semantic_decision_bytes(
            decision_path, decision_sha256, intent_id, stage
        )
        now = _utc_now()
        self._require_live_worker_intent(intent_id, intent_fence, owner, now)
        seal_digest = _capture_semantic_seal_digest(
            task_id, intent_id, stage, active_link_digest
        )
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_live_task_fence(database, task_id, task_fence, owner, now)
            self._require_unsealed_capture_link(
                database, task_id, intent_id, active_link_digest
            )
            self._seal_semantic_decision(
                database,
                task_id=task_id,
                intent_id=intent_id,
                stage=stage,
                decision_path=decision_path,
                decision_sha256=decision_sha256,
                active_link_digest=active_link_digest,
                seal_digest=seal_digest,
                now=now,
            )
            published_at = self._published_decision_time(database, intent_id, stage)
        return SemanticDecision(
            task_id=task_id,
            intent_id=intent_id,
            stage=stage,
            decision_path=decision_path,
            decision_sha256=decision_sha256,
            active_link_digest=active_link_digest,
            seal_digest=seal_digest,
            published_at=published_at,
        )

    def adopt_semantic_decision(
        self,
        decision: SemanticDecision,
        *,
        task_id: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> None:
        """Seal this redrive's link to the decision an ancestor already published.

        Writing Markdown needs the working task's own sealed link, and a redrive has
        none: its link was re-signed for it. The seal names the same decision and
        adds no second one (audit 2026-09-27 B-14).
        """
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        _require_semantic_decision_fences(
            task_id, decision.intent_id, decision.stage, decision.decision_sha256,
            task_fence, intent_fence, owner, IntentFence, OwnerLease,
        )
        now = _utc_now()
        self._require_live_worker_intent(decision.intent_id, intent_fence, owner, now)
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_live_task_fence(database, task_id, task_fence, owner, now)
            self._require_inherited_decision_row(database, decision, task_id, active_link_digest)
            self._insert_inherited_seal(database, decision, task_id, active_link_digest, now)

    def sealed_ancestor_bindings(
        self, task_id: str, intent_id: str, stage: str
    ) -> tuple[dict[str, str], ...]:
        """The capture bindings this task's redrive ancestors sealed to the same decision.

        A block an ancestor committed before it died carries that ancestor's binding;
        its redrive replays the same operation and must recognise the block as its
        own family's, not as a conflict (audit 2026-09-27 B-14).
        """
        with closing(self._connect()) as database:
            seals = [
                _decision_seal_of(database, ancestor, intent_id, stage)
                for ancestor in sorted(_redrive_ancestors(database, task_id))
            ]
        return tuple(_sealed_binding(seal, intent_id) for seal in seals if seal is not None)

    def _require_inherited_decision_row(
        self,
        database: sqlite3.Connection,
        decision: SemanticDecision,
        task_id: str,
        active_link_digest: str,
    ) -> None:
        row = database.execute(
            "SELECT * FROM semantic_decisions WHERE intent_id=? AND stage=?",
            (decision.intent_id, decision.stage),
        ).fetchone()
        if row is None or row["decision_sha256"] != decision.decision_sha256:
            raise QueueOperationError("semantic_decision_conflict")
        self._require_decision_for_task(
            database, row, task_id, intent_id=decision.intent_id, stage=decision.stage,
            active_link_digest=active_link_digest,
        )

    @staticmethod
    def _insert_inherited_seal(
        database: sqlite3.Connection,
        decision: SemanticDecision,
        task_id: str,
        active_link_digest: str,
        now: datetime,
    ) -> None:
        """Insert this task's seal once; a retry of the same task finds it already there."""
        seal_digest = _capture_semantic_seal_digest(
            task_id, decision.intent_id, decision.stage, active_link_digest
        )
        database.execute(
            """INSERT OR IGNORE INTO capture_task_link_seals(
                   task_id,active_digest,consumer_kind,consumer_id,seal_digest,sealed_at
               ) VALUES (?,?,'semantic-decision',?,?,?)""",
            (
                task_id,
                active_link_digest,
                f"{decision.intent_id}:{decision.stage}",
                seal_digest,
                _timestamp(now),
            ),
        )

    def _read_capture_terminal(
        self, terminal_path: str, terminal_sha256: str
    ) -> tuple[dict[str, object], bytes]:
        data = read_runtime_bytes(
            self.state_root / terminal_path,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        if sha256_bytes(data) != terminal_sha256:
            raise QueueOperationError("capture_terminal_conflict")
        value = json.loads(data.decode("utf-8", errors="strict"))
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, data

    def _capture_terminal_decisions(
        self, database: sqlite3.Connection, intent_id: str
    ) -> list[dict[str, str]]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256 FROM semantic_decisions
               WHERE intent_id=? ORDER BY stage""",
            (intent_id,),
        ).fetchall()
        result = []
        for row in rows:
            data = read_runtime_bytes(
                self.state_root / str(row["decision_path"]),
                self.state_root,
                max_bytes=MAX_CAPTURE_DECISION_BYTES,
                owner_only=True,
            )
            if sha256_bytes(data) != row["decision_sha256"]:
                raise QueueOperationError("semantic_decision_conflict")
            result.append(
                {
                    "stage": str(row["stage"]),
                    "decision_path": str(row["decision_path"]),
                    "decision_sha256": str(row["decision_sha256"]),
                }
            )
        return result

    @staticmethod
    def _require_capture_terminal_disposition(
        record: Mapping[str, object], decisions: list[dict[str, str]]
    ) -> None:
        disposition = record.get("disposition")
        if not isinstance(disposition, dict):
            raise QueueOperationError("capture_terminal_invalid")
        required = _capture_terminal_disposition_fields(disposition.get("kind"))
        decision_digests = {item["decision_sha256"] for item in decisions}
        actual = (set(disposition), disposition.get("decision_sha256") in decision_digests)
        expected = (required, True)
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")

    def _require_capture_terminal_record(
        self,
        record: Mapping[str, object],
        *,
        binding: CaptureTaskBinding,
        decisions: list[dict[str, str]],
    ) -> CaptureTaskBinding:
        expected_keys = {
            "schema_version",
            "intent_id",
            "intent_sha256",
            "semantic_decisions",
            "processing_binding",
            "disposition",
        }
        processing = record.get("processing_binding")
        identity = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            record.get("semantic_decisions"),
        )
        expected_identity = (
            expected_keys,
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            decisions,
        )
        if identity != expected_identity or not isinstance(processing, dict):
            raise QueueOperationError("capture_terminal_invalid")
        proven = self._terminal_proof_binding(record, binding)
        self._require_capture_terminal_disposition(record, decisions)
        self._require_versioned_capture_proof(proven, record)
        return proven

    @staticmethod
    def _terminal_processing_of(binding: CaptureTaskBinding) -> dict[str, str]:
        return {"kind": "task", "task_id": binding.task_id, "active_link_digest": binding.active_digest}

    def _terminal_proof_binding(self, record: Mapping[str, object], binding: CaptureTaskBinding) -> CaptureTaskBinding:
        processing = record.get("processing_binding")
        if processing == self._terminal_processing_of(binding):
            return binding
        if not isinstance(processing, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return self._inherited_terminal_binding(processing, binding)

    def _inherited_terminal_binding(self, processing: dict, binding: CaptureTaskBinding) -> CaptureTaskBinding:
        task_id = processing.get("task_id")
        with closing(self._connect()) as database:
            ancestors = _redrive_ancestors(database, binding.task_id)
            if not isinstance(task_id, str) or task_id not in ancestors:
                raise QueueOperationError("capture_terminal_invalid")
            parent = self.active_capture_binding(database, task_id)
        self._require_terminal_ancestor(parent, binding, processing)
        return parent

    def _require_terminal_ancestor(self, parent: CaptureTaskBinding, child: CaptureTaskBinding, processing: dict) -> None:
        actual = (parent.intent_id, parent.intent_sha256, parent.handler_version, parent.seal_digest is not None, processing)
        expected = (child.intent_id, child.intent_sha256, child.handler_version, True, self._terminal_processing_of(parent))
        if actual != expected:
            raise QueueOperationError("capture_terminal_invalid")

    def _adopt_terminal_seal(
        self, database: sqlite3.Connection, binding: CaptureTaskBinding, proven: CaptureTaskBinding, now: datetime,
    ) -> None:
        if binding.task_id == proven.task_id:
            return
        decision = self.indexed_capture_decision(
            task_id=proven.task_id, intent_id=proven.intent_id, stage="flush",
            active_link_digest=proven.active_digest,
        )
        if decision is None:
            raise QueueOperationError("capture_terminal_invalid")
        self._require_inherited_decision_row(database, decision, binding.task_id, binding.active_digest)
        self._insert_inherited_seal(database, decision, binding.task_id, binding.active_digest, now)

    def _require_versioned_capture_proof(self, binding: CaptureTaskBinding, record: Mapping[str, object]) -> None:
        if binding.handler_version == 1:
            return
        if binding.handler_version != 2:
            raise QueueOperationError("capture_terminal_handler_unsupported")
        from breadcrumb_terminal import require_terminal_proof

        try:
            require_terminal_proof(self, binding, record)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise QueueOperationError("breadcrumb_terminal_unverified") from exc

    def _require_capture_terminal_owner(
        self,
        *,
        lease: QueueLease,
        intent_id: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> None:
        from markdown_transaction import IntentFence
        from operational_ownership import OwnerLease

        actual = (
            isinstance(owner, OwnerLease),
            task_fence.task_id,
            task_fence.mode,
            task_fence.owner,
            isinstance(intent_fence, IntentFence),
            getattr(intent_fence, "intent_id", None),
            getattr(intent_fence, "mode", None),
            getattr(intent_fence, "owner", None),
        )
        expected = (True, lease.id, "worker", owner, True, intent_id, "worker", owner)
        if actual != expected:
            raise ValueError("capture terminal fences do not match")

    @staticmethod
    def _require_capture_terminal_task_fence(
        database: sqlite3.Connection,
        task_fence: TaskFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    @staticmethod
    def _require_capture_terminal_intent_fence(
        database: sqlite3.Connection,
        intent_fence: IntentFence,
        owner: OwnerLease,
        now: datetime,
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM intent_fences WHERE intent_id=? AND mode='worker'
               AND token=? AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                intent_fence.intent_id,
                intent_fence.token,
                intent_fence.epoch,
                owner.token,
                owner.epoch,
                _timestamp(now),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("intent_fence_lost")

    @staticmethod
    def _commit_capture_terminal_row(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        lease: QueueLease,
        *,
        terminal_path: str,
        terminal_sha256: str,
        intent_id: str,
        now: datetime,
    ) -> None:
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES (?,?,?,?, 'succeeded',NULL)""",
            (
                lease.id,
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
            ),
        )
        changed = database.execute(
            """UPDATE tasks SET state='succeeded',result_reference=?,result_sha256=?,
                   result_operation_id=?,error_code=NULL,updated_at=?,last_attempt_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   lease_heartbeat_at=NULL,attempt_started_at=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (
                terminal_path,
                terminal_sha256,
                f"capture-terminal:{intent_id}",
                _timestamp(now),
                _timestamp(now),
                lease.id,
                lease.token,
            ),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def complete_existing_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str | None:
        relative = f"run/queue-results/capture-{intent_id}.json"
        candidate = self.state_root / relative
        try:
            candidate.lstat()
        except FileNotFoundError:
            return None
        data = read_runtime_bytes(
            candidate,
            self.state_root,
            max_bytes=64 * 1024,
            owner_only=True,
        )
        return self.complete_capture_terminal(
            lease,
            intent_id=intent_id,
            terminal_path=relative,
            terminal_sha256=sha256_bytes(data),
            active_link_digest=active_link_digest,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )

    def complete_capture_terminal(
        self,
        lease: QueueLease,
        *,
        intent_id: str,
        terminal_path: str,
        terminal_sha256: str,
        active_link_digest: str,
        task_fence: TaskFence,
        intent_fence: object,
        owner: OwnerLease,
    ) -> str:
        _require_lower_sha256(intent_id, "intent_id")
        _require_lower_sha256(terminal_sha256, "terminal_sha256")
        _require_capture_terminal_path(terminal_path, intent_id)
        self._require_capture_terminal_owner(
            lease=lease,
            intent_id=intent_id,
            task_fence=task_fence,
            intent_fence=intent_fence,
            owner=owner,
        )
        record, _data = self._read_capture_terminal(terminal_path, terminal_sha256)
        registry = self.ownership_registry()
        now = _utc_now()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
            self._require_capture_terminal_intent_fence(
                coordinator_database, intent_fence, owner, now
            )
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            self._require_capture_terminal_task_fence(database, task_fence, owner, now)
            binding = self.active_capture_binding(database, lease.id)
            if binding.active_digest != active_link_digest or binding.intent_id != intent_id:
                raise QueueOperationError("capture_link_conflicted")
            decisions = self._capture_terminal_decisions(database, intent_id)
            proven = self._require_capture_terminal_record(record, binding=binding, decisions=decisions)
            self._adopt_terminal_seal(database, binding, proven, now)
            self._commit_capture_terminal_row(
                database,
                row,
                lease,
                terminal_path=terminal_path,
                terminal_sha256=terminal_sha256,
                intent_id=intent_id,
                now=now,
            )
        return terminal_path

    @staticmethod
    def _publish_corrupt_fixed_files(
        package: Path,
        *,
        payload: bytes,
        history: bytes,
        metadata: bytes,
    ) -> None:
        package.parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(package.parent, 0o700)
        if not package.exists():
            package.mkdir()
            _harden_owner_only(package, 0o700)
        for name, data in (
            ("payload.bin", payload),
            ("attempt-history.json", history),
            ("task-metadata.json", metadata),
        ):
            _write_durable_file(package / name, data)

    @staticmethod
    def _insert_corrupt_disposition(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        package_path: str,
        manifest_sha256: str,
        disposition_sha256: str,
        active_link_digest: str | None,
        disposed_at: str,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_dispositions(
                   task_id,operation_id,package_path,manifest_sha256,
                   disposition_sha256,active_link_digest,disposed_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                task_id,
                operation_id,
                package_path,
                manifest_sha256,
                disposition_sha256,
                active_link_digest,
                disposed_at,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_disposition_failed")

    @staticmethod
    def _corrupt_task_row(
        database: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row:
        """The task being quarantined, in a state that allows it."""
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["state"] not in {"dead", "quarantine_pending"}:
            raise QueueOperationError("corrupt_quarantine_state_invalid")
        return row

    @staticmethod
    def _corrupt_attempt_history(
        database: sqlite3.Connection, task_id: str
    ) -> bytes:
        rows = database.execute(
            """SELECT attempt,started_at,finished_at,outcome,error_code
               FROM attempt_history WHERE task_id=? ORDER BY sequence""",
            (task_id,),
        ).fetchall()
        return canonical_json_bytes([_attempt_history_entry(item) for item in rows])

    def _corrupt_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        reason: str,
        actor: str,
    ) -> _CorruptEvidence:
        """Everything the export is derived from, read under one transaction."""
        row = self._corrupt_task_row(database, task_id)
        raw = bytes(row["payload_blob"])
        raw_sha256 = validate_payload_blob(
            raw, row["input_hash"], parse=False
        ).input_hash
        history = self._corrupt_attempt_history(database, task_id)
        metadata = _corrupt_metadata(task_id, row)
        history_sha256 = sha256_bytes(history)
        metadata_sha256 = sha256_bytes(metadata)
        return _CorruptEvidence(
            raw=raw,
            history=history,
            metadata=metadata,
            raw_sha256=raw_sha256,
            history_sha256=history_sha256,
            metadata_sha256=metadata_sha256,
            disposition_key=_corrupt_disposition_key(
                actor=actor,
                history_sha256=history_sha256,
                metadata_sha256=metadata_sha256,
                raw_sha256=raw_sha256,
                reason=reason,
                task_id=task_id,
            ),
            lineage_generation=int(row["lineage_generation"]),
        )

    def _begin_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: _CorruptEvidence,
        operation_id: str,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> None:
        """Record a new export operation and move the task into quarantine."""
        inserted = database.execute(
            """INSERT INTO corrupt_export_operations(
                   operation_id,task_id,disposition_key,
                   task_fence_token_digest,task_fence_epoch,
                   intent_fence_digest,raw_sha256,history_sha256,
                   metadata_sha256,lineage_generation,cursor_task_id,
                   link_count,page_count,rolling_root,state,
                   actor_identity,reason,created_at,updated_at
               ) VALUES (?,?,?,?,?,NULL,?,?,?,?, '',0,0,?,'exporting',?,?,?,?)""",
            (
                operation_id,
                task_id,
                evidence.disposition_key,
                sha256_bytes(task_fence.token.encode("utf-8")),
                task_fence.epoch,
                evidence.raw_sha256,
                evidence.history_sha256,
                evidence.metadata_sha256,
                evidence.lineage_generation,
                sha256_bytes(b""),
                actor,
                reason,
                _timestamp(now),
                _timestamp(now),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_export_start_failed")
        changed = database.execute(
            """UPDATE tasks SET state='quarantine_pending',updated_at=?
               WHERE id=? AND state='dead'""",
            (_timestamp(now), task_id),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("corrupt_quarantine_start_failed")

    @staticmethod
    def _corrupt_export_conflict(
        database: sqlite3.Connection, existing: sqlite3.Row, task_id: str
    ) -> CorruptExportProgress:
        """A different export already ran; a disposed one is reported as done."""
        if existing["state"] == "disposed":
            disposition = database.execute(
                "SELECT * FROM corrupt_dispositions WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if disposition is not None:
                return CorruptExportProgress(
                    task_id=task_id,
                    operation_id=str(existing["operation_id"]),
                    state="quarantined",
                    pages_written=int(existing["page_count"]),
                    links_exported=int(existing["link_count"]),
                    complete=True,
                    code=None,
                )
        raise QueueOperationError("corrupt_export_conflict")

    def _open_corrupt_export(
        self,
        database: sqlite3.Connection,
        *,
        task_id: str,
        evidence: _CorruptEvidence,
        task_fence: object,
        actor: str,
        reason: str,
        now: datetime,
    ) -> tuple[str, str] | CorruptExportProgress:
        """The operation to continue, or the progress of one already finished."""
        operation_id = f"corrupt-export:{evidence.disposition_key}"
        existing = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if existing is None:
            self._begin_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                operation_id=operation_id,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
            return operation_id, evidence.disposition_key
        if (
            existing["operation_id"] == operation_id
            and existing["disposition_key"] == evidence.disposition_key
        ):
            return str(existing["operation_id"]), str(existing["disposition_key"])
        return self._corrupt_export_conflict(database, existing, task_id)

    def _advance_corrupt_export_once(
        self, task_id: str, operation_id: str, package: Path
    ) -> None:
        """Move an in-progress export one page forward, under its own lock."""
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
            if operation is None:
                raise QueueOperationError("corrupt_export_lost")
            if operation["state"] == "exporting":
                self._advance_corrupt_export(
                    database, operation, operation_id, task_id, package
                )

    def _corrupt_export_operation(self, task_id: str) -> sqlite3.Row:
        with closing(self._connect()) as database:
            operation = database.execute(
                "SELECT * FROM corrupt_export_operations WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if operation is None:
            raise QueueOperationError("corrupt_export_lost")
        return operation

    def _quarantine_corrupt_owned(
        self,
        task_id: str,
        *,
        reason: str,
        actor: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Export and dispose one corrupt task while holding every fence."""
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            evidence = self._corrupt_evidence(database, task_id, reason, actor)
            opened = self._open_corrupt_export(
                database,
                task_id=task_id,
                evidence=evidence,
                task_fence=task_fence,
                actor=actor,
                reason=reason,
                now=now,
            )
        if isinstance(opened, CorruptExportProgress):
            return opened
        operation_id, disposition_key = opened
        package = self.results_dir / f"corrupt-{disposition_key}"
        self._publish_corrupt_fixed_files(
            package,
            payload=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
        )
        self._advance_corrupt_export_once(task_id, operation_id, package)
        operation = self._corrupt_export_operation(task_id)
        if operation["state"] != "manifested":
            return CorruptExportProgress(
                task_id=task_id,
                operation_id=operation_id,
                state="quarantine_pending",
                pages_written=int(operation["page_count"]),
                links_exported=int(operation["link_count"]),
                complete=False,
                code=None,
            )
        return self._dispose_corrupt_export(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            package=package,
            raw=evidence.raw,
            history=evidence.history,
            metadata=evidence.metadata,
            actor=actor,
            reason=reason,
            task_fence=task_fence,
            capture_binding=capture_binding,
            intent_fence=intent_fence,
        )

    def quarantine_corrupt(
        self,
        task_id: str,
        *,
        reason: str,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptExportProgress:
        from operational_ownership import OwnerLease, current_actor_identity

        _require_active(deadline, cancelled)
        if not isinstance(owner, OwnerLease) or owner.role != "repair":
            raise ValueError("corrupt quarantine requires a repair owner")
        _check_quarantine_names(task_id, reason)
        registry = self.ownership_registry()
        actor = current_actor_identity()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self.task_fence(
            task_id, mode="queue-operator", owner=owner
        ) as task_fence, self._corrupt_intent_fence(
            task_id, owner=owner
        ) as (capture_binding, intent_fence):
            return self._quarantine_corrupt_owned(
                task_id,
                reason=reason,
                actor=actor,
                task_fence=task_fence,
                capture_binding=capture_binding,
                intent_fence=intent_fence,
            )

    def _dispose_corrupt_export(
        self,
        operation: sqlite3.Row,
        *,
        operation_id: str,
        task_id: str,
        disposition_key: str,
        package: Path,
        raw: bytes,
        history: bytes,
        metadata: bytes,
        actor: str,
        reason: str,
        task_fence: object,
        capture_binding: object,
        intent_fence: object,
    ) -> CorruptExportProgress:
        """Publish the manifest and disposition, then retire the task."""
        manifest = _corrupt_manifest(
            operation,
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
        )
        _write_durable_file(package / "manifest.json", canonical_json_bytes(manifest))
        if capture_binding is None or intent_fence is None:
            return _blocked_corrupt_progress(
                operation, task_id=task_id, operation_id=operation_id
            )
        sealed = self.seal_capture_binding(
            task_id,
            consumer_kind="corrupt-disposition",
            consumer_id=operation_id,
            active_link_digest=capture_binding.active_digest,
        )
        manifest_sha256 = sha256_bytes(
            _read_stable_owner_file(package / "manifest.json", 64 * 1024)
        )
        disposed_at = str(operation["created_at"])
        disposition_record = _corrupt_disposition_record(
            operation_id=operation_id,
            task_id=task_id,
            disposition_key=disposition_key,
            manifest_sha256=manifest_sha256,
            active_link_digest=sealed.active_digest,
            actor=actor,
            reason=reason,
            disposed_at=disposed_at,
        )
        disposition_bytes = canonical_json_bytes(disposition_record)
        _write_durable_file(package / "disposition.json", disposition_bytes)
        _verify_corrupt_package_bytes(
            package,
            raw=raw,
            history=history,
            metadata=metadata,
            disposition_bytes=disposition_bytes,
        )
        self._commit_corrupt_disposition(
            task_id=task_id,
            operation_id=operation_id,
            sealed=sealed,
            task_fence=task_fence,
            disposition_record=disposition_record,
            manifest_sha256=manifest_sha256,
            disposition_sha256=sha256_bytes(disposition_bytes),
            disposed_at=disposed_at,
        )
        return _quarantined_corrupt_progress(
            operation, task_id=task_id, operation_id=operation_id
        )

    def _commit_corrupt_disposition(
        self,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
        disposition_record: Mapping[str, object],
        manifest_sha256: str,
        disposition_sha256: str,
        disposed_at: str,
    ) -> None:
        """Retire the task, once everything it depends on still holds."""
        with closing(self._connect()) as database, begin_immediate(database):
            self._require_disposable_corrupt_state(
                database,
                task_id=task_id,
                operation_id=operation_id,
                sealed=sealed,
                task_fence=task_fence,
            )
            self._insert_corrupt_disposition(
                database,
                task_id=task_id,
                operation_id=operation_id,
                package_path=str(disposition_record["package_path"]),
                manifest_sha256=manifest_sha256,
                disposition_sha256=disposition_sha256,
                active_link_digest=sealed.active_digest,
                disposed_at=disposed_at,
            )
            self._retire_quarantined_task(database, task_id, operation_id)

    @staticmethod
    def _require_disposable_corrupt_state(
        database: sqlite3.Connection,
        *,
        task_id: str,
        operation_id: str,
        sealed: object,
        task_fence: object,
    ) -> None:
        """The task, the operation, the fence and the seal all still agree."""
        current_task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        current_operation = database.execute(
            "SELECT * FROM corrupt_export_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        current_fence = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND expires_at>?""",
            (task_id, task_fence.token, task_fence.epoch, _timestamp(_utc_now())),
        ).fetchone()
        current_seal = database.execute(
            """SELECT 1 FROM capture_task_link_seals
               WHERE task_id=? AND active_digest=? AND seal_digest=?
                 AND consumer_kind='corrupt-disposition' AND consumer_id=?""",
            (task_id, sealed.active_digest, sealed.seal_digest, operation_id),
        ).fetchone()
        incoming_count = database.execute(
            "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
        ).fetchone()[0]
        if current_task is None or current_operation is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        if current_fence is None or current_seal is None:
            raise QueueOperationError("corrupt_export_verification_failed")
        _require_disposable_rows(current_task, current_operation, incoming_count)

    @staticmethod
    def _retire_quarantined_task(
        database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> None:
        """Both rows move together, or neither does."""
        changed = database.execute(
            """UPDATE tasks SET state='quarantined',updated_at=?
               WHERE id=? AND state='quarantine_pending'""",
            (_timestamp(_utc_now()), task_id),
        ).rowcount
        advanced = database.execute(
            """UPDATE corrupt_export_operations
               SET state='disposed',updated_at=?
               WHERE operation_id=? AND state='manifested'""",
            (_timestamp(_utc_now()), operation_id),
        ).rowcount
        if (changed, advanced) != (1, 1):
            raise QueueOperationError("corrupt_disposition_failed")

    def _bounded_lineage_links(
        self,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
    ) -> list[dict[str, object]]:
        """As many lineage links as one bounded page holds.

        The bounds are inputs — at most 1000 candidate rows and at most 1 MiB of
        page bytes — so a page rebuilt after a rollback is byte-identical and
        `_write_durable_file` recognises its own work. See `docs/research/
        2026-09-18-a-page-that-is-rewritten-must-come-out-the-same.md`.
        """
        links: list[dict[str, object]] = []
        for child in candidates[:1000]:
            candidate = {
                "created_at": str(child["created_at"]),
                "input_hash": str(child["input_hash"]),
                "state": str(child["state"]),
                "task_id": str(child["id"]),
                "updated_at": str(child["updated_at"]),
            }
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "links": [*links, candidate],
                }
            )
            if len(prospective) > 1024 * 1024:
                break
            links.append(candidate)
        return links

    def _publish_lineage_page(
        self, package: Path, page_number: int, page_bytes: bytes
    ) -> None:
        """Write the page and prove the bytes on disk are the bytes we meant."""
        page_path = package / f"lineage-page-{page_number:08d}.json"
        _write_durable_file(page_path, page_bytes)
        if _read_stable_owner_file(page_path, 1024 * 1024) != page_bytes:
            raise QueueOperationError("corrupt_page_verification_failed")

    def _write_lineage_page(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        package: Path,
        links: list[dict[str, object]],
        candidates: list,
    ) -> None:
        page_number = int(operation["page_count"]) + 1
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "links": links,
            }
        )
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        self._publish_lineage_page(package, page_number, page_bytes)
        inserted = database.execute(
            """INSERT INTO corrupt_export_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   link_count,page_sha256,rolling_root
               ) VALUES (?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                links[0]["task_id"],
                links[-1]["task_id"],
                len(links),
                page_sha256,
                rolling_root,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_page_insert_failed")
        state = "manifested" if len(candidates) <= len(links) else "exporting"
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET cursor_task_id=?,link_count=link_count+?,
                   page_count=page_count+1,rolling_root=?,state=?,updated_at=?
               WHERE operation_id=? AND state='exporting'
                 AND cursor_task_id=? AND page_count=?""",
            (
                links[-1]["task_id"],
                len(links),
                rolling_root,
                state,
                _timestamp(_utc_now()),
                operation_id,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_page_fence_lost")

    def _finish_corrupt_export(
        self, database: sqlite3.Connection, operation_id: str
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_export_operations
               SET state='manifested',updated_at=?
               WHERE operation_id=? AND state='exporting'""",
            (_timestamp(_utc_now()), operation_id),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_manifest_fence_lost")

    def _advance_corrupt_export(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        task_id: str,
        package: Path,
    ) -> None:
        """One bounded step of the resumable lineage export."""
        candidates = database.execute(
            """SELECT id,state,created_at,updated_at,input_hash
               FROM tasks WHERE redrive_of=? AND id>?
               ORDER BY id LIMIT 1001""",
            (task_id, operation["cursor_task_id"]),
        ).fetchall()
        links = self._bounded_lineage_links(candidates, operation, operation_id)
        if links:
            self._write_lineage_page(
                database, operation, operation_id, package, links, candidates
            )
            return
        if not candidates:
            self._finish_corrupt_export(database, operation_id)

    def _bounded_purge_children(
        self,
        database: sqlite3.Connection,
        candidates: list,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> tuple[list[dict[str, object]], str | None]:
        """As many children as one bounded page holds, and what stopped it.

        Bounded by its inputs, never by a clock, so the page a retry rebuilds is
        the page already on disk. See `docs/research/
        2026-09-18-a-page-that-is-rewritten-must-come-out-the-same.md`.
        """
        children: list[dict[str, object]] = []
        for child in candidates[:1000]:
            blocker = self._corrupt_child_purge_blocker(database, child, now=now)
            if blocker is not None:
                return children, blocker
            descriptor = self._corrupt_purge_child_descriptor(database, child)
            prospective = canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "page_number": int(operation["page_count"]) + 1,
                    "previous_root": str(operation["rolling_root"]),
                    "before_generation": int(operation["expected_generation"]),
                    "after_generation": int(operation["expected_generation"])
                    + len(children)
                    + 1,
                    "children": [*children, descriptor],
                }
            )
            if len(prospective) > 1024 * 1024:
                return children, None
            children.append(descriptor)
        return children, None

    def _publish_purge_page(self, page_path: Path, page_bytes: bytes) -> bool:
        """False when another owner already wrote a different page here."""
        try:
            _write_durable_file(page_path, page_bytes)
        except QueueOperationError as exc:
            if exc.code != "durable_file_conflict":
                raise
            return False
        try:
            observed_page = _read_stable_owner_file(page_path, 1024 * 1024)
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError("corrupt_purge_page_invalid") from exc
        if observed_page != page_bytes:
            raise QueueOperationError("corrupt_purge_page_invalid")
        return True

    def _authorize_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-lineage',?,?,?)""",
            (task_id, operation_id, operation["purge_token"], _timestamp(now)),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _clear_purge_authorization(
        self, database: sqlite3.Connection, task_id: str
    ) -> None:
        cleared = database.execute(
            "DELETE FROM task_purge_authorizations WHERE task_id=?",
            (task_id,),
        ).rowcount
        if cleared != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _delete_purged_child(
        self,
        database: sqlite3.Connection,
        child_id: str,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        now: datetime,
    ) -> None:
        self._authorize_purge(database, child_id, operation, operation_id, now)
        self._delete_task_owned_evidence(database, child_id)
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=? AND redrive_of=?",
            (child_id, task_id),
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("corrupt_child_delete_failed")
        self._clear_purge_authorization(database, child_id)

    def _require_parent_generation(
        self, database: sqlite3.Connection, task_id: str, after_generation: int
    ) -> None:
        parent_generation = database.execute(
            "SELECT lineage_generation FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if parent_generation is None:
            raise QueueOperationError("corrupt_lineage_generation_changed")
        if int(parent_generation[0]) != after_generation:
            raise QueueOperationError("corrupt_lineage_generation_changed")

    def _advance_purge_operation(
        self,
        database: sqlite3.Connection,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        before_generation: int,
        after_generation: int,
        rolling_root: str,
    ) -> None:
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET expected_generation=?,cursor_task_id=?,page_count=page_count+1,
                   rolling_root=?,updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND cursor_task_id=? AND page_count=?""",
            (
                after_generation,
                children[-1]["task_id"],
                rolling_root,
                _timestamp(_utc_now()),
                operation_id,
                before_generation,
                operation["cursor_task_id"],
                operation["page_count"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_page_fence_lost")

    def _commit_purge_page(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation: sqlite3.Row,
        operation_id: str,
        children: list[dict[str, object]],
        package: Path,
        now: datetime,
    ) -> tuple[int, str, bool]:
        """(page number, rolling root, published) for one bounded purge page."""
        page_number = int(operation["page_count"]) + 1
        before_generation = int(operation["expected_generation"])
        after_generation = before_generation + len(children)
        page_bytes = canonical_json_bytes(
            {
                "operation_id": operation_id,
                "page_number": page_number,
                "previous_root": str(operation["rolling_root"]),
                "before_generation": before_generation,
                "after_generation": after_generation,
                "children": children,
            }
        )
        if len(page_bytes) > 1024 * 1024:
            raise QueueOperationError("corrupt_purge_page_too_large")
        page_sha256 = sha256_bytes(page_bytes)
        rolling_root = sha256_bytes(
            bytes.fromhex(str(operation["rolling_root"]))
            + bytes.fromhex(page_sha256)
        )
        page_path = package / f"purge-page-{page_number:08d}.json"
        if not self._publish_purge_page(page_path, page_bytes):
            return page_number, rolling_root, False
        self._insert_corrupt_purge_page(
            database,
            operation_id=operation_id,
            page_number=page_number,
            first_task_id=str(children[0]["task_id"]),
            last_task_id=str(children[-1]["task_id"]),
            deleted_link_count=len(children),
            page_sha256=page_sha256,
            rolling_root=rolling_root,
            expected_generation=after_generation,
        )
        self._authorize_purge(database, task_id, operation, operation_id, now)
        for child in children:
            self._delete_purged_child(
                database, str(child["task_id"]), task_id, operation, operation_id, now
            )
        self._clear_purge_authorization(database, task_id)
        self._require_parent_generation(database, task_id, after_generation)
        self._advance_purge_operation(
            database,
            operation,
            operation_id,
            children,
            before_generation,
            after_generation,
            rolling_root,
        )
        return page_number, rolling_root, True

    def _purge_page_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        task_fence: TaskFence,
    ) -> tuple[sqlite3.Row, int, CorruptPurgeProgress | None]:
        """The operation row and prior link count, or the progress to return."""
        operation, parent = _purge_operation_rows(database, task_id, operation_id)
        prior_links_deleted = int(
            database.execute(
                """SELECT COALESCE(SUM(deleted_link_count),0)
                   FROM corrupt_purge_pages WHERE operation_id=?""",
                (operation_id,),
            ).fetchone()[0]
        )
        pages = int(operation["page_count"])
        if operation["state"] != "purging" or parent["state"] != "purge_pending":
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="purge_pending",
                ),
            )
        self._require_corrupt_task_fence(database, task_fence)
        if int(parent["lineage_generation"]) != int(operation["expected_generation"]):
            return (
                operation,
                prior_links_deleted,
                _purge_progress(
                    task_id,
                    operation_id,
                    pages,
                    prior_links_deleted,
                    state="blocked",
                    code="corrupt_lineage_generation_changed",
                ),
            )
        return operation, prior_links_deleted, None

    def _purge_corrupt_lineage_page(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        owner: OwnerLease,
        task_fence: TaskFence,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        del owner
        _require_active(deadline, cancelled)
        with closing(self._connect()) as database, begin_immediate(database):
            operation, prior, early = self._purge_page_preconditions(
                database, task_id, operation_id, task_fence
            )
            if early is not None:
                return early
            return self._purge_one_lineage_page(
                database,
                task_id,
                operation_id=operation_id,
                operation=operation,
                prior=prior,
                package=package,
            )

    def _purge_one_lineage_page(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        operation_id: str,
        operation: sqlite3.Row,
        prior: int,
        package: Path,
    ) -> CorruptPurgeProgress:
        """One bounded page of children, inside the caller's transaction."""
        pages = int(operation["page_count"])
        candidates = database.execute(
            """SELECT * FROM tasks WHERE redrive_of=? AND id>?
               ORDER BY id LIMIT 1001""",
            (task_id, operation["cursor_task_id"]),
        ).fetchall()
        if not candidates:
            return _purge_progress(
                task_id, operation_id, pages, prior, state="purge_pending"
            )
        now = _utc_now()
        children, blocker_code = self._bounded_purge_children(
            database, candidates, operation, operation_id, now
        )
        if not children:
            return _purge_progress(
                task_id,
                operation_id,
                pages,
                prior,
                state=_blocked_or_pending(blocker_code),
                code=blocker_code,
            )
        return self._commit_purged_children(
            database,
            task_id,
            operation_id=operation_id,
            operation=operation,
            children=children,
            package=package,
            now=now,
            pages=pages,
            prior=prior,
            blocker_code=blocker_code,
        )

    def _commit_purged_children(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        operation_id: str,
        operation: sqlite3.Row,
        children: list,
        package: Path,
        now: datetime,
        pages: int,
        prior: int,
        blocker_code: str | None,
    ) -> CorruptPurgeProgress:
        """Publish this page of children, or report the fence that took it."""
        page_number, _root, published = self._commit_purge_page(
            database, task_id, operation, operation_id, children, package, now
        )
        if not published:
            return _purge_progress(
                task_id,
                operation_id,
                pages,
                prior,
                state="blocked",
                code="orphan_corrupt_purge_page_conflict",
            )
        return _purge_progress(
            task_id,
            operation_id,
            page_number,
            prior + len(children),
            state=_blocked_or_pending(blocker_code),
            code=blocker_code,
        )

    @staticmethod
    def _insert_corrupt_purge_page(
        database: sqlite3.Connection,
        *,
        operation_id: str,
        page_number: int,
        first_task_id: str,
        last_task_id: str,
        deleted_link_count: int,
        page_sha256: str,
        rolling_root: str,
        expected_generation: int,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO corrupt_purge_pages(
                   operation_id,page_number,first_task_id,last_task_id,
                   deleted_link_count,page_sha256,rolling_root,expected_generation
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                operation_id,
                page_number,
                first_task_id,
                last_task_id,
                deleted_link_count,
                page_sha256,
                rolling_root,
                expected_generation,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("corrupt_purge_page_insert_failed")

    def _corrupt_child_purge_blocker(
        self,
        database: sqlite3.Connection,
        child: sqlite3.Row,
        *,
        now: datetime,
    ) -> str | None:
        """Why this child of a corrupt export cannot be purged yet."""
        for check in _CORRUPT_CHILD_CHECKS:
            code = check(self, database, child, now)
            if code is not None:
                return code
        return None

    def _corrupt_purge_child_descriptor(
        self, database: sqlite3.Connection, child: sqlite3.Row
    ) -> dict[str, object]:
        task_id = str(child["id"])
        history = canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )
        task_evidence = canonical_json_bytes(
            {
                "task_id": task_id,
                "state": str(child["state"]),
                "input_hash": str(child["input_hash"]),
                "updated_at": str(child["updated_at"]),
                "lineage_generation": int(child["lineage_generation"]),
            }
        )
        return {
            "task_id": task_id,
            "task_sha256": sha256_bytes(task_evidence),
            "history_sha256": sha256_bytes(history),
        }

    @staticmethod
    def _delete_task_owned_evidence(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        for table in (
            "capture_task_link_seals",
            "capture_task_link_resolutions",
            "capture_task_links",
            "attempt_history",
            "task_source_links",
        ):
            database.execute(f'DELETE FROM "{table}" WHERE task_id=?', (task_id,))

    @contextmanager
    def _corrupt_purge_task_fence(
        self, task_id: str, *, owner: OwnerLease
    ) -> Iterator[TaskFence]:
        fence = self.acquire_task_fence(task_id, mode="queue-operator", owner=owner)
        try:
            yield fence
        finally:
            with closing(self._connect()) as database:
                task = database.execute(
                    "SELECT 1 FROM tasks WHERE id=?", (task_id,)
                ).fetchone()
                current = database.execute(
                    """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
                       AND fencing_epoch=?""",
                    (task_id, fence.token, fence.epoch),
                ).fetchone()
            self._release_corrupt_purge_fence(fence, current, task)

    def _release_corrupt_purge_fence(self, fence, current, task) -> None:
        if current is not None:
            self.release_task_fence(fence)
            return
        if task is not None:
            raise QueueOperationError("task_fence_lost")

    def _capture_terminal_file(
        self, intent_id: str
    ) -> tuple[dict[str, object], bytes]:
        path = self.results_dir / f"capture-{intent_id}.json"
        try:
            raw = _read_stable_owner_file(path, 64 * 1024)
            value = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, PermissionError, ValueError, json.JSONDecodeError) as exc:
            raise QueueOperationError("capture_intent_unresolved") from exc
        if not isinstance(value, dict):
            raise QueueOperationError("capture_terminal_invalid")
        return value, raw

    def _require_capture_terminal_identity(
        self, record: Mapping[str, object], task_id: str, binding: CaptureTaskBinding
    ) -> CaptureTaskBinding:
        disposition = record.get("disposition")
        actual = (
            set(record),
            record.get("schema_version"),
            record.get("intent_id"),
            record.get("intent_sha256"),
            isinstance(record.get("semantic_decisions"), list),
        )
        expected = (
            {
                "schema_version",
                "intent_id",
                "intent_sha256",
                "semantic_decisions",
                "processing_binding",
                "disposition",
            },
            "capture-terminal/v1",
            binding.intent_id,
            binding.intent_sha256,
            True,
        )
        allowed = {"markdown_committed", "no_durable_content", "operator_discard"}
        if actual != expected or task_id != binding.task_id:
            raise QueueOperationError("capture_terminal_invalid")
        if not isinstance(disposition, dict) or disposition.get("kind") not in allowed:
            raise QueueOperationError("capture_terminal_invalid")
        return self._terminal_proof_binding(record, binding)

    @staticmethod
    def _require_capture_terminal_database_binding(
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
        raw: bytes,
    ) -> None:
        intent = database.execute(
            "SELECT intent_sha256 FROM capture_intents WHERE intent_id=?",
            (binding.intent_id,),
        ).fetchone()
        if intent is None or intent["intent_sha256"] != binding.intent_sha256:
            raise QueueOperationError("capture_terminal_invalid")
        task = database.execute(
            """SELECT result_reference,result_sha256,result_operation_id
               FROM tasks WHERE id=?""",
            (task_id,),
        ).fetchone()
        expected = (
            f"run/queue-results/capture-{binding.intent_id}.json",
            sha256_bytes(raw),
            f"capture-terminal:{binding.intent_id}",
        )
        if task is None or tuple(task) != expected:
            raise QueueOperationError("capture_terminal_unbound")

    def _require_capture_terminal_proof(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> dict[str, object]:
        if binding.intent_id is None or binding.intent_sha256 is None:
            raise QueueOperationError("capture_intent_unresolved")
        record, raw = self._capture_terminal_file(binding.intent_id)
        proven = self._require_capture_terminal_identity(record, task_id, binding)
        self._require_capture_terminal_database_binding(
            database, task_id, binding, raw
        )
        self._require_versioned_capture_proof(proven, record)
        return record

    def _capture_terminal_blocker(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> str | None:
        try:
            self._require_capture_terminal_proof(database, task_id, binding)
        except QueueOperationError as exc:
            return exc.code
        return None

    @staticmethod
    def _require_capture_purge_unshared(
        database: sqlite3.Connection, task_id: str, intent_id: str
    ) -> None:
        other = database.execute(
            """SELECT task_id FROM capture_task_links
               WHERE intent_id=? AND task_id<>?
               UNION ALL
               SELECT task_id FROM capture_task_link_resolutions
               WHERE selected_intent_id=? AND task_id<>?
               LIMIT 1""",
            (intent_id, task_id, intent_id, task_id),
        ).fetchone()
        if other is not None:
            raise QueueOperationError("capture_link_conflicted")

    def _capture_purge_artifact(
        self,
        task_id: str,
        source_path: str,
        expected_sha256: str,
        *,
        max_bytes: int,
        error_code: str,
    ) -> _CapturePurgeArtifact:
        try:
            data = read_runtime_bytes(
                self.state_root / source_path,
                self.state_root,
                max_bytes=max_bytes,
                owner_only=True,
            )
        except (OSError, PermissionError, ValueError) as exc:
            raise QueueOperationError(error_code) from exc
        if sha256_bytes(data) != expected_sha256:
            raise QueueOperationError(error_code)
        archive_path = _capture_purge_archive_path(task_id, source_path)
        return _CapturePurgeArtifact(source_path, archive_path, expected_sha256)

    def _capture_purge_intent_path(
        self, database: sqlite3.Connection, binding: CaptureTaskBinding
    ) -> _CapturePurgeArtifact:
        row = database.execute(
            "SELECT * FROM capture_intents WHERE intent_id=?", (binding.intent_id,)
        ).fetchone()
        if row is None:
            raise QueueOperationError("capture_intent_conflict")
        relative = str(row["relative_path"])
        artifact = self._capture_purge_artifact(
            binding.task_id,
            relative,
            str(binding.intent_sha256),
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            error_code="capture_intent_conflict",
        )
        size = (self.state_root / relative).stat().st_size
        if (size, row["publication_state"]) != (row["byte_size"], "ready"):
            raise QueueOperationError("capture_intent_conflict")
        return artifact

    def _require_capture_purge_decision_file(
        self, task_id: str, row: sqlite3.Row
    ) -> _CapturePurgeArtifact:
        return self._capture_purge_artifact(
            task_id,
            str(row["decision_path"]),
            str(row["decision_sha256"]),
            max_bytes=1024 * 1024,
            error_code="semantic_decision_conflict",
        )

    @staticmethod
    def _capture_purge_decisions_match(
        rows: list[sqlite3.Row],
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> bool:
        indexed = [
            {
                "stage": str(row["stage"]),
                "decision_path": str(row["decision_path"]),
                "decision_sha256": str(row["decision_sha256"]),
            }
            for row in rows
        ]
        digests_match = all(
            row["active_link_digest"] == binding.active_digest for row in rows
        )
        return indexed == record.get("semantic_decisions") and digests_match

    def _capture_purge_decision_paths(
        self,
        database: sqlite3.Connection,
        binding: CaptureTaskBinding,
        record: Mapping[str, object],
    ) -> tuple[_CapturePurgeArtifact, ...]:
        rows = database.execute(
            """SELECT stage,decision_path,decision_sha256,active_link_digest
               FROM semantic_decisions WHERE intent_id=? ORDER BY stage""",
            (binding.intent_id,),
        ).fetchall()
        if not self._capture_purge_decisions_match(rows, binding, record):
            raise QueueOperationError("capture_terminal_invalid")
        return tuple(
            self._require_capture_purge_decision_file(binding.task_id, row)
            for row in rows
        )

    def _capture_purge_evidence(
        self,
        database: sqlite3.Connection,
        task_id: str,
        binding: CaptureTaskBinding,
    ) -> _CapturePurgeEvidence:
        record = self._require_capture_terminal_proof(database, task_id, binding)
        intent_id = str(binding.intent_id)
        self._require_capture_purge_unshared(database, task_id, intent_id)
        intent = self._capture_purge_intent_path(database, binding)
        decisions = self._capture_purge_decision_paths(
            database, binding, record
        )
        return _CapturePurgeEvidence(
            task_id,
            intent_id,
            intent,
            decisions,
            f"run/queue-results/capture-{intent_id}.json",
            self._capture_purge_source_paths(binding),
        )

    def _capture_purge_source_paths(self, binding: CaptureTaskBinding) -> tuple[_CapturePurgeArtifact, ...]:
        if binding.handler_version == 1:
            return ()
        from breadcrumb_storage import _stored_manifest, load_bundle, runtime_source_records

        manifest = _stored_manifest(self.state_root, binding.intent_id)
        bundle = load_bundle(self.state_root, manifest)
        return tuple(
            self._capture_purge_artifact(
                binding.task_id, relative, sha256_bytes(data),
                max_bytes=_MAX_QUEUE_PAYLOAD_BYTES, error_code="breadcrumb_source_changed",
            )
            for relative, data in runtime_source_records(self.state_root, bundle)
        )

    @staticmethod
    def _require_corrupt_task_fence(
        database: sqlite3.Connection, task_fence: TaskFence
    ) -> None:
        row = database.execute(
            """SELECT 1 FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=? AND canonical_owner_token=?
               AND canonical_fencing_epoch=? AND expires_at>?""",
            (
                task_fence.task_id,
                task_fence.token,
                task_fence.epoch,
                task_fence.owner.token,
                task_fence.owner.epoch,
                _timestamp(_utc_now()),
            ),
        ).fetchone()
        if row is None:
            raise QueueOperationError("task_fence_lost")

    def _publish_corrupt_purge_receipt(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> bytes:
        with closing(self._connect()) as database, begin_immediate(database):
            operation = database.execute(
                "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            task = database.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key,
                          export.rolling_root AS original_frozen_root
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            if operation is None or task is None or disposition is None:
                raise QueueOperationError("corrupt_purge_lost")
            self._require_corrupt_task_fence(database, task_fence)
            self._require_receipt_preconditions(database, task_id, task, operation)
            _require_package_digests(package, disposition)
            receipt_bytes = _write_purge_receipt(
                package,
                _purge_receipt_record(
                    operation, disposition, operation_id=operation_id, task_id=task_id
                ),
            )
            self._advance_purge_receipt_state(database, operation, operation_id)
            return receipt_bytes

    def _require_receipt_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation: sqlite3.Row,
    ) -> None:
        """The task is pending purge, on the expected generation, with no children."""
        incoming_count = int(
            database.execute(
                "SELECT COUNT(*) FROM tasks WHERE redrive_of=?", (task_id,)
            ).fetchone()[0]
        )
        observed = (
            task["state"],
            int(task["lineage_generation"]),
            incoming_count,
        )
        if observed != ("purge_pending", int(operation["expected_generation"]), 0):
            raise QueueOperationError("corrupt_purge_receipt_precondition_failed")

    @staticmethod
    def _advance_purge_receipt_state(
        database: sqlite3.Connection, operation: sqlite3.Row, operation_id: str
    ) -> None:
        """Move the operation to receipt-published, under its own fence."""
        if operation["state"] != "purging":
            return
        updated = database.execute(
            """UPDATE corrupt_purge_operations
               SET state='receipt-published',updated_at=?
               WHERE operation_id=? AND state='purging'
                 AND expected_generation=? AND page_count=? AND rolling_root=?""",
            (
                _timestamp(_utc_now()),
                operation_id,
                operation["expected_generation"],
                operation["page_count"],
                operation["rolling_root"],
            ),
        ).rowcount
        if updated != 1:
            raise QueueOperationError("corrupt_purge_receipt_fence_lost")

    def _corrupt_parent_rows(
        self, database: sqlite3.Connection, task_id: str, operation_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
        operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*,export.raw_sha256,export.history_sha256,
                      export.metadata_sha256,export.disposition_key,
                      export.rolling_root AS original_frozen_root
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS export
                 ON export.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if operation is None or task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_lost")
        return operation, task, disposition

    def _current_attempt_history(
        self, database: sqlite3.Connection, task_id: str
    ) -> bytes:
        return canonical_json_bytes(
            [
                {
                    "attempt": int(row["attempt"]),
                    "started_at": str(row["started_at"]),
                    "finished_at": str(row["finished_at"]),
                    "outcome": str(row["outcome"]),
                    "error_code": row["error_code"],
                }
                for row in database.execute(
                    """SELECT attempt,started_at,finished_at,outcome,error_code
                       FROM attempt_history WHERE task_id=? ORDER BY sequence""",
                    (task_id,),
                )
            ]
        )

    def _require_parent_delete_preconditions(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        operation: sqlite3.Row,
        task: sqlite3.Row,
        disposition: sqlite3.Row,
        package: Path,
    ) -> None:
        """Nothing is deleted until the package still proves what was exported."""
        receipt_bytes = _read_stable_owner_file(
            package / "purge-receipt.json", 64 * 1024
        )
        receipt = json.loads(receipt_bytes.decode("utf-8", errors="strict"))
        validate_schema(
            receipt, Path(__file__).with_name("schemas") / "corrupt-purge-v1.json"
        )
        raw = _read_stable_owner_file(
            package / "payload.bin", _MAX_QUEUE_PAYLOAD_BYTES
        )
        history = _read_stable_owner_file(
            package / "attempt-history.json", _MAX_EXPORT_METADATA_BYTES
        )
        current_history = self._current_attempt_history(database, task_id)
        children = database.execute(
            "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
        ).fetchone()
        if (
            not _parent_state_ready_for_delete(operation, task)
            or children is not None
            or not _exported_bytes_match(
                raw, history, current_history, task, disposition
            )
            or not _receipt_matches_disposition(
                receipt, disposition, operation, operation_id, task_id
            )
        ):
            raise QueueOperationError("corrupt_parent_delete_precondition_failed")

    def _delete_corrupt_parent(
        self,
        task_id: str,
        *,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
    ) -> None:
        with closing(self._connect()) as database, begin_immediate(database):
            operation, task, disposition = self._corrupt_parent_rows(
                database, task_id, operation_id
            )
            self._require_corrupt_task_fence(database, task_fence)
            self._require_parent_delete_preconditions(
                database,
                task_id,
                operation_id,
                operation,
                task,
                disposition,
                package,
            )
            self._authorize_corrupt_parent_purge(
                database, task_id, operation_id, operation
            )
            self._delete_corrupt_parent_rows(database, task_id, task_fence)
            self._clear_purge_authorization(database, task_id)

    def _authorize_corrupt_parent_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        operation: sqlite3.Row,
    ) -> None:
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'corrupt-parent',?,?,?)""",
            (
                task_id,
                operation_id,
                operation["purge_token"],
                _timestamp(_utc_now()),
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    def _delete_corrupt_parent_rows(
        self, database: sqlite3.Connection, task_id: str, task_fence: TaskFence
    ) -> None:
        """Take the fence, then the evidence, then the task -- in that order."""
        deleted_fence = database.execute(
            """DELETE FROM task_fences WHERE task_id=? AND token=?
               AND fencing_epoch=?""",
            (task_id, task_fence.token, task_fence.epoch),
        ).rowcount
        if deleted_fence != 1:
            raise QueueOperationError("task_fence_lost")
        self._delete_task_owned_evidence(database, task_id)
        deleted_task = database.execute(
            "DELETE FROM tasks WHERE id=?", (task_id,)
        ).rowcount
        if deleted_task != 1:
            raise QueueOperationError("corrupt_parent_delete_failed")

    def _corrupt_purge_rows(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row | None]:
        task = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        disposition = database.execute(
            """SELECT disposition.*, operation.disposition_key,
                      operation.rolling_root AS original_frozen_root,
                      operation.lineage_generation
               FROM corrupt_dispositions AS disposition
               JOIN corrupt_export_operations AS operation
                 ON operation.operation_id=disposition.operation_id
               WHERE disposition.task_id=?""",
            (task_id,),
        ).fetchone()
        if task is None or disposition is None:
            raise QueueOperationError("corrupt_purge_state_invalid")
        if task["state"] not in {"quarantined", "purge_pending"}:
            raise QueueOperationError("corrupt_purge_state_invalid")
        existing_operation = database.execute(
            "SELECT * FROM corrupt_purge_operations WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return task, disposition, existing_operation

    def _retention_blocker(
        self,
        disposition: sqlite3.Row,
        existing_operation: sqlite3.Row | None,
        now: datetime,
    ) -> str | None:
        """A disposed task is retained for thirty days before it may be purged."""
        disposed_at = _parse_timestamp(str(disposition["disposed_at"]))
        if disposed_at is None:
            raise QueueOperationError("corrupt_disposition_invalid")
        if existing_operation is None and disposed_at + timedelta(days=30) > now:
            return "corrupt_retention_active"
        return None

    def _start_corrupt_purge(
        self,
        database: sqlite3.Connection,
        task_id: str,
        task: sqlite3.Row,
        operation_id: str,
        task_fence: TaskFence,
        now: datetime,
    ) -> str | None:
        """None once the operation exists, or the code that blocks starting it."""
        try:
            binding = self.active_capture_binding(database, task_id)
        except QueueOperationError:
            return "capture_intent_unresolved"
        terminal_code = self._capture_terminal_blocker(database, task_id, binding)
        if terminal_code is not None:
            return terminal_code
        purge_token = sha256_bytes(
            canonical_json_bytes(
                {
                    "operation_id": operation_id,
                    "task_id": task_id,
                    "task_fence_epoch": task_fence.epoch,
                }
            )
        )
        inserted = database.execute(
            """INSERT INTO corrupt_purge_operations(
                   operation_id,task_id,purge_token,expected_generation,
                   cursor_task_id,page_count,rolling_root,state,created_at,updated_at
               ) VALUES (?,?,?,?,'',0,?,'purging',?,?)""",
            (
                operation_id,
                task_id,
                purge_token,
                int(task["lineage_generation"]),
                sha256_bytes(b""),
                _timestamp(now),
                _timestamp(now),
            ),
        ).rowcount
        changed = database.execute(
            """UPDATE tasks SET state='purge_pending',updated_at=?
               WHERE id=? AND state='quarantined'""",
            (_timestamp(now), task_id),
        ).rowcount
        if (inserted, changed) != (1, 1):
            raise QueueOperationError("corrupt_purge_start_failed")
        return None

    def _directed_corrupt_purge(
        self, task_id: str, task_fence: TaskFence, now: datetime
    ) -> tuple[str, Path, str | None]:
        """(operation id, package, blocking code) for this purge attempt."""
        with closing(self._connect()) as database, begin_immediate(database):
            task, disposition, existing_operation = self._corrupt_purge_rows(
                database, task_id
            )
            retention = self._retention_blocker(disposition, existing_operation, now)
            if retention is not None:
                return "", self.results_dir, retention
            package_key = str(disposition["disposition_key"])
            package = self.results_dir / f"corrupt-{package_key}"
            package_code = self._corrupt_package_purge_blocker(package, disposition)
            if package_code is not None:
                return "", package, package_code
            return self._corrupt_purge_operation(
                database,
                task_id,
                task=task,
                disposition=disposition,
                existing_operation=existing_operation,
                task_fence=task_fence,
                now=now,
                package=package,
                package_key=package_key,
            )

    def _corrupt_purge_operation(
        self,
        database: sqlite3.Connection,
        task_id: str,
        *,
        task: sqlite3.Row,
        disposition: sqlite3.Row,
        existing_operation: sqlite3.Row | None,
        task_fence: TaskFence,
        now: datetime,
        package: Path,
        package_key: str,
    ) -> tuple[str, Path, str | None]:
        """Start this purge operation, or re-attach to the one already open."""
        operation_key = sha256_bytes(
            canonical_json_bytes(
                {
                    "disposition_sha256": disposition["disposition_sha256"],
                    "package_key": package_key,
                    "task_id": task_id,
                }
            )
        )
        operation_id = f"corrupt-purge:{operation_key}"
        if existing_operation is None:
            blocker = self._start_corrupt_purge(
                database, task_id, task, operation_id, task_fence, now
            )
            return operation_id, package, blocker
        _require_matching_purge_operation(existing_operation, task, operation_id)
        return operation_id, package, None

    def _finish_corrupt_purge(
        self,
        task_id: str,
        operation_id: str,
        package: Path,
        task_fence: TaskFence,
        owner: OwnerLease,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> CorruptPurgeProgress:
        page_progress = self._purge_corrupt_lineage_page(
            task_id,
            operation_id=operation_id,
            package=package,
            owner=owner,
            task_fence=task_fence,
            deadline=deadline,
            cancelled=cancelled,
        )
        if page_progress.state == "blocked":
            return page_progress
        with closing(self._connect()) as database:
            incoming = database.execute(
                "SELECT 1 FROM tasks WHERE redrive_of=? LIMIT 1", (task_id,)
            ).fetchone()
        if incoming is not None:
            return page_progress
        self._publish_corrupt_purge_receipt(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        self._delete_corrupt_parent(
            task_id,
            operation_id=operation_id,
            package=package,
            task_fence=task_fence,
        )
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=operation_id,
            state="purged",
            pages_written=page_progress.pages_written,
            links_deleted=page_progress.links_deleted,
            complete=True,
            code=None,
        )

    def purge_quarantined(
        self,
        task_id: str,
        *,
        owner: OwnerLease,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> CorruptPurgeProgress:
        from operational_ownership import OwnerLease

        _require_active(deadline, cancelled)
        _require_repair_owner(owner, OwnerLease, "corrupt purge requires a repair owner")
        _require_task_identifier(task_id)
        registry = self.ownership_registry()
        with closing(registry._connect()) as coordinator_database:
            registry.require(coordinator_database, owner)
        completed = self._completed_corrupt_purge_progress(task_id)
        if completed is not None:
            return completed
        with self.queue_owner(
            role="queue-operator",
            scope=f"task:{sha256_bytes(task_id.encode('utf-8'))}",
            parent=owner,
        ), self._corrupt_purge_task_fence(task_id, owner=owner) as task_fence:
            operation_id, package, blocker = self._directed_corrupt_purge(
                task_id, task_fence, _utc_now()
            )
            if blocker is not None:
                return _blocked_purge(task_id, blocker)
            return self._finish_corrupt_purge(
                task_id, operation_id, package, task_fence, owner, deadline, cancelled
            )

    def _completed_corrupt_purge_progress(
        self, task_id: str
    ) -> CorruptPurgeProgress | None:
        completed = self._completed_purge_rows(task_id)
        if completed is None:
            return None
        operation, disposition, links_deleted = completed
        package = self.results_dir / f"corrupt-{disposition['disposition_key']}"
        receipt = _read_purge_receipt(package)
        _require_matching_purge_receipt(receipt, operation, disposition, task_id)
        return CorruptPurgeProgress(
            task_id=task_id,
            operation_id=str(operation["operation_id"]),
            state="purged",
            pages_written=int(operation["page_count"]),
            links_deleted=links_deleted,
            complete=True,
            code=None,
        )

    def _completed_purge_rows(
        self, task_id: str
    ) -> tuple[sqlite3.Row, sqlite3.Row, int] | None:
        """The purge rows for a task that is gone, or None while it is still here."""
        with closing(self._connect()) as database:
            operation = _published_purge_operation(database, task_id)
            if operation is None:
                return None
            disposition = database.execute(
                """SELECT disposition.*,export.disposition_key
                   FROM corrupt_dispositions AS disposition
                   JOIN corrupt_export_operations AS export
                     ON export.operation_id=disposition.operation_id
                   WHERE disposition.task_id=?""",
                (task_id,),
            ).fetchone()
            links_deleted = int(
                database.execute(
                    """SELECT COALESCE(SUM(deleted_link_count),0)
                       FROM corrupt_purge_pages WHERE operation_id=?""",
                    (operation["operation_id"],),
                ).fetchone()[0]
            )
        if disposition is None:
            raise QueueOperationError("corrupt_purge_completion_invalid")
        return operation, disposition, links_deleted

    def _corrupt_package_purge_blocker(
        self, package: Path, disposition: sqlite3.Row
    ) -> str | None:
        """Why the exported package cannot be trusted as the task's evidence."""
        try:
            if not _corrupt_package_trustworthy(package, disposition):
                return "corrupt_package_invalid"
        except (OSError, PermissionError, ValueError, json.JSONDecodeError):
            return "corrupt_package_invalid"
        return None

    @staticmethod
    def _release_unless_nested(
        registry: Any, lease: OwnerLease, *, nested: bool
    ) -> None:
        """Release the lease unless it belongs to a caller further out."""
        if nested:
            return
        registry.release(lease)

    def _queue_owner_lease(
        self, registry: Any, role: str, scope: str, parent: OwnerLease | None
    ) -> OwnerLease:
        """The lease this queue owner runs under, projected or freshly acquired."""
        if parent is None:
            return registry.acquire(role, scope=scope)
        _require_projectable_parent(registry, parent, role)
        return parent

    @contextmanager
    def queue_owner(
        self,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
        parent: OwnerLease | None = None,
    ) -> Iterator[OwnerLease]:
        if role not in _QUEUE_OWNER_PARENT_ROLES:
            raise ValueError("queue owner role must be queue-worker or queue-operator")
        registry = self.ownership_registry()
        nested = parent is not None
        lease = self._queue_owner_lease(registry, role, scope, parent)
        try:
            self._insert_queue_projection(
                lease, role=role, scope=scope, registry=registry
            )
        except BaseException:
            self._release_unless_nested(registry, lease, nested=nested)
            raise
        try:
            yield lease
        finally:
            self._remove_queue_projection(lease)
            self._release_unless_nested(registry, lease, nested=nested)

    def _insert_queue_projection(
        self,
        lease: OwnerLease,
        *,
        role: Literal["queue-worker", "queue-operator"],
        scope: str,
        registry: Any | None = None,
    ) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        domain_role = "worker" if role == "queue-worker" else "operator"
        owners = self.ownership_registry() if registry is None else registry
        with closing(
            self._connect()
        ) as database, begin_immediate(database):
            _reclaim_dead_queue_projections(database, owners, lease)
            database.execute(
                """INSERT INTO queue_ownership(
                       actor_id,domain_role,canonical_role,canonical_scope,
                       owner_token,fencing_epoch,process_id,process_start_identity,
                       acquired_at,heartbeat_at,expires_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lease.actor_id,
                    domain_role,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                    _timestamp(lease.acquired_at),
                    _timestamp(lease.heartbeat_at),
                    _timestamp(lease.expires_at),
                ),
            )

    def heartbeat_queue_owner(self, lease: OwnerLease) -> OwnerLease:
        registry = self.ownership_registry()
        current = registry.heartbeat(lease)
        with closing(
            self._connect()
        ) as database, begin_immediate(database):
            updated = database.execute(
                """UPDATE queue_ownership SET heartbeat_at=?,expires_at=?
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    _timestamp(current.heartbeat_at),
                    _timestamp(current.expires_at),
                    current.actor_id,
                    current.role,
                    current.scope,
                    current.token,
                    current.epoch,
                    current.process.pid,
                    current.process.start_identity,
                ),
            ).rowcount
            if updated != 1:
                raise QueueOperationError("queue_owner_fence_lost")
        return current

    def _remove_queue_projection(self, lease: OwnerLease) -> None:
        from operational_ownership import OwnerLease

        if not isinstance(lease, OwnerLease):
            raise TypeError("lease must be an OwnerLease")
        with closing(
            self._connect()
        ) as database, begin_immediate(database):
            deleted = database.execute(
                """DELETE FROM queue_ownership
                   WHERE actor_id=? AND canonical_role=? AND canonical_scope=?
                     AND owner_token=? AND fencing_epoch=? AND process_id=?
                     AND process_start_identity=?""",
                (
                    lease.actor_id,
                    lease.role,
                    lease.scope,
                    lease.token,
                    lease.epoch,
                    lease.process.pid,
                    lease.process.start_identity,
                ),
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("queue_owner_fence_lost")

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        task_id: str | None = None,
    ) -> QueueLease | None:
        _check_claim_arguments(owner, lease_seconds, max_attempts)
        task_filter = _claim_task_filter(task_id)
        with closing(self._connect()) as database, begin_immediate(database):
            return self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                  max_attempts=max_attempts, task_filter=task_filter)

    def _claim_in(self, database, *, owner, lease_seconds, max_attempts, task_filter):
        now = _utc_now()
        self._delete_stale_source_fences(database)
        self._recover_expired_in(database, now)
        claimable = self._next_claimable_task(database, now, max_attempts, task_filter)
        if claimable is None:
            return None
        row, validation = claimable
        token = sha256_bytes(os.urandom(32))
        expires_at = now + timedelta(seconds=lease_seconds)
        if not _take_task_lease(database, row, owner=owner, token=token, now=now,
                               expires_at=expires_at, max_attempts=max_attempts):
            return None
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=validation.payload or {},
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires_at,
            attempt=int(row["attempts"]) + 1,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def claim_uncertain_compile(
        self, owner: str, *, task_id: str, work_id: str, dispatch_digest: str,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
    ) -> QueueLease:
        """Acquire one uncertain dispatch atomically; this does not authorize a repeat.

        The operator entrypoint must hold its canonical queue owner, heartbeat the
        returned task lease and settle it. Normal payload, attempt and source
        fences remain in force. A failed acquisition leaves the task blocked.
        """
        _check_claim_arguments(owner, lease_seconds, max_attempts)
        task_filter = _claim_task_filter(task_id)
        compile_progress._require_digest(dispatch_digest)
        with closing(self._connect()) as database, begin_immediate(database):
            row = _require_uncertain_compile_dispatch(database, task_id, work_id, dispatch_digest)
            if row['state'] == 'blocked':
                _unblock_in(database, task_id, _utc_now())
            lease = self._claim_in(database, owner=owner, lease_seconds=lease_seconds,
                                   max_attempts=max_attempts, task_filter=task_filter)
            if lease is None:
                raise QueueOperationError('compile_recovery_not_claimable')
            _mark_compile_recovery(database, lease)
            return lease

    def _next_claimable_task(
        self, database: sqlite3.Connection, now: datetime, max_attempts: int, task_filter
    ) -> tuple[sqlite3.Row, PayloadValidation] | None:
        """The next ready task with a readable payload and attempts to spare."""
        _register_source_reference(database)
        predicate, target_parameters = task_filter
        while True:
            row = database.execute(
                f"""SELECT * FROM tasks WHERE {_V3_WORK_CLAIMABLE}{predicate}
                   ORDER BY priority DESC, available_at, created_at, id LIMIT 1""",
                (max_attempts, _timestamp(now), *target_parameters),
            ).fetchone()
            if row is None:
                return None
            claimable = self._claimable_row(database, row, now)
            if claimable is not None:
                return claimable

    def _claimable_row(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> tuple[sqlite3.Row, PayloadValidation] | None:
        """This row when it can be claimed; None means skip it and look again."""
        validation = self._require_valid_task_payload(
            database, row, now=now, parse=True
        )
        if validation is None:
            return None
        if _retire_exhausted_history(database, row, now):
            return None
        return row, validation

    @staticmethod
    def _validate_capture_claim(
        owner: str, lease_seconds: int, max_attempts: int,
        handler_versions: tuple[int, ...],
    ) -> None:
        _check_claim_arguments(owner, lease_seconds, max_attempts)
        if not handler_versions:
            raise ValueError("capture claim requires supported handler versions")
        for version in handler_versions:
            _require_bounded_int(version, 1, 2_147_483_647, "invalid capture handler version")

    @staticmethod
    def _capture_claim_row(
        database: sqlite3.Connection, max_attempts: int, now: datetime,
        handler_versions: tuple[int, ...],
    ) -> sqlite3.Row | None:
        placeholders = ",".join("?" for _version in handler_versions)
        return database.execute(
            f"""SELECT task.* FROM tasks AS task
               JOIN capture_task_links AS link ON link.task_id=task.id
               WHERE task.state='ready' AND task.kind='flush'
                 AND task.handler_version IN ({placeholders}) AND task.attempts<?
                 AND task.available_at<=?
                 AND (SELECT COUNT(*) FROM attempt_history AS history
                      WHERE history.task_id=task.id)<?
               ORDER BY task.priority DESC,task.available_at,task.created_at,task.id
               LIMIT 1""",
            (*handler_versions, max_attempts, _timestamp(now), _MAX_RUNTIME_ATTEMPTS),
        ).fetchone()

    def _lease_capture_row(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        owner: str,
        lease_seconds: int,
        max_attempts: int,
        now: datetime,
    ) -> QueueLease | None:
        validation = self._require_valid_task_payload(database, row, now=now, parse=True)
        if validation is None:
            return None
        token = sha256_bytes(os.urandom(32))
        expires_at = now + timedelta(seconds=lease_seconds)
        changed = database.execute(
            """UPDATE tasks SET state='leased',attempts=attempts+1,
                   lease_owner=?,lease_token=?,lease_expires_at=?,
                   lease_heartbeat_at=?,attempt_started_at=?,updated_at=?
               WHERE id=? AND state='ready' AND attempts<?""",
            (
                owner,
                token,
                _timestamp(expires_at),
                _timestamp(now),
                _timestamp(now),
                _timestamp(now),
                row["id"],
                max_attempts,
            ),
        ).rowcount
        if changed != 1:
            return None
        return QueueLease(
            id=str(row["id"]),
            kind=str(row["kind"]),
            handler_version=int(row["handler_version"]),
            payload=validation.payload or {},
            input_hash=str(row["input_hash"]),
            owner=owner,
            token=token,
            expires_at=expires_at,
            attempt=int(row["attempts"]) + 1,
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            last_attempt_at=_parse_timestamp(row["last_attempt_at"]),
            prior_attempts=int(row["attempts"]),
        )

    def claim_capture(
        self,
        owner: str,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        handler_versions: tuple[int, ...] = (1,),
    ) -> QueueLease | None:
        self._validate_capture_claim(owner, lease_seconds, max_attempts, handler_versions)
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._capture_claim_row(database, max_attempts, now, handler_versions)
            if row is None:
                return None
            return self._lease_capture_row(
                database,
                row,
                owner=owner,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
                now=now,
            )

    def heartbeat(
        self,
        lease: QueueLease,
        *,
        lease_seconds: int = DEFAULTS.queue_lease_seconds,
    ) -> QueueLease:
        _require_positive_task_lease(lease_seconds)
        expires_at = self._with_verified_lease(
            lease, partial(self._extend_leased_task, lease=lease, lease_seconds=lease_seconds)
        )
        return replace(lease, expires_at=expires_at)

    @staticmethod
    def _extend_leased_task(
        database: sqlite3.Connection,
        _row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        lease_seconds: int,
    ) -> datetime:
        """Move the lease's expiry on; a lease that is no longer ours raises."""
        expires_at = now + timedelta(seconds=lease_seconds)
        changed = database.execute(
            """UPDATE tasks SET lease_expires_at=?, lease_heartbeat_at=?,
                   updated_at=? WHERE id=? AND lease_token=? AND state='leased'""",
            (
                _timestamp(expires_at),
                _timestamp(now),
                _timestamp(now),
                lease.id,
                lease.token,
            ),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")
        return expires_at

    def _validated_result_digest(self, relative: str) -> str | None:
        try:
            path = self.state_root / relative
            if path.parent.resolve(strict=True) != self.results_dir.resolve(strict=True):
                return None
            data = _read_stable_owner_file(path, _MAX_RESULT_BYTES)
            return sha256_bytes(data)
        except (OSError, PermissionError, ValueError):
            return None

    def _bury_corrupt_result(
        self, database: sqlite3.Connection, now: datetime, *, lease: QueueLease
    ) -> None:
        """Kill the task whose published result no longer verifies."""
        database.execute(
            """UPDATE tasks SET state='dead',error_code='result_corrupt',
                   updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=?""",
            (_timestamp(now), lease.id),
        )

    def _rebind_adopted_result(
        self,
        database: sqlite3.Connection,
        now: datetime,
        *,
        lease: QueueLease,
        relative: str,
        digest: str,
        operation_id: str,
    ) -> None:
        """Point the leased task at the result it is adopting."""
        changed = database.execute(
            """UPDATE tasks SET result_reference=?,result_sha256=?,
                   result_operation_id=?,updated_at=?
               WHERE id=? AND lease_token=? AND state='leased'""",
            (relative, digest, operation_id, _timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def _adopt_result_row(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        relative: str,
        operation_id: str,
    ) -> str:
        """Adopt the published result, or bury the task if it no longer verifies."""
        digest = self._validated_result_digest(relative)
        if digest is None:
            self._bury_corrupt_result(database, now, lease=lease)
            return "corrupt"
        self._rebind_adopted_result(
            database,
            now,
            lease=lease,
            relative=relative,
            digest=digest,
            operation_id=operation_id,
        )
        return "adopted"

    def adopt_published_result(
        self, lease: QueueLease, *, operation_id: str
    ) -> str | None:
        if not operation_id:
            raise ValueError("operation_id must be non-empty")
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        relative = f"run/queue-results/{result_name}"
        if not (self.state_root / relative).exists():
            return None
        outcome = self._with_verified_lease(
            lease,
            partial(
                self._adopt_result_row,
                lease=lease,
                relative=relative,
                operation_id=operation_id,
            ),
        )
        return str(outcome) if outcome is not None else None

    def _with_verified_lease(self, lease: QueueLease, action: _LeaseAction) -> object:
        """Run `action` on the leased row, once the lease and payload hold.

        A payload mismatch demotes the task inside the transaction and must be
        reported to the caller afterwards, so the action is simply skipped and
        the refusal raised once the transaction has closed. Whatever the action
        answers is passed back.
        """
        now = _utc_now()
        result: object = None
        with closing(self._connect()) as database, begin_immediate(database):
            row = self._require_lease_row(database, lease, now)
            verified = (
                self._require_valid_task_payload(database, row, now=now, parse=True)
                is not None
            )
            if verified:
                _require_lease_input(row, lease)
                result = action(database, row, now)
        if not verified:
            self._raise_payload_mismatch()
        return result

    def _store_result_bytes(self, operation_id: str, result: bytes) -> tuple[str, str]:
        """Write the result file; answer its vault-relative path and digest."""
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        result_name = f"{sha256_bytes(operation_id.encode('utf-8'))}.result"
        _write_durable_file(self.results_dir / result_name, result)
        return f"run/queue-results/{result_name}", sha256_bytes(result)

    def compile_origin(self, lease: QueueLease) -> str:
        """Resolve original work under the current lease and payload fence."""
        return self._with_verified_lease(lease, self._compile_origin)

    @staticmethod
    def _compile_origin(database, row, now):
        return _compile_lineage_origin(database, row)

    def get_compile_event(self, work_id: str, *, sequence: int | None = None):
        """Read a checkpoint only after the explicit journal migration."""
        from compile_progress import event_at, latest_event

        with closing(self._connect()) as database:
            if sequence is not None:
                return event_at(database, work_id, sequence)
            return latest_event(database, work_id)

    def get_compile_dispatch(self, work_id: str):
        with closing(self._connect()) as database:
            return compile_progress.latest_dispatch(database, work_id)

    def inspect_compile_progress(self, task_id: str):
        """Read one consistent lineage/checkpoint snapshot without recovering work."""
        _require_task_identifier(task_id)
        with closing(self._connect()) as database:
            database.execute('BEGIN')
            return _compile_operator_progress(database, task_id)

    def verify_compile_checkpoint_owner(self, lease: QueueLease, checkpoint) -> None:
        """Reuse the lease/payload fence and immutable redrive lineage."""
        self._with_verified_lease(
            lease, partial(self._require_compile_checkpoint_owner, checkpoint=checkpoint),
        )

    def settle_compile_resolution(self, lease, dispatch, resolution, *, check_owner=None):
        """Revalidate the result, then release only this administrative lease."""
        from llm_wiki.compile_call_runtime import resolve_model_call

        if resolve_model_call(self, lease, dispatch) != resolution:
            raise compile_progress.ProgressConflict('compile resolution changed before settlement')
        if check_owner is not None:
            check_owner()
        outcome, event = resolution
        return self._with_verified_lease(
            lease, partial(self._settle_compile_resolution, event=event, outcome=outcome))

    @staticmethod
    def _settle_compile_resolution(database, row, now, *, event, outcome):
        if compile_progress.latest_event(database, event.work_id) != event:
            raise compile_progress.ProgressConflict('compile resolution changed before release')
        _require_compile_task_family(database, row, event)
        state, history = {
            'answer_recovered': ('ready', 'succeeded'),
            'repeat_authorized': ('ready', 'succeeded'),
            'outcome_unknown': ('blocked', 'blocked'),
        }[outcome]
        _settle_compile_recovery(database, row, now, state=state,
                                 outcome=history, code=f'compile_{outcome}')
        return state

    def abort_compile_recovery(self, lease):
        self._with_verified_lease(lease, self._abort_compile_recovery)

    @staticmethod
    def _abort_compile_recovery(database, row, now):
        _settle_compile_recovery(database, row, now, state='blocked',
                                 outcome='blocked', code='compile_recovery_interrupted')

    def recover_expired_compile_recovery(self, task_id, dispatch):
        """Recover only the named administrative lease, never unrelated work."""
        with closing(self._connect()) as database, begin_immediate(database):
            row = _required_redrive_row(database, task_id)
            _require_compile_task_family(database, row, dispatch)
            now = _utc_now()
            if not _is_compile_recovery(row) or row['lease_expires_at'] > _timestamp(now):
                return
            _require_dispatch_identity(compile_progress.latest_dispatch(database, dispatch.work_id),
                                       compile_progress._event_digest(dispatch))
            self._settle_expired_lease(database, row, now)

    @staticmethod
    def _require_compile_checkpoint_owner(database, row, now, *, checkpoint) -> None:
        _require_compile_task_family(database, row, checkpoint)

    def record_compile_event(self, lease: QueueLease, event) -> None:
        """Use the same ownership and payload fence as terminal results."""
        from compile_progress import ProgressConflict, _snapshot_event

        event = _snapshot_event(event)
        if event.task_id != lease.id:
            raise ProgressConflict("compile checkpoint names another task")
        self._with_verified_lease(
            lease, partial(self._append_compile_event, event=event, lease=lease)
        )

    def _append_compile_event(self, database, row, now, *, event, lease) -> None:
        from compile_progress import ProgressConflict, append_event

        if row["kind"] != "compile":
            raise ProgressConflict("only compile tasks may append compile progress")
        _require_dispatch_execution(row, event)
        _require_compile_work_family(database, row, event)
        _require_compile_root_work(database, row, event)
        if "artifact" in event.details:
            self.verify_compile_artifact(event)
        compile_progress.require_disjoint_dispatch(
            database, event, partial(self._verify_retained_compile_answer, database))
        self._require_lease_row(database, lease, _utc_now())
        append_event(database, event)

    def _verify_retained_compile_answer(self, database, answer):
        from llm_wiki.compile_answer_records import _decoded_answer, _require_same_call

        dispatch = compile_progress.answer_dispatch(database, answer)
        _require_same_call(answer, dispatch)
        _decoded_answer(dispatch, self.read_compile_artifact(answer))

    def save_compile_artifact(self, lease: QueueLease, event, data: bytes):
        """Durably store intermediate bytes before publishing their checkpoint.

        The existing queue byte bound is retained pending resource qualification;
        it is not a new limit or a claim that arbitrary model output is accepted.
        A fence loss after the file write leaves an unreferenced file, not progress.
        """
        _check_result_bytes(data)
        return self.save_compile_artifact_stream(lease, event, (data,))

    def save_compile_artifact_stream(self, lease: QueueLease, event, chunks, *, deadline=None):
        """Persist an iterable without a total-size or in-memory-result ceiling."""
        return self.save_compile_artifact_writer(
            lease, event, partial(_copy_artifact_chunks, chunks=chunks), deadline=deadline)

    def save_compile_artifact_writer(self, lease, event, producer, *, deadline=None):
        """Run a sequential format writer inside the same durable publication."""
        event = compile_progress._snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        self.results_dir.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(self.results_dir, 0o700)
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8')) + '.result'
        digest = _write_durable_producer(self.results_dir / name, producer, deadline=deadline)
        saved = replace(event, details={**event.details, 'artifact': {
            'reference': f'run/queue-results/{name}', 'sha256': digest}})
        self.record_compile_event(lease, saved)
        return saved

    def publish_initial_compile_capture(self, lease, *, producer, decode_root, deadline=None):
        """Publish root and snapshot together after durable, validated capture bytes.

        A complete orphan is decoded before considering a fresh producer. The
        decoder must verify the source format and bind the original payload.
        """
        origin = self.compile_origin(lease)
        work_id = compile_progress.root_work_id(origin)
        relative = 'run/queue-results/' + sha256_bytes(f'compile:{work_id}:1'.encode()) + '.result'
        digest = self._initial_compile_capture_file(relative, producer, deadline)
        with self._open_compile_artifact_bytes(relative, digest, deadline) as handle:
            root = compile_progress._snapshot_event(decode_root(handle))
        _require_initial_capture_root(root, lease, origin)
        snapshot = replace(root, sequence=1, stage='snapshot_ready', details={
            'artifact': {'reference': relative, 'sha256': digest}})
        self._with_verified_lease(lease, partial(self._append_initial_compile_capture,
            root=root, snapshot=snapshot, lease=lease, deadline=deadline))
        return snapshot

    def _initial_compile_capture_file(self, relative, producer, deadline):
        path = self.state_root / relative
        try:
            digest, _size = _digest_stable_owner_file(path, deadline=deadline)
            return digest
        except FileNotFoundError:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            _harden_owner_only(self.results_dir, 0o700)
            return _write_durable_producer(path, producer, deadline=deadline)

    def _append_initial_compile_capture(self, database, row, now, *, root, snapshot, lease, deadline):
        operational_deadline_active(deadline)
        _require_initial_capture_root(root, lease, _compile_lineage_origin(database, row))
        if compile_progress.event_at(database, root.work_id, 0) is not None:
            raise compile_progress.ProgressConflict('initial capture work is already recorded')
        self._append_compile_event(database, row, now, event=root, lease=lease)
        self._append_compile_event(database, row, now, event=snapshot, lease=lease)
        operational_deadline_active(deadline)

    def verify_compile_artifact(self, event, *, deadline=None) -> int:
        """Verify retained bytes in bounded memory; return the physical size."""
        relative, expected = compile_progress.artifact_identity(event)
        try:
            digest, size = _digest_stable_owner_file(self.state_root / relative, deadline=deadline)
        except TimeoutError:
            raise
        except (OSError, ValueError, QueueOperationError) as error:
            raise compile_progress.ProgressCorrupt('compile artifact is unavailable') from error
        if digest != expected:
            raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
        return size

    @contextmanager
    def open_compile_artifact(self, event, *, deadline=None):
        """A verified seekable file; the descriptor stays owned by this context."""
        relative, expected = compile_progress.artifact_identity(event)
        with self._open_compile_artifact_bytes(relative, expected, deadline) as handle:
            yield handle

    @contextmanager
    def _open_compile_artifact_bytes(self, relative, expected, deadline):
        path = self.state_root / relative
        before = path.lstat()
        _check_owner_file_metadata(path, before, before.st_size)
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            _verify_open_artifact(handle, path, before, expected, deadline)
            try:
                yield handle
            finally:
                _require_stream_identity(path, handle.fileno(), before)

    @staticmethod
    def _compile_artifact_operation(event) -> str:
        return f"compile:{event.work_id}:{event.sequence}"

    def recover_compile_artifact(self, lease: QueueLease, event, *, validate=None):
        """Adopt an atomic answer file left before the event transaction committed."""
        from compile_progress import _snapshot_event

        event = _snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8'))
        relative = f"run/queue-results/{name}.result"
        try:
            data = _read_stable_owner_file(self.state_root / relative, _MAX_RESULT_BYTES)
        except FileNotFoundError:
            return None
        if validate is not None:
            validate(data)
        saved = replace(event, details={
            **event.details, "artifact": {"reference": relative, "sha256": sha256_bytes(data)},
        })
        self.record_compile_event(lease, saved)
        return saved

    def recover_compile_artifact_stream(self, lease, event, *, expected_digest, deadline=None):
        """Adopt a complete orphan only when it matches the expected snapshot."""
        compile_progress._require_digest(expected_digest)
        saved = self._compile_orphan_event(lease, event, deadline)
        if saved is None:
            return None
        if saved.details['artifact']['sha256'] != expected_digest:
            raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
        self.record_compile_event(lease, saved)
        return saved

    def recover_compile_artifact_validated(self, lease, event, *, validate, deadline=None):
        """Adopt only after a caller proves the complete format's expected identity."""
        saved = self._compile_orphan_event(lease, event, deadline)
        if saved is None:
            return None
        with self.open_compile_artifact(saved, deadline=deadline) as handle:
            validate(handle)
        self.record_compile_event(lease, saved)
        return saved

    def _compile_orphan_event(self, lease, event, deadline):
        event = compile_progress._snapshot_event(event)
        self._with_verified_lease(
            lease, partial(self._verify_compile_artifact_owner, event=event)
        )
        name = sha256_bytes(self._compile_artifact_operation(event).encode('utf-8'))
        relative = f'run/queue-results/{name}.result'
        try:
            digest, _ = _digest_stable_owner_file(self.state_root / relative, deadline=deadline)
        except FileNotFoundError:
            return None
        return replace(event, details={**event.details, 'artifact': {
            'reference': relative, 'sha256': digest}})

    @staticmethod
    def _verify_compile_artifact_owner(database, row, now, *, event) -> None:
        from compile_progress import ProgressConflict

        if row["kind"] != "compile" or row["id"] != event.task_id:
            raise ProgressConflict("compile artifact does not belong to this task")
        _require_compile_work_family(database, row, event)
        _require_compile_root_work(database, row, event)

    def read_compile_artifact(self, event) -> bytes:
        from compile_progress import ProgressCorrupt, artifact_identity

        relative, digest = artifact_identity(event)
        try:
            data = _read_stable_owner_file(self.state_root / relative, _MAX_RESULT_BYTES)
        except (OSError, ValueError, QueueOperationError) as error:
            raise ProgressCorrupt("compile artifact is unavailable") from error
        if sha256_bytes(data) != digest:
            raise ProgressCorrupt("compile artifact digest mismatch")
        return data

    def _require_publication_is_new(
        self, row: sqlite3.Row, operation_id: str, relative: str, digest: str
    ) -> None:
        """Refuse a publication that contradicts one this lease already made."""
        _check_result_operation(row, operation_id)
        if self._validated_result_digest(relative) != digest:
            raise ResultConflictError("operation ID already has different result bytes")

    def _bind_published_result(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        operation_id: str,
        relative: str,
        digest: str,
    ) -> None:
        """Point the leased task at the result bytes already written."""
        self._require_compile_completion(database, row, now)
        self._require_publication_is_new(row, operation_id, relative, digest)
        changed = database.execute(
            """UPDATE tasks SET result_reference=?,result_sha256=?,
                   result_operation_id=?,updated_at=?
               WHERE id=? AND lease_token=? AND state='leased'""",
            (relative, digest, operation_id, _timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def publish_result(
        self, lease: QueueLease, *, operation_id: str, result: bytes
    ) -> str:
        _check_result_request(operation_id, result)
        self._with_verified_lease(lease, self._require_compile_completion)
        relative, digest = self._store_result_bytes(operation_id, result)
        self._with_verified_lease(
            lease,
            partial(
                self._bind_published_result,
                lease=lease,
                operation_id=operation_id,
                relative=relative,
                digest=digest,
            ),
        )
        return relative

    def _require_compile_completion(self, database, row, now):
        if row['kind'] != 'compile' or self.expected_contract.user_version != compile_progress.QUEUE_SCHEMA_VERSION:
            return
        _require_complete_compile_attestation(database, row)

    def _result_is_intact(self, row: sqlite3.Row) -> bool:
        """Whether the task's recorded result still hashes to what it claims."""
        digest = row["result_sha256"]
        reference = row["result_reference"]
        if not isinstance(digest, str) or not isinstance(reference, str):
            return False
        return self._validated_result_digest(reference) == digest

    def _settle_acknowledged(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
    ) -> None:
        """Close the leased attempt, recording whether its result survived."""
        self._require_compile_completion(database, row, now)
        state, error_code, outcome = _ACKNOWLEDGED_OUTCOMES[self._result_is_intact(row)]
        _record_acknowledged_attempt(
            database, row, now, lease=lease, outcome=outcome, error_code=error_code
        )
        changed = database.execute(
            """UPDATE tasks SET state=?,error_code=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   lease_heartbeat_at=NULL,attempt_started_at=NULL
               WHERE id=? AND lease_token=? AND state='leased'""",
            (state, error_code, _timestamp(now), lease.id, lease.token),
        ).rowcount
        if changed != 1:
            raise LeaseFenceError(f"lease is stale or not owned: {lease.id}")

    def acknowledge(self, lease: QueueLease) -> QueueTask:
        """Settle the lease and return the task, as the legacy queue does.

        The worker counts a terminal task by reading the record back, so a
        backend that returns nothing here leaves it counting `None`.
        """
        self._with_verified_lease(lease, partial(self._settle_acknowledged, lease=lease))
        return self.get(lease.id)

    def fail(
        self,
        lease: QueueLease,
        failure: QueueFailure,
        *,
        max_attempts: int = DEFAULTS.queue_max_attempts,
        retry_base_seconds: int = DEFAULTS.retry_base_seconds,
        retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    ) -> None:
        _require_adopted_retry_policy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        )
        if not isinstance(failure, QueueFailure) or not failure.error_code:
            raise ValueError("failure must have a non-empty error code")
        self._with_verified_lease(
            lease,
            partial(
                self._fail_leased_task,
                lease=lease,
                failure=failure,
                max_attempts=max_attempts,
            ),
        )

    @staticmethod
    def _fail_leased_task(
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        lease: QueueLease,
        failure: QueueFailure,
        max_attempts: int,
    ) -> None:
        _record_failed_attempt(database, lease, row, failure, now)
        _apply_failure_state(database, lease, row, failure, now, max_attempts)

    def recover_expired_leases(self) -> int:
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            return self._recover_expired_in(database, now)

    def _recover_expired_in(self, database: sqlite3.Connection, now: datetime) -> int:
        """Settle every expired lease; a task out of attempts is dead, never stuck ready.

        See `docs/research/2026-09-14-one-second-chance-and-no-stuck-task.md`.
        """
        rows = database.execute(
            "SELECT * FROM tasks WHERE state='leased' AND lease_expires_at<=?",
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            self._settle_expired_lease(database, row, now)
        _retire_exhausted_ready(database, now)
        return len(rows)

    def _settle_expired_lease(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> None:
        if self._require_valid_task_payload(database, row, now=now, parse=True) is None:
            return
        if _is_compile_recovery(row):
            _settle_compile_recovery(database, row, now, state='blocked',
                                     outcome='lease_expired', code='compile_recovery_expired')
            return
        self._settle_execution_expired_lease(database, row, now)

    @staticmethod
    def _settle_execution_expired_lease(database, row, now):
        state, error_code = _EXPIRED_LEASE_OUTCOMES[
            int(row["attempts"]) >= DEFAULTS.queue_max_attempts
        ]
        database.execute(
            """INSERT INTO attempt_history(
                   task_id,attempt,started_at,finished_at,outcome,error_code
               ) VALUES (?,?,?,?,?,?)""",
            (
                row["id"],
                row["attempts"],
                row["attempt_started_at"] or _timestamp(now),
                _timestamp(now),
                "lease_expired",
                error_code,
            ),
        )
        changed = database.execute(
            """UPDATE tasks SET state=?,error_code=?,
                   updated_at=?,available_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=? AND state='leased'""",
            (state, error_code, _timestamp(now), _timestamp(now), row["id"]),
        ).rowcount
        if changed != 1:
            raise QueueOperationError("lease_expiry_failed")

    def cancel(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        _require_active(deadline, cancelled)
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            row = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["state"] in _TERMINAL_STATES:
                return False
            changed = self._cancel_valid_row(database, row, now)
        if changed is None:
            self._raise_payload_mismatch()
        return bool(changed)

    def _cancel_valid_row(
        self, database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> bool | None:
        """Cancel a row whose payload holds; None when the payload was demoted instead."""
        if self._require_valid_task_payload(database, row, now=now, parse=True) is None:
            return None
        return self._cancel_row(database, row, now)

    @staticmethod
    def _cancel_row(
        database: sqlite3.Connection, row: sqlite3.Row, now: datetime
    ) -> bool:
        """An attempt cancelled in flight is recorded, as the legacy queue records it."""
        if row["state"] == "leased":
            _record_cancelled_attempt(database, row, now)
        return database.execute(
            """UPDATE tasks SET state='cancelled',error_code='cancelled',
                   updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,lease_heartbeat_at=NULL,
                   attempt_started_at=NULL WHERE id=? AND state=?""",
            (_timestamp(now), row["id"], row["state"]),
        ).rowcount == 1

    def unblock(self, task_id: str) -> None:
        """A blocked task goes back to ready; any other state is refused."""
        with closing(self._connect()) as database, begin_immediate(database):
            _unblock_in(database, task_id, _utc_now())

    def _insert_redriven_task(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
        *,
        replacement: str,
        task_id: str,
        validation: PayloadValidation,
    ) -> None:
        """Copy the dead task into a fresh ready one that points back at it."""
        inserted = database.execute(
            """INSERT INTO tasks(
                   id,kind,handler_version,payload_blob,input_hash,state,
                   priority,created_at,updated_at,available_at,redrive_of
               ) VALUES (?,?,?,?,?,'ready',?,?,?,?,?)""",
            (
                replacement,
                row["kind"],
                row["handler_version"],
                validation.raw,
                validation.input_hash,
                row["priority"],
                _timestamp(now),
                _timestamp(now),
                _timestamp(now),
                task_id,
            ),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("redrive_failed")
        self._carry_capture_link(database, task_id, replacement, now)

    @staticmethod
    def _parent_capture_link(
        database: sqlite3.Connection, task_id: str
    ) -> sqlite3.Row | None:
        return database.execute(
            "SELECT intent_id, intent_sha256, handler_version "
            "FROM capture_task_links WHERE task_id=?",
            (task_id,),
        ).fetchone()

    def _carry_capture_link(
        self,
        database: sqlite3.Connection,
        task_id: str,
        replacement: str,
        now: datetime,
    ) -> None:
        """A redriven capture task keeps its intent, or it reaches no worker.

        `claim_capture` selects only tasks that have a row in
        `capture_task_links`, and a redrive used to copy the task without it,
        so a redriven capture task was invisible to the capture worker for
        good — and the sweeper that hands a task to an intent looks for
        intents with *no* task, so an intent whose only task was dead could be
        neither adopted nor redriven. Twenty-three session classifications sat
        in that gap on this vault.

        This is not a way around the admission fence. The fence exists so a
        task cannot appear for an intent nobody holds; this intent passed it
        once, and its signed link already exists. What travels is the same
        record — intent, its digest, the handler version — re-signed for the
        child's id, which is what current practice calls carrying the
        idempotency key across a redrive. See
        `docs/research/2026-09-07-how-many-second-chances-an-intent-gets.md`.
        """
        parent = self._parent_capture_link(database, task_id)
        if parent is None:
            return
        record = _capture_link_record(
            replacement,
            str(parent["intent_id"]),
            str(parent["intent_sha256"]),
            int(parent["handler_version"]),
        )
        self._insert_capture_link(
            database,
            task_id=replacement,
            intent_id=str(parent["intent_id"]),
            intent_sha256=str(parent["intent_sha256"]),
            handler_version=int(parent["handler_version"]),
            link_digest=sha256_bytes(canonical_json_bytes(record)),
            created_at=_timestamp(now),
        )

    def redrive(
        self,
        task_id: str,
        *,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> str:
        _require_active(deadline, cancelled)
        now = _utc_now()
        replacement = uuid.uuid4().hex
        with closing(self._connect()) as database, begin_immediate(database):
            row = _require_dead_task(database, task_id)
            self._delete_stale_source_fences(database)
            self._assert_payload_not_fenced(database, _task_payload_text(row))
            validation = self._require_valid_task_payload(
                database, row, now=now, parse=True
            )
            if validation is not None:
                self._insert_redriven_task(
                    database,
                    row,
                    now,
                    replacement=replacement,
                    task_id=task_id,
                    validation=validation,
                )
        if validation is None:
            self._raise_payload_mismatch()
        return replacement

    def purge(
        self,
        *,
        terminal_before: datetime,
        export_path: Path,
        include_dead: bool = False,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> PurgeReceipt:
        _require_active(deadline, cancelled)
        plan = self._ordinary_purge_plan(
            terminal_before, export_path, include_dead=include_dead
        )
        manifest_bytes = self._publish_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )
        capture_evidence = self._commit_ordinary_purge(
            plan,
            manifest_bytes,
            deadline=deadline,
            cancelled=cancelled,
        )
        self._cleanup_ordinary_purge_artifacts(plan, capture_evidence)
        self._publish_ordinary_purge_receipt(plan, manifest_bytes)
        self._clear_ordinary_purge_authorizations(plan, manifest_bytes)
        return PurgeReceipt(len(plan.task_ids), plan.task_ids, plan.retained)

    def _ordinary_purge_plan(
        self, terminal_before: datetime, export_path: Path, *, include_dead: bool
    ) -> _OrdinaryPurgePlan:
        retention_cutoff = _utc_now() - timedelta(
            days=DEFAULTS.queue_result_retention_days
        )
        cutoff = _timestamp(min(_as_utc(terminal_before), retention_cutoff))
        export = Path(export_path).absolute()
        if export.is_symlink():
            raise QueueOperationError("export_verification_failed")
        if export.exists():
            return self._load_ordinary_purge_plan(cutoff, export)
        return self._new_ordinary_purge_plan(cutoff, export, include_dead)

    def _demote_corrupt_finished_tasks(self, cutoff: str, include_dead: bool) -> None:
        """Move corrupt finished rows out of the purge selection, and commit it.

        One corrupt row used to refuse the whole plan, and nothing else can move
        a finished row. Demoted to `dead / payload_hash_mismatch` it leaves the
        selection and becomes something `quarantine-corrupt` accepts.
        """
        now = _utc_now()
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                _ordinary_purge_selection(include_dead), (cutoff,)
            ).fetchall()
            for row in rows:
                self._require_valid_task_payload(database, row, now=now, parse=True)

    def _new_ordinary_purge_plan(
        self, cutoff: str, export: Path, include_dead: bool
    ) -> _OrdinaryPurgePlan:
        self._demote_corrupt_finished_tasks(cutoff, include_dead)
        with closing(self._connect()) as database:
            database.execute("BEGIN")
            selected = database.execute(
                f"{_ordinary_purge_selection(include_dead)} ORDER BY created_at,id",
                (cutoff,),
            ).fetchall()
            rows, retained = self._rows_with_releasable_evidence(database, selected)
            records = tuple(
                self._export_task_in_transaction(database, row) for row in rows
            )
            capture_evidence = self._ordinary_capture_evidence_for_rows(
                database, rows
            )
        task_ids = tuple(str(row["id"]) for row in rows)
        records_bytes = canonical_json_bytes(list(records))
        if len(records_bytes) > _MAX_EXPORT_METADATA_BYTES:
            raise QueueOperationError("export_metadata_too_large")
        return _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            None,
            retained,
        )

    def _rows_with_releasable_evidence(
        self, database: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[list[sqlite3.Row], tuple[str, ...]]:
        """The rows the purge can prove, and the ids of those it keeps.

        A capture task without a terminal record used to abort the whole plan, so
        one unresolved capture kept every finished row in the queue. It is left
        in place and named instead. See
        `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
        Compile history likewise retains its task; its foreign key must not abort
        deletion of unrelated finished work. Semantic retirement is separate.
        """
        kept: list[sqlite3.Row] = []
        retained: list[str] = []
        for row in rows:
            task_id = str(row["id"])
            if self._ordinary_task_evidence_releasable(database, task_id):
                kept.append(row)
                continue
            retained.append(task_id)
        return kept, tuple(retained)

    def _ordinary_task_evidence_releasable(self, database, task_id: str) -> bool:
        if compile_progress.retains_task(database, task_id):
            return False
        return self._capture_resolved(database, task_id)

    def _capture_resolved(self, database: sqlite3.Connection, task_id: str) -> bool:
        link = database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if link is None:
            return True
        # A redriven capture's parent and child share one intent, and the terminal
        # record binds only the task that finished it: the parent failed the proof
        # and the whole weekly purge stopped (audit 2026-09-26 A-12). The family is
        # kept, named in `retained`, and never purged half.
        if _in_redrive_family(database, task_id):
            return False
        return self._capture_proof_settled(database, task_id)

    def _capture_proof_settled(self, database: sqlite3.Connection, task_id: str) -> bool:
        binding = self.active_capture_binding(database, task_id)
        blocker = self._capture_terminal_blocker(database, task_id, binding)
        if blocker in (None, "capture_intent_unresolved"):
            return blocker is None
        # A terminal record that exists but does not bind this task is damage,
        # not a capture still waiting: the purge refuses rather than keeping it.
        raise QueueOperationError(blocker)

    def redrive_dead_captures(
        self,
        *,
        changed_after: datetime,
        limit: int = DEAD_CAPTURE_REDRIVES_PER_PASS,
        deadline: float = float("inf"),
        cancelled: Callable[[], bool] | None = None,
    ) -> DeadCaptureRedrive:
        """Give every dead capture that died before the code changed its one redrive.

        A fix arriving is the event a dead-letter redrive waits for, and nobody
        else here would ever ask. A redriven capture that dies again is final. See
        `docs/research/2026-09-25-a-dead-capture-gets-its-second-chance-after-a-fix.md`.
        """
        before = _timestamp(_as_utc(changed_after))
        redriven: list[tuple[str, str]] = []
        refused: list[tuple[str, str]] = []
        for task_id in self._dead_captures_before(before, limit):
            _require_active(deadline, cancelled)
            outcome = self._redrive_or_refusal(task_id, deadline, cancelled)
            target = redriven if outcome[0] == "redriven" else refused
            target.append((task_id, outcome[1]))
        return DeadCaptureRedrive(tuple(redriven), tuple(refused))

    def _redrive_or_refusal(
        self,
        task_id: str,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[str, str]:
        try:
            return "redriven", self.redrive(task_id, deadline=deadline, cancelled=cancelled)
        except QueueOperationError as exc:
            return "refused", exc.code

    def _dead_captures_before(self, before: str, limit: int) -> list[str]:
        """Dead capture tasks at redrive depth 0 that died before `before`."""
        with closing(self._connect()) as database:
            rows = database.execute(
                """SELECT t.id FROM tasks AS t
                   JOIN capture_task_links AS l ON l.task_id=t.id
                   WHERE t.state='dead' AND t.error_code IS NOT 'payload_hash_mismatch'
                     AND t.redrive_of IS NULL AND t.updated_at<?
                     AND NOT EXISTS (SELECT 1 FROM tasks AS c
                                     JOIN capture_task_links AS cl ON cl.task_id=c.id
                                     WHERE c.redrive_of=t.id)
                   ORDER BY t.updated_at, t.id LIMIT ?""",
                (before, limit),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def _ordinary_capture_evidence_for_rows(
        self, database: sqlite3.Connection, rows: list[sqlite3.Row]
    ) -> tuple[_CapturePurgeEvidence, ...]:
        evidence: list[_CapturePurgeEvidence] = []
        for row in rows:
            evidence.extend(
                self._ordinary_capture_purge_evidence(database, str(row["id"]))
            )
        return tuple(evidence)

    def _load_ordinary_purge_plan(
        self, cutoff: str, export: Path
    ) -> _OrdinaryPurgePlan:
        self._require_ordinary_purge_export_directory(export)
        try:
            records_bytes = _read_stable_owner_file(
                export / "records.json", _MAX_EXPORT_METADATA_BYTES
            )
            manifest_bytes = _read_stable_owner_file(
                export / "manifest.json", _MAX_EXPORT_METADATA_BYTES
            )
            records, manifest = self._decode_ordinary_purge_export(
                records_bytes, manifest_bytes
            )
        except QueueOperationError:
            raise
        except (OSError, PermissionError, UnicodeError, ValueError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        task_ids = tuple(str(record["task_id"]) for record in records)
        self._require_ordinary_purge_manifest(
            manifest,
            cutoff=cutoff,
            task_ids=task_ids,
            records_bytes=records_bytes,
        )
        capture_evidence = self._capture_purge_evidence_from_manifest(
            manifest["capture_artifacts"], records
        )
        plan = _OrdinaryPurgePlan(
            cutoff,
            export,
            task_ids,
            records,
            records_bytes,
            capture_evidence,
            manifest_bytes,
        )
        self._require_existing_ordinary_purge_files(plan, manifest)
        return plan

    @staticmethod
    def _require_ordinary_purge_export_directory(export: Path) -> None:
        metadata = export.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        actual = (
            stat.S_ISDIR(metadata.st_mode),
            stat.S_ISLNK(metadata.st_mode),
            bool(getattr(metadata, "st_file_attributes", 0) & reparse),
            _is_owner_only(export),
        )
        if actual != (True, False, False, True):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _decode_ordinary_purge_export(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[tuple[dict[str, object], ...], dict[str, object]]:
        records_value, manifest = (
            _QueueV3CandidateReader._decode_ordinary_purge_json(
                records_bytes, manifest_bytes
            )
        )
        records = _QueueV3CandidateReader._ordinary_purge_records(
            records_value, records_bytes
        )
        return records, manifest

    @staticmethod
    def _decode_ordinary_purge_json(
        records_bytes: bytes, manifest_bytes: bytes
    ) -> tuple[list[object], dict[str, object]]:
        try:
            records_value = json.loads(records_bytes.decode("utf-8", errors="strict"))
            manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if (isinstance(records_value, list), isinstance(manifest, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return records_value, manifest

    @staticmethod
    def _ordinary_purge_records(
        records_value: list[object], records_bytes: bytes
    ) -> tuple[dict[str, object], ...]:
        if not all(isinstance(record, dict) for record in records_value):
            raise QueueOperationError("export_verification_failed")
        records = tuple(records_value)
        if canonical_json_bytes(list(records)) != records_bytes:
            raise QueueOperationError("export_verification_failed")
        for record in records:
            validate_schema(
                record, Path(__file__).with_name("schemas") / "queue-task-v3.json"
            )
        return records

    @staticmethod
    def _require_ordinary_purge_manifest(
        manifest: Mapping[str, object],
        *,
        cutoff: str,
        task_ids: tuple[str, ...],
        records_bytes: bytes,
    ) -> None:
        identity = (
            set(manifest),
            manifest.get("cutoff"),
            manifest.get("records_sha256"),
            manifest.get("task_ids"),
        )
        expected = (
            {"capture_artifacts", "cutoff", "records_sha256", "results", "task_ids"},
            cutoff,
            sha256_bytes(records_bytes),
            list(task_ids),
        )
        if identity != expected or len(set(task_ids)) != len(task_ids):
            raise QueueOperationError("export_verification_failed")
        if not isinstance(manifest.get("results"), list) or not isinstance(
            manifest.get("capture_artifacts"), list
        ):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _capture_purge_manifest_identity(
        value: object,
    ) -> tuple[str, str, str, str, str, str, str]:
        record = _QueueV3CandidateReader._capture_purge_manifest_mapping(value)
        _QueueV3CandidateReader._require_capture_purge_manifest_hashes(record)
        return (
            str(record["task_id"]),
            str(record["intent_id"]),
            str(record["terminal_path"]),
            str(record["kind"]),
            str(record["source_path"]),
            str(record["archive_path"]),
            str(record["sha256"]),
        )

    @staticmethod
    def _capture_purge_manifest_mapping(value: object) -> dict[str, object]:
        fields = {
            "archive_path",
            "intent_id",
            "kind",
            "sha256",
            "source_path",
            "task_id",
            "terminal_path",
        }
        if not isinstance(value, dict):
            raise QueueOperationError("export_verification_failed")
        actual = (set(value), {type(item) for item in value.values()})
        if actual != (fields, {str}):
            raise QueueOperationError("export_verification_failed")
        return value

    @staticmethod
    def _require_capture_purge_manifest_hashes(
        value: Mapping[str, object],
    ) -> None:
        if value["kind"] not in {"intent", "decision", "source"}:
            raise QueueOperationError("export_verification_failed")
        try:
            _require_lower_sha256(str(value["intent_id"]), "intent_id")
            _require_lower_sha256(str(value["sha256"]), "sha256")
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc

    @staticmethod
    def _capture_purge_manifest_artifact(
        task_id: str,
        source_path: str,
        archive_path: str,
        digest: str,
    ) -> _CapturePurgeArtifact:
        try:
            restricted_relative_path(
                source_path, ("run/capture-intents", "run/queue-results")
            )
            restricted_relative_path(archive_path, ("capture-artifacts",))
        except ValueError as exc:
            raise QueueOperationError("export_verification_failed") from exc
        if archive_path != _capture_purge_archive_path(task_id, source_path):
            raise QueueOperationError("export_verification_failed")
        return _CapturePurgeArtifact(source_path, archive_path, digest)

    def _capture_purge_evidence_from_manifest(
        self,
        value: object,
        records: tuple[dict[str, object], ...],
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not isinstance(value, list):
            raise QueueOperationError("export_verification_failed")
        grouped: dict[
            tuple[str, str, str], list[tuple[str, _CapturePurgeArtifact]]
        ] = {}
        for item in value:
            task_id, intent_id, terminal, kind, source, archive, digest = (
                self._capture_purge_manifest_identity(item)
            )
            artifact = self._capture_purge_manifest_artifact(
                task_id, source, archive, digest
            )
            grouped.setdefault((task_id, intent_id, terminal), []).append(
                (kind, artifact)
            )
        evidence = tuple(
            self._capture_purge_evidence_group(key, grouped[key])
            for key in sorted(grouped)
        )
        self._require_capture_purge_evidence_records(evidence, records)
        return evidence

    @staticmethod
    def _capture_purge_evidence_group(
        key: tuple[str, str, str],
        items: list[tuple[str, _CapturePurgeArtifact]],
    ) -> _CapturePurgeEvidence:
        intents, decisions, sources = _QueueV3CandidateReader._partition_capture_purge_artifacts(
            items
        )
        _QueueV3CandidateReader._require_unique_capture_purge_paths(items, intents)
        task_id, intent_id, terminal_path = key
        return _CapturePurgeEvidence(
            task_id, intent_id, intents[0], decisions, terminal_path, sources
        )

    @staticmethod
    def _partition_capture_purge_artifacts(
        items: list[tuple[str, _CapturePurgeArtifact]],
    ) -> tuple[tuple[_CapturePurgeArtifact, ...], ...]:
        grouped: dict[str, list[_CapturePurgeArtifact]] = {"intent": [], "decision": [], "source": []}
        for kind, artifact in items:
            grouped[kind].append(artifact)
        return tuple(tuple(grouped[kind]) for kind in ("intent", "decision", "source"))

    @staticmethod
    def _require_unique_capture_purge_paths(
        items: list[tuple[str, _CapturePurgeArtifact]],
        intents: tuple[_CapturePurgeArtifact, ...],
    ) -> None:
        paths = {artifact.source_path for _kind, artifact in items}
        if (len(intents), len(paths)) != (1, len(items)):
            raise QueueOperationError("export_verification_failed")

    @staticmethod
    def _ordinary_purge_record_capture_identity(
        record: Mapping[str, object],
    ) -> tuple[str, str, str, str] | None:
        binding = record.get("capture_binding")
        if binding is None:
            return None
        result = record.get("result")
        if (isinstance(binding, dict), isinstance(result, dict)) != (True, True):
            raise QueueOperationError("export_verification_failed")
        return _QueueV3CandidateReader._ordinary_capture_record_values(
            record, binding, result
        )

    @staticmethod
    def _ordinary_capture_record_values(
        record: Mapping[str, object],
        binding: Mapping[str, object],
        result: Mapping[str, object],
    ) -> tuple[str, str, str, str]:
        task_id = record.get("task_id")
        intent_id = binding.get("intent_id")
        intent_sha256 = binding.get("intent_sha256")
        terminal_path = result.get("reference")
        values = (task_id, intent_id, intent_sha256, terminal_path)
        if tuple(isinstance(item, str) for item in values) != (True,) * len(values):
            raise QueueOperationError("export_verification_failed")
        if terminal_path != f"run/queue-results/capture-{intent_id}.json":
            raise QueueOperationError("export_verification_failed")
        return str(task_id), str(intent_id), str(intent_sha256), str(terminal_path)

    def _require_capture_purge_evidence_records(
        self,
        evidence: tuple[_CapturePurgeEvidence, ...],
        records: tuple[dict[str, object], ...],
    ) -> None:
        expected = self._ordinary_purge_expected_capture_records(records)
        observed = {
            item.task_id: (
                item.intent_id,
                item.intent.sha256,
                item.terminal_path,
            )
            for item in evidence
        }
        if (len(observed), observed) != (len(evidence), expected):
            raise QueueOperationError("export_verification_failed")

    def _ordinary_purge_expected_capture_records(
        self, records: tuple[dict[str, object], ...]
    ) -> dict[str, tuple[str, str, str]]:
        expected: dict[str, tuple[str, str, str]] = {}
        for record in records:
            identity = self._ordinary_purge_record_capture_identity(record)
            if identity is not None:
                task_id, intent_id, intent_sha256, terminal = identity
                expected[task_id] = (intent_id, intent_sha256, terminal)
        return expected

    @staticmethod
    def _ordinary_purge_expected_results(
        records: tuple[dict[str, object], ...],
    ) -> list[dict[str, str]]:
        return [
            {"id": str(record["task_id"]), "sha256": str(record["result"]["sha256"])}
            for record in records
            if isinstance(record.get("result"), dict)
        ]

    @staticmethod
    def _capture_purge_artifacts(
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> tuple[_CapturePurgeArtifact, ...]:
        return tuple(
            artifact
            for item in evidence
            for artifact in (item.intent, *item.decisions, *item.sources)
        )

    def _require_existing_ordinary_purge_files(
        self, plan: _OrdinaryPurgePlan, manifest: Mapping[str, object]
    ) -> None:
        expected_results = self._ordinary_purge_expected_results(plan.records)
        if manifest["results"] != expected_results:
            raise QueueOperationError("export_verification_failed")
        self._require_existing_ordinary_result_files(plan.export, expected_results)
        self._require_existing_capture_purge_files(plan)

    @staticmethod
    def _require_existing_ordinary_result_files(
        export: Path, expected_results: list[dict[str, str]]
    ) -> None:
        for item in expected_results:
            relative = _ordinary_purge_result_archive_path(item["id"])
            data = _read_stable_owner_file(export / relative, _MAX_RESULT_BYTES)
            if sha256_bytes(data) != item["sha256"]:
                raise QueueOperationError("export_verification_failed")

    def _require_existing_capture_purge_files(
        self, plan: _OrdinaryPurgePlan
    ) -> None:
        for artifact in self._capture_purge_artifacts(plan.capture_evidence):
            data = _read_stable_owner_file(
                plan.export / artifact.archive_path, _MAX_QUEUE_PAYLOAD_BYTES
            )
            if sha256_bytes(data) != artifact.sha256:
                raise QueueOperationError("export_verification_failed")

    def _publish_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        if plan.manifest_bytes is not None:
            return self._existing_ordinary_purge_manifest(plan)
        return self._publish_new_ordinary_purge_export(
            plan, deadline=deadline, cancelled=cancelled
        )

    @staticmethod
    def _existing_ordinary_purge_manifest(plan: _OrdinaryPurgePlan) -> bytes:
        current = _read_stable_owner_file(
            plan.export / "manifest.json", _MAX_EXPORT_METADATA_BYTES
        )
        if current != plan.manifest_bytes:
            raise QueueOperationError("export_verification_failed")
        return current

    def _publish_new_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        parent = plan.export.parent
        parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(parent, 0o700)
        actual = (parent.is_symlink(), parent.is_dir(), _is_owner_only(parent))
        if actual != (False, True, True):
            raise QueueOperationError("export_parent_permissions_invalid")
        _cleanup_export_staging(parent, plan.export.name)
        staging = parent / f".{plan.export.name}.staging-{uuid.uuid4().hex}"
        staging.mkdir()
        _harden_owner_only(staging, 0o700)
        try:
            manifest_bytes = self._write_ordinary_purge_export(
                plan, staging, deadline=deadline, cancelled=cancelled
            )
            _require_active(deadline, cancelled)
            staging.replace(plan.export)
            fsync_directory(parent)
            return manifest_bytes
        except BaseException:
            _remove_export_staging(staging)
            raise

    def _write_ordinary_purge_export(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> bytes:
        results_export = staging / "results"
        results_export.mkdir()
        _harden_owner_only(results_export, 0o700)
        results = self._ordinary_purge_result_manifest(
            plan.records,
            results_export,
            deadline=deadline,
            cancelled=cancelled,
        )
        capture_artifacts = self._ordinary_purge_capture_manifest(
            plan,
            staging,
            deadline=deadline,
            cancelled=cancelled,
        )
        manifest_bytes = canonical_json_bytes(
            {
                "capture_artifacts": capture_artifacts,
                "cutoff": plan.cutoff,
                "records_sha256": sha256_bytes(plan.records_bytes),
                "results": results,
                "task_ids": list(plan.task_ids),
            }
        )
        records_path = staging / "records.json"
        manifest_path = staging / "manifest.json"
        _write_durable_file(records_path, plan.records_bytes)
        _write_durable_file(manifest_path, manifest_bytes)
        self._require_ordinary_purge_export(
            records_path, plan.records_bytes, manifest_path, manifest_bytes
        )
        fsync_directory(results_export)
        fsync_directory(staging)
        return manifest_bytes

    def _ordinary_purge_capture_manifest(
        self,
        plan: _OrdinaryPurgePlan,
        staging: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        manifest: list[dict[str, str]] = []
        for evidence in plan.capture_evidence:
            _require_active(deadline, cancelled)
            for kind, artifact in self._tagged_capture_purge_artifacts(evidence):
                manifest.append(
                    self._publish_capture_purge_artifact(
                        staging, evidence, artifact, kind=kind
                    )
                )
        capture_dir = staging / "capture-artifacts"
        if capture_dir.exists():
            fsync_directory(capture_dir)
        return manifest

    @staticmethod
    def _tagged_capture_purge_artifacts(evidence: _CapturePurgeEvidence):
        yield "intent", evidence.intent
        for artifact in evidence.decisions:
            yield "decision", artifact
        for artifact in evidence.sources:
            yield "source", artifact

    def _publish_capture_purge_artifact(
        self,
        staging: Path,
        evidence: _CapturePurgeEvidence,
        artifact: _CapturePurgeArtifact,
        *,
        kind: Literal["intent", "decision", "source"],
    ) -> dict[str, str]:
        data = read_runtime_bytes(
            self.state_root / artifact.source_path,
            self.state_root,
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(data) != artifact.sha256:
            raise QueueOperationError("capture_purge_changed")
        target = staging / artifact.archive_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _harden_owner_only(target.parent, 0o700)
        _write_durable_file(target, data)
        return {
            "archive_path": artifact.archive_path,
            "intent_id": evidence.intent_id,
            "kind": kind,
            "sha256": artifact.sha256,
            "source_path": artifact.source_path,
            "task_id": evidence.task_id,
            "terminal_path": evidence.terminal_path,
        }

    def _ordinary_purge_result_manifest(
        self,
        records: tuple[dict[str, object], ...],
        results_export: Path,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> list[dict[str, str]]:
        result_manifest: list[dict[str, str]] = []
        for record in records:
            _require_active(deadline, cancelled)
            result = record["result"]
            if not isinstance(result, dict):
                continue
            reference = str(result["reference"])
            digest = str(result["sha256"])
            data = _read_stable_owner_file(
                self.state_root / reference, _MAX_RESULT_BYTES
            )
            if sha256_bytes(data) != digest:
                raise QueueOperationError("result_verification_failed")
            archive = _ordinary_purge_result_archive_path(str(record["task_id"]))
            _write_durable_file(results_export.parent / archive, data)
            result_manifest.append({"id": str(record["task_id"]), "sha256": digest})
        return result_manifest

    @staticmethod
    def _require_ordinary_purge_export(
        records_path: Path,
        records_bytes: bytes,
        manifest_path: Path,
        manifest_bytes: bytes,
    ) -> None:
        actual = (
            _read_stable_owner_file(records_path, _MAX_EXPORT_METADATA_BYTES),
            _read_stable_owner_file(manifest_path, _MAX_EXPORT_METADATA_BYTES),
        )
        if actual != (records_bytes, manifest_bytes):
            raise QueueOperationError("export_verification_failed")

    def _commit_ordinary_purge(
        self,
        plan: _OrdinaryPurgePlan,
        manifest_bytes: bytes,
        *,
        deadline: float,
        cancelled: Callable[[], bool] | None,
    ) -> tuple[_CapturePurgeEvidence, ...]:
        if not plan.task_ids:
            return ()
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        receipt_published = self._ordinary_purge_receipt_matches(
            plan, manifest_bytes
        )
        expected = {item.task_id: item for item in plan.capture_evidence}
        with closing(self._connect()) as database, begin_immediate(
            database,
            before_commit=lambda: _require_active(deadline, cancelled),
        ):
            state = self._ordinary_purge_commit_state(
                database,
                plan,
                operation_id=operation_id,
                manifest_sha256=manifest_sha256,
                receipt_published=receipt_published,
            )
            if state == "pending":
                for task_id, expected_record in zip(plan.task_ids, plan.records):
                    _require_active(deadline, cancelled)
                    self._purge_ordinary_task(
                        database,
                        task_id,
                        expected_record,
                        expected_capture=expected.get(task_id),
                        cutoff=plan.cutoff,
                        operation_id=operation_id,
                        manifest_sha256=manifest_sha256,
                    )
        return plan.capture_evidence

    def _ordinary_purge_commit_state(
        self,
        database: sqlite3.Connection,
        plan: _OrdinaryPurgePlan,
        *,
        operation_id: str,
        manifest_sha256: str,
        receipt_published: bool,
    ) -> str:
        placeholders = ",".join("?" for _ in plan.task_ids)
        present = database.execute(
            f"SELECT id FROM tasks WHERE id IN ({placeholders})",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        authorizations = database.execute(
            f"""SELECT * FROM task_purge_authorizations
                WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
            plan.task_ids,
        ).fetchall()
        counts = (len(present), len(authorizations))
        if counts == (len(plan.task_ids), 0):
            return "pending"
        return self._settled_ordinary_purge_state(
            counts,
            authorizations,
            plan,
            operation_id=operation_id,
            manifest_sha256=manifest_sha256,
            receipt_published=receipt_published,
        )

    def _settled_ordinary_purge_state(
        self,
        counts: tuple[int, int],
        authorizations: list,
        plan: _OrdinaryPurgePlan,
        *,
        operation_id: str,
        manifest_sha256: str,
        receipt_published: bool,
    ) -> str:
        """Where a resumed purge stands once its tasks are already gone."""
        if counts == (0, len(plan.task_ids)):
            self._require_ordinary_purge_authorizations(
                authorizations, plan.task_ids, operation_id, manifest_sha256
            )
            return "cleanup"
        if (counts, receipt_published) == ((0, 0), True):
            return "complete"
        raise QueueOperationError("purge_resume_conflict")

    @staticmethod
    def _require_ordinary_purge_authorizations(
        rows: list[sqlite3.Row],
        task_ids: tuple[str, ...],
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        expected = sorted(
            (
                task_id,
                "ordinary",
                operation_id,
                _ordinary_purge_authorization_digest(
                    task_id, operation_id, manifest_sha256
                ),
            )
            for task_id in task_ids
        )
        actual = sorted(
            (
                str(row["task_id"]),
                str(row["mode"]),
                str(row["operation_id"]),
                str(row["authorization_digest"]),
            )
            for row in rows
        )
        if actual != expected:
            raise QueueOperationError("purge_authorization_failed")

    def _purge_ordinary_task(
        self,
        database: sqlite3.Connection,
        task_id: str,
        expected_record: Mapping[str, object],
        *,
        expected_capture: _CapturePurgeEvidence | None,
        cutoff: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        row = database.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        self._require_ordinary_purge_task(row, cutoff)
        self._require_ordinary_purge_payload(database, row)
        self._require_ordinary_purge_record(database, row, expected_record)
        evidence = self._ordinary_capture_purge_evidence(database, task_id)
        expected_evidence = () if expected_capture is None else (expected_capture,)
        if evidence != expected_evidence:
            raise QueueOperationError("capture_purge_changed")
        self._insert_ordinary_purge_authorization(
            database, task_id, operation_id, manifest_sha256
        )
        self._delete_capture_purge_rows_before_task(database, evidence)
        self._delete_task_owned_evidence(database, task_id)
        self._delete_ordinary_task(database, task_id)
        self._delete_capture_purge_rows_after_task(database, evidence)

    @staticmethod
    def _require_ordinary_purge_task(row: sqlite3.Row | None, cutoff: str) -> None:
        """Still a finished row an ordinary purge may take, still older than the cutoff.

        Which of those states this particular purge planned for is pinned by the
        exported record, compared field for field a moment later, so this guard
        names the states the operation can take at all and no narrower.
        """
        if (
            row is None
            or row["state"] not in _ORDINARY_PURGEABLE_STATES
            or row["updated_at"] >= cutoff
        ):
            raise QueueOperationError("purge_selection_changed")

    def _require_ordinary_purge_payload(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> None:
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            self._demote_payload_mismatch(database, row, now=_utc_now())
            raise QueueOperationError("payload_hash_mismatch")

    def _require_ordinary_purge_record(
        self,
        database: sqlite3.Connection,
        row: sqlite3.Row,
        expected_record: Mapping[str, object],
    ) -> None:
        current = self._export_task_in_transaction(database, row)
        if canonical_json_bytes(current) != canonical_json_bytes(expected_record):
            raise QueueOperationError("purge_selection_changed")

    def _ordinary_capture_purge_evidence(
        self, database: sqlite3.Connection, task_id: str
    ) -> tuple[_CapturePurgeEvidence, ...]:
        link = database.execute(
            "SELECT 1 FROM capture_task_links WHERE task_id=?", (task_id,)
        ).fetchone()
        if link is None:
            return ()
        binding = self.active_capture_binding(database, task_id)
        return (self._capture_purge_evidence(database, task_id, binding),)

    @staticmethod
    def _insert_ordinary_purge_authorization(
        database: sqlite3.Connection,
        task_id: str,
        operation_id: str,
        manifest_sha256: str,
    ) -> None:
        authorization_digest = _ordinary_purge_authorization_digest(
            task_id, operation_id, manifest_sha256
        )
        inserted = database.execute(
            """INSERT INTO task_purge_authorizations(
                   task_id,mode,operation_id,authorization_digest,created_at
               ) VALUES (?,'ordinary',?,?,?)""",
            (task_id, operation_id, authorization_digest, _timestamp(_utc_now())),
        ).rowcount
        if inserted != 1:
            raise QueueOperationError("purge_authorization_failed")

    @staticmethod
    def _delete_capture_purge_rows_before_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM semantic_decisions WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != len(item.decisions):
                raise QueueOperationError("capture_purge_changed")

    @staticmethod
    def _delete_ordinary_task(
        database: sqlite3.Connection, task_id: str
    ) -> None:
        deleted = database.execute(
            "DELETE FROM tasks WHERE id=?", (task_id,)
        ).rowcount
        if deleted != 1:
            raise QueueOperationError("purge_delete_failed")

    @staticmethod
    def _delete_capture_purge_rows_after_task(
        database: sqlite3.Connection,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        for item in evidence:
            deleted = database.execute(
                "DELETE FROM capture_intents WHERE intent_id=?", (item.intent_id,)
            ).rowcount
            if deleted != 1:
                raise QueueOperationError("capture_purge_changed")

    def _cleanup_ordinary_purge_artifacts(
        self,
        plan: _OrdinaryPurgePlan,
        evidence: tuple[_CapturePurgeEvidence, ...],
    ) -> None:
        retained_terminals = {item.terminal_path for item in evidence}
        for item in evidence:
            self._unlink_capture_purge_artifacts(plan.export, item)
        for record in plan.records:
            self._cleanup_ordinary_result(record, retained_terminals)

    def _cleanup_ordinary_result(
        self, record: Mapping[str, object], retained_terminals: set[str]
    ) -> None:
        """Drop this export's result file unless something still names it."""
        reference = _cleanable_result_reference(record, retained_terminals)
        if reference is None:
            return
        with closing(self._connect()) as database, begin_immediate(database):
            if self._result_reference_retained(database, reference):
                return
            path = self.state_root / reference
            path.unlink(missing_ok=True)
            fsync_directory(path.parent)

    @staticmethod
    def _result_reference_retained(database, reference: str) -> bool:
        retained = database.execute(
            "SELECT 1 FROM tasks WHERE result_reference=? LIMIT 1", (reference,)
        ).fetchone()
        return retained is not None or compile_progress.retains_artifact(database, reference)

    def _unlink_capture_purge_artifacts(
        self, export: Path, evidence: _CapturePurgeEvidence
    ) -> None:
        for artifact in (evidence.intent, *evidence.decisions, *evidence.sources):
            self._unlink_capture_purge_artifact(export, evidence.task_id, artifact)

    def _unlink_capture_purge_artifact(
        self,
        export: Path,
        task_id: str,
        artifact: _CapturePurgeArtifact,
    ) -> None:
        self._capture_purge_manifest_artifact(
            task_id, artifact.source_path, artifact.archive_path, artifact.sha256
        )
        archived = _read_stable_owner_file(
            export / artifact.archive_path, _MAX_QUEUE_PAYLOAD_BYTES
        )
        if sha256_bytes(archived) != artifact.sha256:
            raise QueueOperationError("export_verification_failed")
        source = self.state_root / artifact.source_path
        try:
            before = source.lstat()
        except FileNotFoundError:
            return
        with closing(self._connect()) as database, begin_immediate(database):
            if self._result_reference_retained(database, artifact.source_path):
                return
            self._unlink_verified_source(source, before, artifact)

    def _unlink_verified_source(
        self, source: Path, before: os.stat_result, artifact: _CapturePurgeArtifact
    ) -> None:
        """Remove the source only while it is still what the export recorded."""
        current = read_runtime_bytes(
            source,
            self.state_root,
            max_bytes=_MAX_QUEUE_PAYLOAD_BYTES,
            owner_only=True,
        )
        if sha256_bytes(current) != artifact.sha256 or not os.path.samestat(
            before, source.lstat()
        ):
            raise QueueOperationError("capture_purge_changed")
        source.unlink()
        fsync_directory(source.parent)

    @staticmethod
    def _ordinary_purge_receipt_bytes(
        plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bytes:
        manifest_sha256 = sha256_bytes(manifest_bytes)
        return canonical_json_bytes(
            {
                "schema_version": "ordinary-purge-receipt/v1",
                "operation_id": _ordinary_purge_operation_id(manifest_sha256),
                "manifest_sha256": manifest_sha256,
                "task_ids": list(plan.task_ids),
            }
        )

    def _ordinary_purge_receipt_matches(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> bool:
        path = plan.export / "purge-receipt.json"
        try:
            current = _read_stable_owner_file(path, 64 * 1024)
        except FileNotFoundError:
            return False
        if current != self._ordinary_purge_receipt_bytes(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")
        return True

    def _publish_ordinary_purge_receipt(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        receipt_bytes = self._ordinary_purge_receipt_bytes(plan, manifest_bytes)
        _write_durable_file(plan.export / "purge-receipt.json", receipt_bytes)
        if not self._ordinary_purge_receipt_matches(plan, manifest_bytes):
            raise QueueOperationError("purge_receipt_invalid")

    def _clear_ordinary_purge_authorizations(
        self, plan: _OrdinaryPurgePlan, manifest_bytes: bytes
    ) -> None:
        if not plan.task_ids:
            return
        manifest_sha256 = sha256_bytes(manifest_bytes)
        operation_id = _ordinary_purge_operation_id(manifest_sha256)
        placeholders = ",".join("?" for _ in plan.task_ids)
        with closing(self._connect()) as database, begin_immediate(database):
            rows = database.execute(
                f"""SELECT * FROM task_purge_authorizations
                    WHERE task_id IN ({placeholders}) ORDER BY task_id""",  # noqa: S608
                plan.task_ids,
            ).fetchall()
            if not rows:
                return
            self._require_ordinary_purge_authorizations(
                rows, plan.task_ids, operation_id, manifest_sha256
            )
            _delete_ordinary_purge_authorizations(database, plan.task_ids, placeholders)

    def _export_task_in_transaction(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, object]:
        """The export record for one task, read inside an open transaction."""
        task_id = str(row["id"])
        validation = validate_payload_blob(
            bytes(row["payload_blob"]), row["input_hash"], parse=True
        )
        if validation.code is not None:
            raise QueueOperationError("payload_hash_mismatch")
        rows = _read_exported_rows(database, task_id)
        return _exported_task_record(row, validation.payload, rows)


@contextmanager
def capture_task_fences(
    queue: _QueueV3CandidateReader,
    coordinator: object,
    task_id: str,
    *,
    intent_id: str | None,
    mode: Literal["worker", "queue-operator"],
    owner: OwnerLease,
) -> Iterator[tuple[TaskFence, object | None]]:
    task_fence = queue.acquire_task_fence(task_id, mode=mode, owner=owner)
    intent_fence = None
    try:
        if intent_id is not None:
            intent_mode = "worker" if mode == "worker" else "operator"
            intent_fence = coordinator.acquire_intent_fence(
                intent_id, mode=intent_mode, owner=owner
            )
        yield task_fence, intent_fence
    finally:
        if intent_fence is not None:
            coordinator.release_intent_fence(intent_fence)
        queue.release_task_fence(task_fence)


def _state_root() -> Path:
    env = os.environ.get("LLM_WIKI_STATE_ROOT")
    if env:
        return Path(env).resolve()
    try:
        from memory_state import STATE_ROOT

        return Path(STATE_ROOT)
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("LLM_WIKI_ROOT", Path(__file__).resolve().parent.parent))


def _fence_owner_alive(row: sqlite3.Row) -> bool:
    """A v3 fence names its owner's process, so a reused PID is not its owner (C-12)."""
    import process_liveness

    return process_liveness.owner_alive(int(row["owner_pid"]), str(row["owner_start_identity"]))


# Columns added to the queue v2 tables after the original schema: the table,
# the column, the statement that adds it, and the backfill it needs, if any.
_QUEUE_V2_ADDED_COLUMNS = (
    ("tasks", "last_attempt_at", "ALTER TABLE tasks ADD COLUMN last_attempt_at TEXT", None),
    ("tasks", "result_sha256", "ALTER TABLE tasks ADD COLUMN result_sha256 TEXT", None),
    (
        "tasks",
        "redrive_of",
        "ALTER TABLE tasks ADD COLUMN redrive_of TEXT REFERENCES tasks(id)",
        None,
    ),
    (
        "source_fences",
        "heartbeat_at",
        "ALTER TABLE source_fences ADD COLUMN heartbeat_at TEXT",
        "UPDATE source_fences SET heartbeat_at=acquired_at WHERE heartbeat_at IS NULL",
    ),
    (
        "source_fences",
        "expires_at",
        "ALTER TABLE source_fences ADD COLUMN expires_at TEXT",
        "UPDATE source_fences SET expires_at=acquired_at WHERE expires_at IS NULL",
    ),
)


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """The column names a table currently has."""
    rows = connection.execute(f"PRAGMA table_info({quote_sqlite_identifier(table)})").fetchall()
    return {str(row["name"]) for row in rows}


def _run_optional_statement(connection: sqlite3.Connection, sql: str | None) -> None:
    """Run the statement if this column needs one."""
    if sql is None:
        return
    connection.execute(sql)


def _add_missing_queue_columns(connection: sqlite3.Connection) -> None:
    """Bring an older queue v2 schema up to the current column set."""
    present = {
        table: _table_columns(connection, table)
        for table in ("tasks", "source_fences")
    }
    for table, column, add, backfill in _QUEUE_V2_ADDED_COLUMNS:
        if column in present[table]:
            continue
        connection.execute(add)
        _run_optional_statement(connection, backfill)


def _verify_open_artifact(handle, path, before, expected, deadline):
    _require_stream_identity(path, handle.fileno(), before)
    actual, _ = _stream_digest(handle, before.st_size, deadline=deadline)
    if actual != expected:
        raise compile_progress.ProgressCorrupt('compile artifact digest mismatch')
    _require_stream_identity(path, handle.fileno(), before)
    handle.seek(0)


def _vault_root() -> Path:
    """The vault this process belongs to, resolved the way writers resolve it."""
    return Path(
        os.environ.get("LLM_WIKI_ROOT", str(Path(__file__).resolve().parent.parent))
    ).resolve()


def _queue(
    *,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
) -> MemoryQueue | _QueueV3CandidateReader:
    return active_or_legacy_memory_queue(
        _vault_root(),
        _state_root(),
        policy=QueueRetryPolicy(
            max_attempts, retry_base_seconds, retry_cap_seconds
        ),
    )


def _v3_queue_for_cli() -> _QueueV3CandidateReader:
    """The v3 queue the repair commands work on: adopted, else the candidate."""
    from markdown_transaction import _reliability_v3_records_present

    state_root = _state_root()
    if _reliability_v3_records_present(state_root):
        return active_memory_queue(_vault_root(), state_root)
    return MemoryQueue._from_v3_candidate(
        state_root / "run" / "queue-v3.candidate.sqlite3",
        state_root=state_root, vault=_vault_root(),
    )


def active_memory_queue(vault: Path, state_root: Path, *, deadline: float | None = None) -> _QueueV3CandidateReader:
    """Open the queue side of an adopted pair with verified files and contracts.

    Normal admission checks adoption evidence, identities and schema; complete
    retained-history certification belongs to adoption, backup and doctor.
    """
    from installed_memory_repair import _read_adoption_record
    from markdown_transaction import _require_adopted_once

    resolved_vault = Path(vault).resolve(strict=True)
    state = Path(state_root).absolute()
    _require_adopted_once(resolved_vault, state, deadline=deadline)
    queue_path = state / "run" / "queue-v3.sqlite3"
    coordinator_path = state / "run" / "markdown-transactions-v3.sqlite3"
    adoption = _read_adoption_record(state / "run/reliability-v3-adopted.json", state)
    contract = _adopted_queue_contract(adoption)
    require_queue_v3_openable(
        queue_path, state_root=state, deadline=deadline, expected_contract=contract,
    )
    return _QueueV3CandidateReader(
        queue_path, coordinator_path=coordinator_path, vault=resolved_vault,
        expected_contract=contract,
    )


def _adopted_queue_contract(adoption: dict) -> OperationalDatabaseContract:
    if adoption["schema_version"] in {"reliability-v3-adoption/v2", "reliability-v3-adoption/v3"}:
        return _QUEUE_COMPILE_CONTRACT
    return _QUEUE_V3_CONTRACT


def _json_queue_entries(state_root: Path, *, deadline: float | None = None) -> int:
    """Entries under `run/queue/`, the file-per-task queue of v3.3.0–v3.4.0.

    An unreadable directory or a symlink counts as one: unknown is not empty.
    """
    directory = Path(state_root) / "run" / "queue"
    if directory.is_symlink():
        return 1
    if not directory.is_dir():
        return 0
    try:
        with os.scandir(directory) as entries:
            return sum(_deadline_queue_entry(deadline) for _ in entries)
    except OSError:
        return 1


def _deadline_queue_entry(deadline: float | None) -> int:
    operational_deadline_active(deadline)
    return 1


def _require_no_json_queue(state_root: Path, *, deadline: float | None = None) -> None:
    """Records of the JSON queue are refused, never imported or dropped.

    No release since v4.0.0 wrote that queue and, since 2026-09-23, none reads
    it; the installer's adoption refuses on the same directory. See
    `docs/research/2026-09-23-the-json-queue-import-goes.md`.
    """
    operational_deadline_active(deadline)
    if _json_queue_entries(state_root, deadline=deadline):
        raise QueueOperationError("legacy_json_queue_unsupported")


def _legacy_memory_queue(
    state_root: Path, policy: QueueRetryPolicy | None, *, deadline: float | None = None
) -> MemoryQueue:
    """The pre-adoption queue, given the caller's bounds only if it named any."""
    _require_no_json_queue(state_root, deadline=deadline)
    if policy is None:
        return MemoryQueue(state_root, deadline=deadline)
    return MemoryQueue(
        state_root,
        max_attempts=policy.max_attempts,
        retry_base_seconds=policy.retry_base_seconds,
        retry_cap_seconds=policy.retry_cap_seconds,
        deadline=deadline,
    )


_SOURCE_FAILURE_KEYS = "SELECT logical_path, source_digest FROM source_failures ORDER BY logical_path, source_digest"


def active_or_legacy_memory_queue(
    vault: Path,
    state_root: Path,
    *,
    policy: QueueRetryPolicy | None = None,
    deadline: float | None = None,
) -> MemoryQueue | _QueueV3CandidateReader:
    """The adopted V3 queue where adoption is in force, else the legacy one.

    Adoption replaces the pre-adoption `run/queue.sqlite3` with a JSON tombstone,
    so a reader that opens that path directly fails with `file is not a database`
    on every call. This is the queue's instance of the one rule
    `markdown_transaction.active_or_legacy_coordinator` states for the
    coordinator, and it reads the same evidence; a caller that constructs
    `MemoryQueue` itself bypasses adoption and dies on the tombstone.

    `policy` belongs to the legacy queue alone: the adopted backend settles
    retries by its published contract and takes `max_attempts` per call. Callers
    with no policy of their own pass none, and construct exactly as they did.
    """
    from markdown_transaction import _reliability_v3_records_present

    operational_deadline_active(deadline)
    if _reliability_v3_records_present(state_root):
        return active_memory_queue(vault, state_root, deadline=deadline)
    return _legacy_memory_queue(state_root, policy, deadline=deadline)


@contextmanager
def _repair_owner_for_cli(queue: _QueueV3CandidateReader) -> Iterator[OwnerLease]:
    """A repair owner in the registry that pairs with the queue being repaired."""
    registry = queue.ownership_registry()
    owner = registry.acquire("repair", scope=f"repair:cli:{os.getpid()}")
    try:
        yield owner
    finally:
        registry.release(owner)


def enqueue(task_type: str, payload: dict[str, Any]) -> str:
    """Compatibility facade that enqueues handler version 1."""
    return _queue().enqueue(task_type, 1, payload)


def _compat_task(task: QueueTask | QueueLease) -> dict[str, Any]:
    created = task.created_at
    last_attempt = task.last_attempt_at
    attempts = task.attempts if isinstance(task, QueueTask) else task.prior_attempts
    return {
        "id": task.id,
        "type": task.kind,
        "handler_version": task.handler_version,
        "enqueued_at": created.isoformat(timespec="seconds"),
        "attempts": attempts,
        "last_attempt_at": (
            last_attempt.isoformat(timespec="seconds") if last_attempt else None
        ),
        "payload": task.payload,
    }


def _as_positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise QueueOperationError(
            "restore_record_invalid", f"{field} is not a positive integer"
        )
    return value


def _as_priority(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(-100, min(100, value))


def _restore_json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not JSON") from None
    if not isinstance(value, dict):
        raise QueueOperationError("restore_manifest_invalid", "manifest is not an object")
    return value


def _restored_task_id(queue: object, record: Mapping[str, object]) -> str:
    """Re-enqueue one exported record onto `queue` as new ready work."""
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise QueueOperationError("restore_record_invalid", "payload is not an object")
    return queue.enqueue(  # type: ignore[attr-defined]
        str(record.get("kind") or ""),
        _as_positive_int(record.get("handler_version"), "handler_version"),
        payload,
        priority=_as_priority(record.get("priority")),
    )


def _restore_from_export(
    queue: object,
    *,
    export_path: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None,
) -> RestoreReceipt:
    """Re-enqueue the work in one verified purge export onto `queue`."""
    _require_active(deadline, cancelled)
    records = _verified_export_records(Path(export_path).absolute())
    restored: list[tuple[str, str]] = []
    for record in records:
        _require_active(deadline, cancelled)
        restored.append((str(record["id"]), _restored_task_id(queue, record)))
    return RestoreReceipt(len(restored), tuple(restored))


def _verified_export_records(export: Path) -> list[dict[str, object]]:
    """Read one purge export only when its manifest and digests all verify."""
    manifest = _restore_json_object(
        _read_stable_owner_file(export / "manifest.json", _MAX_EXPORT_METADATA_BYTES)
    )
    records_bytes = _read_stable_owner_file(
        export / "records.json", _MAX_EXPORT_METADATA_BYTES
    )
    if sha256_bytes(records_bytes) != manifest.get("records_sha256"):
        raise QueueOperationError("restore_verification_failed", "records digest mismatch")
    _verify_exported_results(export, manifest)
    records = json.loads(records_bytes.decode("utf-8"))
    _require_record_list(records, manifest)
    return records


def _is_object_list(records: object) -> bool:
    if not isinstance(records, list):
        return False
    return all(isinstance(item, dict) for item in records)


def _manifest_task_ids(manifest: Mapping[str, object]) -> list[str]:
    declared = manifest.get("task_ids") or []
    return [str(item) for item in declared]


def _require_record_list(records: object, manifest: Mapping[str, object]) -> None:
    if not _is_object_list(records):
        raise QueueOperationError(
            "restore_manifest_invalid", "records are not a list of objects"
        )
    exported = [str(item.get("id")) for item in records]
    if exported != _manifest_task_ids(manifest):
        raise QueueOperationError("restore_verification_failed", "task ids do not match")


def _verify_exported_results(export: Path, manifest: Mapping[str, object]) -> None:
    for item in manifest.get("results") or []:
        if not isinstance(item, Mapping):
            raise QueueOperationError(
                "restore_manifest_invalid", "result entry is not an object"
            )
        data = _read_stable_owner_file(
            export / "results" / f"{item.get('id')}.result", _MAX_RESULT_BYTES
        )
        if sha256_bytes(data) != item.get("sha256"):
            raise QueueOperationError("restore_verification_failed", "result digest mismatch")


def cancel(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    return _queue().cancel(task_id, deadline=deadline, cancelled=cancelled)


def redrive(
    task_id: str,
    *,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> str:
    return _queue().redrive(task_id, deadline=deadline, cancelled=cancelled)


def purge(
    *,
    terminal_before: datetime,
    export_path: Path,
    include_dead: bool = False,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> PurgeReceipt:
    return _queue().purge(
        terminal_before=terminal_before,
        export_path=export_path,
        include_dead=include_dead,
        deadline=deadline,
        cancelled=cancelled,
    )


def restore(
    *,
    export_path: Path,
    deadline: float = float("inf"),
    cancelled: Callable[[], bool] | None = None,
) -> RestoreReceipt:
    return _queue().restore(
        export_path=export_path,
        deadline=deadline,
        cancelled=cancelled,
    )


def _run_processor_inline(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
) -> bool | DeferredResult:
    del timeout
    return processor(task)


def _record_processor_failure(task: dict[str, Any], error: BaseException) -> None:
    """Leave the reason on disk before the wire flattens it to one letter.

    The frame deliberately carries a stable code and no traceback, so the
    parent can only ever record `processor_failed`. Measured 2026-08-28 on the
    live vault: 72 such attempts and eleven `flush` tasks stuck at eight
    attempts each, with no reason anywhere — not in `attempt_history`, not in
    the step's stderr. Eleven sessions had failed to become memory and the
    trail could not say why. Recording here keeps the wire contract and still
    answers the question.
    """
    try:
        from capture_diagnostics import record_capture_failure

        kind = str(task.get("kind", "unknown"))
        reason = f"{kind}: {type(error).__name__}: {redact_secrets(str(error))}"
        record_capture_failure(
            "queue_processor", reason[:MAX_PROCESSOR_REASON_CHARS],
            error=error,
            session_id=str(task.get("id", ""))[:32] or None,
        )
    except Exception:  # noqa: BLE001 - diagnostics never change the outcome
        pass


def _processor_result_frame(
    processor: Callable[[dict], bool | DeferredResult], task: dict[str, Any]
) -> bytes:
    """Run the task; a failure is reported as a stable code, never a traceback."""
    try:
        return _encode_processor_outcome(processor(task))
    except Exception as error:  # noqa: BLE001 - parent receives a stable code only
        _record_processor_failure(task, error)
        return b"E"


def _processor_child_entry(
    sender: Any,
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
) -> None:
    try:
        # Results use sender, never stdout (which may carry the parent's MCP).
        os.dup2(2, 1)
        if os.name != "nt":
            os.setsid()
        _send_processor_frame(sender, _processor_result_frame(processor, task))
    except Exception:  # noqa: BLE001 - parent receives a stable code only
        pass
    finally:
        sender.close()


def _run_processor_child(
    processor: Callable[[dict], bool | DeferredResult],
    task: dict[str, Any],
    timeout: float,
    lease_lost: Callable[[], bool] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> bool | DeferredResult:
    return _run_child_runtime(
        processor, task, timeout, lease_lost, cancelled,
        child_entry=_processor_child_entry,
    )


# The pause between claim attempts while the queue database is busy, within the
# caller's deadline. Basis unknown: value predates measurement; review if claims
# show contention in doctor or the nightly log.
_CLAIM_BUSY_RETRY_SECONDS = 0.05


def _is_busy_database(error: sqlite3.OperationalError) -> bool:
    from lease_renewal import busy_database

    return busy_database(error)


def _worker_claim_options(lease_seconds, max_attempts, task_id):
    options = {'lease_seconds': lease_seconds, 'max_attempts': max_attempts}
    if task_id is not None:
        options['task_id'] = task_id
    return options


def _worker_claim_active(deadline, monotonic, cancelled):
    return monotonic() < deadline and not (cancelled is not None and cancelled())


def _claim_when_reachable(
    queue: MemoryQueue,
    *,
    owner: str,
    lease_seconds: int,
    max_attempts: int,
    deadline: float,
    monotonic: Callable[[], float],
    task_id: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> QueueLease | None:
    """A busy database means "try again shortly", not a dead worker.

    Another worker holding the write lock used to surface as an unhandled
    `database is locked` out of the worker loop, which ended the run.
    """
    options = _worker_claim_options(lease_seconds, max_attempts, task_id)
    while _worker_claim_active(deadline, monotonic, cancelled):
        try:
            return queue.claim(owner, **options)
        except sqlite3.OperationalError as error:
            # Only a busy database is "nothing to claim yet". A disk I/O error or
            # a missing table is a failure the operator has to see, not an idle queue.
            if not _is_busy_database(error):
                raise
            time.sleep(max(0.0, min(_CLAIM_BUSY_RETRY_SECONDS, deadline - monotonic())))
    return None


def _adopted_counts(
    queue: MemoryQueue, lease: QueueLease, counts: dict[str, int]
) -> bool:
    """True when a previously published result settled this lease already."""
    adopted = queue.adopt_published_result(lease, operation_id=lease.id)
    if adopted == "adopted":
        _count_terminal(counts, queue.acknowledge(lease))
        return True
    if adopted == "corrupt":
        _count_terminal(counts, queue.get(lease.id))
        return True
    return False


def _run_processor(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    remaining: float,
) -> _ProcessorOutcome:
    try:
        outcome = processor_runner(processor, _compat_task(lease), remaining)
    except TimeoutError:
        return _ProcessorOutcome(False, True, False)
    except QueueOperationError as exc:
        return _ProcessorOutcome(False, False, exc.code == "process_cleanup_failed", exc.code)
    except Exception as exc:  # noqa: BLE001 - queue exposes stable codes only
        return _ProcessorOutcome(False, False, False, processor_error_code(exc))
    return _ProcessorOutcome(outcome, False, False)


def _interruptible(processor_runner, heartbeat: _LeaseHeartbeat, *, cancelled=None):
    """The child runner stops its child when the lease is lost; injected runners stay as they are."""
    if processor_runner is not _run_processor_child:
        return processor_runner
    return partial(_run_processor_child, lease_lost=lambda: heartbeat.error is not None,
                   cancelled=cancelled)


def _processor_result(
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    heartbeat: _LeaseHeartbeat,
    deadline: float,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None = None,
) -> _ProcessorOutcome:
    heartbeat.start()
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return _ProcessorOutcome(False, True, False)
        result = _run_processor(processor, _interruptible(processor_runner, heartbeat, cancelled=cancelled), lease, remaining)
        if monotonic() >= deadline:
            return _ProcessorOutcome(result.outcome, True, result.cleanup_failed, result.error_code)
        return result
    finally:
        heartbeat.stop()


def _fail_lease(
    queue: MemoryQueue,
    lease: QueueLease,
    failure: QueueFailure,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    queue.fail(
        lease,
        failure,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _count_terminal(counts, queue.get(lease.id))


def _unsuccessful_worker_failure(result: _ProcessorOutcome) -> QueueFailure | None:
    if result.timed_out:
        return QueueFailure("worker_timeout")
    if not bool(result.outcome):
        return QueueFailure(result.error_code or "processor_failed")
    return None


def _worker_failure(result: _ProcessorOutcome) -> QueueFailure | None:
    """The failure this outcome means, or None when the task succeeded."""
    if result.cleanup_failed:
        return QueueFailure(
            "process_cleanup_failed", blocked_capability="process_cleanup"
        )
    if result.error_code == 'worker_cancelled':
        return QueueFailure('worker_cancelled', blocked_capability='caller_cancelled')
    return _unsuccessful_worker_failure(result)


def _settle_worker_failure(
    queue: MemoryQueue,
    lease: QueueLease,
    result: _ProcessorOutcome,
    failure: QueueFailure,
    counts: dict[str, int],
    bounds: dict[str, int],
) -> None:
    """Count a halted cleanup, then settle the lease as failed."""
    if result.cleanup_failed:
        counts["halted"] = 1
    _fail_lease(queue, lease, failure, counts, **bounds)


def _published_result_data(outcome: bool | DeferredResult) -> bytes:
    return outcome.data if isinstance(outcome, DeferredResult) else b""


def _settle_processor_outcome(
    queue: MemoryQueue,
    lease: QueueLease,
    result: _ProcessorOutcome,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    bounds = {
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    failure = _worker_failure(result)
    if failure is not None:
        _settle_worker_failure(queue, lease, result, failure, counts, bounds)
        return
    _settle_successful_processor(queue, lease, result, counts, bounds)


def _settle_successful_processor(queue, lease, result, counts, bounds):
    try:
        queue.publish_result(
            lease, operation_id=lease.id, result=_published_result_data(result.outcome),
        )
        task = queue.acknowledge(lease)
    except CompileCompletionError as error:
        failure = QueueFailure(error.code, blocked_capability='compile_completion')
        _fail_lease(queue, lease, failure, counts, **bounds)
        return
    _count_terminal(counts, task)


def _purge_operation_rows(
    database: sqlite3.Connection, task_id: str, operation_id: str
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """The purge operation and its parent task, or a lost refusal."""
    operation = database.execute(
        "SELECT * FROM corrupt_purge_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    parent = database.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if operation is None or parent is None:
        raise QueueOperationError("corrupt_purge_lost")
    return operation, parent


def _heartbeat_lost(heartbeat: _LeaseHeartbeat, result: _ProcessorOutcome) -> bool:
    """Whether the lease heartbeat died without the cleanup already failing."""
    return heartbeat.error is not None and not result.cleanup_failed


def _count_dead_task(queue: MemoryQueue, lease: QueueLease, counts: dict[str, int]) -> None:
    """A task the queue has already buried is counted dead as well as failed."""
    if queue.get(lease.id).state == "dead":
        counts["dead"] += 1


def _settle_worker_outcome(
    queue: MemoryQueue,
    lease: QueueLease,
    result: _ProcessorOutcome,
    counts: dict[str, int],
    *,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
) -> None:
    """Settle the attempt; a lost fence counts as a failure, never a crash."""
    try:
        _settle_processor_outcome(
            queue,
            lease,
            result,
            counts,
            max_attempts=max_attempts,
            retry_base_seconds=retry_base_seconds,
            retry_cap_seconds=retry_cap_seconds,
        )
    except (LeaseFenceError, ResultConflictError):
        counts["failed"] += 1
        _count_dead_task(queue, lease, counts)


def _work_one(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    *,
    owner: str,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    task_id: str | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, int]:
    lease = _claim_when_reachable(
        queue,
        owner=owner,
        lease_seconds=lease_seconds,
        max_attempts=max_attempts,
        deadline=deadline,
        monotonic=monotonic,
        task_id=task_id,
        cancelled=cancelled,
    )
    return _work_claimed(queue, processor, processor_runner, lease,
        deadline=deadline, monotonic=monotonic, lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds, max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds, retry_cap_seconds=retry_cap_seconds,
        cancelled=cancelled)


def _work_claimed(queue, processor, processor_runner, lease, *, deadline, monotonic,
                  lease_seconds, heartbeat_seconds, max_attempts, retry_base_seconds,
                  retry_cap_seconds, cancelled=None):
    """Supervise and settle a lease issued by the same canonical claim path."""
    counts = {"ok": 0, "failed": 0, "dead": 0, "skipped": 0, "halted": 0}
    if lease is None:
        return counts
    if _adopted_counts(queue, lease, counts):
        return counts
    _run_and_settle(
        queue,
        processor,
        processor_runner,
        lease,
        counts,
        deadline=deadline,
        monotonic=monotonic,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
        cancelled=cancelled,
    )
    return counts


def _bind_lease_processor(processor, queue, lease, deadline):
    if isinstance(processor, LeaseProcessor):
        return partial(processor.process, queue, lease, deadline)
    return processor


def _run_and_settle(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult] | LeaseProcessor,
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ],
    lease: QueueLease,
    counts: dict[str, int],
    *,
    deadline: float,
    monotonic: Callable[[], float],
    lease_seconds: int,
    heartbeat_seconds: int,
    max_attempts: int,
    retry_base_seconds: int,
    retry_cap_seconds: int,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Run one claimed task under a heartbeat and settle whatever came back."""
    processor = _bind_lease_processor(processor, queue, lease, deadline)
    heartbeat = _LeaseHeartbeat(
        queue,
        lease,
        heartbeat_seconds=heartbeat_seconds,
        lease_seconds=lease_seconds,
    )
    result = _processor_result(
        processor, processor_runner, lease, heartbeat, deadline, monotonic, cancelled=cancelled
    )
    if _heartbeat_lost(heartbeat, result):
        counts["failed"] += 1
        return
    _settle_worker_outcome(
        queue,
        lease,
        result,
        counts,
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )


# A worker claims no task with less than this left: one provider call (90 s by default)
# plus the child's start and settle. A task claimed later is killed by the worker's own
# deadline and loses an attempt. See
# `docs/research/2026-09-14-no-task-is-claimed-to-be-killed.md`.
WORKER_MIN_CLAIM_SECONDS = 120


def _worker_exhausted(
    *,
    cancelled: Callable[[], bool] | None,
    now: float,
    deadline: float,
    idle_started: float | None,
    idle_seconds: int,
) -> bool:
    """The worker has run out of time, permission, or patience."""
    if cancelled is not None and cancelled():
        return True
    if now >= deadline:
        return True
    return idle_started is not None and now - idle_started >= idle_seconds


def _worker_idle_pause(
    progress: _WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Sleep out part of the idle window; False when the budget is gone."""
    idle_remaining = idle_seconds - (now - progress.idle_started)
    budget_remaining = deadline - monotonic()
    if budget_remaining <= 0:
        return False
    sleep(min(1.0, idle_remaining, budget_remaining))
    return True


def _worker_idle_step(
    progress: _WorkerProgress,
    *,
    now: float,
    idle_seconds: int,
    deadline: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> bool:
    """Wait out one idle turn; False when the worker should stop."""
    if idle_seconds == 0:
        return False
    if progress.idle_started is None:
        progress.idle_started = now
    return _worker_idle_pause(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def _worker_turn(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: _WorkerProgress,
    *,
    now: float,
    owner: str,
    deadline: float,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    policy: Mapping[str, int],
    cancelled: Callable[[], bool] | None = None,
) -> bool:
    """One turn of the worker loop; False when it should stop."""
    counts = _work_one(
        queue,
        processor,
        processor_runner,
        owner=owner,
        deadline=deadline,
        monotonic=monotonic,
        cancelled=cancelled,
        **policy,
    )
    outcome = progress.advance(counts)
    if outcome == "halted":
        return False
    if outcome == "worked":
        return True
    return _worker_idle_step(
        progress,
        now=now,
        idle_seconds=idle_seconds,
        deadline=deadline,
        monotonic=monotonic,
        sleep=sleep,
    )


def _drive_worker(
    queue: MemoryQueue,
    processor: Callable[[dict], bool | DeferredResult],
    processor_runner: Callable[..., bool | DeferredResult],
    progress: _WorkerProgress,
    *,
    owner: str,
    deadline: float,
    max_tasks: int,
    idle_seconds: int,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    cancelled: Callable[[], bool] | None,
    policy: Mapping[str, int],
    min_claim_seconds: float = 0,
) -> None:
    while progress.processed < max_tasks:
        now = monotonic()
        if _worker_exhausted(
            cancelled=cancelled,
            now=now,
            deadline=deadline - min_claim_seconds,
            idle_started=progress.idle_started,
            idle_seconds=idle_seconds,
        ):
            return
        if not _worker_turn(
            queue,
            processor,
            processor_runner,
            progress,
            now=now,
            owner=owner,
            deadline=deadline,
            idle_seconds=idle_seconds,
            monotonic=monotonic,
            sleep=sleep,
            policy=policy,
            cancelled=cancelled,
        ):
            return


def _require_policy_this_queue_takes(
    queue: MemoryQueue | _QueueV3CandidateReader, policy: Mapping[str, int]
) -> None:
    """Refuse a retry policy the queue will not take, before any task is claimed.

    The adopted queue settles its own attempts, so a worker told otherwise would
    claim a task and only then find it cannot record the failure.
    """
    if isinstance(queue, MemoryQueue):
        return
    _require_adopted_attempt_limit(policy["max_attempts"])
    _require_adopted_retry_policy(
        policy["max_attempts"],
        policy["retry_base_seconds"],
        policy["retry_cap_seconds"],
    )


def run_worker(
    processor: Callable[[dict], bool | DeferredResult] | LeaseProcessor,
    *,
    max_tasks: int = DEFAULTS.worker_max_tasks,
    max_seconds: int = DEFAULTS.worker_max_seconds,
    idle_seconds: int = DEFAULTS.worker_idle_seconds,
    lease_seconds: int = DEFAULTS.queue_lease_seconds,
    heartbeat_seconds: int = DEFAULTS.queue_heartbeat_seconds,
    max_attempts: int = DEFAULTS.queue_max_attempts,
    retry_base_seconds: int = DEFAULTS.retry_base_seconds,
    retry_cap_seconds: int = DEFAULTS.retry_cap_seconds,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    processor_runner: Callable[
        [Callable[[dict], bool | DeferredResult], dict[str, Any], float],
        bool | DeferredResult,
    ] = _run_processor_child,
    cancelled: Callable[[], bool] | None = None,
    min_claim_seconds: float = 0,
) -> WorkerSummary:
    """Run a short-lived worker bounded by tasks, wall time, and idle time.

    No task is claimed with less than `min_claim_seconds` of the wall time left.
    LeaseProcessor opts in to canonical ownership arguments bound after claim.
    """
    if max_tasks < 0 or max_seconds < 0 or idle_seconds < 0:
        raise ValueError("worker limits must be non-negative")
    policy = {
        "lease_seconds": lease_seconds,
        "heartbeat_seconds": heartbeat_seconds,
        "max_attempts": max_attempts,
        "retry_base_seconds": retry_base_seconds,
        "retry_cap_seconds": retry_cap_seconds,
    }
    _validate_worker_policy(
        lease_seconds,
        heartbeat_seconds,
        max_attempts,
        retry_base_seconds,
        retry_cap_seconds,
    )
    progress = _WorkerProgress({"ok": 0, "failed": 0, "dead": 0, "skipped": 0})
    queue = _queue(
        max_attempts=max_attempts,
        retry_base_seconds=retry_base_seconds,
        retry_cap_seconds=retry_cap_seconds,
    )
    _require_policy_this_queue_takes(queue, policy)
    _drive_worker(
        queue,
        processor,
        processor_runner,
        progress,
        owner=f"worker-{os.getpid()}-{uuid.uuid4().hex}",
        deadline=monotonic() + max_seconds,
        max_tasks=max_tasks,
        idle_seconds=idle_seconds,
        monotonic=monotonic,
        sleep=sleep,
        cancelled=cancelled,
        policy=policy,
        min_claim_seconds=min_claim_seconds,
    )
    return WorkerSummary(
        progress.processed,
        progress.totals["ok"],
        progress.totals["failed"],
        progress.totals["dead"],
        progress.totals["skipped"],
        queue.count_eligible(max_attempts=max_attempts),
    )


def _count_terminal(counts: dict[str, int], task: QueueTask) -> None:
    if task.state == "succeeded":
        counts["ok"] += 1
        return
    counts["failed"] += 1
    if task.state == "dead":
        counts["dead"] += 1


def _operator_status() -> dict[str, object]:
    tasks = _queue().list_tasks()
    states = {state: 0 for state in _STATES}
    capabilities: set[str] = set()
    codes: set[str] = set()
    for task in tasks:
        states[task.state] += 1
        if task.blocked_capability:
            capabilities.add(task.blocked_capability)
        if task.error_code:
            codes.add(task.error_code)
    return {
        "counts": {"total": len(tasks)},
        "states": states,
        "capabilities": sorted(capabilities),
        "codes": sorted(codes),
    }


def _manual_query(payload: Mapping[str, Any]) -> bool | DeferredResult:
    """Answer one deferred query with the configured backend."""
    from llm_client import call_llm

    prompt = payload.get("prompt", "")
    if not prompt:
        return False
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 4000),
    )
    if not result:
        return False
    return DeferredResult(result.encode("utf-8"))


def _round_trips_as_date(day: str) -> bool:
    """Whether the text is a calendar date that survives `%Y-%m-%d` both ways."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
    except ValueError:
        return False


def _valid_day(raw_day: object, now: datetime) -> str | None:
    """The day this flush belongs to, when it is a real calendar date."""
    day = now.strftime("%Y-%m-%d") if raw_day is None else raw_day
    if not isinstance(day, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None:
        return None
    return day if _round_trips_as_date(day) else None


def _daily_log_path(day: str) -> Path | None:
    """The daily log for this day, when it stays inside the daily directory."""
    root = _vault_root()
    daily_dir = (root / "knowledge" / "daily").resolve()
    daily_path = (daily_dir / f"{day}.md").resolve()
    try:
        daily_path.relative_to(daily_dir)
    except ValueError:
        return None
    return daily_path


def _flush_operation_id(payload: Mapping[str, Any]) -> str:
    """The same id for a flush and every redrive of it: the payload, not the task.

    See `docs/research/2026-09-14-a-redriven-flush-appends-nothing-new.md`.
    """
    return f"flush:{sha256_bytes(canonical_json_bytes(dict(payload)))}"


def _append_flush_block(
    daily_path: Path,
    payload: Mapping[str, Any],
    operation_id: str,
    result: str,
    now: datetime,
) -> None:
    """Append what the classifier judged worth keeping, once per flush."""
    from daily_log_append import locked_append_once
    from flush_memory import _classify_response

    tier, body = _classify_response(result)
    if tier == "ok" or not body:
        return
    session_id = payload.get("session_id", "deferred")
    event = payload.get("event", "session-end")
    block = (
        f"\n## [{now.strftime('%H:%M:%S')}] deferred-{event} | {session_id}\n"
        f"- Tier: `{tier}`\n\n{redact_secrets(body)}\n"
    )
    locked_append_once(daily_path, block, operation_id)


def _flush_target_path(payload: Mapping[str, Any], now: datetime) -> Path | None:
    """The daily log this flush may append to, when the day names one."""
    day = _valid_day(payload.get("day"), now)
    if day is None:
        return None
    return _daily_log_path(day)


def _flush_inputs(payload: Mapping[str, Any], now: datetime):
    """The prompt and the daily log this flush writes, or None when either is absent."""
    prompt = payload.get("prompt", "")
    if not prompt:
        return None
    daily_path = _flush_target_path(payload, now)
    if daily_path is None:
        return None
    return prompt, daily_path


def _manual_flush(task: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Summarize one session into its daily log."""
    from iso_time import local_now
    from llm_client import call_llm

    now = local_now()
    inputs = _flush_inputs(payload, now)
    if inputs is None:
        return False
    prompt, daily_path = inputs
    result = call_llm(
        prompt,
        payload.get("system_prompt", ""),
        max_tokens=int(payload.get("max_tokens") or 1500),
    )
    if not result:
        return False
    _append_flush_block(daily_path, payload, _flush_operation_id(payload), result, now)
    return True


def _manual_compile() -> bool:
    """Run one compile pass in a child process, for as long as one compile may run.

    The bound is the nightly's (`scheduled_nightly.compile_wait_seconds`, measured and
    operator-set); without one a hung compile held the queue worker for good (audit
    2026-09-27 C-9).
    """
    from scheduled_nightly import compile_wait_seconds

    root = _vault_root()
    command = [sys.executable, str(root / "scripts" / "compile_memory.py"), "--trigger", "auto"]
    try:
        completed = subprocess.run(
            command, cwd=root, check=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=compile_wait_seconds(),
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def _manual_query_task(
    _task: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool | DeferredResult:
    return _manual_query(payload)


def _manual_compile_task(
    _task: Mapping[str, Any], _payload: Mapping[str, Any]
) -> bool | DeferredResult:
    return _manual_compile()


_MANUAL_TASK_HANDLERS = {
    "query": _manual_query_task,
    "flush": _manual_flush,
    "compile": _manual_compile_task,
}


def _manual_processor(task: dict[str, Any]) -> bool | DeferredResult:
    handler = _MANUAL_TASK_HANDLERS.get(task.get("type"))
    if handler is None:
        return False
    return handler(task, task.get("payload", {}))


def _uses_compile_journal(queue):
    contract = getattr(queue, 'expected_contract', None)
    return getattr(contract, 'user_version', None) == compile_progress.QUEUE_SCHEMA_VERSION


def _manual_lease_processor(queue, lease, deadline, task, *, lock_token=None, owner=None):
    if lease.kind != 'compile' or not _uses_compile_journal(queue):
        return _manual_processor(task)
    if task != _compat_task(lease):
        raise LeaseFenceError('compatibility payload differs from compile lease')
    from compile_memory import run_leased_compile

    return run_leased_compile(queue, lease, deadline=deadline, lock_token=lock_token, owner=owner) == 0


# Order matters: the first matching entry wins, so the narrower exception
# classes come before the broader ones they inherit from.
_CLI_ERROR_CODES: tuple[tuple[type[BaseException] | tuple[type[BaseException], ...], str], ...] = (
    (compile_progress.ProgressCorrupt, "compile_progress_corrupt"),
    (compile_progress.ProgressConflict, "compile_progress_conflict"),
    (KeyError, "task_not_found"),
    (PermissionError, "permission_denied"),
    (sqlite3.Error, "sqlite_error"),
    ((UnicodeError, json.JSONDecodeError), "decode_error"),
    (
        (
            subprocess.SubprocessError,
            multiprocessing.ProcessError,
            BrokenPipeError,
            EOFError,
            ChildProcessError,
        ),
        "process_error",
    ),
    (TimeoutError, "worker_timeout"),
    (OSError, "os_error"),
    ((TypeError, ValueError), "invalid_input"),
)


def _queue_operation_code(error: QueueOperationError) -> str:
    """The operation's own code, when it is one we are willing to print."""
    code = error.code
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        return code
    return "internal_error"


def _cli_error_code(error: BaseException) -> str:
    if isinstance(error, QueueOperationError):
        return _queue_operation_code(error)
    for kinds, code in _CLI_ERROR_CODES:
        if isinstance(error, kinds):
            return code
    return "internal_error"


def _cli_error_detail(error: BaseException) -> str:
    """The boundary a queue failure names, redacted and bounded, or nothing."""
    if not isinstance(error, QueueOperationError):
        return ""
    return redact_secrets(error.detail)[:_MAX_CLI_DETAIL_CHARS]


def _cli_error_payload(error: BaseException) -> dict[str, object]:
    detail = _cli_error_detail(error)
    code = _cli_error_code(error)
    return {"codes": [code], "detail": detail} if detail else {"codes": [code]}


def _emit_cli_error(error: BaseException) -> int:
    payload = canonical_json_bytes(_cli_error_payload(error))
    print(payload.decode("utf-8"))
    return 2


def _emit_invalid_arguments() -> int:
    payload = canonical_json_bytes({"ok": False, "code": "invalid_arguments"})
    print(payload.decode("utf-8"))
    return 2


def _build_cli_parser() -> _RedactedArgumentParser:
    parser = _RedactedArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "list",
            "status",
            "compile-progress",
            "compile-recover",
            "work",
            "cancel",
            "redrive",
            "unblock",
            "purge",
            "redrive-dead-captures",
            "restore",
            "quarantine-corrupt",
            "purge-corrupt",
        ],
    )
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--max-tasks", type=int, default=DEFAULTS.worker_max_tasks)
    parser.add_argument("--max-seconds", type=int, default=DEFAULTS.worker_max_seconds)
    parser.add_argument("--min-claim-seconds", type=int, default=WORKER_MIN_CLAIM_SECONDS)
    parser.add_argument("--idle-seconds", type=int, default=DEFAULTS.worker_idle_seconds)
    parser.add_argument("--lease-seconds", type=int, default=DEFAULTS.queue_lease_seconds)
    parser.add_argument(
        "--heartbeat-seconds", type=int, default=DEFAULTS.queue_heartbeat_seconds
    )
    parser.add_argument("--max-attempts", type=int, default=DEFAULTS.queue_max_attempts)
    parser.add_argument(
        "--retry-base-seconds", type=int, default=DEFAULTS.retry_base_seconds
    )
    parser.add_argument(
        "--retry-cap-seconds", type=int, default=DEFAULTS.retry_cap_seconds
    )
    parser.add_argument("--terminal-before")
    parser.add_argument("--changed-after")
    parser.add_argument("--export", type=Path)
    parser.add_argument(
        "--include-dead",
        action="store_true",
        help="Also purge attempts-exhausted tasks; they are retained by default.",
    )
    parser.add_argument("--reason")
    parser.add_argument("--work-id")
    parser.add_argument("--dispatch-digest")
    parser.add_argument("--accept-unknown-cost", action="store_true")
    return parser


def _cli_list(_args, _parser) -> int:
    records = [{"id": task.id, "state": task.state} for task in _queue().list_tasks()]
    print(json.dumps(records, sort_keys=True))
    return 0


def _cli_status(_args, _parser) -> int:
    print(json.dumps(_operator_status(), sort_keys=True))
    return 0


def _cli_compile_progress(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue = _compile_queue_for_cli()
    print(json.dumps(queue.inspect_compile_progress(task_id), sort_keys=True))
    return 0


def _compile_queue_for_cli():
    queue = active_memory_queue(_vault_root(), _state_root())
    if not _uses_compile_journal(queue):
        raise QueueOperationError('compile_progress_upgrade_required')
    return queue


def _cli_compile_recover(args, _parser) -> int:
    from operational_ownership import OperationalOwnershipError

    task_id = _require_cli_task_id(args)
    _require_task_identifier(task_id)
    compile_progress._require_digest(args.work_id)
    compile_progress._require_digest(args.dispatch_digest)
    authorization = _cli_compile_consent(args)
    _validate_worker_policy(args.lease_seconds, args.heartbeat_seconds, args.max_attempts,
                            args.retry_base_seconds, args.retry_cap_seconds)
    queue = _compile_queue_for_cli()
    dispatch = queue.get_compile_dispatch(args.work_id)
    _require_dispatch_identity(dispatch, args.dispatch_digest)
    try:
        result = _operator_compile_recovery(queue, task_id, dispatch, authorization, args)
    except OperationalOwnershipError as error:
        reason = _queue_operation_code(QueueOperationError(error.code))
        raise QueueOperationError('compile_recovery_owner_unavailable', reason) from error
    print(json.dumps(result, sort_keys=True))
    return 0


def _cli_compile_consent(args):
    import getpass

    if not args.accept_unknown_cost:
        return None
    if not isinstance(args.reason, str) or not args.reason.strip():
        raise QueueOperationError('compile_repeat_reason_required')
    return {'dispatch_digest': args.dispatch_digest, 'actor': getpass.getuser(), 'reason': args.reason}


def _operator_compile_recovery(queue, task_id, dispatch, authorization, policy):
    with _compile_recovery_owner(queue, task_id) as (owner, lost):
        _require_compile_recovery_owner(queue, owner, lost)
        queue.recover_expired_compile_recovery(task_id, dispatch)
        observed = queue.inspect_compile_progress(task_id)
        work = _inspected_compile_work(observed, dispatch.work_id)
        if not _needs_compile_recovery(observed['state'], work, dispatch):
            return {'outcome': 'not_blocked', 'task_state': observed['state'], 'work': work}
        return _owned_compile_recovery(queue, task_id, dispatch, authorization, policy, owner, lost)


def _needs_compile_recovery(state, work, dispatch):
    return state == 'blocked' or (state == 'ready' and work['sequence'] == dispatch.sequence)


def _inspected_compile_work(observed, work_id):
    for work in observed['works']:
        if work['work_id'] == work_id:
            return work
    raise QueueOperationError('compile_recovery_work_not_in_task')


@contextmanager
def _compile_recovery_owner(queue, task_id):
    from types import SimpleNamespace

    from operational_ownership import heartbeat_owner

    lost = threading.Event()
    renewer = SimpleNamespace(heartbeat=queue.heartbeat_queue_owner)
    with queue.queue_owner(role='queue-operator', scope=f'compile-recovery:{task_id}') as owner:
        with heartbeat_owner(owner, registry=renewer, lost=lost):
            yield owner, lost


def _require_compile_recovery_owner(queue, owner, lost):
    if lost.is_set():
        raise QueueOperationError('compile_recovery_owner_lost')
    registry = queue.ownership_registry()
    with closing(registry._connect()) as database:
        registry.require(database, owner)


def _owned_compile_recovery(queue, task_id, dispatch, authorization, policy, owner, lost):
    _require_compile_recovery_owner(queue, owner, lost)
    lease = queue.claim_uncertain_compile(
        owner.actor_id, task_id=task_id, work_id=dispatch.work_id,
        dispatch_digest=compile_progress._event_digest(dispatch),
        lease_seconds=policy.lease_seconds, max_attempts=policy.max_attempts)
    try:
        resolution, state = _heartbeat_compile_resolution(
            queue, lease, dispatch, authorization, policy, owner, lost)
    except BaseException:
        _abort_owned_compile_recovery(queue, lease, owner, lost)
        raise
    outcome, event = resolution
    return {'outcome': outcome, 'task_state': state, 'work': compile_progress.operator_summary(event)}


def _heartbeat_compile_resolution(queue, lease, dispatch, authorization, policy, owner, lost):
    heartbeat = _LeaseHeartbeat(queue, lease, heartbeat_seconds=policy.heartbeat_seconds,
                                 lease_seconds=policy.lease_seconds)
    heartbeat.start()
    try:
        result = _finish_compile_recovery(queue, lease, dispatch, authorization, owner, lost)
    finally:
        heartbeat.stop()
    _require_settled_recovery_heartbeat(heartbeat)
    return result


def _finish_compile_recovery(queue, lease, dispatch, authorization, owner, lost):
    from llm_wiki.compile_call_runtime import resolve_model_call

    resolution = resolve_model_call(queue, lease, dispatch, authorization=authorization)
    state = queue.settle_compile_resolution(
        lease, dispatch, resolution,
        check_owner=partial(_require_compile_recovery_owner, queue, owner, lost))
    return resolution, state


def _require_settled_recovery_heartbeat(heartbeat):
    # Called only after fenced settlement committed. A concurrent renewal may
    # then observe that intentionally released lease; no work continues under it.
    if heartbeat.error is None or isinstance(heartbeat.error, LeaseFenceError):
        return
    raise heartbeat.error


def _abort_owned_compile_recovery(queue, lease, owner, lost):
    task = queue.get(lease.id)
    if (task.state, task.lease_token) != ('leased', lease.token):
        return
    _require_compile_recovery_owner(queue, owner, lost)
    queue.abort_compile_recovery(lease)


def _cli_work(args, parser) -> int:
    try:
        _validate_worker_policy(
            args.lease_seconds,
            args.heartbeat_seconds,
            args.max_attempts,
            args.retry_base_seconds,
            args.retry_cap_seconds,
        )
    except ValueError as exc:
        parser.error(str(exc))
    summary = run_worker(
        LeaseProcessor(_manual_lease_processor),
        max_tasks=args.max_tasks,
        max_seconds=args.max_seconds,
        idle_seconds=args.idle_seconds,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        max_attempts=args.max_attempts,
        retry_base_seconds=args.retry_base_seconds,
        retry_cap_seconds=args.retry_cap_seconds,
        min_claim_seconds=args.min_claim_seconds,
    )
    counts = {
        "dead": summary.dead,
        "failed": summary.failed,
        "processed": summary.processed,
        "remaining_eligible": summary.remaining_eligible,
        "skipped": summary.skipped,
        "succeeded": summary.succeeded,
    }
    print(json.dumps({"counts": counts}, sort_keys=True))
    if summary.failed or summary.dead or summary.remaining_eligible:
        return 1
    return 0


def _require_cli_task_id(args) -> str:
    if not args.task_id:
        raise QueueOperationError("task_id_required")
    return str(args.task_id)


def _cli_quarantine_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    if not isinstance(args.reason, str):
        raise ValueError("quarantine reason is invalid")
    if not 1 <= len(args.reason.encode("utf-8")) <= 4096:
        raise ValueError("quarantine reason is invalid")
    queue = _v3_queue_for_cli()
    with _repair_owner_for_cli(queue) as owner:
        progress = queue.quarantine_corrupt(task_id, reason=args.reason, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_purge_corrupt(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    queue = _v3_queue_for_cli()
    with _repair_owner_for_cli(queue) as owner:
        progress = queue.purge_quarantined(task_id, owner=owner)
    print(
        json.dumps(
            {
                "code": progress.code,
                "complete": progress.complete,
                "links_deleted": progress.links_deleted,
                "operation_id": progress.operation_id,
                "page_count": progress.pages_written,
                "state": progress.state,
                "task_id": progress.task_id,
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_cancel(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    changed = cancel(task_id)
    state = "unchanged"
    if changed:
        state = _queue().get(task_id).state
    print(json.dumps({"id": task_id, "state": state}, sort_keys=True))
    if changed:
        return 0
    return 1


def _cli_unblock(args, _parser) -> int:
    task_id = _require_cli_task_id(args)
    _queue().unblock(task_id)
    print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
    return 0


def _cli_redrive(args, _parser) -> int:
    task_id = redrive(_require_cli_task_id(args))
    print(json.dumps({"id": task_id, "state": "ready"}, sort_keys=True))
    return 0


def _cli_terminal_before(args) -> datetime:
    if args.terminal_before is None:
        raise QueueOperationError("terminal_before_required")
    try:
        return datetime.fromisoformat(args.terminal_before)
    except ValueError:
        raise QueueOperationError("terminal_before_invalid") from None


def _cli_purge(args, _parser) -> int:
    terminal_before = _cli_terminal_before(args)
    if args.export is None:
        raise QueueOperationError("export_required")
    receipt = purge(
        terminal_before=terminal_before,
        export_path=args.export,
        include_dead=args.include_dead,
    )
    print(
        json.dumps(
            {
                "counts": {"purged": receipt.purged, "retained": len(receipt.retained)},
                "ids": list(receipt.task_ids),
                "retained": list(receipt.retained),
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_redrive_dead_captures(args, _parser) -> int:
    if args.changed_after is None:
        raise QueueOperationError("changed_after_required")
    try:
        changed_after = datetime.fromisoformat(args.changed_after)
    except ValueError:
        raise QueueOperationError("changed_after_invalid") from None
    outcome = _v3_queue_for_cli().redrive_dead_captures(changed_after=changed_after)
    print(
        json.dumps(
            {
                "redriven": [{"dead": dead, "ready": ready} for dead, ready in outcome.redriven],
                "refused": [{"dead": dead, "code": code} for dead, code in outcome.refused],
            },
            sort_keys=True,
        )
    )
    return 0


def _cli_restore(args, _parser) -> int:
    if args.export is None:
        raise QueueOperationError("export_required")
    restored = restore(export_path=args.export)
    print(
        json.dumps(
            {
                "counts": {"restored": restored.restored},
                "ids": [
                    {"exported": exported, "restored": current}
                    for exported, current in restored.task_ids
                ],
            },
            sort_keys=True,
        )
    )
    return 0


_CLI_COMMANDS = {
    "list": _cli_list,
    "status": _cli_status,
    "compile-progress": _cli_compile_progress,
    "compile-recover": _cli_compile_recover,
    "work": _cli_work,
    "quarantine-corrupt": _cli_quarantine_corrupt,
    "purge-corrupt": _cli_purge_corrupt,
    "cancel": _cli_cancel,
    "redrive": _cli_redrive,
    "unblock": _cli_unblock,
    "purge": _cli_purge,
    "redrive-dead-captures": _cli_redrive_dead_captures,
    "restore": _cli_restore,
}


def _cli() -> int:
    parser = _build_cli_parser()
    try:
        args = parser.parse_args()
        handler = _CLI_COMMANDS.get(args.command)
        if handler is None:
            return 2
        return handler(args, parser)
    except _InvalidArguments:
        return _emit_invalid_arguments()
    except Exception as exc:  # noqa: BLE001 - CLI is a redacted trust boundary
        return _emit_cli_error(exc)


if __name__ == "__main__":
    raise SystemExit(_cli())
