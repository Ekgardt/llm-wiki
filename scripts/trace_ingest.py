#!/usr/bin/env python3
"""Ingest `execution-trace/v1` edges and serve them beside the static graph.

`CODE-09`. The static extractor leaves an *observation*, never a CALLS
assertion, wherever a call reaches its target through a value it cannot type.
Measured on this repository's live generation, that is the largest single
population in the graph. A runtime trace resolves exactly those receivers,
because the program itself decided them.

Three contracts govern where this may put anything, and they are not
negotiable (`CLAUDE.md` section 1):

* A generation is immutable after activation, and query-time observations are
  never written into one. A trace is a runtime observation, so nothing here
  ever opens a generation for writing.
* `cache/` is disposable derived state. The trace store lives there, beside
  `cache/evidence-graph/`, and deleting it loses only traces.
* Markdown, Git and project journals remain the only authority.

Trace edges are therefore stored in a sidecar and read *alongside* the active
generation, never merged into it -- and they are served in a field of their own
(`trace_callers`), never in `callers`. This vault's own rule is that a zero must
say which kind of zero it is (`NEW-124`, `NEW-135`); an edge must likewise say
how it was learned. A trace proves that this caller called this callee in one
run. It does not prove reachability in general, and its *absences* mean
nothing at all -- so trace evidence is additive only, and must never feed a
dead-code answer. See
`docs/research/2026-08-28-what-an-execution-trace-proves.md`.

The trace file is untrusted input. It is parsed with `json.loads` and nothing
else; no deserialiser here can execute anything. Every path inside it is
checked to be repository-relative and is then used only as a dictionary key --
this module never opens a path a trace names. Producing the file from a
`cProfile` profile (which is `marshal`, and unsafe on hostile input) is the
separate, explicitly trusted job of `scripts/trace_collect.py`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

TRACE_SCHEMA = "execution-trace/v1"
STORE_SCHEMA_VERSION = "execution-traces/v1"

# Ceiling on one accepted trace file. Sized from measurement, not taste
# (2026-08-28): a `cProfile` run of ten of this repository's test modules --
# 570 tests, 98 s, the broadest single-process trace this product can
# realistically ask an operator for -- produced 15,271 edges in a 3.54 MB
# `execution-trace/v1` file. 64 MB leaves ~18x headroom, and is also the memory
# bound, because the parsed records are held in one list to be bound in a
# single pass. A larger trace is refused by name (`trace_file_too_large`)
# rather than silently truncated.
MAX_TRACE_BYTES = 64 * 1024 * 1024
# Record ceiling, from the same measurement: 15,271 edges at ~20x headroom.
MAX_TRACE_RECORDS = 300_000
MAX_PATH_CHARS = 512
MAX_NAME_CHARS = 256
MAX_LINE_NUMBER = 10_000_000
MAX_CALL_COUNT = 2**31 - 1
# Bound on the sample of trace-derived callers one answer names. Mirrors
# `code_graph.UNRESOLVED_CALLER_LIMIT`, and for the same reason: the count
# stays exact above it, so a cut list still states how much it is missing.
MAX_TRACE_CALLERS = 200
# Node-id filter bound, mirroring `evidence_graph.MAX_NODE_FILTER`: this
# repository's worst same-name collision is `__init__` at 296, and 512 stays
# under the historic SQLite 999 host-parameter floor.
MAX_NODE_FILTER = 512
IO_CHUNK_BYTES = 64 * 1024

_FRAME_KEYS = frozenset({"path", "line", "name"})
_RECORD_KEYS = frozenset({"caller", "callee", "count"})
# An absolute POSIX path, a Windows drive letter, a backslash, or a NUL.
_UNSAFE_PATH = re.compile(r"^/|^[A-Za-z]:|\\|\x00")

_STORE_SCHEMA = """
CREATE TABLE trace (
  trace_digest TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  generation_id TEXT NOT NULL,
  source_name TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
  record_count INTEGER NOT NULL CHECK (record_count >= 0),
  edge_count INTEGER NOT NULL CHECK (edge_count >= 0),
  unbound_count INTEGER NOT NULL CHECK (unbound_count >= 0),
  ingested_at TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE trace_edge (
  trace_digest TEXT NOT NULL REFERENCES trace(trace_digest),
  caller_node_id TEXT NOT NULL,
  callee_node_id TEXT NOT NULL,
  call_count INTEGER NOT NULL CHECK (call_count > 0),
  PRIMARY KEY (trace_digest, caller_node_id, callee_node_id)
) WITHOUT ROWID;
CREATE INDEX trace_edge_reverse ON trace_edge(callee_node_id, caller_node_id);
"""


class TraceRefused(ValueError):
    """A refusal that names itself, so a rejected trace says which bound broke."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(" ".join(part for part in (reason, detail) if part))


# --------------------------------------------------------------------------
# Untrusted record validation
# --------------------------------------------------------------------------


def _plain_text(value: object, limit: int) -> bool:
    if not isinstance(value, str):
        return False
    return 0 < len(value) <= limit and "\x00" not in value


def _valid_relative_path(value: object) -> bool:
    """A repository-relative POSIX path that cannot escape the repository."""
    if not _plain_text(value, MAX_PATH_CHARS):
        return False
    if _UNSAFE_PATH.search(str(value)):
        return False
    parts = Path(str(value)).parts
    return ".." not in parts and "." not in parts


def _bounded_integer(value: object, maximum: int) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return 1 <= value <= maximum


def _valid_frame(frame: object) -> bool:
    if not isinstance(frame, dict) or set(frame) != _FRAME_KEYS:
        return False
    if not _valid_relative_path(frame["path"]):
        return False
    return _bounded_integer(frame["line"], MAX_LINE_NUMBER) and _plain_text(
        frame["name"], MAX_NAME_CHARS
    )


def _frame_key(frame: dict) -> tuple[str, int, str]:
    return (str(frame["path"]), int(frame["line"]), str(frame["name"]))


def _require_record_shape(record: object) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise TraceRefused(
            "trace_record_invalid", "record keys are not caller/callee/count"
        )
    if _valid_frame(record["caller"]) and _valid_frame(record["callee"]):
        return
    raise TraceRefused(
        "trace_frame_invalid",
        "a frame is not a repository-relative path, definition line and name",
    )


def _validated_record(record: object) -> tuple[tuple, tuple, int]:
    _require_record_shape(record)
    assert isinstance(record, dict)
    if not _bounded_integer(record["count"], MAX_CALL_COUNT):
        raise TraceRefused(
            "trace_count_invalid", "count is not a positive bounded integer"
        )
    return (
        _frame_key(record["caller"]),
        _frame_key(record["callee"]),
        int(record["count"]),
    )


# --------------------------------------------------------------------------
# Reading one bounded trace file
# --------------------------------------------------------------------------


def _require_trace_size(path: Path) -> int:
    size = path.stat().st_size
    if size > MAX_TRACE_BYTES:
        raise TraceRefused(
            "trace_file_too_large", f"{size} bytes exceeds the {MAX_TRACE_BYTES} ceiling"
        )
    return size


def _decoded(line: str, number: int) -> dict:
    try:
        value = json.loads(line)
    except ValueError as error:
        raise TraceRefused("trace_line_not_json", f"line {number}: {error}") from None
    if isinstance(value, dict):
        return value
    raise TraceRefused("trace_line_not_object", f"line {number} is not a JSON object")


def _require_header(handle) -> None:
    first = next(handle, "")
    if not first.strip():
        raise TraceRefused("trace_file_empty", "the schema header line is missing")
    if _decoded(first, 1).get("schema") == TRACE_SCHEMA:
        return
    raise TraceRefused("trace_schema_unknown", f"expected schema {TRACE_SCHEMA}")


def _require_record_budget(count: int, number: int) -> None:
    if count < MAX_TRACE_RECORDS:
        return
    raise TraceRefused(
        "trace_record_limit_exceeded",
        f"line {number} passes the {MAX_TRACE_RECORDS} record ceiling",
    )


def _collected_records(handle) -> list[tuple[tuple, tuple, int]]:
    records: list[tuple[tuple, tuple, int]] = []
    for number, line in enumerate(handle, start=2):
        if not line.strip():
            continue
        _require_record_budget(len(records), number)
        records.append(_validated_record(_decoded(line, number)))
    return records


def read_trace_records(path: Path) -> list[tuple[tuple, tuple, int]]:
    """Parse one bounded trace file into validated (caller, callee, count) keys."""
    source = Path(path)
    _require_trace_size(source)
    try:
        with source.open("r", encoding="utf-8", errors="strict") as handle:
            _require_header(handle)
            return _collected_records(handle)
    except UnicodeDecodeError as error:
        raise TraceRefused("trace_not_utf8", str(error)) from None


def trace_digest(path: Path) -> str:
    """SHA-256 of the trace file, which is what makes ingestion idempotent."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(IO_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Binding frames to graph nodes
# --------------------------------------------------------------------------


def _graph_database(graph):
    """The generation's read connection.

    `code_graph._store_report` reads the same connection the same way; the
    facade exposes no bulk reader, and one pass over 23,253 definition
    occurrences is far cheaper than a query per distinct frame.
    """
    return graph._database


def _definition_rows(graph) -> list:
    return _graph_database(graph).execute(
        "SELECT s.relative_path, o.line_start, n.metadata_json, n.node_id "
        "FROM occurrence o JOIN source s USING(source_id) JOIN node n USING(node_id) "
        "WHERE o.role = 'definition' AND n.kind IN ('function', 'method')"
    ).fetchall()


def _definition_key(row) -> tuple[str, int, str]:
    name = json.loads(row[2]).get("name", "")
    return (str(row[0]), int(row[1]), str(name))


def build_definition_index(graph) -> dict[tuple[str, int, str], str]:
    """(path, definition line, name) -> node id.

    A `cProfile` key carries `co_firstlineno`, and this graph stores the `def`
    line as the definition occurrence, so for an undecorated function the join
    is exact. Measured on the live generation, this key is unique across all
    20,746 function and method definitions -- zero collisions -- so an exact hit
    needs no tie-break. A key that did name two nodes is dropped: it cannot bind
    anything unambiguously.
    """
    grouped: dict[tuple[str, int, str], set[str]] = {}
    for row in _definition_rows(graph):
        grouped.setdefault(_definition_key(row), set()).add(str(row[3]))
    return {key: ids.pop() for key, ids in grouped.items() if len(ids) == 1}


def build_decorated_index(index: dict) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """(path, name) -> sorted (definition line, node id), for the decorator window.

    `co_firstlineno` is the **first decorator's** line, not the `def` line
    (measured 2026-08-29, Python 3.12.3), so a decorated function's frame key
    sits a few lines above the definition this graph stores. Undecorated
    functions bind exactly and never reach this map.
    """
    grouped: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for (path, line, name), node_id in index.items():
        grouped.setdefault((path, name), []).append((line, node_id))
    return {key: sorted(values) for key, values in grouped.items()}


# How far below a frame line a `def` may sit and still be the same function.
# A decorator line is always <= the `def` line, so the window opens forwards
# only; searching backwards could bind a frame to a definition that ends before
# it starts. 32 absorbs any real decorator stack in this repository without
# turning an exact join into a nearest-neighbour search.
MAX_DECORATOR_LINES = 32


def _windowed_match(candidates: list[tuple[int, str]], line: int) -> str | None:
    """The one definition within the decorator window, or None if not unique."""
    hits = [
        node_id
        for definition_line, node_id in candidates
        if line <= definition_line <= line + MAX_DECORATOR_LINES
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _resolve_frame(key: tuple[str, int, str], index: dict, decorated: dict) -> str | None:
    """Exact `(path, line, name)` first; then the bounded decorator window."""
    exact = index.get(key)
    if exact is not None:
        return exact
    path, line, name = key
    return _windowed_match(decorated.get((path, name), []), line)


def _accumulate(edges: dict, pair: tuple | None, count: int) -> int:
    """Add one bound edge. Returns 1 when the pair could not be bound."""
    if pair is None:
        return 1
    edges[pair] = edges.get(pair, 0) + count
    return 0


def _bound_pair(caller_key, callee_key, index, decorated) -> tuple[str, str] | None:
    caller = _resolve_frame(caller_key, index, decorated)
    callee = _resolve_frame(callee_key, index, decorated)
    if caller is None or callee is None:
        return None
    return (caller, callee)


def bind_records(records, index) -> tuple[dict[tuple[str, str], int], int]:
    """Fold validated records into node-id edges plus an unbound count."""
    decorated = build_decorated_index(index)
    edges: dict[tuple[str, str], int] = {}
    unbound = 0
    for caller_key, callee_key, count in records:
        pair = _bound_pair(caller_key, callee_key, index, decorated)
        unbound += _accumulate(edges, pair, count)
    return edges, unbound


# --------------------------------------------------------------------------
# The disposable sidecar store
# --------------------------------------------------------------------------


def _default_state_root() -> Path:
    """Mirror `code_graph._generation_state_root` so the store sits beside it."""
    configured = os.environ.get("LLM_WIKI_STATE_ROOT")
    if configured:
        return Path(configured).resolve()
    try:
        from . import memory_state
    except ImportError:
        import memory_state
    return memory_state.STATE_ROOT


def _resolved_state_root(state_root: Path | None) -> Path:
    if state_root is None:
        return _default_state_root()
    return Path(state_root).resolve()


def store_path(state_root: Path | None = None) -> Path:
    """Where trace edges live: disposable derived cache, never `run/`."""
    root = _resolved_state_root(state_root)
    return root / "cache" / "execution-traces" / "traces.sqlite3"


def _ensure_schema(database: sqlite3.Connection) -> None:
    present = database.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='trace'"
    ).fetchone()[0]
    if present:
        return
    database.executescript(_STORE_SCHEMA)


def _configure(database: sqlite3.Connection) -> sqlite3.Connection:
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("PRAGMA synchronous=FULL")
    database.execute("PRAGMA foreign_keys=ON")
    return database


def open_store(state_root: Path | None = None) -> sqlite3.Connection:
    """Open (creating if absent) the trace store under `cache/`."""
    path = store_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    database = _configure(sqlite3.connect(path))
    _ensure_schema(database)
    return database


def _read_only_store(state_root: Path | None = None) -> sqlite3.Connection | None:
    path = store_path(state_root)
    if not path.is_file():
        return None
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    return database


def _edge_rows(digest: str, edges: dict) -> list[tuple]:
    return [
        (digest, caller, callee, count)
        for (caller, callee), count in sorted(edges.items())
    ]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _trace_row(digest: str, report: dict) -> tuple:
    return (
        digest,
        STORE_SCHEMA_VERSION,
        report["generation_id"],
        report["source_name"],
        report["byte_size"],
        report["record_count"],
        report["edge_count"],
        report["unbound_count"],
        _now(),
    )


def _replace_trace(database, digest: str, report: dict, edges: dict) -> None:
    """Idempotent by digest: the same trace file replaces, never accumulates."""
    with database:
        database.execute("DELETE FROM trace_edge WHERE trace_digest = ?", (digest,))
        database.execute("DELETE FROM trace WHERE trace_digest = ?", (digest,))
        database.execute(
            "INSERT INTO trace (trace_digest, schema_version, generation_id, "
            "source_name, byte_size, record_count, edge_count, unbound_count, "
            "ingested_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _trace_row(digest, report),
        )
        database.executemany(
            "INSERT INTO trace_edge (trace_digest, caller_node_id, callee_node_id, "
            "call_count) VALUES (?, ?, ?, ?)",
            _edge_rows(digest, edges),
        )


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


def _open_graph(directory: Path):
    """The active generation, or None. Never opened for writing."""
    try:
        from .code_graph import _active_evidence_graph
    except ImportError:
        from code_graph import _active_evidence_graph
    return _active_evidence_graph(Path(directory))


def _active_graph(directory: Path):
    graph = _open_graph(directory)
    if graph is None:
        raise TraceRefused(
            "no_active_generation", "trace frames can only bind to an active generation"
        )
    return graph


def _ingest_report(path: Path, digest: str, graph, records, edges, unbound) -> dict:
    return {
        "trace_digest": digest,
        "generation_id": graph.generation_id,
        "source_name": Path(path).name,
        "byte_size": Path(path).stat().st_size,
        "record_count": len(records),
        "edge_count": len(edges),
        "unbound_count": unbound,
    }


def _ingest_with_graph(path: Path, graph, state_root) -> dict:
    records = read_trace_records(path)
    edges, unbound = bind_records(records, build_definition_index(graph))
    digest = trace_digest(path)
    report = _ingest_report(path, digest, graph, records, edges, unbound)
    with closing(open_store(state_root)) as database:
        _replace_trace(database, digest, report, edges)
    return report


def ingest_trace(path: Path, directory: Path, *, state_root: Path | None = None) -> dict:
    """Bind one trace file to the active generation and store its edges.

    Re-ingesting the same file is a no-op in effect: rows are keyed by the
    file's SHA-256 and replaced wholesale, so nothing doubles.
    """
    graph = _active_graph(directory)
    try:
        return _ingest_with_graph(Path(path), graph, state_root)
    finally:
        graph.close()


# --------------------------------------------------------------------------
# The read path: trace-derived callers, labelled as such
# --------------------------------------------------------------------------


def _require_target_bound(target_ids: list[str]) -> None:
    if len(target_ids) <= MAX_NODE_FILTER:
        return
    raise TraceRefused(
        "trace_target_filter_too_wide",
        f"{len(target_ids)} nodes exceed the {MAX_NODE_FILTER} filter bound",
    )


def _caller_totals(target_ids: list[str], state_root) -> list[sqlite3.Row]:
    _require_target_bound(target_ids)
    database = _read_only_store(state_root)
    if database is None:
        return []
    placeholders = ",".join("?" for _ in target_ids)
    with closing(database):
        return database.execute(
            "SELECT caller_node_id, SUM(call_count) AS call_count, "
            "COUNT(DISTINCT trace_digest) AS trace_count FROM trace_edge "
            f"WHERE callee_node_id IN ({placeholders}) GROUP BY caller_node_id "
            "ORDER BY call_count DESC, caller_node_id",
            target_ids,
        ).fetchall()


def _qualified_name(metadata: dict) -> str:
    owner = str(metadata.get("owner", ""))
    name = str(metadata.get("name", ""))
    if not owner:
        return name
    return f"{owner}.{name}"


def _definition_line(graph, node_id: str) -> int:
    occurrences = graph.occurrences(node_id, max_rows=1)
    if not occurrences:
        return 0
    return int(occurrences[0]["line_start"])


def _trace_caller_row(graph, entry, function_name: str) -> dict | None:
    caller = graph.node(entry["caller_node_id"])
    if caller is None:
        return None
    metadata = caller["metadata"]
    root = Path(graph.repository_scope.checkout_root)
    return {
        "file": str(root / str(metadata.get("path", ""))),
        "line": _definition_line(graph, entry["caller_node_id"]),
        "function": function_name,
        "qualified_name": _qualified_name(metadata),
        "symbol_id": entry["caller_node_id"],
        "call_count": int(entry["call_count"]),
        "trace_count": int(entry["trace_count"]),
        "evidence": "execution-trace",
    }


def _trace_caller_rows(graph, entries, function_name: str) -> tuple[list[dict], int]:
    """Rows for callers still present in the generation, plus a stale count."""
    built = [_trace_caller_row(graph, entry, function_name) for entry in entries]
    rows = [row for row in built if row is not None]
    return rows, len(built) - len(rows)


def _trace_caller_fields(graph, function_name: str, limit: int, state_root) -> dict:
    targets = graph.find_nodes(
        kinds=("function", "method"), name=function_name, max_rows=MAX_NODE_FILTER
    )
    target_ids = sorted({str(item["node_id"]) for item in targets})
    entries = _caller_totals(target_ids, state_root)
    rows, stale = _trace_caller_rows(graph, entries, function_name)
    sample = rows[:limit]
    return {
        "trace_callers": sample,
        "trace_caller_count": len(rows),
        "trace_callers_truncated": len(rows) > len(sample),
        "trace_stale_edges": stale,
        "trace_generation": graph.generation_id,
        "trace_evidence_is_dynamic": True,
    }


def _empty_trace_fields() -> dict:
    return {
        "trace_callers": [],
        "trace_caller_count": 0,
        "trace_callers_truncated": False,
        "trace_stale_edges": 0,
        "trace_generation": None,
        "trace_evidence_is_dynamic": True,
    }


def trace_callers(
    function_name: str,
    directory: Path,
    *,
    limit: int = MAX_TRACE_CALLERS,
    state_root: Path | None = None,
) -> dict:
    """Callers of `function_name` that a trace watched happening.

    These are never merged into a static `callers` list. A trace-derived edge
    says a call occurred in some recorded run; it does not say which call site
    made it, and its absence says nothing at all.

    Without an active generation there is nothing to bind against, and the
    answer is empty rather than an error: `find_callers` degrades to a live
    scan in exactly that case, and a reader must still get an answer.
    """
    graph = _open_graph(directory)
    if graph is None:
        return _empty_trace_fields()
    try:
        return _trace_caller_fields(graph, function_name, limit, state_root)
    finally:
        graph.close()


def with_trace_callers(
    answer: dict,
    function_name: str,
    directory: Path,
    *,
    state_root: Path | None = None,
) -> dict:
    """Add the trace fields to a `find_callers` report, leaving `callers` alone."""
    if not isinstance(answer, dict):
        raise TypeError("answer must be a find_callers report")
    fields = trace_callers(function_name, directory, state_root=state_root)
    return {**answer, **fields}


# --------------------------------------------------------------------------
# Measurement: which unresolved dispatch observations a trace covers
# --------------------------------------------------------------------------


def _dispatch_observations(graph) -> list:
    return _graph_database(graph).execute(
        "SELECT source_node_id, target_text FROM observation "
        "WHERE edge_type = 'CALLS' AND reason = 'dynamic_dispatch'"
    ).fetchall()


def _callee_names(graph) -> dict[str, str]:
    return {
        node_id: key[2] for key, node_id in build_definition_index(graph).items()
    }


def _observed_names(names: dict[str, str], state_root) -> dict[str, set[str]]:
    """caller node id -> the set of callee names a trace saw it call."""
    database = _read_only_store(state_root)
    if database is None:
        return {}
    observed: dict[str, set[str]] = {}
    with closing(database):
        rows = database.execute(
            "SELECT DISTINCT caller_node_id, callee_node_id FROM trace_edge"
        ).fetchall()
    for caller, callee in rows:
        observed.setdefault(caller, set()).add(names.get(callee, ""))
    return observed


def _attribute(target_text: object) -> str:
    return str(target_text or "").rsplit(".", 1)[-1]


def _covered(observations, observed: dict[str, set[str]]) -> int:
    return sum(
        1
        for source_node_id, target_text in observations
        if _attribute(target_text) in observed.get(source_node_id, ())
    )


def dispatch_resolution(directory: Path, *, state_root: Path | None = None) -> dict:
    """How many unresolved dispatch observations the stored traces cover.

    An observation is counted covered when the trace saw its *caller* call
    something whose name is the attribute the observation names. That is an
    inference, not a proof: where one function has two call sites naming the
    same attribute on different receivers, one trace edge covers both. The
    number is reported as coverage, never as resolution of a particular site.
    """
    graph = _active_graph(directory)
    try:
        observations = _dispatch_observations(graph)
        observed = _observed_names(_callee_names(graph), state_root)
        return {
            "generation_id": graph.generation_id,
            "dispatch_observations": len(observations),
            "covered_by_traces": _covered(observations, observed),
            "callers_with_trace_edges": len(observed),
        }
    finally:
        graph.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _stored_traces(state_root) -> list[dict]:
    database = _read_only_store(state_root)
    if database is None:
        return []
    with closing(database):
        rows = database.execute(
            "SELECT trace_digest, source_name, generation_id, record_count, "
            "edge_count, unbound_count, ingested_at FROM trace ORDER BY ingested_at"
        ).fetchall()
    return [dict(row) for row in rows]


def _run_ingest(arguments) -> int:
    report = ingest_trace(arguments.trace, arguments.directory)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def _run_status(arguments) -> int:
    print(json.dumps(_stored_traces(None), indent=2, sort_keys=True))
    del arguments
    return 0


def _run_callers(arguments) -> int:
    print(json.dumps(trace_callers(arguments.symbol, arguments.directory), indent=2, sort_keys=True))
    return 0


def _run_resolution(arguments) -> int:
    print(json.dumps(dispatch_resolution(arguments.directory), indent=2, sort_keys=True))
    return 0


_COMMANDS = {
    "ingest": _run_ingest,
    "status": _run_status,
    "callers": _run_callers,
    "resolution": _run_resolution,
}


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest and read execution traces")
    parser.add_argument("command", choices=sorted(_COMMANDS))
    parser.add_argument("--trace", type=Path, help="an execution-trace/v1 file")
    parser.add_argument("--symbol", help="function or method name for `callers`")
    parser.add_argument("--directory", type=Path, default=Path.cwd())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    try:
        return _COMMANDS[arguments.command](arguments)
    except TraceRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
