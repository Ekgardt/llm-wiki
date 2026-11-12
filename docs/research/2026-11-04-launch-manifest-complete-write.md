# A package launch requires the complete authored manifest

Date: 2026-11-04. Reproduced while qualifying the existing launch-package boundary.
The installed vault is unchanged; this correction is only in the working branch.

`_write_sealed_file` ignored the return count of one `os.write`. A controlled short
write returned success after 3 of 52 bytes. A second, independent reproduction
uses an actual POSIX `RLIMIT_FSIZE` in a child process after the verified server
snapshot has been prepared. Limits of 3 and 51 bytes both let `create_launch_tree`
return successfully with that many manifest bytes; zero capacity already failed.
Two new regressions failed, and fourteen existing/control cases passed.

The entry digest does not verify the separately authored package manifest, so
later verification of the server entry cannot detect this truncation. The caller
must finish writing the manifest before sealing and accepting the launch tree.
This is a false successful preparation, not a claim that malicious bytes executed.

Use the standard buffered binary writer over the already exclusively created
file descriptor. Its close flushes the complete buffered payload or raises; the
existing `finally` retains descriptor ownership with `closefd=False`. Existing
create-only flags, mode, bytes, cleanup, digest checks and runtime paths remain.
This adds no fsync/power-loss guarantee: this is disposable launch scratch, and
its existing durability contract is unchanged. No numeric retry bound is added.

The runtime publication owner already has its own complete-write loop, but
importing that owner here introduces unrelated publication dependencies. Copying
its loop duplicates a standard facility; moving publication code into the stable
reader changes that reader's responsibility. Standard buffered binary IO supplies
the required complete-write behavior without either ownership change. Binary mode
retains exact bytes on Windows; this package strategy itself is POSIX-only.

Primary sources checked on this date (three independent publishers):
- [Python binary/raw IO](https://docs.python.org/3.14/library/io.html): raw writes
  need return-count handling; high-level binary IO implements retries.
- [Linux write(2)](https://man7.org/linux/man-pages/man2/write.2.html): successful
  partial writes can result from file-size limits or storage exhaustion.
- [Microsoft CRT _write](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/write?view=msvc-170):
  returns a count/error and distinguishes binary from newline-translating text IO.
The Open Group page returned HTTP403 and is not counted as a checked source.

The child-only file-size limit and ignored SIGXFSZ exercise real kernel behavior;
they do not change pytest/the parent process limits or simulate process death.
Every refused preparation must remove its launch tree. Existing tests retain the
successful exact manifest, entry integrity, private modes, guard cleanup and
Pyright descriptor strategy. Native Linux/macOS CI is required for the new POSIX
regression; a Windows skip is not evidence for those platform semantics.

Evidence: logs/launch-partial-write-reproduction.json,
logs/launch-manifest-write-graph.json and logs/launch-manifest-write-red.txt.

Local qualification on Linux/Python 3.14.7: 22 launch/path checks passed;
194 launch/session/TypeScript checks passed with six managed-artifact skips.
An isolated, byte-verified copy of the existing pinned TypeScript artifact then
ran all ten TypeScript/identity checks successfully (7.52 seconds), replacing
those missing local observations without installing into or testing against the
live vault. The changed test file was reweighed with all 16 cases passing;
the weight-completeness guard, Ruff and actual complexity/shape checks for all
five changed functions passed. Full-suite and native macOS CI evidence for this
correction are still pending. Reports are retained under
`logs/launch-manifest-write-*` and `logs/launch-manifest-real-typescript.txt`.
