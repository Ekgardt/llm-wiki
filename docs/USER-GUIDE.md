# User Guide — LLM-Wiki Memory System

How to actually work with this system in **your** tools. After one-time
setup, scheduled maintenance runs unattended and reports failures truthfully.

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
repeated mistakes. Capture and scheduled maintenance are automatic, but failed
or pending compile/queue work remains visible in runtime state and produces a
nonzero scheduled-maintenance result rather than being reported as complete.

**The LLM part**: generic CLI work uses the first available `llm_client.py`
backend (OpenCode → Codex → Claude CLI → OpenAI → Ollama). In OpenCode
SDK-only mode, classification, compile, and deferred queue prompts instead use
the active authenticated SDK with the exact model `openai/gpt-5.6-luna`.
There is no CLI or lower-model fallback for that service path. If no generic
backend succeeds, `llm_client.call_llm()` returns `None`; only callers designed
for deferral explicitly enqueue a task. SDK maintenance uses that queue bridge
explicitly rather than relying on an automatic client fallback.

OpenCode attributes each project from `worktree`, then `directory`. User prompts
remain captured. Tool breadcrumbs are limited to direct file mutation tools;
read, search, and shell activity creates none, while persisted targets and
provenance are bounded and redacted.

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
   uv run pytest -q          # 2793 collected; local Windows: 2749 passed, 44 skipped; skips vary by environment
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

3. **Create runtime dirs** (gitignored; `cache/` is derived, while `run/`
   will hold durable automation/recovery state):
   ```bash
   mkdir -p cache logs run/queue cache/cognee
   ```

4. **Wire up your agents** (see below).

For OpenCode, `XDG_CONFIG_HOME` owns the effective config location only when
it is absolute. Unix uses that one effective destination. Windows normalizes
and deduplicates the effective destination plus `~/.config/opencode`; this
keeps compatibility with Windows OpenCode installations that still load there.

### Wire up your agents

| Agent | How to wire |
|-------|-------------|
| **Claude Code** | `uv run python scripts/merge_claude_settings.py` — merges hooks into `~/.claude/settings.json` (5 hooks: SessionStart, PreCompact, SessionEnd, UserPromptSubmit, PostToolUse). Backup written automatically. |
| **OpenCode** | Copy `scripts/llm-wiki-memory-opencode.js` to the effective `$XDG_CONFIG_HOME/opencode/plugins/llm-wiki-memory.js`. Unset, empty, or relative XDG falls back to `~/.config`; on Windows the installer also writes that compatibility target when it differs. |
| **Codex CLI** | POSIX: `uv run python scripts/merge_codex_hooks.py --vault-root "$LLM_WIKI_ROOT"`.<br>PowerShell: `uv run python scripts/merge_codex_hooks.py --vault-root "$env:LLM_WIKI_ROOT"`.<br>Then review/trust the merged `~/.codex/hooks.json` via `/hooks`. The Windows installer may retain `codex-memory-wrapper.ps1` as compatibility/exit-capture fallback, not as the primary integration. |
| **Cursor** | Copy `integrations/cursor/rules/llm-wiki.mdc` to `.cursor/rules/`. |
| **Antigravity** | Copy `integrations/antigravity/AGENTS.md` to your project root. |
| **Obsidian** | Import `integrations/obsidian/Article-to-Inbox.json` as a Web Clipper template. |

### Register scheduled maintenance

**Windows (Task Scheduler):**
```powershell
powershell -ExecutionPolicy Bypass -File scripts/install-scheduled-tasks.ps1
```
Creates `LLMWiki-Nightly` (daily 03:00) and `LLMWiki-Weekly` (Sunday 04:00).
Both tasks launch `pythonw`; their maintenance child processes are also created
without console windows.

**Unix (cron):** add to `crontab -e`:
```
0 3 * * *   cd $LLM_WIKI_ROOT && uv run python scripts/scheduled_nightly.py
0 4 * * 0   cd $LLM_WIKI_ROOT && uv run python scripts/scheduled_weekly.py
```

