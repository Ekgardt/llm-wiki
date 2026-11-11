"""A navigation answer is fresh only when it was computed from the current revision (audit 2026-09-27 C-19).

docs/research/2026-09-27-an-answer-without-a-provider-is-not-fresh.md
"""
from __future__ import annotations

import mcp_server
from code_navigation import NavigationStatus


def test_every_navigation_status_names_its_freshness() -> None:
    named = {status.value: mcp_server.navigation_freshness(status.value) for status in NavigationStatus}

    assert (set(named) <= set(mcp_server.NAVIGATION_FRESHNESS), named["unsupported"], named["ok"]) == (
        True, "missing", "fresh"
    )


def test_an_unknown_status_is_not_assumed_fresh() -> None:
    assert mcp_server.navigation_freshness("something-new") == "unknown"
