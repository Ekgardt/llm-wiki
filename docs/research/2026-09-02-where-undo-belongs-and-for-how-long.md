# Where undo belongs, and for how long

Dated 2026-09-02. The owner asked whether we should keep this data at all, and
where it should live if we should.

## The confusion, named

We are using one mechanism for two jobs that every mature system keeps apart:

- **Crash consistency** — if a write dies halfway, put the file back the way it
  was. Needed for seconds.
- **"Undo what I did last Tuesday"** — restore an earlier state on purpose.
  Needed for weeks.

Our transaction trail does both with the same artefact: a full copy of the file
before and after every write, kept for thirty days. That is why it is 4.58 GB.

## What the field does with each

**Crash consistency is transient everywhere.** In Oracle, undo lives in a
separate tablespace and `UNDO_RETENTION` is a **minimum in seconds**; rollback
segments are explicitly reusable, and new transactions take over the space of
committed or aborted ones. In PostgreSQL the old row versions sit in the table
and **autovacuum deletes them as soon as no transaction needs them**. The rule
above both: a log record for an active transaction may not be discarded until
that transaction commits or rolls back — and after that, it may.

Nobody keeps undo data for thirty days for crash safety. It exists to finish or
unwind a write in flight, and then it is garbage.

**Restoring an earlier state is a backup problem, not an undo problem.** In
PostgreSQL that is point-in-time recovery: a base backup plus archived
write-ahead log, replayed to the moment you want — and the documentation is
blunt that once a transaction commits, rollback cannot undo it. The commercial
practice around it is equally settled: the 3-2-1 rule — three copies, two kinds
of media, one off-site — is still cited in 2026 as the minimum acceptable
architecture by CISA's ransomware guide and NIST SP 800-209, with
grandfather-father-son retention balancing storage against how far back anyone
actually needs to go.

## What that means here, concretely

**Should we keep it?** Two answers, because they are two things.

The before-image is needed **until the write commits**, and after that it is
waste. Today it is kept for thirty days. Dropping it at commit removes roughly
half the trail immediately and matches what every database does.

The ability to go back a week is worth keeping — but as a **backup**, not as a
pile of per-write copies. That is what Restic is for, and the audit-closure
contract of 2026-08-15 already names Restic for encrypted vault recovery. It
deduplicates by content and compresses; our own measurement says the same
history would be 0.47 GB instead of 4.58.

**Where should it live?** Three separate places, by job:

1. **In flight** — the before-image, inside `run/`, deleted at commit. Seconds,
   megabytes.
2. **Recent history** — a daily Restic snapshot of `knowledge/`, kept locally.
   Content-addressed, deduplicated, and already the approved tool.
3. **Off the machine** — the same snapshot repeated somewhere else, because
   3-2-1 is about surviving the disk, not surviving a bad write. We have no
   off-machine copy of the private knowledge at all today. That is a bigger hole
   than the 4.5 GB, and nothing in the current design covers it.

**Why not git for the history?** It would work and it is already a dependency,
but it earns its keep only for the text tree, and it brings a real hazard: this
vault's own repository has a public remote. A second, local-only repository
inside `run/` has no remote and cannot push, but it is one more store to
maintain beside a backup tool that already does the job better and encrypts.
Restic is the better answer for the same reason it was chosen in August.

## What I would propose, in order

1. **Drop the before-image at commit** instead of at thirty days. Half the
   trail, no loss of any guarantee that anybody relies on, matches Oracle and
   PostgreSQL both.
2. **Deduplicate and compress what remains**, since it is still whole copies of
   an append-only journal.
3. **A daily Restic snapshot of `knowledge/`**, which is what actually answers
   "put it back the way it was last Tuesday" — and, unlike today, would survive
   the disk dying.

The third is the one that matters most and the one we do not have.

## Sources

- [Oracle vs PostgreSQL: How Undo Tablespace and MVCC Handle Updates and Deletes](https://medium.com/@vahidusefzadeh/oracle-vs-postgresql-how-undo-tablespace-and-mvcc-handle-updates-and-deletes-358e29ae576c)
- [Deep Dive into Oracle Undo Tablespace Management in 19c](https://oracle-dba-help.blogspot.com/2025/03/deep-dive-into-oracle-undo-tablespace-management.html)
- [Well-known Databases Use Different Approaches for MVCC — EDB](https://www.enterprisedb.com/blog/well-known-databases-use-different-approaches-mvcc)
- [PostgreSQL: Continuous Archiving and Point-in-Time Recovery](https://www.postgresql.org/docs/current/continuous-archiving.html)
- [Postgres Rollback Explained — Bytebase](https://www.bytebase.com/blog/postgres-rollback/)
- [Write-ahead logging and the ARIES crash recovery algorithm](https://sookocheff.com/post/databases/write-ahead-logging/)
- [What Is the 3-2-1 Backup Rule? A Complete 2026 Guide — AvePoint](https://www.avepoint.com/blog/backup/3-2-1-backup-rule)
- [What is GFS Backup Retention Policy? — Vinchin](https://www.vinchin.com/disaster-recovery/gfs-backup-retention-policy.html)
- Our measurement: `run/transactions`, 4.58 GB, 50% exact duplicates, 79%
  compressible
- Our contract: Restic named in the audit-closure decision of 2026-08-15
