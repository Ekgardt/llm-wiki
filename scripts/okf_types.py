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

TYPE_ALIASES: dict[str, str] = {
    "comparison": "synthesis",
    "connection": "synthesis",
    "fact": "concept",
}
