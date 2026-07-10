# User Guide — LLM-Wiki Memory System

How to actually work with this system in **your** tools. After one-time
setup, the system maintains itself.

For the canonical structure reference (paths, env vars, zones), see
[STRUCTURE.md](STRUCTURE.md). For the design rationale, see
[ARCHITECTURE.md](ARCHITECTURE.md). For the agent operating contract, see
`AGENTS.md` (byte-identical to `CLAUDE.md`).

---

## The mental model in one paragraph

This system **watches what you do** in your AI coding agent (OpenCode, Codex,
Claude Code, Cursor, or Antigravity), **decides what's worth remembering**
using an LLM, and **saves it as markdown pages** in your vault. Next time
you open a project, those pages are loaded back into the agent's context so
it picks up where you stopped — no re-explaining, no lost decisions, no
repeated mistakes. **All of this happens automatically** — capture on every
action, classification on session end, compile in background, nightly deep
maintenance via scheduler.

**The LLM part**: the system needs a "brain" to read transcripts and decide
what to keep. That brain is **whichever agent you're already using** — the
`llm_client.py` abstraction auto-detects the first alive backend
(OpenCode → Codex → Claude CLI → OpenAI → Ollama). **No extra API keys
required** beyond what you already have; Ollama is optional (only needed for
the Cognee graph layer at 300+ pages).

---

## One-time setup

### Option A: One-command installer (recommended)

```bash
# macOS / Linux / WSL2
curl -fsSL https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.sh | bash

# Windows
irm https://raw.githubusercontent.com/Ekgardt/llm-wiki/main/install.ps1 | iex
```

The installer detects your agents and wires them up automatically.

### Option B: Manual setup

1. **Clone + install dependencies:**
   ```bash
   git clone https://github.com/Ekgardt/llm-wiki.git
   cd llm-wiki
   uv sync
   uv run pytest -q          # verify: 226 tests collected in 0.26s tests should pass
   ```

2. **Set environment variables** (add to your shell profile):
   ```bash
   export LLM_WIKI_ROOT="$(pwd)"
   export LLM_WIKI_STATE_ROOT="$LLM_WIKI_ROOT"   # runtime inside vault
   ```
   ```powershell
   [Environment]::SetEnvironmentVariable("LLM_WIKI_ROOT", "$(Get-Location)", "User")
   [Environment]::SetEnvironmentVariable("LLM_WIKI_STATE_ROOT", "$(Get-Location)", "User")
   ```

3. **Create runtime dirs** (gitignored, regenerated on demand):
   ```bash
   mkdir -p cache logs run/queue cognee
   ```

4. **Wire up your agents** (see below).

### Wire up your agents

| Agent | How to wire |
|-------|-------------|
| **Claude Code** | `uv run python scripts/merge_claude_settings.py` — merges hooks into `~/.claude/settings.json` (5 hooks: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse). Backup written automatically. |
| **OpenCode** | Copy `scripts/llm-wiki-memory-opencode.js` to `~/.config/opencode/plugins/llm-wiki-memory.js`. Plugin autoloads. |
| **Codex CLI** | Windows: add `. "$env:LLM_WIKI_ROOT\scripts\codex-memory-wrapper.ps1"` to `$PROFILE`. Unix: alias `codex-mem` to `uv run python scripts/codex_memory.py daily-log`. |
| **Cursor** | Copy `integrations/cursor/rules/llm-wiki.mdc` to `.cursor/rules/`. |
| **Antigravity** | Copy `integrations/antigravity/AGENTS.md` to your project root. |
| **Obsidian** | Import `integrations/obsidian/Article-to-Inbox.json` as a Web Clipper template. |

### Register scheduled maintenance

**Windows (Task Scheduler):**
```powershell
powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1
```
Creates `LLMWiki-Nightly` (daily 03:00) and `LLMWiki-Weekly` (Sunday 04:00).

**Unix (cron):** add to `crontab -e`:
```
0 3 * * *   cd $LLM_WIKI_ROOT && uv run python scripts/scheduled_nightly.py
0 4 * * 0   cd $LLM_WIKI_ROOT && uv run python scripts/scheduled_weekly.py
```

---

## What happens automatically — and when

```
REAL-TIME (while you work)
  Every Edit/Write/Bash → breadcrumb appended to today's daily log
  SessionStart → load project handoff + drain queue + background compile

END OF SESSION (agent idle or you close)
  LLM classifies transcript → FLUSH_MAJOR / FLUSH_MINOR / FLUSH_OK
  MAJOR/MINOR content → structured summary appended to daily log
  MAJOR triggers background compile (detached, doesn't block you)

NIGHTLY 03:00 (scheduler, even while you sleep)
  Drain deferred queue → compile all pending → structural lint → prune old reports

SUNDAY 04:00 (scheduler)
  Everything nightly does + OKF conformance sweep + archive stale + prune failed queue tasks
```

Nothing here requires your attention. If the LLM is offline, work is queued
in `run/queue/` and drained at the next session.

---

## Working with the system day-to-day

### Asking questions about your knowledge

