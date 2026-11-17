"""Helpers shared by test_retrieval_telemetry.py and the test files importing them."""
from __future__ import annotations


def _event(**overrides):
    import retrieval_telemetry

    values = {
        "event_kind": "impression",
        "query": "private query",
        "retrieval_mode": "bm25",
        "candidate_id": "page",
        "rank": 1,
        "generation": "legacy",
        "source_tool": "test",
    }
    values.update(overrides)
    return retrieval_telemetry.make_event(**values)
