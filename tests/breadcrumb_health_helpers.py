"""Helpers shared by test_breadcrumb_health.py and the test files importing them."""
from __future__ import annotations

import markdown_transaction
import queue_core

from tests.reliability_v3_adoption_helpers import _vault, build_adopted_reliability_v3


def _runtime(tmp_path):
    vault, state = _vault(tmp_path)
    build_adopted_reliability_v3(vault, state)
    queue = queue_core.active_memory_queue(vault, state)
    coordinator = markdown_transaction.active_markdown_coordinator(vault, state)
    return state, queue, coordinator
