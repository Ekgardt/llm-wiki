"""Source snippet for a symbol name, resolved through the active generation.

CODE-02, roadmap 2026-08-28: parity with codebase-memory-mcp's
`get_code_snippet`. The generation's node metadata carries path/owner/signature
but no line numbers (measured 2026-08-28), so the block is recovered from the
file by its definition line and indentation, bounded, deterministic, no model.
Research: `docs/research/2026-08-28-symbol-snippet-mode.md`.
"""

from __future__ import annotations

import re
from pathlib import Path

MAX_LOCATIONS = 5
MAX_FILE_BYTES = 1024 * 1024
MAX_SNIPPET_LINES = 120


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


def _graph_paths(directory: Path, symbol: str, deadline: float) -> list[dict]:
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return []
    rows = graph.find_nodes(name=symbol, max_rows=MAX_LOCATIONS, deadline=deadline)
    return [row.get("metadata") or {} for row in rows[:MAX_LOCATIONS]]


def _owned_snippets(directory: Path, metadata: dict, symbol: str) -> list[dict]:
    relative = str(metadata.get("path") or "")
    found = _file_snippets(directory, relative, symbol)
    for snippet in found:
        snippet["owner"] = metadata.get("owner")
    return found


def _unique_paths(metadata_rows: list[dict]) -> list[dict]:
    seen: list[str] = []
    unique: list[dict] = []
    for metadata in metadata_rows:
        relative = str(metadata.get("path") or "")
        if relative and relative not in seen:
            seen.append(relative)
            unique.append(metadata)
    return unique


def snippet_for_symbol(directory: Path, symbol: str, deadline: float) -> dict:
    """Bounded source blocks for every graph-known location of the symbol."""
    metadata_rows = _graph_paths(directory, symbol, deadline)
    snippets: list[dict] = []
    for metadata in _unique_paths(metadata_rows):
        snippets.extend(_owned_snippets(directory, metadata, symbol))
    return {
        "symbol": symbol,
        "snippets": snippets[:MAX_LOCATIONS],
        "graph": "active_generation" if metadata_rows else "unavailable_or_absent",
    }
