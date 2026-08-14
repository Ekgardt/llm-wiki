from __future__ import annotations

import copy
import hashlib
import json
import re
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
    "queue-task-v3.json": (
        "https://llm-wiki.local/schemas/queue-task-v3.json",
        "queue-task/v3",
    ),
    "compile-receipt-v3.json": (
        "https://llm-wiki.local/schemas/compile-receipt-v3.json",
        "compile-receipt/v3",
    ),
    "transaction-abort-v1.json": (
        "https://llm-wiki.local/schemas/transaction-abort-v1.json",
        "transaction-abort/v1",
    ),
    "capture-task-link-resolution-v1.json": (
        "https://llm-wiki.local/schemas/capture-task-link-resolution-v1.json",
        "capture-task-link-resolution/v1",
    ),
    "corrupt-task-manifest-v1.json": (
        "https://llm-wiki.local/schemas/corrupt-task-manifest-v1.json",
        "corrupt-task-manifest/v1",
    ),
    "corrupt-task-disposition-v1.json": (
        "https://llm-wiki.local/schemas/corrupt-task-disposition-v1.json",
        "corrupt-task-disposition/v1",
    ),
    "corrupt-package-supersession-v1.json": (
        "https://llm-wiki.local/schemas/corrupt-package-supersession-v1.json",
        "corrupt-package-supersession/v1",
    ),
    "corrupt-purge-v1.json": (
        "https://llm-wiki.local/schemas/corrupt-purge-v1.json",
        "corrupt-purge/v1",
    ),
    "operational-db-tombstone-v1.json": (
        "https://llm-wiki.local/schemas/operational-db-tombstone-v1.json",
        "operational-db-tombstone/v1",
    ),
    "reliability-v3-migration-v1.json": (
        "https://llm-wiki.local/schemas/reliability-v3-migration-v1.json",
        "reliability-v3-migration/v1",
    ),
    "reliability-v3-adoption-v1.json": (
        "https://llm-wiki.local/schemas/reliability-v3-adoption-v1.json",
        "reliability-v3-adoption/v1",
    ),
}

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
TIMESTAMP = "2026-08-05T12:00:00Z"

QUEUE_V3_FIXTURE = {
    "schema_version": "queue-task/v3",
    "task_id": "queue-task-fixture-0001",
    "kind": "compile",
    "handler_version": 1,
    "payload": {},
    "input_hash": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    "dedupe_key": None,
    "state": "ready",
    "priority": 0,
    "created_at": TIMESTAMP,
    "updated_at": TIMESTAMP,
    "available_at": TIMESTAMP,
    "attempts": 0,
    "last_attempt_at": None,
    "lease": None,
    "error_code": None,
    "blocked_capability": None,
    "result": None,
    "redrive_of": None,
    "lineage_generation": 0,
    "attempt_history": [],
    "source_links": [],
    "capture_binding": None,
}

COMPILE_RECEIPT_V3_FIXTURE = {
    "schema_version": "compile-receipt/v3",
    "source": {
        "logical_path": "knowledge/daily/2026-08-05.md",
        "sha256": EMPTY_SHA256,
        "byte_size": 0,
        "occurrence_bounds": None,
    },
    "source_identity": "4baadbd599fef9c22e1fed700ba9896e1c7569ad2e3632e907d7ba4b5a3a5d9b",
    "batch_manifest": [
        {
            "logical_path": "knowledge/daily/2026-08-05.md",
            "sha256": EMPTY_SHA256,
            "byte_size": 0,
            "occurrence_bounds": None,
        },
        {
            "logical_path": "knowledge/daily/2026-08-06.md",
            "sha256": EMPTY_SHA256,
            "byte_size": 0,
            "occurrence_bounds": None,
        },
    ],
    "batch_manifest_sha256": "31b1d6e0eae61f2343273c102e78fbc616db5d2b30373e5a5af51b03e7e70c10",
    "action_key": "e9d6c77149458cf6c916ed40b7ec92986dad7e745598f456ef714bf7896679d8",
    "operation_id": "compile:7ff26d5b5e1758ccb7afb040c321d93b046bb3885e4a43cf11f521b8c11f6176",
    "packing": {
        "algorithm": "compile-complete-items/v1",
        "tokenizer_identity": "utf8-byte-estimate/v1",
        "count_source": "estimated",
        "max_input_tokens": 32768,
        "reserved_output_tokens": 4000,
        "safety_margin_tokens": 1024,
        "measured_input_tokens": 12000,
    },
    "provider_budget": {
        "provider": "fake",
        "model": "fake-v1",
        "max_output_tokens": 4000,
    },
    "dispositions": [
        {
            "source_identity": "4baadbd599fef9c22e1fed700ba9896e1c7569ad2e3632e907d7ba4b5a3a5d9b",
            "disposition": "no_durable_content",
        },
        {
            "source_identity": "5097e77048c160eeefa7d73325016585c0e5faf7c3ac0d359688a41913615eb8",
            "disposition": "no_durable_content",
        },
    ],
    "operations": [],
    "evidence": [],
}

