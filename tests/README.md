# Regression test suite

Small pytest-based suite covering the critical scenarios surfaced by four rounds of colleague review plus the post-three-zone audit. **Not** an exhaustive unit test battery — each test protects against a specific regression pattern we've already seen in practice.

## Coverage

The suite has **2793 tests across 34 files on every platform**. Local Windows
verification reports **2749 passed, 44 skipped**; skip count varies with optional
Git Bash, PowerShell, and symlink availability. Highlights:

| Test file | Guards against |
|---|---|
| `test_bootstrap_project.py` | Per-project bootstrap exclusion, nested junction/reparse no-follow, minimum-access held Windows directory handles, directory and per-entry ABA identity checks, single collection under concurrency, complete atomic publication, and next-SessionStart visibility. |
| `test_slug.py` | Native-absolute ownership, atomic claims and alias backfill, bounded exact-path legacy reuse, exhaustive collision fallback, caller consistency, daily isolation, one-pass state rendering, and idempotency. |
| `test_compile_failure.py` | The `silent data loss` class bug where a failed LLM compile would still write `compiled_daily_hashes`. Monkey-patches `run_compile` to simulate failure, asserts hashes unchanged, exit=1, `last_compile_status=error`, `knowledge/log.md` untouched. |
| `test_compile_audit.py` | `parse_compile_audit` extracts/tolerates/merges LLM audit lines; snapshots use strict bounded UTF-8, unambiguous frontmatter status, and canonical body-only `«title»: summary`. |
| `test_compile_bounded_batches.py` | Noisy backlog filtering, bounded prompts, one-pass exact-bullet admission, exact project provenance, live/in-plan duplicate rejection, pinned-batch/index/effect-receipt completion, retired-store reactivation and transactional cold archive, truthful quotas, partial failures, concurrent appends, and idempotent SDK retries. |
| `test_memory_quality_contract.py` | Operational compile records require canonical project scope and durable framing; grounded Q&A filing pins fresh source identity, exact rendered provider bodies, strict evidence/JSON, side-effect ordering, and safe POSIX paths. |
| `test_audit_runtime_contracts.py` | Post-audit regression guards: module-level `import re` in compile_memory (contradiction path), `subprocess` import in query_memory, feedback stdin JSON contract, loop_detector breadcrumb regex matches the real writer format, flush_memory delegates to `maybe_compile.spawn_compile_if_idle` (OS-held compile lock). |
| `test_audit_fixes.py` | e2e compile with `MEMORY_LLM_PROVIDER=fake` end-to-end against a tmp vault; pinned `LLM_WIKI_ROOT` in settings.json hooks; no title-case duplicate notes after three-zone rename. |
| `test_context_noise.py` | In-memory normalization of timestamp blocks and legacy heading/bullet summaries, project-matching durable-summary/user-prompt priority, tool-breadcrumb exclusion, completed-record framing, bounded SessionStart input, saved-handoff priority, and technical-noise removal. |
| `test_project_state.py` | Canonical handoff ownership, exact template-section filtering, PID/time-only handoff exclusion, symlink/reparse path handling, cross-project isolation, and Git-fresh bootstrap precedence. |
| `test_slugify.py` | Unicode-safe slugify for Cyrillic questions; punct-only / emoji-only inputs get deterministic hash suffix instead of colliding. |
| `test_session_end_skip.py` | SessionEnd hook skips vault cwd (delegates to project-level hook) and skips $HOME (not a project); writes tagged entry for normal non-vault cwd. |
| `test_capture_hooks.py` | Exit-0 invariants, prompt retention, direct-file-mutation-only breadcrumbs, read/search/shell exclusion, bounded/redacted targets and provenance, vault-internal skip, and rate limiting. |
| `test_feedback_capture.py` | Correction/preference/instruction/rejection detection, shared bounded hostile-candidate loading with list-specific schema checks, and candidate save/promote. |
| `test_flush_classification.py` | FLUSH_MAJOR/MINOR/OK classification, confirmed-producer provenance versioning, exact durable completion, fallback truthfulness, and tier gating of `maybe_trigger_compile`. |
| `test_graph_neighbors.py` | Triple-RRF fusion weights + graph-neighbor boost resolution. |
| `test_guardrails.py` | Correction/preference collection, project filter, dedup, formatting. |
| `test_maybe_compile.py` | Fixed-file OS-lock probing, held-lock refusal, crash release, SDK deferral, and pending-work/index-state checks. |
| `test_memory_queue.py` | Durable FIFO sequence, canonical task suffix integrity, exactly-once durable provenance with explicit legacy fallback, the provenance recovery/apply trust boundary, exact flush-result acknowledgement, migration journal, SDK lease identity/digest, stale recovery, retries/backoff, bounded drain, and failure status. |
| `test_merge_claude_settings.py` | Direct `uv` argv materialization, idempotent owned-hook replacement, user-hook preservation, fail-closed malformed settings, env/permission merge, and backup writes. |
| `test_plugin_helpers.py` | Bounded/fail-closed stdin exits without writes on rejection; valid payloads write daily-log/state/breadcrumb records with bounded, redacted metadata. |
| `test_readme_i18n.py` | All 3 READMEs exist, share live count (2793), correct repo (`Ekgardt/llm-wiki`), mention `knowledge/`, mention `3.4.0`. |
| `test_integration_injection.py` | OpenCode prompt capture, mutation-only tool filtering, windowless Windows Python execution, bounded repeatable maintenance passes/continuations, bounded/abortable authenticated source-session lookup before provider work, project attribution, and SDK session cleanup. |
| `test_quality_guards.py` | Documentation counts/parity, windowless `pythonw` Task Scheduler registration, and console-free Windows maintenance child processes. |
| `test_search_ranking.py` | Original-line H1 recognition, canonical selector identity/BOM/visibility/wikilink-safe path diagnostics, exact index links, stale-path filtering, RRF weights, and authority ranking. |
| `test_wikilinks_tracked.py` | `git ls-files knowledge` filtered, broken-link detector + untracked-target reporting. |
| `test_archive_stale.py` | Type-aware archive thresholds (debugging=60d, decisions/concepts never). |
| `test_repair_installed_memory.py` | Schema-v4 actionable audit and report-only diagnostics, pre-staging ownership, external-output and direct-manifest preflight, sealed one-field approval, physical removal under fixed-order locks, exact rollback/commit purge recovery, producer-shaped feedback and completed-daily classification, strict verify, and recovery-only legacy-v3 preservation. |
| `test_scheduled_weekly.py` | One maintenance lease through final rebuild, truthful failures, and byte-exact preservation plus safe ID-only reporting of exhausted queue tasks. |