---

## What happens automatically — and when

```
REAL-TIME (while you work)
  User prompts → bounded, redacted prompt records
  Mutation-only direct file tools (Edit/Write/MultiEdit/NotebookEdit/apply_patch)
    → bounded, redacted breadcrumb appended to today's daily log
  Shell, read, and search activity is not captured
  SessionStart → load project handoff + published bootstrap + bounded queue/compile maintenance

END OF SESSION (agent idle or you close)
  LLM classifies transcript → FLUSH_MAJOR / FLUSH_MINOR / FLUSH_OK
  MAJOR/MINOR content → structured summary appended to daily log
  MAJOR triggers background compile (detached, doesn't block you)

NIGHTLY 03:00 (scheduler, even while you sleep)
  Drain queue when the configured backend is usable → trigger/wait for compile
  → structural lint → rebuild FTS/graph → prune old reports

SUNDAY 04:00 (scheduler)
  Hold one maintenance lease across nightly work + OKF sweep + archive + failed-queue report
  → optional contradiction check → final Markdown/FTS/graph rebuild
```

First discovery schedules project bootstrap scanning in a detached worker, so
that first hook may omit it. Once `bootstrap.md` is atomically published, the
next SessionStart exposes a bounded excerpt labeled as untrusted project-derived
data. Bootstrap remains SessionStart-only and is not added to the search index.

Daily context normalizes timestamp blocks and legacy heading/bullet summaries
into one in-memory record model. It prioritizes a project-matching durable
summary, then a matching user prompt, with a legacy durable summary as fallback;
tool breadcrumbs are never injected.

If OpenCode SDK-only work has no active authenticated session, it remains
pending for the next OpenCode session. Nightly and weekly return nonzero when
queue work remains, compile fails or remains pending, or a required lint/index
step fails. Inspect their report under `logs/` instead of assuming completion.

---

## Working with the system day-to-day

### Asking questions about your knowledge

```bash
uv run python scripts/search_memory.py "how do we handle auth?"
uv run python scripts/search_memory.py "database performance" --semantic
uv run python scripts/search_memory.py --project my-app "decisions"
uv run python scripts/search_memory.py "past decision" --as-of 2026-08-03
uv run python scripts/search_memory.py "recent change" --since 2026-08-01
uv run python scripts/query_memory.py "why did we choose Postgres?" --file-back
```

`search_memory.py` runs hybrid BM25 + Vector + Graph fusion. `--as-of` and
`--since` require exact, real `YYYY-MM-DD` dates. `--as-of` checks page
timestamps and `valid_to`. Relevance always ranks first; `source_authority`,
confidence, and stable path order only break exact score ties.
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

Compilation groups meaningful timestamp blocks into requests bounded by
`MEMORY_COMPILE_PROMPT_CHAR_BUDGET` (default 120,000 characters). One block
that cannot fit fails visibly. A generation manifest pins the source prefix,
batch IDs, and layout so retries do not silently change boundaries after an
append or budget change. Each fully validated provider plan is written to a
digest-protected journal before note mutation; per-operation markers let a
retry reconcile a crash without duplicating an already durable write.

Before any plan is journaled or mutates a note, deterministic admission requires
citations from durable sections, valid lifecycle and body-word bounds, and no
duplicate against either the live corpus or an earlier create in the same plan.
Provider audit counters cannot bypass these checks. Ambiguous semantic pairs are
reported only; they are not merged or deleted.

The final pinned batch sets `compile_index_pending`. Only a successful Markdown
index rebuild plus a self-validating effect receipt publishes the compiled daily
hash and clears that state. A stale or missing receipt invalidates trust and
replays recovery rather than claiming completion. Failed index work is resumed
from the journal without asking the model for a replacement plan. Bounded
journals and manifests can reactivate from nondestructive retired stores; active
and retired stores fail closed when truthful count or byte quotas are reached.
One OpenCode pass handles at most 10 compile batches, then schedules a bounded
continuation.

