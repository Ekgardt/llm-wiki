"""The stands must ask the product the way a caller asks it.

Both vault stands used to call `search_memory.search(question, limit=k)`: no
budget, no wrapper, a shape no caller uses. Four separately confirmed retrieval
defects lived in that gap and neither stand could see any of them
(`knowledge/log.md`, 2026-08-26).

These tests hold the door shut. They read the call shape off the product rather
than off a copy of it, so the day an entry point changes, the stand fails here
instead of quietly measuring something else.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT / "benchmark", ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

GOLD = "knowledge/notes/gold-page.md"
OTHER = "knowledge/notes/some-other-page.md"


def _corpus() -> dict:
    return {
        "corpus_id": "vault-retrieval-v1",
        "thresholds": {"min_hit_at_5": 0.6, "min_gain_over_grep_at_5": 0.1},
        "cases": [
            {"case_id": "one", "question": "какой вопрос", "gold_path": GOLD},
        ],
    }


def _capture(monkeypatch) -> dict:
    """Record the keyword arguments the product hands to `search`."""
    import search_memory

    seen: dict = {}

    def recorder(query, scope="all", limit=10, **kwargs):
        seen.update(kwargs)
        seen["scope"] = scope
        seen["limit"] = limit
        return []

    monkeypatch.setattr(search_memory, "search", recorder)
    return seen


def test_the_mcp_path_goes_through_the_servers_own_wrapper(monkeypatch):
    """Not `search()` directly: the wrapper is where `max_candidates` is decided."""
    from retrieval_paths import MCP, rows

    seen = _capture(monkeypatch)
    rows(MCP, "вопрос", 6)

    assert seen["source_tool"] == "mcp.recall"


def test_the_mcp_path_carries_the_budget_the_server_gives_every_operation(monkeypatch):
    """A budget is what three of the four 2026-08-26 defects needed to appear."""
    from mcp_server import MCP_OPERATION_SECONDS
    from retrieval_paths import MCP, rows

    seen = _capture(monkeypatch)
    before = time.monotonic()
    rows(MCP, "вопрос", 6)

    deadline = seen["deadline_monotonic"]
    assert before < deadline <= before + MCP_OPERATION_SECONDS + 1.0


def test_the_mcp_path_does_not_pass_the_answer_size_as_the_pool(monkeypatch):
    """Passing it collapsed each leg's pool; the wrapper says so in a comment."""
    from retrieval_paths import MCP, rows

    seen = _capture(monkeypatch)
    rows(MCP, "вопрос", 6)

    assert seen.get("max_candidates") is None


def _cli_kwargs_the_product_passes(monkeypatch) -> dict:
    """Read the CLI's call shape off the CLI itself, defaults and all."""
    import search_memory

    seen = _capture(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["search_memory.py", "вопрос"])
    search_memory.main()
    return seen


def test_the_cli_path_passes_exactly_what_the_cli_passes(monkeypatch):
    from retrieval_paths import CLI_KWARGS, CLI_SCOPE

    product = _cli_kwargs_the_product_passes(monkeypatch)
    mirrored = {key: product[key] for key in CLI_KWARGS}

    assert (mirrored, product["scope"]) == (CLI_KWARGS, CLI_SCOPE)


def test_the_cli_leaves_the_cross_encoder_off(monkeypatch):
    """A one-shot process pays about twenty seconds to load it, every time."""
    product = _cli_kwargs_the_product_passes(monkeypatch)

    assert product["rerank"] is False


def test_the_old_shape_carried_no_budget_at_all(monkeypatch):
    """The control. This is why the stands saw nothing, kept so it stays visible."""
    from retrieval_paths import API, rows

    seen = _capture(monkeypatch)
    rows(API, "вопрос", 6)

    assert seen.get("deadline_monotonic") is None


def _budget_sensitive(monkeypatch) -> None:
    """A product that answers correctly only when no budget is passed.

    Nothing real behaves this way. It stands in for the whole class of defects
    that appear only on a budgeted call, so one question can be asked of the
    stand: does that argument reach the score?
    """
    import search_memory

    def fake(query, scope="all", limit=10, **kwargs):
        if kwargs.get("deadline_monotonic") is None:
            return [{"path": GOLD}]
        return [{"path": OTHER}]

    monkeypatch.setattr(search_memory, "search", fake)


def test_a_budget_only_defect_moves_the_number_on_the_measured_path(
    monkeypatch, tmp_path
):
    from retrieval_paths import MCP
    from run_vault_retrieval import run

    _budget_sensitive(monkeypatch)
    report = run(_corpus(), tmp_path, path=MCP, warmup=False)

    assert report["metrics"]["product_hit_at_5"] == 0.0


def test_the_same_defect_is_invisible_to_the_shape_the_stands_used(
    monkeypatch, tmp_path
):
    """Same injection, old shape: the number does not move. That was the gap."""
    from retrieval_paths import API
    from run_vault_retrieval import run

    _budget_sensitive(monkeypatch)
    report = run(_corpus(), tmp_path, path=API, warmup=False)

    assert report["metrics"]["product_hit_at_5"] == 1.0


def test_a_spent_budget_is_a_recorded_miss_and_not_a_crashed_stand(
    monkeypatch, tmp_path
):
    """The agent on that path gets nothing; the stand says so and names why."""
    import search_memory
    from retrieval_paths import MCP
    from run_vault_retrieval import run

    def exhausted(query, scope="all", limit=10, **kwargs):
        raise TimeoutError("retrieval deadline exceeded")

    monkeypatch.setattr(search_memory, "search", exhausted)
    report = run(_corpus(), tmp_path, path=MCP, warmup=False)

    assert report["misses"][0]["fallback_reason"].startswith("TimeoutError")


def test_a_case_that_does_not_answer_the_same_way_twice_is_named(
    monkeypatch, tmp_path
):
    """Reported spread, not assumed stability: the wobbling case is printed."""
    import search_memory
    from retrieval_paths import MCP
    from run_vault_retrieval import run

    monkeypatch.setattr(search_memory, "search", _alternator())
    report = run(_corpus(), tmp_path, path=MCP, repeat=2, warmup=False)

    assert [item["case_id"] for item in report["unstable_cases"]] == ["one"]


def _alternator():
    """Gold, then not gold — one call each, so the two rounds disagree."""
    replies = [[{"path": GOLD}], [{"path": OTHER}]]

    def fake(query, scope="all", limit=10, **kwargs):
        return replies.pop(0)

    return fake
