"""A hybrid search that runs out of time still answers lexically within the deadline.

See docs/research/2026-09-25-the-lexical-fallback-keeps-its-own-second.md.
"""

from __future__ import annotations

import time

import mcp_server


def test_the_lexical_pass_runs_in_the_reserved_second(monkeypatch) -> None:
    seen: list[tuple[bool, float]] = []

    def run(query, limit, deadline, *, semantic, trace_sink=None, **_kwargs):
        seen.append((semantic, deadline))
        if semantic:
            raise TimeoutError("hybrid ran out of time")
        mcp_server._check_deadline(deadline)
        return [{"path": "knowledge/notes/a.md", "score": 1.0}]

    monkeypatch.setattr(mcp_server, "_run_vault_search", run)
    deadline = time.monotonic() + 5.0

    rows = mcp_server._search_vault("question", 3, deadline=deadline)

    hybrid, lexical = seen
    assert (len(rows), hybrid[1], lexical[1]) == (
        1, deadline - mcp_server.LEXICAL_FALLBACK_RESERVE_SECONDS, deadline,
    )