RECOVERY_FIXTURES = {
    "transaction-abort-v1.json": (
        {
            "schema_version": "transaction-abort/v1",
            "transaction_id": "transaction-fixture-0001",
            "intent_id": "1" * 64,
            "active_link_digest": "2" * 64,
            "intent_fence_token_sha256": "3" * 64,
            "intent_fence_epoch": 4,
            "abort_operation_id": "abort:fixture-0001",
            "before_manifest_sha256": "5" * 64,
            "restored_target_count": 3,
            "restored_tree_sha256": "6" * 64,
            "actor_identity": "posix-uid:1000",
            "aborted_at": TIMESTAMP,
        },
        [()],
    ),
    "capture-task-link-resolution-v1.json": (
        {
            "schema_version": "capture-task-link-resolution/v1",
            "task_id": "queue-task-fixture-0001",
            "supersedes_digest": None,
            "observed": {
                "link_digest": "1" * 64,
                "index_digest": "2" * 64,
                "intent_file_digest": "3" * 64,
            },
            "selected_intent": {
                "intent_id": "4" * 64,
                "intent_sha256": "5" * 64,
                "handler_version": 1,
            },
            "actor_identity": "posix-uid:1000",
            "reason": "Select the one verified capture intent.",
            "created_at": TIMESTAMP,
        },
        [(), ("observed",), ("selected_intent",)],
    ),
    "corrupt-task-manifest-v1.json": (
        {
            "schema_version": "corrupt-task-manifest/v1",
            "operation_id": "corrupt-export:fixture-0001",
            "task_id": "queue-task-fixture-0001",
            "package_key": "1" * 64,
            "package_path": "run/queue-results/corrupt-" + "1" * 64,
            "raw_sha256": "2" * 64,
            "history_sha256": "3" * 64,
            "metadata_sha256": "4" * 64,
            "lineage_generation": 7,
            "link_count": 2503,
            "page_count": 3,
            "rolling_root": "5" * 64,
        },
        [()],
    ),
    "corrupt-task-disposition-v1.json": (
        {
            "schema_version": "corrupt-task-disposition/v1",
            "operation_id": "corrupt-export:fixture-0001",
            "task_id": "queue-task-fixture-0001",
            "package_key": "1" * 64,
            "package_path": "run/queue-results/corrupt-" + "1" * 64,
            "manifest_path": "run/queue-results/corrupt-" + "1" * 64 + "/manifest.json",
            "manifest_sha256": "2" * 64,
            "active_link_digest": "3" * 64,
            "actor_identity": "posix-uid:1000",
            "reason": "Retain the corrupt task as verified evidence.",
            "disposed_at": TIMESTAMP,
        },
        [()],
    ),
    "corrupt-package-supersession-v1.json": (
        {
            "schema_version": "corrupt-package-supersession/v1",
            "package_key": "42fbe6ca5d6056b0ff192b9029e89df6bfa9fb758a3c9e2a142d1392bc35fdde",
            "package_path": "run/queue-results/corrupt-42fbe6ca5d6056b0ff192b9029e89df6bfa9fb758a3c9e2a142d1392bc35fdde",
            "initial_identity": {
                "platform": "posix",
                "volume": "dev:2049",
                "file_id": "ino:1048576",
                "size": 4096,
                "mtime_ns": 1785931200000000000,
            },
            "observed_file_count": 2503,
            "page_count": 3,
            "observed_root": "06dedfd36393cf68f9bb0d6588998569561f90105158c5b7c75e9e1361fdfd02",
            "actor_identity": "posix-uid:1000",
            "reason": "Retain and supersede the conflicting package fixture.",
            "disposed_at": TIMESTAMP,
        },
        [(), ("initial_identity",)],
    ),
    "corrupt-purge-v1.json": (
        {
            "schema_version": "corrupt-purge/v1",
            "operation_id": "corrupt-purge:fixture-0001",
            "task_id": "queue-task-fixture-0001",
            "package_key": "1" * 64,
            "package_path": "run/queue-results/corrupt-" + "1" * 64,
            "manifest_sha256": "2" * 64,
            "disposition_sha256": "3" * 64,
            "original_frozen_root": "4" * 64,
            "purge_page_count": 3,
            "final_rolling_root": "5" * 64,
            "final_generation": 10,
            "observed_incoming_link_count": 0,
        },
        [()],
    ),
    "operational-db-tombstone-v1.json": (
        {
            "schema_version": "operational-db-tombstone/v1",
            "database": "queue",
            "source_state": "upgrade",
            "legacy_path": "run/queue.sqlite3",
            "replacement_path": "run/queue-v3.sqlite3",
            "retired_path": "run/queue-v2-retired.sqlite3",
            "retired_sha256": "fdc558565db7af20b2575b742eec196aa849376d3187f9dbdc0154e62f6c6999",
            "operation_id": "reliability-v3:54b59979e2b8a1202629857747bb714d840aab8a77208da637fc6bb81b745751",
            "adoption_schema_sha256": "02df576bf6a53c1a38c464b0515130924fdb3ae974df6802b7cacb9fdc2e7cad",
        },
        [()],
    ),
    "reliability-v3-migration-v1.json": (
        {
            "schema_version": "reliability-v3-migration/v1",
            "operation_id": "reliability-v3:54b59979e2b8a1202629857747bb714d840aab8a77208da637fc6bb81b745751",
            "source_state": "upgrade",
            "databases": [
                {
                    "database": "queue",
                    "legacy_path": "run/queue.sqlite3",
                    "replacement_path": "run/queue-v3.sqlite3",
                    "retired_path": "run/queue-v2-retired.sqlite3",
                    "source_identity": {
                        "platform": "posix",
                        "volume": "dev:2049",
                        "file_id": "ino:1048576",
                        "size": 4096,
                        "mtime_ns": 1785931200000000000,
                    },
                    "source_sha256": "1" * 64,
                },
                {
                    "database": "coordinator",
                    "legacy_path": "run/markdown-transactions.sqlite3",
                    "replacement_path": "run/markdown-transactions-v3.sqlite3",
                    "retired_path": "run/markdown-transactions-v2-retired.sqlite3",
                    "source_identity": {
                        "platform": "posix",
                        "volume": "dev:2049",
                        "file_id": "ino:1048577",
                        "size": 8192,
                        "mtime_ns": 1785931200000000001,
                    },
                    "source_sha256": "2" * 64,
                },
            ],
            "schemas": {
                "queue_schema_sha256": "3" * 64,
                "coordinator_schema_sha256": "4" * 64,
                "adoption_schema_sha256": "5" * 64,
            },
            "installed_integration_sha256": "6" * 64,
        },
        [
            (),
            ("databases", 0),
            ("databases", 0, "source_identity"),
            ("schemas",),
        ],
    ),
    "reliability-v3-adoption-v1.json": (
        {
            "schema_version": "reliability-v3-adoption/v1",
            "operation_id": "reliability-v3:54b59979e2b8a1202629857747bb714d840aab8a77208da637fc6bb81b745751",
            "source_state": "upgrade",
            "migration": {
                "path": "run/reliability-v3-migration.json",
                "sha256": "1" * 64,
            },
            "databases": [
                {
                    "database": "queue",
                    "active": {
                        "path": "run/queue-v3.sqlite3",
                        "sha256": "2" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048576",
                            "size": 16384,
                            "mtime_ns": 1785931200000000002,
                        },
                    },
                    "tombstone": {
                        "path": "run/queue.sqlite3",
                        "sha256": "3" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048577",
                            "size": 512,
                            "mtime_ns": 1785931200000000003,
                        },
                    },
                    "retired": {
                        "path": "run/queue-v2-retired.sqlite3",
                        "sha256": "4" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048578",
                            "size": 4096,
                            "mtime_ns": 1785931200000000004,
                        },
                    },
                    "application_id": 1280790835,
                    "user_version": 3,
                    "pragmas": {
                        "journal_mode": "delete",
                        "synchronous": 2,
                        "foreign_keys": 1,
                        "trusted_schema": 0,
                    },
                },
                {
                    "database": "coordinator",
                    "active": {
                        "path": "run/markdown-transactions-v3.sqlite3",
                        "sha256": "5" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048579",
                            "size": 24576,
                            "mtime_ns": 1785931200000000005,
                        },
                    },
                    "tombstone": {
                        "path": "run/markdown-transactions.sqlite3",
                        "sha256": "6" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048580",
                            "size": 512,
                            "mtime_ns": 1785931200000000006,
                        },
                    },
                    "retired": {
                        "path": "run/markdown-transactions-v2-retired.sqlite3",
                        "sha256": "7" * 64,
                        "identity": {
                            "platform": "posix",
                            "volume": "dev:2049",
                            "file_id": "ino:2048581",
                            "size": 8192,
                            "mtime_ns": 1785931200000000007,
                        },
                    },
                    "application_id": 1280791603,
                    "user_version": 3,
                    "pragmas": {
                        "journal_mode": "delete",
                        "synchronous": 2,
                        "foreign_keys": 1,
                        "trusted_schema": 0,
                    },
                },
            ],
            "schemas": {
                "queue_schema_sha256": "8" * 64,
                "coordinator_schema_sha256": "9" * 64,
                "adoption_schema_sha256": "a" * 64,
            },
            "installed_integration_sha256": "b" * 64,
        },
        [
            (),
            ("migration",),
            ("databases", 0),
            ("databases", 0, "active"),
            ("databases", 0, "active", "identity"),
            ("databases", 0, "tombstone"),
            ("databases", 0, "retired"),
            ("databases", 0, "pragmas"),
            ("schemas",),
        ],
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


