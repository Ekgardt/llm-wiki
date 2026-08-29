"""`find_dead_code` costs what it claims, and a cut lands where doubt is.

Measured on this vault 2026-08-29, before the change: 873 candidates,
254,150 characters — 63,537 estimated tokens raw, 50,673 once the opaque ids
were gone. That is 2.0x the 25,000-token client tool-result ceiling
`answer_budget.MAX_BUDGET_TOKENS` documents, and MCP defines no truncation
signal, so the host cut it and the reader was never told.

The shape decided the fix as much as the size. Of the 873, 461 were
`zero_confirmed_incoming_calls` — nothing names the symbol anywhere — and 412
were `unresolved_receiver`, where a call site does name it and the receiver did
not resolve, so no deadness is claimed at all. Sorted by name, a 25,000-token
cut threw away 97 of the defensible rows to keep 162 of the doubtful ones.

Research: `docs/research/2026-08-29-a-default-budget-for-a-dead-code-answer.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import answer_budget  # noqa: E402
import code_graph  # noqa: E402

# Enough rows to carry the real answer past the ceiling; the live vault's 873
# cost 50,673 tokens, and these rows are the live shape.
_DEFENSIBLE = 461
_DOUBTFUL = 412

# `file` and `owner` vary the way the live answer's do. They used to be one
# constant string on every row, which was never true of a real answer -- the
# 2026-08-29 measurement found 93 distinct files and 59 distinct owners across
# 532 delivered rows -- and once `answer_budget` learned to state a constant
# column once, the fixture's fake constants compacted it under the ceiling and
# four tests stopped exercising the cut they exist to prove. The modulus keeps
# roughly the live ratio of one distinct file per six rows.
_DISTINCT_FILES = 93


def _candidate(index: int, reason: str) -> dict:
    module = f"module_{index % _DISTINCT_FILES:03d}"
    return {
        "name": f"_helper_number_{index:04d}",
        "symbol_id": f"code:node:{index:032x}",
        "owner": f"scripts.{module}",
        "file": f"/home/user/llm-wiki/scripts/{module}.py",
        "line": 3253 + index,
        # Genuinely constant on every live row, and hoisted out by
        # `answer_budget` for exactly that reason -- kept here so the fixture
        # still exercises the hoist.
        "status": "candidate",
        "reason": reason,
        "graph_complete": False,
    }


def _live_shaped_answer() -> dict:
    """The real answer shape, in the order `find_dead_code` now returns it."""
    rows = [_candidate(index, "unresolved_receiver") for index in range(_DOUBTFUL)]
    rows += [
        _candidate(_DOUBTFUL + index, "zero_confirmed_incoming_calls")
        for index in range(_DEFENSIBLE)
    ]
    return {
        "directory": "/home/user/llm-wiki",
        "candidates": code_graph._ordered_dead_candidates(rows),
        "source_generation": "generation-18cfd903a7a4e112-3ce112cb",
        "graph_complete": False,
        "unresolved_count": None,
        "fallback": False,
        **code_graph._dead_code_counts(rows),
    }


def _reasons(answer: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in answer["candidates"]:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return counts


# --- the default itself ----------------------------------------------------


def test_the_default_answer_fits_the_ceiling_the_client_actually_has():
    """Before: 50,673 tokens with no budget applied, cut by the host in silence."""
    answer = _live_shaped_answer()
    assert answer_budget.estimate_tokens(answer) > answer_budget.MAX_BUDGET_TOKENS
    shaped = answer_budget.shape_code_answer(answer)
    assert answer_budget.estimate_tokens(shaped) <= answer_budget.MAX_BUDGET_TOKENS


def test_a_cut_answer_says_it_was_cut_and_by_how_much():
    """A silent cut is worse than a large answer; the reader is told."""
    shaped = answer_budget.shape_code_answer(_live_shaped_answer())
    report = shaped["answer_budget"]
    assert report["truncated"] is True
    assert report["rows_omitted"] == _DEFENSIBLE + _DOUBTFUL - len(shaped["candidates"])
    assert report["rows_omitted"] > 0
    assert report["budget_tokens"] == answer_budget.DEFAULT_BUDGET_TOKENS


def test_an_answer_under_the_default_is_returned_as_it_was():
    """The default must not tax an answer that never needed cutting.

    The opaque-identifier note stays — it is the pre-existing behaviour and it
    reports a real drop. What must not appear is budget accounting: a caller who
    asked for no budget is not billed a `budget_tokens` block for an answer that
    was never at risk.
    """
    small = {"directory": ".", "candidates": [_candidate(0, "unresolved_receiver")]}
    shaped = answer_budget.shape_code_answer(small)
    assert shaped["answer_budget"] == {"omitted_fields": ["symbol_id"]}
    assert len(shaped["candidates"]) == 1


def test_an_answer_with_nothing_at_all_to_drop_stays_byte_identical():
    plain = {"status": "error", "mode": "summary", "error": "row ceiling exceeded"}
    assert answer_budget.shape_code_answer(plain) == plain


# --- what the cut is allowed to take ---------------------------------------


def test_the_cut_takes_the_doubtful_rows_and_leaves_every_defensible_one():
    """`unresolved_receiver` claims nothing, so it is what a budget drops first.

    Sorted by name, the same cut lost 97 defensible candidates.
    """
    shaped = answer_budget.shape_code_answer(_live_shaped_answer())
    kept = _reasons(shaped)
    assert kept["zero_confirmed_incoming_calls"] == _DEFENSIBLE
    assert kept["unresolved_receiver"] < _DOUBTFUL


def test_the_defensible_candidates_come_first_so_a_tail_cut_is_survivable():
    reasons = [row["reason"] for row in _live_shaped_answer()["candidates"]]
    assert reasons[:_DEFENSIBLE] == ["zero_confirmed_incoming_calls"] * _DEFENSIBLE
    assert set(reasons[_DEFENSIBLE:]) == {"unresolved_receiver"}


def test_an_unrecognised_reason_sorts_last_rather_than_ahead_of_a_known_one():
    """A reason added later must not silently outrank the defensible one."""
    rows = [
        _candidate(1, "some_future_reason"),
        _candidate(2, "zero_confirmed_incoming_calls"),
    ]
    ordered = code_graph._ordered_dead_candidates(rows)
    assert [row["reason"] for row in ordered] == [
        "zero_confirmed_incoming_calls",
        "some_future_reason",
    ]


# --- the totals a cut must not distort --------------------------------------


def test_the_counts_survive_the_cut_that_removes_the_rows():
    """354 rows can go; "873 candidates, 412 of them doubtful" may not."""
    shaped = answer_budget.shape_code_answer(_live_shaped_answer())
    assert shaped["candidate_count"] == _DEFENSIBLE + _DOUBTFUL
    assert shaped["candidates_by_reason"] == {
        "unresolved_receiver": _DOUBTFUL,
        "zero_confirmed_incoming_calls": _DEFENSIBLE,
    }
    assert len(shaped["candidates"]) < shaped["candidate_count"]


def test_the_counts_describe_the_candidates_not_the_rows_that_fit():
    counts = code_graph._dead_code_counts(
        [_candidate(0, "unresolved_receiver"), _candidate(1, "unresolved_receiver")]
    )
    assert counts == {
        "candidate_count": 2,
        "candidates_by_reason": {"unresolved_receiver": 2},
    }


# --- the caller's own budget still wins -------------------------------------


def test_an_explicit_budget_is_still_obeyed_below_the_default():
    shaped = answer_budget.shape_code_answer(
        _live_shaped_answer(), budget_tokens=8_000
    )
    assert answer_budget.estimate_tokens(shaped) <= 8_000
    assert shaped["answer_budget"]["budget_tokens"] == 8_000
