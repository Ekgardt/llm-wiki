"""Prune a reply to the sentences that bear on the question, byte-exact.

RECOMP (arXiv:2310.04408): an extractive compressor that keeps the useful
sentences of retrieved text gives up to 10x compression with minimal loss,
and beats token pruning. Provence (arXiv:2501.16214, ICLR 2025): a
sentence-level pruner that keeps from none to all of a passage's sentences.

A turn of more than a few sentences — a reply, or a long user message — is
delivered as its sentences that score highest against the question, by the
dual encoder retrieval already runs; RECOMP's extractive compressor is a
dual encoder too. A short turn is delivered whole. Measured 2026-09-08 on
a LongMemEval count with replies alone pruned: 65 KB of prompt, because the
user's turns there run to a kilobyte each. Every kept sentence keeps its
byte range, so the citation gates hash and check it exactly as they did the
whole turn.
See `docs/research/2026-09-08-small-keys-large-values-and-a-loop-that-stops.md`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


TURN_PREFIXES = (b"**user:**", b"**assistant:**")
# A turn no longer than this is delivered whole; pruning a short turn saves
# nothing and risks the one sentence that mattered.
PRUNE_ABOVE_BYTES = 800
# How much of a long turn reaches the model: its first sentence, then the
# best-scoring sentences until this many bytes are spent. RECOMP's extractive
# compressor works to a length budget too.
KEEP_BYTES = 600
# Sentence ends are ASCII, so a cut never lands inside a UTF-8 character.
_BOUNDARY = re.compile(rb"(?<=[.!?])[ \t]+|\n+")

# numpy (the hybrid profile) is imported only where sentences are scored: an encoder
# exists only with that profile. See `docs/research/2026-09-14-a-base-install-can-search.md`.
Encoder = Callable[[Sequence[str], bool], "np.ndarray"]


def sentence_spans(content: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """Byte ranges of the sentences in content[start:end], each non-empty."""
    spans: list[tuple[int, int]] = []
    cursor = start
    for boundary in _BOUNDARY.finditer(content, start, end):
        if content[cursor : boundary.start()].strip():
            spans.append((cursor, boundary.start()))
        cursor = boundary.end()
    if content[cursor:end].strip():
        spans.append((cursor, end))
    return spans


def _holds_a_turn(piece: bytes) -> bool:
    """Whether a turn marker opens any line of this piece.

    Asking whether the piece *starts* with one exempted the first chunk of every
    entry, because an entry starts with its heading — so the first turn of a
    file, often the user's longest message, was delivered whole however long it
    was. See `docs/research/2026-09-18-the-context-is-built-under-the-same-clock.md`.
    """
    return any(line.lstrip().startswith(TURN_PREFIXES) for line in piece.splitlines())


def prunes(content: bytes, start: int, end: int) -> bool:
    """Only a turn of a conversation, and only one long enough to be worth pruning."""
    if not _holds_a_turn(content[start:end]):
        return False
    if end - start <= PRUNE_ABOVE_BYTES:
        return False
    return len(sentence_spans(content, start, end)) > 1


def _scores(question: str, sentences: Sequence[str], encode: Encoder) -> np.ndarray:
    import numpy as np

    query = np.asarray(encode([question], True), dtype=float)[0]
    passages = np.asarray(encode(sentences, False), dtype=float)
    norms = np.linalg.norm(passages, axis=1) * (np.linalg.norm(query) or 1.0)
    return passages @ query / np.where(norms == 0, 1.0, norms)


def _merged(kept: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Adjacent kept sentences as one span, in byte order."""
    merged: list[tuple[int, int]] = []
    for span in sorted(kept):
        if merged and merged[-1][1] >= span[0] - 2:
            merged[-1] = (merged[-1][0], span[1])
            continue
        merged.append(span)
    return merged


def pruned_spans(
    content: bytes, start: int, end: int, question: str, encode: Encoder
) -> list[tuple[int, int]]:
    """The byte ranges of the reply worth reading for this question, in order.

    The first sentence carries the speaker marker and stays, so the reader
    knows who is talking.
    """
    spans = sentence_spans(content, start, end)
    texts = [content[s:e].decode("utf-8", errors="replace") for s, e in spans]
    ranked = _ranked(_scores(question, texts, encode))
    chosen = _within_budget(spans, [0, *ranked])
    return _merged([spans[index] for index in sorted(chosen)])


def _ranked(scores: np.ndarray) -> list[int]:
    """Sentence indices, best score first; ties keep their order."""
    import numpy as np

    return [int(index) for index in np.argsort(-scores, kind="stable")]


# The first sentence carries the speaker marker and is kept before anything is
# scored, so a first sentence of KEEP_BYTES or more used to spend the whole
# budget and deliver nothing the question itself chose. At least this many
# sentences are kept whatever they cost.
MIN_SENTENCES = 2


def _within_budget(spans: Sequence[tuple[int, int]], order: Sequence[int]) -> set[int]:
    """The first sentence and then the best ones, until KEEP_BYTES are spent."""
    chosen: set[int] = set()
    spent = 0
    for index in order:
        if index in chosen:
            continue
        chosen.add(index)
        spent += spans[index][1] - spans[index][0]
        if _budget_spent(spent, len(chosen)):
            break
    return chosen


def _budget_spent(spent: int, kept: int) -> bool:
    if kept < MIN_SENTENCES:
        return False
    return spent >= KEEP_BYTES
