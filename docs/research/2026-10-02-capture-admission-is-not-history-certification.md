# Capture admission is not retained-history certification

Date: 2026-10-02. Scope: actor admission through the existing adopted Reliability V3 pair. No schema, database, directory, runtime root, environment contract, daemon, MCP tool, provider, or host timeout changes.

## Evidence

Actual Codex app-server 0.160.0 notifications recorded PostToolUse capture failures at 5001–5003 ms. The installed factory profile took 0.732 s opening the queue and 1.405 s opening the coordinator. SQLite execution accounted for 2.075 s: the adoption gate checked both entire files, and the coordinator's candidate constructor additionally certified every retained operation and cross-table ownership projection. The coordinator retained approximately 226 MiB of operational history. Deleting that history is neither an admission optimization nor allowed recovery behavior.

A regression uses SQLite's real authorizer to deny whole-file integrity pragmas and unrelated transaction/operation reads only during normal opening. The prior admission failed there. A second regression replaced the queue file after a successful cached admission; the prior cache tracked only the coordinator identity and accepted the replacement. Both failures were reproduced before correction. Negative checks independently require metadata and schema changes, and replacement of either database, to fail closed.

## Research, versions and choice

Independent primary sources checked on this date:

- [SQLite integrity checks](https://www.sqlite.org/pragma.html#pragma_integrity_check) distinguish complete structural and constraint checking from ordinary queries; foreign-key verification is separate.
- [PostgreSQL pg_amcheck](https://www.postgresql.org/docs/current/app-pgamcheck.html) exposes complete corruption checking as an explicit diagnostic operation, with its own execution controls. Current documentation is PostgreSQL 18; PostgreSQL is an analogy, not a proposed dependency.
- [MySQL CHECK TABLE](https://dev.mysql.com/doc/refman/8.4/en/check-table.html) documents a distinct table-checking operation and locking implications. MySQL 8.4 is an analogy, not a proposed dependency.
- [Python sqlite3](https://docs.python.org/3/library/sqlite3.html) documents read transactions and authorizer callbacks used by the regression. The installed runtime is Python 3.12.3 with SQLite 3.45.1; the implementation retains the Python 3.10 baseline and uses no newer transaction API.

Increasing the host timeout retains work proportional to all historical data on each new event. Dropping history breaks recovery and retention. Caching a supposed global integrity verdict across mutable database content would misrepresent certification. The chosen design follows the already existing normal queue-open contract: normal actors validate immutable adoption records, schema/integration digests, tombstones, retained immutable adoption references, both active file identities, schema completeness, and all connection metadata in a consistent read snapshot. Their active coordinator opens validate containment, connection contract, and schema without scanning unrelated historical rows. Cache identity includes both database files. The queue's paired coordinator uses the same normal path only for an adopted reader with its vault; explicit unpublished candidate paths retain full certification.

Operations continue to validate the records they actually mutate, canonical leases, tokens, fencing epochs, payload hashes, provenance, CAS preconditions, and recoverable artifacts. SQLite constraints and operational connection settings remain enforced. There is no successful integrity report fabricated by admission. Complete adoption certification, doctor, unpublished candidates and backup retain whole-file and foreign-key checks; complete coordinator certification additionally preserves operation and ownership cross-table invariants. A negative retained-operation test exposed that the standalone adoption diagnostic previously lacked those semantic checks: they now run explicitly in the full certification path on the same read transaction. Doctor's message names certification failure without claiming every normal actor was necessarily refused.

## Tradeoffs and qualification

Normal admission does not establish that every unrelated retained row is healthy. Latent corruption outside a requested operation belongs to explicit complete certification and health reporting; affected operations still fail closed. This boundary matches the existing queue contract and does not replace it with a new control plane. The complete runtime retention and deletion checks remain necessary and unchanged.

The first candidate profile against the installed databases took 0.594 s for queue admission and 2.735 s for coordinator admission. It removed the historical scans but encountered real writer contention: connection pragmas consumed 2.796 s and admission retried seven times. This sample does not establish overall latency improvement. No contention error, capture counter, quarantine, or retained record was suppressed. Native host timing and actual publication must be qualified after installation; the overall five-second timeout remains open until that evidence exists.

Initial related checks: 380 passed, 5 skipped. Mandatory guards: 64 passed. Additional full diagnostic/adoption/read-transaction/doctor checks: 198 passed, 3 skipped. Actual Lizard measurements and red/green regressions are retained under `logs/audit-2026-10-02-capture-admission-*`. These are scoped checks, not a full cross-platform matrix claim.

## Installed verification

The first installation attempt stopped before any code replacement because the old full coordinator opener encountered `database is locked`. Its preimages and failure are retained. The tested admission implementation then acquired the same canonical maintenance fence; complete adoption and coordinator certification ran under that fence before the verified code replacement and normal release. No competing process was killed and no ownership check was bypassed.

The installed factory pair subsequently measured 0.307 s for the queue and 0.010 s for the coordinator, compared with the earlier 0.732/1.405 s observation; these are individual live observations, not a universal performance guarantee. Four installed prompt/tool contention and recovery scenarios passed on separate adopted temporary vaults. Eight installed admission/certification scenarios passed, including changed metadata, missing schema, replacement of either file, and detection of a corrupt retained operation. No model call or live-knowledge test write was used. Native PostToolUse notifications after installation initially completed in 1910, 1090 and 426 ms; broader native event and contention qualification remains ongoing. Final mandatory guards: 64 passed; Ruff and Gitleaks passed; all 37 measured changed/new functions have CCN at most five.
