# Canonical Structure Reference

> **Single source of truth for the llm-wiki repository layout.**
> Any agent working in this repo MUST read this file before changing
> structure, paths, or env contracts. Changes require explicit user sign-off
> (see `../AGENTS.md` §0 — the root agent contract). The
> `tests/test_structure.py` suite enforces the invariants defined here.

## Three-zone layout

```
llm-wiki/                          ← vault root (= $LLM_WIKI_ROOT)
│
├── scripts/                       CODE — pipeline + hooks + helpers
├── tests/                         CODE — regression suite (pytest)
├── docs/                          CODE — architecture + user guide
├── skills/                        CODE — 9 agent skills (SKILL.md)
├── rules/                         CODE — file-handling policies
├── integrations/                  CODE — IDE/agent integrations
├── benchmark/                     CODE — benchmark suite + report
│
├── knowledge/                     KNOWLEDGE — content (gitignored: personal)
│   ├── daily/                       append-only session logs
│   ├── notes/                       durable OKF pages (flat slugs)
│   ├── projects/<slug>/             per-project state.md
│   ├── raw/                         immutable sources
│   ├── inbox/                       unprocessed staging
│   └── feedback/                    correction candidates
│
├── cache/                        RUNTIME — gitignored, derived indexes/caches
├── logs/                         RUNTIME — gitignored, diagnostic output
├── run/                          RUNTIME — gitignored, durable automation state
│
├── AGENTS.md                     ROOT — agent contract (byte-identical to CLAUDE.md)
├── CLAUDE.md                     ROOT — agent contract (byte-identical to AGENTS.md)
├── CHANGELOG.md                  ROOT — Keep-a-Changelog
├── CONTRIBUTING.md               ROOT — contribution guide
├── README.md                     ROOT — English (primary)
├── README.ru.md                  ROOT — Russian (faithful translation)
├── README.zh-CN.md               ROOT — Chinese (faithful translation)
├── LICENSE                       ROOT — MIT
├── install.ps1                   ROOT — Windows installer
├── install.sh                    ROOT — Unix installer
├── pyproject.toml                ROOT — project metadata + ruff/pytest config
├── uv.lock                       ROOT — lockfile
├── .github/                      ROOT — CI workflows, issue templates
├── .gitignore                    ROOT — ignore rules
├── .gitattributes                ROOT — line-ending normalization
├── .gitleaksignore               ROOT — false-positive allowlist
└── .pre-commit-config.yaml       ROOT — pre-commit hooks (ruff + lint + gitleaks)
```

## Env contracts (fixed)

| Variable | Default | Purpose |
|----------|---------|---------|
| `$LLM_WIKI_ROOT` | Resolved from `scripts/` location (worktree-aware via `git rev-parse --git-common-dir`) | Vault root — code + knowledge + runtime |
| `$LLM_WIKI_STATE_ROOT` | **The vault root itself** | Runtime root → `cache/`, `logs/`, `run/` at vault root. Override for multi-disk or hermetic tests. |
| `$MEMORY_LLM_PROVIDER` | Auto-detected (`opencode` → `codex` → `claude` → `openai` → `ollama`) | LLM backend for compile/flush/query. `fake` for tests. |

### External integration paths

- OpenCode plugins install under `$XDG_CONFIG_HOME/opencode/plugins/` when
  `XDG_CONFIG_HOME` is an absolute path. Unset, empty, or relative values are
  ignored and the absolute `~/.config/opencode/plugins/` fallback is used.
- On Windows, the installer also targets the `~/.config/opencode/plugins/`
  compatibility location when it differs from the effective XDG path.
  Destinations are normalized and compared case-insensitively before copying,
  so one physical path receives only one copy.
- These are client configuration paths outside the vault. They do not change
  `$LLM_WIKI_ROOT` or the three-zone layout.

## What lives where

### CODE zone (tracked in git)
- `scripts/` — pipeline and integration helpers. Central hub:
  `memory_state.py` (path/lock/state), `compile_memory.py` (LLM compile +
  VERIFY-BEFORE-WRITE), `flush_memory.py` (3-tier classification),
  `maybe_compile.py` (OS-lock-aware spawn), `search_memory.py` (triple-RRF),
  `llm_client.py` (5 backends + fake).
- `tests/` — 32 test files, 1916 collected on every platform; local Windows
  verification: 1881 passed, 35 skipped. Skip count varies with optional Bash,
  PowerShell, and symlink availability. Hermetic via `conftest.py` (pins
  `LLM_WIKI_ROOT` to checkout, redirects `LLM_WIKI_STATE_ROOT` to a temp
  dir, defaults `MEMORY_LLM_PROVIDER=fake`).
