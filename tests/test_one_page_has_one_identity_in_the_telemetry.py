"""Every retrieval event names a page the same way, and a page's window is the contract's.

Third audit, 2026-09-17 (retrieval L13 and L12). `retrieval_events` has one `candidate_id`
column and three writers filled it three ways — a chunk hash, a bare stem, a vault-relative
path — while `retrieval_disposition` joins on the path and `access_tracking` read the stem. So
a generation-mode impression never reached a page's access count. The decay that decides
whether an old page is kept also ran on its own copy of the archive windows, and called
`exp(-d/h)` a half-life.
See `docs/research/2026-09-17-one-page-one-identity-and-one-set-of-windows.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import access_tracking  # noqa: E402
import retrieval  # noqa: E402

PAGE = "knowledge/notes/auth.md"


def _shown(**extra) -> dict:
    row = {"path": PAGE, "effective_mode": "generation", "generation": "g1"}
    row.update(extra)
    return row


def _recorded(monkeypatch, rows: list[dict]) -> list:
    """The events the product's impression path would write."""
    written: list = []
    import retrieval_telemetry

    monkeypatch.setattr(
        retrieval_telemetry, "best_effort_record_events", lambda events: written.extend(events)
    )
    retrieval._record_impressions(
        rows, query="who signs in", corpus_generation="g1", source_tool="search_memory"
    )
    return written


def test_a_generation_impression_names_the_page_not_the_chunk(monkeypatch) -> None:
    written = _recorded(monkeypatch, [_shown(chunk_id="9f" * 32)])

    assert [event.candidate_id for event in written] == [PAGE]


def test_a_row_that_carries_a_slug_still_names_the_page(monkeypatch) -> None:
    """A stem is not an identity: two projects each hold a `state.md`."""
    written = _recorded(monkeypatch, [_shown(slug="auth")])

    assert [event.candidate_id for event in written] == [PAGE]


def test_the_adapter_records_the_page_the_flush_reads_back() -> None:
    assert access_tracking.page_identity("auth") == PAGE


@pytest.mark.parametrize(
    ("page_type", "days"),
    [("debugging", 60), ("gap", 90), ("pattern", 180), ("qa", 365)],
)
def test_a_page_decays_by_half_at_the_window_the_contract_states(page_type, days) -> None:
    """CLAUDE.md and okf_types hold the windows; this is the third reader of that one set."""
    from okf_types import TYPE_AGE_DAYS

    assert TYPE_AGE_DAYS[page_type] == days
    assert access_tracking._half_life_days(page_type) == float(days)
    assert access_tracking._decayed(1.0, days, float(days)) == pytest.approx(0.5)


def test_a_type_that_never_archives_never_decays() -> None:
    kept = [access_tracking._half_life_days(name) for name in ("decision", "concept", "entity")]

    assert kept == [None, None, None]
    assert access_tracking._decayed(0.7, 10_000, None) == 0.7
