"""The retrieval v2 facade stays the entry point, and the runner digest names every part."""
from __future__ import annotations

import sys
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parent.parent / "benchmark"
if str(BENCHMARK) not in sys.path:
    sys.path.insert(0, str(BENCHMARK))

import retrieval_v2_cli  # noqa: E402
import retrieval_v2_environment  # noqa: E402
import run_retrieval_v2  # noqa: E402


def test_the_facade_exposes_the_command_line_entry_point():
    assert run_retrieval_v2.main is retrieval_v2_cli.main


def test_the_runner_digest_names_the_facade_and_every_concern_module():
    parts = sorted(path.name for path in BENCHMARK.glob("retrieval_v2_*.py"))

    assert retrieval_v2_environment.RUNNER_SOURCES == ("run_retrieval_v2.py", *parts)

