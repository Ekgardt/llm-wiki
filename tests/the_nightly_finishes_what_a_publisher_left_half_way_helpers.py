"""Helpers shared by test_the_nightly_finishes_what_a_publisher_left_half_way.py and the test files importing them."""
from __future__ import annotations

import markdown_transaction
import queue_core


def _active_vault(tmp_path, monkeypatch, queue, coordinator) -> None:
    monkeypatch.setenv("LLM_WIKI_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("LLM_WIKI_ROOT", str(tmp_path))
    monkeypatch.setattr(queue_core, "active_memory_queue", lambda _vault, _state: queue)
    monkeypatch.setattr(markdown_transaction, "active_markdown_coordinator", lambda _vault, _state: coordinator)
