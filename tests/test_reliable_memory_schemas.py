from __future__ import annotations

import json
from pathlib import Path

import pytest
from reliable_memory import SchemaValidationError, validate_schema

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
