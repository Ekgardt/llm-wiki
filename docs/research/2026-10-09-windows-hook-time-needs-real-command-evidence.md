# Measure the Windows host path before claiming its timeout is fixed

On 2026-10-09 CI 37972971617, Windows Python 3.11 shard 4 reported 8.469 seconds in the unchanged post-tool checkpoint test against the existing five-second host contract. Durable capture succeeded; the timing assertion failed. A previous reduction in post-publication ACL writes therefore does not establish complete resolution.

The capture directory path still hardens five directories on each occurrence. Windows hardening starts an ACL-changing command and an ACL-reading command for each directory. Further publications and protected reads also run commands. This is a candidate contributor, not yet a measured explanation of that failed runner.

A separate Windows-only diagnostic scenario calls the complete original test, including its unchanged timing assertion, capture checks, worker delivery and journal checks. Wrappers call the original ingestion and ACL command functions exactly once with their original arguments and return their results unchanged. JUnit properties distinguish setup, the actual timed host ingestion, and subsequent delivery, recording each command's operation and elapsed time. Thus fixture setup and worker delivery cannot be mistaken for the five-second host interval. No production behavior or timeout is changed.

Actual AST/Lizard analysis of all three observer functions gives CCN 2, zero if statements and zero branch nesting. Ruff passes; parsing uses the Python 3.10 grammar. Direct local execution of both original scenarios with the observer passes, with host ingestion of 0.123 and 0.099 seconds. These Linux measurements validate observer wiring only; they do not qualify Windows timing. The Windows-marked pytest cases are skipped locally. Their shard weight records that local skip and must later be refreshed with actual Windows measurements.

This diagnostic is retained until actual Windows command measurements establish or refute the candidate cause and an appropriate regression proves the correction. Remove it after that evidence is preserved and permanent regression coverage is installed; it is not a new product feature. The macOS EBADF's exact cause also remains unproven. Neither failure may be retired merely because a subsequent run is green.

Source: `tests/test_windows_hook_acl_time_is_observed_without_changing_capture.py`; unchanged `tests/test_a_prompt_checkpoint_does_not_outwait_its_host.py`; `scripts/integration_adapter.py::_ensure_capture_intent_directories`; `scripts/markdown_transaction.py::_run_acl_command`; original CI job 113964026387; private observer complexity report.

## Actual Windows evidence and observer correction

CI 37979502512 completed successfully with all 58 jobs on commit ecab0151. The observer measured host ingestion of 1.063/1.078 seconds on Python 3.10, 0.704/0.687 on 3.12, and 0.630/0.684 on 3.14 for prompt/post-tool respectively. Each host interval contained 34 ACL commands, taking 0.172–0.420 seconds in total. These passing measurements do not explain the earlier 8.469-second failure. They refute a claim that the command count alone proves the historical cause.

The first observer omitted the original module's `shipped_append_budgets` mark. Calling a test function directly does not inherit its module marks; the autouse fixture consequently selected its content-test append budgets. The observer now carries the same mark as the original. The original test, production budgets and five-second assertion are unchanged. The measurements above remain historical diagnostic evidence with that fixture limitation, not strict qualification of the corrected observer.
