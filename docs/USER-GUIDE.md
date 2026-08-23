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
required** beyond what you already have; Ollama is an optional local backend.

Backend disclosure:

| Backend | Processing boundary |
|---------|---------------------|
| OpenCode | LLM Wiki calls a loopback OpenCode server; the selected OpenCode model may use a remote service. |
| Codex / Claude | LLM Wiki invokes a local CLI; the CLI may use its account's remote service. |
| OpenAI | Prompts are sent to the configured HTTPS API. |
| Ollama | The configured Ollama endpoint is used. A remote endpoint or cloud model is not local processing. |

All first-party calls pass through the fail-closed model boundary. Built-in secret
detectors are always active. `LLM_WIKI_DLP_POLICY`, when set, must name an absolute,
regular UTF-8 JSON file containing exactly `version`, `literals`,
`allow_fingerprints`, and `sha256`. The digest is SHA-256 of canonical JSON for the
first three fields. Literals are fixed strings, not regular expressions; fingerprints
allow only one exact payload. A missing, unreadable, invalid, oversized, or
digest-mismatched required policy blocks model transport and publication.

For the strict Ollama path, disable cloud in the Ollama server, restart it, and force
the provider and a literal loopback IP:

```bash
export MEMORY_LLM_PROVIDER=ollama
export OLLAMA_NO_CLOUD=1
export MEMORY_LLM_BASE_URL=http://127.0.0.1:11434/v1
```

```powershell
$env:MEMORY_LLM_PROVIDER = "ollama"
$env:OLLAMA_NO_CLOUD = "1"
$env:MEMORY_LLM_BASE_URL = "http://127.0.0.1:11434/v1"
```

This disables provider fallback, rejects non-literal-loopback endpoints, and requires
the selected model to appear in `/api/tags` with local size/digest metadata and no
`remote_model` or `remote_host`. The descriptor still reports
`external_runtime_unverified`: the client cannot prove that an independently managed
Ollama process was restarted with cloud disabled. Do not describe this state as
verified network isolation.

---

## One-time setup

### Option A: Local installer from an inspected checkout (recommended)

Install `uv` from https://docs.astral.sh/uv/ first. The installer does not execute a
mutable remote dependency bootstrap.

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
```

```powershell
git clone https://github.com/Ekgardt/llm-wiki.git
Set-Location llm-wiki
$env:LLM_WIKI_ROOT = (Get-Location).Path
.\install.ps1
```

Remote bootstrap accepts only a full 40-hex `LLM_WIKI_COMMIT`. It fetches that exact
commit into a new `~/LLM-wiki`, verifies `HEAD`, repository identity, and required files,
then executes the checked-out installer. Branches and tags are rejected. Existing
checkouts retain all remote settings unless `--protect-push` or `-ProtectPush` is explicit.
The installer detects agents. It configures OpenCode, Codex, and Claude only when their
configuration verifies. When local Cursor or Antigravity is detected, it also installs
and verifies managed user-level hooks. Cursor cloud agents do not load those hooks.
Obsidian remains a viewer-only integration.

### Installed-vault reliability check

Run the shared installed-vault validator without mutation:

```bash
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

The check performs bounded reads and does not create `run/`, change Git, or delete
knowledge, operational state, retired databases, legacy caches, tombstones, or
compatibility markers. It reports Reliability V3 fresh, upgrade-required, partial,
adopted, and conflict evidence in a closed JSON envelope.

Mutating Reliability V3 adoption remains disabled until the v3 queue mutation and
canonical ownership tasks are complete. Supplying the full offline apply gate currently
fails closed with `reliability_v3_runtime_activation_incomplete`; do not treat that as a
successful cutover and do not remove v2 state manually.

### Option B: Manual setup

1. **Clone + install dependencies:**
   ```bash
   git clone https://github.com/Ekgardt/llm-wiki.git
   cd llm-wiki
   uv sync --locked --extra mcp-server
   uv run pytest -q          # inspect the current full regression status
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
   mkdir -p cache logs run
   ```

4. **Wire up your agents** (see below).

### Wire up your agents

