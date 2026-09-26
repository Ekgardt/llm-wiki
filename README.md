# LLM Wiki

[![Tests](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml/badge.svg)](https://github.com/Ekgardt/llm-wiki/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Version](https://img.shields.io/badge/version-5.0.0-blue.svg)](CHANGELOG.md)

**One local memory for all your AI coding agents — plain Markdown on your disk, in Git, owned by you.**

Claude Code, Codex and OpenCode each forget everything when a session ends. LLM Wiki
records what happened in every session, distils it overnight into short knowledge
pages, and gives the next session — in any of those agents — the decisions, lessons
and project state it needs. You stop explaining the same thing twice.

Storage, capture, search and the MCP server run locally. Turning sessions into pages
needs a language model: the one you configure, or the first one found among OpenCode,
Codex, Claude, OpenAI and Ollama. Only Ollama is local; the others are cloud services,
so auto-detection is not a local-only guarantee. Current version: **5.0.0**.

**Languages:** [English](README.md) | [Русский](README.ru.md) | [简体中文](README.zh-CN.md)

---

## How it works

```
You work with an agent as usual
      ↓  thin hooks send each session event to integration_adapter.py
Session record  →  knowledge/raw/sessions/<date>/  (redacted, kept for every session)
Daily log       →  knowledge/daily/<date>.md
      ↓  compile (at session start when idle, and every night)
Knowledge pages →  knowledge/notes/<slug>.md   (every quote checked against its source)
      ↓
Next session, in any agent: learned rules, open threads, last decision,
project state — and 12 task-shaped MCP tools to ask the memory anything
```

The idea is "compile, not retrieve" ([Karpathy, April 2026](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)):
instead of searching raw transcripts at question time, a background pass turns them
into structured pages once, and the agent reads those.

A day at work, as the system sees it:

- You fix a bug with Claude Code and say "never use the legacy API here again".
  The session is recorded; that night the correction becomes a rule page.
- The next morning you open Codex in the same repository. Its first message already
  carries the rule, the project's open threads and yesterday's decision.
- You ask "why did we drop LanceDB?". The agent calls `recall` and answers with the
  decision page and the exact lines of the session it came from.

---

## Quick Start

### Prerequisites

- Python 3.10+ and git
- [uv](https://docs.astral.sh/uv/) 0.12.3 exactly — both installers refuse any other version
- An agent you already use: Claude Code, Codex or OpenCode
- Node 22 only if you want precise TypeScript or Python navigation (see below)

### Install

Clone and read the source first, then run the installer from that checkout:

```bash
git clone https://github.com/Ekgardt/llm-wiki.git
cd llm-wiki
```

**macOS / Linux / WSL2:**
```bash
LLM_WIKI_ROOT="$(pwd)" bash ./install.sh
```

**Windows:**
```powershell
$env:LLM_WIKI_ROOT = (Get-Location).Path
.\install.ps1
```

The installer builds the vault's own `.venv` from the exact lock, installs the pinned
search models, runs a bounded production smoke test, wires every agent it finds,
registers the nightly and weekly passes, and builds the first search index. It prints
what it did for each agent and whether anything still needs your hand.

A remote bootstrap is supported only for an exact commit: set `LLM_WIKI_COMMIT` to a
full 40-character commit OID and pipe the installer from a trusted location. Branch
and tag names are refused. Every release lists its commit and the SHA-256 of each file
the bootstrap runs:

```bash
uv run python scripts/release_manifest.py v5.0.0 --markdown
```

### Check it

```bash
uv run python scripts/doctor.py
uv run python scripts/search_memory.py "auth"
```

`doctor` is read-only and says what is healthy, degraded or broken, and what to run.

### Dependency profiles

MCP is part of the production baseline; `mcp-server` remains a compatibility alias.
The installer does this for you; by hand:

```bash
uv sync --locked --no-default-groups
uv run --locked --no-sync python scripts/install_smoke.py --deadline-seconds 120
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

The repair command is read-only by default. On a fresh or quiet vault the installer
runs it with `--apply --adopt-ownership-v3 --confirm-all-agents-stopped` to move the
runtime to its current database format; on a vault that already holds work it asks you
to confirm that no agent is running first. It never deletes knowledge or `run/`.

Optional extras add to what is installed and keep what you already chose:

```bash
uv sync --locked --no-default-groups --inexact --extra hybrid      # vectors + reranker
uv sync --locked --no-default-groups --inexact --extra code-graph  # code index
```

Contributors install the development group and run the full regression suite:

```bash
uv sync --locked
uv run --locked --no-sync pytest -q
```

Pre-commit hooks (ruff, structural lint, gitleaks) are available.
Opt-in; the installer does not activate these hooks:
`uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push`.

---

## Agents

| Agent | Status | How it is wired |
|-------|--------|-----------------|
| **Claude Code** | Automatic when the settings merge verifies | MCP server + hooks in `settings.json`: seven lifecycle events go to `integration_adapter.py`, two hooks add code-graph hints to searches and subagents |
| **Codex CLI** | Automatic when its configuration verifies; approve the hooks once in `/hooks` | MCP server + seven lifecycle hooks |
| **OpenCode** | Automatic when its configuration verifies | MCP server + a thin JS lifecycle plugin |
| **Obsidian** | Viewer only | An optional Obsidian viewer: open the vault folder; nothing to install |

Cursor and Antigravity were retired on 2026-08-26; the installer no longer detects or
configures them, and `uninstall` still takes back hooks an earlier install wrote.

All agents share one vault: a decision recorded in Claude Code is in Codex's next
session.

### The MCP interface

The local MCP server exposes **12 task-shaped tools**: `recall`, `read_page`,
`wiki_overview`, `vault_status`, `get_decisions`, `get_context`,
`check_contradiction`, `log_decision`, `compile`, `find_dead_code`,
`get_architecture` and `doctor`. Every answer comes in one envelope that states its
schema version, freshness, evidence quality and warnings, and two resources expose
health and context. A session start stays silent when everything is healthy and
injects only degraded or broken findings. `doctor(repair=true)` limits itself to safe,
idempotent local repairs.

The server speaks stdio by default. When several agents run at once, one shared local
server is cheaper:

```bash
uv run python scripts/mcp_http.py --port 8931
```

It binds literal loopback only, refuses any `Origin`, and requires the bearer token it
writes to `<state root>/run/mcp-http/token` with mode 0600.

---

## What you get

**Capture that never loses a session.** Every session writes a redacted record of
itself — the conversation, one line per tool call, a subagent's report — before any
judgement about its value. A classifier then decides whether it also deserves a
compiled page. Secrets (keys, tokens, passwords in URLs and commands) are removed
before anything is written.

**Pages you can trust.** Compile turns daily logs into typed pages (decision, pattern,
debugging, concept…) with YAML frontmatter. Python checks every quote against the
source line and its digest before a page is written; a second model pass reviews each
change and drops weak ones. A contradiction with an existing page is recorded, not
overwritten: the old page is marked `superseded`, and uncertain cases wait in
quarantine. Every write is a recoverable transaction with a two-day undo.

**Context at session start.** Learned rules from your corrections, open threads, the
last decision, lint alerts and insights from other projects, scoped to the project you
are in: a page compiled from one project's sessions carries `project:` and does not
reach another project's sessions.

**Search that says how it answered.** Lexical search (BM25) is the base; the `hybrid`
extra adds multilingual vectors (`intfloat/multilingual-e5-small` on ONNX Runtime)
and a cross-encoder reranker (`BAAI/bge-reranker-v2-m3`), and relation questions add
the evidence graph. Ranking weighs where a claim came from (you, the web, a model, a
guess) and what kind of page holds it. Every answer reports the mode it asked for, the
signals it actually used, and why it fell back. Until the first index is built, search
reads the Markdown directly and says so.

**Many projects, one vault.** Each repository gets a project folder with its state,
context and an append-only journal; a new project is bootstrapped from its Git history
and README on first sight.

**Maintenance that runs itself.** A nightly pass updates the code (fast-forward only,
never pushes, declines when it would touch a file you changed), works the queue,
compiles, refreshes the search index, keeps a local Git snapshot of `knowledge/`, and
reports health. A weekly pass lints (17 checks), archives old daily logs, and migrates
frontmatter. Windows uses Task Scheduler, macOS a LaunchAgent, Linux a user systemd
timer; cron is an explicit degraded fallback.

**Code intelligence.** `get_architecture` and `find_dead_code` answer from a code index
of your repositories; precise definitions, references, callers and diagnostics come
from pinned language servers (see [Code navigation](#code-navigation)).

---

## Where things live

```
CODE        scripts/  tests/  docs/  skills/  rules/  integrations/  benchmark/
KNOWLEDGE   knowledge/{daily,notes,projects,raw,inbox}
RUNTIME     cache/  logs/  run/        (inside the vault, never committed)
```

- **Code** is this repository.
- **Knowledge** is your memory. The repository ships it empty: every page, daily log and
  session record is denied by `.gitignore`, and only the READMEs are tracked. Publishing
  a page is a deliberate act.
- **Runtime** is gitignored. `cache/` and `logs/` can be deleted and rebuilt; `run/`
  holds transactions, the queue and undo history and follows the deletion rules in
  [docs/STRUCTURE.md](docs/STRUCTURE.md).
- **Authority.** Markdown, Git history and project journals are the truth. Search
  indexes, vectors, the evidence graph and telemetry are derived and rebuildable.

Design rationale: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Day-to-day operation,
recovery and backup: [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Keeping memory safe

The operational databases use rollback journaling with `synchronous=FULL`; keep the
state root on a local disk (network paths are refused). Queue delivery is at least
once, so every handler is idempotent.

```bash
uv run python scripts/doctor.py
uv run python scripts/doctor.py --repair
uv run python scripts/doctor.py --rebuild-generation
uv run python scripts/markdown_transaction.py recover
uv run python scripts/markdown_transaction.py undo <transaction-id>
uv run python scripts/markdown_transaction.py prune --retention-days 30
uv run python scripts/memory_queue.py work --max-tasks 20 --max-seconds 600 --idle-seconds 2 --lease-seconds 120 --heartbeat-seconds 40 --max-attempts 8 --retry-base-seconds 30 --retry-cap-seconds 3600
uv run python scripts/memory_queue.py redrive <task-id>
uv run python scripts/memory_queue.py purge --terminal-before <ISO-8601> --export <path>
uv run python scripts/archive_daily.py --commit --hot-days 90
```

Daily logs older than 90 days move into verified, uncompressed BagIt packages (the
weekly pass does this); evidence that quotes them still resolves. For a copy that
survives a lost disk, the vault has an encrypted Restic backup with staged restore;
the nightly Git snapshot of `knowledge/` is local and not encrypted. See the backup
section of [docs/USER-GUIDE.md](docs/USER-GUIDE.md).

---

## Code navigation

Precise modes — `definition`, `references`, `implementations`, `type`,
`diagnostics`, and positioned `callers`/`callees` — use four pinned language servers:
**Pyright 1.1.411** (Python), **typescript-language-server 6.0.0** with tsserver 5.9.3
(TypeScript/JavaScript), **gopls v0.23.0** (Go) and **rust-analyzer 1.98.1** (Rust).
Each is installed explicitly; a query never downloads anything:

```bash
uv run python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile typescript --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile gopls --state-root "$LLM_WIKI_STATE_ROOT"
uv run python scripts/install_language_server.py --profile rust-analyzer --state-root "$LLM_WIKI_STATE_ROOT"
```

A file no server claims answers `unsupported`; a server that is missing or failing
degrades the answer to the code index instead of failing it.
This path is for trusted local repositories and is not an OS sandbox. Details:
[docs/CODE-NAVIGATION.md](docs/CODE-NAVIGATION.md).

---

## Search index

`cache/evidence-graph/catalog.sqlite3` selects one immutable active generation under
`cache/evidence-graph/generations/<generation-id>/`: the full-text index, vectors,
evidence graph and tiers built from one snapshot of your pages. A new generation is
activated only after its manifest, hashes, databases and evidence spans validate; a
failed build leaves the previous one active. The installer builds the first
generation, the nightly refreshes it, and
`uv run python scripts/doctor.py --rebuild-generation` rebuilds it on demand.
Deleting `cache/evidence-graph/` loses nothing but time.

---

## Benchmark

Retrieval is gated on the frozen public synthetic corpus
`benchmark/retrieval-v2.json` — multilingual pages with graded evidence,
distractors, temporal history and questions that must be refused:

```bash
uv run python benchmark/run_benchmark.py
```

Long-horizon memory is measured on LongMemEval (`benchmark/run_longmemeval.py`), and
contradiction handling on its own frozen corpus:

```bash
uv run python benchmark/run_contradiction_benchmark.py --corpus benchmark/contradiction-v1.json
```

No comparison with other memory systems is claimed here: their published numbers use
different datasets.

---

## Contributing

Contributions are welcome. The bar is "does this survive a real multi-agent
workflow?". See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, coding standards and the
release checklist (the three READMEs, CHANGELOG and version change together).

---

## Credits

- [Karpathy's LLM Wiki gist](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) — the "compile, not retrieve" pattern
- [Harrison Chase, "Wiki Memory"](https://blog.langchain.dev/wiki-memory/) — agent-maintained files
- [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md) — vendor-neutral Markdown knowledge format
- [Anthropic, effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) — capture, compact and subagent patterns
- [VEP Semantic DNA](https://vep.live) — confidence, supersede and temporal lifecycle

---

## License

[MIT](LICENSE)
