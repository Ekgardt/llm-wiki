"""Contract tests for uniform MCP tool and resource responses."""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


MANDATORY_FIELDS = {
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
}


def test_build_envelope_has_all_mandatory_fields_and_utc_timestamp(tmp_path):
    from mcp_contract import build_envelope

    now = datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc)
    envelope = build_envelope({"answer": 42}, root=tmp_path, now=now)

    assert set(envelope) == MANDATORY_FIELDS
    generated = datetime.fromisoformat(envelope["generated_at"])
    assert generated == now
    assert generated.utcoffset() == timedelta(0)
    assert envelope["data"] == {"answer": 42}


def test_build_envelope_is_json_serializable_and_has_explicit_indicators(tmp_path):
    from mcp_contract import build_envelope

    envelope = build_envelope(
        {"path": tmp_path / "page.md"},
        root=tmp_path,
        warnings=["index unavailable", tmp_path / "warning"],
        fallback=True,
        partial=True,
    )

    json.dumps(envelope)
    assert envelope["warnings"][:2] == [
        "index unavailable",
        str(tmp_path / "warning"),
    ]
    assert envelope["data"] == {"path": str(tmp_path / "page.md")}
    assert envelope["fallback"] is True
    assert envelope["partial"] is True


@pytest.mark.parametrize("coverage, confidence", [(-1, 0.5), (0.5, 2), ("high", 1)])
def test_build_envelope_rejects_unbounded_quality_values(tmp_path, coverage, confidence):
    from mcp_contract import build_envelope

    with pytest.raises(ValueError):
        build_envelope(
            {},
            root=tmp_path,
            coverage=coverage,
            confidence=confidence,
        )


def test_freshness_uses_explicit_component_generations_not_fts_mtime(tmp_path):
    from mcp_contract import build_envelope

    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    index = tmp_path / "cache" / "index.sqlite"
    index.parent.mkdir()
    index.touch()
    recent = now - timedelta(minutes=30)
    timestamp = recent.timestamp()
    index.chmod(0o600)
    import os

    os.utime(index, (timestamp, timestamp))

    envelope = build_envelope(
        {},
        root=tmp_path,
        now=now,
        components={
            "lexical": {"generation": "gen-17", "freshness": "fresh"},
            "dense": {"generation": "gen-17", "freshness": "stale"},
        },
    )

    assert envelope["index_timestamp"] is None
    assert envelope["freshness"] == "stale"
    assert envelope["components"] == {
        "lexical": {"generation": "gen-17", "freshness": "fresh"},
        "dense": {"generation": "gen-17", "freshness": "stale"},
    }


def test_freshness_ignores_old_or_missing_legacy_index(tmp_path):
    from mcp_contract import build_envelope

    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    index = tmp_path / "cache" / "index.sqlite"
    index.parent.mkdir()
    index.touch()
    old = now - timedelta(days=2)
    import os

    os.utime(index, (old.timestamp(), old.timestamp()))

    stale = build_envelope({}, root=tmp_path, now=now)
    unknown = build_envelope({}, root=tmp_path / "missing", now=now)

    observed = (stale["freshness"], unknown["freshness"], unknown["index_timestamp"])
    assert observed == ("unknown", "unknown", None)
    assert (stale["components"], unknown["components"]) == ({}, {})


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 1),
        OSError("git unavailable"),
    ],
)
def test_source_commit_failures_are_local_warnings(tmp_path, monkeypatch, failure):
    import mcp_contract

    mcp_contract._SOURCE_COMMITS.clear()

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(mcp_contract.subprocess, "run", fail)

    envelope = mcp_contract.build_envelope({}, root=tmp_path)

    assert envelope["source_commit"] is None
    assert any("commit" in warning.lower() for warning in envelope["warnings"])


def test_source_commit_nonzero_exit_is_a_local_warning(tmp_path, monkeypatch):
    import mcp_contract

    mcp_contract._SOURCE_COMMITS.clear()
    monkeypatch.setattr(
        mcp_contract.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, "", "fatal"),
    )

    envelope = mcp_contract.build_envelope({}, root=tmp_path)

    assert envelope["source_commit"] is None
    assert any("commit" in warning.lower() for warning in envelope["warnings"])