- `docs/` — `ARCHITECTURE.md`, `USER-GUIDE.md`, `AGENTS.md` (knowledge
  subsystem brief — subordinate to the root `../AGENTS.md` contract),
  `EXPORTING.md`, `SETUP-COGNEE.md`, `operating-model.md`,
  `STRUCTURE.md` (this file).
- `skills/` — 9 SKILL.md files (knowledge-compile, knowledge-lookup,
  knowledge-review, knowledge-qa-file-back, contradict-check,
  crystallize-playbook, bridge-promote-insight, session-memory-compile,
  session-memory-review).
- `rules/` — 3 rule files (wiki-files, raw-files, output-files).
- `integrations/` — claude-code (settings.json), codex
  (`hooks.template.json` for native lifecycle hooks), cursor (rules),
  antigravity (AGENTS.md), obsidian (Web Clipper template).
- `benchmark/` — `run_benchmark.py` + `report.md`.

### KNOWLEDGE zone (tracked: public fixtures; gitignored: personal)
- `knowledge/daily/` — append-only `YYYY-MM-DD.md`. Private (gitignored).
  Public synthetic fixtures (`2026-04-13.md`, `2026-04-19.md`) are
  un-ignored to restore Evidence links.
- `knowledge/notes/` — durable OKF pages, flat `<slug>.md`. Public examples
  tracked via allowlist; personal pages gitignored.
- `knowledge/projects/<slug>/` — per-project `state.md`, `context.md`,
  `.blackboard/`. Template tracked; real projects gitignored.
- `knowledge/raw/` — immutable sources. Gitignored (personal).
- `knowledge/inbox/` — unprocessed staging. Gitignored.
- `knowledge/feedback/` — correction candidates (JSON). Gitignored.

### RUNTIME zone (always gitignored, inside vault)
- `cache/` — regenerable derived data: `index.sqlite` (FTS5), `vectors.json`
  (embeddings), QMD index.
- `logs/` — diagnostic output that may be rotated: `lint-YYYY-MM-DD.md`,
  `compile-last.log`, `session-start-last.txt`.
- `run/` — durable automation and recovery state: `state.json` (compile hashes,
  byte-prefix checkpoints, dedupe, heartbeats),
  `compile.pid` (fixed OS-backed compile lock file; contents are not a PID),
  `compile-journal/` (durable accepted plans and replay state),
  `compile-manifests/` (immutable per-source generation layouts),
  `queue/` (deferred LLM tasks), `queue-sequence` (durable FIFO counter),
  `queue-order.lock` (cross-process ordering lock), `queue-migration.json`
  (temporary resumable legacy migration journal), `project-state-claim.lock`
  (serialized slug allocation), `bootstrap-project/<slug>.lock` (per-project
  bootstrap exclusion), `backups/<timestamp>/`
  (repair manifests, staged files, and transaction journals), `state.json.lock`.
- `cache/cognee/` — optional semantic graph data (only if Cognee installed).

`cache/` is derived and can be rebuilt. `logs/` may be rotated after preserving
any diagnostics needed for an investigation. **Never delete `run/` wholesale.**
Queue tasks, compile journals/manifests, checkpoints, and repair transactions
carry durable ownership and recovery state. Inspect queues with
`memory_queue.py status|list`; let compile retry service journals/manifests via
`compile_memory.py`/`maybe_compile.py`; and use
`repair_installed_memory.py audit|apply|verify` with the recorded manifest for
repair transactions. Do not manually remove these artifacts to clear an error.

## Forbidden at vault root

These directories MUST NOT exist at the vault root (three-zone violation):

| Path | Reason |
|------|--------|
| `wiki/` | Legacy pre-three-zone. Consolidated into `knowledge/notes/`. |
| `memory/` | Legacy pre-three-zone. Consolidated into `knowledge/`. |
| `outputs/` | Legacy. No outputs zone in three-zone layout. |
| `state/` | Legacy runtime name. Use `run/` inside the vault. |
| `LLM-wiki-state/` | Legacy sibling layout. Runtime now lives inside the vault. |

The `tests/test_structure.py::test_forbidden_root_dirs_absent` test catches
any of these appearing.

## Changing this structure

1. **Describe the proposed change** in plain language (what, why, impact).
2. **Get explicit user sign-off.**
3. **Update this file** (`docs/STRUCTURE.md`) to reflect the new canonical
   layout.
4. **Update `tests/test_structure.py`** to enforce the new invariants.
5. **Update `AGENTS.md` + `CLAUDE.md`** (keep byte-identical).
6. **Update all scripts/docs that reference the changed paths.**
7. **Run `uv run pytest -q` + `uv run ruff check scripts/ tests/`** — must
   be green.

Never skip steps 1-2. Architectural improvisation is the root cause of the
most expensive bugs in this project's history.