If a hot retired store reaches its truthful quota, do not remove records by
hand. Use the compiler-owned cold archive:

```bash
uv run python scripts/compile_memory.py --archive-retired audit
uv run python scripts/compile_memory.py --archive-retired backup-only
# Review the emitted manifest and change only top-level approved to true.
uv run python scripts/compile_memory.py --archive-retired apply --manifest PATH
uv run python scripts/compile_memory.py --archive-retired verify --manifest PATH
```

Only whole manifest/journal components outside state, queue, and active-artifact
recovery closure are eligible. The transaction keeps a reviewed byte-exact
`payload/` copy and moves the exact original hot files into `source-retired/`.
Cold evidence is preserved but is not searched by automatic compile replay.

### Deferred queue behavior

Tasks receive a durable monotonic `enqueue_sequence` under a cross-process
ordering lock. Legacy files are assigned deterministic FIFO positions through
a resumable migration journal. SDK preparation leases one eligible task by
moving it from `.json` to `.processing` and returns a lease ID plus a digest.
The result is accepted only when task ID, lease ID, and digest still match.

Success acknowledges and removes the task only after its result has been
applied. Provider or apply failure records `last_error`, increments `attempts`,
and returns the task to pending state. Retries wait at least 60 seconds and stop
after 5 attempts for human attention. A `.processing` lease older than 10
minutes is recovered. OpenCode drains at most 5 queue tasks per maintenance
pass. Maintenance passes are repeatable and schedule bounded continuations while
eligible work remains. Durable task provenance is applied exactly once; legacy
tasks without it retain an explicit fallback instead of inferred provenance.

### Linting and maintenance

```bash
uv run python scripts/lint_memory.py --scope all           # 13 structural checks
uv run python scripts/lint_memory.py --contradictions      # + LLM-judged contradictions
uv run python scripts/archive_stale.py --apply           # archive old pages by type
uv run python scripts/lookup_mode.py                       # show retrieval tier + QMD status
uv run python scripts/memory_queue.py status               # pending/failed queue counts
uv run python scripts/maybe_compile.py --status            # lock and pending-work status
```

### Installed-memory cleanup

Run this only against the installed vault, never the public source checkout.
Use four explicit stages and review every JSON artifact before continuing:

```bash
REPAIR_REVIEW_DIR="/absolute/path/outside-the-vault-and-state-root"
mkdir -p "$REPAIR_REVIEW_DIR"
uv run python scripts/repair_installed_memory.py audit --root "$LLM_WIKI_ROOT" --state-root "$LLM_WIKI_STATE_ROOT" --output "$REPAIR_REVIEW_DIR/repair-audit.json"
uv run python scripts/repair_installed_memory.py apply --root "$LLM_WIKI_ROOT" --state-root "$LLM_WIKI_STATE_ROOT" --audit-report "$REPAIR_REVIEW_DIR/repair-audit.json" --backup-only --output "$REPAIR_REVIEW_DIR/repair-backup.json"
# Read repair-backup.json and use its backup_manifest value below.
# Inspect every manifest entry, then change only top-level approved: false to true.
uv run python scripts/repair_installed_memory.py apply --root "$LLM_WIKI_ROOT" --state-root "$LLM_WIKI_STATE_ROOT" --audit-report "$REPAIR_REVIEW_DIR/repair-audit.json" --manifest "/absolute/path/to/manifest.json" --output "$REPAIR_REVIEW_DIR/repair-apply.json"
uv run python scripts/repair_installed_memory.py verify --root "$LLM_WIKI_ROOT" --state-root "$LLM_WIKI_STATE_ROOT" --manifest "/absolute/path/to/manifest.json" --output "$REPAIR_REVIEW_DIR/repair-verify.json"
```

