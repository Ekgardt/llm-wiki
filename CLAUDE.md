# LLM Wiki Project Contract

You are maintaining a persistent markdown knowledge base in the style of an LLM wiki.

## Three-zone layout (canonical)

```
# CODE (this repo)
scripts/   tests/   docs/   skills/   rules/   integrations/   benchmark/

# KNOWLEDGE (this repo — public examples only in source; full data in installed vault)
knowledge/
  daily/      # append-only session capture
  notes/      # durable compiled pages (OKF frontmatter)
  projects/   # per-project state.md / context
  raw/        # immutable sources
  inbox/      # unprocessed staging
  feedback/   # correction candidates

# RUNTIME (OUTSIDE vault — never commit)
$LLM_WIKI_STATE_ROOT/          # default: <vault-parent>/LLM-wiki-state/
  run/     # state.json, compile.pid, queue/
  logs/    # lint/nightly reports
  cache/   # search / QMD indexes
```

Public **source** (`D:\projects\llm-wiki`) develops code.  
Installed **runtime** vault is separate (`$LLM_WIKI_ROOT`). Do not mix them.

## Project structure (paths)
- `knowledge/raw/` — source-of-truth inputs; treat as immutable.
- `knowledge/inbox/` — newly captured material not yet compiled.
- `knowledge/notes/` — compiled durable knowledge layer.
- `knowledge/daily/` — episodic session logs (append-only).
- `knowledge/projects/<slug>/` — per-project handoff (`state.md`).
- `knowledge/feedback/` — unpromoted correction candidates.
- Runtime indexes/state — **only** under `$LLM_WIKI_STATE_ROOT` (not in the vault root).

## Global rules
1. Prefer answering from `knowledge/notes/` first.
2. Read `knowledge/raw/` or `knowledge/inbox/` only when the wiki is missing, stale, or contradictory.
3. When durable knowledge appears, update the wiki rather than leaving it only in chat.
4. Every important update should touch:
   - the most relevant wiki page(s)
   - `knowledge/index.md`
   - `knowledge/log.md`
5. Preserve provenance. When writing claims, include a `Source:` / Evidence line pointing to the relevant file(s).
6. Mark uncertainty explicitly.
7. Track contradictions and superseded claims instead of silently deleting history.
8. Use Obsidian-style wikilinks like `[[Concept Name]]` whenever a stable concept/entity/page exists.
9. Do not dump raw excerpts into the wiki unless the quote itself matters.
10. Prefer concise pages that link outward over giant pages that try to hold everything.

## Wiki page conventions
Every durable wiki page should try to include:
- Title
- One-sentence summary
- Key facts / synthesis
- Open questions (if any)
- Source
- Links to related pages

## Special files
@knowledge/index.md
@knowledge/log.md

## Default behavior for new material
When asked to compile or ingest new material:
1. inspect `knowledge/inbox/` and/or the target source file
2. decide whether to create or update pages under `knowledge/notes/`
3. update `knowledge/index.md`
4. append a concise entry to `knowledge/log.md`
5. summarize what changed

## Extended rules (OKF + lifecycle — Phase 2+)

11. **Every durable page MUST have YAML frontmatter with at least `type:`.** OKF v0.1 conformance. Use `scripts/migrate_to_okf.py --apply` to backfill missing frontmatter; `lint_memory.py` flags violations as `missing_frontmatter` / `missing_required_type`.

12. **When a new fact conflicts with an existing page, mark the old `status: superseded` and add a `superseded_by: [[<new-slug>]]` link — never delete.** History outranks tidiness. The old page stays in git, in the graph, and in the index, but retrieval excludes it. Decisions are immutable: supersede, never edit in place. `lint_memory.py::invalid_supersede_chain` verifies that `superseded_by` targets resolve.

13. **Set `confidence` (high|medium|low) and `source_authority` (user|ai-derived|web|inferred) when a page makes a claim.** Hierarchy: user-stated > web-sourced > ai-derived > inferred. The compile/search pipeline uses these fields to rank retrieval results. Without them, pages default to medium / inferred and lose ranking.

14. **Track knowledge gaps: when a concept is mentioned but has no page, add a stub to `knowledge/notes/`** so the absence is visible, not lost. `lint_memory.py::orphan_gaps` flags gap pages with no inbound link from outside gaps/. Gaps close when a real page is created and backlinks the gap.

15. **Sessions start with self-awareness: read your knowledge state** (page counts, open gaps, last compile timestamp, active threads) before acting. The SessionStart hook injects a metacognitive block — read it. If compile backlog > 0 or stale pages exist, propose running `/lint` or `/knowledge-compile` before doing real work.

16. **Skills and rules are first-class knowledge: they live under the same frontmatter schema and are linted alongside wiki and memory.** No special casing. A skill without `type: skill` frontmatter fails `missing_required_type` the same way a wiki page does.

## Page-type quick reference (OKF)
- **knowledge/notes/** — durable knowledge (concepts, decisions, patterns, debugging, qa, workflows, facts, gaps)
- **knowledge/daily/** — append-only raw session capture (no frontmatter; exempt from OKF)
- **skills/** — agent workflows (type: skill)
- **rules/** — file-handling policies (type: rule)
- **knowledge/projects/<slug>/state.md** — per-project handoff page (type: project-state)

## LLM backend (Phase 5+ — multi-tool support)

The memory pipeline needs an LLM for classification, compilation, contradiction checks, and playbook crystallization. Backend is **auto-detected**, no API keys required on this machine:

- **OpenCode plugin** (`~/.config/opencode/plugins/llm-wiki-memory.js`) — uses OpenCode's own SDK for LLM work. Fires on session.created / tool.execute.after / session.idle / experimental.session.compacting.
- **Codex wrapper** (`scripts/codex-memory-wrapper.ps1`) — wraps `codex` and triggers memory capture on exit.
- **Python scripts** — use `scripts/llm_client.py` which auto-picks backends. Override via `MEMORY_LLM_PROVIDER` (including `fake` for tests).

**Zero-cost path**: $0/month beyond existing agent subscriptions. Cognee (optional) is the only feature that requires Ollama.
