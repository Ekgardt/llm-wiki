# LLM Wiki — Agent Operating Contract

You are working in the **LLM-wiki** memory system — a local, file-based,
git-native knowledge base for multi-agent memory. This file is the canonical
operating contract for any AI agent (Claude Code, OpenCode, Codex, Cursor,
Antigravity) editing this repository. `AGENTS.md` and `CLAUDE.md` are kept
byte-identical so every agent reads the same rules regardless of which file
it loads.

---

## 0. Process rules (mandatory)

### How to talk to the user
- Write in **plain human language**. Short sentences.
- Avoid jargon stacks, audit IDs, severity tables unless the user explicitly
  asked for a technical report.
- After any task: **what happened**, **what it means**, **what (if anything)
  they should do** — in that order.
- If nothing is required from the user, say so explicitly.
- Match the user's language (Russian → Russian, English → English).

### Architecture changes require explicit sign-off
Before changing **structure, paths, env contracts, or runtime location**:
1. Describe the proposed change in plain language (what, why, impact).
2. Get the user's explicit "yes".
3. Record the decision in `knowledge/notes/` (decision page) and update
   `docs/STRUCTURE.md` (the canonical structure reference).
4. Only then write code.
Never improvise architectural decisions mid-task. When unsure, ask.

### Release / docs sync
- Before any release or version bump: **sync `README.md` + `README.ru.md` +
  `README.zh-CN.md` in the same change**.
- Never leave RU/ZH with stale test counts, install URLs, or architecture.
- Run `uv run pytest tests/test_readme_i18n.py -q` after README edits.
- Update `CHANGELOG.md` (Keep-a-Changelog format) and `pyproject.toml`
  `version` in the same change.
- See `CONTRIBUTING.md` → Release checklist.

---

## 1. Three-zone layout (canonical)

The repository is organized into three zones. This layout is enforced by
`tests/test_structure.py` — do not break it.

```
# CODE (tracked in git)
scripts/   tests/   docs/   skills/   rules/   integrations/   benchmark/

# KNOWLEDGE (tracked in git — public examples only in source;
#            full user data lives in the installed vault)
knowledge/
  daily/      # append-only session capture
  notes/      # durable compiled pages (OKF frontmatter, flat slugs)
  projects/   # per-project state.md / context
  raw/        # immutable sources
  inbox/      # unprocessed staging
  feedback/   # correction candidates

# RUNTIME (inside the vault, gitignored; mixed derived and durable state)
# Override root via LLM_WIKI_STATE_ROOT (tests use a temp dir).
cache/     # regenerable search / QMD / vector indexes
logs/      # rotatable lint, compile, and SessionStart diagnostics
run/       # durable state, queue, compile journals/manifests, repair transactions
```

**Env contracts:**
- `$LLM_WIKI_ROOT` → vault root (the repository root). Default: resolved from
  `scripts/` location, worktree-aware.
- `$LLM_WIKI_STATE_ROOT` → runtime root. **Default: the vault itself** →
  `cache/` (incl. `cache/cognee/`), `logs/`, `run/` at vault root, all gitignored.
  Override for multi-disk setups or hermetic tests.
- `$MEMORY_LLM_PROVIDER` → `fake` (tests), `opencode-sdk` (active OpenCode
  service bridge), or one of `opencode|codex|claude|openai|ollama` (runtime,
  auto-detected synchronous backends).
- `$XDG_CONFIG_HOME` → OpenCode's effective user config root only when
  absolute. Unset, empty, or relative values fall back to `~/.config`.
  Windows installers also write the normalized `~/.config/opencode`
  compatibility target when it differs; Unix installs only to the effective
  XDG target.

**Forbidden at vault root:** `wiki/`, `memory/`, `outputs/`, `state/`,
`LLM-wiki-state/` (legacy sibling layout — removed). Runtime lives **inside**
the vault under gitignored `cache/logs/run/`.

---

## 2. Public source vs installed instance

This repository is the **public source** (dev). The **installed, running**
memory system — with real user data — lives at `$LLM_WIKI_ROOT` on the
operator's machine (a separate clone, pull-only).

**These two locations are completely separate entities. Never mix them.**

