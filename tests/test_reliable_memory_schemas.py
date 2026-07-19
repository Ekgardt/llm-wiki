from __future__ import annotations

import json
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, validate_schema, validate_schema_object

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "scripts" / "schemas"

SCHEMAS = {
    "markdown-transaction-v1.json": (
        "https://llm-wiki.local/schemas/markdown-transaction-v1.json",
        "markdown-transaction/v1",
    ),
    "project-checkpoint-v1.json": (
        "https://llm-wiki.local/schemas/project-checkpoint-v1.json",
        "project-checkpoint/v1",
    ),
    "queue-task-v2.json": (
        "https://llm-wiki.local/schemas/queue-task-v2.json",
        "queue-task/v2",
    ),
    "compile-plan-v2.json": (
        "https://llm-wiki.local/schemas/compile-plan-v2.json",
        "compile-plan/v2",
    ),
    "compile-receipt-v2.json": (
        "https://llm-wiki.local/schemas/compile-receipt-v2.json",
        "compile-receipt/v2",
    ),
    "archive-manifest-v1.json": (
        "https://llm-wiki.local/schemas/archive-manifest-v1.json",
        "archive-manifest/v1",
    ),
}


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def test_committed_schema_names_ids_and_versions_are_fixed():
    assert {path.name for path in SCHEMA_DIR.glob("*.json")} >= set(SCHEMAS)
    for name, (schema_id, version) in SCHEMAS.items():
        schema = _schema(name)
        assert schema["$id"] == schema_id
        assert schema["properties"]["schema_version"]["const"] == version
        assert schema["additionalProperties"] is False


def test_transaction_schema_uses_approved_operations_and_absent_sentinel():
    schema = _schema("markdown-transaction-v1.json")
    operation = schema["properties"]["operations"]["items"]
    assert operation["properties"]["kind"]["enum"] == ["create", "replace", "delete"]
    states = operation["properties"]["before"]["oneOf"]
    assert {entry.get("const") for entry in states if "const" in entry} == {"absent"}


def test_queue_schema_has_exactly_six_approved_states():
    states = _schema("queue-task-v2.json")["properties"]["state"]["enum"]
    assert states == ["ready", "leased", "blocked", "succeeded", "dead", "cancelled"]


def test_queue_schema_accepts_closed_redacted_payload_envelope():
    task = {
        "schema_version": "queue-task/v2",
        "task_id": "task-1",
        "kind": "compile",
        "handler_version": 1,
        "payload": {
            "version": 1,
            "kind": "compile",
            "data_hash": "a" * 64,
            "redacted_data": {
                "fields": [{"name": "source", "value": "[redacted]", "redacted": True}]
            },
        },
        "input_hash": "b" * 64,
        "state": "ready",
        "priority": 0,
        "attempts": 0,
    }
    validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")
    task["handler_version"] = 0
    with pytest.raises(SchemaValidationError):
        validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")
    task["handler_version"] = 1
    task["payload"]["redacted_data"]["secret"] = "open"
    with pytest.raises(SchemaValidationError, match="unknown"):
        validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")


def test_queue_schema_accepts_full_operational_metadata_record():
    task = {
        "schema_version": "queue-task/v2",
        "task_id": "task-1",
        "kind": "compile",
        "handler_version": 2,
        "payload": {
            "version": 1,
            "kind": "compile",
            "data_hash": "a" * 64,
            "redacted_data": {"fields": []},
        },
        "input_hash": "b" * 64,
        "dedupe_key": "compile:daily:2026-07-13",
        "state": "dead",
        "priority": 25,
        "created_at": "2026-07-13T00:00:00Z",
        "updated_at": "2026-07-13T00:02:00Z",
        "available_at": "2026-07-13T00:01:00Z",
        "attempts": 1,
        "lease_token": None,
        "lease_expires_at": None,
        "lease_heartbeat_at": None,
        "error_code": "provider_unavailable",
        "blocked_capability": "llm.compile",
        "result_reference": {
            "result_id": "result-1",
            "manifest_path": "run/results/result-1.json",
            "sha256": "c" * 64,
        },
        "attempt_history": [
            {
                "attempt": 1,
                "started_at": "2026-07-13T00:00:00Z",
                "finished_at": "2026-07-13T00:00:05Z",
                "outcome": "failed",
                "error_code": "provider_unavailable",
            }
        ],
    }
    validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("available_at", 123),
        ("lease_token", 123),
        ("error_code", "Not Stable"),
        ("attempt_history", [{"attempt": 1, "started_at": "now", "outcome": "unknown"}]),
    ],
)
def test_queue_schema_rejects_invalid_operational_metadata(field, value):
    task = {
        "schema_version": "queue-task/v2",
        "task_id": "task-1",
        "kind": "compile",
        "handler_version": 1,
        "payload": {
            "version": 1,
            "kind": "compile",
            "data_hash": "a" * 64,
            "redacted_data": {"fields": []},
        },
        "input_hash": "b" * 64,
        "state": "ready",
        "priority": 0,
        "attempts": 0,
        field: value,
    }
    with pytest.raises(SchemaValidationError):
        validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")


