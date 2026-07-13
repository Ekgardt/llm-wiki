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


def test_freshness_uses_available_index_timestamp(tmp_path):
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

    envelope = build_envelope({}, root=tmp_path, now=now)

    assert envelope["index_timestamp"] == recent.isoformat()
    assert envelope["freshness"] == "fresh"


def test_freshness_is_stale_for_old_index_and_unknown_without_one(tmp_path):
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

    assert stale["freshness"] == "stale"
    assert unknown["freshness"] == "unknown"
    assert unknown["index_timestamp"] is None
    assert any("index" in warning.lower() for warning in unknown["warnings"])


@pytest.mark.parametrize(
    "failure",
    [
        subprocess.TimeoutExpired(["git"], 1),
        OSError("git unavailable"),
    ],
)
def test_source_commit_failures_are_local_warnings(tmp_path, monkeypatch, failure):
    import mcp_contract

    mcp_contract._source_commit.cache_clear()

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(mcp_contract.subprocess, "run", fail)

    envelope = mcp_contract.build_envelope({}, root=tmp_path)

    assert envelope["source_commit"] is None
    assert any("commit" in warning.lower() for warning in envelope["warnings"])


def test_source_commit_nonzero_exit_is_a_local_warning(tmp_path, monkeypatch):
    import mcp_contract

    mcp_contract._source_commit.cache_clear()
    monkeypatch.setattr(
        mcp_contract.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, "", "fatal"),
    )

    envelope = mcp_contract.build_envelope({}, root=tmp_path)

    assert envelope["source_commit"] is None
    assert any("commit" in warning.lower() for warning in envelope["warnings"])


@pytest.mark.parametrize("failure_type", [OverflowError, OSError, ValueError])
def test_index_timestamp_conversion_failures_are_unknown(
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
    assert any("index" in warning.lower() for warning in envelope["warnings"])


def test_envelope_schema_declares_every_field_type_and_numeric_bounds():
    from mcp_contract import envelope_schema

    schema = envelope_schema()
    properties = schema["properties"]

    assert set(schema["required"]) == MANDATORY_FIELDS
    assert schema["additionalProperties"] is False
    assert properties["schema_version"]["type"] == "string"
    assert properties["generated_at"]["type"] == "string"
    assert properties["generated_at"]["format"] == "date-time"
    assert properties["index_timestamp"]["type"] == ["string", "null"]
    assert properties["source_commit"]["type"] == ["string", "null"]
    assert properties["freshness"]["type"] == "string"
    assert set(properties["freshness"]["enum"]) == {"fresh", "stale", "unknown"}
    for field in ("coverage", "confidence"):
        assert properties[field] == {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        }
    assert properties["fallback"]["type"] == "boolean"
    assert properties["partial"]["type"] == "boolean"
    assert properties["warnings"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert properties["data"] == {}


def test_built_envelope_values_match_declared_types_and_bounds(tmp_path):
    from mcp_contract import build_envelope

    envelope = build_envelope(
        {"result": True},
        root=tmp_path,
        coverage=0.25,
        confidence=0.75,
        fallback=True,
        partial=True,
        warnings=["degraded"],
    )

    assert isinstance(envelope["schema_version"], str)
    assert isinstance(envelope["generated_at"], str)
    assert envelope["index_timestamp"] is None or isinstance(
        envelope["index_timestamp"], str
    )
    assert envelope["source_commit"] is None or isinstance(
        envelope["source_commit"], str
    )
    assert envelope["freshness"] in {"fresh", "stale", "unknown"}
    assert isinstance(envelope["coverage"], float)
    assert 0 <= envelope["coverage"] <= 1
    assert isinstance(envelope["confidence"], float)
    assert 0 <= envelope["confidence"] <= 1
    assert isinstance(envelope["fallback"], bool)
    assert isinstance(envelope["partial"], bool)
    assert isinstance(envelope["warnings"], list)
    assert all(isinstance(warning, str) for warning in envelope["warnings"])
    assert envelope["data"] == {"result": True}


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
