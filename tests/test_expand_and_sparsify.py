"""The fly's trick: normalise, fan out, keep the loudest five per cent.

Published in Science in 2017 as FlyHash — locality-sensitive hashing run
backwards, producing a long sparse code instead of a short dense one, and
beating classical LSH by mean average precision where the code is short.

What is tested here is the property the whole thing rests on: similar inputs
must produce overlapping codes and dissimilar inputs must not. Everything else —
whether it helps retrieval, whether novelty improves the compile classifier — is
a measurement, not a test.

See `docs/research/2026-09-06-expand-and-sparsify.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import sparse_code  # noqa: E402

numpy = pytest.importorskip("numpy")


def _vector(seed: int):
    return numpy.random.default_rng(seed).normal(size=sparse_code.INPUT_DIMENSIONS)


def test_a_code_is_sparse_and_of_the_declared_size() -> None:
    positions = sparse_code.code(_vector(1))

    assert len(positions) == sparse_code.ACTIVE
    assert len(set(positions)) == len(positions)
    assert max(positions) < sparse_code.CODE_DIMENSIONS


def test_the_same_input_gives_the_same_code() -> None:
    """The projection is pinned, so a code is comparable across processes."""
    assert sparse_code.code(_vector(2)) == sparse_code.code(_vector(2))


def test_a_nearly_identical_input_overlaps_strongly() -> None:
    base = _vector(3)
    nudged = base + numpy.random.default_rng(99).normal(scale=0.01, size=base.size)

    assert sparse_code.overlap(sparse_code.code(base), sparse_code.code(nudged)) > 0.5


def test_an_unrelated_input_overlaps_weakly() -> None:
    first = sparse_code.code(_vector(4))
    second = sparse_code.code(_vector(5))

    assert sparse_code.overlap(first, second) < 0.2


def test_similarity_is_ordered_not_merely_present() -> None:
    """The property that makes it a hash: closer inputs, larger overlap."""
    base = _vector(6)
    near = base + numpy.random.default_rng(7).normal(scale=0.05, size=base.size)
    far = base + numpy.random.default_rng(8).normal(scale=2.0, size=base.size)

    code_base = sparse_code.code(base)

    assert sparse_code.overlap(code_base, sparse_code.code(near)) > sparse_code.overlap(
        code_base, sparse_code.code(far)
    )


def test_scaling_the_input_does_not_change_the_code() -> None:
    """Divisive normalisation: the pattern carries the signal, not the volume."""
    base = numpy.abs(_vector(9)) + 1.0

    assert sparse_code.code(base) == sparse_code.code(base * 7.0)


def test_novelty_is_one_against_an_empty_memory() -> None:
    assert sparse_code.novelty(sparse_code.code(_vector(10)), []) == 1.0


def test_novelty_is_zero_against_itself() -> None:
    known = sparse_code.code(_vector(11))

    assert sparse_code.novelty(known, [known]) == 0.0


def test_novelty_falls_as_something_similar_is_already_known() -> None:
    base = _vector(12)
    near = base + numpy.random.default_rng(13).normal(scale=0.01, size=base.size)
    unrelated = _vector(14)

    against_near = sparse_code.novelty(sparse_code.code(base), [sparse_code.code(near)])
    against_other = sparse_code.novelty(
        sparse_code.code(base), [sparse_code.code(unrelated)]
    )

    assert against_near < against_other


def test_the_wrong_number_of_dimensions_is_refused() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        sparse_code.code(numpy.zeros(7))
