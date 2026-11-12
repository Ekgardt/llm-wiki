"""Helpers shared by test_compile_claims_producer.py and the test files importing them."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

DATE = "2026-07-14"


QUOTE = "The maintenance lease expires after 30 seconds."


SECOND_QUOTE = "The nightly pass runs at 03:00 local time."


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Old index\n")
    (root / "knowledge/log.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"agent contract\n")
    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root, state_root


def _daily(root: Path, name: str = f"{DATE}.md") -> Path:
    path = root / "knowledge/daily" / name
    path.write_text(
        f"# Daily Session Memory — {DATE}\n\n"
        "## [10:00:00] session-end | manual\n"
        f"{QUOTE}\n"
        f"{SECOND_QUOTE}\n",
        encoding="utf-8",
    )
    return path


def _evidence(quote: str = QUOTE, timestamp: str = "10:00:00") -> dict[str, str]:
    return {
        "daily_date": DATE,
        "timestamp": timestamp,
        "quoted_text": quote,
        "claim": "The lease has a bounded expiry.",
    }


def _operation(claims: list[object] | None = None) -> dict[str, object]:
    operation: dict[str, object] = {
        "action": "create",
        "category": "decisions",
        "slug": "bounded-lease-expiry",
        "title": "Bounded lease expiry",
        "summary": "The maintenance lease expires rather than wedging.",
        "body_section": "Decision",
        "body_markdown": "A lease that cannot expire is a permanent wedge.",
        "evidence": [_evidence()],
        "related": [],
    }
    if claims is not None:
        operation["claims"] = claims
    return operation


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "evidence_index": 0,
        "subject": "maintenance lease",
        "relation": "ends-at",
        "value": {"type": "number", "value": "30", "unit": "seconds"},
    }
    candidate.update(overrides)
    return candidate


def _draft(operations: list[dict[str, object]]) -> str:
    return json.dumps({"operations": operations, "audit": {}})
