"""One generation the prune cannot remove is named `ERROR:`; the pass goes on.

An abandoned tree over the entry ceiling raised outside the per-generation catch
and stopped the whole step. See
docs/research/2026-09-25-one-bad-generation-does-not-stop-the-prune.md.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import prune_generations  # noqa: E402
from test_generation_catalog import _catalog, _publish  # noqa: E402
from test_prune_generations import _chain  # noqa: E402


def _aged(tree: Path, seconds: float) -> None:
    moment = time.time() - seconds
    for current, _directories, files in os.walk(tree):
        for name in files:
            os.utime(Path(current) / name, (moment, moment))
        os.utime(current, (moment, moment))


def test_an_oversized_abandoned_tree_is_an_error_line_and_the_rest_is_removed(tmp_path, monkeypatch) -> None:
    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2", "gen-3"])
    _publish(catalog, "gen-lost", parent="gen-3")
    catalog.register("gen-lost")
    _aged(catalog.generations_path / "gen-lost", 3 * 86400)
    monkeypatch.setattr(prune_generations, "MAX_GENERATION_ENTRIES", 0)

    lines = prune_generations.prune_generations(state_root=catalog.state_root, apply=True)

    errors = [line for line in lines if line.startswith("ERROR: ")]
    assert [line.split(":")[1].strip() for line in errors] == ["gen-1", "gen-lost"]
    assert lines[-1] == "reclaimed 0 bytes"
