# Adoption identity checks retain SQLite locks

Research date: 2026-10-02. The installed operational databases use rollback
journals and synchronous FULL. Python 3.12 runs this vault; Python 3.10 remains
the compatibility baseline. This change preserves existing paths, schemas,
admission, ownership and resource policies.

An identity probe opened and closed an independent descriptor for a mutable
operational database. On POSIX, closing any descriptor for an inode releases
all traditional record locks held by that process for that inode. An existing
SQLite connection therefore silently lost its writer lock. The same probe was
used inside Doctor's read snapshot. A real second process reproduced both
failures: BEGIN IMMEDIATE was correctly refused before the probe and incorrectly
admitted after it. The adoption regression first failed; after that correction,
the Doctor case independently failed while the adoption case passed.

Mutable database identity now uses validated contained metadata and the same
platform identity representation, without a separate POSIX open/close. Existing
SQLite openers still verify file identity around SQLite connect. Immutable
artifacts retain their strict descriptor and digest checks. This is cooperative
local-file protection, not an operating-system sandbox against hostile swaps.
The alternative of wrapping a raw probe in another application lock would not
protect every SQLite connection; changing VFS or journal mode would expand scope
and conflict with the installed operational contract.

At 22:46:44 UTC the capture log first reported a malformed queue. A coherent
online diagnostic copy later reproduced a duplicate page reference under root
page 4, the attempt_history table. The transaction database's quick_check returned
ok. The defect above provides a demonstrated corruption mechanism, but does not
prove the historical cause of this particular damaged page. The first damaged
copy, recovery SQL and a separate recovery candidate are retained privately.
Recovery candidate integrity and foreign-key checks pass, but it is not installed
or certified complete. SQLite warns that recovery can lose, resurrect or alter
records. Replacing the live queue or clearing its history is not part of this
code correction. Five-second host timeout repair and complete audit closure
remain separate unfinished work.

Sources checked on the research date:

- [SQLite corruption causes](https://www.sqlite.org/howtocorrupt.html), especially
  POSIX locks canceled by a separate descriptor close.
- [Linux close(2)](https://man7.org/linux/man-pages/man2/close.2.html), the process-wide
  record-lock release rule.
- [Python sqlite3](https://docs.python.org/3.10/library/sqlite3.html), real SQLite
  transactions and online backups.
- [SQLite recovery](https://www.sqlite.org/recovery.html), recovery limitations
  and separate candidate validation.

Validation: original adoption regression 1 failed; subsequent Doctor regression
1 failed and 1 passed. The initial related run passed 88 tests with 3 skips.
Further checks and installed evidence are recorded in private audit logs;
this page does not assert whole-suite qualification or repaired queue contents.
