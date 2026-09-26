# A test that writes a daily log into the checkout is caught

Date: 2026-09-25. Found while fixing audit B-9; recurrence guard for the class
"a test leaves knowledge behind in the checkout".

## Facts (checked by running the tests one file at a time)

- `tests/test_capture_hooks.py` wrote prompt and tool lines ("this is a long
  enough prompt", session `s1`, slug `vault`) into the checkout's own
  `knowledge/daily/2026-09-24.md` and `2026-09-25.md`. The writers were
  `test_prompt_capture_skips_vault_internal_sessions` and
  `test_tool_capture_skips_vault_internal_sessions`: they pin the rule "a session
  inside the vault is not captured", which was removed for prompts on 2026-09-24
  and for tools on 2026-09-25 (B-1). They patch `ROOT` and `DAILY_DIR`, but the
  append goes through the transaction writer rooted at the checkout, so their
  "no file written" assertion looked in a directory nobody wrote to and passed
  while the lines landed in the checkout.
- `tests/conftest.py` fails a session that leaves anything in
  `knowledge/projects`, `knowledge/notes` or `knowledge/raw/sessions`, and
  deliberately does not watch `knowledge/daily`, because in the live checkout the
  real capture appends to today's log while the suite runs.
- Tests are run in a worktree or a clean detached checkout, not in the live
  vault. There the host's hooks write to the live vault (`LLM_WIKI_ROOT` points
  there), so nothing else appends to the checkout's `knowledge/daily`.

## Source

- pytest fixtures, https://docs.pytest.org/en/stable/how-to/fixtures.html
  (fetched 2026-09-25): an autouse fixture is requested by every test, and a
  session-scoped one "is destroyed at the end of the test session" — the point at
  which the existing guard compares what the checkout holds.

## Decision

- The two stale tests are removed; the behaviour they described no longer
  exists, and `tests/test_a_tool_call_in_the_vault_is_captured.py` pins the
  current one.
- The session guard also watches `knowledge/daily` file by file whenever the
  checkout under test is not the vault the host writes to (`LLM_WIKI_ROOT` as the
  process received it, before the suite pins its own). In the live checkout the
  old exemption stands.

## Files

- `tests/conftest.py`
- `tests/test_capture_hooks.py`
