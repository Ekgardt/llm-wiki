# Regression test suite

Small pytest-based suite covering the critical scenarios surfaced by four rounds of colleague review plus the post-three-zone audit. **Not** an exhaustive unit test battery — each test protects against a specific regression pattern we've already seen in practice.

## Coverage

The suite currently has **281 tests across 27 files**. Highlights:

| Test file | Guards against |
|---|---|
| `test_slug.py` | Slug collision resolution + strict `_slug_owns_dir` ownership (state.md without `- Project root:` must NOT be claimed). Base slug sanitization, Cyrillic preservation, git owner-repo fallback, hash-suffix last resort, idempotency. |
| `test_compile_failure.py` | The `silent data loss` class bug where a failed LLM compile would still write `compiled_daily_hashes`. Monkey-patches `run_compile` to simulate failure, asserts hashes unchanged, exit=1, `last_compile_status=error`, `knowledge/log.md` untouched. |
| `test_compile_audit.py` | `parse_compile_audit` extracts/tolerates/merges LLM audit lines; snapshot fed to the LLM includes `«title»: summary`. |
| `test_audit_runtime_contracts.py` | Post-audit regression guards: module-level `import re` in compile_memory (contradiction path), `subprocess` import in query_memory, feedback stdin JSON contract, loop_detector breadcrumb regex matches the real writer format, flush_memory delegates to `maybe_compile.spawn_compile_if_idle` (PID lock). |
| `test_audit_fixes.py` | e2e compile with `MEMORY_LLM_PROVIDER=fake` end-to-end against a tmp vault; pinned `LLM_WIKI_ROOT` in settings.json hooks; no title-case duplicate notes after three-zone rename. |
| `test_context_noise.py` | Technical noise (`Trigger:`, `Transcript:`, `Project root:`, session-id UUIDs) stripped from SessionStart-injected context; useful signal preserved; ≤4KB cap. |
| `test_slugify.py` | Unicode-safe slugify for Cyrillic questions; punct-only / emoji-only inputs get deterministic hash suffix instead of colliding. |
| `test_session_end_skip.py` | SessionEnd hook skips vault cwd (delegates to project-level hook) and skips $HOME (not a project); writes tagged entry for normal non-vault cwd. |
| `test_capture_hooks.py` | Exit-0 invariants on capture hooks, MIN_PROMPT_CHARS / SIGNIFICANT_TOOLS filters, vault-internal skip, rate-limit window. |
| `test_feedback_capture.py` | Correction/preference/instruction/rejection detection + candidate save/promote. |
| `test_flush_classification.py` | FLUSH_MAJOR/MINOR/OK classification + tier gating of `maybe_trigger_compile`. |
| `test_graph_neighbors.py` | Triple-RRF fusion weights + graph-neighbor boost resolution. |
| `test_guardrails.py` | Correction/preference collection, project filter, dedup, formatting. |
| `test_maybe_compile.py` | PID liveness probe, lock write/read/clear, stale-lock steal, force-override, pending-work hash check. |
| `test_memory_queue.py` | Enqueue/list/mark_attempt/drain/permanently-failed/backoff/max_tasks/status/corrupt-json/age-filter. |
| `test_merge_claude_settings.py` | User hooks preserved + ours replaced, env set, permissions union, backup written. |
| `test_plugin_helpers.py` | Empty/malformed stdin → exit 0; valid payload writes daily-log/state/breadcrumb. |
| `test_readme_i18n.py` | All 3 READMEs exist, share live count (281), correct repo (`Ekgardt/llm-wiki`), mention `knowledge/`, mention `3.4.0`. |
| `test_search_ranking.py` | `_rrf_fuse_triple` weights verified; source_authority boost. |
| `test_wikilinks_tracked.py` | `git ls-files knowledge` filtered, broken-link detector + untracked-target reporting. |
| `test_archive_stale.py` | Type-aware archive thresholds (debugging=60d, decisions/concepts never). |

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
    the vault under gitignored `cache/logs/run/`, but tests must not
    mutate those; never touches the operator's real runtime)
  - `MEMORY_LLM_PROVIDER` → `fake` (no live LLM calls)
  - a skeleton `state.json` if it doesn't exist yet

No pre-configuration required. Output: 281 passed in ~3s.

All tests are self-contained and use `tmp_path` + state snapshots, so running them does not mutate the vault permanently. The compile-failure test briefly flips `state.json::last_compile_status` and restores it via fixture.

### If you want total isolation (CI-style)

`conftest.py` defaults to `$TEMP/llm-wiki-test-state/` (outside the vault)
regardless of any pre-set env value, to guarantee isolation. Production
runtime lives inside the vault under gitignored `cache/logs/run/`, but tests
must not mutate those. To opt INTO using an external state root (e.g. your
live runtime for a manual soak), set:

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
