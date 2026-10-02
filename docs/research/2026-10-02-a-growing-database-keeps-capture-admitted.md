# A growing operational database keeps capture admitted

Research date: 2026-10-02. Validated candidate; fenced installation recorded in the audit evidence.

The installed transaction database passed 256 MiB and admission refused it as an invalid runtime record. The observed file is an owner-only regular SQLite database. Two real adopted fixtures, separately growing the queue and coordinator past the old threshold, reproduce that refusal; path and permission rejection tests still pass.

Admission opens SQLite read-only to inspect identity, schema and operational pragmas. It does not read the entire database into a byte string. A whole-file read ceiling therefore does not measure the work of this admission and incorrectly blocks capture as retained history grows. The shared file validator permits an explicit absent byte budget for these metadata/database opens. Regular-file, reparse/symlink, owner-only, containment, connect identity, schema and pragma checks remain. Callers reading bytes retain their explicit budgets. Doctor's full health-scan ceiling is unchanged and must not be interpreted as corruption or a deletion permit.

Sources checked today: [SQLite implementation limits](https://sqlite.org/limits.html), [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html), [Linux open(2)](https://man7.org/linux/man-pages/man2/open.2.html). These are independent upstream sources. Runtime versions and measured verification are recorded in private audit evidence.

Alternatives: raising 256 MiB merely postpones the failure; deleting history violates retention; reading the entire database to admit a writer wastes resources. Keeping SQLite's existing paging and read-only contract is the smallest correction. This does not certify arbitrary database contents or remove bounded health checks. Shared validation still enforces a supplied finite budget, covered by a regression. No path, environment, schema, runtime root or tool is added.

Verification: original regressions two failed / two passed; final five regression cases pass. Related and mandatory guards: 148 passed, three skipped, 61.26 seconds. Real Lizard CCN is at most four across changed/new functions, Ruff and Gitleaks pass. The installed SQLite is 3.45.1, Python 3.12.3. Read-only candidate admission on the actual 268480512-byte coordinator passed in 0.0378 seconds; this is a component measurement, not a full hook latency guarantee.
