# Catalog growth uses its existing byte budget

Date: 2026-09-30. Status: installed and qualified.

The catalog refuses its 1,025th registration or 16,385th activation even when
its database remains below the existing constructor-selected byte budget.
These two constants explicitly have no measured basis. The same row ceilings
also make complete listings and pointer repair fail. Registration, activation
and repair already check page_count * page_size plus one commit-reserve page
inside the transaction; failures roll back. Those checks stay.

Primary sources checked today:

- [SQLite PRAGMA](https://www.sqlite.org/pragma.html#pragma_page_count) describes
  page counts and page sizes; resource accounting uses those actual metrics.
- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html)
  documents progress callbacks and interruption before a result row exists.
- [Google SRE overload](https://sre.google/sre-book/handling-overload/)
  supports explicit resource admission and deadlines rather than disguising
  resource needs as an unexplained product population limit.

Remove the two redundant row ceilings and their unused admission helper.
Read complete finite results from the already size-validated local database.
During SQL execution and row collection honor the caller deadline and cancellation,
including ORDER BY before the first row. SQLite's minimum callback interval of
one instruction avoids inventing a new cancellation interval. Callers without
stop conditions install no callback. Always remove it and preserve unrelated
SQL errors. The byte-budget default's original numerical basis is still unknown;
this change does not claim to have justified that separate setting.

Alternatives: raising the row numbers moves the same failure; adding two
settings duplicates the existing byte budget; automatically deleting history
loses evidence. Removing all guards permits uncontrolled storage growth. Keep
byte checks, sealed-generation validation, compare-and-swap, read-only access,
rollback and retention rules. Larger valid catalogs can allocate more Python
objects; the explicit byte budget bounds source storage, not exact heap use.
Review heap amplification using representative large manifests if workloads grow.
No paths, schemas, environment keys or runtime locations change.

The regression scales former thresholds down through monkeypatching and proves
that three real sealed registrations and activations fit the actual byte budget.
It fails on the old writer guard, then checks full readers and the active pointer.
Existing byte-ceiling rollback and generation-integrity tests remain required.

The old implementation failed the growth regression. The final catalog, deadline,
byte-rollback and actual complexity group passed 190 tests (3 skipped); Ruff passed.
A first candidate run failed after a broad SQL replacement also touched the
ancestor-retention query and removed a string comma; both defects were corrected
before installation. Its failed output remains as evidence. No guard was weakened.
The two constants and their unused capacity helper were removed. Installation
retained verified preimages under the existing maintenance fence.

Evidence: logs/audit-2026-09-30-finish-catalog-red.txt,
logs/audit-2026-09-30-finish-catalog-qualified.txt,
logs/audit-2026-09-30-finish-catalog-activation.json and
logs/audit-2026-09-30-finish-live-catalog.json.


The next full run exposed a missed consumer: doctor's orphan cleanup copied the
old row-limit query and still imported MAX_GENERATIONS. The catalog-only group
could not qualify that consumer. Reproducing the incremental-generation maintenance
test fails with AttributeError in that helper. Replace the duplicate query with
the public registered_generation_ids method, passing the maintenance deadline
and cancellation, and delete the copied helper. Existing orphan-retention and
incremental-maintenance regressions detect this defect without weakened assertions.
The failed full run is retained; its result cannot be called a green final run.

A separate synthetic reader fixture, explicitly not sealed valid generations,
held 1,025 registrations and 16,385 activations in 745,472 database bytes.
Complete listing/snapshot took 0.13 s, with 2,922,026 peak Python bytes traced.
This qualifies those reader cases, not an arbitrary catalog size or default.
Source: logs/audit-2026-09-30-finish-maintenance-red.txt;
logs/audit-2026-09-30-finish-full.txt;
logs/audit-2026-09-30-finish-catalog-load.json.


The repaired consumer plus full generation-maintenance, in-flight orphan,
catalog and actual complexity groups pass 208 tests (3 skipped) in 51.56 s.
Ruff passes. The replacement is installed with verified preimage and after
hashes under the existing maintenance fence. A fresh complete regression
is still required after the failed run.
Evidence: logs/audit-2026-09-30-finish-maintenance-green.txt;
logs/audit-2026-09-30-finish-doctor-catalog-activation.json.
