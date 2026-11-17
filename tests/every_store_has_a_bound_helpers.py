"""Helpers shared by test_every_store_has_a_bound.py and the test files importing them."""
from __future__ import annotations

import time

import markdown_transaction
from markdown_transaction import MarkdownCoordinator

from tests.slow_machine import LONG_TIMEOUT


def _append(coordinator: MarkdownCoordinator, day: str, family: str = "post-tool"):
    return markdown_transaction.append_until_committed(
        coordinator,
        f"{family}:{day}",
        f"knowledge/daily/{day}.md",
        f"# {day}\n".encode(),
        deadline=time.monotonic() + LONG_TIMEOUT,
        cancelled=None,
    )
