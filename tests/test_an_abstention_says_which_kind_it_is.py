"""An abstention meant two opposite things and the report never said which.

Measured 2026-08-28 at n=50: 26 of 50 answers were `insufficient_evidence`,
and accuracy when the system does answer is 14 of 18 = 0.78, so refusal binds
the LongMemEval score rather than error. But an abstention with a labelled
answer session among the retrieved candidates is a calibration failure — the
evidence was in front of the answerer and it declined — while one without is
retrieval doing the refusing, and the abstention was correct. The two need
opposite work, and choosing between them without this split is guessing.

The evidence test is the dataset's own `answer_session_ids`, not a search for
the gold string: a gold answer is often a word like `2018` and a substring
search over evidence would confirm itself.

See `docs/research/2026-08-29-where-we-stand-against-the-field.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import longmemeval_score  # noqa: E402
import longmemeval_vault  # noqa: E402


def _question(*sessions: str) -> dict:
    return {"answer_session_ids": list(sessions)}


def _row(**fields) -> dict:
    row = {"summary": "", "title": "", "path": "", "heading_ancestry": []}
    row.update(fields)
    return row


def test_a_candidate_from_a_labelled_session_is_counted() -> None:
    rows = [_row(heading_ancestry=["[02:21] session_end | s-answer"])]

    assert longmemeval_vault.answer_sessions_retrieved(_question("s-answer"), rows) == 1


def test_a_candidate_from_another_session_is_not() -> None:
    rows = [_row(heading_ancestry=["[02:21] session_end | s-other"])]

    assert longmemeval_vault.answer_sessions_retrieved(_question("s-answer"), rows) == 0


def test_the_session_is_found_wherever_the_chunk_carries_it() -> None:
    """Path, title or heading — the id travels differently by retrieval leg."""
    by_path = [_row(path="knowledge/daily/2023-05-20.md#s-answer")]
    by_summary = [_row(summary="... session_end | s-answer ...")]

    question = _question("s-answer")
    assert longmemeval_vault.answer_sessions_retrieved(question, by_path) == 1
    assert longmemeval_vault.answer_sessions_retrieved(question, by_summary) == 1


def test_a_question_with_no_labelled_session_counts_nothing() -> None:
    """Abstention questions have no answer session; they must not be miscounted."""
    rows = [_row(summary="anything at all")]

    assert longmemeval_vault.answer_sessions_retrieved(_question(), rows) == 0


def test_an_abstention_with_the_answer_retrieved_is_a_calibration_failure() -> None:
    rows = [
        {
            "status": "insufficient_evidence",
            "answer_sessions_retrieved": 2,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained"] == 1
    assert report["abstained_with_answer_retrieved"] == 1
    assert report["abstained_without_answer_retrieved"] == 0


def test_an_abstention_with_nothing_retrieved_is_retrieval_refusing() -> None:
    rows = [
        {
            "status": "insufficient_evidence",
            "answer_sessions_retrieved": 0,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained_with_answer_retrieved"] == 0
    assert report["abstained_without_answer_retrieved"] == 1


def test_an_answered_question_is_not_an_abstention() -> None:
    rows = [
        {
            "status": "answered",
            "answer_sessions_retrieved": 1,
            "gold": "x",
            "hypothesis": "the answer is x",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["abstained"] == 0
    assert report["answer_retrieved"] == 1


def test_a_provider_failure_is_not_counted_as_an_abstention() -> None:
    """Nothing was produced, so nothing declined; it is already held out."""
    rows = [
        {
            "status": "error",
            "error": "boom",
            "error_kind": "provider_no_response",
            "answer_sessions_retrieved": 1,
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["provider_failures"] == 1
    assert report["abstained"] == 0


def test_a_harness_failure_is_not_a_wrong_answer() -> None:
    """A run that never reached the product reported `accuracy 0.0` for all 50.

    Measured 2026-08-29: a `--workdir` that did not exist failed every question
    in about two seconds on a `mkdtemp` traceback, and the report read
    `accuracy 0.0` with `provider_failures 0` in every category — a harness
    that never started, presented as a memory system that answered everything
    wrongly.
    """
    rows = [
        {
            "status": "error",
            "error": "worker rc=1",
            "error_kind": "harness_failure",
            "gold": "x",
            "hypothesis": "",
        }
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["scored"] == 0
    assert report["accuracy"] is None
    assert report["harness_failures"] == 1
    assert report["provider_failures"] == 0


def test_a_provider_failure_and_a_harness_failure_are_counted_apart() -> None:
    """One says the provider did not answer; the other says nothing was asked."""
    rows = [
        {"error_kind": "harness_failure", "status": "error", "error": "e", "gold": "x"},
        {
            "error_kind": "provider_no_response",
            "status": "error",
            "error": "e",
            "gold": "x",
        },
    ]

    report = longmemeval_score.aggregate(rows)["overall"]

    assert report["harness_failures"] == 1
    assert report["provider_failures"] == 1
    assert report["scored"] == 0


def test_a_missing_workdir_is_created_rather_than_failing_every_question(
    tmp_path,
) -> None:
    requested = tmp_path / "absent" / "nested"

    prepared = longmemeval_vault._prepared_workdir(str(requested))

    assert Path(prepared).is_dir()
    assert Path(prepared) == requested.resolve()


def test_no_workdir_stays_no_workdir() -> None:
    """The default is the system temp directory; that must not change."""
    assert longmemeval_vault._prepared_workdir(None) is None


def test_the_gold_text_is_found_in_a_candidate() -> None:
    """The span signal, separate from the session one: was the answer text there?"""
    question = {"answer": "Business Administration"}
    rows = [_row(summary="...user said Business Administration was the major...")]

    assert longmemeval_vault.gold_in_candidates(question, rows) is True


def test_the_gold_text_absent_from_every_candidate_is_reported() -> None:
    """Right session, wrong span: what 18 of today's evidenced refusals look like."""
    question = {"answer": "Business Administration"}
    rows = [_row(summary="...the user discussed autographed baseballs...")]

    assert longmemeval_vault.gold_in_candidates(question, rows) is False


def test_a_question_with_no_gold_answer_matches_nothing() -> None:
    assert longmemeval_vault.gold_in_candidates({"answer": ""}, [_row()]) is False


def test_the_gold_match_ignores_case_and_spacing() -> None:
    """Gold answers carry the dataset's own formatting, not the transcript's."""
    question = {"answer": "  Business   Administration "}
    rows = [_row(summary="major: business administration")]

    assert longmemeval_vault.gold_in_candidates(question, rows) is True
