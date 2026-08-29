"""Tests for `CODE-09` execution-trace ingestion.

Everything here runs against a temporary state root and a stub graph. No test
touches the live vault, an activated generation, or `run/`.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import trace_ingest  # noqa: E402

TRACE_SCHEMA = trace_ingest.TRACE_SCHEMA


# ---------------------------------------------------------------------------
# Fixtures: a minimal graph with the same shape the real generation has
# ---------------------------------------------------------------------------

_GRAPH_SCHEMA = """
CREATE TABLE source (source_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL);
CREATE TABLE node (node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, metadata_json TEXT NOT NULL);
CREATE TABLE occurrence (
  occurrence_id TEXT PRIMARY KEY, node_id TEXT, source_id TEXT,
  role TEXT NOT NULL, line_start INTEGER NOT NULL
);
CREATE TABLE observation (
  observation_id TEXT PRIMARY KEY, source_node_id TEXT, edge_type TEXT NOT NULL,
  target_text TEXT, reason TEXT NOT NULL
);
"""


class _Scope:
    def __init__(self, root: Path) -> None:
        self.checkout_root = str(root)


class StubGraph:
    """The exact surface `trace_ingest` uses, and nothing else."""

    def __init__(self, root: Path) -> None:
        self.generation_id = "generation-test"
        self.repository_scope = _Scope(root)
        self._database = sqlite3.connect(":memory:")
        self._database.row_factory = sqlite3.Row
        self._database.executescript(_GRAPH_SCHEMA)
        self.closed = False

    def add_definition(self, node_id, path, line, name, owner="pkg.mod", kind="function"):
        metadata = json.dumps({"name": name, "owner": owner, "path": path})
        self._database.execute(
            "INSERT OR IGNORE INTO source VALUES (?, ?)", (f"src:{path}", path)
        )
        self._database.execute(
            "INSERT INTO node VALUES (?, ?, ?)", (node_id, kind, metadata)
        )
        self._database.execute(
            "INSERT INTO occurrence VALUES (?, ?, ?, 'definition', ?)",
            (f"occ:{node_id}", node_id, f"src:{path}", line),
        )
        self._database.commit()

    def add_dispatch_observation(self, observation_id, source_node_id, target_text):
        self._database.execute(
            "INSERT INTO observation VALUES (?, ?, 'CALLS', ?, 'dynamic_dispatch')",
            (observation_id, source_node_id, target_text),
        )
        self._database.commit()

    def find_nodes(self, *, kinds=None, name=None, max_rows=100):
        rows = self._database.execute(
            "SELECT node_id, metadata_json FROM node WHERE kind IN ('function','method')"
        ).fetchall()
        return [
            {"node_id": row["node_id"]}
            for row in rows
            if json.loads(row["metadata_json"]).get("name") == name
        ][:max_rows]

    def node(self, node_id):
        row = self._database.execute(
            "SELECT node_id, metadata_json FROM node WHERE node_id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return {"node_id": row["node_id"], "metadata": json.loads(row["metadata_json"])}

    def occurrences(self, node_id, *, max_rows=100):
        return self._database.execute(
            "SELECT line_start FROM occurrence WHERE node_id = ? LIMIT ?",
            (node_id, max_rows),
        ).fetchall()

    def close(self):
        self.closed = True


@pytest.fixture()
def graph(tmp_path):
    built = StubGraph(tmp_path / "checkout")
    built.add_definition("n:caller", "pkg/a.py", 10, "caller_one")
    built.add_definition("n:callee", "pkg/b.py", 20, "target_method", kind="method")
    return built


@pytest.fixture()
def use_graph(graph, monkeypatch):
    monkeypatch.setattr(trace_ingest, "_open_graph", lambda directory: graph)
    return graph


def write_trace(path: Path, records, schema: str = TRACE_SCHEMA) -> Path:
    lines = [json.dumps({"schema": schema})]
    lines.extend(json.dumps(record) for record in records)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def frame(path, line, name):
    return {"path": path, "line": line, "name": name}


def edge(caller, callee, count=1):
    return {"caller": caller, "callee": callee, "count": count}


# ---------------------------------------------------------------------------
# Bounds: every refusal names itself
# ---------------------------------------------------------------------------


def test_a_trace_over_the_size_ceiling_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_ingest, "MAX_TRACE_BYTES", 64)
    target = tmp_path / "big.jsonl"
    write_trace(target, [edge(frame("pkg/a.py", 10, "x"), frame("pkg/b.py", 20, "y"))])
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_file_too_large"
    assert "64" in str(refusal.value)


def test_the_record_ceiling_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_ingest, "MAX_TRACE_RECORDS", 1)
    records = [
        edge(frame("pkg/a.py", 10, "x"), frame("pkg/b.py", 20, "y")),
        edge(frame("pkg/a.py", 11, "z"), frame("pkg/b.py", 21, "w")),
    ]
    target = write_trace(tmp_path / "many.jsonl", records)
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_record_limit_exceeded"


def test_an_unknown_schema_is_refused_by_name(tmp_path):
    target = write_trace(tmp_path / "t.jsonl", [], schema="execution-trace/v99")
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_schema_unknown"


def test_an_empty_trace_is_refused_by_name(tmp_path):
    target = tmp_path / "empty.jsonl"
    target.write_text("", encoding="utf-8")
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_file_empty"


def test_a_line_that_is_not_json_is_refused_by_name(tmp_path):
    target = tmp_path / "t.jsonl"
    target.write_text(json.dumps({"schema": TRACE_SCHEMA}) + "\nnot json\n", encoding="utf-8")
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_line_not_json"


# ---------------------------------------------------------------------------
# Safety: a trace file is untrusted input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_path",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "pkg/../../outside.py",
        "C:\\Windows\\system32.py",
        "pkg\\a.py",
    ],
)
def test_a_frame_path_outside_the_repository_is_refused_by_name(tmp_path, hostile_path):
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame(hostile_path, 1, "x"), frame("pkg/b.py", 20, "y"))],
    )
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_frame_invalid"


def test_ingestion_never_opens_a_path_the_trace_names(tmp_path, use_graph, monkeypatch):
    """Paths are dictionary keys, never filesystem operands."""
    opened: list[str] = []
    real_open = Path.open

    def watched(self, *args, **kwargs):
        opened.append(str(self))
        return real_open(self, *args, **kwargs)

    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    monkeypatch.setattr(Path, "open", watched)
    trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    assert all(Path(name).name != "a.py" for name in opened)
    assert all(Path(name).name != "b.py" for name in opened)


def test_a_negative_or_zero_call_count_is_refused_by_name(tmp_path):
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "x"), frame("pkg/b.py", 20, "y"), count=0)],
    )
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.read_trace_records(target)
    assert refusal.value.reason == "trace_count_invalid"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def _stored(state_root: Path):
    database = sqlite3.connect(trace_ingest.store_path(state_root))
    with database:
        traces = database.execute("SELECT count(*) FROM trace").fetchone()[0]
        edges = database.execute("SELECT count(*) FROM trace_edge").fetchone()[0]
        total = database.execute("SELECT sum(call_count) FROM trace_edge").fetchone()[0]
    database.close()
    return traces, edges, total


def test_ingesting_the_same_trace_twice_does_not_double_anything(tmp_path, use_graph):
    target = write_trace(
        tmp_path / "t.jsonl",
        [
            edge(
                frame("pkg/a.py", 10, "caller_one"),
                frame("pkg/b.py", 20, "target_method"),
                count=7,
            )
        ],
    )
    first = trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    after_first = _stored(tmp_path)
    second = trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    assert first["trace_digest"] == second["trace_digest"]
    assert _stored(tmp_path) == after_first == (1, 1, 7)


def test_a_second_distinct_trace_still_contributes(tmp_path, use_graph):
    first = write_trace(
        tmp_path / "one.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"), 3)],
    )
    second = write_trace(
        tmp_path / "two.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"), 4)],
    )
    trace_ingest.ingest_trace(first, tmp_path, state_root=tmp_path)
    trace_ingest.ingest_trace(second, tmp_path, state_root=tmp_path)
    assert _stored(tmp_path) == (2, 2, 7)
    answer = trace_ingest.trace_callers("target_method", tmp_path, state_root=tmp_path)
    assert answer["trace_callers"][0]["call_count"] == 7
    assert answer["trace_callers"][0]["trace_count"] == 2


# ---------------------------------------------------------------------------
# Binding: exact join, then the bounded decorator window
# ---------------------------------------------------------------------------


def test_an_undecorated_frame_binds_on_the_exact_definition_line(graph):
    index = trace_ingest.build_definition_index(graph)
    decorated = trace_ingest.build_decorated_index(index)
    assert trace_ingest._resolve_frame(("pkg/a.py", 10, "caller_one"), index, decorated) == "n:caller"


def test_a_decorated_frame_binds_through_the_decorator_window(graph):
    """`co_firstlineno` is the first decorator's line, measured 2026-08-29."""
    index = trace_ingest.build_definition_index(graph)
    decorated = trace_ingest.build_decorated_index(index)
    # The profiler reports line 8; the `def` this graph stores is line 10.
    assert trace_ingest._resolve_frame(("pkg/a.py", 8, "caller_one"), index, decorated) == "n:caller"


