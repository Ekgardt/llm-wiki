"""Shared generated corpus fixture and exact byte-offset/capability contracts."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK_ROOT = Path(__file__).resolve().parents[3] / 'benchmark'
if str(BENCHMARK_ROOT) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_ROOT))

import generate_python_qualification as qualification_generator  # noqa: E402
from generate_python_qualification import (  # noqa: E402
    QualificationRepository,
    generate_qualification_repository,
)

__all__ = ['qualification_generator', 'generated_pair', '_line_start_bytes', '_GOLD_CAPABILITIES']


@pytest.fixture(scope="module")
def generated_pair(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[QualificationRepository, QualificationRepository]:
    root = tmp_path_factory.mktemp("code-navigation-qualification")
    return (
        generate_qualification_repository(root / "first"),
        generate_qualification_repository(root / "second"),
    )


_GOLD_CAPABILITIES = ("definition", "references", "calls")


def _line_start_bytes(content: bytes, line: int) -> int:
    return sum(len(item) for item in content.splitlines(keepends=True)[: line - 1])