| Signal | Public source (HERE) | Installed tool (runtime) |
|---|---|---|
| Role | Dev: edit code, run tests, commit, push | Runtime: capture, compile, search |
| Push | `git@github.com:Ekgardt/llm-wiki.git` | `no-push` (blocked) |
| User data | NONE (clean examples + fixtures only) | YOUR memory, daily logs, state |
| `$LLM_WIKI_ROOT` | Does NOT point here | Points here |

If unsure: `git remote get-url --push origin`. Real GitHub URL → public source.
`no-push` → installed instance.

### What you may do here
- Edit source code (`scripts/`, `tests/`, `install.sh`, etc.)
- Edit public docs (`README.md`, `docs/`, `CONTRIBUTING.md`)
- Run tests: `uv run pytest -q`
- Commit and push to `Ekgardt/llm-wiki` (public repo)

### What you must NEVER do here
- **NEVER create personal knowledge pages** in `knowledge/notes/` or
  `knowledge/daily/` — those dirs hold PUBLIC EXAMPLES only (enforced by
  `.gitignore` allowlist). Personal knowledge goes in `$LLM_WIKI_ROOT`.
- **NEVER write daily logs, session state, or project state here.** The
  running system writes those to `$LLM_WIKI_ROOT`, not here.
- **NEVER run `compile_memory.py`, `flush_memory.py`, or any memory pipeline
  script against this folder.** These scripts operate on `$LLM_WIKI_ROOT`.
- **NEVER commit user data** (decisions about your projects, debugging notes,
  personal design rules) to this repo. It is PUBLIC.
- **NEVER commit runtime dirs** (`cache/`, `logs/`, `run/`).
  They are gitignored; keep them out of the index.

### When asked to "work on the memory system"
- "Improve the system" → develop HERE (edit code, run tests, commit, push).
  The installed instance picks up updates via `git pull` at `$LLM_WIKI_ROOT`.
- "Show me my memory / what do I know about X" → that's the INSTALLED
  instance at `$LLM_WIKI_ROOT`, not here.

---

## 3. Knowledge conventions

### Global rules
1. Prefer answering from `knowledge/notes/` first.
2. Read `knowledge/raw/` or `knowledge/inbox/` only when the wiki is missing,
   stale, or contradictory.
3. When durable knowledge appears, update the wiki rather than leaving it
   only in chat.
4. Every important update should touch:
   - the most relevant wiki page(s)
   - `knowledge/index.md`
   - `knowledge/log.md`
5. Preserve provenance. When writing claims, include a `Source:` / Evidence
   line pointing to the relevant file(s).
6. Mark uncertainty explicitly.
7. Track contradictions and superseded claims instead of silently deleting
   history.
8. Use Obsidian-style wikilinks like `[[Concept Name]]` whenever a stable
   concept/entity/page exists.
9. Do not dump raw excerpts into the wiki unless the quote itself matters.
10. Prefer concise pages that link outward over giant pages that try to hold
    everything.

### Wiki page conventions
Every durable wiki page should try to include:
- Title (`# H1`)
- One-sentence summary (`One-sentence summary: ...`)
- Key facts / synthesis
- Open questions (if any)
- Source / Evidence
- Links to related pages

### Special files
@knowledge/index.md
@knowledge/log.md

### Default behavior for new material
When asked to compile or ingest new material:
1. Inspect `knowledge/inbox/` and/or the target source file.
2. Decide whether to create or update pages under `knowledge/notes/`.
3. Update `knowledge/index.md`.
4. Append a concise entry to `knowledge/log.md`.
5. Summarize what changed.

---

## 4. Extended rules (OKF + lifecycle)

11. **Every durable page MUST have YAML frontmatter with at least `type:`.**
    OKF v0.1 conformance. Use `scripts/migrate_to_okf.py --apply` to backfill
    missing frontmatter; `lint_memory.py` flags violations as
    `missing_frontmatter` / `missing_required_type`.

12. **When a new fact conflicts with an existing page, mark the old
    `status: superseded` and add a `superseded_by: [[<new-slug>]]` link —
    never delete.** History outranks tidiness. The old page stays in git, in
    the graph, and in the index, but retrieval excludes it. Decisions are
    immutable: supersede, never edit in place.

13. **Set `confidence` (high|medium|low) and `source_authority`
    (user|ai-derived|web|inferred) when a page makes a claim.** Hierarchy:
    user-stated > web-sourced > ai-derived > inferred. The compile/search
    pipeline uses these fields to rank retrieval results. Without them, pages
    default to medium / inferred and lose ranking.

