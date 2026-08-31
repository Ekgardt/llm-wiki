"""Dead code answers three ways, not one.

Measured 2026-08-28 on the live vault: `find_dead_code` returned 868
candidates, of which 69 were protocol methods the language invokes itself and
384 were named by some call text — so 52% of the answer was indefensible once
both are counted. The graph held the distinction; the answer did not look.
Research: `docs/research/2026-08-28-what-makes-code-defensibly-dead.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402


class _Graph:
    """Enough graph to build one candidate, with a scriptable name set."""

    def __init__(self, names: object) -> None:
        self._names = names

    def call_target_names(self):
        if isinstance(self._names, Exception):
            raise self._names
        return frozenset(self._names)


def _node(name: str) -> dict:
    return {
        "node_id": f"node:{name}",
        "metadata": {"name": name, "path": "scripts/thing.py", "owner": "scripts.Thing"},
    }


@pytest.fixture(autouse=True)
def _fixed_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        code_graph, "_stored_location", lambda *_a: ("scripts/thing.py", 7)
    )


def _reasons(names: object, symbols: list[str]) -> list[str | None]:
    graph = _Graph(names)
    built = code_graph._stored_dead_candidates(
        graph, [_node(name) for name in symbols], Path("/repo")
    )
    found = {item["name"]: item["reason"] for item in built}
    return [found.get(name) for name in symbols]


@pytest.mark.parametrize(
    "dunder", ["__post_init__", "__reduce__", "__enter__", "__exit__", "__call__"]
)
def test_a_protocol_method_is_never_a_candidate(dunder: str) -> None:
    assert _reasons([], [dunder]) == [None]


def test_a_name_nothing_calls_is_defensibly_dead() -> None:
    assert _reasons(["other"], ["orphan"]) == ["zero_confirmed_incoming_calls"]


def test_a_name_some_call_site_mentions_is_only_doubt() -> None:
    assert _reasons(["reachable"], ["reachable"]) == ["unresolved_receiver"]


def test_a_refused_name_set_makes_every_verdict_doubt() -> None:
    """Fail closed: a set that could not be read cannot prove anything dead."""
    refusal = ValueError("query row ceiling exceeded")
    assert _reasons(refusal, ["orphan", "reachable"]) == [
        "unresolved_receiver",
        "unresolved_receiver",
    ]


def test_the_two_verdicts_are_told_apart_in_one_answer() -> None:
    assert _reasons(["reachable"], ["orphan", "reachable"]) == [
        "zero_confirmed_incoming_calls",
        "unresolved_receiver",
    ]
