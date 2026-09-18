"""Audit 3, B26 at the MCP boundary: a damaged cache degrades, it does not fail.

`code_graph._active_evidence_graph` now raises `GenerationUnreadable` where it
used to answer `None`. The freshness decoration and the navigation graph opener
must both keep answering. Research:
`docs/research/2026-09-18-graph-an-unreadable-generation-must-not-fail-a-good-answer.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402
import mcp_server  # noqa: E402

UNREADABLE = code_graph.GenerationUnreadable


def _raise_unreadable(*_args, **_options):
    raise UNREADABLE(ValueError("catalog is not a database"))


def _scope() -> SimpleNamespace:
    return SimpleNamespace(
        checkout_root="/nowhere", repository_id="repo", checkout_id="checkout"
    )


def test_the_freshness_decoration_says_the_generation_is_unreadable(monkeypatch):
    monkeypatch.setattr(code_graph, "_active_evidence_graph", _raise_unreadable)

    assert mcp_server._repository_freshness(Path(".")) == {
        "unavailable": "generation_unreadable:ValueError"
    }


def test_the_freshness_decoration_says_when_its_bound_is_reached(monkeypatch):
    def expire(*_args, **_options):
        raise TimeoutError("generation catalog deadline reached")

    monkeypatch.setattr(code_graph, "_active_evidence_graph", expire)

    assert mcp_server._repository_freshness(Path(".")) == {"unavailable": "deadline"}


def test_navigation_degrades_on_an_unreadable_generation(monkeypatch):
    monkeypatch.setattr(code_graph, "_active_evidence_graph", _raise_unreadable)
    monkeypatch.setattr(mcp_server, "_operation_cancelled", lambda: (lambda: False))

    assert mcp_server._open_navigation_graph(_scope(), None) is None
