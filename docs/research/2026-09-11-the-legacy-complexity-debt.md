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
