"""Closed-schema validation of the navigation qualification manifest, gold set and report."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from pathlib import Path

BENCHMARK_ROOT = Path(__file__).resolve().parent
FIXTURE_MANIFEST = BENCHMARK_ROOT / "code-navigation-python-v1.json"
MANIFEST_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-v1.schema.json"
GOLD_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-gold-v1.schema.json"
REPORT_SCHEMA = BENCHMARK_ROOT / "code-navigation-python-report-v1.schema.json"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _SchemaViolation(ValueError):
    pass


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


_SCHEMA_TYPE_CHECKS: dict[str, Callable[[object], bool]] = {
    "null": lambda value: value is None,
    "boolean": lambda value: isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: _finite_number(value),
    "string": lambda value: isinstance(value, str),
    "array": lambda value: isinstance(value, list),
    "object": lambda value: isinstance(value, dict),
}


def _schema_type_matches(value: object, expected: str) -> bool:
    check = _SCHEMA_TYPE_CHECKS.get(expected)
    if check is None:
        raise _SchemaViolation("unsupported schema type")
    return check(value)


def _schema_accepts(value: object, schema: object, root: Mapping[str, object]) -> bool:
    try:
        _validate_schema_node(value, schema, root)
    except _SchemaViolation:
        return False
    return True


def _reference_step(target: object, component: str) -> object:
    if not isinstance(target, dict) or component not in target:
        raise _SchemaViolation("schema reference is unresolved")
    return target[component]


def _resolve_reference(reference: object, root: Mapping[str, object]) -> object:
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise _SchemaViolation("only local schema references are supported")
    target: object = root
    for component in reference[2:].split("/"):
        target = _reference_step(target, component)
    return target


def _all_strings(values: object) -> bool:
    return isinstance(values, list) and all(isinstance(item, str) for item in values)


def _expected_types(expected_type: object) -> list[str]:
    expected_types = [expected_type] if isinstance(expected_type, str) else expected_type
    if not _all_strings(expected_types) or not expected_types:
        raise _SchemaViolation("schema type is invalid")
    return expected_types


def _validate_type(value: object, schema: dict) -> None:
    expected_type = schema.get("type")
    if expected_type is None:
        return
    if not any(_schema_type_matches(value, item) for item in _expected_types(expected_type)):
        raise _SchemaViolation("value has the wrong type")


def _in_enum(value: object, choices: object) -> bool:
    return isinstance(choices, list) and any(_json_equal(value, item) for item in choices)


def _validate_const_enum(value: object, schema: dict) -> None:
    if "const" in schema and not _json_equal(value, schema["const"]):
        raise _SchemaViolation("value differs from schema const")
    if "enum" in schema and not _in_enum(value, schema["enum"]):
        raise _SchemaViolation("value is outside schema enum")


def _require_required_keys(value: dict, required: list) -> None:
    if any(key not in value for key in required):
        raise _SchemaViolation("required property is absent")


def _require_no_additional(value: dict, schema: dict, properties: dict) -> None:
    if schema.get("additionalProperties") is False and any(key not in properties for key in value):
        raise _SchemaViolation("additional property is forbidden")


def _validate_properties(value: dict, properties: dict, root: Mapping[str, object]) -> None:
    for key, child_schema in properties.items():
        if key in value:
            _validate_schema_node(value[key], child_schema, root)


def _validate_object(value: dict, schema: dict, root: Mapping[str, object]) -> None:
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    if not _all_strings(required):
        raise _SchemaViolation("schema required list is invalid")
    if not isinstance(properties, dict):
        raise _SchemaViolation("schema properties are invalid")
    _require_required_keys(value, required)
    _require_no_additional(value, schema, properties)
    _validate_properties(value, properties, root)


def _require_array_length(value: list, schema: dict) -> None:
    minimum_items = schema.get("minItems")
    maximum_items = schema.get("maxItems")
    if isinstance(minimum_items, int) and len(value) < minimum_items:
        raise _SchemaViolation("array is too short")
    if isinstance(maximum_items, int) and len(value) > maximum_items:
        raise _SchemaViolation("array is too long")


def _items_unique(value: list) -> bool:
    encoded = [_canonical_json(item) for item in value]
    return len(encoded) == len(set(encoded))


def _validate_array(value: list, schema: dict, root: Mapping[str, object]) -> None:
    _require_array_length(value, schema)
    if schema.get("uniqueItems") is True and not _items_unique(value):
        raise _SchemaViolation("array items are not unique")
    if "items" in schema:
        for item in value:
            _validate_schema_node(item, schema["items"], root)


def _matches_pattern(value: str, pattern: object) -> bool:
    if pattern is None:
        return True
    return isinstance(pattern, str) and re.search(pattern, value) is not None


def _validate_string(value: str, schema: dict) -> None:
    minimum_length = schema.get("minLength")
    if isinstance(minimum_length, int) and len(value) < minimum_length:
        raise _SchemaViolation("string is too short")
    if not _matches_pattern(value, schema.get("pattern")):
        raise _SchemaViolation("string does not match schema pattern")


def _validate_number(value: object, schema: dict) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if _finite_number(minimum) and float(value) < float(minimum):
        raise _SchemaViolation("number is below schema minimum")
    if _finite_number(maximum) and float(value) > float(maximum):
        raise _SchemaViolation("number is above schema maximum")


def _kind_validators() -> tuple:
    return (
        (lambda value: isinstance(value, dict), _validate_object),
        (lambda value: isinstance(value, list), _validate_array),
        (lambda value: isinstance(value, str), lambda value, schema, _root: _validate_string(value, schema)),
        (_finite_number, lambda value, schema, _root: _validate_number(value, schema)),
    )


def _validate_by_kind(value: object, schema: dict, root: Mapping[str, object]) -> None:
    for applies, validate in _kind_validators():
        if applies(value):
            validate(value, schema, root)


def _validate_one_of(value: object, schema: dict, root: Mapping[str, object]) -> None:
    one_of = schema.get("oneOf")
    if one_of is None:
        return
    if not isinstance(one_of, list) or sum(_schema_accepts(value, candidate, root) for candidate in one_of) != 1:
        raise _SchemaViolation("value does not match exactly one schema branch")


def _validate_branch(value: object, branch: object, root: Mapping[str, object]) -> None:
    if not isinstance(branch, dict):
        raise _SchemaViolation("schema allOf branch is invalid")
    condition = branch.get("if")
    if condition is None:
        _validate_schema_node(value, branch, root)
        return
    selected = branch.get("then", {}) if _schema_accepts(value, condition, root) else branch.get("else", {})
    _validate_schema_node(value, selected, root)


def _validate_all_of(value: object, schema: dict, root: Mapping[str, object]) -> None:
    all_of = schema.get("allOf", [])
    if not isinstance(all_of, list):
        raise _SchemaViolation("schema allOf is invalid")
    for branch in all_of:
        _validate_branch(value, branch, root)


def _validate_schema_node(
    value: object,
    schema: object,
    root: Mapping[str, object],
) -> None:
    if not isinstance(schema, dict):
        raise _SchemaViolation("schema node must be an object")
    reference = schema.get("$ref")
    if reference is not None:
        _validate_schema_node(value, _resolve_reference(reference, root), root)
        return
    _validate_type(value, schema)
    _validate_const_enum(value, schema)
    _validate_by_kind(value, schema, root)
    _validate_one_of(value, schema, root)
    _validate_all_of(value, schema, root)


def _validate_schema(value: object, schema_path: Path, label: str) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} schema validation is unavailable") from exc
    try:
        if not isinstance(schema, dict):
            raise _SchemaViolation("schema root must be an object")
        _validate_schema_node(value, schema, schema)
    except (_SchemaViolation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} schema validation failed") from exc


def load_manifest(path: Path = FIXTURE_MANIFEST) -> dict[str, object]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return value


def validate_manifest(value: object) -> None:
    _validate_schema(value, MANIFEST_SCHEMA, "manifest")


def validate_gold(value: object) -> None:
    _validate_schema(value, GOLD_SCHEMA, "gold")


def validate_report(value: object) -> None:
    try:
        _canonical_json(value)
    except ValueError as exc:
        raise ValueError("report numbers must be finite") from exc
    _validate_schema(value, REPORT_SCHEMA, "report")


def _is_count(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 0


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )
