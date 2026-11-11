# SQLite connection and lock-probe ownership

2026-11-03; approved completion mandate; baseline 8d8df4c4.

Move the 29 remaining reliable_memory functions into llm_wiki.sqlite_io:
connection open/configure/close-on-failure, bounded two-connection lock probes,
state/database directory preparation, read-only open with one stranded-journal
recovery attempt, and streamed row iteration. Move the existing replay timeout
and row-batch constants with their complete rationale comments. Public names and
pickle paths remain identical aliases through reliable_memory. Its separate
capture-decision size constant remains where it is until capture ownership is
handled; do not misassign it to SQLite merely to empty a file.

The owner imports runtime environment/metadata/value contracts, SQLite contracts,
pragmas/deadlines and the bounded-IO ancestor check. It never imports the facade,
queue, transactions, publisher or fingerprinting. Caller-owned connections remain
caller-owned after success; failure still closes the connection. The probe owns
only its unique scratch DB/handles and refuses cleanup if directory identity
changed. validate_state_root may initialize run/; validate_database_directory
must not create another runtime root. Preserve these distinct existing effects.

Read-only validation must preserve live advisory locks. A stranded rollback
journal is replayed only under the existing error/filename/deadline conditions,
with the same busy timeout, and retried once. Do not switch to WAL, add retries,
relax FULL synchronization, delete journals directly or silently return an empty
result. Streamed scans retain stop callbacks and their batch bound, never a total
row ceiling. All function bodies, SQL strings, ordering and limits stay unchanged.

Research checked today, independent primary publishers:
- Python 3.10 sqlite3: connection ownership, autocommit/transaction control and
  URI read-only opens. https://docs.python.org/3.10/library/sqlite3.html
- SQLite locking v3 and atomic commit: rollback-journal recovery and lock state
  are database responsibilities. https://www.sqlite.org/lockingv3.html
  https://www.sqlite.org/atomiccommit.html
- Linux man-pages fcntl_locking: process-associated locks can be released by
  closing another descriptor on that file. The metadata-only validation seam
  must therefore remain unchanged.
  https://man7.org/linux/man-pages/man2/fcntl_locking.2.html
Python >=3.10 remains supported by the project despite upstream EOL; no new
runtime technology or dependency is selected. Alternatives (a new store object,
implicit connection contextmanager, WAL migration) change lifetime or storage
semantics without helping this mechanical responsibility split. A pure facade
plus one explicit IO owner preserves the public boundary and removes backimports.

Fault-injection bindings will be audited again: doctor deliberately calls some
facade attributes, while state-root orchestration and journal helpers now look up
bindings in sqlite_io. Retarget only the latter; keep original assertions. Verify
full AST preservation, pickle bytes, lock/race/recovery regressions, guards and
actual complete packet size before counting the owner as ready.

Qualification refinement: the first complete packet with own tests was 100325
tokens and correctly failed its budget guard. The sole bounded_io dependency was
three shared ancestor predicates. The approved internal refinement gives those
predicates a single path_ancestry owner; sqlite_io's lazy import names that owner.
No predicates or tests were omitted. Details and independent research:
2026-11-03-path-ancestry-boundary.md. All 29 function ASTs remain identical apart
from that import path; the three predicates and all 40 remaining bounded_io
functions are AST-identical. Original integration assertions are unchanged.

Initial regression run reproduced four stale injection bindings (4 failed,
60 passed, 3 skipped). Retargeted integration run: 81 passed, 3 skipped;
doctor: 184 passed, 3 skipped. The former healthy-database test also manually
assigned a helper attribute; that assignment and restoration now name sqlite_io.
Doctor's direct calls to facade attributes remain patched at the facade.

Final phase checks: 322 passed/7 skipped in the weighted affected-file run;
260 passed/6 skipped in queue, transaction, lock/adoption and storage-fault
integrations; Python 3.10: 88 passed/3 skipped; all module/bootstrap/boundary,
complexity, structure, CI, privacy and limit guards: 953 passed/2 skipped.
One additional static import guard was added after the broad run and passed its
four-test file. Ruff and local CCN/shape checks pass. All 43 serialized artifacts
round-trip every logical path and byte. This is module qualification in the
candidate, not live installation or whole-project completion. The full suite
running in the separate worktree is still pinned to prior commit 8d8df4c4.
