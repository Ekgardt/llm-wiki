# A provider call fails closed: no isolation, no call; an unknown provider, none

Date: 2026-09-25. Audit items C-2 and C-3 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- C-2. `llm_client._claude_cli_flags` asks `claude --help` which flags exist
  and is `functools.lru_cache`d. A timeout or an OS error returned an empty set,
  and the empty set was cached for the life of the process — the MCP server lives
  for days. Every later call then went without `--no-session-persistence`,
  `--setting-sources ""`, `--tools ""` and `--system-prompt`: private vault text
  saved by the CLI as a session outside the vault, the operator's persona in the
  answer, ~13 000 extra input tokens a call
  (`docs/research/2026-09-14-a-memory-call-leaves-no-session.md`).
- C-3. `_candidate_order` treats an unknown `MEMORY_LLM_PROVIDER` like an empty
  one: the full automatic chain, cloud CLIs and OpenAI included. A typo such as
  `ollma` for a local-only operator sends private text to a cloud provider, and
  nothing says so.

## Source

- Python `functools.lru_cache`, https://docs.python.org/3/library/functools.html#functools.lru_cache
  (fetched 2026-09-25): "The cache keeps references to the arguments and return
  values". It says nothing about exceptions, so it was checked here: a cached
  function that raises was called three times and ran three times
  (Python 3.14.7). Raising on a failed probe makes the next call ask again.
- OWASP, "Fail securely", https://community.owasp.org/Fail_securely (fetched
  2026-09-25): "design your security mechanism so that a failure will follow the
  same execution path as disallowing the operation"

## Decision

- C-2: the probe raises when `claude --help` times out, cannot start, or exits
  non-zero; only a successful answer is cached. When the flags are not known the
  Claude backend answers nothing this time (the same as a missing binary), so the
  chain moves on or the caller waits; the next call asks again. A CLI that
  answers `--help` without some flag keeps today's behaviour for that flag.
- C-3: a non-empty `MEMORY_LLM_PROVIDER` that names no provider yields no
  candidate at all. `doctor`'s environment check names the value and the
  accepted ones.

## Files

- `scripts/llm_client.py`
- `scripts/doctor.py`
- `tests/test_a_provider_call_fails_closed.py`
- `CHANGELOG.md`
