"""A generation directory a build may still be writing is not removed as an orphan.

The repair removed every unregistered, invalid directory, and a build in flight under
another fence is exactly that. Research:
`docs/research/2026-09-14-a-build-in-flight-is-not-an-orphan.md`.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_generation_maintenance import _empty_generation, _vault  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def test_a_fresh_unregistered_directory_survives_the_repair(tmp_path):
    import doctor
    from generation_catalog import GenerationCatalog

    root, state = _vault(tmp_path)
    _empty_generation(state, "gen-1")
    building = GenerationCatalog(state).generations_path / "building-now"
    building.mkdir()
    (building / "evidence.sqlite3").write_bytes(b"half written")

    doctor._repair_generation_catalog(
        root, state, deadline=time.monotonic() + SHORT_TIMEOUT, cancelled=lambda: False, repaired=[]
    )

    assert (building / "evidence.sqlite3").exists()
