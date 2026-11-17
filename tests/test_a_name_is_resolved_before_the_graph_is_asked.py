"""Audit 3, A5-A7: names are resolved, cut and sliced before the reader is asked.

Research: `docs/research/2026-09-17-a-name-is-resolved-before-the-graph-is-asked.md`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.a_name_is_resolved_before_the_graph_is_asked_helpers import (
    COMMON_NAME_FILES,
    SAME_NAME_METHODS,
    built,  # noqa: F401
    indexed,  # noqa: F401
)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(scope="module")
def index_answer(built):  # noqa: F811
    return built[1]


def test_more_routes_than_one_reader_call_takes_still_get_a_hint_file(index_answer):
    hints = index_answer["hints"]
    assert (hints["status"], hints.get("routes")) == ("written", SAME_NAME_METHODS)


def _cut(answer: dict) -> tuple:
    return (
        answer["symbol_resolved"],
        answer["resolved_symbol_nodes"],
        answer.get("matching_symbol_nodes"),
        answer.get("symbol_nodes_truncated"),
    )


def test_a_common_name_is_cut_to_the_seed_limit_and_says_so(indexed):  # noqa: F811
    import code_graph

    answers = (
        code_graph.find_dependencies("main", indexed, with_report=True),
        code_graph.find_argument_flows("main", indexed),
        code_graph.find_service_paths("main", indexed),
        code_graph.find_callers("main", indexed, with_report=True, max_depth=2),
    )
    assert [_cut(answer) for answer in answers] == [(True, 20, COMMON_NAME_FILES, True)] * 4


def test_a_rare_name_carries_no_cut_fields(indexed):  # noqa: F811
    import code_graph

    answer = code_graph.find_dependencies("top", indexed, with_report=True)
    assert _cut(answer) == (True, 1, None, None)


def test_a_path_is_found_between_two_names_even_private_ones(indexed):  # noqa: F811
    import code_graph

    answer = code_graph.find_paths("top", "helper", indexed, with_report=True)
    private = code_graph.find_paths("_private", "helper", indexed, with_report=True)
    found = (
        [path["depth"] for path in answer["paths"]],
        [path["depth"] for path in private["paths"]],
        answer["source_resolved_nodes"],
        answer["target_resolved_nodes"],
    )
    assert found == ([2], [1], 1, 2)


def test_more_same_name_nodes_than_one_reader_call_takes_are_all_asked(indexed):  # noqa: F811
    import code_graph

    callees = code_graph.find_callees("close", indexed, with_report=True)
    callers = code_graph.find_callers("helper", indexed, with_report=True)
    counted = (len(callees["callees"]), len(callers["callers"]))
    assert counted == (SAME_NAME_METHODS, SAME_NAME_METHODS + 1)


# Audit C-36: four more lookups passed their own bound as the reader's row limit.
# docs/research/2026-09-25-a-common-name-is-asked-up-to-the-reader-ceiling.md


def test_community_mode_answers_for_a_name_shared_by_hundreds(indexed):  # noqa: F811
    import code_graph

    answer = code_graph.detect_communities(indexed, symbol="close", with_report=True)
    assert answer["community_count"] >= 1


def test_provenance_cuts_a_common_name_and_says_so(indexed, tmp_path):  # noqa: F811
    import time

    from provenance_join import join_symbol_provenance

    answer = join_symbol_provenance(tmp_path, indexed, "main", time.monotonic() + 60)
    shown = (len(answer["locations"]), answer["location_count"], answer["locations_truncated"])
    assert shown == (5, COMMON_NAME_FILES, True)


def test_a_qualified_name_is_found_among_hundreds_of_same_name_methods(indexed):  # noqa: F811
    import time

    from symbol_snippet import snippet_for_symbol

    answer = snippet_for_symbol(indexed, "Holder7.close", time.monotonic() + 60)
    assert (answer.get("error"), answer["resolved_nodes"]) == (None, 1)


def test_a_too_wide_trace_target_is_refused_by_name(indexed, tmp_path):  # noqa: F811
    from trace_ingest import TraceRefused, trace_callers

    with pytest.raises(TraceRefused, match="trace_target_filter_too_wide"):
        trace_callers("close", indexed, state_root=tmp_path)
