"""The code graph is opened before the first question, not by it.

Measured on the installed vault 2026-09-12: the first code answer in a process
cost 2.22 s and every later one 0.31 s, all of the difference being the
generation open. Warmed on a daemon thread at start, the first question costs
0.30 s. Research:
`docs/research/2026-09-12-warming-the-graph-before-the-first-question.md`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import mcp_server  # noqa: E402


class _Lease:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_the_warm_up_returns_the_lease_it_took(monkeypatch):
    """A held lease is a held lock; what the next caller wants is the reader."""
    lease = _Lease()
    monkeypatch.setattr(mcp_server, "_warm_graph_lease", lambda _seconds: lease)

    mcp_server.warmup_code_graph(1.0)

    assert lease.closed is True


def test_a_warm_up_that_cannot_open_anything_is_quiet(monkeypatch):
    monkeypatch.setattr(mcp_server, "_warm_graph_lease", lambda _seconds: None)

    mcp_server.warmup_code_graph(1.0)  # must not raise


def test_a_failing_open_is_swallowed_rather_than_raised(monkeypatch):
    """Serving never fails because a warm-up did."""
    def explode(*_args, **_kwargs):
        raise RuntimeError("no generation here")

    monkeypatch.setattr("code_graph._active_evidence_graph", explode)

    assert mcp_server._warm_graph_lease(1.0) is None  # noqa: SLF001


def test_the_warm_up_thread_is_a_daemon_so_the_process_can_exit(monkeypatch):
    started: list[object] = []

    class _Thread:
        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs

        def start(self) -> None:
            started.append(self.kwargs)

    monkeypatch.setattr(mcp_server.threading, "Thread", lambda **kwargs: _Thread(**kwargs))
    monkeypatch.delenv("LLMWIKI_NO_GRAPH_WARMUP", raising=False)

    mcp_server._start_graph_warmup()  # noqa: SLF001

    assert [(item["daemon"], item["name"]) for item in started] == [(True, "graph-warmup")]


def test_the_operator_can_keep_the_lazy_behaviour(monkeypatch):
    started: list[object] = []
    monkeypatch.setattr(
        mcp_server.threading, "Thread", lambda **kwargs: started.append(kwargs)
    )
    monkeypatch.setenv("LLMWIKI_NO_GRAPH_WARMUP", "1")

    mcp_server._start_graph_warmup()  # noqa: SLF001

    assert started == []


@pytest.mark.parametrize("name", ["_start_encoder_warmup", "_start_graph_warmup"])
def test_the_server_starts_both_warm_ups(monkeypatch, name):
    """The graph warm-up is a sibling of the encoder's, started the same way."""
    assert callable(getattr(mcp_server, name))
