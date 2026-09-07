# An undo trail that outgrew its purpose

Dated 2026-09-05. Measured on the live vault during today's audit.

## The number

`run/transactions` is **5.3 GB**: 6904 transaction directories, median **700 KB**
each, largest 5 MB. Nothing in it is prunable — every one is inside the two-day
undo window that `memory-keeps-a-second-copy-decision` already narrowed from
thirty days. So the window is not the problem any more. The size of what goes
into it is.

Every write stores a full copy of the target before and a full copy after. The
targets are append-only journals, so the pair is two nearly identical copies of a
file that grows all day, and appending 1 KB to a 500 KB journal costs 1 MB.

## What was measured, not assumed

Sampling 600 transaction directories, 1412 images, 470 MB:

- **95% of images are distinct.** Content-addressed deduplication, which would
  have paid in August when half were byte-identical, no longer pays much.
- **The bytes compress to 25% with zlib and 9.7% with lzma at preset 1**, the
  latter taking 0.21 s for 7.7 MB. Both are in the standard library.

`compression.zstd` would be the modern default — the field's advice is zstd
level 3 for logs and archives — but it arrives in Python 3.14 and this project
supports 3.10 upward. `lzma` is stdlib everywhere we run and compresses better
here than zstd-3 typically does, at a speed that does not matter for a write path
already doing an fsync.

## What the field does

Write-ahead logging keeps a before value to undo and an after value to redo, and
the resulting write amplification is a known, named cost — the literature
measures it as a factor of the number of writes and treats *reducing what is
written*, rather than shortening retention, as the remedy. Compression of log and
archive tiers is the ordinary answer, and stores that cannot compress in software
push it into the device.

Nothing here is novel. What is worth writing down is that we already narrowed
retention once, and that the second lever — the size of a record rather than
their number — was still untouched.

## Decision to implement

Compress each image with `lzma` at preset 1 when it is staged, and decompress
transparently on every read. The recorded `sha256` stays the hash of the
**plaintext**, so every existing verification, rollback and abort path checks
exactly what it checked before.

Old uncompressed images stay readable: the reader decompresses only when the
file begins with the lzma magic. Nothing has to be migrated, and a vault written
by an older build still restores.

Expected: 5.3 GB → roughly 500 MB, with no change to what undo can restore.

## What this does not fix

The append-only shape. A delta against the before-image would cost bytes rather
than kilobytes, and would be the right answer if compression turns out not to be
enough. This is the cheaper half, taken first because it is transparent and
because it does not change what a transaction record *is*.

## Sources

- https://sookocheff.com/post/databases/write-ahead-logging/ — before and after
  images, and why both are kept
- https://scaleflux.com/blog/reduce-write-amplification-write-ahead-logging/ —
  WAL write amplification and reducing what is written
- https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-transaction-log-architecture-and-management-guide
  — transaction log architecture and management
- https://docs.python.org/3/library/compression.zstd.html — zstd in the standard
  library from Python 3.14, which is why it is not used here
- https://www.rfc-editor.org/rfc/rfc8878 — Zstandard, for the comparison
