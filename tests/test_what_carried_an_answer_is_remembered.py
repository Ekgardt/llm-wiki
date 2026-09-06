"""We logged 14 806 impressions and never once recorded which was useful.

The telemetry records what was *shown* — candidate, rank, query, mode — and its
`outcome` column was null on every row. The RAG literature treats that missing
half as the hard problem: these systems return a synthesised answer instead of
links, so the data that would say which document helped is never produced, and
where feedback exists it is clicks, biased by position and needing propensity
models to be usable.

We are not in that situation. A grounded answer names its evidence: every
published claim carries citation ids, and those are exactly the spans that
carried it past the gates. That is a verified statement, by the system that used
the evidence, that this page did the work.

Nothing here trains or fits. It counts, and it is bounded — which is what
trained immunity is: not a memory of the encounter, a readiness afterwards.

See `docs/research/2026-09-06-a-signal-stronger-than-a-click.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import retrieval_disposition as disposition  # noqa: E402
import retrieval_telemetry as telemetry  # noqa: E402


def _cited(db: Path, page: str, query: str, times: int = 1) -> None:
    events = [
        telemetry.make_event(
            event_kind="evidence_read",
            query=query,
            retrieval_mode="base",
            candidate_id=page,
            rank=None,
            generation="grounded-answer",
            source_tool="grounded_qa",
            outcome="cited",
        )
        for _ in range(times)
    ]
    telemetry.record_events(events, db_path=db)


def _shown(db: Path, page: str, query: str) -> None:
    telemetry.record_events(
        [
            telemetry.make_event(
                event_kind="impression",
                query=query,
                retrieval_mode="base",
                candidate_id=page,
                rank=1,
                generation="g1",
                source_tool="search_memory",
            )
        ],
        db_path=db,
    )


def test_a_page_that_carried_an_answer_is_counted(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "how does undo work")

    assert disposition.cited_counts(db_path=db) == {"knowledge/notes/alpha.md": 1}


def test_being_shown_is_not_being_useful(tmp_path: Path) -> None:
    """The whole point: an impression is half a fact."""
    db = tmp_path / "t.sqlite3"
    _shown(db, "knowledge/notes/beta.md", "how does undo work")

    assert disposition.cited_counts(db_path=db) == {}


def test_the_same_question_counts_only_its_own_answers(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "how does undo work")
    _cited(db, "knowledge/notes/beta.md", "where do snapshots live")

    counts = disposition.cited_counts("how does undo work", db_path=db)

    assert counts == {"knowledge/notes/alpha.md": 1}


def test_without_a_question_every_answer_counts(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "how does undo work")
    _cited(db, "knowledge/notes/beta.md", "where do snapshots live")

    assert len(disposition.cited_counts(db_path=db)) == 2


def test_the_boost_is_bounded_however_often_a_page_is_cited(tmp_path: Path) -> None:
    """A page useful once must not become permanently privileged."""
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "q", times=50)

    factor = disposition.disposition(db_path=db)["knowledge/notes/alpha.md"]

    assert factor == 1.0 + disposition.MAX_DISPOSITION_BOOST


def test_one_citation_earns_less_than_the_ceiling(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "q")

    factor = disposition.disposition(db_path=db)["knowledge/notes/alpha.md"]

    assert 1.0 < factor < 1.0 + disposition.MAX_DISPOSITION_BOOST


def test_a_page_nothing_knows_about_gets_nothing_rather_than_a_penalty(tmp_path: Path) -> None:
    """Absence of evidence is not evidence, so it must not read as a demotion."""
    db = tmp_path / "t.sqlite3"
    _cited(db, "knowledge/notes/alpha.md", "q")

    assert "knowledge/notes/unseen.md" not in disposition.disposition(db_path=db)


def test_an_absent_database_is_silence_not_a_failure(tmp_path: Path) -> None:
    assert disposition.cited_counts(db_path=tmp_path / "missing.sqlite3") == {}
    assert disposition.disposition(db_path=tmp_path / "missing.sqlite3") == {}
