"""Helpers shared by test_one_bad_day_does_not_hold_the_rest.py and the test files importing them."""
from __future__ import annotations

from types import SimpleNamespace

from reliable_memory import canonical_json_bytes, sha256_bytes

BUDGET = {"provider": "fake", "model": "fake-v1", "max_output_tokens": 4000}


def _plan(inputs) -> SimpleNamespace:
    key = sha256_bytes(canonical_json_bytes([item.logical_path for item in inputs.dailies]))
    plan = {"schema_version": "compile-plan/v2", "operations": []}
    return SimpleNamespace(plan=plan, action_key=key, cache_hit=False, provider_budget=BUDGET)
