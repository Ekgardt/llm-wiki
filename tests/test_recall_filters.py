"""A recall narrowed by project, date, page type or path is filled from matching pages.

The filters bind every leg in SQL, before the candidate pool is cut, so a
selective filter still finds the one matching page that many others outrank.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import retrieval  # noqa: E402
import search_memory  # noqa: E402


def _artifact(*pages: tuple[str, str, str]) -> sqlite3.Connection:
    """One chunk per (path, type, text)."""
    connection = sqlite3.connect(":memory:")
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    for order, (path, page_type, text) in enumerate(pages):
        connection.execute(
            "INSERT INTO chunks(chunk_id, chunk_order, source_path, type, heading_ancestry, title, content, keys) "
            "VALUES (?, ?, ?, ?, '[]', '', ?, '')",
            [f"c{order}", order, path, page_type, text],
        )
    return connection


def _crowded_artifact() -> sqlite3.Connection:
    """Ten debugging pages that say "cache" more often than the one decision page."""
    noise = [
        (f"docs/research/cache-{n}.md", "debugging", "cache cache cache eviction") for n in range(10)
    ]
    return _artifact(*noise, ("knowledge/notes/cache-decision.md", "decision", "cache policy"))


def _paths(connection: sqlite3.Connection, **filters: str) -> list[str]:
    rows = search_memory._generation_fts_search(
        "cache",
        {"generation_id": "g"},
        connection,
        scope="all",
        limit=1,
        project=None,
        since=None,
        as_of=None,
        **filters,
    )
    return [str(row["path"]) for row in rows]


def test_a_page_type_filter_finds_the_page_others_outrank() -> None:
    assert _paths(_crowded_artifact(), page_type="Decision") == ["knowledge/notes/cache-decision.md"]


def test_a_path_prefix_filter_finds_the_page_others_outrank() -> None:
    assert _paths(_crowded_artifact(), path_prefix="./knowledge/notes/") == [
        "knowledge/notes/cache-decision.md"
    ]


def test_a_path_prefix_is_case_sensitive_as_paths_are() -> None:
    assert _paths(_crowded_artifact(), path_prefix="Knowledge/") == []


def test_a_graph_page_answers_to_the_same_filters() -> None:
    connection = _crowded_artifact()
    paths = {"docs/research/cache-1.md", "knowledge/notes/cache-decision.md"}

    eligible = search_memory.generation_eligible_paths(connection, paths, page_type="decision")

    assert eligible == {"knowledge/notes/cache-decision.md"}


def test_every_leg_drops_a_hit_of_another_type_or_path() -> None:
    rows = [
        {"path": "knowledge/notes/a.md", "type": "decision", "score": 1.0},
        {"path": "knowledge/notes/b.md", "type": "qa", "score": 1.0},
        {"path": "docs/c.md", "type": "decision", "score": 1.0},
    ]
    filters = {"scope": "all", "page_type": "decision", "path_prefix": "knowledge/"}

    assert [hit["path"] for hit in retrieval._filtered_hits(rows, filters)] == ["knowledge/notes/a.md"]


def test_recall_names_its_filters_by_the_search_keywords() -> None:
    arguments = {"query": "q", "type": "decision", "path_prefix": "knowledge/", "since": "2026-11-01"}

    assert mcp_server._recall_filters(arguments) == {
        "page_type": "decision",
        "path_prefix": "knowledge/",
        "since": "2026-11-01",
    }


def test_recall_refuses_a_date_that_is_not_a_day() -> None:
    with pytest.raises(ValueError, match="'as_of' must be a day"):
        mcp_server._recall_filters({"query": "q", "as_of": "last week"})


def test_a_grounded_recall_refuses_filters_it_cannot_apply() -> None:
    with pytest.raises(ValueError, match="plain recall"):
        mcp_server._tool_recall({"query": "q", "grounded": True, "type": "decision"}, time.monotonic() + 5)


def test_the_lexical_fallback_keeps_the_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def search(_query: str, **kwargs: object) -> list[dict]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("semantic timeout")
        return []

    monkeypatch.setattr(search_memory, "search", search)

    mcp_server._search_vault(
        "q", deadline=time.monotonic() + 5, filters={"project": "llm-wiki", "page_type": "qa"}
    )

    assert [(call["project"], call["page_type"]) for call in calls] == [("llm-wiki", "qa")] * 2


def test_the_recall_schema_declares_every_filter_it_reads() -> None:
    properties = mcp_server.TOOL_INPUT_SCHEMAS["recall"]["properties"]

    assert set(mcp_server.RECALL_FILTER_ARGUMENTS.values()) <= set(properties)
