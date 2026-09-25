# The clean run after the audit fixes

Date: 2026-09-25. Branch `work` for PR 42.

## Question

The full suite in a clean detached worktree with an external state root gave
9162 passed and 8 failed. What is each failure, and what is the fix?

## Sources

- uv, "Syncing the environment" (fetched 2026-09-25,
  https://docs.astral.sh/uv/concepts/projects/sync/): "uv sync performs 'exact'
  syncing by default, which means it will remove any packages that are not present
  in the lockfile", and "uv does not sync extras by default".

## Findings (facts)

1. Five failures (`test_embedder_unavailable_names_its_reason.py` ×3,
   `test_the_query_path_reads_local_weights_only.py` ×2) say `No module named`
   `transformers`. They were first taken for an environment effect, because the
   clean worktree had been re-synced without the optional extras. That was wrong:
   CI run 36078137787 failed the same five tests on all 18 test jobs, and the cause
   is finding 5.
2. `test_a_project_context_page_has_a_command.py` pins
   `scripts/build_context.py --slug` in the user guide. The flag does not exist;
   audit C-8 corrected the guide to the positional form, and the test pinned the
   wrong form.
3. `test_the_rest_of_the_live_audit.py::test_a_truncated_transaction_scan_says_its_counts_are_lower_bounds`:
   the audit B-7 change added exact per-state totals to the message and dropped
   the sentence that the scanned counts are lower bounds. The totals are exact
   (one aggregate over the whole table); the scanned details are not. Both must
   be said.
4. `test_runtime_deletion_contract.py::test_policy_retention_blocks_deletion_without_degrading_health`
   allows `capture` and `scheduler` to be degraded in its hermetic vault. The
   backup check added for audit B-10 is degraded too, because a test vault has
   never taken a snapshot. That is the same kind of finding as a missing scheduler.

5. The PR run 36078137787 failed the same two tests of
   `test_the_query_path_reads_local_weights_only.py` on every platform, with the
   embedding package absent. The cause is code, not the environment: the audit
   C-11 change imported `transformers.utils.logging` inside the embedder loader to
   switch off its "Loading weights" bar. The tests stand a fake
   `sentence_transformers` in for the library, and CI does not install the
   optional package, so that second import failed and the loader reported
   `import_failed`. In the installed `transformers` 5.13.0,
   `transformers/utils/logging.py` sets `_tqdm_active = not
   hf_hub_utils.are_progress_bars_disabled()` at import, and `huggingface_hub`
   reads `HF_HUB_DISABLE_PROGRESS_BARS` from the environment.

## Decision (conclusion)

- Run the embedder tests in an environment without the optional extras, as CI
  does, as well as the full suite with them.
- The guide test pins the positional command.
- The truncated-scan message says both: the scanned counts are lower bounds, and
  the rows by state are the exact totals.
- The deletion-contract test allows `backup` beside `capture` and `scheduler`.
- The loader imports only `sentence_transformers` again. The command line, which
  owns its process, sets `HF_HUB_DISABLE_PROGRESS_BARS=1` (unless the operator set
  it) before anything loads the model: `search_memory.quiet_model_loading`.

## Edited files

- `tests/test_a_project_context_page_has_a_command.py`
- `scripts/doctor.py`
- `tests/test_runtime_deletion_contract.py`
- `scripts/search_memory.py`, `tests/test_the_query_path_reads_local_weights_only.py`
