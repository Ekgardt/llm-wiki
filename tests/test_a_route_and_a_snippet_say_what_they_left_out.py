"""A cross-service route hint and a snippet answer never drop a match silently (law 9).

`code_hints.find_routes` kept the first five checkouts serving a route and dropped
the rest without a mark; `symbol_snippet` kept five definitions and five nodes the
same way. See `docs/research/2026-09-27-a-cut-says-what-it-left-out.md`.
"""

from __future__ import annotations

import time
from pathlib import Path

import code_hints
import symbol_snippet

SERVICES = 7


def _meta(checkout_id: str) -> dict[str, str]:
    return {
        "schema_version": code_hints.SCHEMA_VERSION,
        "generation_id": "generation-x",
        "repository_id": "repository:" + "0" * 64,
        "checkout_id": checkout_id,
        "checkout_root": "/nowhere",
        "git_commit": "",
        "symbols": "0",
        "written_at": "0",
    }


def _service(state_root: Path, index: int) -> None:
    checkout_id = "checkout:" + f"{index:x}" * 64
    route = ("GET", "/users", f"handler_{index}", "api.py", index + 1)
    code_hints.write_hints(state_root, _meta(checkout_id), [], [route])


def test_every_checkout_serving_a_route_is_named(tmp_path: Path) -> None:
    for index in range(1, SERVICES + 1):
        _service(tmp_path, index)

    found = code_hints.find_routes(tmp_path, "GET", "/users")

    assert sorted(match["handler"] for match in found) == [f"handler_{index}" for index in range(1, SERVICES + 1)]


class _Graph:
    """Nodes with no stored definition, so each answers from its file on disk."""

    generation_id = "generation-test"

    def __init__(self, count: int) -> None:
        self.rows = [
            {"node_id": f"n{index}", "kind": "function", "identity_key": "frob",
             "metadata": {"name": "frob", "owner": "", "path": f"mod{index}.py"}}
            for index in range(count)
        ]

    def find_nodes(self, **_options):
        return self.rows

    def occurrences(self, _node_id, **_options):
        return []


def _modules(root: Path, count: int, definitions: int) -> None:
    for index in range(count):
        body = "".join(f"def frob():\n    return {line}\n\n" for line in range(definitions))
        (root / f"mod{index}.py").write_text(body, encoding="utf-8")


def test_a_file_reports_every_definition_line() -> None:
    lines = "def frob():\n    pass\n".splitlines() * SERVICES

    assert len(symbol_snippet.definition_lines(lines, "frob")) == SERVICES


def test_a_snippet_answer_counts_the_nodes_and_definitions_it_leaves_out(tmp_path: Path) -> None:
    _modules(tmp_path, SERVICES, 2)

    answer = symbol_snippet.graph_snippets(_Graph(SERVICES), tmp_path, "frob", time.monotonic() + 30)

    shown = symbol_snippet.MAX_LOCATIONS
    assert len(answer["snippets"]) == shown
    assert (answer["resolved_nodes"], answer["nodes_omitted"]) == (SERVICES, SERVICES - shown)
    assert answer["snippets_omitted"] == 2 * shown - shown
