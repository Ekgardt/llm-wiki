from __future__ import annotations

import time

import symbol_snippet

from tests.test_a_name_is_resolved_before_the_graph_is_asked import (
    SAME_NAME_METHODS,
)
from tests.test_a_name_is_resolved_before_the_graph_is_asked import (
    built as built,
)
from tests.test_a_name_is_resolved_before_the_graph_is_asked import (
    indexed as indexed,
)


def test_hundreds_of_real_methods_return_snippets_with_complete_omission_counts(indexed):
    answer = symbol_snippet.snippet_for_symbol(indexed, "close", time.monotonic() + 60)
    assert answer.get("error") is None
    assert answer["resolved_nodes"] == SAME_NAME_METHODS
    assert len(answer["snippets"]) == symbol_snippet.MAX_LOCATIONS
    assert answer["nodes_omitted"] == SAME_NAME_METHODS - symbol_snippet.MAX_LOCATIONS


def test_hundreds_of_real_methods_return_definition_sites_with_omission_counts(indexed):
    answer = symbol_snippet.definition_report(indexed, "close", time.monotonic() + 60)
    assert len(answer["sites"]) == symbol_snippet.MAX_LOCATIONS
    assert answer["sites_omitted"] == SAME_NAME_METHODS - symbol_snippet.MAX_LOCATIONS
