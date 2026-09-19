"""Audit 3, B27 and B24: the live fallback walks once, by one rule, and a
function found by the regex fallback has an end.

Research: `docs/research/2026-09-17-graph-one-live-walk-one-parse.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import code_graph  # noqa: E402

LIBRARY = b"def target():\n    return 1\n"
CALLER = b"from lib import target\n\n\ndef caller():\n    return target()\n"


def _repository(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "lib.py").write_bytes(LIBRARY)
    (root / "app.py").write_bytes(CALLER)
    return root


def _caller_files(root: Path) -> list[str]:
    answer = code_graph.find_callers("target", root, live=True)
    return sorted(Path(row["file"]).relative_to(root).as_posix() for row in answer)


def test_a_repository_under_a_directory_named_venv_still_has_definitions(tmp_path):
    root = _repository(tmp_path / "venv" / "proj")

    stats = code_graph.index_directory(root, verbose=False)
    dead = code_graph.find_dead_code(root, live=True)

    assert (stats["files"], stats["functions"]) == (2, 2)
    assert _caller_files(root) == ["app.py"]
    assert [item["name"] for item in dead] == ["caller"]


def test_every_live_answer_skips_the_same_directories(tmp_path):
    root = _repository(tmp_path / "proj")
    for skipped in (".claude/worktrees/agent", "venv/lib", "node_modules/pkg"):
        (root / skipped).mkdir(parents=True)
        (root / skipped / "copy.py").write_bytes(CALLER)

    callees = code_graph.find_callees("caller", root, live=True)
    stats = code_graph.index_directory(root, verbose=False)

    assert _caller_files(root) == ["app.py"]
    assert [Path(row["file"]).name for row in callees] == ["app.py"]
    assert stats["files"] == 2


def _parse_counter(monkeypatch) -> list[str]:
    parsed: list[str] = []
    real = code_graph._parse_file

    def counting(path, registry, workspace_root):
        parsed.append(Path(path).name)
        return real(path, registry, workspace_root)

    monkeypatch.setattr(code_graph, "_parse_file", counting)
    return parsed


def test_a_live_answer_and_its_report_share_one_parse(tmp_path, monkeypatch):
    root = _repository(tmp_path / "proj")
    parsed = _parse_counter(monkeypatch)

    code_graph.find_callers("target", root, live=True, with_report=True)
    after_callers = sorted(parsed)
    parsed.clear()
    code_graph.find_callees("caller", root, live=True, with_report=True)
    after_callees = sorted(parsed)
    parsed.clear()
    code_graph.detect_communities(root, live=True, with_report=True)

    assert after_callers == ["app.py", "lib.py"]
    assert after_callees == ["app.py", "lib.py"]
    assert sorted(parsed) == ["app.py", "lib.py"]


SCRIPT = (
    b"function helper() {\n  return 1;\n}\n\n"
    b"function entry()\n{\n  if (true) {\n    return helper();\n  }\n}\n"
)
PYTHON = (
    b"def helper():\n    return 1\n\n\n"
    b"async def entry(\n    flag,\n):\n\n    if flag:\n        return helper()\n"
    b"    return 0\n\n\nVALUE = entry\n"
)


def _without_grammars(monkeypatch) -> None:
    """The documented fallback: a machine with no tree-sitter grammar."""
    monkeypatch.setattr(code_graph, "_get_parser", lambda language: None)


def test_a_function_found_by_the_regex_fallback_has_an_end(tmp_path, monkeypatch):
    _without_grammars(monkeypatch)
    (tmp_path / "a.js").write_bytes(SCRIPT)
    (tmp_path / "b.py").write_bytes(PYTHON)

    script = code_graph.parse_file(tmp_path / "a.js")["functions"]
    python = code_graph.parse_file(tmp_path / "b.py")["functions"]

    assert [(f["name"], f["line"], f["end_line"]) for f in script] == [
        ("helper", 1, 3), ("entry", 5, 10),
    ]
    assert [(f["name"], f["line"], f["end_line"]) for f in python] == [
        ("helper", 1, 2), ("entry", 5, 11),
    ]


def test_the_regex_fallback_answers_callees_and_dead_code(tmp_path, monkeypatch):
    _without_grammars(monkeypatch)
    (tmp_path / "a.js").write_bytes(SCRIPT)

    callees = code_graph.find_callees("entry", tmp_path, live=True)
    dead = code_graph.find_dead_code(tmp_path, live=True)

    assert [row["callee"] for row in callees] == ["helper"]
    assert [item["name"] for item in dead] == ["entry"]
