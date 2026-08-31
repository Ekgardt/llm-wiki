"""Two silently wrong answers, and what makes each verdict true.

Both were found by the CODE-07 paired stand on 2026-08-29 and verified twice.

1. `find_dead_code` called live code dead. A name loaded as a *value* — a
   thread target, a registry entry, a name spelled in a `getattr` table —
   produces no `CALLS` edge, so it looked uncalled. Measured on the active
   generation: 402 of 461 `zero_confirmed_incoming_calls` names are named
   somewhere in the same corpus. The tool refuted itself inside one run —
   `_architecture_dependencies` was listed dead while sitting in
   `_ARCHITECTURE_MODE_QUERIES`.

2. `mode=dependencies` answered `[]` for everything, always. The `dependency`
   table holds 0 rows in every generation on disk and no producer writes one,
   while the same file holds 3,934 `IMPORTS` assertions. The loud half of the
   same root: a symbol name was forwarded where a node id was expected, so
   every private name raised instead of answering.

Research: `docs/research/2026-08-29-a-name-loaded-is-a-name-used.md`.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402
import value_references  # noqa: E402

_SOURCE = b'''HANDLERS = {"run": _worker}
BY_PLATFORM = {"Linux": "_by_name"}


def _worker():
    helper()


def _by_name():
    pass


def helper():
    pass


def orphan():
    pass
'''


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _node(node_id: str, kind: str, name: str, path: str = "app.py") -> dict:
    return {
        "node_id": node_id,
        "kind": kind,
        "identity_scheme": "python/v1",
        "identity_key": f"app:{name}",
        "metadata": {"name": name, "path": path, "owner": "app"},
    }


def _definition_span(name: str) -> tuple[int, int, int, int]:
    """The exact bytes and lines of `def <name>():`, as the extractor records."""
    start = _SOURCE.index(f"def {name}(".encode())
    end = _SOURCE.index(b"\n", start)
    line = _SOURCE.count(b"\n", 0, start) + 1
    return start, end, line, line


def _occurrence(node_id: str, name: str) -> dict:
    start, end, line_start, line_end = _definition_span(name)
    return {
        "occurrence_id": f"occurrence-{node_id}",
        "node_id": node_id,
        "source_id": "source",
        "role": "definition",
        "byte_start": start,
        "byte_end": end,
        "line_start": line_start,
        "line_end": line_end,
    }


def _assertion(assertion_id: str, source: str, edge: str, target: str) -> dict:
    return {
        "assertion_id": assertion_id,
        "source_node_id": source,
        "edge_type": edge,
        "target_node_id": target,
        "literal": None,
        "confidence": "high",
        "authority": "ai-derived",
        "resolution": "resolved",
        "extractor": "python/v1",
    }


def _evidence(evidence_id: str, assertion_id: str) -> dict:
    start, end, _line, _end_line = _definition_span("_worker")
    return {
        "evidence_id": evidence_id,
        "assertion_id": assertion_id,
        "observation_id": None,
        "source_id": "source",
        "byte_start": start,
        "byte_end": end,
        "span_sha256": _digest(_SOURCE[start:end]),
    }


def _graph_records() -> dict:
    """One module that imports another, and four functions in it."""
    return {
        "sources": [
            {
                "source_id": "source",
                "relative_path": "app.py",
                "sha256": _digest(_SOURCE),
                "size": len(_SOURCE),
                "media_type": "text/x-python",
                "language": "python",
                "git_oid": None,
            }
        ],
        "source_bytes": {"source": _SOURCE},
        "nodes": [
            _node("module-app", "module", "app"),
            _node("module-dep", "module", "dependency", path="dependency.py"),
            _node("worker", "function", "_worker"),
            _node("by-name", "function", "_by_name"),
            _node("helper", "function", "helper"),
            _node("orphan", "function", "orphan"),
        ],
        "occurrences": [
            _occurrence("worker", "_worker"),
            _occurrence("by-name", "_by_name"),
            _occurrence("helper", "helper"),
            _occurrence("orphan", "orphan"),
        ],
        "assertions": [
            _assertion("imports", "module-app", "IMPORTS", "module-dep"),
            _assertion("calls", "worker", "CALLS", "helper"),
        ],
        "evidence": [
            _evidence("evidence-imports", "imports"),
            _evidence("evidence-calls", "calls"),
        ],
        "observations": [
            {
                "observation_id": "observation",
                "source_node_id": "worker",
                "edge_type": "CALLS",
                "target_text": "queue.dynamic",
                "reason": "dynamic_dispatch",
                "extractor": "python/v1",
            }
        ],
        "dependencies": [],
    }


@pytest.fixture
def graph_directory(tmp_path, monkeypatch):
    """An activated generation whose stored bytes are `_SOURCE`."""
    from generation_catalog import GenerationCatalog
    from repository_scope import resolve_repository_scope

    from tests.test_evidence_graph_recovery import _publish

    catalog = GenerationCatalog(tmp_path / "state")
    scope = resolve_repository_scope(tmp_path)
    _publish(
        catalog,
        "active",
        graph_records=_graph_records(),
        repository_scope=scope.as_dict(),
    )
    catalog.register("active")
    catalog.activate("active", expected_active=None)
    monkeypatch.setattr(code_graph, "_generation_catalog", lambda directory: catalog)
    monkeypatch.setattr(
        code_graph,
        "_workspace_call_graph",
        lambda directory: (_ for _ in ()).throw(AssertionError("live scan used")),
    )
    return tmp_path


def _reasons(directory: Path) -> dict[str, str]:
    answer = code_graph.find_dead_code(directory, with_report=True)
    return {item["name"]: item["reason"] for item in answer["candidates"]}


def test_a_name_used_only_as_a_value_is_not_called_dead(graph_directory) -> None:
    assert _reasons(graph_directory)["_worker"] == "referenced_without_call"


def test_a_name_only_spelled_in_a_dispatch_table_is_not_called_dead(
    graph_directory,
) -> None:
    assert _reasons(graph_directory)["_by_name"] == "referenced_without_call"


def test_a_name_nothing_names_at_all_is_still_defensibly_dead(graph_directory) -> None:
    assert _reasons(graph_directory)["orphan"] == "zero_confirmed_incoming_calls"


def test_the_answer_says_how_many_symbols_it_dropped_and_why(graph_directory) -> None:
    answer = code_graph.find_dead_code(graph_directory, with_report=True)

    assert set(answer) >= {"excluded_count", "excluded_by_rule"}
    assert answer["reference_parsed_sources"] == 1


def test_module_dependencies_are_answered_from_the_imports_that_exist(
    graph_directory,
) -> None:
    answer = code_graph.find_dependencies("app", graph_directory, with_report=True)

    assert [item["node_id"] for item in answer["dependencies"]] == ["module-dep"]
    assert answer["symbol_resolved"] is True


def test_a_file_path_names_the_module_it_defines(graph_directory) -> None:
    """`mode=dependencies` is asked with `scripts/x.py`, not `scripts.x`."""
    answer = code_graph.find_dependencies("app.py", graph_directory, with_report=True)

    assert [item["node_id"] for item in answer["dependencies"]] == ["module-dep"]


def test_reverse_dependencies_answer_who_imports_a_file(graph_directory) -> None:
    answer = code_graph.find_dependencies(
        "dependency.py", graph_directory, reverse=True, with_report=True
    )

    assert [item["node_id"] for item in answer["dependencies"]] == ["module-app"]


def test_a_private_symbol_is_answered_instead_of_raising(graph_directory) -> None:
    answer = code_graph.find_dependencies("_worker", graph_directory, with_report=True)

    assert [item["node_id"] for item in answer["dependencies"]] == ["helper"]


def test_an_unknown_symbol_says_so_instead_of_answering_empty(graph_directory) -> None:
    answer = code_graph.find_dependencies("absent", graph_directory, with_report=True)

    assert (answer["dependencies"], answer["symbol_resolved"]) == ([], False)


def test_the_architecture_symbol_mode_survives_a_leading_underscore(
    graph_directory, monkeypatch
) -> None:
    import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "_validated_code_directory",
        lambda directory, deadline=None: (graph_directory, None),
    )
    answer = mcp_server._get_architecture_mode(
        str(graph_directory), mode="symbol", symbol="_worker"
    )

    assert "error" not in answer
    assert answer["architecture"]["callees"] != []


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
