"""Helpers shared by test_compile_repair_completion.py and the test files importing them."""

from markdown_transaction import active_markdown_coordinator

from tests.compile_call_controller_helpers import work as work


def _coordinator(work):
    return active_markdown_coordinator(work[0].vault, work[0].state_root)
