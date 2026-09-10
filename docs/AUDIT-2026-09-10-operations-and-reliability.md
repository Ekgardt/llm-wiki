# Audit 2026-09-10: operations and reliability

Scope: `scripts/doctor.py`, `scheduled_nightly.py`, `maybe_compile.py`,
`memory_queue.py`, `markdown_transaction.py`, `reliable_memory.py`,
`operational_ownership.py`, `mcp_server.py`, `mcp_http.py`, `install*.py`,
`install.sh`, `install.ps1`, `sync_memory.py`, `session_start*.py`,
`flush_memory.py`, `capture*.py`, `lsp_*.py`, `pyright_*.py`,
`repository_index.py`, `evidence_reader_cache.py`, `code_graph.py`,
`integrations/`, `.github/workflows/tests.yml`, and their tests.
Audited at commit `6fcab30` (branch `work-rounds` fast-forwarded into the
audit worktree). Read-only: no product code or test was changed.

Method. The whole scope (41 files, about 89 000 lines) went through one
measurement pass: lizard at `-C 5`, the owner's `ccn_gate.py` for nesting and
ternaries, and a pattern grep for broad excepts, sleeps, joins, globals and PID
probes. The nightly, the compile trigger, the ownership fence, the state lock,
the installers, the hook manifests, the CI matrix and four test files were read
in full. `doctor.py`, `memory_queue.py`, `markdown_transaction.py`,
`mcp_server.py`, `lsp_process.py` and `install_control.py` got a pattern-driven
pass: each grep hit was read in context, not the whole file. One finding was
reproduced by running the code against a temporary state root.

How to read a finding. `Verified: read` means the defect follows from the code
as written; `Verified: ran` means it was reproduced; `Inference` means the
mechanism is real but the harmful outcome was not reproduced; `Question` means
the audit could not settle it. Rules are the owner's five laws (L1 graph
first, L2 dated research, L3 honesty, L4 quality and reliability, L5
complexity) and the repository contracts in `CLAUDE.md` (C: fail-closed,
bounded, one canonical fence, `run/` deletion contract, Markdown authority).

---

## High

### OPS-01 — The compile lock expires while the compile is still running, and every reader then disagrees

- Status: fixed 2026-09-10 (`docs/research/2026-09-10-a-lock-lives-as-long-as-its-process-not-thirty-minutes.md`).

- Rule: L4, C (bounded, fail-closed); L3 (the reason reported is false).
- Evidence: `scripts/maybe_compile.py:62` (`MAX_COMPILE_DURATION_S = 30*60`),
  `:206-232` (`_is_compile_running` returns "stale" by age alone),
  `:183-189` (`_clear_lock` refuses to unlink when the PID is alive),
  `:293-297` (returns `"skipped: lock race lost"`), `:348` (exit 0 on any
  reason containing `"skipped"`); `scripts/scheduled_nightly.py:377`
  (`COMPILE_WAIT_SECONDS = 1800.0`, the same 30 minutes), `:389-396`,
  `:483-494`; `scripts/compile_memory.py:3935-3963` (the lock is written once at
  spawn and never refreshed; it is cleared only on normal finish).
