# User Guide — LLM-Wiki Memory System

How to actually work with this system in **your** tools. After one-time
setup, the system maintains itself.

For the canonical structure reference (paths, env vars, zones), see
[STRUCTURE.md](STRUCTURE.md). For the design rationale, see
[ARCHITECTURE.md](ARCHITECTURE.md). For the agent operating contract, see
`../AGENTS.md` (root agent contract, byte-identical to `CLAUDE.md`).

---

## The mental model in one paragraph

Agents read and act on the vault through the local MCP server. Thin native
hooks/plugins forward lifecycle events that MCP cannot observe through
`integration_adapter.py`. The system decides what's worth remembering, saves
it as Markdown, and compiles in the background. SessionStart health is quiet
when healthy and injects only degraded/error findings.

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
   uv sync --locked --extra mcp-server
   uv run pytest -q          # verify: 4938 tests collected should pass
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
   mkdir -p cache logs run cache/cognee
   ```

4. **Wire up your agents** (see below).

### Wire up your agents

| Agent | How to wire |
|-------|-------------|
| **Claude Code** | Configure MCP for reads/actions, then run `uv run python scripts/merge_claude_settings.py` for five thin lifecycle hooks. |
| **OpenCode** | Configure MCP, then copy `scripts/llm-wiki-memory-opencode.js` for lifecycle events. |
| **Codex CLI** | Configure MCP; on Windows add `. "$env:LLM_WIKI_ROOT\scripts\codex-memory-wrapper.ps1"` to `$PROFILE` for lifecycle capture. |
| **Cursor** | Configure MCP and copy `integrations/cursor/rules/llm-wiki.mdc` to `.cursor/rules/`. |
| **Antigravity** | Configure MCP and copy `integrations/antigravity/AGENTS.md` to your project root. |
| **Obsidian** | Optional Markdown viewer only: open the vault directly. No Obsidian UI is required. |

The MCP server exposes 12 task-shaped tools, including `doctor`. All tools use
one response envelope, and health/context are also available as MCP resources.

### Exact 12-tool contract

The tool count and names are unchanged. These are the implemented behaviors in the
integrated Tasks 1-29 branch, not the broader Task 17 target:

| Tool | Current behavior |
|---|---|
| `recall` | Routes search through the retrieval planner. Result rows expose requested/effective mode, actual signals, generation, reranker fields, and fallback reason. The requested result limit is clamped to 1-20. |
| `read_page` | Reads one bounded slug-only Markdown page and resolves cited daily/archive evidence with source hashes. Evidence failure fails the page read closed. |
| `wiki_overview` | Reports page count, recommended retrieval tier, and vault root. It does not yet provide per-component generation health. |
| `vault_status` | Reports compile timestamp/status and changed-daily backlog only. |
| `get_decisions` | Uses the same retrieval path, filters active decision results, emits bounded telemetry, and clamps limits to 1-20. |
| `get_context` | Remains the bounded 1-20 slug batch with optional compatibility `content_preview`. The planned token-budgeted repo/symbol/evidence package is **evidence pending**. |
| `check_contradiction` | Returns structured assessments, evidence, validity, and lifecycle recommendations; unsupported evidence is quarantined rather than treated as verified. |
| `log_decision` | Appends through the locked daily-log writer; it does not directly publish a durable decision page. |
| `compile` | Requests the existing non-blocking, single-lock background compile. |
| `find_dead_code` | Queries the active Evidence Graph first and reports source generation, graph completeness, unresolved count, and fallback. `live=true` explicitly bypasses the store. |
| `get_architecture` | Uses the active Evidence Graph first for `summary`; `mode=impact` runs bounded diff-to-graph analysis. Only `summary` and `impact` are currently exposed; the broader planned mode set is **evidence pending**. |
| `doctor` | Exposes nine closed actions: `status`, queue inspect/cancel/redrive/dead-list, transaction recover/undo, archive status, and claim status. Mutation actions require `repair=true`. |

All responses retain JSON text compatibility and the common envelope. Structured MCP
output is used when the installed SDK supports it. The envelope still derives its
top-level index timestamp from legacy `cache/index.sqlite`; per-component generation
freshness in that envelope is **evidence pending**. Treat row-level generation and
fallback fields as the current retrieval truth.

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
in `run/queue.sqlite3` and drained by a short-lived worker at the next session.

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
uv run python scripts/lookup_mode.py                       # show direct/base/hybrid mode
uv run python scripts/doctor.py                            # local health; --repair is explicit
uv run --locked --no-sync python scripts/sync_memory.py --check --json  # read-only check
```