Pass each explicit stale active note as a repeatable root-relative
`--stale-page knowledge/notes/<slug>.md` argument to `audit` and both `apply`
commands. The list must match exactly at every stage.

Every `--output` parent must already exist outside both the vault and state
root. Output preflight rejects links, hard links, and aliases of input artifacts
before inventory, recovery, staging, or mutation. `audit` does not mutate the
vault. `--backup-only` first computes the complete manifest and fsyncs a
schema-v4 `preparing` ownership journal containing its approval-normalized
SHA-256 and every expected staging descriptor, then writes source bytes and
validates private `source-staging/` plus exactly
`run/backups/<timestamp>/manifest.json` with a sealed `approved: false` value,
without changing a source. Approved apply reuses that journal and accepts only
schema v4; schema v3 is recovery/verification-only. Mutating apply never creates
a manifest. It accepts only the reviewed manifest with that one field changed
to `true`; apply, recovery, and verify reject any other manifest rewrite even
when it is resealed and the source bytes are unchanged. Apply then revalidates
the audit digest, stale-page list, strict manifest structure, source hashes and
identities, and staged bytes under the repair, publication, daily, feedback,
and project-state locks.

Apply may physically delete only byte-exact noncanonical note shadows,
explicitly named active stale notes, canonical unpromoted `opencode-idle`
feedback, and whole daily files proven to contain only generated or empty
records, including exactly framed writer completion markers. It may
replace one exact visible handoff placeholder in a canonically owned project
state with `(saved project handoff unavailable)`. Nonidentical shadows,
ordinary feedback, mixed or unrecognized daily content, queue/compile state,
and service-session inventories remain preserved, report-only, or out of scope.

Temporary source bytes support preparation, rollback, and interrupted-
transaction recovery. Preparation recovery removes only exact journal-owned
staging and stops on unknown paths. Commit and rollback record each purge step
before deletion, so recovery accepts only the corresponding missing artifact.
Every journal status also requires its exact legal progress: action/result IDs
are manifest-path prefixes, restore IDs are reverse-attempted subsequences,
purge IDs are staging-path prefixes, and terminal states require complete
commit or rollback/purge sets. Contradictory or duplicated progress fails closed.
Staging is purged before a successful v4 apply returns. The retained manifest
and transaction journal contain hashes and results only, so completed v4 cleanup
has no source backup or post-completion rollback. `verify` may first finish
interrupted recovery under the same locks; once the transaction is terminal,
repeated verify is read-only. Legacy schema-v3 recovery and verification retain
their original artifacts; v3 can no longer enter mutating apply.

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
cached in `cache/vectors.json` (gitignored). The bounded no-follow cache is
accepted only when its exact schema, generation, source hashes, model version,
dimensions, and vectors validate; otherwise it is rebuilt once.

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

Do not fix runtime problems by deleting `run/`. `cache/` can be rebuilt and
diagnostic `logs/` can be rotated, but `run/` contains queue tasks, compile
journals/manifests and checkpoints, plus repair manifests/transactions. Use
`memory_queue.py status|list`, compile retry/status commands, and
`repair_installed_memory.py audit|apply|verify` for the owning state.

### "Compile never runs"
- Compile triggers only on FLUSH_MAJOR sessions after
  `MEMORY_COMPILE_AFTER_HOUR` (default 18:00). Override or run manually:
  `uv run python scripts/compile_memory.py`
- Check `run/state.json` for `compiled_daily_hashes` and `last_compile_status`
- Run `uv run python scripts/maybe_compile.py --status`. In `opencode-sdk`
  mode, pending work requires an active authenticated OpenCode session.
- If `compile_index_pending` exists, the daily is not complete; inspect
  `last_compile_error`, `last_compile_sdk_error`, and `run/compile-journal/`.
- If the error reports a full retired store, use the four-phase
  `--archive-retired` workflow above; never delete retired files manually.

