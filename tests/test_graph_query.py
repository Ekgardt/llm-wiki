"""CODE-01: the bounded multi-hop pipeline refuses by name and invents nothing."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import code_graph  # noqa: E402
import graph_query  # noqa: E402

FAR_DEADLINE = time.monotonic() + 3600.0


def _node(node_id: str, name: str, path: str = "pkg/mod.py") -> dict:
    return {
        "node_id": node_id,
        "kind": "function",
        "metadata": {"name": name, "path": path, "owner": "pkg.mod"},
    }


def _field_ok(wanted: str | None, actual: object) -> bool:
    return wanted is None or actual == wanted


class _FakeGraph:
    """CALLS: a -> b -> c, and d -> b. Refusals injected by node id."""

    generation_id = "generation-test"

    def __init__(self) -> None:
        self.closed = False
        self.refuse: set[str] = set()
        self.nodes = {
            node["node_id"]: node
            for node in (
                _node("n:a", "alpha"),
                _node("n:b", "beta"),
                _node("n:c", "gamma"),
                _node("n:d", "delta", path="pkg/other.py"),
            )
        }
        self.calls = [("n:a", "n:b"), ("n:b", "n:c"), ("n:d", "n:b")]

    def _matches(self, node: dict, kinds, name, path) -> bool:
        kind_ok = kinds is None or node["kind"] in kinds
        metadata = node["metadata"]
        return (
            kind_ok
            and _field_ok(name, metadata.get("name"))
            and _field_ok(path, metadata.get("path"))
        )

    def find_nodes(self, *, kinds=None, name=None, path=None, max_rows=100, deadline=None):
        rows = [
            dict(node)
            for node in self.nodes.values()
            if self._matches(node, kinds, name, path)
        ]
        if len(rows) > max_rows:
            raise ValueError("Evidence Graph query row ceiling exceeded")
        return rows

    def _ends(self, node_id: str, direction: str) -> list[str]:
        if direction == "out":
            return [t for s, t in self.calls if s == node_id]
        return [s for s, t in self.calls if t == node_id]

    def neighbors(self, node_id, *, direction, edge_types, **_bounds):
        if node_id in self.refuse:
            raise ValueError("Evidence Graph query row ceiling exceeded")
        found = self._ends(node_id, direction) if "CALLS" in edge_types else []
        return [dict(self.nodes[end]) for end in found]

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_graph(monkeypatch) -> _FakeGraph:
    graph = _FakeGraph()
    monkeypatch.setattr(code_graph, "_active_evidence_graph", lambda _d: graph)
    return graph


def _run(query: dict | str, deadline: float = FAR_DEADLINE) -> dict:
    import json

    text = query if isinstance(query, str) else json.dumps(query)
    return graph_query.run_graph_query(Path("/x"), text, deadline)


class TestParseRefusalsAreNamed:
    def test_non_json_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not valid JSON"):
            graph_query.parse_graph_query("callers of X")

    def test_a_non_object_query_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must be a JSON object"):
            graph_query.parse_graph_query("[1, 2]")

    def test_unknown_top_level_keys_are_refused(self) -> None:
        with pytest.raises(ValueError, match="unknown keys: cypher"):
            graph_query.parse_graph_query('{"start": {"name": "x"}, "cypher": "y"}')

    def test_an_empty_start_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one of: name, path, kind"):
            graph_query.parse_graph_query('{"start": {}}')

    def test_an_unserved_edge_names_the_served_set(self) -> None:
        query = '{"start": {"name": "x"}, "hops": [{"edge": "writes_to", "direction": "in"}]}'
        with pytest.raises(ValueError, match="not served; served edges: calls"):
            graph_query.parse_graph_query(query)

    def test_a_bad_direction_is_refused(self) -> None:
        query = '{"start": {"name": "x"}, "hops": [{"edge": "calls", "direction": "up"}]}'
        with pytest.raises(ValueError, match="direction must be 'in' or 'out'"):
            graph_query.parse_graph_query(query)

    def test_more_than_max_hops_is_refused(self) -> None:
        hop = '{"edge": "calls", "direction": "in"}'
        query = f'{{"start": {{"name": "x"}}, "hops": [{hop}, {hop}, {hop}, {hop}]}}'
        with pytest.raises(ValueError, match="at most 3 hops"):
            graph_query.parse_graph_query(query)

    def test_limit_bounds_are_enforced(self) -> None:
        with pytest.raises(ValueError, match="between 1 and 200"):
            graph_query.parse_graph_query('{"start": {"name": "x"}, "limit": 201}')
        with pytest.raises(ValueError, match="must be an integer"):
            graph_query.parse_graph_query('{"start": {"name": "x"}, "limit": true}')

    def test_an_oversized_query_is_refused_before_parsing(self) -> None:
        text = '{"start": {"name": "' + "x" * graph_query.MAX_QUERY_BYTES + '"}}'
        with pytest.raises(ValueError, match="exceeds"):
            graph_query.parse_graph_query(text)


class TestBoundedExecution:
    def test_one_hop_in_returns_the_callers(self, fake_graph: _FakeGraph) -> None:
        answer = _run(
            {"start": {"name": "beta"}, "hops": [{"edge": "calls", "direction": "in"}]}
        )
        names = sorted(node["name"] for node in answer["nodes"])
        assert answer["status"] == "ok"
        assert names == ["alpha", "delta"]

    def test_two_hops_out_reach_only_the_second_ring(self, fake_graph: _FakeGraph) -> None:
        hops = [{"edge": "calls", "direction": "out"}] * 2
        answer = _run({"start": {"name": "alpha"}, "hops": hops})
        assert [node["name"] for node in answer["nodes"]] == ["gamma"]
        assert answer["hops_applied"] == 2

    def test_a_start_only_query_lists_matching_nodes(self, fake_graph: _FakeGraph) -> None:
        answer = _run({"start": {"path": "pkg/mod.py", "kind": "function"}})
        assert len(answer["nodes"]) == 3
        assert answer["hops_applied"] == 0

    def test_an_unknown_name_is_empty_not_invented(self, fake_graph: _FakeGraph) -> None:
        answer = _run(
            {"start": {"name": "missing"}, "hops": [{"edge": "calls", "direction": "in"}]}
        )
        assert answer["status"] == "ok"
        assert answer["nodes"] == []

    def test_the_graph_is_closed_after_the_answer(self, fake_graph: _FakeGraph) -> None:
        _run({"start": {"name": "alpha"}})
        assert fake_graph.closed is True

    def test_no_active_generation_is_a_named_answer(self, monkeypatch) -> None:
        monkeypatch.setattr(code_graph, "_active_evidence_graph", lambda _d: None)
        answer = _run({"start": {"name": "alpha"}})
        assert answer == {
            "status": "error",
            "error": "no_active_generation",
            "note": graph_query.QUERY_NOTE,
        }


class TestCeilingsRefuseByName:
    def test_a_start_overflow_is_a_named_refusal(self, fake_graph: _FakeGraph) -> None:
        with pytest.raises(ValueError, match="start matches more than 1 nodes"):
            _run({"start": {"kind": "function"}, "limit": 1})

    def test_a_non_ceiling_engine_error_is_not_relabelled(self, monkeypatch) -> None:
        graph = _FakeGraph()

        def _refuse_kind(**_kwargs):
            raise ValueError("kind is outside the controlled kind set")

        graph.find_nodes = _refuse_kind
        monkeypatch.setattr(code_graph, "_active_evidence_graph", lambda _d: graph)
        with pytest.raises(ValueError, match="controlled kind set"):
            _run({"start": {"kind": "nonsense"}})

    def test_a_refused_expansion_is_recorded_not_silent(self, fake_graph: _FakeGraph) -> None:
        fake_graph.refuse.add("n:d")
        answer = _run(
            {"start": {"name": "delta"}, "hops": [{"edge": "calls", "direction": "out"}]}
        )
        assert answer["nodes"] == []
        assert answer["refused_expansions"] == [
            {
                "hop": 0,
                "refused_node_ids": ["n:d"],
                "refused_count": 1,
                "reason": "engine row or work ceiling exceeded",
            }
        ]

    def test_frontier_truncation_is_named(self, fake_graph: _FakeGraph) -> None:
        answer = _run(
            {
                "start": {"name": "beta"},
                "hops": [{"edge": "calls", "direction": "in"}],
                "limit": 1,
            }
        )
        assert len(answer["nodes"]) == 1
        assert answer["frontier_truncated"] is True

    def test_an_expired_deadline_stops_the_walk(self, fake_graph: _FakeGraph) -> None:
        query = {"start": {"name": "beta"}, "hops": [{"edge": "calls", "direction": "in"}]}
        with pytest.raises(TimeoutError, match="graph query deadline reached"):
            _run(query, deadline=time.monotonic() - 1.0)


class TestModeWiring:
    def test_the_mode_is_declared_and_contracted(self) -> None:
        import mcp_server

        enum = mcp_server.TOOL_INPUT_SCHEMAS["get_architecture"]["properties"]["mode"]["enum"]
        assert "query" in enum
        assert mcp_server._ARCHITECTURE_CONTRACTS["query"] == (
            {"directory", "mode", "query"},
            {"directory", "mode", "query"},
        )

    def test_a_valid_call_passes_argument_validation(self) -> None:
        import mcp_server

        arguments = {"directory": "/x", "mode": "query", "query": '{"start": {"name": "a"}}'}
        assert mcp_server._validate_tool_arguments("get_architecture", arguments) is None

    def test_a_call_without_the_query_argument_is_refused(self) -> None:
        import mcp_server

        error = mcp_server._validate_tool_arguments(
            "get_architecture", {"directory": "/x", "mode": "query"}
        )
        assert error == "required arguments are missing for query: query"

    def test_a_query_call_refuses_stray_arguments(self) -> None:
        import mcp_server

        error = mcp_server._validate_tool_arguments(
            "get_architecture",
            {"directory": "/x", "mode": "query", "query": "{}", "symbol": "a"},
        )
        assert error == "arguments are not valid for query: symbol"
