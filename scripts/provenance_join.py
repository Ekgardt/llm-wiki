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

# Graph locations joined into one provenance answer; the protocol reply bound is 10 000.
MAX_LOCATIONS = 5
# Pages shown in one answer, a display cut: the answer states `page_count` and
# `pages_truncated`, so a reader knows when more pages name the symbol.
MAX_PAGES = 8
# Sources listed per page, a display cut; `read_page` gives the whole page.
MAX_SOURCES_PER_PAGE = 6
# A note above this is skipped as unreadable, not read partly: 256 KiB is about
# thirteen times the largest note of this vault (19 KB, 2026-09-27) and keeps one scan's
# memory bounded whatever a user puts in knowledge/notes.
MAX_NOTE_BYTES = 256 * 1024

_SOURCE_LINE = re.compile(
    r"(knowledge/daily/[0-9-]+\.md|knowledge/raw/sessions/[^\s)\]`]+"
    r"|docs/research/[^\s)\]`]+\.md)"
)


def _symbol_pattern(symbol: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol)}(?![A-Za-z0-9_])")


def _graph_locations(directory: Path, symbol: str, deadline: float) -> list[dict]:
    """Where the symbol lives, from the active generation; empty when absent."""
    from llm_wiki.code_graph_generation import active_evidence_graph

    graph = active_evidence_graph(directory)
    if graph is None:
        return []
    try:
        return _named_rows(graph, symbol, deadline)
    finally:
        graph.close()


def _named_rows(graph, symbol: str, deadline: float) -> list[dict]:
    """Every node of this name up to the reader's ceiling; the cut is ours.

    `max_rows=MAX_LOCATIONS` made the reader refuse any name with more than
    five nodes (audit C-36,
    docs/research/2026-09-25-a-common-name-is-asked-up-to-the-reader-ceiling.md).
    """
    from evidence_graph import MAX_ROWS

    rows = graph.find_nodes(name=symbol, max_rows=MAX_ROWS, deadline=deadline)
    return [_location_row(row) for row in rows]


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
    # Every note: a cap of 500 left the notes after the 500th alphabetically
    # unsearched. The scan is bounded by the caller's deadline and each note by
    # MAX_NOTE_BYTES. docs/research/2026-09-27-a-cut-says-what-it-left-out.md
    return sorted(notes.glob("*.md"))


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
) -> tuple[list[dict], bool]:
    """Every matching page the deadline allowed, and whether every note was read."""
    notes = _note_files(vault)
    pages: list[dict] = []
    for path in notes:
        if time.monotonic() >= deadline:
            return pages, False
        pages.extend(_page_hits(path, pattern))
    return pages, True


def _page_hits(path: Path, pattern: re.Pattern[str]) -> list[dict]:
    hit = _page_hit(path, pattern)
    return [] if hit is None else [hit]


def _pages_report(pages: list[dict], complete: bool) -> dict:
    """The first MAX_PAGES pages, and what the cut and the deadline left out."""
    return {
        "pages": pages[:MAX_PAGES],
        "page_count": len(pages),
        "pages_truncated": len(pages) > MAX_PAGES,
        "scan_complete": complete,
    }


def join_symbol_provenance(
    vault: Path, directory: Path, symbol: str, deadline: float
) -> dict:
    """The one-call chain: symbol -> locations -> pages naming it -> their sources."""
    pattern = _symbol_pattern(symbol)
    locations = _graph_locations(directory, symbol, deadline)
    pages, complete = _matching_pages(vault, pattern, deadline)
    return {
        "symbol": symbol,
        "locations": locations[:MAX_LOCATIONS],
        "location_count": len(locations),
        "locations_truncated": len(locations) > MAX_LOCATIONS,
        **_pages_report(pages, complete),
        "verification": (
            "cited_sources are surfaced, not re-verified here; "
            "read_page resolves a page's citations against source bytes"
        ),
        "graph": "active_generation" if locations else "unavailable_or_absent",
    }
