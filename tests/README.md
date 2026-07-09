# Regression test suite

Small pytest-based suite covering the critical scenarios surfaced by four rounds of colleague review. **Not** an exhaustive unit test battery — each test protects against a specific regression pattern we've already seen in practice.

## Coverage

| Test file | Guards against |
|---|---|
| `test_slug.py` | Round 2 / Round 5: slug collision resolution + strict `_slug_owns_dir` ownership (state.md without `- Project root:` must NOT be claimed). Base slug sanitization, Cyrillic preservation, git owner-repo fallback, hash-suffix last resort, idempotency. |
| `test_compile_failure.py` | Round 1 #C2: the `silent data loss` class bug where a failed LLM compile would still write `compiled_daily_hashes`. Monkey-patches `run_compile` to simulate failure, asserts hashes unchanged, exit=1, `last_compile_status=error`, `knowledge/log.md` untouched. |
| `test_context_noise.py` | Round 3 #I4: technical noise (`Trigger:`, `Transcript:`, `Project root:`, session-id UUIDs) stripped from SessionStart-injected context; useful signal (index header, wikilinks) preserved. |
| `test_slugify.py` | Round 3 #I5 + Round 5 #5: Unicode-safe slugify for Cyrillic questions; punct-only / emoji-only inputs get deterministic hash suffix instead of colliding on `"question"`. |
| `test_session_end_skip.py` | Round 2 + 3.5: SessionEnd hook skips vault cwd (delegates to project-level hook) and skips $HOME (not a project); writes tagged entry for normal non-vault cwd. |

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
  - `LLM_WIKI_STATE_ROOT` → `<vault>/../LLM-wiki-state/` (default sibling)
  - `state.json` with `{}` if it doesn't exist yet

No pre-configuration required. Output: 37 passed in ~2s.

All tests are self-contained and use `tmp_path` + state snapshots, so running them does not mutate the vault permanently. The compile-failure test briefly flips `state.json::last_compile_status` and restores it via fixture.

### If you want total isolation (CI-style)

Point `LLM_WIKI_STATE_ROOT` at a scratch dir before invoking:

```bash
LLM_WIKI_STATE_ROOT=/tmp/llm-wiki-ci-state uv run pytest tests/
```

The conftest honors a pre-set `LLM_WIKI_STATE_ROOT` (uses `setdefault`, not overwrite).

## Design principles

- **Every test maps to a named round/finding.** If a test fails, the commit history + docstring explains what class of bug it's protecting.
- **Snapshot + restore, not sandbox.** Using the real vault catches integration drift that pure unit-test mocks would miss. The trade-off is tests must be careful to restore state.
- **One scenario per test function.** Failures tell you precisely which invariant broke.
- **No network, no API calls.** The compile failure test monkey-patches the SDK call; no real LLM invocation.

## What's intentionally NOT tested here

- End-to-end real-project flow (requires live Claude Code sessions; covered by manual soak tests).
- `/compact` re-firing of hooks (Claude Code internal; tested manually in Phase 4).
- QMD index behavior (dormant until tier crosses to HYBRID at 50+ pages).
- `settings.json` hook registration (Claude Code's responsibility; re-setup guide validates manually).

## If you add a test

Name it `test_<feature>_<invariant>.py`. Start the docstring with "Regression test:" and reference the round/finding that motivated it. Keep the test self-contained — no cross-file fixtures beyond `conftest.py`'s path setup.