def test_a_frame_below_the_definition_never_binds(graph):
    """A decorator is above its `def`, so the window opens forwards only."""
    index = trace_ingest.build_definition_index(graph)
    decorated = trace_ingest.build_decorated_index(index)
    assert trace_ingest._resolve_frame(("pkg/a.py", 11, "caller_one"), index, decorated) is None


def test_a_frame_beyond_the_decorator_window_stays_unbound(graph):
    index = trace_ingest.build_definition_index(graph)
    decorated = trace_ingest.build_decorated_index(index)
    far = 10 - trace_ingest.MAX_DECORATOR_LINES - 1
    assert trace_ingest._resolve_frame(("pkg/a.py", far, "caller_one"), index, decorated) is None


def test_a_frame_matching_two_definitions_in_the_window_stays_unbound(graph):
    """A wrong caller is worse than an unbound frame, so ambiguity refuses."""
    graph.add_definition("n:twin", "pkg/a.py", 12, "caller_one")
    index = trace_ingest.build_definition_index(graph)
    decorated = trace_ingest.build_decorated_index(index)
    assert trace_ingest._resolve_frame(("pkg/a.py", 9, "caller_one"), index, decorated) is None


def test_unbindable_frames_are_counted_not_dropped_silently(tmp_path, use_graph):
    target = write_trace(
        tmp_path / "t.jsonl",
        [
            edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method")),
            edge(frame("pkg/zzz.py", 99, "ghost"), frame("pkg/b.py", 20, "target_method")),
        ],
    )
    report = trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    assert report["record_count"] == 2
    assert report["edge_count"] == 1
    assert report["unbound_count"] == 1