14. **Track knowledge gaps: when a concept is mentioned but has no page, add
    a stub to `knowledge/notes/`** so the absence is visible, not lost.
    `lint_memory.py::orphan_gaps` flags gap pages with no inbound link from
    outside gaps/. Gaps close when a real page is created and backlinks the gap.

15. **Sessions start with self-awareness: read your knowledge state** (page
    counts, open gaps, last compile timestamp, active threads) before acting.
    The SessionStart hook injects a metacognitive block — read it. If compile
    backlog > 0 or stale pages exist, propose running `/lint` or
    `/knowledge-compile` before doing real work.

16. **Skills and rules are first-class knowledge: they live under the same
    frontmatter schema and are linted alongside wiki and memory.** A skill
    without `type: skill` frontmatter fails `missing_required_type` the same
    way a wiki page does.

---

## 5. Page-type quick reference (OKF)

| Type | Location | Notes |
|------|----------|-------|
| `concept` | `knowledge/notes/<slug>.md` | Mental models. Never archives. |
| `decision` | `knowledge/notes/<slug>.md` | Dated choice + rationale. Immutable; supersede. |
| `pattern` | `knowledge/notes/<slug>.md` | Recurring approach. 180-day archive. |
| `debugging` | `knowledge/notes/<slug>.md` | Symptom → cause → fix. 60-day archive. |
| `qa` | `knowledge/notes/<slug>.md` | Settled answer to a recurring question. 365-day. |
| `workflow` | `knowledge/notes/<slug>.md` | Auto-promoted playbook. 365-day. |
| `gap` | `knowledge/notes/<slug>.md` | Not-yet-written knowledge. 90-day. |
| `skill` | `skills/<name>/SKILL.md` | Agent workflow. Never archives. |
| `rule` | `rules/<name>.md` | File-handling policy. Never archives. |
| `project-state` | `knowledge/projects/<slug>/state.md` | Per-project handoff. Never archives. |

Pages live **flat** as `<slug>.md` under `knowledge/notes/` (the compile
pipeline writes flat slugs). Typed subdirectories are optional.

---

## 6. LLM backend

The memory pipeline needs an LLM for classification, compilation,
contradiction checks, and playbook crystallization. Backend is
**auto-detected** via `scripts/llm_client.py` — no API keys required.

Synchronous priority: OpenCode → Codex → Claude CLI → OpenAI → Ollama.
`llm_client.call_llm()` returns `None` when no selected backend succeeds; it
does not enqueue automatically. Only queue-capable callers, such as deferred
flush handling, explicitly persist work through `memory_queue.enqueue()`.
OpenCode SDK maintenance uses the queue bridge explicitly.

Override via `MEMORY_LLM_PROVIDER` env var. `fake` returns a canned response
for tests/e2e.

**OpenCode SDK-only contract:** classification, compile, and deferred queue
service prompts use exactly `openai/gpt-5.6-luna` through the active
authenticated SDK. Each operation owns an ephemeral `memory-*` session and
cleans it with `abort`, then `delete`, in `finally`. This path must not fall
back to a CLI or lower model. Cleanup errors are logged; compile provider
failures persist in compile state, and queue failures remain durable tasks.

**Capture and daily-context contract:** OpenCode attributes projects from
`worktree`, then `directory`, and continues to capture user prompts. Tool
breadcrumbs are limited to direct file mutation tools; read, search, and shell
activity creates none, while persisted targets and provenance are bounded and
redacted. Daily context normalizes timestamp blocks and legacy heading/bullet
summaries in memory, prioritizes project-matching durable summaries and user
prompts, and never injects tool breadcrumbs.

**Queue contract:** enqueue order is a durable monotonic sequence protected by
a cross-process lock. SDK work leases the oldest eligible task as `.processing`; apply
must match task ID, lease ID, and digest. A task is acknowledged only after its
result applies successfully. Failure increments attempts, records the error,
and returns the task to pending state with a 60-second retry delay; 5 attempts
require human attention. Leases older than 10 minutes are recoverable. Queue
maintenance uses bounded repeatable passes and continuations while eligible
work remains. Durable task provenance applies once; legacy provenance is
explicit rather than inferred.
Only confirmed flush tasks with complete provenance that remains unchanged by
durable sanitization carry provenance version 1. Confirmed incomplete payloads
remain unversioned. Unversioned transitional flush tasks may recover only
through their exact authenticated OpenCode source session; the SDK directory is
transient, and Python revalidates canonical project ownership before apply.
Versioned or persisted identity cannot be overridden.

