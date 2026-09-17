"""Audit 3, B1: the task-shaped modes validate `directory` like the older ones.

Research: `docs/research/2026-09-17-every-mode-checks-the-directory-it-is-given.md`.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402

MODE_ARGUMENTS = {
    "provenance": {"symbol": "helper"},
    "snippet": {"symbol": "helper"},
    "coverage": {"path": "pkg/core.py"},
    "search": {"symbol": "helper"},
    "query": {"query": "{}"},
    "data_flow": {"symbol": "helper"},
    "cross_service": {"symbol": "helper"},
}


def _answer(mode: str, directory: str):
    arguments = {"mode": mode, "directory": directory, **MODE_ARGUMENTS[mode]}
    return mcp_server._architecture_tool_call(arguments, time.monotonic() + 10)


@pytest.mark.parametrize("mode", sorted(MODE_ARGUMENTS))
def test_a_relative_directory_is_refused_by_name(mode: str) -> None:
    assert _answer(mode, "relative/project") == {
        "error": "directory must be an absolute local path"
    }


@pytest.mark.parametrize("mode", sorted(MODE_ARGUMENTS))
def test_a_filesystem_root_is_refused_by_name(mode: str) -> None:
    root = Path(Path.cwd().anchor)
    assert _answer(mode, str(root)) == {"error": "directory must not be a filesystem root"}


def test_the_table_of_checked_modes_is_the_table_under_test() -> None:
    assert sorted(MODE_ARGUMENTS) == sorted(mcp_server._DIRECTORY_CHECKED_MODES)
