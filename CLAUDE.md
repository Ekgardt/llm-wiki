# LLM Wiki — Agent Operating Contract

You are working in the **LLM-wiki** memory system — a local, file-based,
git-native knowledge base for multi-agent memory. This file is the canonical
operating contract for any AI agent (Claude Code, OpenCode, Codex) editing
this repository. `AGENTS.md` and `CLAUDE.md` are kept
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

# KNOWLEDGE (the repository ships no memory: only READMEs, the project
#            scaffold and two empty runtime files are tracked)
knowledge/
  daily/      # append-only session capture
  notes/      # durable compiled pages (OKF frontmatter, flat slugs)
  projects/   # per-project state.md / context
  raw/        # immutable sources
  inbox/      # unprocessed staging

# RUNTIME (inside the vault, gitignored)
# Override root via LLM_WIKI_STATE_ROOT (tests use a temp dir).
cache/     # FTS5 / vector / graph indexes; legacy cache/cognee/ is retired
logs/      # lint reports, compile logs, SessionStart debug dumps
run/       # state, transactions, queue results, locks, install ownership
```

**Env contracts:**
- `$LLM_WIKI_ROOT` → vault root (the repository root). Default: resolved from
  `scripts/` location, worktree-aware.
- `$LLM_WIKI_STATE_ROOT` → runtime root. **Default: the vault itself** →
  `cache/`, `logs/`, `run/` at vault root, all gitignored.
  Override for multi-disk setups or hermetic tests.
- `$MEMORY_LLM_PROVIDER` → `fake` (tests), or one of
  `opencode|codex|claude|openai|ollama` (runtime, auto-detected).
- `$LLM_WIKI_DLP_POLICY` → optional absolute external policy path. Invalid or
  digest-mismatched required policy blocks protected work.

**Agent integration boundary:** MCP is the common interface for reads and
actions (12 task-shaped tools, uniform response envelope, health/context
resources). Native hooks, plugins, and wrappers are thin lifecycle adapters
for events MCP cannot observe. Automatic health context is injected only when
`doctor` reports degraded/error findings.

**Approved audit-closure contract (2026-08-15):** Cognee is retired from the
supported product; existing `cache/cognee/` is disposable legacy cache and is not
deleted automatically. First-party model calls use one fail-closed DLP boundary.
Verified local-only mode accepts only literal-loopback Ollama and requires
verifiable Ollama cloud disablement. Coherent encrypted private-vault recovery uses
Restic with the existing maintenance fence, SQLite online backup, manifests, and
staged validation. `run/install/` is the only new runtime directory and owns
resumable install state, exact-release/external-path manifests, verified preimages,
and non-secret scheduler definitions. Windows uses Task Scheduler, macOS uses a
user LaunchAgent, Linux uses a user systemd timer, and cron is explicit degraded
fallback. Blackboard and capture reuse the two Reliability v3 databases. No daemon,
MCP tool, or runtime root is added. The prohibition on automatic Git operations was
lifted by the owner on 2026-08-23 for one bounded case only — see
`knowledge/notes/automatic-code-update-decision.md`. See
`knowledge/notes/audit-closure-security-recovery-control-plane-decision.md`.

**Superset product contract (approved 2026-07-19):** LLM Wiki is the single
local-first memory, code-intelligence, and agent-control product for one person
managing many agents, sessions, projects, repositories, branches, and worktrees.
Normal Graphify and codebase-memory-mcp workflows must not require a separate
installation. The approved target includes a native code-index kernel,
repository-scoped generations, multi-repository contracts, temporal claims,
episodic/semantic/procedural/prospective memory, one Adaptive Context Compiler,
durable task/execution/artifact/budget control, an exception-driven local
operator console, optional bounded watching, HTTP MCP, and broad ingestion.
These additions remain derived or operational; they do not displace Markdown,
Git, raw episodes, project journals, accepted decisions, or accepted artifacts
as authority. Completion requires real paired task, token, latency, safety, and
operator-attention evidence. A finished subplan or smoke fixture is not product
completion. See `knowledge/notes/solo-operator-superset-product-decision.md`.

**Reliable mutation boundary:** automatic Markdown writes use recoverable
transactions with before/after hashes. Project handoff is projected from an
append-only `journal.md`. `cache/` and `logs/` are disposable; `run/` must not be
deleted while doctor reports a nonterminal, conflicted, or quarantined
transaction, a transaction inside the 2-day undo window, or any retained queue
task or result, or while a project lease, writer, queue worker, or maintenance
owner is live. Deleting eligible committed artifacts loses undo history.

**Stage 2 operational contract:** Markdown remains authoritative; runtime SQLite
is coordination/derived state, never a knowledge source. Operational databases use
rollback-journal, `synchronous=FULL`, and no WAL on the current SQLite runtime. They
require a local filesystem with correct locking. Network/cloud-synchronized runtime
roots are unsupported; cloud detection is best-effort. External editors may briefly
see a mixed tree, and hash/CAS guarantees apply only to cooperating transaction-API
writers. Queue delivery is at least once. Archives keep 90 hot days and preserve
logical evidence in immutable uncompressed BagIt packages. Claims with uncertain
evidence or evaluator disagreement enter quarantine; automatic semantic supersession
and eager backfill remain disabled. Do not delete `run/` while doctor reports a
source failure, any 2-day undo artifact, retained work/result, or live owner. There
is no persistent daemon, cloud service, remote queue/cache, exactly-once promise, or
gzip archive tier. The single automatic Git operation on the checkout is the nightly
fast-forward update, which never pushes, never resolves a conflict, and declines
whenever the update would touch a locally modified file — see
`knowledge/notes/automatic-code-update-decision.md`. The knowledge snapshot
(`scripts/snapshot_knowledge.py`) commits only in its own remote-less repository
outside the vault (`~/llm-wiki-snapshots/`).

**Reliability v3 (implemented; the installers adopt it):** New
unprocessed lifecycle evidence is create-only under `run/capture-intents/`
until an immutable terminal record proves committed Markdown, validated
no-durable-content, or operator discard; queue enqueue alone does not permit
deletion. Compile receipts bind logical path plus digest and a validated
per-source disposition; digest-only v2 receipts remain historical evidence
only. Queue payload hashes are checked at every transition, and dedupe
aliases only identical kind, handler version, and payload. All operational actors
use one canonical fenced admission registry, including capture, project/Markdown
writers, queue, and LSP. Explicit offline adoption publishes versioned v3
databases and replaces legacy active paths with JSON tombstones, blocking normal
v2 queue/transaction clients after cutover; `install.sh` and `install.ps1` run that
adoption on a fresh vault, and the nightly pass works the adopted queue and adopts
capture intents. Live, expired-but-not-proven-dead, or
unknown owners, unresolved intents, and partial adoption block `run/` deletion.
Doctor
reports a quiescent snapshot only after complete adoption, and never a durable deletion
permit. Class names that still say "Candidate" are the pre-adoption reader, not a
statement that the target is unbuilt.
See `knowledge/notes/v4-reliability-contracts-decision.md`.

**Derived evidence generations:** Markdown, Git, and project journals are
authoritative. All graph, FTS, vector, tier, and telemetry generation state is
disposable and derived. `cache/evidence-graph/` is the new target generation layout.
The rollback-journal,
`synchronous=FULL`, no WAL catalog at `cache/evidence-graph/catalog.sqlite3`
selects one active generation under
`cache/evidence-graph/generations/<generation-id>/`.
Each generation is immutable after activation; optional vectors are absent,
complete, or explicitly stale.
These databases require a local filesystem with correct locking. Cache deletion is
regenerable and does not change the existing `run/` deletion contract.
No generation database belongs under `run/`; it remains operational state only.
The design requires no persistent daemon.

The legacy FTS5 index (`cache/index.sqlite`, `cache/.paths-manifest`) and the legacy
vector cache (`cache/vectors.npy`, `cache/vectors_meta.json`) were retired on 2026-09-23:
the generation is the only index. A search with no active generation reads Markdown
directly, bounded by its deadline, and every such hit says `no_active_generation`; the
installer's sync builds the first generation (`doctor --rebuild-generation` on
demand), and the nightly refreshes it. Those files are read by nothing and may be deleted; nothing deletes them
automatically. LanceDB was retired on 2026-09-07: its table was never built on
the installed vault, its path was reachable only from the deadline-less legacy
search, and its index was keyed to a different embedder than the product's — see
`knowledge/notes/retire-lancedb-decision.md`.

**Implemented code-navigation slice:** The current authoritative corpus checkpoint
remains `corpus-generation/v2` with `evidence-graph/v2`. The whole 2026-07-21 Plan A is
superseded: its one-shot consent/SCIP/publication Tasks 6-16 were superseded by the
replacement plan, and on 2026-09-18 the foundation that nothing in production ever ran --
the sealed code workspace, the `code_capture` manifest section, the verified-analysis
records and the `evidence-graph/v3` schema they filled -- was removed from the code
(`docs/research/2026-09-18-the-superseded-plan-a-seam-leaves-the-code.md`).
The replacement plan implements the production-quality,
Python 3.10-compatible read-only LSP path through four pinned managed language
servers: paths, positions, bounded protocol, startup evidence, leased
platform-qualified lifecycle ownership, repository containment, safe log redaction,
explicit profile installation, document synchronization, session-manager capacity,
the normalized navigation facade, deterministic rendering, precise
`get_architecture` modes, doctor diagnostics, and qualification gates. A query is
routed to one profile by file suffix; a suffix no profile claims falls back to
Pyright, which opens the file, answers nothing, and degrades to structural evidence.
A session whose close failed is closed again by the next caller for its key, under
that caller's deadline, and is evicted before a healthy idle one. A start that ran
out of time or met the operating system is retried at most three times, after 5 s,
30 s and 120 s; identity, protocol and capability failures stay terminal. Open
documents are a bounded cache, not a ledger: the least recently used is closed with
`textDocument/didClose` to make room, and `synchronize` re-reads a retained document
only when its file identity changed. A server unused for 300 seconds is closed by the
next request for another checkout, at most one per call and never the session being
asked for — there is still no daemon and no timer. The transport's server-notification
allowlist is the profile registry's union, so a profile added there is carried by the
transport; a server whose post-initialize identity does not name the engine we pinned
answers as degraded.
A Windows Job Object owns the assigned server tree. On POSIX, the process group
covers the assigned managed server's descendants only while they remain in-group;
hostile `setsid()` escape is unsupported, so this path remains limited to trusted
local repositories and is not an OS sandbox.
It adds no Serena runtime dependency, Rust rewrite, second graph, catalog, active
pointer, runtime root, persistent daemon, or MCP tool. Query-time LSP observations
are not written into active generations. Language servers start lazily inside the
owning MCP process, expose only allowlisted read operations, report readiness and
capability limits, and fall back to existing structural evidence. Installation is
a separate explicit operator action, per profile: `scripts/install_pyright.py` for
Pyright and `scripts/install_language_server.py --profile <name>` for the other
three. The managed artifacts live at `cache/code-tools/pyright/1.1.411/`,
`cache/code-tools/typescript-language-server/6.0.0/` (tsserver 5.9.3),
`cache/code-tools/gopls/v0.23.0/` (built at install time from the pinned Go 1.27.1
toolchain, because upstream publishes no binary) and
`cache/code-tools/rust-analyzer/1.98.1/` (published, and arriving with its pinned
Rust toolchain because it reads the project through `cargo`). Bounded process
scratch lives under `run/lsp/<owner-nonce>/`, holds the sealed digest-verified copy
of a native server that is launched from it, and follows the existing `run/`
deletion contract, which
protects live LSP owners and retained LSP failure evidence. While ownership is live,
`run/lsp/<owner-nonce>/lease.json` is a bounded mutable live lease, refreshed every
10 seconds with a 30 seconds expiry, and remains distinct from immutable create-only
`owner.json` and `failure.json`. `failure.json` carries an optional `stderr_tail`:
the last kilobyte the failed server wrote, redacted line by line through
`lsp_security.redact_lsp_text` and bounded by its JSON encoding. `owner.json` names
the owner and the generation it started with; after a recovery restart the live
generation is named by the lease and the failed one by the failure record, and doctor
no longer requires the three to agree on a generation nonce or a pid. Controlled
cleanup removes the lease after joining its heartbeat; abrupt death leaves it to
expire, and starting a server sweeps sibling owner roots in `run/lsp/` whose records
name only processes proven dead and which hold no `failure.json`; roots holding
failure evidence are kept as evidence, and the nightly retires those older than 14
days beyond the newest 20 (`scripts/retire_lsp_evidence.py`, 2026-09-23). Second-fatal recovery completes
without caller intervention. Incomplete startups enter a bounded module registry;
Pyright sessions adopt returned cleanup owners into session-held normal-exit and
caller-deadline retry, while unadopted owners stay registered. Windows lease refresh
retries only errors 5, 32, and 33 within the previous lease expiry. See
`knowledge/notes/read-only-lsp-navigation-engine-decision.md`,
`knowledge/notes/lsp-live-lease-decision.md`, and
`knowledge/notes/lsp-process-containment-decision.md`.

**Session evidence (approved 2026-08-23):** every captured session writes a
redacted copy of itself to `knowledge/raw/sessions/<date>/<session-id>.md` before
any classification and regardless of the tier — the conversation verbatim, each
tool call as one line naming the tool and its target, and a foreground subagent's
report kept whole up to 8 000 characters. Retention never depends on
a judgement made before the question exists; the classifier decides only whether
a session also deserves a compiled page. That directory is private by default
(`knowledge/raw/**` is denied in `.gitignore`), the record is bounded, and a
failed write never breaks capture. Session evidence is not yet a member of the
corpus generation, so it is on disk and greppable but not yet part of hybrid
retrieval — adding it requires the corpus collector to carry a `session` source
kind. See `knowledge/notes/session-evidence-retention-decision.md`.

**Forbidden at vault root:** `wiki/`, `memory/`, `outputs/`, `state/`,
`LLM-wiki-state/` (legacy sibling layout — removed). Runtime lives **inside**
the vault under gitignored `cache/logs/run/`.

---

## 2. One directory, two audiences

This repository is **both** the public source and the installed, running
vault. `$LLM_WIKI_ROOT` and `$LLM_WIKI_STATE_ROOT` point **here**. The owner
merged the two directories on 2026-08-21; the second one was uninstalled and
removed first. See `knowledge/notes/single-directory-vault-decision.md`.

**What keeps private knowledge out of a public repository is `.gitignore`, not
a directory boundary.** Read that sentence again before you commit anything.

| Zone | Tracked? | What lives there |
|---|---|---|
| `scripts/ tests/ docs/ skills/ rules/ integrations/ benchmark/` | yes | the product |
| `knowledge/daily/*.md`, `knowledge/notes/*` | **denied by default** | your real memory |
| the `!` allowlist inside those denials | yes | the READMEs only — the repository ships no memory (2026-09-10) |
| `cache/ logs/ run/` | never | runtime state |

`knowledge/index.md` is the exception: the runtime rewrites it and it is tracked.
`tests/test_structure.py::test_the_vault_index_and_log_name_only_published_notes`
holds the line — every page it names by path must be published. The vault's
editorial log is `knowledge/log.local.md`, private (denied in `.gitignore`); the
tracked `knowledge/log.md` is only the template the repository ships, and nothing
writes to it. See `docs/research/2026-09-14-the-vault-log-is-private.md`.

### What this changes in practice
- Writing a knowledge page here is **normal runtime behaviour**, not a
  violation. It stays private because its directory is denied by default.
- **Publishing** a page is a deliberate, separate act: adding an explicit `!`
  line for it in `.gitignore`. Since 2026-09-10 no page is published: the
  architecture decision pages this file refers to are the owner's private
  record, and a fresh install starts with an empty memory (issue #19). The
  contracts themselves are stated here and in `docs/STRUCTURE.md`.
- Running `compile_memory.py`, `flush_memory.py` or any pipeline script here is
  **correct**, because here is the vault. It was forbidden when a second,
  private directory existed to run them against.

### Before every commit
- `git status --short` — nothing from `cache/`, `logs/`, `run/`.
- `git diff --cached --name-only -- knowledge/` — every path must be one you
  can defend publishing.
- If a knowledge page appears staged that you did not deliberately publish, the
  allowlist is wrong. Fix `.gitignore`, do not commit the page.

### When asked to "work on the memory system"
- "Improve the system" → edit code, run tests, commit, push to
  `Ekgardt/llm-wiki`.
- "Show me my memory / what do I know about X" → read the vault: the same
  directory, in `knowledge/` and the runtime databases.

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
   - `knowledge/index.md` — regenerated, never hand-edited: run
     `scripts/rebuild_memory_index.py` (the compile does it inside its
     transaction). It is tracked and publication-filtered, so it names only
     published pages; on a vault that publishes none, the private navigation is
     `scripts/search_memory.py` plus the log below.
   - `knowledge/log.local.md` (the private vault log)
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

`knowledge/log.local.md` (the private vault log) is deliberately **not** imported. It is an append-only
editorial changelog, not operating context, and it had grown to 304,980 bytes
— about 76,000 tokens in every session before any work began, which killed
three agent runs outright on 2026-08-29 with `Prompt is too long`. Rule 4
forbids spending tokens like that on the vault's own changelog. Read it when
you need it: `grep` it, or ask the memory for the decision you are after. Rule
4 of section 3 still requires appending to it on every important update. See
`docs/research/2026-08-29-what-belongs-in-every-session.md`. Past 2 MiB a compile
archives it whole to `knowledge/log-archive/log.local.<date>.md` (gitignored) and
starts a fresh log that names the archive
(`docs/research/2026-09-25-the-vault-log-rotates-before-its-cap.md`).

### Default behavior for new material
When asked to compile or ingest new material:
1. Inspect `knowledge/inbox/` and/or the target source file.
2. Decide whether to create or update pages under `knowledge/notes/`.
3. Regenerate `knowledge/index.md` with `scripts/rebuild_memory_index.py`; do
   not edit it by hand.
4. Append a concise entry to `knowledge/log.local.md`.
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
    default to medium / inferred and lose ranking. A raw session record carries
    a fifth value, `source_authority: session`: the user's own words, unreviewed,
    so it ranks between `inferred` and `ai-derived` in retrieval and is never a
    claim authority — a claim compiled from a session record is `ai-derived`.

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
| `entity` | `knowledge/notes/<slug>.md` | Named things/people/orgs. Never archives. |
| `decision` | `knowledge/notes/<slug>.md` | Dated choice + rationale. Immutable; supersede. |
| `pattern` | `knowledge/notes/<slug>.md` | Recurring approach. 180-day archive. |
| `debugging` | `knowledge/notes/<slug>.md` | Symptom → cause → fix. 60-day archive. |
| `qa` | `knowledge/notes/<slug>.md` | Settled answer to a recurring question. 365-day. |
| `synthesis` | `knowledge/notes/<slug>.md` | Cross-page comparison/connection. Never archives. |
| `raw-source` | `knowledge/notes/<slug>.md` | Excerpted primary source. Archivable. |
| `workflow` | `knowledge/notes/<slug>.md` | Auto-promoted playbook. 365-day. |
| `gap` | `knowledge/notes/<slug>.md` | Not-yet-written knowledge. 90-day. |
| `skill` | `skills/<name>/SKILL.md` | Agent workflow. Never archives. |
| `rule` | `rules/<name>.md` | File-handling policy. Never archives. |
| `project-state` | `knowledge/projects/<slug>/state.md` | Per-project handoff. Never archives. |
| `project-context` | `knowledge/projects/<slug>/` | Per-project context. Never archives. |
| `bootstrap-context` | `knowledge/notes/<slug>.md` | Seed context for new sessions. Never archives. |

Pages live **flat** as `<slug>.md` under `knowledge/notes/` (the compile
pipeline writes flat slugs). Typed subdirectories are optional.

---

## 6. LLM backend

The memory pipeline needs an LLM for classification, compilation,
contradiction checks, and playbook crystallization. Backend is
**auto-detected** via `scripts/llm_client.py` — no API keys required.

Priority: OpenCode (only when `OPENCODE_SERVER_PASSWORD` protects its server) →
Codex → Claude CLI → OpenAI → Ollama. If none answers, the call returns
nothing: a capture already sits in `run/queue.sqlite3` and waits an hour before
its next attempt, and every other caller skips or fails its step; nothing else is
queued for later (2026-09-25). A `run/queue/` directory left by a release before
v4.0.0 is refused and named by `doctor`, never imported (2026-09-23).

Override via `MEMORY_LLM_PROVIDER` env var. `fake` returns a canned response
for tests/e2e. A value that names no provider calls none, and `doctor` names it.

**Zero-cost path:** no paid API beyond existing agent subscriptions. Ollama remains
an optional local backend; the retired Cognee bridge is not a supported feature.

---

## 7. Quick command reference

```bash
uv run pytest -q                              # run the full regression suite
uv run ruff check scripts/ tests/             # Python static analysis
uv run python scripts/lint_memory.py --scope all   # structural lint
uv run python scripts/search_memory.py "query"     # hybrid search
uv run python scripts/compile_memory.py            # compile daily logs → notes
uv run python scripts/lookup_mode.py               # show retrieval tier
uv run python scripts/mcp_server.py                # MCP server (12 tools, stdio; base install)
uv run python scripts/doctor.py                    # local health; --repair is explicit
# v4.0 optional features (require --extra flags):
uv run python scripts/repository_index.py index .  # index a repository's code (a generation)
uv run python scripts/code_graph.py .              # read-only live parse summary
uv run python scripts/impact_analysis.py           # git diff → stale wiki pages
uv run python scripts/reflection.py --apply        # A-MEM page consolidation
uv run python scripts/access_tracking.py --flush   # flush access counts
uv run python scripts/build_tiers.py --all          # generate L1 overviews
```

Runtime state is gitignored and never committed. `cache/` and `logs/` are
regenerable; `run/` is operational state and follows the deletion contract in
section 1.
