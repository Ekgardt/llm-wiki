"""An open stopped after the database was opened closes it before the stop is raised.

Research: `docs/research/2026-09-17-an-open-that-runs-out-of-time-closes-its-database.md`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_a_question_is_answered_by_its_own_kind_of_generation import (  # noqa: E402
    _active_memory_generation,
)
from test_repository_index import ALPHA, _repository, vault  # noqa: E402,F401

DESCRIPTORS = Path("/proc/self/fd")
MAX_STOP_POINTS = 400


class _StopAtCall:
    """A cancellation that arrives at the n-th time the open asks about it."""

    def __init__(self, stop_at: int) -> None:
        self.stop_at = stop_at
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls >= self.stop_at


def _open_graph_databases() -> list[str]:
    targets = [os.readlink(DESCRIPTORS / name) for name in os.listdir(DESCRIPTORS) if (DESCRIPTORS / name).is_symlink()]
    return [target for target in targets if target.endswith("evidence.sqlite3")]


def _stopped_open(opener, catalog, scope, stop_at: int) -> tuple[bool, list[str]]:
    """(the open was stopped, graph databases still open afterwards)."""
    try:
        graph = opener(catalog, scope, cancelled=_StopAtCall(stop_at))
    except TimeoutError:
        return True, _open_graph_databases()
    graph.close()
    return False, []


def _leaks(opener, catalog, scope) -> tuple[int, list[str]]:
    """Every stop point until the open gets through: (how many stopped it, what stayed open)."""
    stopped_count = 0
    left_open: list[str] = []
    for stop_at in range(1, MAX_STOP_POINTS):
        stopped, remaining = _stopped_open(opener, catalog, scope, stop_at)
        if not stopped:
            break
        stopped_count += 1
        left_open.extend(remaining)
    return stopped_count, left_open


@pytest.mark.skipif(not DESCRIPTORS.is_dir(), reason="needs /proc to see open descriptors")
@pytest.mark.parametrize("opener_name", ["open_code_for_repository", "open_active_for_repository"])
def test_no_stop_point_of_an_open_leaves_the_graph_database_open(vault, opener_name):  # noqa: F811
    import generation_catalog
    import repository_index
    from evidence_graph import EvidenceGraph
    from repository_scope import resolve_repository_scope

    _root, state = vault
    repository = _repository(state.parent / "repo", {"pkg/alpha.py": ALPHA})
    repository_index.index_repository(repository, state_root=state)
    catalog = generation_catalog.GenerationCatalog(state)
    # The pointer path answers memory questions only, so the active opener needs
    # a memory generation of this checkout to have anything to open:
    # docs/research/2026-09-17-a-question-is-answered-by-its-own-kind-of-generation.md
    _active_memory_generation(catalog, repository, "gen-memory")

    stopped, left_open = _leaks(getattr(EvidenceGraph, opener_name), catalog, resolve_repository_scope(repository))

    assert (stopped > 0, left_open) == (True, [])
