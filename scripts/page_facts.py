"""What the vault knows about a page it just cited.

The grounded answer proves its citations against byte spans, and then the
envelope carrying it reported `coverage: 0.0` and "coverage is unknown" on every
answer — a constant, while the frontmatter of every cited page states its type,
who it came from, how confident it is, and its own type says how long a page like
it stays current. This module reads those facts once, from the page itself, so
the answer can admit what it rests on. See
docs/research/2026-08-24-what-an-answer-should-admit.md.

Nothing here judges whether a span supports a claim: that is entailment, the
citation gate does not verify it, and this module does not pretend to either.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from okf_types import DEFAULT_AGE_DAYS, TYPE_AGE_DAYS
from page_status import normalized_status
from provenance import trust_weight

MAX_FRONTMATTER_BYTES = 8192

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
_FIELDS = {
    "page_type": re.compile(r"^type:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE),
    "authority": re.compile(
        r"^source_authority:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
    ),
    "confidence": re.compile(
        r"^confidence:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE
    ),
    "status": re.compile(r"^status:\s*[\"']?([^\"'\n]+)[\"']?\s*$", re.MULTILINE),
}

# A page states its own confidence; these are the numbers that word is worth.
# Absent means neither claimed nor denied, so it sits between medium and low.
CONFIDENCE_VALUES = {"high": 0.9, "medium": 0.7, "low": 0.4}
UNSTATED_CONFIDENCE = 0.6


@dataclass(frozen=True)
class PageFacts:
    """One cited page, as its own frontmatter and mtime describe it."""

    relative_path: str
    page_type: str
    authority: str
    confidence: str
    status: str
    trust_weight: float
    age_days: int | None
    age_limit_days: int
    aging: bool

    def stated_confidence(self) -> float:
        return CONFIDENCE_VALUES.get(self.confidence, UNSTATED_CONFIDENCE)


def _frontmatter_of(text: str) -> str:
    match = _FRONTMATTER.match(text)
    if match is None:
        return ""
    return match.group(1)


def _field(frontmatter: str, name: str) -> str:
    match = _FIELDS[name].search(frontmatter)
    if match is None:
        return ""
    return match.group(1).strip().casefold()


def _age_days(path: Path) -> int | None:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return None
    return max(0, int((time.time() - modified) / 86400))


def _read_head(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(MAX_FRONTMATTER_BYTES)
    except OSError:
        return None


def _facts_from(relative_path: str, path: Path, text: str) -> PageFacts:
    frontmatter = _frontmatter_of(text)
    page_type = _field(frontmatter, "page_type")
    age = _age_days(path)
    limit = TYPE_AGE_DAYS.get(page_type, DEFAULT_AGE_DAYS)
    authority = _field(frontmatter, "authority")
    return PageFacts(
        relative_path=relative_path,
        page_type=page_type,
        authority=authority,
        confidence=_field(frontmatter, "confidence"),
        status=normalized_status(_field(frontmatter, "status")),
        trust_weight=trust_weight(authority, page_type),
        age_days=age,
        age_limit_days=limit,
        aging=_is_aging(page_type, age, limit),
    )


def _is_aging(page_type: str, age: int | None, limit: int) -> bool:
    """Past the window its own type declares, and of a type that ages at all."""
    from okf_types import NEVER_ARCHIVE_TYPES

    if age is None or page_type in NEVER_ARCHIVE_TYPES:
        return False
    return age > limit


def read_page_facts(vault: Path, relative_path: str) -> PageFacts | None:
    """Facts for one cited page, or None when it cannot be read."""
    if not relative_path:
        return None
    path = Path(vault) / relative_path
    text = _read_head(path)
    if text is None:
        return None
    return _facts_from(relative_path, path, text)
