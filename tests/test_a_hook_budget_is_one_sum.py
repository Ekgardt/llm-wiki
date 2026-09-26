"""A hook delegate is stopped after its append's budget and before the host's limit (audit 2026-09-26 C-1).

docs/research/2026-09-26-a-hook-budget-is-one-sum.md
"""
from __future__ import annotations

import integration_adapter
import pytest
from daily_log_append import BREADCRUMB_APPEND_BUDGET_SECONDS


@pytest.mark.parametrize("delegate", ["user_prompt_capture.py", "post_tool_capture.py"])
def test_the_delegate_outlives_its_append_and_not_its_host(delegate: str) -> None:
    timeout = integration_adapter.DELEGATE_TIMEOUTS[delegate]

    assert BREADCRUMB_APPEND_BUDGET_SECONDS < timeout < integration_adapter.HOST_HOOK_TIMEOUT_SECONDS
