"""Refitting the lane constants is one command over a run's own records.

Third audit, 2026-09-17 (retrieval M5, L1). The constants in `scripts/lane_score.py` were
fitted by probes that live in no repository, so nothing here could refit them; and the stand's
records did not carry the per-candidate lane matrix a refit needs. The refit itself waits for
a measurement run the owner must permit — this proves the command works on a synthetic file.
See `docs/research/2026-09-17-the-lane-score-refit-is-one-command.md`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT / "scripts", ROOT / "benchmark"):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

import fit_lane_score  # noqa: E402

pytest.importorskip("numpy")

TYPES = ("single-session-user", "single-session-assistant")


def _candidate(lexical: int | None, dense: int | None, user: bool, evidence: bool) -> dict:
    return {
        "lexical_rank": lexical,
        "dense_rank": dense,
        "rerank_score": None,
        "user_turn": user,
        "evidence": evidence,
    }


def _question(number: int, question_type: str, evidence_is_user: bool) -> dict:
    """One question whose evidence is the row the lexical lane ranked first.

    Every row has the dense lane silent, which is the degraded condition the
    shipped constants were never fitted under. The twenty-nine rows behind the
    evidence are of the opposite role, so with `USER_TURN = 2.5144` against
    `LEXICAL_RANK = -0.5071·ln(rank)` a user turn at any rank below ~142 outscores
    an assistant turn at rank 1 — the audit's arithmetic, on a file.
    """
    rows = [_candidate(1, None, evidence_is_user, True)]
    filler = [_candidate(rank, None, not evidence_is_user, False) for rank in range(2, 31)]
    return {
        "question_id": f"q{number:03d}",
        "question_type": question_type,
        "lane_matrix": rows + filler,
    }


def _run_file(path: Path, count: int = 30) -> Path:
    """A synthetic result file: both roles carry the evidence, in equal numbers."""
    questions = [
        _question(number, TYPES[number % 2], evidence_is_user=number % 2 == 0)
        for number in range(count)
    ]
    path.write_bytes(
        b"".join(json.dumps(item).encode("utf-8") + b"\n" for item in questions)
    )
    return path


def test_the_stand_records_the_rows_a_refit_needs() -> None:
    import longmemeval_vault

    question = {
        "haystack_sessions": [[{"content": "I sold the drum set last May.", "has_answer": True}]]
    }
    rows = [
        {"content": "**user:** I sold the drum set last May.", "bm25_rank": 1, "vector_rank": 2},
        {"content": "**assistant:** Noted.", "bm25_rank": 5, "vector_rank": None},
    ]

    matrix = longmemeval_vault.lane_matrix(question, rows)

    assert [row["evidence"] for row in matrix] == [True, False]
    assert [row["user_turn"] for row in matrix] == [True, False]
    assert [row["lexical_rank"] for row in matrix] == [1, 5]


def test_an_aggregated_report_is_refused_plainly_not_with_a_traceback(tmp_path: Path, capsys) -> None:
    """An operator will point this at a run's report; that file holds no records."""
    path = tmp_path / "report.json"
    path.write_bytes(b'{\n  "overall": {\n    "accuracy": 0.5\n  }\n}\n')

    assert fit_lane_score.main([str(path)]) == 1
    assert "lane matrix" in capsys.readouterr().err


def test_a_file_with_no_lane_matrix_is_refused_rather_than_fitted(tmp_path: Path, capsys) -> None:
    path = tmp_path / "old.jsonl"
    path.write_bytes(b'{"question_id": "q1", "question_type": "multi-session"}\n')

    assert fit_lane_score.main([str(path)]) == 1
    assert "lane matrix" in capsys.readouterr().err


def test_the_refit_reports_every_type_and_prints_pasteable_constants(tmp_path: Path, capsys) -> None:
    path = _run_file(tmp_path / "run.jsonl")

    assert fit_lane_score.main([str(path)]) == 0

    printed = capsys.readouterr().out
    assert all(name in printed for name in TYPES)
    assert all(f"{name} = " in printed for name in fit_lane_score.FEATURE_NAMES)


def test_a_json_array_is_read_like_the_runner_s_jsonl(tmp_path: Path) -> None:
    lines = _run_file(tmp_path / "run.jsonl")
    array = tmp_path / "run.json"
    records = [json.loads(line) for line in lines.read_text(encoding="utf-8").splitlines()]
    array.write_bytes(json.dumps(records).encode("utf-8"))

    assert len(fit_lane_score.questions([array])) == len(fit_lane_score.questions([lines]))


def test_the_shipped_constants_lose_the_assistant_evidence_on_a_degraded_run(
    tmp_path: Path,
) -> None:
    """The audit's arithmetic, reproduced: a user turn at rank 29 buries rank 1."""
    items = fit_lane_score.questions([_run_file(tmp_path / "run.jsonl")])

    shares = fit_lane_score.by_type(items, fit_lane_score.shipped_verdicts(items, 12))

    assert shares["single-session-user"] == (15, 1.0)
    assert shares["single-session-assistant"] == (15, 0.0)


def test_the_refit_recovers_the_type_the_shipped_constants_lost(tmp_path: Path) -> None:
    items = fit_lane_score.questions([_run_file(tmp_path / "run.jsonl")])

    shares = fit_lane_score.by_type(items, fit_lane_score.cross_validated(items, 12))

    assert shares["single-session-assistant"] == (15, 1.0)
    assert shares["single-session-user"] == (15, 1.0)


def test_the_folds_split_questions_and_never_one_question_s_candidates(tmp_path: Path) -> None:
    items = fit_lane_score.questions([_run_file(tmp_path / "run.jsonl")])

    folds = fit_lane_score._folds(items, fit_lane_score.FOLDS)
    ids = [item.question_id for fold in folds for item in fold]

    assert sorted(ids) == sorted(item.question_id for item in items)
    assert len(set(ids)) == len(ids)
