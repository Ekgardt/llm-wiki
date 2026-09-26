"""An answer about another repository names that repository's commit (audit 2026-09-26 C-9).

docs/research/2026-09-26-an-answer-names-the-commit-it-read.md
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import mcp_contract
import pytest


def _repository(path: Path) -> tuple[Path, str]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.py").write_text("x = 1\n", encoding="utf-8")
    for arguments in (["add", "a.py"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c"]):
        subprocess.run(["git", "-C", str(path), *arguments], check=True)
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    return path, head.stdout.strip()


def _directory_tools() -> list[str]:
    from mcp_server import TOOL_INPUT_SCHEMAS

    return sorted(name for name, schema in TOOL_INPUT_SCHEMAS.items() if "directory" in schema.get("properties", {}))


@pytest.mark.parametrize("tool", _directory_tools())
def test_a_directory_tool_names_the_commit_of_that_directory(tmp_path, tool) -> None:
    import mcp_server

    repository, head = _repository(tmp_path / "other")
    mcp_contract._SOURCE_COMMITS.clear()

    envelope = mcp_server._tool_call_envelope(tool, {}, {"directory": str(repository)}, False, time.monotonic() + 30)

    assert envelope["source_commit"] == head


def test_a_relative_directory_names_no_commit(tmp_path) -> None:
    mcp_contract._SOURCE_COMMITS.clear()

    envelope = mcp_contract.build_envelope({}, root=tmp_path, source_root="some/relative")

    assert (envelope["source_commit"], "Source commit is unavailable." in envelope["warnings"]) == (None, True)


def test_the_directory_tools_are_found() -> None:
    assert {"find_dead_code", "get_architecture"} <= set(_directory_tools())