### "Queue never drains"
- Run `uv run python scripts/memory_queue.py status` and
  `uv run python scripts/memory_queue.py list`.
- Tasks retry after 60 seconds and stop automatically after 5 failed attempts.
- Tasks at the 5-attempt ceiling remain byte-preserved for human review. Weekly
  maintenance reports their count and canonical IDs, returns nonzero, and never
  clears them automatically. Review `status` and `list`; run `clear-failed` only
  as an explicit operator decision after preserving any needed evidence.
- A current `.processing` file is leased work, not a pending `.json` task;
  stale leases are recovered after 10 minutes.
- Check OpenCode logs for provider/cleanup errors. SDK-only work does not fall
  back to a CLI or a different model.

### "Scheduled maintenance says it failed"
- Read `logs/nightly-YYYY-MM-DD.md` or `logs/weekly-YYYY-MM-DD.md`.
- A nonzero exit is expected while queue or compile work remains pending, when
  exhausted queue tasks require human review, or when lint/index rebuilding
  fails. Weekly reports task IDs but not payloads or prompts. Do not treat
  launcher success as compile completion.

### "OpenCode plugin is not loading"
- Resolve the effective config root: absolute `XDG_CONFIG_HOME`, otherwise
  `~/.config`.
- Verify `opencode/plugins/llm-wiki-memory.js` exists under that root. On
  Windows also check `~/.config/opencode/plugins/` when it is a distinct path.
- Restart OpenCode after updating the installed plugin; editing this source
  checkout does not update a running installation.

### "Search returns nothing"
- Rebuild the index: `uv run python scripts/search_memory.py --rebuild`
- Check `cache/index.sqlite` exists and is non-empty

### "Hook errors"
- Check `logs/hook-errors.log` for captured exceptions
- All hooks exit 0 on any error (never break your session), so errors are
  silent unless you check the log

### "Tests fail on fresh clone"
- `uv sync` first (deps must be installed)
- `uv run pytest -q` — collects 2793 on every platform; the local Windows
  verification is 2749 passed, 44 skipped. Skip count varies with optional Bash,
  PowerShell, and symlink availability.
- If fewer than 2793 tests are collected, your checkout is stale; `git pull`

---

## Where things live

| Path | Zone | Purpose |
|------|------|---------|
| `scripts/` | CODE | Pipeline, hooks, maintenance, repair, and integration helpers |
| `tests/` | CODE | Regression suite: 2793 collected platform-wide; local Windows 2749 passed, 44 skipped, with skips varying by optional shell/symlink support |
| `docs/` | CODE | This file + ARCHITECTURE + STRUCTURE + SETUP-COGNEE + EXPORTING |
| `skills/` | CODE | 9 agent skills |
| `rules/` | CODE | 3 file-handling policies |
| `integrations/` | CODE | claude-code, codex native hooks, cursor, antigravity, obsidian |
| `benchmark/` | CODE | Benchmark suite + report |
| `knowledge/daily/` | KNOWLEDGE | Append-only session logs (private) |
| `knowledge/notes/` | KNOWLEDGE | Durable OKF pages |
| `knowledge/projects/<slug>/` | KNOWLEDGE | Per-project state.md |
| `knowledge/raw/` | KNOWLEDGE | Immutable sources |
| `knowledge/inbox/` | KNOWLEDGE | Unprocessed staging |
| `knowledge/feedback/` | KNOWLEDGE | Correction candidates |
| `cache/` | RUNTIME | Regenerable search / QMD / vector indexes (gitignored) |
| `logs/` | RUNTIME | Rotatable lint, compile, and diagnostic logs (gitignored) |
| `run/` | RUNTIME | Durable automation/recovery state: state, queue, journals, manifests, checkpoints, backups, transactions (gitignored; never delete wholesale) |
| `cache/cognee/` | RUNTIME | Optional semantic graph (gitignored) |

For the full canonical reference, see [STRUCTURE.md](STRUCTURE.md).
