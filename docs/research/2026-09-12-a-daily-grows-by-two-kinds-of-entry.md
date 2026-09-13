# A daily grows by two kinds of entry, and only one was searchable

Date: 2026-09-12. Trigger: the owner asked to clear every remaining problem.
`doctor` reported `claims (degraded)`: two claims on
`knowledge/notes/git-autocrlf-stderr-warning-parsed-as-diff-record.md` cite
daily bytes that no longer resolve under their recorded digest.

## What the evidence line promises

A compiled page cites its source as a byte range of a daily plus the SHA-256 of
the daily **as it was when the page was compiled**:

```
daily:2026-09-11 sha256:1110fdb8…2648 block:09:20:13 bytes:1927-2127
```

A daily is append-only, so that digest stops matching the file within hours.
`scripts/evidence_resolver.py` exists for exactly this: it rebuilds the
historical slice by hashing the file forward from the start to each place a
slice could have ended, and a match proves the cited bytes are still the bytes
the page quoted.

## Why these two did not resolve

The candidate ends come from `_daily_entry_offsets`, which finds one thing: the
transaction marker `<!-- llm-wiki-operation:`. Measured on the live vault today,
`knowledge/daily/2026-09-11.md` (10 663 bytes) holds **one** such marker, at
byte 39 — and **five** session blocks, at 799, 4063, 5362, 6936 and 8465. The
digest those two claims recorded is the digest of the file's first 5 362 bytes:

```
sha256(content[:5362]) == 1110fdb8a27e63fe749d258a1e1dcb32b369937134f150603beadbaa9a142648
```

5 362 is a block start, not a marker, so it was never a candidate and the slice
was never found. The bytes are still there, unchanged; only the search was
blind to the boundary they end at.

Two kinds of entry reach a daily: the transactional appender writes its marker,
and the capture path writes a `## [HH:MM:SS] …` block without one. The resolver
knew about the first kind only.

## Decision

The slice search takes both boundaries: the transaction markers it already
uses, plus the start of every `## ` block. One function is added for the slice
candidates; `_daily_entry_offsets` keeps its own meaning, because
`_daily_part_bounds` splits a day for compilation by it and that split must not
change.

Nothing about the recorded evidence changes — no rewriting of pages, no new
field, no migration. A digest that matched before still matches; two claims
that were unresolvable become resolvable, and so does every future claim whose
slice ends at a capture block.

## Rejected

* **`doctor --repair` alone.** It rebuilds the index against the current
  dailies and would leave these two claims unresolved, because the boundary
  they need is still not searched. The repair is the right second step, not the
  fix.
* **Recompiling the page.** It would rebind the evidence to today's bytes and
  hide the defect, which every later page would hit again.
* **Hashing every byte offset.** 10 663 candidates for one day, and a day may
  be far larger; the existing ceiling of 4 096 candidates exists for that
  reason.

Files: `scripts/evidence_resolver.py`, `tests/test_evidence_resolver.py`.