**Compile contract:** meaningful timestamp blocks are bounded by
`MEMORY_COMPILE_PROMPT_CHAR_BUDGET` (default 120,000 characters). Generation
manifests pin source and batch layout. A validated plan is journaled before
mutation, and durable operation markers support crash reconciliation.
Admission is deterministic before either step: citations must come from
durable sections, lifecycle and word bounds must pass, and live-corpus plus
same-plan duplicates are rejected. Provider counters cannot bypass admission;
ambiguous pairs are report-only. A daily hash is published only after every
pinned batch, the Markdown index rebuild, and a self-validating effect receipt
succeed. A stale or missing receipt invalidates trust and triggers replay;
`compile_index_pending` is real unfinished work. Bounded journals and manifests
can reactivate from nondestructive retired stores and fail closed at truthful
quotas.
SDK completion updates canonical compile time and index health only in the
atomic state transition after its final Markdown index rebuild succeeds.

**Maintenance contract:** nightly and weekly return nonzero for failed or
remaining queue/compile work and required lint/index failures. Weekly holds one
maintenance lease across nightly work, mutations, and the final Markdown,
FTS5, and graph rebuild. Exhausted queue tasks remain durable for human review;
weekly reports only their count and canonical IDs and never clears them
automatically. Never infer completion from a detached launcher alone. Windows
Task Scheduler launches `pythonw`, and maintenance child processes are created
without console windows.

**Installed cleanup contract:** use `repair_installed_memory.py` in the order
`audit` → `apply --backup-only` → reviewed `apply --manifest` → `verify
--manifest`. Only backup-only may create a manifest; mutating apply requires the
explicit reviewed manifest under `run/backups/<timestamp>/` and never creates
one implicitly. Only verified empty daily records and generated false feedback
are mutable. Ordinary feedback is preserved; duplicate notes
and orphan service sessions are report-only. Never run cleanup apply or a live
restart without explicit operator approval.

**Project-state contract:** slug claims are serialized by the runtime
`run/project-state-claim.lock` and publish complete `state.md` files atomically.
New states record native-absolute `Project root JSON` ownership and persisted
`Runtime slug JSON`. The confirmed-identity helper completes the direct-child
inventory under that lock and fails closed on iteration or state-read errors;
only then may it reuse an exact contained state path or allocate a new claim.
All deterministic hash candidates are verified before bounded UUID fallback.
Daily records are project-scoped by both confirmed alias and absolute root.
Bootstrap output records and revalidates that alias, root, and exact state path;
orphan output is never injected. Published bootstrap data is bounded, labeled
untrusted, and placed after project identity and the saved handoff without
entering the search index.

**Claude hook compatibility contract:** installers detect the Claude Code
version. Versions before 2.1.139, or unknown versions, receive absolute
shell-command hooks with Bash or PowerShell literal quoting; 2.1.139 and newer
receive direct `command` + `args` hooks. SessionStart covers forked sessions.

**Zero-cost path:** no paid API beyond existing agent subscriptions. Cognee
(optional, 300+ pages) is the only feature that requires Ollama.

---

## 7. Quick command reference

```bash
uv run pytest -q                              # 1916 collected; local Windows: 1881 passed, 35 skipped; skips vary by environment
uv run ruff check scripts/ tests/             # Python static analysis
uv run python scripts/lint_memory.py --scope all   # structural lint
uv run python scripts/search_memory.py "query"     # hybrid search
uv run python scripts/compile_memory.py            # compile daily logs → notes
uv run python scripts/lookup_mode.py               # show retrieval tier
```

Runtime state under `cache/`, `logs/`, and `run/` is gitignored and must never
be committed. `cache/` is regenerable and `logs/` may be rotated, but `run/`
contains durable queue, compile recovery, and repair transaction state. Never
delete `run/` wholesale; use queue `status`/`list`/`drain`, compile retry, and
repair verify tooling for the owning subsystem.
