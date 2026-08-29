"""A caller could not ask a dependency question smaller than the whole closure.

`mode=dependencies` always walked the full reachable set. Measured on the live
generation 2026-08-29 for `scripts/retrieval.py`: the whole set is 346 rows —
59 modules and 287 individual functions and classes — costing 20 735 tokens
after shaping, while the question the parity stand asks, "which project
modules does this file depend on", is answered by the 7 direct modules for
338 tokens. Sixty-one times the cost, for an answer that buries the one asked
for.

`depth` is additive: omitting it walks exactly as far as it always has, so no
existing caller moves. Whether the default should change is a separate
question and is not decided here.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402
import mcp_server  # noqa: E402


class _RecordingGraph:
    """Records the depth each walk was asked for."""

    def __init__(self) -> None:
        self.depths: list[int] = []

    def reachable(self, seed: str, **kwargs) -> list[dict]:
        del seed
        self.depths.append(kwargs["max_depth"])
        return []


@pytest.mark.parametrize(
    ("asked", "expected"),
    (
        (None, code_graph.DEPENDENCY_MAX_DEPTH),
        (1, 1),
        (2, 2),
        (code_graph.DEPENDENCY_MAX_DEPTH, code_graph.DEPENDENCY_MAX_DEPTH),
        (99, code_graph.DEPENDENCY_MAX_DEPTH),
        (0, 1),
        (-4, 1),
    ),
)
def test_the_walk_is_asked_for_the_depth_the_caller_named(
    asked: int | None, expected: int
) -> None:
    """Omitted keeps today's reach; out of range is clamped, never refused."""
    graph = _RecordingGraph()

    code_graph._stored_dependency_rows(graph, ["seed"], False, asked)

    assert graph.depths == [expected]


def test_every_seed_is_walked_at_the_same_depth() -> None:
    graph = _RecordingGraph()

    code_graph._stored_dependency_rows(graph, ["one", "two", "three"], False, 2)

    assert graph.depths == [2, 2, 2]


def test_the_mode_forwards_the_depth_it_was_given(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def find_dependencies(symbol, resolved, **kwargs):
        seen.update({"symbol": symbol, **kwargs})
        return []

    monkeypatch.setitem(sys.modules, "code_graph", code_graph)
    monkeypatch.setattr(code_graph, "find_dependencies", find_dependencies)

    mcp_server._architecture_dependencies(
        {
            "symbol": "scripts/retrieval.py",
            "resolved": Path("/tmp"),
            "reverse": False,
            "live": False,
            "depth": 1,
        }
    )

    assert seen["max_depth"] == 1


def test_an_absent_depth_reaches_the_walk_as_none(monkeypatch) -> None:
    """The mode must not invent a default the walk would then clamp."""
    seen: dict[str, object] = {}

    def find_dependencies(symbol, resolved, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(code_graph, "find_dependencies", find_dependencies)

    mcp_server._architecture_dependencies(
        {
            "symbol": "x",
            "resolved": Path("/tmp"),
            "reverse": False,
            "live": False,
        }
    )

    assert seen["max_depth"] is None


def test_the_mode_accepts_depth_as_an_argument() -> None:
    _, optional = mcp_server._ARCHITECTURE_CONTRACTS["dependencies"]

    assert "depth" in optional


def test_the_schema_ceiling_matches_the_walk_ceiling() -> None:
    """Stated twice so the schema need not import the graph stack; pinned here."""
    assert mcp_server.ARCHITECTURE_MAX_DEPTH == code_graph.DEPENDENCY_MAX_DEPTH
