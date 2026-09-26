# A split day is archived with every part

Date: 2026-09-26. Audit 2026-09-26, finding B-1.

## What was wrong

A daily log larger than 16 KB is compiled in parts, and each part gets its own
`compile-receipt/v3` bound to that part's digest. The archiver looked for one
receipt for the digest of the whole file, found none, and kept the day forever.
Measured on the live vault at audit time: 17 such days, none with a whole-file
receipt. Checked 2026-09-26: the two days older than 90 days (2026-04-13,
2026-04-19) are each under 16 KB, and the nightly pass does not call the archiver,
so no split day has been refused yet. A refused day keeps its source; nothing is
lost either way.

## Decision

- One format version more, not a change of the old one: `archive-manifest/v1`
  stays exactly as it is for a day compiled whole, and every sealed v1 bag stays
  valid. A day with more than one part is written as `archive-manifest/v2`
  (`scripts/schemas/archive-manifest-v2.json`): `compile_parts` replaces the single
  `compile_receipt_ref`/`compile_authority`, one entry per part with its byte span.
- Each part's receipt is embedded as `compile-receipt-<n>.md` and listed in the tag
  manifest, like the single `compile-receipt.md` of v1.
- The reader (`scripts/evidence_resolver.py`) picks the schema by version, requires
  the listed spans to be exactly the day's own compile parts
  (`_daily_part_bounds`), checks every receipt against its part digest, and
  resolves a page's evidence by the part digest the page recorded.
- The bag walk is bounded by `MAX_COMPILE_PARTS`, the most parts a bounded day can
  split into; the schema's `maxItems` carries the same number and a test holds them
  equal.

## Source

RFC 8493, The BagIt File Packaging Format, fetched 2026-09-26 from
https://www.rfc-editor.org/rfc/rfc8493.html:

- §2.2.4: "A bag MAY contain other tag files that are not defined by this document."
- §2.2.4: "Implementations MUST perform standard checksum validation on any tag file
  that is listed in a tag manifest but MUST otherwise ignore their contents."
- §2.2.1: "Each tag manifest MUST list every payload manifest. Each tag manifest
  MUST NOT list any tag manifests but SHOULD list the remaining tag files present in
  the bag."

So several receipt tag files in one bag are within the format, as long as the tag
manifest lists each — which the writer does and the reader requires.

## Files

- `scripts/archive_daily.py`
- `scripts/evidence_resolver.py`
- `scripts/schemas/archive-manifest-v2.json`
- `tests/test_a_split_day_is_archived_with_every_part.py`
- `docs/STRUCTURE.md`
