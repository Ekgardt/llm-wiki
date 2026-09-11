# A refused compile of a day since compiled is history

Date: 2026-09-11. Trigger: the doctor has reported `transactions: error —
1 refused attempt(s) whose work never happened` on every run since
2026-08-25 (`logs/doctor-report.json`), and the session-start block repeats
it to the owner.

## What the attempt is

Transaction `cb387b96…` (operation `compile:75bccb…`, 2026-08-25 12:40Z,
`dlp_content_blocked`) meant to create eight compile receipts
(`knowledge/daily/receipts/v3-…`) plus the index and log. Its `after`
artifacts under `run/transactions/cb387b96…/after/` show every receipt names
`knowledge/daily/2026-08-25.md`, at eight successive snapshots of that day
(sha `bee1eded…` 14 188 bytes … `b6b86f9b…` 16 293 bytes). None of those
receipts was ever written — those snapshots no longer exist; the day kept
growing and was rewritten since. Checked on 2026-09-11 with the compile's
own part rule (`evidence_resolver._daily_part_bounds`): every part of the
day as it is now carries a committed v3 receipt. The work the refusal
stopped — compiling that day — happened; only the refused snapshots' own
receipts are missing, and they never can appear.

The doctor's three proofs (`_unresolved_quarantine`): a committed retry in
the same chain, a commit of the same base operation identity, or every
intended create written by a commit. A compile of a day that has changed
since matches none of them, so the finding can never clear.

## Sources

1. `knowledge/notes/self-resolving-health-findings-decision.md` and the
   doctor docstring: quarantine is retained evidence; a finding describes a
   live condition, and "a health check that is always red stops being read".
2. Compile semantics in this repository: a day is compiled when every part of
   its current bytes has a committed receipt (`compile_memory.daily_is_compiled`,
   `compile_source_identity` = sha256 of `[logical_path, part_sha256]`);
   older snapshots are never compiled again by design.

## Decision

A fourth proof, narrow on purpose: a refused attempt whose intended creates
are **only** compile receipts is history when (a) every receipt artifact it
staged parses as a compile receipt naming a day under `knowledge/daily/`,
and (b) every part of each named day's current bytes has a committed receipt
create in the transaction database. Anything unreadable, a staged receipt
outside the daily directory, a day that is missing, or one uncompiled part
keeps the finding. The proof reads only bounded runtime artifacts and the
day files; it writes nothing.

`_transaction_check` gains the vault root it needs to read the day files.

Files: `scripts/doctor.py`, `tests/test_doctor.py`, `CHANGELOG.md`.
