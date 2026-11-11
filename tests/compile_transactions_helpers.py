"""Helpers shared by test_compile_transactions.py and the test files importing them."""
from __future__ import annotations

from pathlib import Path

import pytest
from reliable_memory import canonical_json_bytes, sha256_bytes


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    root = tmp_path / "vault"
    state_root = tmp_path / "state"
    state_root.mkdir()
    for relative in ("knowledge/daily/receipts", "knowledge/notes", "knowledge/projects"):
        (root / relative).mkdir(parents=True)
    (root / "knowledge/index.md").write_bytes(b"# Old index\n")
    (root / "knowledge/log.local.md").write_bytes(b"# Session Memory Log\n")
    (root / "AGENTS.md").write_bytes(b"agent contract\r\n")


    monkeypatch.setenv("LLM_WIKI_ROOT", str(root))
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(state_root))
    return root, state_root


def _daily(root: Path) -> Path:
    path = root / "knowledge/daily/2026-07-14.md"
    path.write_bytes(
        b"## [10:00:00] session-end | manual\r\n"
        b"A durable exact-byte observation.\r\n"
        b"The prior state is blue.\r\n"
        b"A second durable exact-byte observation.\r\n"
    )
    return path


def _semantic_plan() -> dict[str, object]:
    operation = {
        "action": "create",
        "category": "patterns",
        "slug": "exact-byte-pattern",
        "title": "Exact Byte Pattern",
        "summary": "Compile the bytes that were actually reviewed.",
        "body_section": "Lesson",
        "body_markdown": "Use an immutable snapshot so later appends remain pending.",
        "evidence": [
            {
                "daily_date": "2026-07-14",
                "timestamp": "10:00:00",
                "quoted_text": "A durable exact-byte observation.",
                "claim": "The reviewed source is immutable.",
            }
        ],
        "related": [],
    }
    return {
        "schema_version": "compile-plan/v2",
        "operations": [
            {
                "kind": "create",
                "path": "knowledge/notes/exact-byte-pattern.md",
                "content": canonical_json_bytes(operation).decode("utf-8"),
            }
        ],
    }


def _claim_record(root: Path, *, claim_id: str, value: str, text: str, authority: str) -> dict[str, object]:
    daily = (root / "knowledge/daily/2026-07-14.md").read_bytes()
    start = daily.index(text.encode())
    semantic = {
        "subject": "project",
        "relation": "has-state",
        "value": {"type": "string", "value": value},
        "qualifiers": [],
        "validity": {"from": "2026-07-14", "to": None},
    }
    return {
        "schema_version": "claim/v1",
        "id": claim_id,
        "fingerprint": sha256_bytes(canonical_json_bytes(semantic)),
        "text": text,
        **semantic,
        "observed_at": "2026-07-14T10:00:00Z",
        "lifecycle": "active",
        "confidence": "high",
        "authority": authority,
        "evidence": {
            "reference": (
                f"daily:2026-07-14 sha256:{sha256_bytes(daily)} "
                f"block:10:00:00 bytes:{start}-{start + len(text.encode())}"
            ),
            "sha256": sha256_bytes(text.encode()),
            "text": text,
        },
        "links": [],
        "extractor_version": "test/v1",
    }
