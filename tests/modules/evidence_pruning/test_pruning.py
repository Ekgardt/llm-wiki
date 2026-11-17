"""Original sentence scoring, byte-range and turn-budget regressions."""
from __future__ import annotations

import evidence_pruning

from tests.modules.evidence_pruning.pruning_helpers import CONTENT, _encode


def test_sentence_spans_cover_the_text_without_cutting_a_character() -> None:
    start = CONTENT.index(b"**assistant:**")
    spans = evidence_pruning.sentence_spans(CONTENT, start, len(CONTENT))

    assert len(spans) == 18
    assert all(CONTENT[s:e].decode("utf-8") for s, e in spans)


def test_a_long_reply_keeps_its_marker_and_the_sentences_that_score(monkeypatch) -> None:
    start = CONTENT.index(b"**assistant:**")

    spans = evidence_pruning.pruned_spans(CONTENT, start, len(CONTENT), "drum set care", _encode)
    kept = " ".join(CONTENT[s:e].decode() for s, e in spans)

    assert kept.startswith("**assistant:**")
    assert "Drums need fresh heads" in kept and "Pearl Export" in kept
    # The filler that scores nothing is what the budget leaves out.
    assert kept.count("a great deal more") < 12


def test_a_short_turn_is_not_pruned_and_a_long_one_of_either_side_is() -> None:
    user_start = CONTENT.index(b"**user:** How do I care")
    reply_start = CONTENT.index(b"**assistant:**")
    short = CONTENT[:reply_start] + b"**assistant:** Yes. No. Maybe.\n"
    long_user = b"**user:** " + b"I went to the market and bought a great many things. " * 20

    assert not evidence_pruning.prunes(CONTENT, user_start, reply_start)
    assert not evidence_pruning.prunes(short, reply_start, len(short))
    assert evidence_pruning.prunes(CONTENT, reply_start, len(CONTENT))
    assert evidence_pruning.prunes(long_user, 0, len(long_user))
    assert not evidence_pruning.prunes(b"Plain note text. " * 60, 0, 1020)


def test_a_pruned_turn_stays_within_its_byte_budget() -> None:
    start = CONTENT.index(b"**assistant:**")

    spans = evidence_pruning.pruned_spans(CONTENT, start, len(CONTENT), "drum set care", _encode)

    kept = sum(e - s for s, e in spans)
    assert kept <= evidence_pruning.KEEP_BYTES + 120
    assert "Pearl Export" in " ".join(CONTENT[s:e].decode() for s, e in spans)
