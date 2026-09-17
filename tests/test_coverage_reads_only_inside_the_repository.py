"""Audit 3, A2: coverage hashes a file only through the contained reader.

Research: `docs/research/2026-09-17-coverage-reads-only-inside-the-repository.md`.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import path_coverage  # noqa: E402


class _EmptyGraph:
    generation_id = "generation-x"

    def source_by_path(self, relative, *, deadline=None):
        return None

    def find_nodes(self, **_kwargs):
        return []


def _freshness_of(repository: Path, relative: str) -> str:
    answer = path_coverage._coverage_answer(
        _EmptyGraph(), repository, relative, time.monotonic() + 10
    )
    return answer["freshness"]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inside.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    return root


def test_a_path_outside_the_repository_is_never_opened(repository: Path) -> None:
    outside = repository.parent / "outside.txt"
    verdicts = (
        _freshness_of(repository, str(outside)),
        _freshness_of(repository, "../outside.txt"),
        _freshness_of(repository, "../absent.txt"),
    )
    assert verdicts == ("unreadable", "unreadable", "unreadable")


def test_files_inside_keep_their_honest_answers(repository: Path) -> None:
    verdicts = (
        _freshness_of(repository, "inside.py"),
        _freshness_of(repository, "absent.py"),
    )
    assert verdicts == ("not_indexed", "missing_on_disk")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
def test_a_fifo_and_a_symlink_are_refused_without_waiting(repository: Path) -> None:
    os.mkfifo(repository / "pipe")
    (repository / "link.txt").symlink_to(repository.parent / "outside.txt")
    started = time.monotonic()
    verdicts = (_freshness_of(repository, "pipe"), _freshness_of(repository, "link.txt"))
    assert verdicts == ("unreadable", "unreadable")
    assert time.monotonic() - started < 5


def test_a_file_over_the_ceiling_is_unreadable_not_missing(repository: Path) -> None:
    with open(repository / "huge.bin", "wb") as handle:
        handle.truncate(path_coverage.MAX_HASHED_BYTES + 1)
    assert _freshness_of(repository, "huge.bin") == "unreadable"


def test_the_tool_refuses_a_coverage_path_that_is_not_repository_relative(
    repository: Path,
) -> None:
    import mcp_server

    def verdict(path: str):
        return mcp_server._validate_tool_arguments(
            "get_architecture",
            {"directory": str(repository), "mode": "coverage", "path": path},
        )

    refused = "argument 'path' must be a canonical repository-relative path"
    verdicts = (verdict("/dev/zero"), verdict("../outside.txt"), verdict("inside.py"))
    assert verdicts == (refused, refused, None)
