# The scale stand is one adapter cell shape

Date: 2026-09-11. Trigger: audit finding L13, `benchmark/run_scale_matrix.py`:
21 functions over the gate after the M11 fix — the three optional adapter
cells (`_run_lancedb_cell` CCN 31, `_run_usearch_cell` 29, `_run_sqlite_vec_cell`
14) each build an index, time a cold and a warm pass, compute recall and
assemble the same cell dictionary by hand; `run_crash_matrix` (23),
`evaluate_adoption_gate` (23), `_validate_report_semantics` (23), `main`
(20), `exact_numpy_search` (19), `plan_matrix` (17) and the rest are
validation ladders.

## Sources

1. `tests/test_scale_matrix.py`: the report schema, the adoption gate
   reasons (their order and wording), the crash-matrix statuses and the
   smoke report are asserted; the harness runs offline with `fake`.
2. Rule 5's remedies: what three cells write by hand becomes one
   `_adapter_cell`, one `_ann_metrics` and one `_recall_pair`; a loop that
   decides a status becomes a function that returns it; a ladder of
   argument checks becomes one `_require_*` per argument group.

## Decision

Reports unchanged field for field; the adoption reasons keep their order
and text. Each optional adapter cell is: build (timed), cold pass, warm
pass, recall, `_adapter_cell`. The crash matrix is one
`_crash_point_outcome` per point with named steps (worker, fsync
outcome, active validity, staging recovery, status). `main` is
crash-worker entry, argument bounds, full or smoke report, emit. Build
timing now brackets the index build alone (the mask set is built before
the clock starts); it is a benchmark reading, not a report contract.

Files: `benchmark/run_scale_matrix.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
