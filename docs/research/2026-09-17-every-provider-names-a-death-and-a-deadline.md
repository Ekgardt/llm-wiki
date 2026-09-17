# Every provider names a death and a deadline, not only claude

Dated 2026-09-17. Finding M-A8 of the third audit (medium, confirmed by reading and by
tests). The research before the fix.

Files: `scripts/llm_client.py`,
`tests/test_every_provider_names_a_death_and_a_deadline.py`.

## What was found

- `ProviderExited` (2026-08-27) and `ProviderTimeout` (2026-08-26) were written so that a
  dead process and a passed deadline are not reported as `empty_response`. Only
  `_call_claude` raises them.
- `_codex_last_message` runs `codex exec` with `check=False`, ignores the return code and
  what the process printed, then reads the out-file. A crashed or logged-out codex leaves
  the file empty and is reported as `empty_response` — the exact collapse `ProviderExited`
  was written to stop.
- `subprocess.TimeoutExpired` from codex, and a socket timeout from the opencode, openai
  and ollama HTTP calls, reach the generic `except Exception` in `_completed_call` and are
  reported as `provider_error`. Only claude reports `provider_timeout`, the word the
  nightly and the benchmark read to tell "too slow" from "broken".

## Practice on this date

- `subprocess.run`: "If the timeout expires, the child process will be killed and waited
  for. The TimeoutExpired exception will be re-raised after the child process has
  terminated." and "If check is true, and the process exits with a non-zero exit code, a
  CalledProcessError exception will be raised."
  ([subprocess, Python 3 documentation](https://docs.python.org/3/library/subprocess.html)).
  The standard library keeps the two outcomes as two exception types; the caller that
  wants them kept apart maps types, never message texts.
- Since Python 3.10 `socket.timeout` is an alias of `TimeoutError`, and `urllib` wraps a
  connect-time timeout in `URLError` whose `reason` is that exception; both are reachable
  by type.

## The decision

- The mapping lives once, in `_completed_call`, for every backend: a
  `subprocess.TimeoutExpired`, a `TimeoutError`, or a `URLError` whose `reason` is a
  `TimeoutError` is a `provider_timeout`.
- `_codex_last_message` raises `ProviderExited` with the exit status and a bounded,
  redacted excerpt of what codex printed when the status is not zero — the same two facts
  claude reports.
- Left for the owner, named in the report: killing the whole process tree of a CLI wrapper
  on timeout, `codex.ps1` as a Windows candidate, and the cost of automatic fall-through
  after a timeout. No path, environment variable or contract changes.
