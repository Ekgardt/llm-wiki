"""A long reply reaches the model as the sentences that bear on the question.

RECOMP's extractive compressor and Provence prune retrieved text to its
useful sentences with little loss. Here the dual encoder scores a reply's
sentences against the question; the top ones are delivered with their exact
byte ranges, a user turn is delivered whole, and a short reply is left alone.
See `docs/research/2026-09-08-small-keys-large-values-and-a-loop-that-stops.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import evidence_pruning  # noqa: E402
import query_memory  # noqa: E402
from context_budget import ContextBudget  # noqa: E402
from corpus_snapshot import collect_corpus  # noqa: E402

REPLY = (
    "**assistant:** Sure, here are some thoughts. Drums need fresh heads every year. "
    "A Pearl Export is a classic kit. Guitars like a humidifier. "
    "Pianos should be tuned twice a year. Let me know if you want more. "
    + "There is a great deal more one could say about instrument care in general. " * 12
    + "\n"
)
# Longer than the compiler's small-page threshold, so the reply is its own span.
FILLER = "## [09:00:00] session_end | earlier\n\n**user:** " + "We talked about the weather for a while. " * 60 + "\n\n"
CONTENT = (
    "# Day\n\n" + FILLER + "## [10:00:00] session_end | s\n\n**user:** How do I care for my instruments?\n\n" + REPLY
).encode()


def _about_drums(text: str) -> float:
    lowered = text.casefold()
    return float("drum" in lowered or "pearl" in lowered)


def _encode(texts, is_query):
    """A fake dual encoder: a sentence about drums points the way the question does."""
    if is_query:
        return np.asarray([[1.0, 1.0] for _ in texts], dtype=float)
    return np.asarray([[_about_drums(text), 1.0] for text in texts], dtype=float)


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


def _vault_with(tmp_path: Path) -> tuple[Path, str]:
    vault = tmp_path / "vault"
    (vault / "knowledge" / "daily").mkdir(parents=True)
    (vault / "knowledge" / "notes").mkdir(parents=True)
    daily = "knowledge/daily/2023-05-01.md"
    (vault / daily).write_bytes(CONTENT)
    return vault, daily


def _hashes_hold(evidence) -> bool:
    return all(
        hashlib.sha256(CONTENT[item.byte_start : item.byte_end]).hexdigest() == item.span_sha256
        for item in evidence
    )


def test_the_context_delivers_pruned_spans_that_still_hash(tmp_path: Path, monkeypatch) -> None:
    vault, daily = _vault_with(tmp_path)
    monkeypatch.setattr(query_memory, "_sentence_encoder", lambda: _encode)
    snapshot = collect_corpus(vault, code_roots=(), daily_paths=[daily])
    reply = next(chunk for chunk in snapshot.chunks if chunk.text.startswith("**assistant:**"))

    context = query_memory.build_grounded_context(
        snapshot,
        (reply,),
        vault=vault,
        profile="BASE",
        budget=ContextBudget(None, 65_536, 1200, 512),
        question="drum set care",
    )

    joined = "\n".join(item.text for item in context.evidence)
    assert "**user:** How do I care" in joined
    assert "Pearl Export" in joined and joined.count("a great deal more") < 12
    assert _hashes_hold(context.evidence)


def test_a_pruned_turn_stays_within_its_byte_budget() -> None:
    start = CONTENT.index(b"**assistant:**")

    spans = evidence_pruning.pruned_spans(CONTENT, start, len(CONTENT), "drum set care", _encode)

    kept = sum(e - s for s, e in spans)
    assert kept <= evidence_pruning.KEEP_BYTES + 120
    assert "Pearl Export" in " ".join(CONTENT[s:e].decode() for s, e in spans)
