"""Readers of model output neither throw away a good reply nor take a failure for a result.

Research: `docs/research/2026-09-14-an-error-is-not-an-answer.md`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

PLAN = {"schema_version": "compile-plan/v2", "operations": []}
VERDICT = {"label": "compatible", "confidence": "high", "supported": True}


def test_a_plan_after_a_sentence_with_braces_is_the_plan():
    from compile_memory import _parse_json_object

    reply = "I checked the {slug} rules.\n" + json.dumps(PLAN) + "\nNote: nothing to {do}."

    assert _parse_json_object(reply, "operations") == PLAN


def test_a_draft_followed_by_a_correction_is_ambiguous_and_refused():
    """Two plans in one reply: guessing which was meant is refused since 2026-09-14.

    See `docs/research/2026-09-14-one-answer-or-none.md`.
    """
    import pytest
    from compile_memory import _parse_json_object

    draft = {**PLAN, "operations": [{"kind": "draft"}]}

    with pytest.raises(ValueError):
        _parse_json_object(json.dumps(draft) + "\nCorrected:\n" + json.dumps(PLAN), "operations")


def test_a_fenced_verdict_is_evaluated_not_quarantined():
    from contradiction_pipeline import _validated_evaluation_output

    assert _validated_evaluation_output("```json\n" + json.dumps(VERDICT) + "\n```") == VERDICT


def test_lint_says_when_the_contradiction_check_did_not_run():
    import lint_memory

    assert lint_memory._contradiction_findings(None) == [lint_memory.CONTRADICTIONS_NOT_RUN]


def test_lint_reads_numbered_findings_and_refuses_an_unreadable_reply():
    import lint_memory

    numbered = lint_memory._contradiction_findings("1. a.md vs b.md: dates differ\n* c vs d: rule")
    unreadable = lint_memory._contradiction_findings("I looked and I think NO_CONTRADICTIONS mostly")

    assert (numbered, unreadable) == (
        ["a.md vs b.md: dates differ", "c vs d: rule"],
        [lint_memory.CONTRADICTIONS_UNREADABLE],
    )


class _Store:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.noted: list[str] = []

    def add(self, turn, keys) -> None:
        self.added.append(turn.span_sha256)

    def note_asked(self, turns) -> None:
        self.noted.extend(turn.span_sha256 for turn in turns)


def test_a_turn_the_reply_did_not_cover_is_not_marked_keyed():
    import fact_keys

    turns = [fact_keys.Turn("d.md", 0, 1, name, "text") for name in ("said", "skipped")]
    store = _Store()

    keyed = fact_keys._key_batch(store, turns, {"said": []})

    assert (keyed, store.added, store.noted) == (1, ["said"], ["skipped"])
