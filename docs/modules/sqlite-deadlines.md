# Cooperative SQLite deadline control

llm_wiki.sqlite_deadlines checks the same monotonic caller deadline and configures
an existing SQLite connection. operational_busy_ms clips the requested lock wait
to the remaining budget; refresh_operational_deadline applies it to busy_timeout.
configure_operational_deadline installs the existing progress callback at 1000 VM
instructions. Its measured rationale travels with the constant. This is cooperative
interruption, not a hard bound on filesystem, UDF or operating-system delays.
None or infinity preserves existing connection settings. Expired finite budgets
raise before connection changes. No connection is opened or closed, and no thread,
new timeout or state is introduced. The time module remains the same stdlib object.
Old reliable_memory names are identity aliases; public pickle names are retained.
Tests use real SQLite and a controlled monotonic clock to verify interruption,
refusal before access, recomputed waits and preserved existing controls.

Run `uv run pytest tests/modules/sqlite_deadlines -q -s`.
See ../research/2026-11-03-sqlite-control-boundaries.md.

Tests replacing the entire clock object must patch sqlite_deadlines.time.
Patching a shared stdlib time.monotonic attribute still reaches its importers;
rebinding reliable_memory.time does not replace this owner's module binding.

## Canonical ownership reference

`llm_wiki.sqlite_deadlines` owns cooperative SQLite deadline/progress control;
`llm_wiki.sqlite_migrations` owns resumable statement execution. Both use supplied
connections. reliable_memory retains API aliases; connection and file ownership,
transaction boundaries, callback order and existing limits are unchanged.
