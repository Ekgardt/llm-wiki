"""A never-activated generation no writer touched for a day is removed; a stray tree fails nothing.

956 MB of such publications stayed forever, and one aborted build's tree made the
nightly prune exit 1 every night. Research:
`docs/research/2026-09-14-an-abandoned-publication-is-collected.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from tests.an_abandoned_publication_is_collected_helpers import _age  # noqa: E402
from tests.generation_catalog_helpers import (  # noqa: E402
    _catalog,  # noqa: E402
    _publish,  # noqa: E402
)
from tests.prune_generations_helpers import _chain, _footprint  # noqa: E402


def test_a_day_old_unactivated_publication_is_removed_and_a_fresh_one_kept(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2"])
    for name in ("gen-abandoned", "gen-flight"):
        _publish(catalog, name, parent="gen-2")
        catalog.register(name)
    _age(catalog.generations_path / "gen-abandoned")

    lines = prune_generations.prune_generations(state_root=catalog.state_root, apply=True)

    assert (_footprint(catalog, "gen-abandoned")[:2], _footprint(catalog, "gen-flight")[:2], prune_generations.report(lines)) == (
        (False, 0),
        (True, 1),
        0,
    )


def test_a_tree_with_no_registration_is_reported_but_does_not_fail_the_pass(tmp_path):
    import prune_generations

    catalog = _catalog(tmp_path)
    _chain(catalog, ["gen-1", "gen-2"])
    stray = catalog.generations_path / "gen-stray"
    stray.mkdir()
    (stray / "evidence.sqlite3").write_bytes(b"half built")

    lines = prune_generations.prune_generations(state_root=catalog.state_root, apply=True)

    assert (any(line.startswith("ORPHAN: gen-stray") for line in lines), prune_generations.report(lines), stray.exists()) == (
        True,
        0,
        True,
    )
