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

# RUNTIME (inside the vault, gitignored — regenerated on demand)
# Override root via LLM_WIKI_STATE_ROOT (tests use a temp dir).
cache/     # search / QMD / vector indexes (+ cache/cognee/ for optional graph)
logs/      # lint reports, compile logs, SessionStart debug dumps
run/       # state.json, compile.pid, queue/, locks
```

**Env contracts:**
- `$LLM_WIKI_ROOT` → vault root (the repository root). Default: resolved from
  `scripts/` location, worktree-aware.
- `$LLM_WIKI_STATE_ROOT` → runtime root. **Default: the vault itself** →
  `cache/` (incl. `cache/cognee/`), `logs/`, `run/` at vault root, all gitignored.
  Override for multi-disk setups or hermetic tests.
- `$MEMORY_LLM_PROVIDER` → `fake` (tests), or one of
  `opencode|codex|claude|openai|ollama` (runtime, auto-detected).

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

Priority: OpenCode → Codex → Claude CLI → OpenAI → Ollama. If none available,
the call is enqueued to a persistent queue (`run/queue/`) and processed at
the next active session.

Override via `MEMORY_LLM_PROVIDER` env var. `fake` returns a canned response
for tests/e2e.

**Zero-cost path:** no paid API beyond existing agent subscriptions. Cognee
(optional, 300+ pages) is the only feature that requires Ollama.

---

## 7. Quick command reference

```bash
uv run pytest -q                              # run the test suite (218 tests)
uv run ruff check scripts/ tests/             # Python static analysis
uv run python scripts/lint_memory.py --scope all   # structural lint
uv run python scripts/search_memory.py "query"     # hybrid search
uv run python scripts/compile_memory.py            # compile daily logs → notes
uv run python scripts/lookup_mode.py               # show retrieval tier
```

Runtime state (under `cache/`, `logs/`, `run/`) is gitignored and
regenerated on demand — never commit it.
