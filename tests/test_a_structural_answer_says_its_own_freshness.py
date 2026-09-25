"""A structural answer's component freshness is its own block's, and the commit is re-read.

The graph component said `fresh` beside `stale_by_commit: true`, and the
envelope's commit was cached for the life of the server. See
docs/research/2026-09-25-a-structural-answer-says-its-own-freshness.md.
"""

from __future__ import annotations

import mcp_contract
import mcp_server


def test_the_graph_component_follows_the_answers_freshness() -> None:
    def graph(freshness):
        data = {"source_generation": "generation-a", "freshness": freshness}
        return mcp_server._graph_components(data)["graph"]["freshness"]

    assert (
        graph({"stale_by_commit": True}),
        graph({"stale_by_commit": False}),
        graph({"unavailable": "deadline"}),
    ) == ("stale", "fresh", "unknown")


def test_the_source_commit_is_read_again_after_its_window(monkeypatch) -> None:
    commits = iter(["a" * 40, "b" * 40])
    monkeypatch.setattr(mcp_contract, "_read_source_commit", lambda _root: next(commits))
    mcp_contract._SOURCE_COMMITS.clear()
    first = mcp_contract._source_commit("/srv/repo")
    monkeypatch.setattr(mcp_contract, "SOURCE_COMMIT_TTL_SECONDS", 0.0)
    mcp_contract._SOURCE_COMMITS.clear()
    mcp_contract._SOURCE_COMMITS["/srv/repo"] = (0.0, first)

    assert (first, mcp_contract._source_commit("/srv/repo")) == ("a" * 40, "b" * 40)
