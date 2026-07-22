---
type: decision
title: "Persistent Code-Intelligence Kernel Target"
description: "LLM Wiki will add a persistent code-intelligence kernel through Evidence Graph v3 without adding another publication or agent interface boundary."
date: 2026-07-21
confidence: high
source_authority: user
status: superseded
superseded_by: "[[read-only-lsp-navigation-engine-decision]]"
---
# Persistent Code-Intelligence Kernel Target

> Superseded on 2026-07-22 by [[read-only-lsp-navigation-engine-decision]] after
> implementation Tasks 1-5. The completed foundation remains; the one-shot
> consent/SCIP/publication direction in Tasks 6-16 does not.

One-sentence summary: LLM Wiki will add a persistent code-intelligence kernel through Evidence Graph v3 without adding another publication or agent interface boundary.

## Decision

Date: 2026-07-21.

This page records an approved target, not implemented behavior. The current
checkpoint remains `corpus-generation/v2` with `evidence-graph/v2` until the
implementation and verification tasks in the Plan A implementation plan pass.

The approved target builds `evidence-graph/v3` in a newly built immutable generation
within the existing `corpus-generation/v2` generation layout, catalog, and
publication boundary. After complete validation, that new generation is atomically
activated. v2 generations remain readable for structural capabilities. The target
adds no second graph, catalog, active pointer, runtime root, persistent daemon, or
MCP tool. It preserves exactly 12 task-shaped tools and Python 3.10 support.

Precise analyzer execution requires repository/analyzer/exact-invocation consent
in `run/code-analysis-consent.sqlite3`. Sealed analyzer scratch state uses
`run/analyzer-runs/<filesystem-run-id>/`. Operational SQLite remains
rollback-journal, `synchronous=FULL`, and no WAL.

## Rationale

One immutable, repository-scoped generation keeps structural, search, and precise
code evidence on the same publication boundary. Versioned graph schemas allow new
precision and coverage contracts without reinterpreting existing v2 data. Exact
invocation consent prevents an analyzer approval from silently expanding to a
different executable or argument set, while sealed scratch input keeps precise
analysis away from the live checkout.

Keeping the existing catalog, active pointer, runtime root, MCP surface, and
one-shot execution model avoids parallel sources of truth and preserves the
local-first operating contract. Python 3.10 remains the compatibility floor for
Plan A.

## Rejected alternatives

- A second graph, catalog, or active pointer was rejected because independently
  selected code and knowledge snapshots could disagree.
- An in-place mutation of Evidence Graph v2 was rejected because old readers and
  generations need an exact, readable structural contract.
- Broad analyzer consent was rejected because repository, analyzer, executable,
  and arguments all affect what code can run.
- Analysis against the live checkout was rejected because analyzer writes or
  concurrent source changes could invalidate the captured evidence.
- A persistent analyzer daemon or new MCP tool was rejected because bounded
  one-shot work and the existing 12 tools satisfy the approved boundary.

## Consequences

Plan A must prove schema validation, v2 readability, complete freshness identity,
consent enforcement, sealed scratch verification, truthful coverage, publication,
and recovery before any documentation may report v3 as current behavior. Until
then, readers and operators continue to rely on `corpus-generation/v2` and
`evidence-graph/v2`.

Analyzer consent and scratch data under `run/` are an approved target, not current
behavior. Plan A must extend doctor and deletion eligibility for live or abandoned
analyzer jobs, retained analyzer receipts, and consent, quarantine, or unreadable
analyzer state before implementation is reported complete. Until those protections
pass, the current runtime deletion contract must not be described as covering
analyzer state. Evidence Graph v3 remains disposable derived state built in a new
generation within the existing `corpus-generation/v2` generation layout and
publication boundary.

## Source / Evidence

- User-approved design and implementation direction, 2026-07-21 OpenCode session.
- `docs/superpowers/specs/2026-07-21-persistent-code-intelligence-kernel-design.md`
- `docs/superpowers/plans/2026-07-21-code-kernel-foundation-python.md`, Task 1.
- Python 3.10 SQLite interface: https://docs.python.org/3.10/library/sqlite3.html
- SQLite journal modes: https://www.sqlite.org/pragma.html#pragma_journal_mode

## Related

- [[read-only-lsp-navigation-engine-decision]]
- [[solo-operator-superset-product-decision]]
- [[derived-evidence-generation-decision]]