@pytest.mark.parametrize("failure_type", [OverflowError, OSError, ValueError])
def test_legacy_index_timestamp_is_not_read(
    tmp_path, monkeypatch, failure_type
):
    import mcp_contract

    index = tmp_path / "cache" / "index.sqlite"
    index.parent.mkdir()
    index.touch()
    real_datetime = datetime

    class BrokenDateTime(real_datetime):
        @classmethod
        def fromtimestamp(cls, *args, **kwargs):
            raise failure_type("invalid filesystem timestamp")

    monkeypatch.setattr(mcp_contract, "datetime", BrokenDateTime)
    envelope = mcp_contract.build_envelope(
        {},
        root=tmp_path,
        now=real_datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    assert envelope["index_timestamp"] is None
    assert envelope["freshness"] == "unknown"
    assert not any("index" in warning.lower() for warning in envelope["warnings"])


_SCALAR_FIELD_TYPES = {
    "schema_version": "string",
    "generated_at": "string",
    "index_timestamp": ["string", "null"],
    "source_commit": ["string", "null"],
    "freshness": "string",
    "fallback": "boolean",
    "partial": "boolean",
}


def _value_types(envelope: dict) -> dict:
    return {
        "schema_version": type(envelope["schema_version"]),
        "generated_at": type(envelope["generated_at"]),
        "coverage": type(envelope["coverage"]),
        "confidence": type(envelope["confidence"]),
        "fallback": type(envelope["fallback"]),
        "partial": type(envelope["partial"]),
        "warnings": type(envelope["warnings"]),
        "components": type(envelope["components"]),
    }


def _schema_properties() -> tuple[dict, dict]:
    from mcp_contract import envelope_schema

    schema = envelope_schema()
    return schema, schema["properties"]


def test_envelope_schema_declares_every_field_type():
    schema, properties = _schema_properties()

    assert (set(schema["required"]), schema["additionalProperties"]) == (MANDATORY_FIELDS, False)
    assert {name: properties[name]["type"] for name in _SCALAR_FIELD_TYPES} == _SCALAR_FIELD_TYPES
    assert (properties["generated_at"]["format"], set(properties["freshness"]["enum"])) == (
        "date-time",
        {"fresh", "stale", "unknown"},
    )


def test_envelope_schema_declares_numeric_bounds_and_nested_shapes():
    _schema, properties = _schema_properties()
    bounded = {"type": "number", "minimum": 0, "maximum": 1}
    assert (properties["coverage"], properties["confidence"]) == (bounded, bounded)
    assert properties["warnings"] == {"type": "array", "items": {"type": "string"}}
    assert properties["components"]["additionalProperties"] == {
        "type": "object",
        "properties": {
            "generation": {"type": ["string", "null"]},
            "freshness": {
                "type": "string",
                "enum": ["fresh", "stale", "missing", "unknown"],
            },
        },
        "required": ["generation", "freshness"],
        "additionalProperties": False,
    }
    assert properties["data"] == {}


def _built_envelope(tmp_path) -> dict:
    from mcp_contract import build_envelope

    return build_envelope(
        {"result": True},
        root=tmp_path,
        coverage=0.25,
        confidence=0.75,
        fallback=True,
        partial=True,
        warnings=["degraded"],
    )


def test_built_envelope_values_match_declared_types(tmp_path):
    envelope = _built_envelope(tmp_path)

    assert _value_types(envelope) == {
        "schema_version": str,
        "generated_at": str,
        "coverage": float,
        "confidence": float,
        "fallback": bool,
        "partial": bool,
        "warnings": list,
        "components": dict,
    }
    optional = {type(envelope["index_timestamp"]), type(envelope["source_commit"])}
    assert optional <= {str, type(None)}


def test_built_envelope_values_keep_their_bounds(tmp_path):
    envelope = _built_envelope(tmp_path)

    assert (envelope["coverage"], envelope["confidence"]) == (0.25, 0.75)
    assert envelope["freshness"] in {"fresh", "stale", "unknown"}
    assert ({type(item) for item in envelope["warnings"]}, envelope["data"]) == ({str}, {"result": True})


def test_non_finite_data_is_normalized_to_strict_json_null(tmp_path):
    from mcp_contract import build_envelope

    envelope = build_envelope(
        {
            "nan": float("nan"),
            "positive": float("inf"),
            "nested": [float("-inf")],
        },
        root=tmp_path,
    )

    assert envelope["data"] == {
        "nan": None,
        "positive": None,
        "nested": [None],
    }
    json.dumps(envelope, allow_nan=False)
