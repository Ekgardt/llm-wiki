"""Original pure reference/dispatch assertions, preserved verbatim."""
from __future__ import annotations

import value_references


def test_a_thread_target_counts_as_a_reference() -> None:
    index = value_references.build_reference_index(
        [("app.py", b"import threading\nthreading.Thread(target=_run)\n")]
    )

    assert index.names_a_value("_run") is True


def test_a_visitor_method_is_dispatched_by_its_base_class() -> None:
    source = b"import ast\n\n\nclass V(ast.NodeVisitor):\n    def visit_Call(self):\n        pass\n"
    index = value_references.build_reference_index([("v.py", source)])

    assert index.is_dispatched("v.py", "visit_Call", 5) is True


def test_a_staticmethod_is_not_treated_as_handed_over() -> None:
    source = b"class C:\n    @staticmethod\n    def helper():\n        pass\n"
    index = value_references.build_reference_index([("c.py", source)])

    assert index.is_dispatched("c.py", "helper", 3) is False


def test_a_registering_decorator_hands_the_definition_over() -> None:
    source = b"@server.route('/x')\ndef view():\n    pass\n"
    index = value_references.build_reference_index([("r.py", source)])

    assert index.is_dispatched("r.py", "view", 2) is True


def test_a_source_that_will_not_parse_widens_rather_than_claims_nothing() -> None:
    index = value_references.build_reference_index([("broken.py", b"def (:\n")])

    assert (index.names_a_value("def"), index.lexical_sources) == (True, 1)


def test_prose_that_merely_mentions_a_name_does_not_rescue_it() -> None:
    index = value_references.build_reference_index(
        [("d.py", b'"""Claim one task for the legacy mark_attempt facade."""\n')]
    )

    assert index.names_a_value("mark_attempt") is False