| Agent | How to wire |
|-------|-------------|
| **Claude Code** | Configure MCP for reads/actions, then run `uv run python scripts/merge_claude_settings.py` for five thin lifecycle hooks. |
| **OpenCode** | Configure MCP, then copy `scripts/llm-wiki-memory-opencode.js` for lifecycle events. |
| **Codex CLI** | Configure MCP; on Windows add `. "$env:LLM_WIKI_ROOT\scripts\codex-memory-wrapper.ps1"` to `$PROFILE` for lifecycle capture. |
| **Cursor** | Configure MCP for reads/actions, install Cursor locally, then rerun the native installer. It manages exact LLM-Wiki handlers in `~/.cursor/hooks.json`; the rules file is optional guidance. |
| **Antigravity** | Configure MCP for reads/actions, install Antigravity locally, then rerun the native installer. It manages only the top-level `llm-wiki` fragment in `~/.gemini/config/hooks.json`; `AGENTS.md` remains optional guidance. |
| **Obsidian** | Optional Markdown viewer only: open the vault directly. No Obsidian UI is required. |

Managed IDE hooks preserve unrelated configuration and use verified sibling preimages.
Malformed configuration, ownership conflicts, or drift fail closed instead of being
overwritten. `doctor` reports active, absent, or conflicting structural ownership and
never repairs these files implicitly.

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
| `get_architecture` | Keeps structural `summary`, `symbol`, `callers`, `callees`, `dependencies`, `path`, `community`, and `impact`; precise Python `definition`, `references`, `implementations`, `type`, `diagnostics`, and positioned call modes use the owned Pyright session. |
| `doctor` | Exposes nine closed actions: `status`, queue inspect/cancel/redrive/dead-list, transaction recover/undo, archive status, and claim status. Mutation actions require `repair=true`. |

All responses retain JSON text compatibility and the common envelope. Structured MCP
output is used when the installed SDK supports it. The envelope still derives its
top-level index timestamp from legacy `cache/index.sqlite`; per-component generation
freshness in that envelope is **evidence pending**. Treat row-level generation and
fallback fields as the current retrieval truth.

## Read-only Python code navigation

Precise Python navigation uses pinned **Pyright 1.1.411** through the existing
`get_architecture` MCP tool. Install the managed package explicitly:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
```

No query, doctor check, or profile discovery path downloads or updates Pyright.
The precise modes are `mode=definition`, `mode=references`,
`mode=implementations`, `mode=type`, and `mode=diagnostics`; positioned
`callers` and `callees` also use Pyright. Input lines are one-based and character
values are zero-based UTF-8 byte offsets. Structural modes retain their existing
10-second deadline; precise modes use one absolute 60-second deadline.

This feature supports **trusted local repositories** only. It is not an OS sandbox.
Pyright runs with the current user's permissions and may read configured
interpreters, external stubs, and libraries. Windows uses a Job Object for the
assigned process tree. POSIX uses a process group while descendants remain in that
group; hostile `setsid()` escape is unsupported.

Every result binds pre/post workspace revisions and current source citations. One
stale attempt is retried once. There is no semantic result cache, no query-time graph
publication, and no complete-negative promise. See
[CODE-NAVIGATION.md](CODE-NAVIGATION.md) for status semantics, exact bounds, doctor
codes, and qualification evidence.

### Register scheduled maintenance

The installers publish profile/environment, scheduler, and detected Cursor/Antigravity
hook fragments through one resumable `run/install/` ownership transaction. Version 2
keeps the pre-first-install projection for uninstall and one latest committed update
projection for explicit rollback. Recovery uses persisted historical definitions, not
the current checkout templates. Rerun the native installer to reconcile owned state;
do not edit generated task, plist, unit, or owned hook definitions in place.

Inspect the control-plane state without mutation:

```bash
uv run --locked --no-sync python scripts/install_control.py status --state-root "${LLM_WIKI_STATE_ROOT:-$LLM_WIKI_ROOT}"
```

The `rollback` and `uninstall` subcommands require the same root, state root, home,
profile or PowerShell path, and `uv` executable used by the installer. They restore an
owned projection only when the current value still matches the installed value; drift
blocks mutation. Run `uv run python scripts/install_control.py rollback --help` or
`uninstall --help` for the platform-specific arguments.

**Windows (Task Scheduler):**
```powershell
.\install.ps1
```
Creates `LLMWiki-Nightly` (daily 03:00) and `LLMWiki-Weekly` (Sunday 04:00).

**macOS (per-user LaunchAgent) and Linux (user systemd):**
```bash
bash ./install.sh
```

Linux requires an available user systemd manager. On a host without one, cron is an
explicit degraded fallback and is never selected silently:
```bash
bash ./install.sh --scheduler cron
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

NIGHTLY 03:00 (scheduler, subject to the operating-system login policy)
  Drain deferred queue → compile all pending → structural lint → prune old reports

SUNDAY 04:00 (scheduler)
  Everything nightly does + OKF conformance sweep + archive stale + prune failed queue tasks
