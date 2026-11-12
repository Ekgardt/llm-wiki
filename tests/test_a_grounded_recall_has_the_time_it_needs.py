"""A grounded recall gets the time a provider needs, and its provider call ends with it.

See docs/research/2026-09-25-a-grounded-recall-has-the-time-it-needs-and-no-more.md.
"""

from __future__ import annotations

import threading
import time

import llm_client
import mcp_server
import query_memory

from tests.slow_machine import LONG_TIMEOUT


def test_the_provider_call_runs_under_the_time_left(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_LLM_TIMEOUT_S", raising=False)
    seen: list[int] = []

    def generator(prompt, system_prompt, max_tokens):
        seen.append(llm_client.timeout_s())
        return "answer"

    answer = query_memory.generate_before_deadline(generator, "p", "s", time.monotonic() + 7.5)

    assert (answer, seen) == ("answer", [8])


def test_a_ceiling_set_in_one_thread_does_not_reach_another(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_LLM_TIMEOUT_S", raising=False)
    inside = threading.Event()
    release = threading.Event()

    def hold_a_ceiling() -> None:
        with llm_client.call_ceiling(300):
            inside.set()
            release.wait(5)

    worker = threading.Thread(target=hold_a_ceiling)
    worker.start()
    inside.wait(LONG_TIMEOUT)
    here = llm_client.timeout_s()
    release.set()
    worker.join(LONG_TIMEOUT)

    assert here == llm_client.DEFAULT_TIMEOUT_S


def test_a_grounded_recall_has_the_grounded_budget() -> None:
    budgets = (
        mcp_server.tool_operation_seconds("recall", {"query": "q", "grounded": True}),
        mcp_server.tool_operation_seconds("recall", {"query": "q"}),
    )

    # An ungrounded recall has the retrieval budget measured for its reranker (B-9),
    # not the generic tool budget; a grounded one still has the whole QA deadline.
    assert budgets == (query_memory.QA_DEADLINE_SECONDS, mcp_server.retrieval_operation_seconds())