```bash
uv run python scripts/search_memory.py "how do we handle auth?"
uv run python scripts/search_memory.py "database performance" --semantic
uv run python scripts/search_memory.py --project my-app "decisions"
uv run python scripts/query_memory.py "why did we choose Postgres?" --file-back
```

`search_memory.py` runs hybrid BM25 + Vector + Graph fusion.
`query_memory.py` asks the LLM to answer from the knowledge index and
optionally files the answer as a Q&A page.

### Compiling knowledge manually

```bash
uv run python scripts/compile_memory.py              # compile changed daily logs
uv run python scripts/compile_memory.py --all        # recompile everything
uv run python scripts/compile_memory.py --dry-run    # plan only, no writes
```

Compile runs automatically on MAJOR sessions after the hour cutoff, but you
can trigger it manually anytime. The pipeline uses VERIFY-BEFORE-WRITE —
the LLM cannot fabricate citations.

### Linting and maintenance

```bash
uv run python scripts/lint_memory.py --scope all           # 13 structural checks
uv run python scripts/lint_memory.py --contradictions      # + LLM-judged contradictions
uv run python scripts/archive_stale.py --apply           # archive old pages by type
uv run python scripts/lookup_mode.py                       # show retrieval tier + QMD status
```

### Skills (agent-side workflows)

The 9 skills in `skills/` are invokable from your agent:

- `/knowledge-compile` — run the compile pass
- `/knowledge-lookup` — retrieval strategy advisor
- `/knowledge-review` — audit existing pages
- `/knowledge-qa-file-back` — file a Q&A page from a just-answered question
- `/contradict-check` — LLM-judged contradiction scan
- `/crystallize-playbook` — extract a reusable workflow
- `/bridge-promote-insight` — promote an insight across categories
- `/session-memory-compile` — compile wrapper (alias)
- `/session-memory-review` — review wrapper (alias)

---

## Optional: semantic search (BM25 + Vector)

For hybrid search that finds semantically related pages even when keywords
don't match:

```bash
uv sync --extra semantic
```

This installs `sentence-transformers` with a MiniLM model. Embeddings are
cached in `cache/vectors.json` (gitignored) and rebuilt automatically when
pages change.

## Optional: Cognee graph (300+ pages)

For entity extraction + relationship graph at scale:

```bash
uv sync --extra cognee
```

Requires Ollama running locally. See [SETUP-COGNEE.md](SETUP-COGNEE.md) for
setup steps.

---

## Troubleshooting

### "Nothing happens after install"
- Verify env vars: `echo $LLM_WIKI_ROOT` / `echo $LLM_WIKI_STATE_ROOT`
- Check runtime dirs exist: `cache/`, `logs/`, `run/`, `run/queue/`
- Run `uv run python scripts/lookup_mode.py` — it shows vault state

### "Compile never runs"
- Compile triggers only on FLUSH_MAJOR sessions after
  `MEMORY_COMPILE_AFTER_HOUR` (default 18:00). Override or run manually:
  `uv run python scripts/compile_memory.py`
- Check `run/state.json` for `compiled_daily_hashes` and `last_compile_status`

### "Search returns nothing"
- Rebuild the index: `uv run python scripts/search_memory.py --rebuild`
- Check `cache/index.sqlite` exists and is non-empty

### "Hook errors"
- Check `logs/hook-errors.log` for captured exceptions
- All hooks exit 0 on any error (never break your session), so errors are
  silent unless you check the log

### "Tests fail on fresh clone"
- `uv sync` first (deps must be installed)
- `uv run pytest -q` — should report 226 tests collected in 0.26s passed
- If `< 218`, your checkout is stale; `git pull`

---

## Where things live

| Path | Zone | Purpose |
|------|------|---------|
| `scripts/` | CODE | Pipeline + hooks + helpers (43 .py + 3 helpers) |
| `tests/` | CODE | 23 test files, 226 tests collected in 0.26s tests |
| `docs/` | CODE | This file + ARCHITECTURE + STRUCTURE + SETUP-COGNEE + EXPORTING |
| `skills/` | CODE | 9 agent skills |
| `rules/` | CODE | 3 file-handling policies |
| `integrations/` | CODE | claude-code, cursor, antigravity, obsidian |
| `benchmark/` | CODE | Benchmark suite + report |
| `knowledge/daily/` | KNOWLEDGE | Append-only session logs (private) |
| `knowledge/notes/` | KNOWLEDGE | Durable OKF pages |
| `knowledge/projects/<slug>/` | KNOWLEDGE | Per-project state.md |
| `knowledge/raw/` | KNOWLEDGE | Immutable sources |
| `knowledge/inbox/` | KNOWLEDGE | Unprocessed staging |
| `knowledge/feedback/` | KNOWLEDGE | Correction candidates |
| `cache/` | RUNTIME | Search / QMD / vector indexes (gitignored) |
| `logs/` | RUNTIME | Lint reports, compile logs (gitignored) |
| `run/` | RUNTIME | state.json, compile.pid, queue/ (gitignored) |
| `cache/cognee/` | RUNTIME | Optional semantic graph (gitignored) |

For the full canonical reference, see [STRUCTURE.md](STRUCTURE.md).
