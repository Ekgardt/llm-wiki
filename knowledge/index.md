# Session Memory Index

This index catalogs durable memory distilled from AI agent sessions
(OpenCode, Codex, Claude Code).

## Entry points
- [[docs/operating-model]] — compile cadence, promotion rules, and the daily ↔ notes boundary.
- Recent daily logs live under `knowledge/daily/` — raw, timestamped session captures awaiting compile.

## Concepts
- [[knowledge/notes/editorial-notes-pattern]] — The `## Editorial note` footer marks a page as **vault metadata** (an editorially maintained navigation or changelog artifact) rather than content derived from a `knowledge/raw/` source.
- [[knowledge/notes/LLM Knowledge Base]] — A personal, LLM-maintained markdown wiki compiled from raw source documents, where the LLM (not the human) writes and curates the durable knowledge layer.
- [[knowledge/notes/Preliminary Flagging]] — A convention for writing wiki pages whose content is inferred from operating instructions rather than grounded in a captured `knowledge/raw/` or `inbox/` source — include the content, but mark it **preliminary** and retire the flag once a source arrives.
- [[knowledge/notes/provenance-rule-6]] — CLAUDE.md rule 6 — "mark uncertainty explicitly" — is the root constraint that justifies preliminary flagging, editorial notes, and every "inferred from…" caveat in this vault.

