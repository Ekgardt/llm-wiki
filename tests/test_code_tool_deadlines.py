"""The code tools answer within their budget or refuse by name.

`find_dead_code` and `get_architecture` delegate to ``code_graph``, whose live
extraction takes no deadline. Measured on the live vault on 2026-08-27, that
path parsed 7,545 files, held every AST in memory, and was killed by the OS
after minutes — the caller saw a silent hang, never a named result. These
tests pin the repaired boundary: an expired or exhausted budget yields a
bounded result with ``status: "timeout"``, and a missing ``directory`` is a
named refusal instead of a bare ``KeyError``.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# The injected slow step sleeps this long; the budget below cuts it off far
# earlier. The gap between the two is what distinguishes a bounded return
# from simply waiting the step out.
_SLOW_STEP_SECONDS = 5.0
_BUDGET_SECONDS = 0.5
# Generous ceiling for a loaded machine: well under the slow step, well over
# the budget. A bounded return passes at any load; waiting out the step fails.
_CUTOFF_CEILING_SECONDS = 4.0


def _dispatch(name: str, arguments: dict, deadline: float) -> dict:
    import mcp_server

    data, _clamped = mcp_server._dispatch_tool(name, arguments, deadline)
    return data


def _held_slots(mcp_server) -> list[bool]:
    return [
        mcp_server._CODE_GRAPH_SLOTS.acquire(blocking=False)
        for _ in range(mcp_server.CODE_GRAPH_WORK_SLOTS)
    ]


def _release_slots(mcp_server, held: list[bool]) -> None:
    for taken in held:
        if taken:
            mcp_server._CODE_GRAPH_SLOTS.release()


class TestMissingDirectoryIsNamed:
    def test_find_dead_code_without_directory_refuses_by_name(self):
        data = _dispatch("find_dead_code", {}, time.monotonic() + 5.0)

        assert data == {"error": "directory is required"}

    def test_get_architecture_without_directory_refuses_by_name(self):
        data = _dispatch("get_architecture", {}, time.monotonic() + 5.0)

        assert data == {"error": "directory is required"}

    def test_a_precise_mode_without_position_names_what_is_missing(self):
        data = _dispatch(
            "get_architecture",
            {"directory": "/tmp", "mode": "definition"},
            time.monotonic() + 5.0,
        )

        assert data == {"error": "missing required arguments: path, line, character"}


class TestPastDeadlineIsBounded:
    def test_find_dead_code_with_a_past_deadline_names_the_timeout(self, tmp_path):
        data = _dispatch(
            "find_dead_code", {"directory": str(tmp_path)}, time.monotonic() - 1.0
        )

        assert data["status"] == "timeout", data
        assert data["warning"] == "code_graph_timeout"
        assert data["skipped"] == ["code_graph_analysis"]

    def test_get_architecture_with_a_past_deadline_names_the_timeout(self, tmp_path):
        data = _dispatch(
            "get_architecture", {"directory": str(tmp_path)}, time.monotonic() - 1.0
        )

        assert data["status"] == "timeout", data
        assert data["warning"] == "code_graph_timeout"
        assert data["skipped"] == ["code_graph_analysis"]


class TestExhaustedWorkersRefuseByName:
    def test_no_free_slot_is_a_named_timeout_not_a_queue(self, tmp_path):
        import mcp_server

        held = _held_slots(mcp_server)
        try:
            data = _dispatch(
                "find_dead_code", {"directory": str(tmp_path)}, time.monotonic() + 5.0
            )
        finally:
            _release_slots(mcp_server, held)

        assert all(held), "another test left a code-graph slot taken"
        assert data["status"] == "timeout", data
        assert "busy" in data["detail"]


def _slow_step(release: threading.Event):
    def slow(directory, **options):
        release.wait(_SLOW_STEP_SECONDS)
        return {"entry_points": [], "routes": [], "hotspots": [], "communities": []}

    return slow


def _bounded_elapsed(name: str, tmp_path) -> tuple[dict, float]:
    started = time.monotonic()
    data = _dispatch(name, {"directory": str(tmp_path)}, started + _BUDGET_SECONDS)
    return data, time.monotonic() - started


class TestSlowWorkIsCutOffAtTheBudget:
    def test_a_slow_dead_code_step_returns_at_the_budget(self, tmp_path, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("code_graph.find_dead_code", _slow_step(release))

        data, elapsed = _bounded_elapsed("find_dead_code", tmp_path)
        release.set()

        assert data["status"] == "timeout", data
        assert data["completed"] == ["directory_validation"]
        assert elapsed < _CUTOFF_CEILING_SECONDS, (
            f"returned after {elapsed:.2f}s for a {_BUDGET_SECONDS}s budget "
            f"against a {_SLOW_STEP_SECONDS}s step"
        )

    def test_a_slow_architecture_step_returns_at_the_budget(self, tmp_path, monkeypatch):
        release = threading.Event()
        monkeypatch.setattr("code_graph.get_architecture", _slow_step(release))

        data, elapsed = _bounded_elapsed("get_architecture", tmp_path)
        release.set()

        assert data["status"] == "timeout", data
        assert data["completed"] == ["directory_validation"]
        assert elapsed < _CUTOFF_CEILING_SECONDS, (
            f"returned after {elapsed:.2f}s for a {_BUDGET_SECONDS}s budget "
            f"against a {_SLOW_STEP_SECONDS}s step"
        )
