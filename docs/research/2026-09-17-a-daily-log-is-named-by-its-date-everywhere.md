# A daily log is named by its date, everywhere it is read

Dated 2026-09-17. Finding M-A2 of the third audit (medium, reproduced). The research before
the fix.

Files: `scripts/memory_state.py`, `scripts/maybe_compile.py`, `scripts/fact_keys.py`,
`scripts/mcp_server.py`, `scripts/co_activation.py`, `scripts/agent_timeline.py`,
`scripts/compile_memory.py`, `scripts/lint_memory.py`,
`tests/test_a_readme_beside_the_daily_logs_is_not_pending_work.py`.

## What was found

- `maybe_compile._has_pending_work` globs `knowledge/daily/*.md`. The repository ships
  `knowledge/daily/README.md` in that directory, and that name is never a key of
  `compiled_daily_hashes`, so the answer is always "pending". Reproduced here: a daily
  directory holding only `README.md` answers `True`.
- Effect: every hook trigger and every nightly spawns a whole `compile_memory.py` process
  that reads every daily log to print "nothing to do", and the session-start
  `pending_work` flag is permanently true.
- `compile_memory._canonical_dailies`, `lint_memory._daily_logs` and
  `session_start_context._is_daily_log` each carry their own copy of the
  `YYYY-MM-DD.md` rule. The readers that were never given a copy have the defect:
  `maybe_compile._has_pending_work`, `fact_keys._daily_paths`,
  `mcp_server._daily_files` (the `vault_status` backlog counts the README as one
  uncompiled day), `co_activation.build` (the README's words enter the table under
  the date "README"), `agent_timeline._daily_timeline`.
- `loop_detector._recent_daily_files` already refuses a stem that is not a date.

## Practice on this date

- `Path.glob` matches on the pattern alone: "Glob the given relative pattern in the
  directory represented by this path, yielding all matching files (of any kind)", and `*`
  "Matches any number of non-separator characters, including zero"
  ([pathlib, Python 3 documentation](https://docs.python.org/3/library/pathlib.html)).
  `*.md` therefore cannot tell a log from a README; the name rule has to be stated by the
  reader.
- A rule that three modules copied and five forgot is one rule with one home: the shared
  definition removes the class, a fourth copy only removes this instance.

## The decision

- `memory_state` owns the rule: `DAILY_LOG_NAME` and `daily_logs(daily_dir)`, the sorted
  `YYYY-MM-DD.md` files of a directory, empty when the directory is absent.
- Every reader named above uses it; `compile_memory` and `lint_memory` drop their own copy
  of the pattern. `session_start_context` keeps its stricter calendar check (it belongs to
  the capture area) — it is not wrong.
- No contract, path or environment variable changes.