def test_queue_v3_schema_accepts_the_complete_contract_fixture():
    schema_path = SCHEMA_DIR / "queue-task-v3.json"
    validate_schema(QUEUE_V3_FIXTURE, schema_path)
    for field in QUEUE_V3_FIXTURE:
        missing = copy.deepcopy(QUEUE_V3_FIXTURE)
        del missing[field]
        with pytest.raises(SchemaValidationError):
            validate_schema(missing, schema_path)

    extra = copy.deepcopy(QUEUE_V3_FIXTURE)
    extra["unknown"] = True
    with pytest.raises(SchemaValidationError, match="unknown"):
        validate_schema(extra, schema_path)


def test_queue_v3_schema_has_exactly_nine_closed_states():
    expected = [
        "ready",
        "leased",
        "blocked",
        "succeeded",
        "dead",
        "cancelled",
        "quarantine_pending",
        "quarantined",
        "purge_pending",
    ]
    states = _schema("queue-task-v3.json")["properties"]["state"]["enum"]
    assert states == expected
    task = copy.deepcopy(QUEUE_V3_FIXTURE)
    task["state"] = "unknown"
    with pytest.raises(SchemaValidationError):
        validate_schema(task, SCHEMA_DIR / "queue-task-v3.json")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_compile_receipt_v3_contract(receipt: dict) -> None:
    validate_schema(receipt, SCHEMA_DIR / "compile-receipt-v3.json")
    manifest = receipt["batch_manifest"]
    if manifest != sorted(manifest, key=lambda item: item["logical_path"]):
        raise SchemaValidationError("batch manifest is not path-sorted")
    source_identities = [
        _canonical_sha256([source["logical_path"], source["sha256"]])
        for source in manifest
    ]
    disposition_identities = [item["source_identity"] for item in receipt["dispositions"]]
    if disposition_identities != sorted(source_identities):
        raise SchemaValidationError("dispositions do not match the batch manifest")


