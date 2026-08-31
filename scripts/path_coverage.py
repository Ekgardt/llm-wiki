"""Per-path index coverage: indexed, fresh, and how many nodes — or say why not.

CODE-05, roadmap 2026-08-28: a negative answer is worth exactly as much as
the coverage behind it. Parity with codebase-memory-mcp's per-path coverage,
including its discipline: best-effort signal, never proof of completeness.
Research: `docs/research/2026-08-28-path-coverage-mode.md`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

NODE_CEILING = 10_000
MAX_MANIFEST_BYTES = 32 * 1024 * 1024

COVERAGE_NOTE = (
    "best-effort signal, not proof of completeness; "
    "a fresh source can still have constructs the extractor missed"
)


def _source_manifest(directory: Path, generation_id: str) -> list[dict] | None:
    path = (
        directory
        / "cache"
        / "evidence-graph"
        / "generations"
        / generation_id
        / "source-manifest.json"
    )
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    sources = document.get("sources")
    return sources if isinstance(sources, list) else None


def _manifest_row(sources: list[dict], relative: str) -> dict | None:
    for row in sources:
        if row.get("relative_path") == relative:
            return row
    return None


def _current_sha(directory: Path, relative: str) -> str | None:
    try:
        return hashlib.sha256((directory / relative).read_bytes()).hexdigest()
    except OSError:
        return None


def _freshness(recorded: str | None, current: str | None) -> str:
    if current is None:
        return "missing_on_disk"
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
    sources = _source_manifest(directory, str(graph.generation_id))
    row = _manifest_row(sources, relative) if sources else None
    recorded = row.get("sha256") if row else None
    answer = {
        "path": relative,
        "generation_id": str(graph.generation_id),
        "indexed": row is not None,
        "freshness": _freshness(recorded, _current_sha(directory, relative)),
        "note": COVERAGE_NOTE,
    }
    answer.update(_node_count(graph, relative, deadline))
    return answer
