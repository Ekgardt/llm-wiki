# Keep locking diagnostics outside the sealed corpus ancestor

Research and decision date: 2026-10-01. The owner authorized the specific
relocation after receiving its explanation and impact. No general architectural
permission is inferred.

An actual two-connection SQLite locking probe created and removed a temporary
file in the runtime root. On an installed vault that root is also the corpus
root. The probe changed its ctime without changing device, inode or mode, and a
sealed corpus read refused the root. Actual live collection and explicit
`doctor --rebuild-generation` failed at that same ancestor; the active generation
was preserved. Repeated permission hardening also called chmod even when the
requested mode already applied, another unnecessary metadata mutation.

The shared probe now uses the existing `run/` on the same device. Runtime
initialization, including the language-server installer, prepares that directory.
Read-only diagnostics do not create an absent `run/`; the probe then reports
unavailable. The root and `run/` must be real directories. Ancestors use the existing
bounded-read policy: only verified system-owned symlinks to directories are
accepted; user-controlled links and Windows reparse paths are refused. The directory identity is checked before and after
the locking test, and cleanup declines a replaced directory. Permission
hardening verifies an already-correct mode without another chmod. These checks
retain the existing trusted-local-filesystem operating boundary; they do not
introduce an OS sandbox against hostile processes that can rename directories.

Alternatives: removing the corpus seal or the locking test would weaken safety;
more retries do not remove mutation by every cooperating writer; caching success
between processes adds invalidation state and may hide a changed filesystem.
Using the existing operational directory keeps actual SQLite verification on
the runtime filesystem without an extra directory, database, daemon, setting,
limit, or tool. The tradeoff is that an uninitialized read-only runtime cannot
perform the probe until initialization creates `run/`.

Primary sources checked on the decision date:

- [SQLite locking and rollback journals](https://www.sqlite.org/lockingv3.html)
  describe the two-writer exclusion and the journal beside the database. The
  supported runtime remains rollback-journal, not WAL.
- [Linux chmod](https://man7.org/linux/man-pages/man2/chmod.2.html) describes
  permission changes; applying an already-verified mode is unnecessary.
- [Python 3.10 filesystem operations](https://docs.python.org/3.10/library/os.html)
  define stat identities and no-follow metadata used by the checks. Python 3.10
  compatibility is retained.

Regression evidence includes the actual two-connection probe, unchanged sealed
root, no-directory read-only behavior, symlink and different-device refusal,
replaced-directory cleanup ownership, preservation of unrelated files, and
avoiding redundant chmod. Related tests retain permission and locking failures.
A successful code regression does not by itself establish a fresh installed
memory generation; that requires a separate actual rebuild and verification.

## CI qualification follow-up — 2026-10-02

The full-platform run exposed an actual shared-call defect: database opening
passed the database parent (`run/`) to the state-root initializer, which created
`run/run/`. A real backup therefore carried an unwanted empty directory.
The unchanged publication assertion failed with `(2, 2, (1, 1), False)` rather
than `(1, 1, (1, 0), False)`. A new actual-database test also failed before repair.

State-root initialization and database-parent validation now share preparation
but remain distinct operations. The former prepares existing `run/`; the latter
probes the actual database parent and creates no extra directory. Claims use the
same database-parent validation. Both still require the real two-connection
SQLite exclusion test, safe ancestors and verified directory ownership. This
adds no runtime layout, setting or resource limit.

The Windows repeated-cleanup test now removes the initialized empty `run/` with
`rmdir`, retaining handle-count, exact failure and complete parent cleanup
assertions. The root-seal regression calls the common seal verifier instead of
opening a POSIX directory descriptor directly on Windows. Its root identity,
actual SQLite exclusion and exact absence of residual probe files remain checked.
Windows execution must still be confirmed by CI; Linux success is not its proof.

An additional Windows 3.14 CI test exposed a fixture prerequisite: `copy2`
does not preserve the copied database's owner-only Windows DACL. A database
replacement test therefore hit the real security refusal before its intended
identity-change check. The replacement is now hardened through the existing
platform-aware helper and validated as an owner-only bounded database before
publication. The exact snapshot-change blocker assertion remains unchanged.
This corrects test setup; runtime permissions and deletion checks are unchanged.
The Windows failure log is retained; local qualification alone is not Windows
qualification.
