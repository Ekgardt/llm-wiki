"""The live graph parses only regular files of its own tree, within a bound (audit C-41).

docs/research/2026-09-25-the-live-graph-reads-only-its-own-files.md
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402


def test_a_file_over_the_bound_is_not_parsed(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "small.py").write_text("def small():\n    return 1\n", encoding="utf-8")
    with open(repository / "huge.py", "wb") as handle:
        handle.truncate(code_graph.LIVE_SOURCE_MAX_BYTES + 1)

    names = [path.name for path in code_graph._live_source_files(repository)]

    assert names == ["small.py"]


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_a_link_to_a_file_outside_the_tree_is_not_parsed(tmp_path):
    outside = tmp_path / "private.py"
    outside.write_text("def private_secret():\n    return 1\n", encoding="utf-8")
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "linked.py").symlink_to(outside)

    assert code_graph._live_source_files(repository) == []


def test_a_parse_starts_no_git_process(tmp_path, monkeypatch):
    source = tmp_path / "a.py"
    source.write_text("def a():\n    return 1\n", encoding="utf-8")
    started: list[object] = []
    monkeypatch.setattr(code_graph.subprocess, "run", lambda *args, **kwargs: started.append(args))

    parsed = code_graph.parse_file(source)

    assert ([item["name"] for item in parsed["functions"]], started) == (["a"], [])
