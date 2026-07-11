# Session Memory Index

This index catalogs durable memory distilled from AI agent sessions
(OpenCode, Codex, Claude Code, Cursor, Antigravity).

## Entry points
- [[docs/operating-model]] — compile cadence, promotion rules, and the daily ↔ notes boundary.
- Recent daily logs live under `knowledge/daily/` — raw, timestamped session captures awaiting compile.

## Concepts
- [[knowledge/notes/editorial-notes-pattern]] — The `## Editorial note` footer marks a page as **vault metadata** (an editorially maintained navigation or changelog artifact) rather than content derived from a `knowledge/raw/` source.
- [[knowledge/notes/LLM Knowledge Base]] — A personal, LLM-maintained markdown wiki compiled from raw source documents, where the LLM (not the human) writes and curates the durable knowledge layer.
- [[knowledge/notes/Preliminary Flagging]] — A convention for writing wiki pages whose content is inferred from operating instructions rather than grounded in a captured `knowledge/raw/` or `inbox/` source — include the content, but mark it **preliminary** and retire the flag once a source arrives.
- [[knowledge/notes/provenance-rule-6]] — CLAUDE.md rule 6 — "mark uncertainty explicitly" — is the root constraint that justifies preliminary flagging, editorial notes, and every "inferred from…" caveat in this vault.

## Decisions
- [[knowledge/notes/centralized-memory-subsystem]] — The memory subsystem (`run/state.json`, `knowledge/daily/`, `knowledge/notes/`) resolves to a single canonical location regardless of whether Claude Code runs from the main checkout or a git worktree.
- [[knowledge/notes/flag-inferred-content-as-preliminary]] — When writing a wiki page about a topic that has no corresponding `knowledge/raw/` or `knowledge/inbox/` source, mark the inferred sections as **preliminary** rather than omitting them or presenting them as settled.
- [[knowledge/notes/hook-scripts-defense-in-depth]] — Two hardening decisions made 2026-04-19 to prevent silent failures in session hook scripts: a `_resolve_state_root()` fallback when `LLM_WIKI_STATE_ROOT` is unset, and an explicit guard mapping `.`, `..`, or empty slugs to `"root"`.
- [[knowledge/notes/no-gitkeep-in-inbox-articles]] — Do not add `.gitkeep` to `knowledge/inbox/articles/` — the directory will be created on demand by scripts at first use.
- [[knowledge/notes/state-md-exempt-from-lint]] — `state.md` files under `knowledge/projects/<slug>/` are added to `EDITORIAL_NAMES` in `lint_memory.py` and exempted from backlink-obligation and sparse-floor checks, for the same reason that `index.md` and `log.md` are exempt.

## Patterns
- [[knowledge/notes/add-reciprocal-backlinks-at-creation]] — When creating a new synthesis, concept, or decision page that references existing pages, add all reciprocal backlinks to the related pages in the same editing pass — never defer them to a future cleanup round.
- [[knowledge/notes/audit-current-vs-intended]] — An audit page must distinguish "what is true today (dated)" from "what we want it to become", so later readers can tell fact from aspiration.
- [[knowledge/notes/b-sim-hook-testing]] — The full session-start → edit → session-end → reopen lifecycle for project-state hooks can be exercised entirely via direct script invocation with `CLAUDE_PROJECT_DIR=<path>`, covering all automated behaviors without opening a new Claude Code window.
- [[knowledge/notes/docs-portability-absolute-paths]] — Replace hardcoded absolute paths (`<absolute-path>...`) in canonical documentation with `$ENV_VAR (on this machine: <absolute-path>...)` to keep docs portable across machines while preserving a concrete sanity-check reference.
- [[knowledge/notes/editorial-disclaimer-over-history-rewrite]] — When a changelog's historical entries contradict current code or decisions, add an explicit editorial disclaimer paragraph naming the superseded items and the precedence rule rather than rewriting or deleting the original entries.
- [[knowledge/notes/mirror-existing-pipelines]] — When introducing a new subsystem, mirror the structure of an already-working pipeline in the same repo rather than inventing a new shape.

