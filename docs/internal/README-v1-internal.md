# LLM Wiki — a Karpathy-style vault with session memory

> **ARCHIVED (pre three-zone / v1).** Historical reference only.
> Current layout and contracts: `docs/ARCHITECTURE.md`, root `CLAUDE.md`, `docs/AGENTS.md`.
> Do not follow `memory/`, root `wiki/`, `memory-state/`, or root `raw/`/`inbox/` paths from this doc.

A persistent markdown knowledge base that fuses two complementary patterns:
- **Karpathy's LLM-maintained wiki** — curated external knowledge compiled from `raw/` sources into durable `.md` pages readable in Obsidian.
- **Auto-memory from session hooks** — project-internal lore (decisions, patterns, gotchas, Q&A) captured from Claude Code sessions, then compiled into `knowledge/notes/`.

The two layers are kept deliberately separate and bridged by an explicit promotion step.

## Pipeline at a glance

```
┌────────────────── EXTERNAL KNOWLEDGE (wiki) ──────────────────┐
│                                                               │
│   raw/           inbox/              knowledge/notes/                    │
│   ─────    ──►   ──────    ──►       ─────                    │
│   immutable      staging             concepts/                │
│   sources        (unprocessed)       entities/                │
│                                      syntheses/               │
│                                      comparisons/             │
│                                      connections/             │
│                                      qa/                      │
│                                      raw-sources/             │
│                                      index.md  +  log.md      │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                              ▲
                              │   bridge-promote-insight
                              │   (explicit, bidirectional)
                              ▼
┌──────────────── SESSION MEMORY (memory) ──────────────────────┐
│                                                               │
│   Claude Code session                                         │
│        │                                                      │
│        │  PreCompact / SessionEnd hooks                       │
│        ▼                                                      │
│   scripts/flush_memory.py   (detached, SDK, FLUSH_OK aware)   │
│        │                                                      │
│        ▼                                                      │
│   knowledge/daily/YYYY-MM-DD.md         (raw, append-only)       │
│        │                                                      │
│        │   /session-memory-compile    or auto-spawn           │
│        │                               after 18:00 local      │
│        ▼                                                      │
│   knowledge/notes/                                           │
│     ├─ concepts/      (noun-shaped mental models)             │
│     ├─ decisions/     (dated choice + rejected alternatives)  │
│     ├─ patterns/      (verb-shaped "when X, do Y because Z")  │
│     ├─ debugging/     (symptom → cause → resolution)          │
│     └─ qa/            (settled answer to recurring Q)         │
│   knowledge/index.md (auto-rebuilt)  +  knowledge/log.md            │
│                                                               │
└───────────────────────────────────────────────────────────────┘

Runtime state (outside the vault, default sibling dir next to $LLM_WIKI_ROOT;
on this machine: $LLM_WIKI_STATE_ROOT/, overridable via $LLM_WIKI_STATE_ROOT):
  memory-state/state.json       hashes, dedupe, compile triggers
  memory-reports/               lint reports, compile logs, SessionStart dumps
  qmd/index.sqlite              QMD hybrid lex+vec index
```

## Why two layers

- **`knowledge/notes/` is third-person, citeable, shareable.** A reader unfamiliar with this repo should still get value from it. Every factual claim carries a `Source:` pointer.
- **`memory/` is first-person project lore.** How we work here. References specific incidents, hooks, file layouts. Only meaningful with repo context.
- **Promotion is explicit**, not implicit. When a memory insight generalizes, `bridge-promote-insight` lifts it into `knowledge/notes/` with reciprocal links in both directions. See `docs/operating-model.md` for the two-question checklist that routes new content.

## Directory map

| Path | Role |
|---|---|
| `raw/` | Immutable source-of-truth inputs (articles, papers, captured threads). Never edited. |
| `inbox/` | Staging for newly captured material **not yet compiled**. Moves to `raw/` once compiled. |
| `knowledge/notes/` | Compiled external knowledge — concepts, entities, syntheses, comparisons, connections, Q&A, raw-sources. |
| `knowledge/daily/` | Append-only raw session captures from hooks. Immutable. |
| `knowledge/notes/` | Compiled project lore across five categories. |
| `.claude/` | Claude Code project config — `settings.json`, `rules/`, `skills/`. |
| `scripts/` | Python automation: hook wrappers, flush, compile, lint, query, rebuild-index. |
| `outputs/` | Derived artifacts (exports, generated slides/images). |
| `$LLM_WIKI_STATE_ROOT/run/state.json` | **Runtime**, outside vault. Machine state: SHA-256 hashes, dedupe, compile triggers, last-flush timestamps. Default path: sibling of `$LLM_WIKI_ROOT` named `LLM-wiki-state/`. |
| `$LLM_WIKI_STATE_ROOT/logs/` | **Runtime**, outside vault. Lint reports, spawned-compile stdout/stderr, SessionStart debug dumps. |
| `$LLM_WIKI_STATE_ROOT/qmd/index.sqlite` | **Runtime**, outside vault. QMD hybrid lex+vec search index. |