```

Windows tasks run only while the current user is logged on. macOS LaunchAgents use
the same login-scoped policy.
`StartWhenAvailable` runs a missed Windows task after the machine wakes and the user
signs in; it does not run under a logged-out account. Linux user-systemd timers use
persistent catch-up after the user manager starts. The product does not claim
wake-from-sleep or logged-out execution. Explicit cron fallback follows the host's
cron and sleep policy.

If the LLM is offline, work is queued
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

Plain `search_memory.py` always runs BM25. `--semantic` enables vectors when the
optional model is available; graph-neighbor fusion applies only when graph evidence
is available. If optional signals are unavailable, search returns the BM25 result
instead of claiming triple-fusion.
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
uv run python scripts/lint_memory.py --scope all           # 15 structural checks
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
uv run python scripts/doctor.py --time-budget 60
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

### Encrypted private-vault backup and validated restore

Install exact Restic `0.19.1`, initialize a repository outside the vault, and keep
its credentials in Restic's standard external environment, protected password file,
or password command. The repository-file must contain exactly one repository location
and no inline password. Create an empty protected staging directory before backup:

```bash
uv run python scripts/private_vault_backup.py backup --staging <empty-staging-dir> --restic-binary <absolute-restic-path> --repository-file <absolute-repository-file>
```

Preserve the returned `snapshot_id` and `manifest_sha256` as the backup receipt. The
plaintext staging image is removed after Restic finishes. Backup refuses live or
unknown owners, source races, invalid Reliability-v3 state, corrupt databases,
overlapping repository/staging paths, partial Restic exit, or failed repository check.

Restore only to a pre-existing empty directory:

```bash
uv run python scripts/private_vault_backup.py restore --target <empty-restore-dir> --restic-binary <absolute-restic-path> --repository-file <absolute-repository-file> --snapshot-id <64-hex-id> --manifest-sha256 <64-hex-digest>
```

Restore verifies the exact receipt, canonical manifest, every file/link member, both
SQLite schemas and integrity, and Reliability-v3 ownership projections. Failure
clears restored plaintext; success leaves validated `vault/` and `state/` directories
for reviewed recovery. This command never overwrites or automatically publishes into
an installed vault.

### Queue migration and work

```bash
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path> --include-dead
uv run python scripts/memory_queue.py restore --export <path>
```

Run migration once to import legacy `run/queue/*.json` and `.processing` files.
Migration aborts if it cannot exclude a live legacy owner and quarantines malformed
source records. The queue is priority/FIFO and at least once, not exactly-once;
handlers rely on stable operation IDs. Defaults are priority 0 in `-100..100`, a
120-second lease with 40-second heartbeat, 8 attempts, 30/3600-second full-jitter
retry base/cap, and worker bounds of 20 tasks, 600 seconds, or 2 idle seconds.
`redrive` creates a linked new task without resetting dead history. `purge` requires
a terminal cutoff and verified export path before deleting terminal rows/results.
Succeeded and cancelled results default to 30 days. A dead task — one whose attempts
are exhausted — is kept until you ask for it by name with `--include-dead`, because it
is evidence that work never happened. `restore --export <path>` reads one export back,
verifies its manifest and every digest, and re-enqueues the work as new ready tasks;
it refuses the whole export if anything fails to verify.

### Daily archive and claims

```bash
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
uv run python benchmark/run_flush_classification.py --corpus benchmark/flush-classification-v1.json
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
   and no live project lease, writer, queue worker, maintenance owner, or LSP owner;
   retained LSP failure evidence also blocks deletion. Deleting an
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

This installs `sentence-transformers` with `intfloat/multilingual-e5-small` — 384 dimensions over 100 languages, so a question in one language reaches a page written in another. The English-only model it replaces scored every candidate alike on non-English questions.
Embeddings are cached in `cache/vectors.npy` with metadata in
`cache/vectors_meta.json` (both gitignored) and rebuilt automatically when
pages change.

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
- `uv run pytest -q` — inspect the current full regression status and reported failures
- If collection or imports fail, update the checkout and rerun `uv sync --locked --dev`

---

## Where things live

| Path | Zone | Purpose |
|------|------|---------|
| `scripts/` | CODE | Pipeline + hooks + helpers |
| `tests/` | CODE | Full hermetic regression suite |
| `docs/` | CODE | This file + ARCHITECTURE + STRUCTURE + CODE-NAVIGATION + EXPORTING |
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

For the full canonical reference, see [STRUCTURE.md](STRUCTURE.md).
