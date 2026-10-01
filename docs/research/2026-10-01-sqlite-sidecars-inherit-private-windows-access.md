# SQLite sidecars inherit the runtime directory's Windows access

Inspected 2026-10-01, source `8046f245`. Windows CI job `110168007114`
failed the existing WAL migration scenarios at `_require_windows_owner_only`
for `queue-v3.sqlite3-wal`. This is an access check refusing its input, not a
reason to disable that check.

Codebase Memory traced the shared opener and migration; direct source reads
confirmed the dynamic SQLite file creation boundary absent from the graph.
`open_operational_db` calls `validate_state_root` on the database directory.
That validator used `_set_owner_only`, which returns False on Windows without
setting an ACL. The migration test's existing candidate builders restricted the
database files individually, but did not restrict the directory's inheritance.
SQLite creates its own WAL and SHM, including on a read-only reopen after the
last writable connection closed. File permissions on the main database alone
do not establish the Windows sidecars' permissions.

The normal adoption path separately restricts `run`, so these failures do not
establish that every installed Windows vault was unsafe. The shared root
boundary nevertheless promised restricted operational storage without applying
the existing Windows implementation. Both direct operational opens and the
candidate/migration path use that boundary. Backup/restore and interrupted
migration remain covered by the existing integration tests.

## Evidence and choice

Primary sources consulted 2026-10-01, three independent publishers:

- [Microsoft file security](https://learn.microsoft.com/en-us/windows/win32/fileio/file-security-and-access-rights)
  specifies that newly created files with default security descriptors inherit
  the parent directory's ACL; the main database's ACL is not that parent.
- [Microsoft ACE inheritance rules](https://learn.microsoft.com/en-us/windows/win32/secauthz/ace-inheritance-rules)
  distinguishes object and container inheritance. The existing hardener already
  grants only the owner `(OI)(CI)(F)` on directories and verifies the result.
- [SQLite WAL documentation](https://www.sqlite.org/wal.html) describes the
  adjacent WAL/SHM files and directory access needed to create them. The existing
  approved WAL-reset runtime gate and v3 protocol restriction remain unchanged.
- [Python chmod documentation](https://docs.python.org/3.10/library/os.html#os.chmod)
  states that Windows chmod changes only the read-only flag; POSIX mode bits
  cannot implement the directory DACL contract.

Use `_harden_runtime_owner_only` at the existing `validate_state_root` boundary,
before the locking probe or any database connection. On Windows this uses the
existing verified ACL implementation; POSIX retains the existing mode behavior.
This introduces no new access principal, path, dependency, journal mode, retry
limit or format. It also protects newly created descendant directories/files.

Rejected alternatives: hardening only the test fixture would leave the shared
opener's contract incomplete; repairing sidecars after SQLite writes would leave
an exposure window; skipping the owner check would accept unsafe files. The
Windows ACL subprocess cost is existing hardener behavior, now applied at the
previously incomplete directory boundary; no new cache hides ACL changes.

## Verification scope

New native-Windows regressions create ordinary nested children and real SQLite
WAL/SHM files and check their ACLs without hardening those files afterward. A
negative case explicitly grants another principal read access and requires the
unchanged sidecar validator to reject it. Existing WAL migration/restore tests
are unchanged. Ruff and per-function complexity checks run locally; native
Windows execution and the full relevant CI remain required before claiming the
fix verified. Linux skips do not count as Windows evidence.
