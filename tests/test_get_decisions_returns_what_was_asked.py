"""`get_decisions` returns up to `limit` decision pages, one agent-shaped row each.

It asked the search for `limit` rows and filtered afterwards, so chunks of other
pages took the places, and it returned the search's internal row. See
docs/research/2026-09-25-get-decisions-returns-what-was-asked.md.
"""

from __future__ import annotations

import mcp_server
import search_memory


def _rows(**kwargs):
    rows = [{"path": f"knowledge/notes/concept-{n}.md", "type": "concept"} for n in range(6)]
    for n in range(3):
        rows += [
            {"path": f"knowledge/notes/choice-{n}.md", "type": "decision", "rank": 1, "chunk_id": f"{n}a"},
            {"path": f"knowledge/notes/choice-{n}.md", "type": "decision", "rank": 2, "chunk_id": f"{n}b"},
        ]
    return rows[: kwargs["limit"]]


def test_two_decisions_behind_other_pages_are_both_returned_once(monkeypatch) -> None:
    monkeypatch.setattr(search_memory, "search", lambda query, **kwargs: _rows(**kwargs))

    results = mcp_server._get_decisions("choice", 2)

    assert [row["path"] for row in results] == ["knowledge/notes/choice-0.md", "knowledge/notes/choice-1.md"]
    assert all("rank" not in row and row["type"] == "decision" for row in results)