- What is wrong: after 30 minutes a live compile is reported as not running.
  `status()` says `compile_running: False`; the nightly's `_wait_compile_finished`
  therefore returns immediately at the very moment the wait bound would have
  fired, `_compile_failed_this_pass` sees no new stamp and counts no failure,
  and lint, `repair_backlinks --apply`, the FTS rebuild and the generation
  refresh all run while the compile is still writing pages. A new trigger
  meanwhile gets `"skipped: lock race lost"`, which names a race that did not
  happen, and exits 0. The docstring's guarantee "at most one compile runs at
  any time" holds only because `_clear_lock` quietly refuses; nothing tells the
  caller why. Compile of one daily log measured 6.5 min through the Claude CLI
  (issue #21); a backlog of several logs crosses 30 minutes.
- Verified: ran. A lock with the current PID and a 31-minute-old timestamp in a
  temporary state root gave `status()` → `stale lock (age 1860s > 1800s)`,
  `_clear_lock()` → `False` with the file still present,
  `spawn_compile_if_idle()` → `(False, 'skipped: lock race lost')`, exit code 0.
  `tests/test_maybe_compile.py:165-174` locks this contradiction in (alive PID
  plus old timestamp is asserted "stale").
- Fix direction: one definition of "running" (alive PID wins over age, or the
  compile heartbeats the lock), one bound shared by the trigger and the nightly,
  and a reason string that names what was actually seen.

### OPS-02 — The production nightly never takes the canonical fence; the lease plumbing it carries is dead

- Rule: C (one canonical fenced admission registry; `run/` deletion contract
  protects live maintenance owners); L4; owner's "no shortcuts, use the
  mechanism".
- Evidence: `scripts/scheduled_nightly.py:620-633` (`main()` calls
  `run_nightly(ownership=None)`), `:505-510` (`_require_nightly_owner(None)`
  is a no-op), `:604-617` (the only fence is the legacy `run/maintenance.lock`
  marker); `scripts/scheduled_weekly.py:180-186` (same, `ownership=None`);
  `scripts/operational_ownership.py:1363-1385` (`acquire_scheduled_owner`) has
  no caller outside tests (graph `trace_path` on `run_nightly`: callers are
  `scheduled_nightly.main`, `scheduled_weekly`, one test); grep for
  `acquire_scheduled_owner` in `scripts/` finds only its definition.
  `scripts/scheduled_nightly.py:144-149, 523-530, 58-68`: ownership is passed
  on only if `inspect.signature` finds an `ownership` parameter;
  `scripts/maintenance_helpers.py:144` (`run_step(cmd, log_fn, label, timeout)`)
  and `scripts/doctor.py:8066-8074` (`run_generation_maintenance`) accept none,
  so even a lease handed in by the weekly is silently dropped.
  `scripts/doctor.py:1172-1174, 1546` count live owners from
  `maintenance_owners` rows only.
- What is wrong: a running nightly holds no row in `maintenance_owners`, so the
  doctor's "live maintenance owner" count, and with it the `run/` deletion
  contract, cannot see it. The canonical roles `nightly` and `weekly` exist in
  the registry and are tested, but production never uses them; the
  `inspect.signature` feature-detection is a crutch that hides the fact that no
  step ever receives a fence.
- Verified: read, plus graph trace.
- Fix direction: either `main()` acquires the scheduled owner and the steps that
  need it take it explicitly, or the dead ownership parameters and the marker
  fallback are removed and the contract text is corrected.

### OPS-03 — A lost owner fence is invisible to the body until it exits, and success is recorded before the loss surfaces

- Rule: C (fail-closed), L4.
- Evidence: `scripts/operational_ownership.py:1414-1444` (`heartbeat_owner`:
  the thread appends the failure and returns; nothing signals the body; the
  error is raised only in `finally`, and only if the body did not raise);
  `:1392-1393, 1443` (`thread.join(timeout=heartbeat_seconds*2)`, 80 s for
  long-lease roles); `scripts/scheduled_nightly.py:554-558` (the `finally` of
  `_run_nightly_body` records `success` in state) versus `:561-565`
  (`heartbeat_owner` wraps the body, so its exit error comes after the record).
  Compare `scripts/doctor.py:7004-7039`, where the doctor's own heartbeat sets
  `_lost` and `check()` raises on the next step.
- What is wrong: two heartbeat implementations with opposite policies. Under
  `heartbeat_owner` a fence lost at minute 2 lets the body run unfenced for the
  rest of the pass, the state file says `success`, and the process then exits
  with `owner_fence_lost` or `owner_heartbeat_stop_timeout`. The 80 s join is
  reachable only when the heartbeat thread is stuck inside SQLite (busy bound
  10 s, `reliable_memory.py:29`), so in practice the join is shorter, but the
  bound is a derived number, not a stated one.
- Verified: read. Reachable today only through the weekly path (OPS-02).
- Fix direction: expose the loss to the body (an event checked between steps,
  as the doctor does) and record the terminal state after the fence has been
  released, not before.

### OPS-04 — `tests/slow_machine.py` says no test carries a literal wait; 395 do

- Status: fixed 2026-09-10 (`docs/research/2026-09-10-every-hang-bound-in-the-tests-comes-from-one-place.md`); the 36 negative bounds stay literal by design.

- Rule: L3; L4 (Windows-runner flakes were the motivation, commit `9c88bbf`).
- Evidence: `tests/slow_machine.py:10-11` ("Every wait comes from here; no
  test carries a literal"); grep `timeout=[0-9]|time.sleep([0-9]` over
  `tests/*.py`: 395 hits, among them `tests/test_lsp_protocol.py` 60 (for
  example `:425, :441, :500, :603, :738, :765`, all `future.result(timeout=1)`
  or `timeout=2`), `test_lsp_process.py` 52, `test_integration_injection.py`
  35, `test_sync_memory.py` 12 (`:679 time.sleep(0.7)`, `:710 time.sleep(1)`,
  `:706 timeout=0.2`), `test_memory_queue_cli.py` 11, `test_doctor.py` 6.
- What is wrong: `future.result(timeout=1)` is exactly the hang bound the module
  was written to replace; on the hosted Windows runner the same module records
  a 0.1 s operation measuring 12.89 s. Some hits are deadline arguments to the
  code under test rather than waits, but the statement in the docstring is
  false as written and the migration is unfinished.
- Verified: read (the grep output and the six cited lines).
- Fix direction: finish the migration for the waits, and reword the docstring to
  say what is actually true.

---

## Medium

### OPS-05 — The Windows installer reports Claude settings as owned after the ownership transaction failed

- Status: fixed 2026-09-10 (`docs/research/2026-09-10-the-installer-says-owned-only-after-the-transaction-committed.md`); verified at source level here (no pwsh), by the Windows installer job in CI.

- Rule: L3; installer fail-closed claim.
- Evidence: `install.ps1:387-409` (transaction failure is caught, a warning is
  printed, `$schedulerWarning = $true`, the script continues), `:488-490`
  (unconditional `Ok "Claude settings owned by the install transaction"`),
  `:511-515` (`Claude Code: active automatic`), `:551-555, :583` ("installed
  with warnings", exit 1 only at the very end). Compare `install.sh:458-466`,
  which calls `fail` immediately at the same step.
- What is wrong: on Windows the same failure that stops the Linux installer
  lets agent wiring, runtime sync and model download proceed, then prints a
  success line for settings that the failed transaction was supposed to write.
- Verified: read.
- Fix direction: treat the ownership transaction as the gate on both
  platforms, and never print "owned" from a branch that did not check the
  transaction result.

### OPS-06 — A nightly step that times out leaves its process tree running

- Rule: C (bounded, no daemon), L4.
- Evidence: `scripts/maintenance_helpers.py:53-63, 144-158` (`subprocess.run(
  timeout=...)`; on `TimeoutExpired` Python kills the direct child only);
  steps that spawn children: `memory_queue.py work` (processor subprocesses,
  `memory_queue.py:14429-14453` kills its own tree only when it is the one
  timing out), `repository_index.py refresh-all`, `install_models.py`.
  `scripts/sync_memory.py` already has `_run_process_tree` (process-group kill,
  taskkill `/T` on Windows) and the nightly runner does not use it.
- What is wrong: a step killed at its bound continues to write to the vault
  through its orphans while the next step runs; the log says "TIMEOUT —
  skipping, continuing" as if the work had stopped.
- Verified: read. Inference for the orphan outcome (not reproduced).
- Fix direction: run every step through the existing tree-killing runner.

### OPS-07 — Three stale-lock stealers unlink without checking what they unlink

- Rule: L4 (race window), C (at most one writer).
- Evidence: `scripts/maybe_compile.py:177-182, 190-194` (read, decide stale,
  `LOCK_FILE.unlink()`); `scripts/memory_state.py:266-273` (`_await_lock_turn`:
  age check, owner check, `_unlink_quietly()`); `scripts/scheduled_nightly.py:
  594-601` (`_steal_marker`: `unlink` then `_write_marker`). None re-checks
  identity between the decision and the unlink; `_release_state_lock`
  (`memory_state.py:294-308`) guards the release but the steal has no such
  guard.
- What is wrong: two processes that both decide "stale" can each unlink the
  other's freshly claimed lock and both proceed; for `state.json.lock` that is
  two read-modify-write writers on the state file. The window is small and
  needs a lock older than the stale bound, but the guarantee is stated as
  absolute.
- Verified: read. Inference for the double-writer outcome.
- Fix direction: steal by renaming the stale file to a unique name and
  verifying its content, or by an identity compare (inode or content) before
  the unlink, the way `_remove_exact_marker` already does.

### OPS-08 — PID liveness is implemented five times with three different failure policies

- Rule: L4 (maintainability, one truth); C (doubt refuses).
- Evidence: `scripts/memory_state.py:93-130` (`OSError` → dead);
  `scripts/doctor.py:6228-6237` (`OSError` → dead) beside `:6185-6204`
  (`PermissionError` → unknown, in the same file); `scripts/markdown_transaction.py:
  4404-4432` (copy of memory_state); `scripts/lsp_process_tree.py:746, 812`;
  `scripts/memory_queue.py:12487-12493` (any exception → alive, "inability to
  prove death is treated as live"); `scripts/operational_ownership.py:206-389`
  (process start identity, PID-reuse safe, returns `unknown` on doubt).
- What is wrong: the legacy locks (`compile.pid`, `maintenance.lock`,
  `state.json.lock`, doctor's `_live_owner` at `doctor.py:732-741`) trust a bare
  `os.kill(pid, 0)`: a reused PID reads as alive and blocks forever; a process
  owned by another user (EPERM) reads as dead and its lock is stolen. The
  registry that solves both exists in the same tree and is not used by them.
- Verified: read. Question Q2 below covers the Windows scheduler case.
- Fix direction: one liveness module with the start-identity probe, and one
  stated policy for "cannot tell".

### OPS-09 — Several failures are reported as an exception class name and nothing else

- Rule: L3, L4 ("does every failing step say why").
- Evidence: `scripts/scheduled_nightly.py:450-451` (`health report skipped:
  {type(exc).__name__}`); `scripts/doctor.py:8542-8545` (`Index repair failed:
  {type}`), `:8670-8673`, `:8694-8697` (`Repair failed: {type}`);
  `scripts/self_update.py:158-159` (`_outcome("error", type(error).__name__)`,
  no command named); `tests/test_scheduled_nightly.py:301-315` asserts the
  type-name-only line. Subprocess steps do say why (`maintenance_helpers.py:
  122-131`: stderr head plus artifact path), so the gap is in the in-process
  steps.
- What is wrong: `RuntimeError` or `FileNotFoundError` alone does not let the
  owner act; the nightly log is the one place the reason had to land.
  Additionally `self_update._merged_update` (`self_update.py:179-187`) runs the
  fast-forward merge and then `uv sync`; if `uv` is not on the scheduler's
  PATH the `FileNotFoundError` turns the whole outcome into `error` after the
  checkout has already moved (inference).
- Verified: read.
- Fix direction: log the redacted message beside the class, and in the
  updater report the merge result separately from the dependency sync.

### OPS-10 — The same bound is declared twice and the outer copy can pre-empt the inner one

- Rule: L4 (contradictory or duplicated constants).
- Evidence: `scripts/scheduled_nightly.py:55` (`REPOSITORY_REFRESH_BUDGET_SECONDS
  = 15*60`, used as the subprocess kill timeout at `:297-301`) and
  `scripts/repository_index.py:913` (`REFRESH_ALL_BUDGET_SECONDS = 15*60`, the
  internal deadline that defers a repository "never half-built"); the inner
  clock starts after interpreter start-up, so the outer SIGKILL lands first.
  `scheduled_nightly.py:377` (`COMPILE_WAIT_SECONDS = 1800`) equals
  `maybe_compile.py:62` (`MAX_COMPILE_DURATION_S = 1800`), which is what makes
  OPS-01 unobservable from the nightly.
- What is wrong: the graceful deferral path cannot run because the parent
  kills the child at the same second; and the two 1800 s values are related by
  meaning but not by code.
- Verified: read. Question Q3 covers whether a kill mid-build is atomic.
- Fix direction: the parent passes its budget to the child (`--budget-seconds`
  exists) and allows a margin above it; one constant for the compile bound.

### OPS-11 — `maybe_compile.main` decides its exit code by a substring of the reason text

- Status: fixed 2026-09-10 with OPS-01.

- Rule: L4; owner's rule "no substrings in error text as control flow".
- Evidence: `scripts/maybe_compile.py:348` (`return 0 if spawned or "skipped"
  in reason else 1`); `:293-297` (a live lock reports `"skipped: lock race
  lost"`).
- What is wrong: the outcome is a string, so a refusal that should be visible
  ("could not claim lock", a live lock past its age) exits 0 because the word
  "skipped" is in it (reproduced in OPS-01).
- Verified: ran.
- Fix direction: return a typed outcome and map it to exit codes.

### OPS-12 — The shipped Claude Code allowlist grants write and execute through "read-only" helpers

- Rule: L4 (reliability, security of the integration).
- Evidence: `integrations/claude-code/settings.json:12-33`: `Bash(sed *)`
  (`sed -i` rewrites files), `Bash(xargs *)` (runs any command),
  `Bash(find *)` (`-exec`, `-delete`), `Bash(uv run --directory *)` (runs any
  Python in any directory); the deny list (`:5-11`) covers only reading secret
  files.
- What is wrong: the entries read as a read-only toolkit and are not one; an
  agent can delete or overwrite vault files without a permission prompt.
- Verified: read.
- Fix direction: narrow each entry to its read-only form (`sed -n`, `find`
  without action flags, the exact `uv run` invocations needed).

### OPS-13 — The MCP warm-up hides its own failure from the user and the doctor

- Rule: L3, L4 (silent fallback).
- Evidence: `scripts/mcp_server.py:5639-5643` (`contextlib.suppress(
  BaseException)` around `_warm_reranker` and each warm-up pass), `:5679-5681`
  (`suppress(Exception)` on the warm thread); nothing is logged or recorded.
  `scripts/mcp_http.py:389-393` (`suppress(BaseException)` on shutdown).
- What is wrong: a failed warm means the first answers fall back to the lexical
  leg, which is the symptom the owner measured on 2026-08-24/26; the suppress
  also swallows `MemoryError` and `KeyboardInterrupt` on that thread. The
  docstring calls the failure "never fatal", which is true, and "an unwarmed
  path serves exactly as it does today", which hides that the path is degraded.
- Verified: read.
- Fix direction: catch `Exception`, record the failure where the doctor or the
  health resource can show it.

### OPS-14 — CI never exercises the macOS installer or a clean install on Windows and macOS

- Rule: L4; `CLAUDE.md` names launchd, Task Scheduler and systemd as supported.
- Evidence: `.github/workflows/tests.yml:248-279` (installer job: `ubuntu-24.04`
  and `windows-2025` only), `:166-246` (`clean-production`, `clean-hybrid`,
  `clean-code-graph`: `ubuntu-24.04` only), `:339-343` (qualification gate
  `ubuntu-24.04` only). Shards, timeouts (20/40 min) and the `all-green` gate
  are sound.
- What is wrong: the LaunchAgent path (`install_control.py:516-546, 1207-1312`)
  and `install_smoke.py` on Windows have no CI evidence; a regression there
  merges green.
- Verified: read.
- Fix direction: add the macOS runner to the installer job and the Windows
  runner to `clean-production`.

### OPS-15 — Rule 5 is not met by 110 functions in scope

- Rule: L5.
- Evidence: measured with lizard `-C 5` and `ccn_gate.py` on 2026-09-10:
  functions over CCN 5 — `lsp_security.py` 40, `lsp_protocol.py` 30,
  `pyright_profile.py` 22, `sync_memory.py` 6, `maybe_compile.py` 4
  (`_clear_lock` 19, `spawn_compile_if_idle` 15, `_is_compile_running` 8,
  `_read_lock` 7), `lsp_positions.py` 4 (`file_uri_to_path` 40),
  `install_smoke.py` 3, `capture_operation.py` 1 (`claim_operation.mutate`
  13); nesting over 2: 55 blocks (`lsp_security` 17, `lsp_protocol` 17,
  `pyright_profile` 10, `sync_memory` 8, `capture_operation` 2,
  `maybe_compile` 1); ternaries with hidden branching: 9 (`sync_memory` 6,
  `maybe_compile` 2, `lsp_security` 1). The other 33 files in scope measure
  clean. Outside scope but seen in the merge gate output: `benchmark/*.py`
  carries functions at CCN 124, 85, 84, 75, 71.
- Verified: ran.
- Fix direction: the gate already refuses new edits; schedule the legacy
  files, starting with the four lock functions in `maybe_compile.py`, which
  are also the site of OPS-01.

---

## Low

### OPS-16 — Stale docstrings state behaviour the code does not have

- Status: fixed 2026-09-10 (the three docstrings rewritten from the code).

- Rule: L3.
- Evidence: `scripts/scheduled_nightly.py:1-10` ("runs at 03:00 via Windows
  Task Scheduler", a three-step list; the pass has fourteen steps and three
  schedulers); `scripts/maybe_compile.py:19` ("Self-heals from stale locks",
  false past 30 minutes with a live PID); `tests/test_maybe_compile.py:3-9`
  claims "Multiple concurrent spawn attempts only one succeeds" (no such test
  exists in the file) and "--force ignores existing lock" (the code refuses a
  live lock, `maybe_compile.py:264-270`).
- Verified: read.
- Fix direction: rewrite the three docstrings from the code.

### OPS-17 — Two tests assert the defect rather than the intent

- Status: the maybe_compile assertion retargeted 2026-09-10 with OPS-01; the nightly health-line assertion and the end-to-end nightly test remain open.

- Rule: L4 (tests that do not test what their name claims).
- Evidence: `tests/test_maybe_compile.py:165-174` (alive PID plus old
  timestamp must be "stale", see OPS-01); `tests/test_scheduled_nightly.py:
  301-315` (the health-report failure line must be the class name only, see
  OPS-09); `tests/test_scheduled_nightly.py:251-263` replaces `_run_steps`,
  `_wait_for_compile_idle`, `_last_compile_finished` and
  `_wait_compile_finished` and calls `_nightly_steps` with a fake runner, so no
  test runs `_run_nightly_body` or checks the log format of a failing step.
- Verified: read.
- Fix direction: retarget the two assertions once OPS-01/OPS-09 are decided;
  add one end-to-end nightly test with a failing subprocess step.

### OPS-18 — Two queue heartbeat threads are joined without a timeout

- Rule: C (bounded).
- Evidence: `scripts/memory_queue.py:13903-13905, 13961-13963`
  (`self._thread.join()`); the thread may be inside SQLite bounded by
  `queue_busy_ms = 5000` (`reliable_memory.py:30`), so the wait is bounded
  only by a constant from another module. Every other join in scope carries a
  timeout (`operational_ownership.py:1393`, `markdown_transaction.py:7411`,
  `doctor.py:7001`).
- Verified: read.
- Fix direction: join with `heartbeat_seconds * 2` and refuse by name, as
  `operational_ownership` does.

### OPS-19 — The state-lock stale bound and the 10 s hook budget can steal a slow writer's lock

- Rule: C (at most one writer).
- Evidence: `scripts/memory_state.py:90` (`_STALE_LOCK_SECONDS = 30`), `:222-229`
  (the holder writes its PID once and never refreshes mtime), `:266-273`.
- What is wrong: a legitimately slow writer past 30 s survives only because
  its PID reads alive; combined with OPS-08 (EPERM read as dead) the steal
  is possible. Low because writers hold the lock for milliseconds.
- Verified: read. Inference.
- Fix direction: covered by OPS-07 and OPS-08.

### OPS-20 — `install.sh` hides why V3 adoption failed

- Rule: L3.
- Evidence: `install.sh:620` (`2>/dev/null ... || echo unknown`), `:624`
  (`>/dev/null 2>&1`), `:628-635` (the warning tells the user to run the same
  command blind).
- Verified: read.
- Fix direction: capture stderr to the install log and print its head.

### OPS-21 — Best-effort capture paths drop failures with no counter

- Rule: L4 (observable capture contract).
- Evidence: `scripts/capture_operation.py:95-101, 124-127` (`except
  Exception:` → fallback id or `pass`); `scripts/flush_memory.py:1516-1526`
  (`_capture_feedback`, `pass`); `scripts/mcp_server.py:1069-1078, 1191-1200,
  1400-1409` (telemetry events, `pass`).
- What is wrong: each is defensible alone ("a lost race is not a hook
  failure"), but none increments a counter, so the doctor's capture-loss
  check cannot see them.
- Verified: read.
- Fix direction: count them in the existing capture-diagnostics counter.

### OPS-22 — Hidden module globals

- Rule: L4.
- Evidence: `scripts/maybe_compile.py:66, 140, 261` (`_current_owner`),
  `scripts/mcp_server.py:1990-2026` (`_NAVIGATION_MANAGER`, `_CLOSING`,
  `_EPOCH`; lock- and epoch-guarded).
- Verified: read. The MCP globals are guarded; the compile owner is not.
- Fix direction: carry the owner token in the return value of the claim.

### OPS-23 — The nightly skip reason never reaches the doctor's message

- Rule: L3.
- Evidence: `scripts/scheduled_nightly.py:581-591` (a marker older than 30 min
  whose PID reads alive — including a reused PID — is never abandoned, so the
  nightly skips every night), `scripts/doctor.py:5300-5357`
  (`last_nightly_skip` is placed in `details`; the message says only
  "Nightly maintenance is stale" after 26 h).
- Verified: read.
- Fix direction: when `last_nightly_skip` is newer than `last_nightly_at`,
  say "skipped: maintenance_lock_held" in the message.

---

## Questions the audit could not settle

- Q1. Does `compile_memory._mark_finished` run on every exit path, including a
  killed interpreter? If not, a compile that dies before the stamp is written
  is invisible to `_compile_failed_this_pass` (`scheduled_nightly.py:349-364`),
  which then reports `failures=0`. Not read: the `compile_memory.py` main body.
- Q2. Under Windows Task Scheduler, does the nightly run in the same security
  context as the interactive hooks? If not, `OpenProcess` fails across the
  boundary and every legacy lock (OPS-08) reads the other side as dead.
- Q3. When `repository_index.py refresh-all` is SIGKILLed at the outer 900 s
  bound (OPS-10), is the generation activation atomic? The catalog design says
  yes; the activation code was not read in this audit.
- Q4. `integrations/codex/hooks.json` registers SessionStart, PreCompact,
  PostCompact and Stop only; Claude gets UserPromptSubmit and PostToolUse as
  well. Whether Codex lacks those events or capture was left out is not
  determined here.
- Q5. `scripts/plugin*.py` from the brief does not exist; `tests/test_plugin_
  helpers.py` exists. Where the plugin helpers live was not located.

---

## Counts

By severity: critical 0, high 4, medium 11, low 8; questions 5. Total findings
23.

By rule offended (a finding may count under more than one): L3 honesty 8
(OPS-01, 04, 05, 09, 13, 16, 20, 23); L4 quality/reliability 16 (OPS-01, 02,
03, 04, 06, 07, 08, 09, 10, 11, 12, 13, 14, 17, 18, 21, 22); L5 complexity 1
(OPS-15); repository contracts C 8 (OPS-01, 02, 03, 06, 07, 08, 18, 19);
L1 and L2: no finding (the graph was used for callers; no design change was
audited).

Verification: ran 3 (OPS-01, 11, 15), read 20, inference-only outcomes noted
inside OPS-06, 07, 09, 10, 19.

## Files in scope not read line by line

Pattern-driven pass only (grep hits read in context): `scripts/doctor.py`,
`scripts/memory_queue.py`, `scripts/markdown_transaction.py`,
`scripts/mcp_server.py`, `scripts/lsp_process.py`, `scripts/install_control.py`,
`scripts/reliable_memory.py`, `scripts/flush_memory.py`, `scripts/code_graph.py`,
`scripts/repository_index.py`, `scripts/installer_config.py`.

Metrics only, not read: `scripts/install_pyright.py`,
`scripts/installed_memory_repair.py`, `scripts/pyright_session.py`,
`scripts/pyright_profile.py`, `scripts/lsp_security.py`,
`scripts/lsp_protocol.py`, `scripts/lsp_process_tree.py`,
`scripts/lsp_identity.py`, `scripts/lsp_server_profile.py`,
`scripts/lsp_launch_package.py`, `scripts/lsp_positions.py`,
`scripts/lsp_paths.py`, `scripts/lsp_profiles.py`,
`scripts/install_language_server.py`, `scripts/install_smoke.py`,
`scripts/capture_diagnostics.py`, `scripts/capture_adoption.py`,
`scripts/capture_hooks.py`, `scripts/session_start_project_state.py`,
`scripts/sync_memory.py`, `scripts/evidence_reader_cache.py`,
`scripts/mcp_http.py` (shutdown only), `scripts/scheduled_weekly.py`,
`scripts/run-scheduled-task.ps1`, `install.sh` lines 1-399,
`install.ps1` lines 1-319.

Tests read in full: `test_scheduled_nightly.py`, `test_maybe_compile.py`,
`test_a_failing_nightly_is_visible.py`, `slow_machine.py`. Tests grepped only:
`test_self_update.py`, `test_operational_ownership.py`, `test_doctor.py`,
`test_lsp_protocol.py`, `test_sync_memory.py`. Tests not opened: the rest of
the 327.

Source of every line number: the audit worktree at `6fcab30`, 2026-09-10.
