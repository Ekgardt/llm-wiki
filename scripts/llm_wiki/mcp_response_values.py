"""Pure MCP response schema and value rules; no runtime observation or IO."""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0"


COMPONENT_FRESHNESS = {"fresh", "stale", "missing", "unknown"}


def envelope_schema() -> dict[str, Any]:
    """Return the JSON Schema shared by structured MCP tool outputs."""
    nullable_string = {"type": ["string", "null"]}
    bounded_number = {"type": "number", "minimum": 0, "maximum": 1}
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string"},
            "generated_at": {"type": "string", "format": "date-time"},
            "index_timestamp": nullable_string,
            "source_commit": nullable_string,
            "freshness": {"type": "string", "enum": ["fresh", "stale", "unknown"]},
            "coverage": bounded_number,
            "confidence": bounded_number,
            "fallback": {"type": "boolean"},
            "partial": {"type": "boolean"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "components": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "properties": {
                        "generation": nullable_string,
                        "freshness": {
                            "type": "string",
                            "enum": ["fresh", "stale", "missing", "unknown"],
                        },
                    },
                    "required": ["generation", "freshness"],
                    "additionalProperties": False,
                },
            },
            "data": {},
        },
        "required": [
            "schema_version",
            "generated_at",
            "index_timestamp",
            "source_commit",
            "freshness",
            "coverage",
            "confidence",
            "fallback",
            "partial",
            "warnings",
            "components",
            "data",
        ],
        "additionalProperties": False,
    }


def bounded(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return float(value)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def components(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("components must be an object")
    return {name: _component_detail(name, detail) for name, detail in value.items()}


def _component_detail(name: object, detail: object) -> dict[str, Any]:
    _require_component_name(name)
    if not isinstance(detail, dict) or set(detail) != {"generation", "freshness"}:
        raise ValueError("component details must contain generation and freshness")
    return _generation_and_freshness(detail["generation"], detail["freshness"])


def _require_component_name(name: object) -> None:
    if not isinstance(name, str) or not name or len(name) > 64:
        raise ValueError("component names must be bounded non-empty strings")


def _generation_and_freshness(generation: object, freshness: object) -> dict[str, Any]:
    if generation is not None and not _bounded_generation(generation):
        raise ValueError("component generation must be null or a bounded string")
    if freshness not in COMPONENT_FRESHNESS:
        raise ValueError("component freshness is invalid")
    return {"generation": generation, "freshness": freshness}


def _bounded_generation(generation: object) -> bool:
    return isinstance(generation, str) and bool(generation) and len(generation) <= 128


def freshness(components: dict[str, dict[str, Any]]) -> str:
    states = {detail["freshness"] for detail in components.values()}
    if "stale" in states:
        return "stale"
    if states and states == {"fresh"}:
        return "fresh"
    return "unknown"


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return _json_safe_structure(value)


def _json_safe_structure(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return _json_safe_sequence(value)


def _json_safe_sequence(value: Any) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return _json_or_text(value)


def _json_or_text(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value