def test_compile_receipt_v3_requires_path_bound_source_and_all_dispositions():
    receipt = copy.deepcopy(COMPILE_RECEIPT_V3_FIXTURE)
    assert receipt["source_identity"] == _canonical_sha256(
        [receipt["source"]["logical_path"], receipt["source"]["sha256"]]
    )
    assert receipt["batch_manifest_sha256"] == _canonical_sha256(receipt["batch_manifest"])
    assert receipt["action_key"] == _canonical_sha256({"fixture": "compile-receipt-v3"})
    operation_preimage = {
        "action_key": receipt["action_key"],
        "batch_manifest_sha256": receipt["batch_manifest_sha256"],
        "dispositions": receipt["dispositions"],
    }
    assert receipt["operation_id"] == f"compile:{_canonical_sha256(operation_preimage)}"
    _validate_compile_receipt_v3_contract(receipt)

    mutations = []
    missing_path = copy.deepcopy(receipt)
    del missing_path["batch_manifest"][0]["logical_path"]
    mutations.append(missing_path)
    duplicate_source = copy.deepcopy(receipt)
    duplicate_source["dispositions"][1]["source_identity"] = duplicate_source[
        "dispositions"
    ][0]["source_identity"]
    mutations.append(duplicate_source)
    missing_disposition = copy.deepcopy(receipt)
    missing_disposition["dispositions"].pop()
    mutations.append(missing_disposition)
    extra_disposition = copy.deepcopy(receipt)
    extra_disposition["dispositions"].append(
        {"source_identity": "f" * 64, "disposition": "no_durable_content"}
    )
    mutations.append(extra_disposition)
    unknown_disposition = copy.deepcopy(receipt)
    unknown_disposition["dispositions"][0]["disposition"] = "skipped"
    mutations.append(unknown_disposition)
    for invalid in mutations:
        with pytest.raises(SchemaValidationError):
            _validate_compile_receipt_v3_contract(invalid)


