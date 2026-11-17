"""Helpers shared by test_dense_admission_trust_weight.py and the test files importing them."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import generation_search
import pytest

_COLUMNS = (
    "chunk_id TEXT, chunk_order INTEGER, source_id TEXT, source_path TEXT, "
    "source_sha256 TEXT, heading_ancestry TEXT, type TEXT, project TEXT, "
    "authority TEXT, confidence TEXT, status TEXT, valid_from TEXT, "
    "valid_to TEXT, language TEXT, title TEXT, content TEXT, span_sha256 TEXT, rank REAL"
)


_COMMENTARY = "docs/research/2026-08-29-commentary.md"


_DECISION = "knowledge/notes/some-decision.md"


def _row(order: int, path: str, page_type: str) -> tuple:
    return (
        f"chunk-{order}",
        order,
        f"source:{path}",
        path,
        "0" * 64,
        json.dumps([]),
        page_type,
        "",
        "ai-derived",
        "high",
        "active",
        "2026-08-01",
        None,
        "en",
        Path(path).stem,
        "body text",
        "1" * 64,
        0.0,
    )


@pytest.fixture()
def connection() -> sqlite3.Connection:
    handle = sqlite3.connect(":memory:")
    handle.row_factory = sqlite3.Row
    handle.execute(f"CREATE TABLE chunks ({_COLUMNS})")
    handle.executemany(
        f"INSERT INTO chunks VALUES ({','.join('?' * 18)})",
        (_row(0, _COMMENTARY, "doc"), _row(1, _DECISION, "decision")),
    )
    handle.commit()
    return handle


def _scored(connection: sqlite3.Connection, similarities: list[float]) -> list[dict]:
    return generation_search.vector_scored_rows(
        connection,
        similarities,
        "generation-test",
        scope="all",
        since=None,
        as_of=None,
        project=None,
        deadline=None,
        cancelled=None,
    )
