from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, validate_schema

SCHEMAS = Path(__file__).parents[1] / "scripts" / "schemas"
RELATIONS = (
    "equals",
    "has-state",
    "has-value",
    "member-of",
    "located-at",
    "starts-at",
    "ends-at",
    "uses",
    "depends-on",
)


def claim() -> dict[str, object]:
    return {
        "schema_version": "claim/v1",
        "id": "claim:source-1:0",
        "fingerprint": "a" * 64,
        "text": "Service A depends on Service B.",
        "subject": "service:a",
        "relation": "depends-on",
        "value": {"type": "entity", "value": "service:b"},
        "qualifiers": [
            {"key": "environment", "value": {"type": "string", "value": "prod"}}
        ],
        "validity": {"from": "2026-01-01T00:00:00Z", "to": None},
        "observed_at": "2026-01-02T03:04:05Z",
        "lifecycle": "active",
        "confidence": "high",
        "authority": "user",
        "evidence": {
            "reference": f"daily:2026-01-02 sha256:{'b' * 64} block:03:04:05 bytes:1-2",
            "sha256": "c" * 64,
            "text": "x",
        },
        "links": ["claim:source-0:0"],
        "extractor_version": "extractor/v1",
    }


def ledger(item: dict[str, object] | None = None) -> dict[str, object]:
    return {"schema_version": "claim-ledger/v1", "claims": [item or claim()]}


def test_relation_schema_is_the_exact_closed_vocabulary() -> None:
    schema = json.loads((SCHEMAS / "claim-relations-v1.json").read_text(encoding="utf-8"))
    assert tuple(schema["enum"]) == RELATIONS
    for relation in RELATIONS:
        validate_schema(relation, SCHEMAS / "claim-relations-v1.json")
    with pytest.raises(SchemaValidationError):
        validate_schema("mentions", SCHEMAS / "claim-relations-v1.json")


@pytest.mark.parametrize(
    "value",
    [
        {"type": "string", "value": "ready"},
        {"type": "number", "value": "2.5", "unit": "seconds"},
        {"type": "boolean", "value": True},
        {"type": "entity", "value": "service:a"},
        {"type": "date", "value": "2026-01-02"},
        {"type": "timestamp", "value": "2026-01-02T03:04:05Z"},
    ],
)
def test_ledger_accepts_every_typed_value(value: dict[str, object]) -> None:
    item = claim()
    item["value"] = value
    validate_schema(ledger(item), SCHEMAS / "claim-ledger-v1.json")


def test_ledger_rejects_unknown_fields_invalid_intervals_and_untyped_values() -> None:
    invalid = claim()
    invalid["unknown"] = True
    with pytest.raises(SchemaValidationError):
        validate_schema(ledger(invalid), SCHEMAS / "claim-ledger-v1.json")

    for number in (2.5, "1e3", "01", "1.0", "NaN", "Infinity"):
        invalid = claim()
        invalid["value"] = {"type": "number", "value": number, "unit": "seconds"}
        with pytest.raises(SchemaValidationError):
            validate_schema(ledger(invalid), SCHEMAS / "claim-ledger-v1.json")
        invalid = claim()
        invalid["qualifiers"] = [
            {"key": "latency", "value": {"type": "number", "value": number, "unit": "ms"}}
        ]
        with pytest.raises(SchemaValidationError):
            validate_schema(ledger(invalid), SCHEMAS / "claim-ledger-v1.json")

    invalid = claim()
    invalid["value"] = {"value": "service:b"}
    with pytest.raises(SchemaValidationError):
        validate_schema(ledger(invalid), SCHEMAS / "claim-ledger-v1.json")

    invalid = claim()
    invalid["validity"] = {"from": None, "to": "not-a-time"}
    with pytest.raises(SchemaValidationError):
        validate_schema(ledger(invalid), SCHEMAS / "claim-ledger-v1.json")


def test_candidate_is_closed_quarantined_and_contains_one_claim() -> None:
    candidate_claim = claim()
    candidate_claim["lifecycle"] = "quarantined"
    candidate = {
        "schema_version": "claim-candidate/v1",
        "status": "quarantined",
        "reason": "ambiguous semantic conflict",
        "claim": candidate_claim,
        "source_page": "knowledge/notes/service-a.md",
        "created_at": "2026-01-02T03:04:05Z",
    }
    validate_schema(candidate, SCHEMAS / "claim-candidate-v1.json")
    for key, value in (("status", "active"), ("extra", True)):
        invalid = copy.deepcopy(candidate)
        invalid[key] = value
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, SCHEMAS / "claim-candidate-v1.json")
    invalid = copy.deepcopy(candidate)
    invalid["claim"]["value"] = {"type": "boolean", "value": "true"}
    with pytest.raises(SchemaValidationError):
        validate_schema(invalid, SCHEMAS / "claim-candidate-v1.json")
    invalid = copy.deepcopy(candidate)
    invalid["source_page"] = "knowledge/notes/../../secret.md"
    with pytest.raises(SchemaValidationError):
        validate_schema(invalid, SCHEMAS / "claim-candidate-v1.json")
    for lifecycle in ("active", "inactive", "superseded"):
        invalid = copy.deepcopy(candidate)
        invalid["claim"]["lifecycle"] = lifecycle
        with pytest.raises(SchemaValidationError):
            validate_schema(invalid, SCHEMAS / "claim-candidate-v1.json")


def test_claim_candidate_is_not_a_durable_okf_type() -> None:
    from okf_types import CANONICAL_TYPES, INBOX_TYPES

    assert "claim-candidate" not in CANONICAL_TYPES
    assert INBOX_TYPES == frozenset({"claim-candidate"})