## Running

```bash
uv run pytest tests/
```

or

```bash
pip install pytest
pytest tests/
```

**Runs hermetically on a fresh clone** — `conftest.py` bootstraps:
  - `LLM_WIKI_ROOT` → the vault directory (so hook subprocesses operate)
  - `LLM_WIKI_STATE_ROOT` → `$TEMP/llm-wiki-test-state/` (stable temp dir
    OUTSIDE the vault for hermeticity — production runtime lives inside
    the vault under gitignored `cache/logs/run/`; `run/` contains durable
    automation/recovery state, so tests must not mutate it)
  - `MEMORY_LLM_PROVIDER` → `fake` (no live LLM calls)
  - a skeleton `state.json` if it doesn't exist yet

No pre-configuration required. Collection is platform-stable at 2793. Local
Windows output is 2749 passed, 44 skipped; optional Bash, PowerShell, and symlink
availability can change the skip count elsewhere.

All tests are self-contained and use `tmp_path` + state snapshots, so running them does not mutate the vault permanently. The compile-failure test briefly flips `state.json::last_compile_status` and restores it via fixture.

### If you want total isolation (CI-style)

`conftest.py` defaults to `$TEMP/llm-wiki-test-state/` (outside the vault)
regardless of any pre-set env value, to guarantee isolation. Production
runtime lives inside the vault under gitignored `cache/logs/run/`, and `run/`
contains durable automation/recovery state. Tests must not mutate it. To opt
INTO using an external state root (e.g. your live runtime for a manual soak), set:

```bash
LLM_WIKI_TEST_USE_EXTERNAL_STATE=1 uv run pytest tests/
```

This switches conftest to `setdefault` semantics so a pre-set `LLM_WIKI_STATE_ROOT` wins.

## Design principles

- **Every test maps to a named round/finding.** If a test fails, the commit history + docstring explains what class of bug it's protecting.
- **Snapshot + restore, not sandbox.** Using the real vault catches integration drift that pure unit-test mocks would miss. The trade-off is tests must be careful to restore state.
- **One scenario per test function.** Failures tell you precisely which invariant broke.
- **No network, no API calls.** The compile failure test monkey-patches the SDK call; the fake provider covers the e2e path; no real LLM invocation.

## What's intentionally NOT tested here

- End-to-end real-project flow (requires live Claude Code sessions; covered by manual soak tests).
- `/compact` re-firing of hooks (Claude Code internal; tested manually in Phase 4).
- QMD index behavior (dormant until tier crosses to HYBRID at 50+ pages).
- Installer orchestration (`install.ps1` / `install.sh`) — the merge *primitive* is tested via `test_merge_claude_settings.py`, but the install entrypoint scripts themselves are not executed by CI.

## If you add a test

Name it `test_<feature>_<invariant>.py`. Start the docstring with "Regression test:" and reference the round/finding that motivated it. Keep the test self-contained — no cross-file fixtures beyond `conftest.py`'s path setup.
