"""A recall narrowed by project, date, page type or path is filled from matching pages.

The filters bind every leg in SQL, before the candidate pool is cut, so a
selective filter still finds the one matching page that many others outrank.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.slow_machine import SHORT_TIMEOUT

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402
import retrieval  # noqa: E402
import search_memory  # noqa: E402


def _artifact(*pages: tuple[str, str, str]) -> sqlite3.Connection:
    """One chunk per (path, type, text)."""
    connection = sqlite3.connect(":memory:")
    search_memory.register_casefold_function(connection)
    connection.executescript(search_memory._GENERATION_FTS_DDL)
    for order, (path, page_type, text) in enumerate(pages):
        connection.execute(
            "INSERT INTO chunks(chunk_id, chunk_order, source_path, type, heading_ancestry, "
            "title, content, keys) VALUES (?, ?, ?, ?, '[]', '', ?, '')",
            [f"c{order}", order, path, page_type, text],
        )
    return connection


def _crowded_artifact() -> sqlite3.Connection:
    """Ten debugging pages that say "cache" more often than the one decision page."""
    noise = [
        (f"docs/research/cache-{n}.md", "debugging", "cache cache cache eviction")
        for n in range(10)
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
        as_of=None,
        **{"since": None, **filters},
    )
    return [str(row["path"]) for row in rows]


def test_a_page_type_filter_finds_the_page_others_outrank() -> None:
    assert _paths(_crowded_artifact(), page_type="Decision") == [
        "knowledge/notes/cache-decision.md"
    ]


DECISION = "Решение"


def test_a_page_type_in_another_script_matches_in_any_case() -> None:
    connection = _artifact(
        ("knowledge/notes/a.md", "debugging", "cache cache cache"),
        ("knowledge/notes/b.md", DECISION, "cache"),
    )

    assert _paths(connection, page_type=DECISION.lower()) == ["knowledge/notes/b.md"]


def _markdown_page(root: Path, name: str, page_type: str, body: str) -> Path:
    path = root / "knowledge" / "notes" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: {page_type}\n---\n# {name}\n\n{body}\n", encoding="utf-8")
    return path


def test_without_a_generation_a_page_type_filter_finds_the_page_others_outrank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    noise = [
        _markdown_page(tmp_path, f"cache-{n}", "debugging", "cache cache cache eviction")
        for n in range(10)
    ]
    wanted = _markdown_page(tmp_path, "cache-decision", "decision", "cache policy")

    hits = search_memory.markdown_hits(
        "cache", limit=1, page_paths=[*noise, wanted], page_type="Decision"
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["cache-decision"]


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

    hits = retrieval._filtered_hits(rows, filters)

    assert [hit["path"] for hit in hits] == ["knowledge/notes/a.md"]


def test_recall_names_its_filters_by_the_search_keywords() -> None:
    arguments = {
        "query": "q",
        "type": "decision",
        "path_prefix": "knowledge/",
        "since": "2026-11-01",
    }

    assert mcp_server._recall_filters(arguments) == {
        "page_type": "decision",
        "path_prefix": "knowledge/",
        "since": "2026-11-01",
    }


def test_a_since_floor_keeps_only_pages_from_that_day_on() -> None:
    """The caller asked for a window, so older pages are left out, however well they match."""
    connection = _crowded_artifact()
    connection.execute("UPDATE chunks SET valid_from = '2026-01-01'")
    connection.execute(
        "UPDATE chunks SET valid_from = '2026-11-20' "
        "WHERE source_path = 'knowledge/notes/cache-decision.md'"
    )

    assert _paths(connection, since="2026-11-01") == ["knowledge/notes/cache-decision.md"]


@pytest.mark.parametrize("day", ["last week", "20261124", "2026-W47-2", "2026-11-1", "2026-02-30"])
def test_recall_refuses_a_date_that_is_not_a_day(day: str) -> None:
    with pytest.raises(ValueError, match="'as_of' must be a day"):
        mcp_server._recall_filters({"query": "q", "as_of": day})


def test_a_grounded_recall_refuses_filters_it_cannot_apply() -> None:
    with pytest.raises(ValueError, match="plain recall"):
        mcp_server._tool_recall(
            {"query": "q", "grounded": True, "type": "decision"}, time.monotonic() + SHORT_TIMEOUT
        )


NEW_PAGE = "knowledge/notes/new.md"
OLD_PAGE = "knowledge/notes/old.md"
HYBRID_TRACE = {
    "requested_mode": "HYBRID",
    "effective_mode": "HYBRID",
    "signals_used": ["lexical", "dense"],
    "corpus_generation": "g",
}


def _dated_recall(
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    fill: tuple[str, ...] | BaseException = (OLD_PAGE, NEW_PAGE),
    fill_trace: dict[str, object] | None = None,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    """Recall where only the new page is inside the window; `fill` answers the date-free search."""
    calls: list[dict[str, object]] = []
    impressions: list[dict[str, object]] = []

    def search(_query: str, **kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        sink, filters = kwargs["trace_sink"], kwargs["filters"]
        assert isinstance(sink, dict)
        assert isinstance(filters, dict)
        if "since" in filters:
            sink.update(HYBRID_TRACE)
            return [{"path": NEW_PAGE}]
        if isinstance(fill, BaseException):
            raise fill
        sink.update(fill_trace or HYBRID_TRACE)
        return [{"path": path} for path in fill]

    monkeypatch.setattr(mcp_server, "_search_vault", search)
    monkeypatch.setattr(retrieval, "_record_impressions", lambda rows, **kw: impressions.append(
        {"paths": [row["path"] for row in rows], **kw}
    ))
    data, _clamped = mcp_server._tool_recall(arguments, time.monotonic() + SHORT_TIMEOUT)
    assert {getattr(call["caller"], "emit_telemetry", None) for call in calls} == {False}
    return data, impressions


def _recall_paths(monkeypatch: pytest.MonkeyPatch, arguments: dict[str, object]) -> list[str]:
    data, _impressions = _dated_recall(monkeypatch, arguments)
    return [str(row["path"]) for row in data["results"]]


def test_a_recall_date_puts_pages_inside_it_first_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _recall_paths(monkeypatch, {"query": "nightly", "since": "2026-11-01", "limit": 5})

    assert paths == [NEW_PAGE, OLD_PAGE]


def test_a_recall_date_never_pushes_out_a_full_window(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _recall_paths(monkeypatch, {"query": "nightly", "since": "2026-11-01", "limit": 1})

    assert paths == [NEW_PAGE]


def test_a_fill_that_runs_out_of_time_keeps_the_dated_pages_and_says_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arguments = {"query": "nightly", "since": "2026-11-01", "limit": 5}

    data, _impressions = _dated_recall(monkeypatch, arguments, fill=TimeoutError("deadline"))

    trace = data["retrieval_trace"]
    assert [row["path"] for row in data["results"]] == [NEW_PAGE]
    assert (trace["partial"], trace["fallback_reason"]) == (True, "retrieval_deadline_exceeded")


def test_a_fill_answered_by_words_alone_makes_the_whole_answer_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lexical = {
        **HYBRID_TRACE,
        "effective_mode": "BASE",
        "signals_used": ["lexical"],
        "partial": True,
        "fallback_reason": "retrieval_deadline_exceeded",
    }
    arguments = {"query": "nightly", "since": "2026-11-01", "limit": 5}

    data, _impressions = _dated_recall(monkeypatch, arguments, fill_trace=lexical)

    trace = data["retrieval_trace"]
    assert (trace["partial"], trace["fallback_reason"]) == (True, "retrieval_deadline_exceeded")


def test_a_dated_recall_logs_each_shown_page_once_at_its_shown_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fill = (NEW_PAGE, "knowledge/notes/o1.md", "knowledge/notes/o2.md", "knowledge/notes/o3.md")
    arguments = {"query": "nightly", "since": "2026-11-01", "limit": 3}

    _data, impressions = _dated_recall(monkeypatch, arguments, fill=fill)

    assert [entry["paths"] for entry in impressions] == [
        [NEW_PAGE, "knowledge/notes/o1.md", "knowledge/notes/o2.md"]
    ]


def test_the_lexical_fallback_keeps_the_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    def search(_query: str, **kwargs: object) -> list[dict[str, object]]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise TimeoutError("semantic timeout")
        return []

    monkeypatch.setattr(search_memory, "search", search)

    mcp_server._search_vault(
        "q",
        deadline=time.monotonic() + SHORT_TIMEOUT,
        filters={"project": "llm-wiki", "page_type": "qa"},
    )

    assert [(call["project"], call["page_type"]) for call in calls] == [("llm-wiki", "qa")] * 2


def test_the_recall_schema_declares_every_filter_it_reads() -> None:
    properties = mcp_server.TOOL_INPUT_SCHEMAS["recall"]["properties"]

    assert set(mcp_server.RECALL_FILTER_ARGUMENTS.values()) <= set(properties)


def test_an_until_ceiling_keeps_only_pages_up_to_that_day() -> None:
    """The ceiling is inclusive: a page dated on the day itself is inside it."""
    connection = _crowded_artifact()
    connection.execute("UPDATE chunks SET valid_from = '2026-11-20'")
    connection.execute(
        "UPDATE chunks SET valid_from = '2026-03-01' "
        "WHERE source_path = 'knowledge/notes/cache-decision.md'"
    )

    assert _paths(connection, until="2026-03-01") == ["knowledge/notes/cache-decision.md"]


def test_since_and_until_together_keep_the_days_between() -> None:
    connection = _artifact(
        ("knowledge/notes/early.md", "qa", "cache"),
        ("knowledge/notes/inside.md", "qa", "cache"),
        ("knowledge/notes/late.md", "qa", "cache"),
    )
    for path, day in (("early", "2026-01-01"), ("inside", "2026-06-15"), ("late", "2026-12-01")):
        connection.execute(
            "UPDATE chunks SET valid_from = ? WHERE source_path = ?",
            [day, f"knowledge/notes/{path}.md"],
        )

    assert _paths(connection, since="2026-06-01", until="2026-06-30") == [
        "knowledge/notes/inside.md"
    ]


def test_every_leg_drops_a_hit_dated_after_until() -> None:
    rows = [
        {"path": "knowledge/notes/a.md", "timestamp": "2026-05-01", "score": 1.0},
        {"path": "knowledge/notes/b.md", "timestamp": "2026-09-01", "score": 1.0},
        {"path": "knowledge/notes/undated.md", "score": 1.0},
    ]

    hits = retrieval._filtered_hits(rows, {"scope": "all", "until": "2026-06-30"})

    assert [hit["path"] for hit in hits] == ["knowledge/notes/a.md", "knowledge/notes/undated.md"]


def test_without_a_generation_an_until_ceiling_leaves_out_later_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_memory, "ROOT", tmp_path)
    late = _markdown_page(tmp_path, "cache-late", "qa", "cache policy")
    late.write_text(
        "---\ntype: qa\ntimestamp: 2026-11-20\n---\n# cache-late\n\ncache policy\n",
        encoding="utf-8",
    )
    early = _markdown_page(tmp_path, "cache-early", "qa", "cache policy")
    early.write_text(
        "---\ntype: qa\ntimestamp: 2026-03-01\n---\n# cache-early\n\ncache policy\n",
        encoding="utf-8",
    )

    hits = search_memory.markdown_hits(
        "cache", limit=5, page_paths=[late, early], until="2026-06-30"
    )

    assert [Path(hit["path"]).stem for hit in hits] == ["cache-early"]


def test_recall_reads_until_as_a_date_that_orders_rather_than_cuts() -> None:
    filters = mcp_server._recall_filters(
        {"query": "q", "since": "2026-11-01", "until": "2026-11-30"}
    )

    assert filters == {"since": "2026-11-01", "until": "2026-11-30"}
    assert mcp_server._without_dates(filters) == {}


def test_recall_refuses_a_range_that_ends_before_it_starts() -> None:
    with pytest.raises(ValueError, match="'since' must not be later than 'until'"):
        mcp_server._recall_filters({"query": "q", "since": "2026-11-30", "until": "2026-11-01"})
