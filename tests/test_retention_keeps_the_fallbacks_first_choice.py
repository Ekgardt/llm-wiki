"""Retention keeps the generation the fallback would try first.

A full rebuild records no parent, so walking the parent chain kept no spare and
the prune removed the previously active generation, the fallback's first choice.
See docs/research/2026-09-25-retention-keeps-the-fallbacks-first-choice.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from test_generation_catalog import _catalog, _publish  # noqa: E402


def test_a_rebuild_without_a_parent_keeps_the_previous_activation(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-1", parent=None)
    catalog.register("gen-1")
    catalog.activate("gen-1", expected_active=None)
    _publish(catalog, "gen-2", parent=None)
    catalog.register("gen-2")
    catalog.activate("gen-2", expected_active="gen-1")

    assert catalog.retained_generations() == ("gen-2", "gen-1")


def test_an_incremental_chain_still_keeps_one_spare(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    previous = None
    for name in ("gen-1", "gen-2", "gen-3"):
        _publish(catalog, name, parent=previous)
        catalog.register(name)
        catalog.activate(name, expected_active=previous)
        previous = name

    assert catalog.retained_generations() == ("gen-3", "gen-2")
