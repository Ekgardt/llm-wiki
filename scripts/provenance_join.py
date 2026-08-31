"""Join a code symbol to the decisions that shaped it and their session sources.

MEM-16, approved 2026-08-28. The competitors hold one half each: code-graph
tools have no memory of why, memory systems have no graph of what. This vault
holds both, and the join is the question an operator actually asks: "why is
this code like this?" — symbol → decision pages that name it → the daily and
session evidence those pages cite. Research:
`docs/research/2026-08-28-code-decision-session-join.md`.

The join itself is deterministic and read-only. It does not re-verify the
byte-level citations it surfaces — `read_page` already does that per slug, and
the response says so instead of implying verification it did not perform.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

MAX_LOCATIONS = 5
MAX_PAGES = 8
MAX_SOURCES_PER_PAGE = 6
MAX_NOTE_BYTES = 256 * 1024
MAX_NOTES_SCANNED = 500

_SOURCE_LINE = re.compile(
    r"(knowledge/daily/[0-9-]+\.md|knowledge/raw/sessions/[^\s)\]`]+"
    r"|docs/research/[^\s)\]`]+\.md)"
)


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


def _graph_locations(directory: Path, symbol: str, deadline: float) -> list[dict]:
    """Where the symbol lives, from the active generation; empty when absent."""
    from code_graph import _active_evidence_graph

    graph = _active_evidence_graph(directory)
    if graph is None:
        return []
    rows = graph.find_nodes(name=symbol, max_rows=MAX_LOCATIONS, deadline=deadline)
    return [_location_row(row) for row in rows[:MAX_LOCATIONS]]


def _location_row(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    return {
        "path": metadata.get("path") or row.get("path"),
        "kind": row.get("kind"),
        "name": row.get("name"),
    }


def _note_files(vault: Path) -> list[Path]:
    notes = vault / "knowledge" / "notes"
    if not notes.is_dir():
        return []
    return sorted(notes.glob("*.md"))[:MAX_NOTES_SCANNED]


def _read_note(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_NOTE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _first_match_line(text: str, pattern: re.Pattern[str]) -> str | None:
    for line in text.splitlines():
        if pattern.search(line):
            return line.strip()[:240]
    return None


def _page_title(text: str, path: Path) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()[:120]
    return path.stem


def _cited_sources(text: str) -> list[str]:
    seen: list[str] = []
    for match in _SOURCE_LINE.finditer(text):
        value = match.group(1)
        if value not in seen:
            seen.append(value)
        if len(seen) >= MAX_SOURCES_PER_PAGE:
            break
    return seen


def _page_hit(path: Path, pattern: re.Pattern[str]) -> dict | None:
    text = _read_note(path)
    if text is None:
        return None
    matched = _first_match_line(text, pattern)
    if matched is None:
        return None
    return {
        "slug": path.stem,
        "title": _page_title(text, path),
        "matched_line": matched,
        "cited_sources": _cited_sources(text),
    }


def _matching_pages(
    vault: Path, pattern: re.Pattern[str], deadline: float
) -> list[dict]:
    pages: list[dict] = []
    for path in _note_files(vault):
        if time.monotonic() >= deadline or len(pages) >= MAX_PAGES:
            break
        hit = _page_hit(path, pattern)
        if hit is not None:
            pages.append(hit)
    return pages


def join_symbol_provenance(
    vault: Path, directory: Path, symbol: str, deadline: float
) -> dict:
    """The one-call chain: symbol -> locations -> pages naming it -> their sources."""
    pattern = _symbol_pattern(symbol)
    locations = _graph_locations(directory, symbol, deadline)
    pages = _matching_pages(vault, pattern, deadline)
    return {
        "symbol": symbol,
        "locations": locations,
        "pages": pages,
        "verification": (
            "cited_sources are surfaced, not re-verified here; "
            "read_page resolves a page's citations against source bytes"
        ),
        "graph": "active_generation" if locations else "unavailable_or_absent",
    }
