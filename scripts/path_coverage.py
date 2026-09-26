"""Per-path index coverage: indexed, fresh, parsed, how many nodes — or why not.

CODE-05, roadmap 2026-08-28: a negative answer is worth exactly as much as
the coverage behind it. Parity with codebase-memory-mcp's per-path coverage,
including its discipline: best-effort signal, never proof of completeness.

Issue #24, B1 (2026-09-10): the answer is one generation's word about one
file. `indexed` and `freshness` come from the generation's own `source` row,
the same generation the node count comes from — the manifest it used to read
lives beside the vault's generations only, so every foreign repository
answered `indexed=false` next to a real node count. The `parse` block
re-parses the stored bytes with the grammar the extractor used and names the
ranges it could not read: the extractor records one `parse_error`
observation for the whole file and indexes nothing from it, so those ranges
are exactly where a negative answer cannot be trusted.
Research: `docs/research/2026-08-28-path-coverage-mode.md`,
`docs/research/2026-09-10-a-query-surface-that-answers-the-whole-graph.md`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

try:
    from .python_parse import PARSE_FAILURES, parse_python
except ImportError:
    from python_parse import PARSE_FAILURES, parse_python

NODE_CEILING = 10_000
PARSE_ERROR_LIMIT = 20
PARSE_NODE_CEILING = 200_000
MAX_PARSE_BYTES = 4 * 1024 * 1024

COVERAGE_NOTE = (
    "best-effort signal, not proof of completeness; "
    "a fresh source can still have constructs the extractor missed"
)


MAX_HASHED_BYTES = 16 * 1024 * 1024
# Never a digest: 64 hex characters cannot spell it.
UNREADABLE = "unreadable"


def contained_scope(directory: Path, deadline: float):
    """The repository scope rooted at exactly this directory, or None.

    Audit 3, A2: the file on disk is read only through the contained reader the
    precise modes use, never by joining the caller's text onto a directory.
    Research: `docs/research/2026-09-17-coverage-reads-only-inside-the-repository.md`.
    """
    from repository_scope import resolve_repository_scope

    try:
        scope = resolve_repository_scope(Path(directory), deadline=deadline)
    except TimeoutError:
        raise
    except OSError:
        return None
    if Path(scope.checkout_root) != Path(directory).resolve():
        return None
    return scope


def _exists_inside(scope, relative: str) -> bool | None:
    """True or False for a path inside the scope; None when it is not inside."""
    import os

    from lsp_security import resolve_repository_source

    if scope is None:
        return None
    try:
        source = resolve_repository_source(scope, relative, must_exist=False)
    except (TypeError, ValueError):
        return None
    return os.path.lexists(source.absolute_path)


def _contained_sha(scope, relative: str, deadline: float) -> str:
    from lsp_security import PathContainmentError, read_repository_source_bytes

    try:
        content = read_repository_source_bytes(
            scope, relative, max_bytes=MAX_HASHED_BYTES, deadline=deadline
        )
    except PathContainmentError:
        return UNREADABLE
    return hashlib.sha256(content).hexdigest()


def _current_sha(scope, relative: str, deadline: float) -> str | None:
    """The file's digest, None when it is absent, UNREADABLE when refused."""
    exists = _exists_inside(scope, relative)
    if exists is None:
        return UNREADABLE
    if not exists:
        return None
    return _contained_sha(scope, relative, deadline)


_DISK_STATES = {None: "missing_on_disk", UNREADABLE: UNREADABLE}


def _freshness(recorded: str | None, current: str | None) -> str:
    """What the disk says first; only a digest is compared with the index."""
    disk_state = _DISK_STATES.get(current)
    if disk_state is not None:
        return disk_state
    return _indexed_freshness(recorded, current)


def _indexed_freshness(recorded: str | None, current: str | None) -> str:
    if recorded is None:
        return "not_indexed"
    return "fresh" if recorded == current else "stale"


def _node_count(graph, relative: str, deadline: float) -> dict:
    try:
        rows = graph.find_nodes(
            path=relative, max_rows=NODE_CEILING, deadline=deadline
        )
    except ValueError:
        return {"nodes": NODE_CEILING, "nodes_exact": False}
    return {"nodes": len(rows), "nodes_exact": True}


