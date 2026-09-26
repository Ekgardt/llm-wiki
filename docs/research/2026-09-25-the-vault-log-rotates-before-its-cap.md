# The vault log rotates before its cap

Date: 2026-09-25. Q8 (legacy/time-bomb sweep).

## Fact
- `compile_memory` rewrites the whole private vault log
  (`knowledge/log.local.md`) inside each compile transaction and refuses when the
  after-image passes `MAX_LOG_BYTES` (4 MiB): "knowledge log exceeds after-image
  limit". From then on every compile that touches a page fails.
- The live log was 427 705 bytes on 2026-09-25 and grows about 4.5 KB a day, so
  the refusal is roughly two years away; nothing rotates it.
- `query_memory.append_log` appends through `append_knowledge`, bounded by the
  transaction layer's 64 MiB target limit, so only compile has the 4 MiB wall.
- Every scanner that walks knowledge pages walks `knowledge/notes` only, so a
  file elsewhere under `knowledge/` is invisible to search, index, lint and
  impact.

## Source (fetched 2026-09-25)
logrotate(8), https://man7.org/linux/man-pages/man8/logrotate.8.html:
"size size — Log files are rotated only if they grow bigger than size bytes."
Rotation by size, before a hard limit, is the ordinary answer to a growing log.

## Decision (logged with `log_decision`)
- When the compile's after-image would pass `LOG_ROTATE_BYTES` (2 MiB, half the
  cap), the same compile transaction creates
  `knowledge/log-archive/log.local.<YYYY-MM-DD>.md` with the whole current log and
  replaces `knowledge/log.local.md` with the header, one line naming the archive,
  and the new entry. One transaction: the archive exists exactly when the log was
  cut, and an undo restores both.
- `knowledge/log-archive/` joins the transaction allowlist and is denied in
  `.gitignore`, like the log itself.
- The session-start block reads the last dated entries of the current log; right
  after a rotation it shows fewer, which is acceptable.

## Uncertainty
Two rotations on one day would collide on the archive name; a rotation leaves a
log of a few hundred bytes, so a second one the same day needs 2 MiB of entries
in a day. The create precondition refuses rather than overwrites if it happens.

## Files
- scripts/compile_memory.py
- scripts/markdown_transaction.py
- .gitignore
- docs/STRUCTURE.md
- tests/test_the_vault_log_rotates_before_its_cap.py
