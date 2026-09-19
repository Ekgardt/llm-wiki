# A retired generation does not come back

Dated 2026-09-17. Third audit, finding G-M6. The research before the fix.

Files: `scripts/generation_catalog.py`,
`tests/test_a_retired_generation_does_not_come_back.py`.

## What was found

- `GenerationCatalog.discard_unactivated` works in two steps. The registration is deleted and
  committed under the catalog's own cleanup fence; the tree is removed in a second
  transaction under the caller's deadline. When the second step does not happen — the
  caller's deadline has passed, the lock times out, the filesystem refuses — a complete,
  valid tree with no row remains. The existing tests pin exactly that outcome (row gone,
  directory present) and it is a sound one for an aborted build, whose tree is incomplete.
- A generation retired by `repository_retention` is complete. `recover_orphans` (run by the
  doctor) registers every complete unregistered tree again, with `registered_at = now`, and
  "newest registration holding `code_roots`" is how every code reader and the next build's
  parent are chosen. The retired, older generation becomes the one code answers read.
- Reproduced on the product path, no fault injection: register a generation, call
  `discard_unactivated` with a deadline that has already passed (`TimeoutError`, row gone,
  tree present), then `recover_orphans()` returns it.
- Since G-H1 the retention pass also retires the vault's code generations every night, so
  the window is met more often than before.

## Practice on this date

- unlink(2): "unlink() deletes a name from the filesystem. If that name was the last link
  to a file and no processes have the file open, the file is deleted and the space it was
  using is made available for reuse." (https://man7.org/linux/man-pages/man2/unlink.2.html,
  fetched 2026-09-17). One name, one call: there is no half-done state, and no deadline is
  needed for it, unlike the recursive removal of a tree of hundreds of megabytes.
- The catalog admits a tree only through its `manifest.json` (`_register_shaped` reads and
  hashes it first). A tree without it is not a publication; the doctor's repair removes such
  an orphan once no writer has touched it for a day.

## The decision

- Between the two existing steps `discard_unactivated` unseals the tree: under the same
  cleanup fence as the row delete, and only when nothing references the generation any more
  (the check the tree removal already makes, so a registration that raced in is left
  whole), it unlinks the generation's `manifest.json`. A generation directory that is itself
  a link is left alone, as the tree removal treats it.
- Whatever then stops the tree removal, what is left can no longer be recovered as a
  generation. The next discard of the same id, or the doctor's orphan repair, finishes it.
- Not fsynced on purpose: after a power loss the name may reappear, which is today's
  behaviour in a rarer case, and the existing fsync contract of this method (one fsync, of
  the generations directory, after the tree is gone) stays as pinned.
- The related low point (`_write_transaction` raising `TimeoutError` after the body of
  `discard_superseded` removed the tree) heals through `_interrupted_discards` and is not
  changed.