## Decisions
- [[knowledge/notes/agent-native-mcp-foundation]] — LLM Wiki uses MCP as the common read/action interface for every agent while host-specific hooks remain thin lifecycle-event adapters.
- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]] — Audit closure retires the unsupported Cognee bridge and adds one fail-closed DLP, recovery, install, scheduling, coordination, and evidence contract without another daemon, database, or MCP tool.
- [[knowledge/notes/automatic-code-update-decision]] — the nightly pass may advance the checkout to the remote
- [[knowledge/notes/baseline-environment-binding-decision]] — The frozen retrieval-v2 baseline is bound to the exact
- [[knowledge/notes/blackboard-fenced-resource-claims-decision]] — Blackboard coordination uses two bounded tables in the existing coordinator-v3 database for atomic resource claims while immutable coordination history remains authoritative Markdown.
- [[knowledge/notes/centralized-memory-subsystem]] — The memory subsystem (`run/state.json`, `knowledge/daily/`, `knowledge/notes/`) resolves to a single canonical location regardless of whether Claude Code runs from the main checkout or a git worktree.
- [[knowledge/notes/citation-relevance-gate-decision]] — A citation that shares no content with the claim it is offered for is rejected; entailment itself is still not verified and is not claimed.
- [[knowledge/notes/classification-measurement-stand-decision]] — Session classification is measured by a labelled corpus and three gates; the shipped corpus is public and small, and the real number needs real sessions.
- [[knowledge/notes/cross-lingual-citation-relevance-decision]] — Word overlap decides a citation only within one script; across scripts only tokens that survive translation count, and where there are none the gate abstains.
- [[knowledge/notes/daily-entry-quote-anchor-decision]] — A daily entry starts at a timestamp heading or an operation marker, and evidence binds to the entry that contains its quote — the timestamp narrows the search, the quote settles it.
- [[knowledge/notes/dead-task-retirement-and-restore-decision]] — A task whose attempts are exhausted is retired only when asked for by name, through the same verified export, and one command brings its work back.
- [[knowledge/notes/derived-evidence-generation-decision]] — Evidence retrieval uses disposable immutable cache generations while Markdown, Git, and project journals remain authoritative.
- [[knowledge/notes/durable-capture-producer-activation-decision]] — SessionEnd and PreCompact capture publish durable Reliability V3 intent evidence before returning; detached work only wakes recovery and deletion requires terminal proof.
- [[knowledge/notes/flag-inferred-content-as-preliminary]] — When writing a wiki page about a topic that has no corresponding `knowledge/raw/` or `knowledge/inbox/` source, mark the inferred sections as **preliminary** rather than omitting them or presenting them as settled.
- [[knowledge/notes/hook-scripts-defense-in-depth]] — Two hardening decisions made 2026-04-19 to prevent silent failures in session hook scripts: a `_resolve_state_root()` fallback when `LLM_WIKI_STATE_ROOT` is unset, and an explicit guard mapping `.`, `..`, or empty slugs to `"root"`.
- [[knowledge/notes/idempotent-retry-after-quarantine-decision]] — a refused write keeps its idempotency key and its evidence,
- [[knowledge/notes/install-ownership-control-plane-decision]] — Profile, user environment, and native scheduler mutations use one bounded `run/install` manifest and resumable transaction that fail closed on ambiguous ownership or external drift.
- [[knowledge/notes/integration-config-backup-retention-decision]] — Claude and Codex configuration merges create verified byte-exact sibling preimages and retain only bounded LLM-Wiki-owned backups without claiming filesystem metadata preservation.
- [[knowledge/notes/lsp-live-lease-decision]] — Live LSP lifecycle ownership is represented by one bounded mutable lease anchored inside the immutable owner directory.
- [[knowledge/notes/lsp-process-containment-decision]] — Windows Job Objects own assigned LSP trees, while POSIX process groups cover only pinned Pyright descendants that remain in the group.
- [[knowledge/notes/nightly-builds-generation-vectors-decision]] — semantic retrieval stops being code that only tests
- [[knowledge/notes/no-gitkeep-in-inbox-articles]] — Do not add `.gitkeep` to `knowledge/inbox/articles/` — the directory will be created on demand by scripts at first use.
- [[knowledge/notes/observable-capture-and-bounded-maintenance-decision]] — A failed capture is recorded durably instead of vanishing,
- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]] — Typed provenance multiplies the score that decides the order on every retrieval path, from one shared table, and the weight is reported.
- [[knowledge/notes/oversized-daily-compile-decision]] — a daily log larger than the compile input budget should be
- [[knowledge/notes/part-scoped-evidence-decision]] — A compiled page cites the bytes of the compile part it was written from, and every reader verifies that citation by finding an entry-aligned slice, starting where a part starts, whose bytes still hash to what the page recorded.
- [[knowledge/notes/read-only-lsp-navigation-engine-decision]] — LLM Wiki will keep its structural Evidence Graph and add an owned read-only LSP engine for precise live navigation, starting with production-quality Python support.
- [[knowledge/notes/reliability-v3-runtime-adoption-implementation-decision]] — The approved Reliability V3 operational database pair may now be implemented with explicit offline adoption, retained v2 evidence, immutable tombstones, and no change to Markdown authority or runtime roots.
- [[knowledge/notes/reliable-memory-stage-2]] — Stage 2 keeps Markdown authoritative while adding recoverable transactions, durable checkpoints, safe archives, versioned compile caching, a fenced priority queue, and evidence-backed claims.
- [[knowledge/notes/retire-cursor-and-antigravity-decision]] — Cursor and Antigravity stop being supported hosts of LLM Wiki;
- [[knowledge/notes/secret-shape-not-secret-name-decision]] — A key named `token` proves nothing about what follows it,
- [[knowledge/notes/self-resolving-health-findings-decision]] — a health finding describes a live condition and returns to
- [[knowledge/notes/session-evidence-retention-decision]] — every session leaves a redacted, searchable copy of itself
- [[knowledge/notes/session-promotion-policy-decision]] — every session is kept regardless of what any classifier
- [[knowledge/notes/single-directory-vault-decision]] — The vault and the public source live in one directory;
- [[knowledge/notes/solo-operator-superset-product-decision]] — LLM Wiki is the single local-first memory, code-intelligence, and agent-control product for one operator managing many agents and sessions.
- [[knowledge/notes/state-md-exempt-from-lint]] — `state.md` files under `knowledge/projects/<slug>/` are added to `EDITORIAL_NAMES` in `lint_memory.py` and exempted from backlink-obligation and sparse-floor checks, for the same reason that `index.md` and `log.md` are exempt.
- [[knowledge/notes/system-symlink-ancestor-decision]] — A bounded read accepts a symlinked ancestor only when
- [[knowledge/notes/v4-reliability-contracts-decision]] — The approved, not-yet-implemented V4 reliability target would add path-bound compile receipts, truthful queue serialization, durable capture intents, unified fenced ownership, and bounded execution while preserving Markdown authority and the 12-tool local runtime.
- [[knowledge/notes/warm-navigation-overhead-threshold-decision]] — The warm navigation overhead gate is 30 ms p95, measured on the slowest supported machine class rather than on a quiet one.

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
- [[knowledge/notes/Obsidian]] — Obsidian is a Markdown-based note app used here only as an optional human-facing viewer for an LLM-maintained vault.

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