def _tombstone(source_state: str) -> dict:
    tombstone = copy.deepcopy(RECOVERY_FIXTURES["operational-db-tombstone-v1.json"][0])
    tombstone["source_state"] = source_state
    if source_state == "fresh":
        del tombstone["retired_path"]
        del tombstone["retired_sha256"]
    return tombstone


def test_fresh_tombstone_forbids_retired_fields():
    schema_path = SCHEMA_DIR / "operational-db-tombstone-v1.json"
    fresh = _tombstone("fresh")
    validate_schema(fresh, schema_path)
    for field, value in (
        ("retired_path", "run/queue-v2-retired.sqlite3"),
        ("retired_sha256", "f" * 64),
    ):
        invalid = copy.deepcopy(fresh)
        invalid[field] = value
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, schema_path)


def test_upgrade_tombstone_requires_retired_path_and_hash():
    schema_path = SCHEMA_DIR / "operational-db-tombstone-v1.json"
    upgrade = _tombstone("upgrade")
    validate_schema(upgrade, schema_path)
    for field in ("retired_path", "retired_sha256"):
        invalid = copy.deepcopy(upgrade)
        del invalid[field]
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, schema_path)


def test_fresh_migration_and_adoption_have_no_retired_source_artifacts():
    migration = copy.deepcopy(RECOVERY_FIXTURES["reliability-v3-migration-v1.json"][0])
    migration["source_state"] = "fresh"
    for database in migration["databases"]:
        for field in ("retired_path", "source_identity", "source_sha256"):
            del database[field]
    validate_schema(migration, SCHEMA_DIR / "reliability-v3-migration-v1.json")

    adoption = copy.deepcopy(RECOVERY_FIXTURES["reliability-v3-adoption-v1.json"][0])
    adoption["source_state"] = "fresh"
    for database in adoption["databases"]:
        del database["retired"]
    validate_schema(adoption, SCHEMA_DIR / "reliability-v3-adoption-v1.json")

    for name, record in (
        ("reliability-v3-migration-v1.json", migration),
        ("reliability-v3-adoption-v1.json", adoption),
    ):
        invalid = copy.deepcopy(record)
        invalid["source_state"] = "upgrade"
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, SCHEMA_DIR / name)

    upgrade_migration = copy.deepcopy(
        RECOVERY_FIXTURES["reliability-v3-migration-v1.json"][0]
    )
    upgrade_migration["source_state"] = "fresh"
    with pytest.raises(SchemaValidationError):
        validate_schema(
            upgrade_migration,
            SCHEMA_DIR / "reliability-v3-migration-v1.json",
        )

    upgrade_adoption = copy.deepcopy(
        RECOVERY_FIXTURES["reliability-v3-adoption-v1.json"][0]
    )
    upgrade_adoption["source_state"] = "fresh"
    with pytest.raises(SchemaValidationError):
        validate_schema(
            upgrade_adoption,
            SCHEMA_DIR / "reliability-v3-adoption-v1.json",
        )


