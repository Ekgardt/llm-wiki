# Repeated historical evidence search

Status: installed and qualified, 2026-09-30.

The installed nightly pass twice exceeded its existing 120-second lint deadline.
A foreground reproduction completed only after several minutes; its 25-second
stack trace was hashing historical daily slices in `EvidenceResolver`.
The installed notes contain 1,835 references but only 130 distinct daily-source
digests. Each reference repeated the same search, including references elsewhere
on the same page. This is an operation-local computation problem, not grounds
to increase the maintenance deadline or skip evidence validation.

Sources checked on 2026-09-30:

- [Python hashlib](https://docs.python.org/3/library/hashlib.html): SHA-256
  identifies the exact bytes supplied; incremental hashing preserves the digest.
- [Git objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects):
  content-addressed identity separates content from mutable path names.
- [Google SRE overload](https://sre.google/sre-book/handling-overload/):
  distinguish useful work from repeated work and respect resource deadlines.

Chosen repair: retain historical slice offsets inside one resolver instance,
keyed by logical file, the SHA-256 of its current bytes, and the historical source
digest. Re-read the source and hash its contents on every lookup. Replace a
file's cached offsets when its contents change. Cache offsets, including an
unsuccessful search, rather than duplicate potentially large source buffers.
Continue checking every reference's block and byte range normally.

Alternatives: a larger timeout preserves the repeated work; timestamp-only
caching misses a same-size edit with restored timestamps; a persistent index
adds synchronization and recovery obligations. None is needed here. The chosen
cache is disposable, operation-local, and introduces no path, environment,
database, dependency, or numeric limit. Its size follows the actual references
and daily sources examined. SHA-256 identity remains the existing trust contract.

Regression coverage reproduces the duplicate search before the repair and checks
that same-size tampering with unchanged timestamps and a forged block still fail.
The related regression group passed 32 tests. Lint of the installed vault
completed in 60.45 seconds under the unchanged 120-second deadline. The
post-installation nightly pass completed at 17:34 UTC with zero failed steps.
See [the installation evidence](2026-09-30-durable-capture-installation.md).
These results establish this repair, not complete audit closure.
