# Qualified WAL for adopted coordination databases

Date: 2026-09-30. Status: preparation authorized; live migration not qualified.

The installed CPython 3.14.6 uses SQLite 3.53.1. The adopted coordinator and
queue currently use DELETE journaling. A held read snapshot deterministically
prevents a DELETE writer's commit; the same transaction commits in WAL, while
both snapshots retain their original values and integrity checks succeed.
This is a concurrency mechanism, not proof that every historical hook timeout
has this cause. Full integrity scans remain a separate cost.

## Primary sources and alternatives

- SQLite: https://sqlite.org/wal.html — simultaneous readers and one writer,
  local shared memory, persistent sidecars and checkpoint costs. FULL preserves
  commit durability; NORMAL is not selected. The WAL-reset fix is required:
  3.51.3 onward, or documented 3.44.6 / 3.50.7 backports.
- APSW: https://rogerbinns.github.io/apsw/tips.html — independent wrapper guidance
  recommends WAL and explicit contention handling. Adopting APSW itself would
  add a dependency without removing the surrounding admission requirements.
- Python: https://docs.python.org/3.14/library/sqlite3.html — explicit transaction
  management and the online backup API. Existing stdlib connections can retain
  their interface and produce consistent snapshots while writes continue.
- Installed release: https://sqlite.org/releaselog/3_53_1.html.

Retaining DELETE avoids sidecars but keeps reader/writer commit interference.
Increasing hook timeouts only tolerates longer blocking and does not remove it.
Removing integrity checks weakens corruption detection and is rejected. A new
persistent writer service adds lifecycle complexity and is not needed for this
local two-database case. WAL/FULL is selected for qualification, retaining one
writer, canonical ownership, full validation, and existing default checkpoints.
Long readers can still retain WAL pages; WAL is not a cure for CPU or writer
contention. No arbitrary new limits or checkpoint tuning are introduced.

## Data path and acceptance

Native adapters admit evidence through coordinator/queue factories, adoption
validation, shared SQLite openers, canonical ownership, transaction receipts,
and Markdown projection. Existing openers force DELETE even on an already-WAL
database. Ordinary opens must preserve the persisted mode, accepting WAL only
for the two recognized v3 application headers on a fixed SQLite runtime.
Other operational databases retain their existing DELETE contract. Sidecars
must be private regular files under the runtime root; validation must not open
and close an extra descriptor that would drop SQLite advisory locks.

The adoption manifest pins the observed mode. A qualified offline migration
must coordinate both modes and that manifest under exclusive canonical
admission, with durable interruption recovery and verified rollback. Old
processes capable of forcing DELETE must be stopped before cutover. Online
backup, staged restore, corruption rejection, held-reader writes, and already
started/new captures must pass. Public CI alone does not satisfy this contract.

Codebase Memory identified shared opener consumers in ownership, transactions,
queue, claims, generation catalog, telemetry, doctor, repair and MCP readers.
Backup uses sqlite3.Connection.backup rather than copying active database bytes.
Graph edges include heuristic false positives; SQL, sidecars and manifest
bindings must additionally be checked against source and runtime evidence.

## Local qualification evidence

The five regression cases failed on bea86752: ordinary writers downgraded WAL,
read-only opens rejected it, and an unrelated database was silently downgraded.
The mode-preserving candidate passes held-reader commits for both protocol IDs,
unsafe-version rejection, foreign-contract rejection, sidecar symlink/permissions/
hardlink refusal, and online backup of an uncheckpointed committed value.
The related durability/adoption/backup/structure suite passed 199 tests with
three existing platform skips before the final four hardlink cases were added.
Lizard reports no CCN above five in either changed Python file; AST inspection
confirms at most two if statements and at most two nested loop/if levels in every
changed function. These are candidate checks, not an installed migration proof.

