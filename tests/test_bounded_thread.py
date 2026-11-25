"""Every product thread keeps its join bound, even where the interpreter does not."""

from __future__ import annotations

import ast
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import bounded_thread  # noqa: E402

from tests.slow_machine import SHORT_TIMEOUT  # noqa: E402

# A bound the test expects to run out while the thread is still inside its target.
ELAPSING_BOUND_SECONDS = 0.05


@pytest.fixture
def interpreter_join_ignores_its_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """python/cpython#161712: a timed join that waits for the thread's end instead."""
    real_join = threading.Thread.join

    def join(thread: threading.Thread, timeout: float | None = None) -> None:
        real_join(thread)

    monkeypatch.setattr(threading.Thread, "join", join)


def test_a_timed_join_returns_while_the_thread_still_runs(
    interpreter_join_ignores_its_timeout: None,
) -> None:
    release = threading.Event()
    thread = bounded_thread.BoundedThread(target=release.wait, args=(SHORT_TIMEOUT,))
    thread.start()
    try:
        thread.join(ELAPSING_BOUND_SECONDS)
        assert thread.is_alive()
    finally:
        release.set()
        thread.join()
    assert not thread.is_alive()


def test_a_finished_thread_is_joined_whole_by_a_timed_join() -> None:
    thread = bounded_thread.BoundedThread(target=lambda: None)
    thread.start()
    thread.join(SHORT_TIMEOUT)
    assert not thread.is_alive()


def test_a_thread_that_raises_still_ends_its_timed_join(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(threading, "excepthook", lambda _args: None)

    def fail() -> None:
        raise RuntimeError("target failed")

    thread = bounded_thread.BoundedThread(target=fail)
    thread.start()
    thread.join(SHORT_TIMEOUT)
    assert not thread.is_alive()


def _plain_thread_constructions(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        f"{path.name}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {"threading.Thread", "Thread"}
    ]


def test_no_script_starts_a_thread_whose_join_can_lose_its_bound() -> None:
    scripts = sorted((ROOT / "scripts").rglob("*.py"))
    offenders = [
        site
        for path in scripts
        if path.name != "bounded_thread.py"
        for site in _plain_thread_constructions(path)
    ]

    assert offenders == []
