"""A refresh pass stops inside its budget and reports what it deferred.

A 900-second `refresh-all` was still running at 1 000 s, and one build that ran
out of time escaped the whole pass with no report. Research:
`docs/research/2026-09-14-a-pass-that-keeps-its-budget.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import search_memory  # noqa: E402

from tests.test_repository_refresh import adopted_vault  # noqa: E402,F401

DIMENSIONS = 4


class _Embedder:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        return [[float(len(text)), 1.0, 2.0, 3.0] for text in texts]


def test_embedding_stops_between_chunks_when_the_pass_is_out_of_time():
    embedder = _Embedder()
    checks = iter([None, None])

    def check_stop() -> None:
        if next(checks, "stop") == "stop":
            raise TimeoutError("generation retrieval deadline exceeded")

    texts = ["x" * index for index in range(search_memory.EMBED_STOP_BATCH * 3)]
    with pytest.raises(TimeoutError):
        search_memory._embedded_rows(texts, embedder, DIMENSIONS, check_stop)

    assert embedder.calls == 2


def test_chunked_embedding_keeps_every_row_in_order():
    texts = ["x" * index for index in range(search_memory.EMBED_STOP_BATCH + 7)]

    matrix = search_memory._embedded_rows(texts, _Embedder(), DIMENSIONS)

    assert np.array_equal(matrix[:, 0], np.arange(len(texts), dtype=np.float32))


def test_a_pass_with_no_budget_left_reports_instead_of_raising(adopted_vault):  # noqa: F811
    import repository_index

    _root, state = adopted_vault

    report = repository_index.refresh_all_repositories(state_root=state, budget_seconds=0)

    assert report["schema_version"] == repository_index.SCHEMA_VERSION
