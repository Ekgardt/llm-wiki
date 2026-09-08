"""Prune a reply to the sentences that bear on the question, byte-exact.

RECOMP (arXiv:2310.04408): an extractive compressor that keeps the useful
sentences of retrieved text gives up to 10x compression with minimal loss,
and beats token pruning. Provence (arXiv:2501.16214, ICLR 2025): a
sentence-level pruner that keeps from none to all of a passage's sentences.

On captured conversations the reply is 80% of the bytes and the fact is in
the user's turn, so a user turn is delivered whole and an assistant turn is
delivered as its sentences that score highest against the question, by the
dual encoder retrieval already runs — RECOMP's extractive compressor is a
dual encoder too. Every kept sentence keeps its byte range, so the citation
gates hash and check it exactly as they did the whole turn.
See `docs/research/2026-09-08-small-keys-large-values-and-a-loop-that-stops.md`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import numpy as np

PRUNED_PREFIX = b"**assistant:**"
# A reply with this many sentences or fewer is delivered whole; pruning a short
# reply saves nothing and risks the one sentence that mattered.
PRUNE_ABOVE_SENTENCES = 4
# How many sentences of a long reply reach the model.
KEEP_SENTENCES = 3
# Sentence ends are ASCII, so a cut never lands inside a UTF-8 character.
_BOUNDARY = re.compile(rb"(?<=[.!?])[ \t]+|\n+")

Encoder = Callable[[Sequence[str], bool], np.ndarray]


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


def prunes(content: bytes, start: int, end: int) -> bool:
    """Only a reply long enough to be worth pruning."""
    if not content[start:end].lstrip().startswith(PRUNED_PREFIX):
        return False
    return len(sentence_spans(content, start, end)) > PRUNE_ABOVE_SENTENCES


def _scores(question: str, sentences: Sequence[str], encode: Encoder) -> np.ndarray:
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
    ranked = np.argsort(-_scores(question, texts, encode), kind="stable")
    chosen = {0, *(int(index) for index in ranked[:KEEP_SENTENCES])}
    return _merged([spans[index] for index in sorted(chosen)])