# ---------------------------------------------------------------------------
# The read path: trace evidence is labelled and never merged
# ---------------------------------------------------------------------------


def test_trace_callers_are_labelled_as_dynamic_evidence(tmp_path, use_graph):
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    answer = trace_ingest.trace_callers("target_method", tmp_path, state_root=tmp_path)
    assert answer["trace_caller_count"] == 1
    row = answer["trace_callers"][0]
    assert row["evidence"] == "execution-trace"
    assert row["qualified_name"] == "pkg.mod.caller_one"
    assert answer["trace_evidence_is_dynamic"] is True


def test_trace_callers_are_never_merged_into_static_callers(tmp_path, use_graph):
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    static = {"callers": [], "unresolved_caller_count": 3, "source_generation": "g"}
    merged = trace_ingest.with_trace_callers(
        static, "target_method", tmp_path, state_root=tmp_path
    )
    assert merged["callers"] == []
    assert merged["unresolved_caller_count"] == 3
    assert len(merged["trace_callers"]) == 1


def test_an_edge_whose_caller_left_the_generation_is_counted_stale(tmp_path, use_graph):
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    use_graph._database.execute("DELETE FROM node WHERE node_id = 'n:caller'")
    use_graph._database.commit()
    answer = trace_ingest.trace_callers("target_method", tmp_path, state_root=tmp_path)
    assert answer["trace_callers"] == []
    assert answer["trace_stale_edges"] == 1


def test_reading_without_an_active_generation_is_empty_not_an_error(tmp_path, monkeypatch):
    """`find_callers` degrades to a live scan here, so the merge must not raise."""
    monkeypatch.setattr(trace_ingest, "_open_graph", lambda directory: None)
    answer = trace_ingest.trace_callers("target_method", tmp_path, state_root=tmp_path)
    assert answer["trace_caller_count"] == 0
    assert answer["trace_generation"] is None
    merged = trace_ingest.with_trace_callers(
        {"callers": [], "fallback": True}, "target_method", tmp_path, state_root=tmp_path
    )
    assert merged["callers"] == []


def test_ingesting_without_an_active_generation_is_refused_by_name(tmp_path, monkeypatch):
    """Reading degrades; writing must not, because nothing could bind."""
    monkeypatch.setattr(trace_ingest, "_open_graph", lambda directory: None)
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    with pytest.raises(trace_ingest.TraceRefused) as refusal:
        trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    assert refusal.value.reason == "no_active_generation"


def test_reading_without_a_store_is_empty_not_an_error(tmp_path, use_graph):
    answer = trace_ingest.trace_callers("target_method", tmp_path, state_root=tmp_path)
    assert answer["trace_caller_count"] == 0
    assert answer["trace_callers"] == []


# ---------------------------------------------------------------------------
# Placement and measurement
# ---------------------------------------------------------------------------


def test_the_store_lives_in_disposable_cache_never_in_run(tmp_path):
    path = trace_ingest.store_path(tmp_path)
    assert path.relative_to(tmp_path).parts[0] == "cache"
    assert "run" not in path.relative_to(tmp_path).parts


def test_dispatch_coverage_counts_only_observations_the_trace_saw(tmp_path, use_graph):
    use_graph.add_dispatch_observation("o:1", "n:caller", "registry.target_method")
    use_graph.add_dispatch_observation("o:2", "n:caller", "registry.something_else")
    before = trace_ingest.dispatch_resolution(tmp_path, state_root=tmp_path)
    assert before["dispatch_observations"] == 2
    assert before["covered_by_traces"] == 0
    target = write_trace(
        tmp_path / "t.jsonl",
        [edge(frame("pkg/a.py", 10, "caller_one"), frame("pkg/b.py", 20, "target_method"))],
    )
    trace_ingest.ingest_trace(target, tmp_path, state_root=tmp_path)
    after = trace_ingest.dispatch_resolution(tmp_path, state_root=tmp_path)
    assert after["covered_by_traces"] == 1
