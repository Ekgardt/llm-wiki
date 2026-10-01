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
