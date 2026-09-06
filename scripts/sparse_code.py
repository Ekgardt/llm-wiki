#!/usr/bin/env python3
"""Expand and sparsify: the fly's trick, as a comparable code.

The fly's olfactory circuit normalises its input, fans it out through a *sparse
binary random* matrix into a much higher dimension — about 50 inputs to 2000
Kenyon cells — and then lets feedback inhibition silence all but the highest
firing five per cent. Published as an algorithm in Science in 2017 and known as
FlyHash, it is locality-sensitive hashing run backwards: a long sparse code
instead of a short dense one, and it beats classical LSH by mean average
precision on three datasets, most where the code is short.

Two things here want it. The compile classifier returned FLUSH_OK on **50 of 50**
sessions while the vault's own diagnostics say it may be losing signal; the same
circuit was published separately as a data structure for novelty detection, which
is the second opinion that classifier has never had. And retrieval compares one
384-dimensional embedding by cosine, where a long sparse code is a different and
cheaper trade.

Nothing here trains. The projection is drawn once from a pinned seed and never
learned — which is why the seed is part of the format: a code is only comparable
to codes drawn from the same matrix.

See `docs/research/2026-09-06-expand-and-sparsify.md`.
"""

from __future__ import annotations

# The embedding this vault already computes, from intfloat/multilingual-e5-small.
INPUT_DIMENSIONS = 384

# The fly's ratio is about forty; published follow-ups use ten and upward. Twenty
# keeps a code inside a few kilobytes while the expansion stays clearly
# super-linear.
EXPANSION = 20
CODE_DIMENSIONS = INPUT_DIMENSIONS * EXPANSION

# The fly's own figure, kept by every follow-up: 5% of 7680 is 384 active
# positions, the same count as the input has dimensions.
SPARSITY = 0.05
ACTIVE = int(CODE_DIMENSIONS * SPARSITY)

# How many inputs each projection row samples. Sparse on purpose: the biology's
# connections are sparse, and a dense matrix would cost the expansion in time
# what it gains in dimension.
SAMPLES_PER_ROW = 12

# Drawn once. Changing this invalidates every stored code, so it is a format
# version and not a tuning knob.
PROJECTION_SEED = 20260906


def _projection():
    """The sparse binary matrix, built once per process from the pinned seed."""
    import numpy

    generator = numpy.random.default_rng(PROJECTION_SEED)
    matrix = numpy.zeros((CODE_DIMENSIONS, INPUT_DIMENSIONS), dtype=numpy.float32)
    for row in range(CODE_DIMENSIONS):
        columns = generator.choice(INPUT_DIMENSIONS, SAMPLES_PER_ROW, replace=False)
        matrix[row, columns] = 1.0
    return matrix


_MATRIX = None


def projection():
    global _MATRIX
    if _MATRIX is None:
        _MATRIX = _projection()
    return _MATRIX


def _normalised(vector):
    """Divisive normalisation: the pattern carries the signal, not the volume.

    The fly divides by the mean, which works on firing rates because they are
    non-negative and their mean is a real intensity. An embedding is signed and
    close to zero-mean, so dividing by it amplifies noise without bound —
    measured here on 2026-09-06, a nudge of 0.05 on a unit-normal vector changed
    every one of the 384 active positions. Centring and then dividing by the
    Euclidean norm is the same idea on the input we actually have: the direction
    survives, the scale does not.
    """
    import numpy

    values = numpy.asarray(vector, dtype=numpy.float32).reshape(-1)
    if values.size != INPUT_DIMENSIONS:
        raise ValueError(f"expected {INPUT_DIMENSIONS} dimensions, got {values.size}")
    centred = values - float(values.mean())
    norm = float(numpy.linalg.norm(centred))
    if norm == 0.0:
        return centred
    return centred / norm


def code(vector) -> tuple[int, ...]:
    """The active positions of one embedding's sparse code, sorted.

    Returned as positions rather than a dense array because 96% of the array is
    zero and the positions are what every comparison uses.
    """
    import numpy

    activated = projection() @ _normalised(vector)
    winners = numpy.argpartition(activated, -ACTIVE)[-ACTIVE:]
    return tuple(sorted(int(index) for index in winners))


def overlap(first: tuple[int, ...], second: tuple[int, ...]) -> float:
    """How much two codes share, from 0 for nothing to 1 for the same code."""
    if not first or not second:
        return 0.0
    shared = len(set(first) & set(second))
    return shared / float(max(len(first), len(second)))


def novelty(candidate: tuple[int, ...], known) -> float:
    """How unlike everything already known this is, from 0 to 1.

    **Read this relatively, never against a fixed threshold.** Measured on this
    vault 2026-09-06: 149 pairs of unrelated notes have cosine between 0.767 and
    0.923, median 0.842 — the embedding space `intfloat/multilingual-e5-small`
    produces is compressed into a narrow band, which is normal for this family
    and not a defect. The E5 prefixes are applied correctly; the model simply
    puts everything close together and carries its signal in the *ordering*.

    A code inherits that: overlaps are ordered faithfully, so "more novel than
    that one" is sound, while "novelty above 0.4" means nothing at all. The
    first time this was ignored, an absolute cut at 0.6 declared 23 209 pairs of
    unrelated pages to be near-duplicates.

    Nothing known makes everything novel, which is the honest answer for an
    empty memory rather than a refusal.
    """
    best = max((overlap(candidate, other) for other in known), default=0.0)
    return 1.0 - best
