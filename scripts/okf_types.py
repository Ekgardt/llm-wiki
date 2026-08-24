"""Canonical OKF type values — single source of truth.

Imported by lint_memory.py, migrate_to_okf.py, rebuild_memory_index.py,
archive_stale.py. Do NOT duplicate type sets in consuming modules.

Type taxonomy aligns with Zettelkasten layers of evidence:
  Layer 1 (Data):     debugging, pattern, raw-source
  Layer 2 (Interpret): concept, decision, qa, entity
  Layer 3 (Synthesis): synthesis
  Structural:         workflow, gap
  Agent config:       skill, rule
  Project:            project-state, project-context, bootstrap-context
"""
from __future__ import annotations

CANONICAL_TYPES: frozenset[str] = frozenset({
    "debugging",
    "pattern",
    "raw-source",
    "concept",
    "decision",
    "qa",
    "entity",
    "synthesis",
    "workflow",
    "gap",
    "skill",
    "rule",
    "project-state",
    "project-context",
    "bootstrap-context",
})

# Quarantined staging documents only. These are deliberately not durable OKF
# page types and must never be accepted under knowledge/notes/.
INBOX_TYPES: frozenset[str] = frozenset({"claim-candidate"})

NEVER_ARCHIVE_TYPES: frozenset[str] = frozenset({
    "skill",
    "rule",
    "concept",
    "entity",
    "decision",
    "synthesis",
    "project-state",
    "project-context",
    "bootstrap-context",
})

# How long a page of each type stays current. The archiver decides what to move
# out of the way with these, and the answer path uses the same numbers to say
# that a cited page is past its own window — one set, two readers.
TYPE_AGE_DAYS: dict[str, int] = {
    "debugging": 60,       # old debugging notes go stale fast
    "gap": 90,             # gaps close when a real page is created (AGENTS.md §5)
    "pattern": 180,        # patterns live longer
    "workflow": 365,       # workflows are durable
    "qa": 365,             # Q&A stays relevant
}

# Default for untyped pages.
DEFAULT_AGE_DAYS = 180

TYPE_ALIASES: dict[str, str] = {
    "comparison": "synthesis",
    "connection": "synthesis",
    "fact": "concept",
}
