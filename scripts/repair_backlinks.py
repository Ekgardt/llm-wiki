"""Give every page the backlink it owes, without asking anyone to do it.

The vault's own rule is that a link is reciprocal: when a page names another,
the named page links back, so a reader arriving from either side sees the
connection ([[add-reciprocal-backlinks-at-creation]]). The compile pass writes
pages that link outward and cannot edit the pages they name, so every compile
left findings that only a human could clear — and a finding that waits on a
human is the one thing this vault is not allowed to produce.

This pass closes them the only way that needs no judgement: it appends the
missing link, under `## Related` when the page has one, in a new section when it
does not. It never removes, reorders, or rewrites anything else, and it writes
through the same transaction machinery as every other automatic writer, so each
addition is recoverable.

Usage:
    uv run python scripts/repair_backlinks.py            # dry-run
    uv run python scripts/repair_backlinks.py --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bounded_io import read_stable_bytes  # noqa: E402
from lint_memory import (  # noqa: E402
    NOTES,
    VAULT,
    missing_backlink_pairs,
)
from markdown_transaction import mutate_knowledge, stable_operation_id  # noqa: E402
from memory_state import ROOT  # noqa: E402
from reliable_memory import sha256_bytes  # noqa: E402
from vault_editorial import EDITORIAL_NAMES  # noqa: E402

# One page is never large enough to justify an unbounded read.
MAX_PAGE_BYTES = 512 * 1024

# The headings a page already uses for its outward links, most specific first.
RELATED_HEADINGS = ("## Related", "## Links", "## Related pages")


def _pages() -> list[Path]:
    return [
        page
        for page in sorted(NOTES.rglob("*.md"))
        if page.name not in EDITORIAL_NAMES and "archive" not in page.parts
    ]


def page_slug(page: Path) -> str:
    """How this vault writes a link to a page: `knowledge/notes/<name>`."""
    return page.relative_to(ROOT).with_suffix("").as_posix()


def _link_line(slug: str) -> str:
    return f"- [[{slug}]] — links to this page."


def _section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Where the named section's body starts and ends, if the page has one."""
    if heading not in lines:
        return None
    start = lines.index(heading) + 1
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            return start, index
    return start, len(lines)


def _first_section(lines: list[str]) -> tuple[int, int] | None:
    for heading in RELATED_HEADINGS:
        bounds = _section_bounds(lines, heading)
        if bounds is not None:
            return bounds
    return None


def _insert_at(lines: list[str], end: int, line: str) -> list[str]:
    """Put the link at the end of the section, before its trailing blank lines."""
    position = end
    while position > 0 and not lines[position - 1].strip():
        position -= 1
    return [*lines[:position], line, *lines[position:]]


def _ends_blank(lines: list[str]) -> bool:
    return bool(lines) and not lines[-1].strip()


def _appended_section(lines: list[str], line: str) -> list[str]:
    tail = [] if _ends_blank(lines) else [""]
    return [*lines, *tail, "## Related", "", line, ""]


def with_backlink(text: str, slug: str) -> str:
    """The page's text with one link added, and nothing else touched."""
    line = _link_line(slug)
    lines = text.split("\n")
    if line in lines:
        return text
    bounds = _first_section(lines)
    if bounds is None:
        return "\n".join(_appended_section(lines, line))
    return "\n".join(_insert_at(lines, bounds[1], line))


def _repair_pair(source: Path, target: Path) -> str:
    relative = target.relative_to(ROOT).as_posix()
    current = read_stable_bytes(target, MAX_PAGE_BYTES, label="backlink target")
    updated = with_backlink(current.decode("utf-8"), page_slug(source)).encode("utf-8")
    if updated == current:
        return f"UNCHANGED: {relative}"
    mutate_knowledge(
        stable_operation_id("repair-backlink", relative, updated),
        {target: updated},
        preconditions={relative: sha256_bytes(current)},
    )
    return f"LINKED: {relative} → {source.stem}"


def _attempt(source: Path, target: Path) -> str:
    try:
        return _repair_pair(source, target)
    except (OSError, RuntimeError, ValueError, UnicodeDecodeError) as exc:
        return f"ERROR: {target.relative_to(ROOT).as_posix()}: {type(exc).__name__}"


def repair(*, apply: bool) -> list[str]:
    """Add every owed backlink; a dry run only names them."""
    pairs = missing_backlink_pairs(_pages(), [VAULT, NOTES])
    if not apply:
        return [
            f"WOULD LINK: {target.relative_to(ROOT).as_posix()} → {source.stem}"
            for source, target in pairs
        ]
    return [_attempt(source, target) for source, target in pairs]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the links")
    arguments = parser.parse_args()
    outcomes = repair(apply=arguments.apply)
    for line in outcomes:
        print(f"  {line}")
    failures = len([line for line in outcomes if line.startswith("ERROR:")])
    print(f"repair_backlinks: {len(outcomes)} owed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
