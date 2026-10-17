"""Inject terminal-cleanup failure after the real server has started."""
from __future__ import annotations

import lsp_process
import pytest

from tests import test_lsp_process as process_tests
from tests.test_lsp_process import (
    _no_lsp_lifecycle_owner_leaks as _no_lsp_lifecycle_owner_leaks,
)


@pytest.fixture(autouse=True)
def observe_normal_startup_budget(monkeypatch):
    normal_budget = lsp_process._STARTUP_WAIT_SECONDS
    original = lsp_process._resolved_startup_deadline

    def observed(configured_deadline):
        assert lsp_process._STARTUP_WAIT_SECONDS == normal_budget
        return original(configured_deadline)

    monkeypatch.setattr(lsp_process, "_resolved_startup_deadline", observed)


test_terminal_cleanup_fault_keeps_the_normal_startup_budget = (
    process_tests.test_startup_terminal_intent_timeout_returns_retryable_cleanup_error
)
