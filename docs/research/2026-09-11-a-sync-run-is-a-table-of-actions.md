# A sync run is a table of actions

Date: 2026-09-11. Trigger: audit finding OPS-15 (Rule 5 debt), files
`scripts/sync_memory.py` (`run_sync` CCN 55 with a 240-line `if/elif`
ladder over the action names and a nested index-refresh decision;
`_dependency_action` 34, the same `uv` step written three times with
its own three `except` arms; `_run_index_builder` 14;
`_doctor_subset_action` 11; `_run_process_tree` and
`_finish_timed_out_process` 6) and `scripts/install_smoke.py`
(`run_smoke` 10, `_doctor_report` 6, `_validate_doctor_report` 6).

## Sources

1. Rule 5's remedies: a ladder of more than two arms becomes a table; a
   step written three times becomes one function with a parameter; a
   decision that needs shared state takes it by name.
2. `tests/test_sync_memory.py`, `tests/test_install_smoke.py`,
   `tests/test_lsp_process_tree.py`, `tests/test_maintenance_helpers.py`:
   every message, status and detail these assert is kept verbatim; the
   helper names the tests patch (`_run_process_tree`, `_run_uv`,
   `_dependency_action`, `_run_index_builder`, `_run_generation_builder`,
   `_terminate_windows_tree`) are kept.

## Decision

Behaviour unchanged. `run_sync` builds one `_SyncRun` (paths, apply flag,
deadline, doctor cache) and walks `ACTIONS` through a table of methods;
the index action is four small steps (which refresh is needed, the
generation refresh, the legacy refresh with its freshness validation, and
the combined result). The three `uv` steps are one `_UvStep` record each
run by `_run_uv_step`, which returns the completed process or the error
result. `_run_process_tree` hands its Popen options and its tree kill to
helpers. `install_smoke.run_smoke` checks its deadline, resolves its
roots and reads the remaining budget through named helpers.

Files: `scripts/sync_memory.py`, `scripts/install_smoke.py`,
`CHANGELOG.md`, `docs/AUDIT-2026-09-10-operations-and-reliability.md`.