## Automation

- **Hooks** (`.claude/settings.json` → `scripts/session_end_capture.py`, `precompact_capture.py`, `session_start_context.py`).
  - `SessionEnd` / `PreCompact` spawn `flush_memory.py` detached, which uses the Claude Agent SDK to distill the transcript and append to `knowledge/daily/`. The summarizer returns `FLUSH_OK` when nothing is worth persisting — the block is then skipped entirely.
  - `SessionStart` injects a ~2 KB context slice (trimmed index + latest daily excerpt + recent log) into every new session.
  - All wrappers short-circuit on `CLAUDE_INVOKED_BY` to prevent recursion when the memory automation itself spawns SDK sub-sessions.

- **Auto-compile after 18:00.** `flush_memory.py` checks `MEMORY_COMPILE_AFTER_HOUR` (default 18) and, if today's daily log hash has changed since the last compile, spawns `compile_memory.py` detached.

- **SHA-256 incrementality.** `compile_memory.py` reads `$LLM_WIKI_STATE_ROOT/run/state.json` and skips daily logs whose hash matches the last compile.

- **Lint.** `scripts/lint_memory.py` runs seven checks over both `memory/` and `knowledge/notes/`: broken wikilinks, orphan pages, orphan daily logs, stale compiled pages, missing backlinks, sparse pages (<200 words by default), and — opt-in via `--contradictions` — LLM-judged contradictions.

- **Regression tests.** `tests/` holds a pytest suite covering the critical semantics surfaced by audit rounds — slug collision resolution, compile fail-safe, context noise stripping, Unicode slugify, SessionEnd skip rules. **Hermetic** — runs green on a fresh clone with no pre-configured env vars (`conftest.py` bootstraps `LLM_WIKI_ROOT` / `LLM_WIKI_STATE_ROOT` and creates a skeleton `state.json`). Run with `uv run pytest tests/` (37 tests, ~2s). See `tests/README.md` for coverage map and CI isolation.

- **Tiered retrieval.** `scripts/lookup_mode.py` prints the recommended `/knowledge-lookup` tier for the current vault size: **DIRECT** (<50 curated pages) reads `knowledge/index.md` + target pages, no QMD; **HYBRID** (50–300) is wiki-first with `qmd search` / `qmd query` fallback; **QMD** (>300) makes the hybrid ranker primary. The helper also reports QMD index age (default `$LLM_WIKI_STATE_ROOT/qmd/index.sqlite`, machine-local) and warns when it's stale (>24h). Current vault: 16 curated pages → DIRECT. Run `python scripts/lookup_mode.py` for the live count.

## Skills (slash commands)

| Skill | Purpose |
|---|---|
| `/knowledge-compile` | Compile `raw/` / `inbox/` material into new or updated `knowledge/notes/` pages. |
| `/knowledge-lookup` | Answer from `knowledge/notes/` first, fall back to QMD search only when needed. |
| `/knowledge-review` | Structural review pass over `knowledge/notes/` (orphans, links, sourcing, size). |
| `/session-memory-compile` | Run `compile_memory.py` over changed daily logs. |
| `/session-memory-review` | Review pass over `knowledge/notes/`. |
| `/bridge-promote-insight` | Lift a memory page into `knowledge/notes/` with reciprocal links. |
| `/knowledge-qa-file-back` | Promote a just-answered question into `knowledge/notes/` or `knowledge/notes/qa/`. |
| `/contradict-check` | LLM-judged contradiction check — wraps `lint_memory.py --contradictions`. Installable as a git pre-commit hook. |

## Contract

- `raw/` is immutable. Never edit files there.
- Every `knowledge/notes/` claim carries a `Source:` line.
- Uncertainty is marked explicitly — see `knowledge/notes/Preliminary Flagging.md`.
- Vault-metadata pages (indexes, logs, Vault Home) carry a `## Editorial note` footer — see `knowledge/notes/Editorial Notes Pattern.md`.
- Contradictions are surfaced, not silently resolved — superseded claims keep their history (CLAUDE.md rule 7).

## Getting started

1. Clone and point Obsidian at the project root as a vault.
2. Drop a source file into `inbox/articles/` (or use Obsidian Web Clipper).
3. Run `/knowledge-compile` to lift it into `knowledge/notes/`. Move the processed file into `raw/`.
4. Work normally in Claude Code. `SessionEnd` automatically captures daily summaries.
5. Periodically run `/session-memory-compile` to distill daily logs into `knowledge/notes/`.
6. Run `python scripts/lint_memory.py` weekly; `/contradict-check` before significant commits.

See `CLAUDE.md` for the full project contract and `docs/operating-model.md` for the `memory/` ↔ `knowledge/notes/` routing rules.