def test_queue_attempt_history_rejects_unknown_fields():
    history = {
        "attempt": 1,
        "started_at": "2026-07-13T00:00:00Z",
        "finished_at": None,
        "outcome": "blocked",
        "error_code": None,
        "mutable_note": "not allowed",
    }
    task = {
        "schema_version": "queue-task/v2",
        "task_id": "task-1",
        "kind": "compile",
        "handler_version": 1,
        "payload": {
            "version": 1,
            "kind": "compile",
            "data_hash": "a" * 64,
            "redacted_data": {"fields": []},
        },
        "input_hash": "b" * 64,
        "state": "blocked",
        "priority": 0,
        "attempts": 1,
        "attempt_history": [history],
    }
    with pytest.raises(SchemaValidationError, match="unknown"):
        validate_schema(task, SCHEMA_DIR / "queue-task-v2.json")


def test_project_checkpoint_schema_covers_provenance_and_stable_delta_semantics():
    change = {"id": "stable-1", "action": "upsert", "value": "active"}
    checkpoint = {
        "schema_version": "project-checkpoint/v1",
        "occurrence_id": "occ-1",
        "idempotency_key": "session:event",
        "project": "reliable-memory",
        "sequence": 2,
        "provenance": {
            "agent": "opencode",
            "session": "session-1",
            "worktree": "D:/projects/llm-wiki",
            "branch": "feature",
            "source_event": "post-tool",
        },
        "trigger": "task-complete",
        "reason": "checkpoint",
        "delta": {
            "goal": change,
            "phase": change,
            "current_task": change,
            "next_actions": [change],
            "decisions": [change],
            "blockers": [{"id": "block-1", "action": "close", "value": "resolved"}],
            "changed_files": [change],
            "commands": [change],
            "verification": [change],
        },
        "evidence_event_ids": ["event-1"],
        "last_applied_sequence": 1,
    }
    validate_schema(checkpoint, SCHEMA_DIR / "project-checkpoint-v1.json")
    checkpoint["delta"]["goal"]["action"] = "append"
    with pytest.raises(SchemaValidationError):
        validate_schema(checkpoint, SCHEMA_DIR / "project-checkpoint-v1.json")


def test_archive_manifest_requires_receipt_queue_preflight_and_terminal_operations():
    manifest = {
        "schema_version": "archive-manifest/v1",
        "logical_daily_id": "2026-01-01",
        "original_path": "knowledge/daily/2026-01-01.md",
        "source_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "compile_receipt_ref": {
            "schema": "compile-receipt-ref/v1",
            "path": "knowledge/daily/2026-01-01.md",
            "source_digest": "c" * 64,
            "receipt_file_hash": "d" * 64,
        },
        "queue_preflight": {
            "checked_at": "2026-07-13T00:00:00Z",
            "passed": True,
            "blocking_task_ids": [],
        },
        "operations": [{"operation_id": "compile:1", "state": "succeeded"}],
        "evidence": [],
        "pins": [],
        "retention_days": 30,
    }
    validate_schema(manifest, SCHEMA_DIR / "archive-manifest-v1.json")
    manifest["operations"][0]["state"] = "ready"
    with pytest.raises(SchemaValidationError):
        validate_schema(manifest, SCHEMA_DIR / "archive-manifest-v1.json")


