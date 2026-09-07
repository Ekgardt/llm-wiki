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


def _refused(db: Path, page: str, query: str, gate: str) -> None:
    telemetry.record_events(
        [
            telemetry.make_event(
                event_kind="evidence_read",
                query=query,
                retrieval_mode="base",
                candidate_id=page,
                rank=None,
                generation="grounded-answer",
                source_tool="grounded_qa",
                outcome=f"refused: {gate}",
            )
        ],
        db_path=db,
    )


def test_a_refusal_names_the_page_that_was_in_front_of_the_model(tmp_path: Path) -> None:
    """Until this was logged, the only way to know was to write it down by hand.

    That was done once, on 2026-09-02, and seven of eleven destroyed answers
    turned out to be correct — which is why the gates now apply per claim.
    """
    db = tmp_path / "t.sqlite3"
    _refused(db, "knowledge/notes/alpha.md", "q", "cited span states different figures")

    assert disposition.refused_counts(db_path=db) == {"knowledge/notes/alpha.md": 1}


def test_the_gate_that_refused_is_countable(tmp_path: Path) -> None:
    db = tmp_path / "t.sqlite3"
    _refused(db, "knowledge/notes/alpha.md", "q", "cited span states different figures")
    _refused(db, "knowledge/notes/beta.md", "q2", "claim cites evidence not supplied")

    gates = disposition.refusal_gates(db_path=db)

    assert set(gates) == {
        "cited span states different figures",
        "claim cites evidence not supplied",
    }


def test_a_refusal_is_not_a_citation_and_earns_no_boost(tmp_path: Path) -> None:
    """Being present when nothing survived is a place to look, not a merit."""
    db = tmp_path / "t.sqlite3"
    _refused(db, "knowledge/notes/alpha.md", "q", "some gate")

    assert disposition.cited_counts(db_path=db) == {}
    assert disposition.disposition(db_path=db) == {}


def test_the_reason_of_a_total_refusal_is_parsed_into_gates() -> None:
    import query_memory

    answer = {
        "reason": "no claim survived its citation gates: gate one; gate two; gate one"
    }

    assert query_memory._refused_gates(answer) == ["gate one", "gate two"]


def test_an_ordinary_abstention_is_not_a_gate_refusal() -> None:
    import query_memory

    assert query_memory._refused_gates({"reason": "The evidence does not say."}) == []
    assert query_memory._refused_gates({"reason": None}) == []


def test_the_disposition_multiplies_the_fused_score(monkeypatch) -> None:
    """Ranking reads the signal now; before 2026-09-06 nothing did.

    A page that carried answers before is raised, bounded and recorded by name
    on the candidate, so an ordering can still be explained factor by factor.
    """
    import retrieval

    monkeypatch.setattr(
        retrieval,
        "_standing_disposition",
        lambda query: {"knowledge/notes/alpha.md": 1.25},
    )
    scores = {"a": 1.0, "b": 1.0}
    meta = {
        "a": {"relative_path": "knowledge/notes/alpha.md"},
        "b": {"relative_path": "knowledge/notes/beta.md"},
    }

    weighted = retrieval._weigh_by_trust(scores, meta, curated_first=False, query="q")

    assert weighted["a"] > weighted["b"]
    assert meta["a"]["carried_weight"] == 1.25
    assert meta["b"]["carried_weight"] == 1.0


def test_a_search_still_works_when_there_is_no_history(monkeypatch) -> None:
    """A vault that has never answered anything must rank exactly as before."""
    import retrieval

    monkeypatch.setattr(retrieval, "_standing_disposition", lambda query: {})
    scores = {"a": 2.0}
    meta = {"a": {"relative_path": "knowledge/notes/alpha.md"}}

    weighted = retrieval._weigh_by_trust(scores, meta, curated_first=False, query="q")

    assert weighted["a"] == 2.0
    assert meta["a"]["carried_weight"] == 1.0
