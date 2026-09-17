# Every compile takes the compile lock

Dated 2026-09-17. Finding M-A3 of the third audit (medium, confirmed by reading and by a
test). The research before the fix.

Files: `scripts/compile_memory.py`,
`tests/test_a_compile_asked_through_mcp_takes_the_compile_lock.py`.

## What was found

- `compile_memory.main` is the only entry that claims `run/compile.pid`, stamps
  `last_compile_status = "running"` and runs the pass under
  `call_ceiling(COMPILE_PROVIDER_CEILING_S)`.
- The MCP `compile` tool calls `compile_memory.run_pending_compile`, which goes straight to
  `_run`. So a compile asked through MCP:
  - runs beside a hook-spawned compile. The module's own rule is "two compiles writing one
    daily log is worse than one late compile". The transaction layer refuses the loser's
    write, but both have already paid for the model calls;
  - drafts under the 90 s default call ceiling — the `draft:claude:provider_timeout` the
    300 s ceiling was measured and added to stop (2026-08-28);
  - finishes with `_mark_finished`, overwriting the `last_compile_status` of the spawned
    compile that is still running, which the nightly reads to tell "died" from "done".
- The class: an entry point that reaches `_run` without the three guards. There are two
  entries (`main`, `run_pending_compile`); after the fix there is one guarded path and both
  go through it.

## Practice on this date

- "In concurrent programming, concurrent accesses to shared resources can lead to
  unexpected or erroneous behavior. Thus, the parts of the program where the shared
  resource is accessed need to be protected in ways that avoid the concurrent access."
  ([Critical section, Wikipedia](https://en.wikipedia.org/wiki/Critical_section)). A guard
  that one of two entrances skips does not protect the section.
- The repository's own earlier decision keeps the lock's life equal to its process's life
  and refuses on doubt
  (`docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md`);
  the fix reuses that mechanism unchanged rather than adding a second lock.

## The decision

- One function, `_compile_under_lock(args, deadline, cancelled, owner)`, claims the lock,
  records a refusal without touching the holder's status, stamps the start, applies the
  call ceiling, runs, records an escaping error, and releases the lock. `main` and
  `run_pending_compile` both call it.
- A refused in-process compile returns the same code `main` returns (1); the MCP tool
  already reports a non-zero code as `failed`, and the reason is on stderr and in
  `last_compile_refused_reason`.
- The caller's MCP deadline and cancellation still bound the run; the ceiling bounds one
  provider call inside it. No contract, path or environment variable changes.