def test_archive_manifest_rejects_receipt_payload_masquerading_as_reference():
    manifest = {
        "schema_version": "archive-manifest/v1",
        "logical_daily_id": "2026-01-01",
        "original_path": "knowledge/daily/2026-01-01.md",
        "source_hash": "a" * 64,
        "payload_hash": "b" * 64,
        "compile_receipt": {
            "schema_version": "compile-receipt/v2",
            "receipt_id": "receipt-1",
            "path": "knowledge/daily/2026-01-01.md",
            "sha256": "c" * 64,
        },
        "queue_preflight": {
            "checked_at": "2026-07-13T00:00:00Z",
            "passed": True,
            "blocking_task_ids": [],
        },
        "operations": [],
        "evidence": [],
        "pins": [],
        "retention_days": 30,
    }
    with pytest.raises(SchemaValidationError):
        validate_schema(manifest, SCHEMA_DIR / "archive-manifest-v1.json")


def test_compile_plan_accepts_absent_content_for_delete():
    validate_schema(
        {
            "schema_version": "compile-plan/v2",
            "operations": [{"kind": "delete", "path": "knowledge/notes/old.md", "content": "absent"}],
        },
        SCHEMA_DIR / "compile-plan-v2.json",
    )


@pytest.mark.parametrize("name", SCHEMAS)
def test_schemas_reject_unknown_top_level_properties(name, tmp_path):
    schema = _schema(name)
    required = schema["required"]
    instance = {key: _minimal_value(schema["properties"][key]) for key in required}
    instance["unknown"] = True
    with pytest.raises(SchemaValidationError, match="unknown"):
        validate_schema(instance, SCHEMA_DIR / name)


def _minimal_value(rule: dict):
    if "const" in rule:
        return rule["const"]
    if "enum" in rule:
        return rule["enum"][0]
    rule_type = rule.get("type")
    if rule_type == "string":
        return rule.get("pattern", "x").strip("^$").replace(".*", "x") or "x"
    if rule_type == "integer":
        return rule.get("minimum", 0)
    if rule_type == "number":
        return rule.get("minimum", 0)
    if rule_type == "boolean":
        return False
    if rule_type == "array":
        return []
    if rule_type == "object":
        return {
            key: _minimal_value(rule["properties"][key]) for key in rule.get("required", [])
        }
    if "oneOf" in rule:
        return _minimal_value(rule["oneOf"][0])
    raise AssertionError(f"No minimal value for {rule}")


def test_validate_schema_supports_the_committed_closed_subset(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "type": "object",
                "required": ["name", "count", "tags", "choice"],
                "properties": {
                    "name": {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                    "tags": {"type": "array", "minItems": 1, "maxItems": 2, "items": {"type": "string"}},
                    "choice": {"oneOf": [{"const": "yes"}, {"const": "no"}]},
                },
                "additionalProperties": False,
            }
        ),
        encoding="utf-8",
    )
    validate_schema({"name": "okay", "count": 2, "tags": ["x"], "choice": "yes"}, schema_path)
    for bad in (
        {"name": "X", "count": 2, "tags": ["x"], "choice": "yes"},
        {"name": "okay", "count": 4, "tags": ["x"], "choice": "yes"},
        {"name": "okay", "count": 2, "tags": [], "choice": "yes"},
        {"name": "okay", "count": 2, "tags": ["x"], "choice": "maybe"},
    ):
        with pytest.raises(SchemaValidationError):
            validate_schema(bad, schema_path)


def test_validate_schema_does_not_treat_boolean_as_numeric_const(tmp_path):
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(json.dumps({"const": 1}), encoding="utf-8")
    with pytest.raises(SchemaValidationError):
        validate_schema(True, schema_path)


def test_validate_schema_supports_strict_closed_type_unions():
    nullable_string = {"type": ["string", "null"], "minLength": 2, "maxLength": 4}
    validate_schema_object(None, nullable_string)
    validate_schema_object("okay", nullable_string)
    with pytest.raises(SchemaValidationError):
        validate_schema_object("x", nullable_string)

    nullable_number = {"type": ["number", "null"], "minimum": 1, "maximum": 2}
    for value in (None, 1, 1.5, 2):
        validate_schema_object(value, nullable_number)
    for value in (True, float("inf"), float("nan")):
        with pytest.raises(SchemaValidationError):
            validate_schema_object(value, nullable_number)

    for invalid_type in (
        [],
        ["string", "string"],
        ["string", "unknown"],
        ["string", 1],
        "unknown",
        1,
    ):
        with pytest.raises(SchemaValidationError, match="schema type"):
            validate_schema_object(None, {"type": invalid_type})
