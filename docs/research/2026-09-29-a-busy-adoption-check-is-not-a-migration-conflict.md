# A busy adoption check is not a migration conflict

Date: 2026-09-29. Python 3.10–3.14 remain supported. The installed reproduction
used Python 3.14.6 and the existing rollback-journal SQLite contract.

The full doctor reported adoption admitted writers, then capture reported an
adoption conflict and prescribed offline migration. A direct invocation of the
same inspection body while the vault was active raised SQLite `OperationalError:
database is locked`. Its public envelope discarded the cause and classified
every exception as an invalid adoption record. Capture then interpreted that
classification as proof migration was needed. This explains the diagnostic
contradiction; it does not identify the particular brief writer or prove the
cause of unrelated historical failures.

The path is SQLite validation → `inspect_installed_vault` → doctor's adoption
state → capture health message. The repair CLI also reads the envelope, and
`_apply_reliability_v3_adoption` refuses an error before any adoption write.
Codebase Memory and source inspection confirmed these consumers. No on-disk
adoption schema, database, timeout, or writer admission rule changes.

## Sources and choice

- [SQLite result codes](https://www.sqlite.org/rescode.html) distinguish BUSY
  and LOCKED from corruption and define the primary code in the low eight bits.
- [Python sqlite3 exceptions](https://docs.python.org/3/library/sqlite3.html#exceptions)
  expose `sqlite_errorcode` from Python 3.11. Python 3.10 therefore needs the
  exact SQLite lock messages as a compatibility fallback.
- [Google SRE monitoring](https://sre.google/sre-book/monitoring-distributed-systems/)
  distinguishes symptoms from causes and calls for actionable diagnostic signals.

Preserve the unsuccessful, closed inspection result, but name contention as
`busy` with `operational_database_busy`. Doctor advises checking again when the
transaction finishes. Other conflicts require read-only inspection before any
repair. Fresh or incomplete adoption retains its existing migration guidance.
The CLI exit status remains unsuccessful, and repair remains refused while busy.

Alternatives rejected: pretending the previously observed adoption is still
verified would hide a missed check; automatically rerunning offline adoption
would be unjustified; adding arbitrary waits would change latency and still not
justify diagnosing contention as corruption. The cost of the chosen behavior is
an explicit incomplete health check during contention, rather than a false green.

## Verification and cleanup

A separate process holds a real exclusive lock on each operational database.
The original implementation reports `conflict`; the corrected implementation
reports `busy`, capture remains degraded, repair stays closed, and release allows
normal validation again. Additional cases cover extended result codes, Python
3.10 messages, and non-contention errors. Existing immutable-artifact, schema,
offline-confirmation and runtime-deletion checks remain required.

The old blanket classification is replaced in the existing boundary. No parallel
implementation, runtime adapter, retained data, or migration artifact is added.
