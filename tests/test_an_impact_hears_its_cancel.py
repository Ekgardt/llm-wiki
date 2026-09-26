"""An impact run stops when its caller cancels, in Git and in the mapping (audit 2026-09-26 C-9).

docs/research/2026-09-26-an-impact-hears-its-cancel.md
"""
from __future__ import annotations

import ast
import os
import threading
import time
from pathlib import Path

import impact_analysis
import pytest

from tests.slow_machine import LONG_TIMEOUT

posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in git is a POSIX shell script")


def _hanging_git(tmp_path: Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text("#!/bin/sh\nexec sleep 600\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@posix_only
def test_a_cancel_stops_a_running_git_child(tmp_path, monkeypatch) -> None:
    _hanging_git(tmp_path, monkeypatch)
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    started = time.monotonic()

    with pytest.raises(TimeoutError, match="cancelled"):
        impact_analysis._git(
            tmp_path, ["status"], deadline=time.monotonic() + LONG_TIMEOUT, max_bytes=1024, cancelled=cancel.is_set
        )

    assert time.monotonic() - started < LONG_TIMEOUT / 2


def test_a_cancel_stops_the_range_walk() -> None:
    content = b"line\n" * 1000

    with pytest.raises(TimeoutError, match="cancelled"):
        impact_analysis._changed_ranges(content, content + b"x\n", deadline=float("inf"), cancelled=lambda: True)


def _is_stop_check(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_check_impact_stop"


def _deadline_only(node: ast.Call) -> bool:
    if len(node.args) != 1:
        return False
    return not isinstance(node.args[0], ast.Starred)


def _stop_checks_without_cancel(function: ast.FunctionDef) -> list[int]:
    checks = [node for node in ast.walk(function) if _is_stop_check(node)]
    return [node.lineno for node in checks if _deadline_only(node)]


def test_every_stop_check_also_hears_the_cancel() -> None:
    """A deadline-only check is where a cancel goes unheard (the class, not one loop)."""
    tree = ast.parse(Path(impact_analysis.__file__).read_text(encoding="utf-8"))
    functions = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]

    assert {f.name: _stop_checks_without_cancel(f) for f in functions if _stop_checks_without_cancel(f)} == {}
