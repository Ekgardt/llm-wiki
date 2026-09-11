# The legacy complexity debt

Date: 2026-09-11. Trigger: with this round's files at zero (see
`2026-09-11-the-second-gate-counts-ifs.md`), the same read-only managed
analysis over every Python file of `scripts/`, `benchmark/` and
`integrations/` still reports 474 findings in 46 files: code written before
law 5 was set on 2026-08-17 and never touched since. Law 5 binds code that
is written or modified; the owner's standing instruction is to close every
open problem, so the debt is closed too, in the order that cannot collide
with the issue #24 branch still open.

## What the analysis counts

1. radon cyclomatic complexity > 5 — radon adds one for every boolean
   operator value, `assert`, `except` and comprehension clause, so it runs
   above lizard on the same function.
2. More than two `if` statements in one block (law 5: "при появлении более
   двух if внутреннюю логику необходимо выделять в независимые подфункции").
3. `if` whose branch already exits but still carries an `else`.
4. Nesting of `if`/`for`/`while` deeper than two, and branching inside a
   conditional expression.

## Method

The refactorings are the catalogue ones and preserve behaviour by
construction: Extract Function, Replace Nested Conditional with Guard
Clauses, Decompose Conditional, Replace Conditional with a lookup table
(Fowler, *Refactoring*, 2nd ed., 2018, ch. 6 and 10), and Split Loop /
Replace Loop with Pipeline for loops that carry several ifs. Every batch:
messages, exception types, check order, clock reads and module-level names
that tests monkeypatch stay the same; the analysis, `lizard -C 5`, the ccn
gate and ruff run on the file; the module's test files run; the whole suite
runs in a clean detached checkout before any push.

Files are taken in this order. First, files the issue #24 branch does not
touch: `scripts/compile_cache.py`, `scripts/build_tiers.py`,
`scripts/check_knowledge_writers.py`, `scripts/context_budget.py`,
`scripts/build_advisory.py`, `scripts/feedback_capture.py`,
`scripts/build_context.py`, `scripts/graph_neighbors.py`,
`scripts/context_compiler.py`, `scripts/lint_memory.py`,
`scripts/ci_timing_report.py`, `scripts/bootstrap_project.py`,
`scripts/private_vault_backup.py`, `scripts/daily_log_append.py`,
`scripts/retrieval_telemetry.py`, `scripts/reflection.py`,
`scripts/session_feedback.py`, `scripts/project_extractor.py`,
`scripts/lookup_mode.py`, `scripts/build_guardrails.py`,
`scripts/query_memory.py`, `scripts/interruption.py`,
`scripts/integration_config_backup.py`, `scripts/repair_installed_memory.py`,
`scripts/user_prompt_capture.py`, `scripts/reranker.py`,
`scripts/repair_refused_page_creation.py`, `scripts/migrate_to_okf.py`,
`scripts/knowledge_extractor.py`, `scripts/bounded_io.py`,
`scripts/access_tracking.py`, `scripts/installer_config.py`,
`scripts/integration_hook_config.py`, `benchmark/compare_arms.py`,
`scripts/lsp_process.py`, `scripts/lsp_process_tree.py`,
`scripts/install_pyright.py`, `scripts/windows_workspace.py`,
`scripts/code_navigation_renderer.py`, `scripts/mcp_contract.py`,
`CHANGELOG.md`. Then, after the #24 branch is merged: `code_workspace`,
`code_extractor`, `code_navigation`, `code_intelligence`, `install_control`,
`codex_memory`, `merge_claude_settings`.

## Sources

1. Law 5, `/home/user/.claude/CLAUDE.md`.
2. M. Fowler, *Refactoring: Improving the Design of Existing Code*, 2nd ed.
   (2018) — the refactoring catalogue named above.
3. radon documentation, "Cyclomatic Complexity" — which constructs add one.

## A dead lock is deleted, not refactored

`daily_log_append._daily_lock` (CCN 16, nesting 4) has had no production
caller since 9375f3d moved every daily-log write onto the transaction's
`append_knowledge`: no script, hook or integration names it (a repository
grep finds only its own definition, the CHANGELOG and four tests). The
serialization it once gave is proven where it now lives: cross-process
same-file appends converge without loss or interleaving in
`test_concurrent_identical_append_converges_once_during_distinct_event_churn`
(18 processes) and `test_concurrent_appends_lose_no_bytes`. The tests that
exercised the dead lock itself give assurance about code nothing runs, so
they go with it: `TestDailyLockExclusivity` in
`tests/test_security_invariants.py`, the `nullcontext` stand-in in
`tests/test_memory_queue.py`, the `STATE_ROOT` isolation of the lock in
`tests/test_capture_hooks.py`, and the `_daily_lock` marker in
`tests/test_quality_guards.py`, whose remaining markers (`append_daily`,
`locked_append`) are the two writers every daily-log script uses.

## A module that never ran is deleted

`scripts/session_feedback.py` (the "self-correcting flywheel": record which
decisions were injected, look for corrections in the next days, raise a
staleness score) arrived in e20b204 together with contextual retrieval and
was never wired: `record_injection` has no caller in any script, hook,
integration or scheduler, so its injection list is always empty,
`run_feedback_check` always returns zeros and `should_inject` is consulted
by nothing. It also carried a lost update — `run_feedback_check` saved the
feedback it loaded before the per-decision updates, erasing them — which
only never mattered because nothing ran it. It goes with
`tests/test_session_feedback.py` and its `tests/shard_weights.json` entry,
as the dead LLM branches of contextual retrieval went earlier today.

## The code-intelligence files do not wait

The issue #24 branch, once finished, touched none of `scripts/code_workspace.py`,
`scripts/code_extractor.py`, `scripts/code_navigation.py`,
`scripts/code_intelligence.py` or `scripts/install_control.py` (its diff is
`code_hints`, `graph_hint`, `repository_*`, `codex_*`, `merge_claude_settings`,
`integration_adapter`, `evidence_graph`, `mcp_server`, `scheduled_nightly`,
`doctor` and the OpenCode plugin), so these five are taken now; only the files
that branch changed wait for it.

## The extractor keeps its version

`scripts/code_extractor.py` (46 findings) mints every node, assertion and
observation the evidence graph holds, so the refactor is judged by its
output, not by its tests alone: the old and the new `extract_code` over all
541 tracked sources of this repository (Python, Bash, JavaScript,
TypeScript) return equal records — 28,181 nodes, 77,807 assertions, 83,122
observations — and equal ones again with 200 SCIP symbols and 30
co-changes, with a deadline set, and for every refused input (same
exception, same message). A cancellation callback counting its own calls
fires at the same call in both (1, 6, 501, 50,001), so every stop check
sits where it sat. Because the output is byte-identical,
`EXTRACTOR_VERSION` stays `code-extractor/v11`: a bump would only force a
full graph rebuild for nothing.

The first cut was 7-14% slower (23.4 s → 25.6 s on the repository). The
profile named the cost: argument validation repeated in each of 6.6 million
stop checks, one extra call per scalar in `_deep_freeze`, and one extra call
per AST node. The collector now validates `deadline` and `cancelled` once,
in its constructor (`extract_code` already validated them before building
it), `_deep_freeze` returns scalars first, and the edge passes filter node
types before calling out. The second cut runs 23.8 s against 23.9 s.

`scripts/answer_budget.py` pointed at "scripts/code_extractor.py:226" for
the identifier form; the line had already drifted, and the comment now
names `code_extractor._identifier` instead.

## Navigation is judged against its own branches

`scripts/code_navigation.py` (39 findings, one method at CCN 90) is the
freshness-proven facade over Pyright: every answer is fenced by a workspace
revision taken before and after it, and a moving workspace gets one retry.
The rewrite keeps each attempt's state in a small class (`_QueryAttempts`
and `_QueryAttempt` for queries, `_StructuralRun` for symbol resolution and
edge verification); an attempt that has decided its result ends through one
private exception carrying that result, and the first-attempt retry through
another, so no phase needs a ladder of early returns. Every warning text,
status, deadline check and publication point is kept.

The facade's 272 tests pass, but a line trace showed they never reach 78
statements — among them a type query whose two provider calls fail in
different ways, a valid hover range, outgoing calls, the source-document
cache replacing an entry, and most revision faults of symbol resolution and
edge verification. Those branches were compared old against new in 46
scenarios (every one equal, with statuses from `ok` to `stale`), and kept as
`tests/test_code_navigation_fault_paths.py`: 40 cases that pass on the
code before the change and after it, so they pin existing behaviour rather
than the new code's.
