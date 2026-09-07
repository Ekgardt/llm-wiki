#!/usr/bin/env python3
"""Things mentioned together, with the link fading if it is not renewed.

The Venus flytrap has no nervous system and still counts: a touched hair emits a
signal, the signal accumulates, the accumulation decays over minutes, and at a
threshold the trap closes. Memory as a decaying accumulator, built out of nothing
but chemistry.

Applied here to the question our multi-session answers keep failing: not *where
is the right session* — retrieval returns the labelled answer session for 87% of
questions — but *where is the second one*, the conversation that completes the
answer and never comes back with the first.

The field calls this spreading activation and is actively working on it: token
co-occurrence graphs expanding a query through bridging entities, per-step
semantic gates, hypergraph diffusion. The lesson shared by all of them is the one
the flytrap already states — activation that spreads without a decay or a gate
reaches the whole graph and means nothing. So the decay and the cap here are not
decoration; they are the mechanism.

The evidence is what the vault already writes. `[[a]]` and `[[b]]` in one entry
is the author's own statement that these belong together: no extraction model, no
token windows, nothing inferred.

Derived and disposable. The table lives under `cache/`, is rebuilt from the
vault, and nothing depends on it existing.

See `docs/research/2026-09-06-what-was-mentioned-together.md`.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memory_state import ROOT, STATE_ROOT  # noqa: E402

TABLE_PATH = STATE_ROOT / "cache" / "co-activation.json"

# A pairing loses half its weight over this long. Six weeks is short enough that
# last spring's coincidence stops mattering and long enough that a project worked
# on monthly keeps its links.
HALF_LIFE_DAYS = 42.0

# What a neighbour may gain, at most. The same ceiling as the citation
# disposition, and for the same reason: one coincidence must not privilege a page
# for ever.
MAX_NEIGHBOUR_BOOST = 0.15

# How many pairings a page may carry. A page linked to everything is linked to
# nothing, and a bound keeps the table small enough to read on every query.
MAX_NEIGHBOURS = 32

# Entries with more links than this are indexes and navigation pages: everything
# in them co-occurs with everything, which is a fact about the page and not about
# the subjects.
MAX_LINKS_PER_ENTRY = 12

_WIKILINK = re.compile(r"\[\[([^\]|#]{1,200})")


def links_in(text: str) -> list[str]:
    """The wikilink targets of one entry, normalised and deduplicated."""
    found = {match.group(1).strip().casefold() for match in _WIKILINK.finditer(text)}
    return sorted(name for name in found if name)


def _pairs(names: list[str]) -> list[tuple[str, str]]:
    if len(names) > MAX_LINKS_PER_ENTRY:
        return []
    return [
        (first, second)
        for index, first in enumerate(names)
        for second in names[index + 1 :]
    ]


def _decay(age_days: float) -> float:
    return 0.5 ** (max(age_days, 0.0) / HALF_LIFE_DAYS)


def _age_days(entry_day: str, today: date) -> float:
    try:
        return float((today - date.fromisoformat(entry_day)).days)
    except (TypeError, ValueError):
        return 0.0


def accumulate(
    table: dict[str, dict[str, float]], text: str, entry_day: str, today: date
) -> None:
    """Add one entry's pairings to the table, weighted by how recent it is."""
    weight = _decay(_age_days(entry_day, today))
    for first, second in _pairs(links_in(text)):
        table.setdefault(first, {})[second] = table.setdefault(first, {}).get(second, 0.0) + weight
        table.setdefault(second, {})[first] = table.setdefault(second, {}).get(first, 0.0) + weight


def _trimmed(neighbours: dict[str, float]) -> dict[str, float]:
    ranked = sorted(neighbours.items(), key=lambda item: (-item[1], item[0]))
    return dict(ranked[:MAX_NEIGHBOURS])


def _entry_text(entry: Path) -> str:
    """An entry that cannot be read contributes nothing rather than stopping the build."""
    try:
        return entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build(vault: Path | None = None, today: date | None = None) -> dict[str, dict[str, float]]:
    """The co-occurrence table, read from the daily entries of the vault."""
    root = Path(vault or ROOT)
    when = today or datetime.now(timezone.utc).date()
    table: dict[str, dict[str, float]] = {}
    for entry in sorted((root / "knowledge" / "daily").glob("*.md")):
        accumulate(table, _entry_text(entry), entry.stem, when)
    return {name: _trimmed(neighbours) for name, neighbours in table.items()}


def save(table: dict[str, dict[str, float]], path: Path | None = None) -> Path:
    destination = Path(path or TABLE_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(table, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return destination


def load(path: Path | None = None) -> dict[str, dict[str, float]]:
    """The stored table, or nothing at all. A missing table is not an error."""
    source = Path(path or TABLE_PATH)
    if not source.is_file():
        return {}
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _strongest(neighbours: dict[str, float]) -> float:
    return max(neighbours.values(), default=0.0)


def neighbours(name: str, table: dict[str, dict[str, float]]) -> dict[str, float]:
    """What this page was mentioned with, as a bounded multiplier above one.

    Scaled against the strongest pairing this page has, so the boost says "most
    often mentioned with" rather than "mentioned in a busy vault".
    """
    found = table.get(name.casefold()) or {}
    top = _strongest(found)
    if not top:
        return {}
    return {
        other: 1.0 + MAX_NEIGHBOUR_BOOST * (weight / top)
        for other, weight in found.items()
    }
