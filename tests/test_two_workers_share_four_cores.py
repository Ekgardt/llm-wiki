"""Two stand workers on four cores get two threads each, not four.

Measured 2026-09-07: two workers embedding 131 chunks side by side took
28.5 s each with four torch threads apiece and 15.6 s each with two. The
threads were fighting for the cores. The stand now hands each worker its
share, and leaves an operator's own OMP_NUM_THREADS alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import run_longmemeval  # noqa: E402


def _args(concurrency: int) -> argparse.Namespace:
    return argparse.Namespace(concurrency=concurrency, provider="fake", provider_timeout=5)


def test_each_worker_gets_its_share_of_the_cores():
    assert run_longmemeval.worker_threads(2, cores=4) == 2
    assert run_longmemeval.worker_threads(1, cores=4) == 4
    assert run_longmemeval.worker_threads(3, cores=4) == 1


def test_a_worker_never_gets_fewer_than_one_thread():
    assert run_longmemeval.worker_threads(8, cores=4) == 1
    assert run_longmemeval.worker_threads(0, cores=4) == 4


def test_the_worker_environment_carries_the_share(monkeypatch):
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setattr(run_longmemeval, "worker_threads", lambda concurrency, cores=None: 2)

    environment = run_longmemeval._worker_environment(_args(2))

    assert environment["OMP_NUM_THREADS"] == "2"


def test_an_operator_value_is_kept(monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "3")

    environment = run_longmemeval._worker_environment(_args(2))

    assert environment["OMP_NUM_THREADS"] == "3"