def _error_range(node) -> dict:
    return {
        "kind": "MISSING" if node.is_missing else "ERROR",
        "line_start": node.start_point[0] + 1,
        "line_end": node.end_point[0] + 1,
        "byte_start": node.start_byte,
        "byte_end": node.end_byte,
    }


def _visit(node, stack: list, found: list) -> None:
    if node.is_error or node.is_missing:
        found.append(_error_range(node))
        return
    if node.has_error:
        stack.extend(reversed(node.children))


def _error_ranges(root) -> tuple[list[dict], bool]:
    """ERROR and MISSING ranges of a tree-sitter tree, bounded in both counts."""
    found: list[dict] = []
    stack = [root]
    visited = 0
    while stack and len(found) <= PARSE_ERROR_LIMIT and visited < PARSE_NODE_CEILING:
        visited += 1
        _visit(stack.pop(), stack, found)
    truncated = len(found) > PARSE_ERROR_LIMIT or bool(stack)
    return found[:PARSE_ERROR_LIMIT], truncated


def _parse_status(errors: list[dict]) -> str:
    return "error" if errors else "ok"


def _tree_sitter_parse(language: str, content: bytes) -> dict:
    from code_graph import GRAMMAR_LOADERS, _get_parser

    if language not in GRAMMAR_LOADERS:
        return {"status": "unsupported_language", "language": language}
    parser = _get_parser(language)
    if parser is None:
        return {"status": "not_parsed", "language": language, "reason": "grammar unavailable"}
    errors, truncated = _error_ranges(parser.parse(content).root_node)
    return {
        "status": _parse_status(errors),
        "language": language,
        "errors": errors,
        "errors_truncated": truncated,
    }


def _python_parse(content: bytes) -> dict:
    try:
        parse_python(content)
    except SyntaxError as exc:
        line = int(exc.lineno or 1)
        error = {
            "kind": "SyntaxError",
            "line_start": line,
            "line_end": int(exc.end_lineno or line),
            "message": str(exc.msg)[:200],
        }
        return {"status": "error", "language": "python", "errors": [error], "errors_truncated": False}
    except PARSE_FAILURES as exc:
        error = {"kind": type(exc).__name__, "line_start": 1, "line_end": 1, "message": str(exc)[:200]}
        return {"status": "error", "language": "python", "errors": [error], "errors_truncated": False}
    return {"status": "ok", "language": "python", "errors": [], "errors_truncated": False}


def _parse_refusal(language: str | None, content: bytes) -> dict | None:
    if not language:
        return {"status": "unsupported_language", "language": None}
    if len(content) > MAX_PARSE_BYTES:
        return {"status": "not_parsed", "language": language, "reason": "source over 4 MiB"}
    return None


def _parse_report(language: str | None, content: bytes) -> dict:
    """Re-parse the stored bytes the way the extractor did and name the gaps."""
    refusal = _parse_refusal(language, content)
    if refusal is not None:
        return refusal
    if language == "python":
        return _python_parse(content)
    return _tree_sitter_parse(str(language), content)


def _source_fields(graph, source: dict | None, relative: str, deadline: float) -> dict:
    if source is None:
        return {"indexed": False, "parse": {"status": "not_indexed"}, "observations": {}}
    return {
        "indexed": True,
        "language": source.get("language"),
        "size": source.get("size"),
        "parse": _parse_report(source.get("language"), source["content"]),
        "observations": graph.source_observations(relative, deadline=deadline),
    }


def coverage_for_path(directory: Path, relative: str, deadline: float) -> dict:
    """One path's standing in the active generation, honestly bounded."""
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return {
            "path": relative,
            "coverage": "no_active_generation",
            "note": COVERAGE_NOTE,
        }
    try:
        return _coverage_answer(graph, directory, relative, deadline)
    finally:
        graph.close()


def _coverage_answer(graph, directory: Path, relative: str, deadline: float) -> dict:
    source = graph.source_by_path(relative, deadline=deadline)
    recorded = source.get("sha256") if source else None
    current = _current_sha(contained_scope(directory, deadline), relative, deadline)
    answer = {
        "path": relative,
        "generation_id": str(graph.generation_id),
        **_source_fields(graph, source, relative, deadline),
        "freshness": _freshness(recorded, current),
        "note": COVERAGE_NOTE,
    }
    answer.update(_node_count(graph, relative, deadline))
    return answer
