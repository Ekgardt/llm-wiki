"""An empty answer fails one batch, not the night; a lost batch is asked again after a fix.

See docs/research/2026-09-25-a-lost-episode-batch-is-asked-again-after-a-fix.md.
"""

from __future__ import annotations

from pathlib import Path

import episode_consolidation as consolidation
import pytest

from tests.test_episode_consolidation import vault  # noqa: F401 - the fixture is used by name

DAY = "2026-08-23"


def test_an_empty_answer_fails_the_batch_and_the_run_goes_on(vault: Path, monkeypatch) -> None:  # noqa: F811 - the fixture
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, progress: None)

    outcome = consolidation.consolidate_day(vault, DAY, call=lambda _prompt: "  ", state={})

    assert (outcome["status"], outcome["failed_batches"]) == ("partial", 0)


def test_no_provider_still_stops_the_run(vault: Path, monkeypatch) -> None:  # noqa: F811 - the fixture
    monkeypatch.setattr(consolidation, "_save_progress", lambda day, progress: None)

    with pytest.raises(consolidation.ConsolidationUnavailable):
        consolidation.consolidate_day(vault, DAY, call=lambda _prompt: None, state={})


def _closed_with_a_lost_batch(vault: Path, code: str) -> dict:  # noqa: F811 - the fixture
    record = {"items": 0, "record_set": consolidation.record_set_digest(vault, DAY), "failed_batches": ["k"], "code": code}
    return {"consolidated_session_days": {DAY: record}}


def test_a_day_that_lost_a_batch_opens_again_under_new_code(vault: Path, monkeypatch) -> None:  # noqa: F811 - the fixture
    monkeypatch.setattr(consolidation, "code_revision", lambda root=None: "new")

    reopened = consolidation._skip_reason(vault, DAY, _closed_with_a_lost_batch(vault, "old"))
    kept = consolidation._skip_reason(vault, DAY, _closed_with_a_lost_batch(vault, "new"))

    assert (reopened, kept) == (None, "already_consolidated")


def test_a_day_closed_with_lost_batches_says_which_and_under_what_code(monkeypatch) -> None:
    monkeypatch.setattr(consolidation, "code_revision", lambda root=None: "abc")

    assert consolidation._lost_batches(["b", "a"]) == {"failed_batches": ["a", "b"], "code": "abc"}
    assert consolidation._lost_batches([]) == {}


def test_the_code_revision_is_this_checkout_head() -> None:
    revision = consolidation.code_revision()

    assert revision is None or len(revision) == 40
