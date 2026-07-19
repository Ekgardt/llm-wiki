"""Uniform, JSON-compatible response contract for local MCP operations."""
from __future__ import annotations

import json
import math
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
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


def build_envelope(
    data: Any,
    *,
    root: Path | None = None,
    state_root: Path | None = None,
    now: datetime | None = None,
    coverage: float = 1.0,
    confidence: float = 1.0,
    fallback: bool = False,
    partial: bool = False,
    warnings: list[Any] | None = None,
    components: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one conservative response envelope from local metadata."""
    coverage = _bounded("coverage", coverage)
    confidence = _bounded("confidence", confidence)
    generated_at = _as_utc(now or datetime.now(timezone.utc))
    vault_root, runtime_root = _roots(root, state_root)
    response_warnings = [str(item) for item in warnings or []]

    component_details = _components(components)
    freshness = _freshness(component_details)
    source_commit = _source_commit(str(vault_root))
    if source_commit is None:
        response_warnings.append("Source commit is unavailable.")

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "index_timestamp": None,
        "source_commit": source_commit,
        "freshness": freshness,
        "coverage": coverage,
        "confidence": confidence,
        "fallback": bool(fallback),
        "partial": bool(partial),
        "warnings": response_warnings,
        "components": component_details,
        "data": _json_safe(data),
    }


def _roots(root: Path | None, state_root: Path | None) -> tuple[Path, Path]:
    if root is not None:
        vault_root = Path(root).resolve()
        return vault_root, Path(state_root or vault_root).resolve()
    try:
        from memory_state import ROOT, STATE_ROOT

        return ROOT, Path(state_root or STATE_ROOT).resolve()
    except (ImportError, OSError):
        fallback_root = Path(__file__).resolve().parent.parent
        return fallback_root, Path(state_root or fallback_root).resolve()


def _bounded(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return float(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _components(value: dict[str, dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError("components must be an object")
    normalized: dict[str, dict[str, Any]] = {}
    for name, detail in value.items():
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ValueError("component names must be bounded non-empty strings")
        if not isinstance(detail, dict) or set(detail) != {"generation", "freshness"}:
            raise ValueError("component details must contain generation and freshness")
        generation = detail["generation"]
        freshness = detail["freshness"]
        if generation is not None and (
            not isinstance(generation, str) or not generation or len(generation) > 128
        ):
            raise ValueError("component generation must be null or a bounded string")
        if freshness not in COMPONENT_FRESHNESS:
            raise ValueError("component freshness is invalid")
        normalized[name] = {"generation": generation, "freshness": freshness}
    return normalized


def _freshness(components: dict[str, dict[str, Any]]) -> str:
    states = {detail["freshness"] for detail in components.values()}
    if "stale" in states:
        return "stale"
    if states and states == {"fresh"}:
        return "fresh"
    return "unknown"


@lru_cache(maxsize=8)
def _source_commit(root: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value
