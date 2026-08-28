# LLM Wiki

[![Tests](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-4.0.0-blue.svg)](CHANGELOG.md)

**A local-first memory system for AI agents. Markdown files, git-tracked, and owned by you.**

LLM Wiki gives every AI coding agent you use — Claude Code, OpenCode, Codex — one MCP-first interface to a shared, persistent knowledge base. MCP handles reads and actions; thin native lifecycle adapters capture session events that MCP cannot observe. Durable knowledge survives across sessions so you never re-explain the same thing twice.

Everything lives on your disk as plain markdown: readable in Obsidian, diffable in git, owned entirely by you.

Storage, capture, MCP, and retrieval are local. Model-backed classification and
compilation use the configured provider: OpenCode, Codex, Claude, and OpenAI may use
cloud services; Ollama may be local. Auto-detection is not a local-only guarantee.

**Languages:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## Table of Contents

- [How it works](#how-it-works)
- [Features](#features)
- [Quick Start](#quick-start)
- [Wire up your agents](#wire-up-your-agents)
- [Architecture](#architecture)
- [Evidence generations and migration](#evidence-generations-and-migration)
- [Benchmark](#benchmark)
- [Comparison](#comparison)
- [Contributing](#contributing)
- [Credits](#credits)
- [License](#license)

---

## How it works

```
Your agent reads and acts through the local MCP server
             ↓
Thin hooks/plugins send lifecycle events through integration_adapter.py
             ↓
Background compile distills daily logs into durable knowledge pages
(with VERIFY-BEFORE-WRITE — citations are checked, not trusted)
             ↓
Next session: guardrails + advisory + metacognitive context auto-injected
             ↓
Agent picks up where you stopped — no re-explaining, no repeated mistakes
```

The system follows the "compile, not retrieve" pattern ([Karpathy, April 2026](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)): raw session signals are captured in real time, then a background LLM pass compiles them into structured knowledge pages rather than relying on raw retrieval at query time.

---

## Features

### Capture pipeline
- **Thin lifecycle adapters**: Claude Code and Codex hooks plus the OpenCode plugin normalize events through `integration_adapter.py`
- **3-tier session classification**: FLUSH_MAJOR (decisions/lessons → triggers compile), FLUSH_MINOR (gotchas → save only), FLUSH_OK (chatter → skip)
- **Non-LLM breadcrumbs** — prompt + tool-usage tagging at ms-latency, no API calls
- **Secret redaction** — API keys, tokens, long base64 stripped before any write

### Agent-native interface
- **MCP-first access** — 12 task-shaped local tools for recall, context, decisions, maintenance, code intelligence, and `doctor`
- **Uniform response envelope** — every tool reports schema version, freshness, evidence quality, warnings, and data; MCP resources expose health and context
- **Automatic health** — SessionStart stays quiet when healthy and injects only degraded/error findings; `doctor(repair=true)` limits repairs to safe, idempotent local actions

### Compile pipeline
- **JSON-protocol compile** — no agent tool-use required, works with any LLM backend
- **Fail-closed model boundary** — prompts are redacted before transport; sensitive provider output and invalid required DLP policy block publication
- **VERIFY-BEFORE-WRITE** — Python-side deterministic citation verification; the LLM cannot fabricate evidence
- **Semantic dedup with quarantine** — update is preferred over create; uncertain or evaluator-disputed contradictions are quarantined and automatic semantic supersession remains disabled
- **Incremental** — SHA-256 hashing; only changed daily logs are recompiled
- **Concurrency-safe** — PID lock with stale detection; only one compile runs at a time
- **Persistent task queue** — offline-tolerant; deferred LLM work drains on next session

### Search and retrieval
- **Generation-consistent retrieval**: one validated immutable generation can bind FTS, vectors, graph, tiers, and evidence to the same source snapshot
- **Truthful retrieval traces**: results report requested/effective mode, signals actually used, generation, reranker state, and fallback reason
- **Triple-fusion when available**: BM25 (FTS5) + Vector (sentence-transformers) + evidence-backed Graph-neighbor RRF
- **Weighted RRF**: BM25=2.0, Vector=1.0, Graph=0.5 — prevents regression on known-item queries
- **Title + filename boost** — exact filename match short-circuits to rank 1
- **Typed-provenance ranking** — one weight table (`user` 1.35, `web` 1.1, `ai-derived` 1.0, `inferred` 0.8) multiplies the score that decides the order on every path: BM25, fused RRF, and reranked
- **Temporal queries** — `--as-of YYYY-MM-DD` filters by `valid_to` frontmatter
- **Local retrieval modes** — direct page reads at small scale, SQLite FTS5 BM25 as the always-available base, and optional vectors/LanceDB + graph + reranker for hybrid retrieval
- **Grounded QA** — retrieved source spans carry citation IDs, paths, source/span hashes, revisions, and byte/line ranges; unsupported, conflicting, or out-of-scope answers abstain

### Proactive intelligence
- **Guardrails** — auto-injects learned corrections at SessionStart (prevents repeating mistakes)
- **Advisory** — surfaces open threads, last decision, lint alerts, cross-project insights
- **Metacognitive context** — vault inventory, compile backlog, flush tier distribution
- **Feedback capture** — detects corrections/preferences in transcripts, saves as promotion candidates

### Multi-project and multi-agent
- **One vault, many projects** — 5-step collision-safe slug system, per-project `state.md`
- **Project bootstrap** — auto-generates context from git history, README, tech stack
- **Blackboard protocol** — parallel agents claim tasks, signal completion, detect conflicts
- **Loop detector** — flags repeated edit cycles (fix → review → redo)
- **Agent timeline** — attribution: which agent decided what and when

### Maintenance
- **16 lint checks (15 structural + 1 LLM-judged contradiction)** — broken wikilinks, orphans, stale compiles, missing backlinks, sparse pages, missing frontmatter, missing or invalid type, missing sources, invalid supersede chains, orphan gaps, temporal validity, unresolvable evidence, invalid claim schema, contradictions
- **Type-aware archive** — debugging 60d, patterns 180d, decisions never
- **Nightly + weekly schedules** — compile, lint, archive, OKF migration (Task Scheduler on Windows, LaunchAgent on macOS, user systemd on Linux; cron is an explicit degraded fallback)
- **OKF v0.1 frontmatter** — `type`, `confidence`, `source_authority`, `supersede` fields; auto-migration from legacy pages

### Infrastructure
- **5 LLM backends** (auto-detected): OpenCode → Codex → Claude CLI → OpenAI → Ollama
- **Cross-platform**: Windows, macOS, Linux, WSL2
- **Local and zero-daemon** — the installed baseline includes the MCP package; vector search remains optional
- **Cross-platform CI matrix**: Ubuntu + Windows + macOS, Python 3.10 through 3.14
- **Pre-commit hooks (opt-in)**: ruff (static analysis) + structural lint + gitleaks (secret scanning). Opt-in; the installer does not activate these hooks. Enable them with `uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push`.

---

## Quick Start

### Prerequisites

- Python 3.10+
- git
- [uv](https://docs.astral.sh/uv/)
- An AI agent you already use (Claude Code, OpenCode, or Codex)

### Source install

The recommended path is still to clone and inspect the source before installing:

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
```

After inspection, run the installer from that checkout:

**macOS / Linux / WSL2:**
```bash
LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
```

**Windows:**
```powershell
$env:LLM_WIKI_ROOT = (Get-Location).Path
.\install.ps1
```

The installer also supports remote bootstrap only when `LLM_WIKI_COMMIT` is an exact
40-hex commit OID. Pipe the installer from a trusted location while setting that value;
the bootstrap fetches that exact commit, verifies `HEAD`, repository identity, and required
files, then executes only the checked-out installer. Branch and tag names are rejected.

The local installer syncs the locked production baseline, runs a bounded production smoke,
creates runtime directories, and wires supported agents. The full regression suite remains
a separate development and release gate. Existing checkouts keep all Git remote settings;
pass `--protect-push` or `-ProtectPush` to replace every remote's push URLs with `no-push`.

### Verifying a release

A published release states the exact commit the remote bootstrap accepts —
branch and tag names are rejected — together with the SHA-256 of every file the
bootstrap runs. Print them for any tag from a local checkout:

```bash
uv run python scripts/release_manifest.py v4.0.0 --markdown
```

Install that exact commit:

```bash
LLM_WIKI_COMMIT=$(git rev-parse v4.0.0^{commit}) bash ./install.sh
```

### Shared HTTP transport (optional)

The MCP server speaks stdio by default: every agent starts its own process. If
you run several agents at once, one shared local server is cheaper — measured
on this vault, a marginal agent costs 1220.3 MiB through stdio and 0.1 MiB
through the shared server, and a new session answers in 0.010-0.013 s instead
of 1.3-2.9 s. A single call costs 8-22 ms more, so for one agent stdio still
wins.

```bash
uv run python scripts/mcp_http.py --port 8931
```

It binds literal loopback only, refuses any `Origin`, and requires the bearer
token it writes to `<state root>/run/mcp-http/token` with mode 0600. stdio is
unchanged and remains the default.

### Dependency profiles

MCP is part of the production baseline; `mcp-server` remains a compatibility alias. Fresh
production installs use the exact lock without development groups:

```bash
uv sync --locked --no-default-groups
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

The repair command is read-only by default and reports fresh, upgrade-required,
partial, adopted, or conflicting Reliability V3 evidence without creating `run/`.
Mutating adoption is intentionally not activated yet: even with the offline apply
flags, the backend fails closed with `reliability_v3_runtime_activation_incomplete`
until the v3 queue writers and canonical ownership protocol are complete. It never
deletes `run/`, knowledge, retired databases, legacy caches, or compatibility markers.

Optional extras are additive and preserve packages already selected by the operator:

```bash
uv sync --locked --no-default-groups --inexact --extra hybrid
uv sync --locked --no-default-groups --inexact --extra code-graph
```

Contributors install the locked development group and run the full regression suite without
an implicit sync:

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

Node 22 is optional and is needed only for qualified precise Python navigation with Pyright.

### Verify it works

```bash
uv run python scripts/search_memory.py "auth"
uv run python scripts/lookup_mode.py
```

---

## Wire up your agents

LLM Wiki detects installed agents during install and reports whether integration is automatic or still requires a manual step:

| Agent | Status | Integration | How |
|-------|--------|-------------|-----|
| **OpenCode** | Automatic when configuration verifies | MCP + thin JS lifecycle plugin | MCP provides reads/actions; the plugin forwards lifecycle events to `integration_adapter.py` |
| **Codex CLI** | Automatic when configuration verifies; review hook trust in `/hooks` | MCP + official lifecycle hooks | MCP provides reads/actions; hooks forward lifecycle events |
| **Claude Code** | Automatic when settings merge verifies | MCP + thin settings.json hooks | MCP provides reads/actions; five hooks forward lifecycle events |
| **Obsidian** | Viewer only | Optional Markdown viewer | Open the vault directly; no Obsidian UI or ingestion feature is required |

Cursor and Antigravity were retired on 2026-08-26; the installer no longer detects or configures them,
and `uninstall` still takes back hooks an earlier install wrote.
All agents share the same vault — a decision recorded by Claude Code is visible to OpenCode in its next session.

### Optional: semantic search

For hybrid BM25 + Vector search (finds semantically related pages even when keywords don't match):

```bash
uv sync --locked --no-default-groups --inexact --extra semantic
```

---

## Architecture

```
CODE          scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE     knowledge/{daily,notes,projects,raw,inbox,feedback}
RUNTIME       cache/  logs/  run/   (gitignored, inside vault)
```

- **CODE** — tracked in git. The pipeline, tests, docs, skills, rules, integrations.
- **KNOWLEDGE** — tracked in git (public examples). Full user data lives in the installed vault. Daily logs and personal pages are gitignored.
- **RUNTIME** — gitignored. Search indexes and logs are disposable; transactions, queue state, and undo images under `run/` are operational state.
- **Authority boundary** — Markdown, Git history, and append-only project journals are authoritative. FTS, vectors, Evidence Graph databases, tiers, telemetry, and model caches are derived and rebuildable.

Full design rationale (7 axioms, system architecture diagram, memory taxonomy, search architecture) in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

For the canonical structure reference (what lives where, env contracts, forbidden layouts), see [docs/STRUCTURE.md](docs/STRUCTURE.md).

---

## Evidence generations and migration

`cache/evidence-graph/catalog.sqlite3` selects one immutable active generation under `cache/evidence-graph/generations/<generation-id>/`. A candidate is registered only after its manifest, source membership, artifact hashes, database integrity, and evidence spans validate. Activation is a compare-and-swap pointer update. A failed or interrupted pre-activation build leaves the previous generation active; a corrupt active generation is skipped in favor of the newest validated prior generation. Complete orphan generations may be registered during recovery but are not activated automatically.

Deleting `cache/evidence-graph/` deletes only derived state. Stop active commands first, keep `run/`, and rebuild before expecting generation-backed retrieval. Until installed-vault migration evidence proves removal safe, keep legacy `cache/index.sqlite`, `cache/vectors.npy`, `cache/vectors_meta.json`, and `cache/lancedb/`. If no validated generation can be opened, retrieval falls back to those legacy paths or lexical/live extraction and reports the fallback. Safe rollback never deletes `knowledge/`, Git history, project journals, or `run/`.

The model matrix pins candidate revisions and requires EN/RU/ZH quality, resource, license, and Pareto gates before selecting defaults. No new embedding model or reranker is selected yet: **evidence pending**. Existing optional vector compatibility still uses its pinned legacy model. Token counts are labelled `reported`, `tokenizer`, `estimated`, `mixed`, or `unknown`; monetary cost is separately `reported`, `estimated`, or `unknown`. A UTF-8 byte estimate is conservative planning data, not a tokenizer-independent guarantee.

Real Graphify comparison and model-superiority evidence are pending. The deterministic comparative smoke validates orchestration only and supports no quality or token-ratio claim.

See [docs/USER-GUIDE.md](docs/USER-GUIDE.md) for activation, recovery, rollback, citation, and exact MCP behavior details.

---

## Reliable memory operations

Markdown remains authoritative. Runtime SQLite coordinates recoverable writes and queued work but is not a knowledge source. Operational databases use rollback-journal mode, `synchronous=FULL`, and no WAL on the current SQLite runtime. Keep the state root on a local filesystem; network paths are rejected and cloud-synchronized folder detection is best-effort.

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/doctor.py --time-budget 60
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py migrate
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path> --include-dead
uv run python scripts/memory_queue.py restore --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
uv run python benchmark/run_flush_classification.py --corpus benchmark/flush-classification-v1.json
```

Queue delivery is at least once, so handlers use stable operation IDs for idempotency. Archives move eligible daily logs older than the 90-day hot window into verified, uncompressed BagIt packages while preserving logical evidence resolution. Uncertain or evaluator-disputed claims are quarantined; semantic supersession stays disabled until the frozen benchmark gate is met. See [docs/USER-GUIDE.md](docs/USER-GUIDE.md) for recovery, retention, and deletion safety.

---

## Benchmark

> **Historical legacy methodology**: BM25-only FTS5 over the git-tracked public corpus, with graph, vectors, and reranking disabled. `current-generated-v2` had 112 deterministic known-item queries: exact title, summary keywords, partial title, and slug. `legacy-60-v1.json` stores the original 60 query texts and gold paths verbatim, so later page edits cannot change that gate. Ignored personal pages and `$LLM_WIKI_ROOT` are excluded, so a clean clone reproduces the same corpus. This is not LoCoMo or LongMemEval; competitor rows use different datasets.

| Historical metric | Historical current 112 | Historical legacy 60 | agentmemory | Zep | Mem0 |
|--------|-------------|-----------|-------------|-----|------|
| Recall@1 | **94.6%** | n/a | n/a | n/a | n/a |
| Recall@3 | **100.0%** | n/a | n/a | n/a | n/a |
| Recall@5 | **100.0%** | **100.0%** | 95.2% | 94.7% | 91.6% |
| Recall@10 | **100.0%** | n/a | n/a | n/a | n/a |
| MRR | **0.9702** | **0.9694** | 0.882 | n/a | n/a |
| Latency p50 | **6.3ms** | n/a | 14ms | 155ms | 880ms |

These are historical results from the legacy runner. The default command now runs the frozen retrieval-v2 benchmark. Only plain `--legacy-only` selects the old gate; combining it with `--semantic` or `--report` fails closed.

Run retrieval-v2: `uv run python benchmark/run_benchmark.py`

Reproduce the old gate: `uv run python benchmark/run_benchmark.py --legacy-only`

### MCP agent interface

The local stdio MCP server exposes **12 task-shaped tools**, including `doctor`, with one response envelope and health/context resources. `find_dead_code(directory)` returns conservative candidates, while `get_architecture(directory)` reports entry points, routes, canonical-symbol hotspots, and communities. Filesystem analysis requires an explicit existing non-root directory and never falls back to the process CWD.

Precise Python modes `definition`, `references`, `implementations`, `type`,
`diagnostics`, and positioned `callers`/`callees` use pinned **Pyright 1.1.411**.
Install it explicitly; queries never download or update it:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
```

This path supports **trusted local repositories** only and is **not an OS sandbox**.
See [docs/CODE-NAVIGATION.md](docs/CODE-NAVIGATION.md) for positions, deadlines,
freshness, containment, and qualification limits.

---

## Comparison

| Capability | LLM Wiki | agentmemory | ReMe | akitaonrails |
|------------|----------|-------------|------|--------------|
| Markdown-first | Yes | No | Yes | Yes |
| Multi-agent (3+ tools) | Yes (3) | Yes (32+ via MCP) | Claude only | Yes (12+) |
| IDE support | optional Obsidian viewer | No | No | No |
| Compile-not-retrieve | Yes | No | No | No |
| VERIFY-BEFORE-WRITE | Yes | No | No | No |
| Guardrails (learned corrections) | Yes | No | No | No |
| Blackboard coordination | Yes | No | No | No |
| Loop detection | Yes | No | No | No |
| Agent timeline | Yes | No | No | No |
| Feedback learning | Yes | No | No | No |
| Local / zero-daemon | Yes | No (Docker) | No (pip) | No (Rust) |
| Temporal validity (`valid_to`) | Yes | No | No | No |
| Typed provenance ranking | Yes | No | No | No |

---

## Contributing

Contributions are welcome. The bar is "does this survive contact with an actual multi-agent workflow?"

See [CONTRIBUTING.md](CONTRIBUTING.md) for:
- Development setup
- Release checklist (README i18n sync, CHANGELOG, version bump)
- Coding standards (ruff, pytest, pre-commit)
- How to add a new agent integration

---

## Credits

- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the "compile, not retrieve" pattern
- [Harrison Chase's "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — agent-maintained files
- [Google's OKF spec](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — vendor-neutral markdown knowledge format
- [Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — capture/compact/subagent patterns
- [VEP Semantic DNA](https://vep.live) — confidence/supersede/temporal lifecycle

---

## License

[MIT](LICENSE)
