"""Shared definitions of editorial/metadata page sets across the vault.

Multiple scripts need to distinguish **editorial metadata** pages from
**curated content** pages:

- `lint_memory.py` exempts editorial pages from orphan, sparse, and
  backlink checks.
- `lookup_mode.py` excludes editorial pages from the curated-page
  counter that chooses the retrieval tier.

Before this module these lived as duplicate literals, drifting over
time. Centralizing them here keeps the two callers in sync and gives
new scripts a single import point.

## Conventions

`EDITORIAL_NAMES` — filenames whose presence in *any* path under the
vault marks the page as editorial. Matching is by basename, case-sensitive.

`BACKLINK_EXEMPT_NAMES` — pages that legitimately don't require inbound
backlinks from their callers (workflows and high-level entry points).

`BROKEN_LINK_SKIP_NAMES` — pages whose prose frequently contains literal
`[[...]]` that aren't real wikilinks (docs with bracket placeholders,
append-only changelogs with historical references to renamed pages).

`editorial_parents_to_skip()` — directories to skip wholesale when
enumerating content (e.g. `wiki/projects/_template/` is a skeleton).

## What NOT to put here

Purely script-internal helpers (path resolution, state I/O) stay in
`memory_state.py`. This module is editorial policy only.
"""
from __future__ import annotations

from pathlib import Path

# Pages intentionally exempt from orphan / sparse / backlink checks.
# Indexes, logs, and human front doors are editorial metadata; project
# state pages are auto-updated "where we left off" records with the
# same rationale.
EDITORIAL_NAMES: frozenset[str] = frozenset({
    "index.md",
    "log.md",
    "Vault Home.md",
    "AGENTS.md",
    "operating-model.md",
    # Directory-level readmes are metadata, not curated content.
    "README.md",
    # Per-project state pages under `wiki/projects/<slug>/state.md` are
    # auto-updated by the SessionStart hook. Same rationale as index/log.
    "state.md",
})

# Pages that point DOWN to concepts but shouldn't impose BACKLINK
# obligations on everything that links UP to them.
BACKLINK_EXEMPT_NAMES: frozenset[str] = frozenset({
    "Ingestion Workflow.md",
    "Retrieval Workflow.md",
    "Review Workflow.md",
    "Vault Home.md",
    "index.md",
    "log.md",
    # Utility synthesis cited by many concepts — would otherwise require
    # a backlink on every concept page.
    "Karpathy LLM Wiki Workflow.md",
})

# Files whose prose frequently contains bracketed literals that look like
# wikilinks but are code fences / placeholders / historical references.
BROKEN_LINK_SKIP_NAMES: frozenset[str] = frozenset({
    "operating-model.md",
    "AGENTS.md",
    # log.md is editorial changelog — historical entries may cite pages
    # that were later renamed or contain literal bracketed strings as
    # examples. Append-only, so rewriting is not an option.
    "log.md",
})


def editorial_parents_to_skip(wiki_root: Path) -> tuple[Path, ...]:
    """Directory paths to treat as editorial scaffolding (skip entirely).

    Returns resolved paths so callers can do a `parent in skip` check
    without worrying about relative/absolute or case mismatches.
    """
    return (
        (wiki_root / "projects" / "_template").resolve(),
    )


def is_editorial_name(filename: str) -> bool:
    """True if *filename* is in EDITORIAL_NAMES."""
    return filename in EDITORIAL_NAMES


def is_backlink_exempt(filename: str) -> bool:
    """True if the page is exempt from the backlink-reciprocity check.

    Editorial names are always backlink-exempt; so are the explicit
    BACKLINK_EXEMPT_NAMES (workflows and utility syntheses).
    """
    return filename in EDITORIAL_NAMES or filename in BACKLINK_EXEMPT_NAMES