## Debugging
- [[knowledge/notes/case-sensitive-grep-injected-context]] — Grepping the injected `additionalContext` payload with a lowercase pattern silently misses content that was written with an initial capital, producing a false "notes lost" verdict even when the hook ran correctly.
- [[knowledge/notes/edit-multiple-matches]] — When the Edit tool fails because `old_string` matches multiple locations, expand the string with unique preceding context rather than switching to `replace_all`.
- [[knowledge/notes/hook-errors-silent-without-state-root]] — When `LLM_WIKI_STATE_ROOT` is absent from `~/.claude/settings.json::env`, hook scripts cannot locate the error log and silently swallow all failures — "no errors in hook-errors.log" does not mean the hooks ran cleanly.
- [[knowledge/notes/prospective-memory-page-drift]] — Memory and wiki pages written speculatively during planning go stale after implementation completes, producing false descriptions of vault behavior that no lint check will catch.

## Q&A
- [[knowledge/notes/inbox-vs-raw-after-compile]] — Once a source file has been compiled into durable wiki pages, move it from `knowledge/inbox/` to `knowledge/raw/`; `knowledge/inbox/` is staging for *unprocessed* material only.

## Entities
- [[knowledge/notes/Andrej Karpathy]] — AI researcher and educator; source author of the April 2026 X thread that this vault's operating pattern is modeled on.
- [[knowledge/notes/Obsidian]] — Markdown-based note app used as the human-facing viewer for an LLM-maintained vault.

## Syntheses
- [[knowledge/notes/2026-04-13 Three Conventions One Root]] — [[knowledge/notes/editorial-notes-pattern|Editorial Notes Pattern]], [[Preliminary Flagging]], and [[knowledge/notes/pipeline-mirroring|Pipeline Mirroring]] all emerged from the same memory-review session and are three orthogonal operationalizations of a single CLAUDE.md rule — rule 6, *mark uncertainty explicitly*.
- [[knowledge/notes/Global Multi-Project Migration Plan]] — plan for extending LLM-wiki from a single-project vault into a global "second brain" that auto-captures context from every Claude Code session across all projects and restores it on return.
- [[knowledge/notes/Karpathy LLM Wiki Workflow]] — End-to-end pattern from [[Andrej Karpathy]]'s April 2026 thread for turning raw source material into an LLM-maintained markdown wiki viewed through [[Obsidian]].
- [[knowledge/notes/Memory Subsystem Action Plan]] — historical plan for turning `knowledge/notes/` into a compiling knowledge subsystem — most items done, kept as a record of the build and a home for any remaining follow-ups.
- [[knowledge/notes/Product Requirements in the AI Era]] — A 2026 synthesis of how product/business requirements shift toward short, prompt-native specs (PRO), iterative simulation-before-code, and AI-augmented discovery — and where AI still loses to deterministic classical methods.
- [[knowledge/notes/Wiki vs Memory Compiler vs Fusion]] — Three approaches to giving an LLM durable, reusable knowledge outside the context window — differing in *what* they persist (curated source knowledge vs. session-derived behavior) and *who* drives compilation (explicit ingest vs. passive accumulation).

## Workflows
- [[knowledge/notes/Ingestion Workflow]] — New source material is captured into `knowledge/inbox/` or `knowledge/raw/`, then compiled into durable wiki pages.
- [[knowledge/notes/Retrieval Workflow]] — Answers should come from the compiled wiki first, then from raw material only when needed, with the exact strategy picked by vault size.
- [[knowledge/notes/Review Workflow]] — The wiki should be pruned, linked, and quality-checked regularly.

## Raw sources
- [[knowledge/notes/Karpathy X Thread - April 2026]] — Durable wiki-side pointer to the April 2026 X thread by [[Andrej Karpathy]] introducing the "LLM knowledge base" pattern — exists so the vault is not solely dependent on the external post.

## Editorial note
This index is vault metadata — a navigation map over `knowledge/notes/`, not a page derived from `raw/` or `inbox/`. It is regenerated by `scripts/rebuild_memory_index.py`; edits to page titles or one-sentence summaries will be picked up on the next rebuild.