### Bounded synchronization

```bash
uv run --locked --no-sync python scripts/sync_memory.py --check --json
uv run --locked --no-sync python scripts/sync_memory.py --apply --json
```

`sync_memory.py` defaults to `--check`. It reports the ordered actions
`environment`, `dependencies`, `integrations`, `transactions`, `queue`,
`indexes`, and `doctor`, each as `ok`, `changed`, `skipped`, or `error`.
Apply mode has explicit elapsed-time and action-count limits, uses the locked
MCP baseline only, repairs missing runtime directories, and rebuilds a stale
FTS index in a bounded child process. Transaction recovery and queued
flush/compile work remain diagnostic-only here; run the explicit Doctor,
transaction, or queue operator command when those actions require attention.
Sync does not install semantic, reranker, code-graph, or model dependencies,
does not run Git, and never writes under `knowledge/`.
Exit codes are stable: `0` means synchronized (`ok` or `changed`), `1` means
incomplete or degraded, and `2` means an error or invalid invocation.

## Reliable operations

Markdown remains authoritative; SQLite coordinates transactions, receipts,
derived indexes, leases, and queued work. Keep `$LLM_WIKI_STATE_ROOT` on a local
filesystem. Network filesystems are rejected. Cloud-folder detection is best-effort,
so do not place live runtime SQLite files in a synchronized folder even if no warning
appears. The current runtime uses rollback-journal, `synchronous=FULL`, and no WAL.

### Evidence generation activation and recovery

Generation publication has no in-place update. A builder captures exact source bytes
and hashes, writes and fsyncs a new directory, validates its manifest, source
membership, database schema/integrity, artifact hashes, and evidence spans, registers
it, then compare-and-swap activates it against the expected prior ID. If source
membership changes or any step fails before activation, the old generation remains
active.

Readers verify the active pointer and artifact seal around each open. A corrupt or
missing active generation is skipped; the catalog revalidates activation history and
parent lineage and repairs the pointer to the newest usable prior generation.
Recovery can register a complete immediate-child orphan, but does not activate it.
There is currently no supported end-user generation migration/status CLI: **evidence
pending**. Use `doctor` for overall runtime health and inspect MCP retrieval rows for
`generation`, `effective_mode`, `signals_used`, and `fallback_reason`.

### Legacy cache migration and safe rollback

Migration is additive and non-destructive:

1. Back up or commit authoritative Markdown and Git state as you normally would.
2. Leave `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json`, and
   `cache/lancedb/` in place.
3. Build and validate a generation through the integrated builder/catalog API.
4. Activate only with the expected active generation ID; a CAS mismatch means retry
   from a fresh snapshot, not overwrite.
5. Exercise lexical, optional vector, graph, code, impact, and citation reads and
   confirm their generation/fallback fields.
6. Keep the legacy caches until installed-vault migration evidence proves removal
   safe. That evidence is currently pending.

To roll back, stop active commands. Either reactivate a previously validated
generation through the catalog API or delete only `cache/evidence-graph/` and rebuild
later. Never remove `knowledge/`, `.git/`, project journals, or `run/`. With legacy
caches retained, retrieval resumes through legacy FTS/vector/Lance paths. If graph
state is absent, code tools use bounded live extraction and report `fallback=true`,
`graph_complete=false`.

Deleting all `cache/` is also knowledge-safe but removes every derived index and model
cache, so retrieval is degraded until rebuild. It does not relax the separate `run/`
deletion contract.

### Model, token, and citation labels

The model matrix pins candidate revisions and forbids a predetermined winner. A new
embedding or reranker default requires complete raw EN/RU/ZH quality and resource
measurements, license checks, no parent-recall regression, material improvement, and
Pareto efficiency. The matrix currently selects neither: **evidence pending**. The
legacy optional vector path remains a compatibility path, not proof of superiority.

Token fields must be read with their labels. `reported` comes from a provider;
`tokenizer` from a model adapter; `estimated` currently uses UTF-8 byte length;
`mixed` combines sources; `unknown` has no numeric value. Cost kind is separately
`reported`, `estimated`, or `unknown`. Do not compare estimated and reported values as
if they were equivalent measurements.

Grounded QA citations identify supplied authoritative spans by citation ID, relative
path, source hash, revision, byte/line range, and span hash. Every answered atomic
claim must cite supplied evidence. Invalid, duplicate, missing, changed, generated-
summary, or out-of-root citations fail verification. The safe outcomes are a verified
`answered` document or an explicit `insufficient_evidence`, `conflicting_evidence`,
or `unsupported_time_scope` abstention.

