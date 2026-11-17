"""The existing closed JSON-schema contract, independent of runtime state."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any


class SchemaValidationError(ValueError):
    """Raised when an instance does not satisfy a committed schema."""


def validate_schema(instance: object, schema_path: Path) -> None:
    """Validate the closed JSON Schema subset used by committed contracts."""
    try:
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"cannot load schema {schema_path}: {exc}") from exc
    validate_schema_object(instance, schema)


def validate_schema_object(instance: object, schema: object) -> None:
    """Validate against an already captured and parsed closed schema."""
    if not isinstance(schema, dict):
        raise SchemaValidationError("schema root must be an object")
    validate_rule(instance, schema, "$", root=schema)


def _ref_step(target: object, raw_part: str, reference: str, location: str) -> object:
    part = raw_part.replace("~1", "/").replace("~0", "~")
    if not isinstance(target, dict) or part not in target:
        raise SchemaValidationError(f"{location}: unresolved schema ref {reference}")
    return target[part]


def _resolved_ref_target(reference: object, root: dict, location: str) -> dict:
    """The schema a `$ref` names, resolved against the document root."""
    if not isinstance(reference, str) or not reference.startswith("#/"):
        raise SchemaValidationError(
            f"{location}: only internal JSON Pointer refs are supported"
        )
    target: object = root
    for raw_part in reference[2:].split("/"):
        target = _ref_step(target, raw_part, reference, location)
    if not isinstance(target, dict):
        raise SchemaValidationError(f"{location}: schema ref {reference} is not an object")
    return target


def _option_matches(instance: object, option: dict, location: str, root: dict) -> bool:
    try:
        validate_rule(instance, option, location, root=root)
    except SchemaValidationError:
        return False
    return True


def _require_one_of(instance: object, rule: dict, location: str, root: dict) -> None:
    matches = sum(
        _option_matches(instance, option, location, root) for option in rule["oneOf"]
    )
    if matches != 1:
        raise SchemaValidationError(
            f"{location}: expected exactly one oneOf match, got {matches}"
        )


def _require_const(instance: object, rule: dict, location: str) -> None:
    if "const" not in rule:
        return
    if not _json_equal(instance, rule["const"]):
        raise SchemaValidationError(f"{location}: expected const {rule['const']!r}")


def _require_enum(instance: object, rule: dict, location: str) -> None:
    if "enum" not in rule:
        return
    if not any(_json_equal(instance, candidate) for candidate in rule["enum"]):
        raise SchemaValidationError(f"{location}: value is not in enum")


def _require_const_and_enum(instance: object, rule: dict, location: str) -> None:
    _require_const(instance, rule, location)
    _require_enum(instance, rule, location)


def _expected_types(instance: object, rule: dict, location: str) -> tuple[str, ...]:
    expected = rule.get("type")
    if expected is None:
        return ()
    types = _schema_types(expected)
    if not _matches_type(instance, expected):
        raise SchemaValidationError(f"{location}: expected {expected}")
    return types


def _require_required_properties(instance: dict, rule: dict, location: str) -> None:
    missing = [key for key in rule.get("required", []) if key not in instance]
    if missing:
        raise SchemaValidationError(f"{location}: missing required properties {missing}")


def _require_no_unknown_properties(instance: dict, rule: dict, location: str) -> None:
    if rule.get("additionalProperties") is not False:
        return
    unknown = sorted(set(instance) - set(rule.get("properties", {})))
    if unknown:
        raise SchemaValidationError(f"{location}: unknown properties {unknown}")


def _validate_property(
    key: str, value: object, properties: dict, location: str, root: dict
) -> None:
    if key not in properties:
        return
    validate_rule(value, properties[key], f"{location}.{key}", root=root)


def _validate_object(instance: object, rule: dict, location: str, root: dict) -> None:
    assert isinstance(instance, dict)
    _require_required_properties(instance, rule, location)
    _require_no_unknown_properties(instance, rule, location)
    properties = rule.get("properties", {})
    for key, value in instance.items():
        _validate_property(key, value, properties, location, root)


def _require_item_is_new(
    item: object, earlier: list, index: int, location: str
) -> None:
    if any(_json_equal(item, previous) for previous in earlier):
        raise SchemaValidationError(
            f"{location}: expected uniqueItems, duplicate at index {index}"
        )


def _require_unique_items(instance: list, rule: dict, location: str) -> None:
    if rule.get("uniqueItems") is not True:
        return
    for index, item in enumerate(instance):
        _require_item_is_new(item, instance[:index], index, location)


def _validate_array(instance: object, rule: dict, location: str, root: dict) -> None:
    assert isinstance(instance, list)
    _check_bound(len(instance), rule, "minItems", "maxItems", location)
    _require_unique_items(instance, rule, location)
    if "items" not in rule:
        return
    for index, item in enumerate(instance):
        validate_rule(item, rule["items"], f"{location}[{index}]", root=root)


def _validate_string(instance: object, rule: dict, location: str, root: dict) -> None:
    del root
    assert isinstance(instance, str)
    _check_bound(len(instance), rule, "minLength", "maxLength", location)
    if "pattern" in rule and re.search(rule["pattern"], instance) is None:
        raise SchemaValidationError(f"{location}: string does not match pattern")


def _validate_number(instance: object, rule: dict, location: str, root: dict) -> None:
    del root
    _check_bound(instance, rule, "minimum", "maximum", location)
    if "exclusiveMinimum" in rule and instance <= rule["exclusiveMinimum"]:
        raise SchemaValidationError(f"{location}: below exclusiveMinimum")
    if "exclusiveMaximum" in rule and instance >= rule["exclusiveMaximum"]:
        raise SchemaValidationError(f"{location}: above exclusiveMaximum")


def _is_json_number(instance: object) -> bool:
    return isinstance(instance, (int, float)) and not isinstance(instance, bool)


_TYPE_VALIDATORS = (
    (lambda types, value: "object" in types and isinstance(value, dict), _validate_object),
    (lambda types, value: "array" in types and isinstance(value, list), _validate_array),
    (lambda types, value: "string" in types and isinstance(value, str), _validate_string),
    (
        lambda types, value: bool({"integer", "number"}.intersection(types))
        and _is_json_number(value),
        _validate_number,
    ),
)


def _validate_typed(
    instance: object, rule: dict, location: str, root: dict, types: tuple[str, ...]
) -> None:
    for selects, validator in _TYPE_VALIDATORS:
        if selects(types, instance):
            validator(instance, rule, location, root)
            return


def _validate_resolved_rule(
    instance: object, rule: dict[str, Any], location: str, root: dict[str, Any]
) -> None:
    """Everything a rule says once any `$ref` has been followed."""
    if "oneOf" in rule:
        _require_one_of(instance, rule, location, root)
    _require_const_and_enum(instance, rule, location)
    types = _expected_types(instance, rule, location)
    _validate_typed(instance, rule, location, root, types)


def validate_rule(
    instance: object,
    rule: dict[str, Any],
    location: str,
    *,
    root: dict[str, Any] | None = None,
) -> None:
    if root is None:
        root = rule
    reference = rule.get("$ref")
    if reference is not None:
        target = _resolved_ref_target(reference, root, location)
        validate_rule(instance, target, location, root=root)
        return
    _validate_resolved_rule(instance, rule, location, root)


def _require_string_type_names(expected: list) -> None:
    if not expected:
        raise SchemaValidationError("schema type array must not be empty")
    if not all(isinstance(value, str) for value in expected):
        raise SchemaValidationError("schema type array must contain only strings")


def _schema_type_array(expected: list) -> tuple[str, ...]:
    _require_string_type_names(expected)
    if len(set(expected)) != len(expected):
        raise SchemaValidationError("schema type array must contain unique values")
    return tuple(expected)


def _named_schema_types(expected: object) -> tuple[str, ...]:
    if isinstance(expected, str):
        return (expected,)
    if isinstance(expected, list):
        return _schema_type_array(expected)
    raise SchemaValidationError("schema type must be a string or array of strings")


def _schema_types(expected: object) -> tuple[str, ...]:
    values = _named_schema_types(expected)
    unknown = [value for value in values if value not in _SCHEMA_TYPE_CHECKS]
    if unknown:
        raise SchemaValidationError(f"unsupported schema type: {unknown[0]}")
    return values


def _matches_type(instance: object, expected: object) -> bool:
    return _matches_types(instance, _schema_types(expected))


def _matches_types(instance: object, expected: tuple[str, ...]) -> bool:
    return any(_SCHEMA_TYPE_CHECKS[value](instance) for value in expected)


_SCHEMA_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    ),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}


def _json_equal(left: object, right: object) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    return left == right


def _check_bound(
    value: int | float,
    rule: dict[str, Any],
    minimum_name: str,
    maximum_name: str,
    location: str,
) -> None:
    if minimum_name in rule and value < rule[minimum_name]:
        raise SchemaValidationError(f"{location}: below {minimum_name}")
    if maximum_name in rule and value > rule[maximum_name]:
        raise SchemaValidationError(f"{location}: above {maximum_name}")
