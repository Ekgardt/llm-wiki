# Session Memory Index

This index catalogs durable memory distilled from Claude Code sessions.

## Entry points
- [[memory/operating-model]] — compile cadence, promotion rules, and the `memory/` ↔ `wiki/` boundary.
- Recent daily logs live under `memory/daily/` — raw, timestamped session captures awaiting compile.

## Concepts
- [[memory/knowledge/concepts/editorial-notes-pattern]] — The `## Editorial note` footer marks a page as **vault metadata** (an editorially maintained navigation or changelog artifact) rather than content derived from a `raw/` source.
- [[memory/knowledge/concepts/pipeline-mirroring]] — Noun-form counterpart of the pattern [[memory/knowledge/patterns/mirror-existing-pipelines]] — the rule that a new knowledge subsystem should reuse the shape of an existing pipeline.
- [[memory/knowledge/concepts/provenance-rule-6]] — CLAUDE.md rule 6 — "mark uncertainty explicitly" — is the root constraint that justifies preliminary flagging, editorial notes, and every "inferred from…" caveat in this vault.

## Decisions
- [[memory/knowledge/decisions/centralized-memory-subsystem]] — The memory subsystem (`memory/state/`, `memory/daily/`, `memory/knowledge/`) resolves to a single canonical location regardless of whether Claude Code runs from the main checkout or a git worktree.
- [[memory/knowledge/decisions/flag-inferred-content-as-preliminary]] — When writing a wiki page about a topic that has no corresponding `raw/` or `inbox/` source, mark the inferred sections as **preliminary** rather than omitting them or presenting them as settled.
- [[memory/knowledge/decisions/hook-scripts-defense-in-depth]] — Two hardening decisions made 2026-04-19 to prevent silent failures in session hook scripts: a `_resolve_state_root()` fallback when `LLM_WIKI_STATE_ROOT` is unset, and an explicit guard mapping `.`, `..`, or empty slugs to `"root"`.
- [[memory/knowledge/decisions/no-gitkeep-in-inbox-articles]] — Do not add `.gitkeep` to `inbox/articles/` — the directory will be created on demand by scripts at first use.
- [[memory/knowledge/decisions/state-md-exempt-from-lint]] — `state.md` files under `wiki/projects/<slug>/` are added to `EDITORIAL_NAMES` in `lint_memory.py` and exempted from backlink-obligation and sparse-floor checks, for the same reason that `index.md` and `log.md` are exempt.

## Patterns
- [[memory/knowledge/patterns/add-reciprocal-backlinks-at-creation]] — When creating a new synthesis, concept, or decision page that references existing pages, add all reciprocal backlinks to the related pages in the same editing pass — never defer them to a future cleanup round.
- [[memory/knowledge/patterns/audit-current-vs-intended]] — An audit page must distinguish "what is true today (dated)" from "what we want it to become", so later readers can tell fact from aspiration.
- [[memory/knowledge/patterns/b-sim-hook-testing]] — The full session-start → edit → session-end → reopen lifecycle for project-state hooks can be exercised entirely via direct script invocation with `CLAUDE_PROJECT_DIR=<path>`, covering all automated behaviors without opening a new Claude Code window.
- [[memory/knowledge/patterns/docs-portability-absolute-paths]] — Replace hardcoded absolute paths (`<absolute-path>...`) in canonical documentation with `$ENV_VAR (on this machine: <absolute-path>...)` to keep docs portable across machines while preserving a concrete sanity-check reference.
- [[memory/knowledge/patterns/editorial-disclaimer-over-history-rewrite]] — When a changelog's historical entries contradict current code or decisions, add an explicit editorial disclaimer paragraph naming the superseded items and the precedence rule rather than rewriting or deleting the original entries.
- [[memory/knowledge/patterns/mirror-existing-pipelines]] — When introducing a new subsystem, mirror the structure of an already-working pipeline in the same repo rather than inventing new shape.

## Debugging
- [[memory/knowledge/debugging/case-sensitive-grep-injected-context]] — Grepping the injected `additionalContext` payload with a lowercase pattern silently misses content that was written with an initial capital, producing a false "notes lost" verdict even when the hook ran correctly.
- [[memory/knowledge/debugging/edit-multiple-matches]] — When the Edit tool fails because `old_string` matches multiple locations, expand the string with unique preceding context rather than switching to `replace_all`.
- [[memory/knowledge/debugging/hook-errors-silent-without-state-root]] — When `LLM_WIKI_STATE_ROOT` is absent from `~/.claude/settings.json::env`, hook scripts cannot locate the error log and silently swallow all failures — "no errors in hook-errors.log" does not mean the hooks ran cleanly.
- [[memory/knowledge/debugging/prospective-memory-page-drift]] — Memory and wiki pages written speculatively during planning go stale after implementation completes, producing false descriptions of vault behavior that no lint check will catch.

## Q&A
- [[memory/knowledge/qa/inbox-vs-raw-after-compile]] — Once a source file has been compiled into durable wiki pages, move it from `inbox/` to `raw/`; `inbox/` is staging for *unprocessed* material only.

## Editorial note
This index is vault metadata — a navigation map over `memory/knowledge/`, not a page derived from `raw/` or `inbox/`. It is regenerated by `scripts/rebuild_memory_index.py`; edits to page titles or one-sentence summaries will be picked up on the next rebuild.