### Health, recovery, undo, and retention

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
```

Recovery rolls verified prepared/applying transactions forward and quarantines a
target that matches neither its recorded before nor after hash. It never overwrites
unknown bytes. Undo creates a new forward transaction and works only while every
target still matches the original committed after-hash. Pruning removes expired
transaction images; after the 30-day undo window, or after an explicit prune, that
undo history is gone. External editors may briefly observe a mixed tree while a
multi-file transaction applies. CAS safety is guaranteed only for cooperating
transaction-API writers; concurrent external edits are unsupported and detected
best-effort.

### Queue migration and work

```bash
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
```

Run migration once to import legacy `run/queue/*.json` and `.processing` files.
Migration aborts if it cannot exclude a live legacy owner and quarantines malformed
source records. The queue is priority/FIFO and at least once, not exactly-once;
handlers rely on stable operation IDs. Defaults are priority 0 in `-100..100`, a
120-second lease with 40-second heartbeat, 8 attempts, 30/3600-second full-jitter
retry base/cap, and worker bounds of 20 tasks, 600 seconds, or 2 idle seconds.
`redrive` creates a linked new task without resetting dead history. `purge` requires
a terminal cutoff and verified export path before deleting terminal rows/results.
Dead tasks are retained indefinitely; succeeded/cancelled results default to 30 days.

### Daily archive and claims

```bash
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

The archive moves, never deletes, eligible daily logs older than the 90-day hot
window. A source remains flat if its compile receipt, terminal operations, queue
preflight, exact evidence, or pins do not validate. Published BagIt bags are immutable
and uncompressed; logical evidence resolves from the flat file first and then a
verified bag. There is no gzip archive tier. Claims with invalid evidence, evaluator
disagreement, unsupported semantics, or low confidence enter
`knowledge/inbox/claims/` quarantine. The frozen benchmark reports false
supersession and provenance metrics; automatic semantic supersession and eager
backfill remain disabled.

### Safe runtime deletion

`cache/` and `logs/` are disposable. Do not delete `run/` until `doctor` reports no
nonterminal, conflicted, quarantined, or source failure transaction; no transaction
inside the 30-day undo window; no retained queue task/result or legacy queue artifact;
and no live project lease, writer, queue worker, or maintenance owner. Deleting an
otherwise eligible `run/` loses undo history. Installers and repair commands never
remove it automatically. The system also performs no automatic Git operation and
provides no persistent daemon, cloud service, remote queue/cache, or SQLite knowledge
source.

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

This installs `sentence-transformers` with `BAAI/bge-small-en-v1.5`.
Embeddings are cached in `cache/vectors.npy` with metadata in
`cache/vectors_meta.json` (both gitignored) and rebuilt automatically when
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
- Check runtime dirs exist: `cache/`, `logs/`, `run/`; `queue.sqlite3` is created on demand
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
- Run `uv sync --locked --extra mcp-server` first (the installed baseline includes MCP)
- `uv run pytest -q` — should report 4938 tests collected
- If `< 4938`, your checkout is stale; `git pull`

---

## Where things live

| Path | Zone | Purpose |
|------|------|---------|
| `scripts/` | CODE | Pipeline + hooks + helpers |
| `tests/` | CODE | 4938 tests collected |
| `docs/` | CODE | This file + ARCHITECTURE + STRUCTURE + SETUP-COGNEE + EXPORTING |
| `skills/` | CODE | 9 agent skills |
| `rules/` | CODE | 3 file-handling policies |
| `integrations/` | CODE | Thin claude-code, cursor, and antigravity host wiring |
| `benchmark/` | CODE | Benchmark suite + report |
| `knowledge/daily/` | KNOWLEDGE | Append-only session logs (private) |
| `knowledge/notes/` | KNOWLEDGE | Durable OKF pages |
| `knowledge/projects/<slug>/` | KNOWLEDGE | Append-only journal.md + projected state.md |
| `knowledge/raw/` | KNOWLEDGE | Immutable sources |
| `knowledge/inbox/` | KNOWLEDGE | Unprocessed staging |
| `knowledge/feedback/` | KNOWLEDGE | Correction candidates |
| `cache/` | RUNTIME | FTS5/vector/graph indexes, compile plans, derived claims index |
| `logs/` | RUNTIME | Lint reports, compile logs (gitignored) |
| `run/` | RUNTIME | transactions, receipts, queue database/results, leases, locks |
| `cache/cognee/` | RUNTIME | Optional semantic graph (gitignored) |

For the full canonical reference, see [STRUCTURE.md](STRUCTURE.md).
