"""Partial-result scenarios expire after their backend, not during preflight."""
from __future__ import annotations

import time

import pytest
import retrieval

from tests import test_retrieval_partial_on_expiry as partial_tests
from tests import test_retrieval_review_round2 as review_tests


@pytest.fixture(autouse=True)
def preflight_clock_advance(monkeypatch):
    original_clock = time.monotonic
    original_analysis = retrieval.analyze_query
    advance = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: original_clock() + advance[0])

    def delayed_analysis(*args, **kwargs):
        result = original_analysis(*args, **kwargs)
        # The existing fake backend used 50 ms. Move that elapsed interval
        # into preflight without sleeping to expose the wrong fault phase.
        advance[0] += 0.05
        return result

    monkeypatch.setattr(retrieval, "analyze_query", delayed_analysis)


test_review_partial_result_after_preflight_delay = review_tests.test_retrieve_respects_deadline
test_finished_leg_after_preflight_delay = partial_tests.test_the_finished_lexical_leg_survives_the_deadline
test_label_after_preflight_delay = partial_tests.test_a_salvaged_answer_says_it_is_partial_and_why
test_no_extra_stage_after_preflight_delay = partial_tests.test_salvage_starts_no_new_work_and_runs_no_reranker
test_real_salvage_cost_after_preflight_delay = partial_tests.test_salvage_is_bounded_in_memory_work
