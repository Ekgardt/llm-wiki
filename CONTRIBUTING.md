# Contributing to LLM Wiki

This is a personal memory system that grew into something others might find useful. Contributions are welcome but the bar is "does this survive contact with my actual workflow".

## What kind of contributions are wanted

**Yes please:**
- Bug fixes for things that break the silent-capture / compile pipeline
- New LLM backend adapters (Azure OpenAI, Anthropic direct, Bedrock, local llama.cpp)
- New tool integrations (Cursor, Cline, Continue, Aider, your-favorite-agent)
- Test coverage improvements — especially for concurrency / failure modes
- Documentation improvements (especially macOS/Linux install paths)
- Lint check improvements that catch real defects without false positives

**Probably no:**
- Features that require sending vault data to a third-party service
- Cosmetic refactors with no behaviour change
- Anything that adds a hard dependency on a specific LLM provider
- "Modernization" that breaks the markdown-first / git-first / OKF-conformant principles

## How to develop

```bash
git clone git@github.com:Ekgardt/llm-wiki.git
cd llm-wiki
uv sync --locked                                      # install the development group
uv run --locked --no-sync pytest -q                   # full regression suite
uv run --locked --no-sync pytest tests/test_xxx.py -v # run a specific test file
uv run --locked --no-sync python scripts/repair_installed_memory.py --check --json
```

The installed-vault repair surface is check-only until the Reliability V3 runtime
writers and canonical ownership protocol are complete. Tests must use explicit temporary
`--root` and `--state-root` paths. Do not point repair verification at this public source
checkout or an operator's installed vault, and do not weaken the current
`reliability_v3_runtime_activation_incomplete` fail-closed gate.

Pre-commit hooks (recommended, not mandatory):

```bash
uv run --locked --no-sync pre-commit install --hook-type pre-commit --hook-type pre-push
```

This runs structural lint + gitleaks on every commit, and the LLM-judged
contradiction check on every push. The structural lint hook scopes to
`knowledge/**/*.md` files only — scripts and tests are covered by ruff.

## Architectural principles (don't break these)

The canonical structure reference is [`docs/STRUCTURE.md`](docs/STRUCTURE.md)
and the full agent contract is in `AGENTS.md` (byte-identical to `CLAUDE.md`).
The principles below summarize the non-negotiable invariants.

1. **Markdown-first.** The vault is plain `.md` files. No databases, no proprietary formats. If a feature requires a database to read your memory, it doesn't belong here.

2. **LLM-agnostic.** No hard dependency on any provider. The `llm_client.py` abstraction with 5 backends is the canonical way to call an LLM. New backends go there, not in feature code.

3. **OKF v0.1 conformant.** Every knowledge page has `type:` frontmatter. New page types extend `migrate_to_okf.py`'s `TYPE_INFERENCE` table.

4. **Fail silently, never break the user's session.** Every hook script must `exit 0` on any error. The worst case is "memory didn't get captured this round", never "the user's agent crashed".

5. **Detached where possible.** Long-running work (compile, lint contradictions) runs in background processes via `spawn_detached`. Never block a session-start hook on LLM work.

6. **Verify before write.** The compile pipeline's VERIFY-BEFORE-WRITE rule is non-negotiable. If the LLM claims a citation, Python verifies the literal substring exists in the source. Hallucinated evidence gets dropped.

7. **Three-zone layout.** CODE + KNOWLEDGE tracked in git; RUNTIME
   (`cache/logs/run/`, incl. `cache/cognee/`) inside the vault but gitignored. Never put
   runtime outside the vault. Enforced by `tests/test_structure.py`.

8. **Reliable local mutation.** Markdown remains authoritative. Operational SQLite
   uses rollback-journal and `synchronous=FULL` on a local filesystem; no WAL on the
   current runtime. Automatic writers use the transaction API, queue handlers must
   tolerate at-least-once delivery, and `run/` deletion must honor doctor blockers.

## Test discipline

- Every new script gets at least one test file
- Every new code path gets at least one happy-path test and one failure-path test
- Concurrency-sensitive code (`maybe_compile.py`, `memory_queue.py`) needs explicit race-condition tests
- Tests must be hermetic — no dependency on a real LLM, real network, or pre-existing state beyond what conftest.py bootstraps
- **Minimum coverage**: all scripts with ranking/scoring/archival logic MUST have dedicated tests. This includes: `search_memory.py`, `graph_neighbors.py`, `feedback_capture.py`, `archive_stale.py`, `build_guardrails.py`
- The full regression suite is the release gate; see `tests/` for patterns.

## Test commands

```bash
# Fast subset (pre-commit)
uv run --locked --no-sync pytest -q

# Full suite
uv run --locked --no-sync pytest -v

# Specific module
uv run --locked --no-sync pytest tests/test_search_ranking.py -v

# With coverage
uv run --locked --no-sync pytest --cov=scripts --cov-report=term-missing
```

### real-Pyright CI

The cross-platform navigation job explicitly installs pinned Pyright before running
protocol, process-tree, security, session, facade, and benchmark checks. Tests and
queries must never download it themselves:

```bash
uv run --locked --no-sync python scripts/install_pyright.py --state-root "$LLM_WIKI_STATE_ROOT"
uv run --locked --no-sync python benchmark/run_code_navigation.py --fixture --correctness-only --require-gates
```

Linux Python 3.10 additionally runs the fixed 100 KLOC qualification gate with
`--qualification --require-gates`. Navigation changes must keep Windows, Linux, and
macOS process-ownership claims platform-qualified.

## Release checklist (mandatory — do not skip)

Before tagging a release or updating public marketing numbers:

1. **English README first** — `README.md` is the source of truth.
2. **Sync i18n the same day** — update `README.ru.md` and `README.zh-CN.md` so they match:
    - version string (e.g. v3.4.0)
   - full-regression-suite wording and current test commands
   - install URLs (`Ekgardt/llm-wiki`)
    - architecture (three-zone / `knowledge/`)
    - reliable operations commands and contracts
   - benchmark headline numbers (MRR, latency) if changed
3. **Run the i18n guard test** — `uv run --locked --no-sync pytest tests/test_readme_i18n.py -q` must pass.
4. **CHANGELOG** — newest version at top (Keep a Changelog).
5. **pyproject.toml version** + `uv.lock` package version must match the tag.
6. If benchmark numbers changed, update `docs/ARCHITECTURE.md` search section + `benchmark/report.md` in the same change.
7. Only then: tag, push, GitHub Release.

**Never ship a release with EN updated and RU/ZH left stale.** That is a release blocker.

## Commit message style

We follow a loose conventional-commits style:

```
feat(memory): description
fix(compile): description
docs(readme): description
chore(deps): description
```

The scope is usually the subsystem (`memory`, `compile`, `flush`, `lint`, `plugin`, etc.). Keep the subject line under 72 chars. Body explains why, not what — `git diff` already shows what.

## Pull request flow

1. Fork → branch → commit → push → PR
2. PR description must include:
   - What problem this solves
   - How it was tested (commands you ran + their output)
   - Any architectural trade-offs you made
3. CI must pass (pytest + structural lint + gitleaks)
4. Squash-merge on approval

## Release model

Semantic versioning via `pyproject.toml` `version` + git tags. Releases are
documented in `CHANGELOG.md` (Keep a Changelog format). The `main` branch
is always releasable; tags mark public release points.

## Code of conduct

Be excellent to each other. This is a personal tool shared with the world, not a corporate project. Patience and good faith assumed on all sides.
