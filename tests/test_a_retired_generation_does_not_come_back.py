"""A generation whose registration was dropped is never recovered as an orphan.

Research: `docs/research/2026-09-17-a-retired-generation-does-not-come-back.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
for directory in (TESTS.parent / "scripts", TESTS):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from test_generation_catalog import _catalog, _publish  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402


def _registered(catalog, name: str) -> None:
    _publish(catalog, name)
    catalog.register(name)


def test_a_discard_that_ran_out_of_time_after_the_row_leaves_nothing_to_recover(tmp_path):
    catalog = _catalog(tmp_path)
    _registered(catalog, "gen-retired")

    with pytest.raises(TimeoutError):
        catalog.discard_unactivated("gen-retired", deadline=time.monotonic() - 1)
    recovered = catalog.recover_orphans()

    assert (recovered, list(catalog.registered_generation_ids())) == ([], [])


def test_the_discard_is_finished_by_the_next_call(tmp_path):
    catalog = _catalog(tmp_path)
    _registered(catalog, "gen-retired")
    with pytest.raises(TimeoutError):
        catalog.discard_unactivated("gen-retired", deadline=time.monotonic() - 1)

    finished = catalog.discard_unactivated("gen-retired", deadline=time.monotonic() + SHORT_TIMEOUT)

    assert (finished, (catalog.generations_path / "gen-retired").exists()) == (True, False)


def test_a_publication_that_was_never_discarded_is_still_recovered(tmp_path):
    catalog = _catalog(tmp_path)
    _publish(catalog, "gen-crashed-before-register")

    assert catalog.recover_orphans() == ["gen-crashed-before-register"]