A further dependency is confirmed: the adopted manifest pins journal_mode=delete;
its schema digest is also bound by the migration descriptor and both tombstones.
Changing only the database or expanding the JSON schema would invalidate existing
adoption. Their coordinated transition, failure recovery, full adopted-vault
backup/restore, and restarting old mode-forcing processes remain required before
installation/cutover. No live database has been switched by this change.

## Restore qualification exposed an existing publication defect

A real adopted-vault backup published to new roots fails normal client admission
in DELETE mode too: publication used default umask permissions, and copied
adoption file identities still name the original inodes. Successful archive
validation alone was insufficient. Publication must create private files directly,
then explicitly rebind only physical artifact identities to the verified copies,
validate the complete adoption contract, and publish the rebound record. Source
hashes, paths, protocol/schema versions and semantic records are not relaxed.
An unchanged repeated publication must recognize this verified identity rebinding;
all other destination content conflicts remain refusals. This is part of the
required full restore qualification, not evidence that WAL is installed.

Additional primary references for restoration, checked 2026-09-30:
https://docs.python.org/3.14/library/os.html#os.stat_result documents filesystem
identity and file permission fields; https://restic.readthedocs.io/en/stable/050_restore.html
documents restoration into a target directory and verification. Neither promises
that a copied file preserves the source inode. Preserving source hashes and
validating newly bound physical identities is therefore required by this product's
stronger adoption contract. SQLite snapshot sidecars are settled by SQLite itself;
no potentially live WAL is manually deleted.

The migration candidate passed interruptions at all seven publication boundaries
in both directions, and abrupt child-process exit at database, migration-record,
and adoption-record boundaries. Restart respects the existing expired-and-proven-
dead ownership rule. Held live owners block migration before any mode switch.
Restored DELETE and WAL vaults now pass full normal admission and a real new Markdown
transaction. Repeated publication and content-conflict refusals remain tested.
The v1 adoption schema is retained for unmigrated vaults and rollback, not as an
unused duplicate; removing it requires retiring that active compatibility contract.

A cached queue object exposed a gap in an admission-only migration fence: its
normal enqueue method could still open a writer while the marker existed. The
regression failed before adding the marker check to shared runtime writer opens.
Only the explicit offline migrator opts into writing under the pending marker;
normal cached clients remain blocked. After resume, the pre-migration queue task
retains its dedupe identity and new tasks enqueue normally. A prepared Markdown
transaction also applies after migration, and a committed operation retains its
original receipt identity. Last related run: 387 passed / 3 existing platform
skips; subsequent fence changes: 25 migration tests and 176 related tests passed.

## Last-connection unlink race, 2026-09-30

A restarted compiler refused admission with the sidecar hard-link diagnostic.
Current files had one link. An isolated real SQLite open/close loop plus concurrent
lstat reads reproduced the missing case: in a ten-second sample, 463127 reads
reported one link, 304242 reported absence and 4553 reported zero links. SQLite
removes its WAL/SHM files after the last connection closes; Linux can resolve the
inode before unlink and finish metadata collection after its link count reaches
zero. That is not an additional hard link. The failed live invocation did not log
the numeric count, so the isolated reproduction establishes the defect without
claiming that missing historical value was recorded.

Reject counts above one, preserving regular-file, private-owner permissions and
pre/post-open checks. No retry loop, sleep, sidecar deletion or lock-bearing
file descriptor is added. Deterministic regression supplies real fstat metadata
from an unlinked private file at the lstat seam, then exercises real reader/writer
opens for both operational application IDs and both sidecar suffixes. It fails
on the original additional-hard-links error. Existing genuine hard-link and
symlink refusal cases remain required.

Primary references checked 2026-09-30:
- https://sqlite.org/wal.html — last-connection checkpoint and sidecar lifecycle.
- https://man7.org/linux/man-pages/man2/stat.2.html — metadata observations need
  not describe one atomic instant.
- https://man7.org/linux/man-pages/man2/unlink.2.html — inode lifetime after its
  last name is removed. The two Linux pages are one independent source family.