def _nested(value: object, path: tuple[object, ...]) -> dict:
    for part in path:
        value = value[part]
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize(
    ("name", "fixture", "object_paths"),
    [(name, *fixture) for name, fixture in RECOVERY_FIXTURES.items()],
)
def test_abort_and_corrupt_receipts_reject_unknown_fields(name, fixture, object_paths):
    schema_path = SCHEMA_DIR / name
    validate_schema(fixture, schema_path)
    for path in object_paths:
        invalid = copy.deepcopy(fixture)
        _nested(invalid, path)["unknown"] = True
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, schema_path)


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
            "path": "knowledge/daily/receipts/v3-" + "e" * 64 + ".md",
            "logical_path": "knowledge/daily/2026-01-01.md",
            "source_digest": "c" * 64,
            "source_identity": "e" * 64,
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
    instance = _minimal_value(schema, root=schema)
    instance["unknown"] = True
    with pytest.raises(SchemaValidationError):
        validate_schema(instance, SCHEMA_DIR / name)


def _minimal_value(rule: dict, *, root: dict | None = None):
    if root is None:
        root = rule
    if "$ref" in rule:
        target = root
        for raw_part in rule["$ref"][2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            target = target[part]
        return _minimal_value(target, root=root)
    if "const" in rule:
        return rule["const"]
    if "enum" in rule:
        return rule["enum"][0]
    if "oneOf" in rule:
        return _minimal_value(rule["oneOf"][0], root=root)
    rule_type = rule.get("type")
    if rule_type == "string":
        minimum = rule.get("minLength", 0)
        pattern = rule.get("pattern")
        candidates = (
            "",
            "x" * max(1, minimum),
            "0" * 64,
            "compile:" + "0" * 64,
            "2026-08-05T12:00:00Z",
        )
        for candidate in candidates:
            if len(candidate) >= minimum and (pattern is None or re.search(pattern, candidate)):
                return candidate
        raise AssertionError(f"No minimal string for {rule}")
    if rule_type == "null":
        return None
    if rule_type == "integer":
        return rule.get("minimum", 0)
    if rule_type == "number":
        return rule.get("minimum", 0)
    if rule_type == "boolean":
        return False
    if rule_type == "array":
        return [
            _minimal_value(rule["items"], root=root)
            for _ in range(rule.get("minItems", 0))
        ]
    if rule_type == "object":
        return {
            key: _minimal_value(rule["properties"][key], root=root)
            for key in rule.get("required", [])
        }
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
