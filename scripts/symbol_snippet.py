"""Source snippet for a symbol, resolved through the active generation.

CODE-02, roadmap 2026-08-28: parity with codebase-memory-mcp's
`get_code_snippet`. Issue #24, B3 (2026-09-10): the generation stores one
`definition` occurrence per symbol with the exact line and byte span of the
definition node, and the bytes it was cut from. So a qualified name
(`owner.name`, or a bare name) answers with the exact block out of the
stored source — `precision: "exact"` — and says whether the file on disk
still has those bytes. A node without a definition occurrence, or a
repository without a generation, falls back to the 2026-08-28 recovery by
definition line and indentation over the working tree, marked
`precision: "heuristic"`. Bounded, deterministic, no model.
Research: `docs/research/2026-08-28-symbol-snippet-mode.md`,
`docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

from path_coverage import _current_sha, _freshness

MAX_LOCATIONS = 5
MAX_FILE_BYTES = 1024 * 1024
MAX_SNIPPET_LINES = 120
MAX_NAME_MATCHES = 200
SNIPPET_KINDS = ("class", "function", "method")


def _definition_pattern(symbol: str) -> re.Pattern[str]:
    escaped = re.escape(symbol)
    return re.compile(
        rf"^(\s*)(?:async\s+)?(?:def|class)\s+{escaped}\b|^(\s*){escaped}\s*[=:(]"
    )


def _read_bounded(path: Path) -> list[str] | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def _block_end(lines: list[str], start: int, indent: int) -> int:
    end = start + 1
    limit = min(len(lines), start + MAX_SNIPPET_LINES)
    while end < limit and _still_inside(lines[end], indent):
        end += 1
    return end


def _still_inside(line: str, indent: int) -> bool:
    stripped = line.strip()
    return not stripped or _indent_of(line) > indent


def _definition_lines(lines: list[str], symbol: str) -> list[int]:
    pattern = _definition_pattern(symbol)
    return [
        index for index, line in enumerate(lines) if pattern.match(line)
    ][:MAX_LOCATIONS]


def _snippet_at(lines: list[str], start: int) -> dict:
    end = _block_end(lines, start, _indent_of(lines[start]))
    return {
        "start_line": start + 1,
        "end_line": end,
        "source": "\n".join(lines[start:end]),
        "truncated": end - start >= MAX_SNIPPET_LINES,
    }


def _file_snippets(root: Path, relative: str, symbol: str) -> list[dict]:
    lines = _read_bounded(root / relative)
    if lines is None:
        return [{"path": relative, "error": "file unreadable or over 1 MiB"}]
    starts = _definition_lines(lines, symbol)
    if not starts:
        return [{"path": relative, "error": "definition line not found"}]
    return [
        {"path": relative, **_snippet_at(lines, start)} for start in starts
    ]


def _split_symbol(symbol: str) -> tuple[str, str]:
    owner, _, name = symbol.rpartition(".")
    return owner, name


def _owner_matches(owner: str, wanted: str) -> bool:
    if not wanted:
        return True
    return owner == wanted or owner.endswith("." + wanted)


def _node_fields(node: dict) -> dict:
    metadata = node["metadata"]
    owner = str(metadata.get("owner", ""))
    name = str(metadata.get("name", node["identity_key"]))
    return {
        "qualified_name": f"{owner}.{name}" if owner else name,
        "owner": metadata.get("owner"),
        "kind": node["kind"],
        "symbol_id": node["node_id"],
    }


def _matching_nodes(graph, symbol: str, deadline: float) -> list[dict]:
    wanted, name = _split_symbol(symbol)
    rows = graph.find_nodes(
        kinds=SNIPPET_KINDS, name=name, max_rows=MAX_NAME_MATCHES, deadline=deadline
    )
    matched = [
        row for row in rows if _owner_matches(str(row["metadata"].get("owner", "")), wanted)
    ]
    return matched[:MAX_LOCATIONS]


def _definition_occurrence(graph, node_id: str, deadline: float) -> dict | None:
    for row in graph.occurrences(node_id, max_rows=8, deadline=deadline):
        if row["role"] == "definition":
            return row
    return None


def _stored_lines(graph, relative: str, deadline: float) -> list[str] | None:
    source = graph.source_by_path(relative, deadline=deadline)
    if source is None or int(source["size"]) > MAX_FILE_BYTES:
        return None
    return source["content"].decode("utf-8", errors="ignore").splitlines()


def _exact_block(lines: list[str], occurrence: dict) -> dict:
    start = int(occurrence["line_start"])
    end = int(occurrence["line_end"])
    cut = min(end, start + MAX_SNIPPET_LINES - 1)
    return {
        "start_line": start,
        "end_line": end,
        "source": "\n".join(lines[start - 1 : cut]),
        "truncated": cut < end,
    }


def _exact_snippet(graph, directory: Path, node: dict, occurrence: dict, deadline: float) -> dict:
    relative = str(occurrence["relative_path"])
    lines = _stored_lines(graph, relative, deadline)
    if lines is None:
        return {"path": relative, "error": "stored source unavailable or over 1 MiB", **_node_fields(node)}
    recorded = str(occurrence["source_sha256"])
    return {
        "path": relative,
        **_exact_block(lines, occurrence),
        **_node_fields(node),
        "precision": "exact",
        "source_sha256": recorded,
        "freshness": _freshness(recorded, _current_sha(directory, relative)),
    }


def _heuristic_snippets(directory: Path, node: dict, name: str) -> list[dict]:
    relative = str(node["metadata"].get("path") or "")
    found = _file_snippets(directory, relative, name)
    for snippet in found:
        snippet.update(_node_fields(node))
        snippet["precision"] = "heuristic"
    return found


def _node_snippets(graph, directory: Path, node: dict, name: str, deadline: float) -> list[dict]:
    occurrence = _definition_occurrence(graph, node["node_id"], deadline)
    if occurrence is None:
        return _heuristic_snippets(directory, node, name)
    return [_exact_snippet(graph, directory, node, occurrence, deadline)]


def _graph_snippets(graph, directory: Path, symbol: str, deadline: float) -> dict:
    answer = {"symbol": symbol, "graph": "active_generation", "generation_id": str(graph.generation_id)}
    try:
        nodes = _matching_nodes(graph, symbol, deadline)
    except ValueError:
        return {**answer, "snippets": [], "error": "too many symbols share this name; qualify it as owner.name"}
    _, name = _split_symbol(symbol)
    snippets: list[dict] = []
    for node in nodes:
        snippets.extend(_node_snippets(graph, directory, node, name, deadline))
    return {**answer, "snippets": snippets[:MAX_LOCATIONS], "resolved_nodes": len(nodes)}


def snippet_for_symbol(directory: Path, symbol: str, deadline: float) -> dict:
    """Exact source blocks for every graph-known definition of the symbol."""
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return {"symbol": symbol, "snippets": [], "graph": "unavailable_or_absent"}
    try:
        return _graph_snippets(graph, directory, symbol, deadline)
    finally:
        graph.close()


def _definition_site(graph, node: dict, deadline: float) -> dict | None:
    """Where the graph says one node is defined: path and line span, no source."""
    occurrence = _definition_occurrence(graph, node["node_id"], deadline)
    if occurrence is None:
        return None
    return {
        **_node_fields(node),
        "file": str(node["metadata"].get("path") or ""),
        "line": int(occurrence["line_start"]),
        "end_line": int(occurrence["line_end"]),
    }


def _graph_definition_sites(graph, symbol: str, deadline: float) -> list[dict]:
    try:
        nodes = _matching_nodes(graph, symbol, deadline)
    except ValueError:
        return []
    sites = [_definition_site(graph, node, deadline) for node in nodes]
    return [site for site in sites if site is not None]


def definition_sites(directory: Path, symbol: str, deadline: float) -> list[dict]:
    """Every definition of the symbol the active generation knows, without source.

    A "where is X defined" question is answered by a path and a line, and the
    generation already stores both as the `definition` occurrence of the node.
    The parity run of 2026-09-12 graded our symbol answer `partial` because it
    carried call-site lines and not this one.
    """
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return []
    try:
        return _graph_definition_sites(graph, symbol, deadline)
    finally:
        graph.close()
