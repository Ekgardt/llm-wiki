---
type: synthesis
title: "Memory Subsystem Action Plan"
description: "historical plan for turning `knowledge/notes/` into a compiling knowledge subsystem — most items done, kept as a record of the build and a home for any remaining follow-ups."
timestamp: 2026-07-03T05:41:37
confidence: medium
source_authority: user
---
# Memory Subsystem Action Plan

One-sentence summary: historical plan for turning `knowledge/notes/` into a compiling knowledge subsystem — most items done, kept as a record of the build and a home for any remaining follow-ups.

## Current state (updated 2026-04-18)
The subsystem is **operational**: daily captures flow from hooks → `daily/` → compiled knowledge pages → index → log. Automation (flush, compile, lint) runs via `scripts/` with state in `$LLM_WIKI_STATE_ROOT/run/` (inside the vault; default: `$LLM_WIKI_STATE_ROOT/run/`). The `knowledge/daily/` ↔ `knowledge/notes/` boundary is codified with a two-question checklist. Promotion via `bridge-promote-insight` has been exercised once (`decisions/flag-inferred-content-as-preliminary` → `[[Preliminary Flagging]]`). All original action items are done; the original open questions are resolved (see below).

## Intended model (how it *should* work)
Mirror the `knowledge/raw/ → knowledge/inbox/ → knowledge/notes/` pipeline from [[Karpathy LLM Wiki Workflow]], but scoped to *session artifacts* rather than external sources:

- `knowledge/daily/YYYY-MM-DD.md` = raw layer (immutable session captures: compact summaries, session-end notes).
- `knowledge/notes/<slug>.md` = compiled layer (durable, deduplicated pages by type).
- `knowledge/index.md` = navigation map (mirrors [[index]]'s role for the wiki).
- `knowledge/log.md` = editorial changelog of compile passes.

Five knowledge types, each with a clear inclusion rule:
- **decisions** — choices made and the reason (why we picked X over Y).
- **patterns** — repeatable approaches, conventions, recipes that worked.
- **debugging** — gotchas, root-cause notes, failure modes seen.
- **concepts** — project-internal vocabulary that needs a definition.
- **qa** — resolved questions worth caching (the answer, not the transcript).

## Action plan

1. **Extend `operating-model.md` with a Compile Procedure section.** **Done 2026-04-13.** Expanded 2026-04-14 with the explicit two-question boundary table and worked examples. Current state covers trigger, inclusion filter, five knowledge categories with definitions, `do not lift` exclusions, index-update procedure, and the `knowledge/daily/` ↔ `knowledge/notes/` boundary check.
2. **Do a first compile pass over `knowledge/daily/2026-04-13.md`.** **Done 2026-04-13** — extracted two patterns, one decision, one debugging note; added three concepts on the second pass. Follow-up passes 2026-04-14 expanded four of the compiled pages with content and backlinks; lint reports 0 findings across 7 checks.
3. **Add editorial notes to `knowledge/index.md` and `knowledge/log.md`.** **Done 2026-04-18.** `knowledge/index.md` gets its `## Editorial note` footer via the `scripts/rebuild_memory_index.py` template (implemented 2026-04-13). `knowledge/log.md` got its own `## Editorial note` footer added by hand 2026-04-18 — the log is append-only and not regenerated, so a template-based approach didn't apply.
4. **Define the boundary with `knowledge/notes/`.** **Codified 2026-04-13**, improved 2026-04-14. The rule now lives in `docs/operating-model.md` as a two-question checklist plus three worked examples; expanded criteria are provided for the ambiguous cases. First promotion bridge executed: memory decision → [[Preliminary Flagging]].

## Resolved questions (2026-04-18 review)

- **Manual vs automatic compile passes?** **Hybrid.** Automatic compile spawns from `flush_memory.py` after `MEMORY_COMPILE_AFTER_HOUR` (default 18:00) when the day's daily hash has changed; manual compile is still available via `/session-memory-compile` for mid-day or mid-initiative consolidation. Documented in `docs/AGENTS.md` under Automation contract.
- **`qa` vs `decisions` overlap?** **Kept separate.** The type tie-breaker rule in `operating-model.md` resolves ambiguity — an item that fits both goes to the more actionable side (patterns > concepts; debugging > qa). First real Q&A entry exists (`inbox-vs-raw-after-compile.md`), confirming the type earns its slot.
- **Pruning of `knowledge/daily/` once entries are compiled?** **Never delete, archive later.** Policy: daily logs are append-only and kept indefinitely. When `knowledge/daily/` grows beyond comfortable Obsidian navigation (~30–50 files), compiled logs older than ~90 days get moved into `knowledge/daily/archive/YYYY-MM/`; un-compiled logs stay in the flat directory regardless of age so the next compile pass still finds them. No automation yet — current file count doesn't warrant it. A future `scripts/archive_daily.py` will gate moves on `compiled_daily_hashes` (state.json) plus mtime when needed.

## Remaining follow-ups
None blocking. Two dormant items that will surface when volume demands it:
- Archive automation (`scripts/archive_daily.py`) — write when `daily/` passes ~30 files.
- Periodic review of this plan itself — revisit if the subsystem grows a new layer or automation contract changes.

## Source
- Live audit of `$LLM_WIKI_ROOT/knowledge/notes/` on 2026-04-13, revisited 2026-04-18.
- `docs/operating-model.md`.
- `docs/AGENTS.md`.
- [[Karpathy LLM Wiki Workflow]] (pipeline analogy).

## Related
- [[Karpathy LLM Wiki Workflow]]
- [[Wiki vs Memory Compiler vs Fusion]]
- [[Ingestion Workflow]]
- [[Review Workflow]]
- [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] — the vault convention this audit used to frame the intended model.
- [[Preliminary Flagging]] — sibling convention whose decision was promoted during the same pass.
- [[2026-04-13 Three Conventions One Root|Three Conventions, One Root]] — connection page synthesizing the three conventions surfaced by this audit.
- [[Global Multi-Project Migration Plan]] — later multi-phase plan that extends the memory/wiki pipeline set up here into a global multi-project model.
- [[add-reciprocal-backlinks-at-creation]] — this plan's execution followed the reciprocal-backlink pattern.
- [[audit-current-vs-intended]] — the audit pattern was applied during this plan's build-out.
- [[mirror-existing-pipelines]] — the memory subsystem mirrored existing pipeline shapes per this pattern.
