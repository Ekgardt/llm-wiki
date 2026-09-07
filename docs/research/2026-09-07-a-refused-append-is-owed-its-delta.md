# A refused append is owed its delta — 2026-09-07

## The problem

`doctor` reported nine refused attempts "whose work never happened" for
days. Listed from the v3 coordinator: 114 quarantined attempts carry
`precondition_failed` (93 tool breadcrumbs, 19 project checkpoints, one
compile) and 3 carry `dlp_content_blocked`. Nine are open — closed neither by
a retry in their chain nor by their pages existing.

A daily append is a whole-file replace with a precondition on the bytes it
read. Two sessions append in the same second; the slower one's precondition
fails, its attempt is quarantined, and its block is written nowhere. The
refused transaction keeps both images.

## What was measured

Of the nine open attempts on 2026-09-07 evening, after decompressing the
images: six `post-tool` replaces of `knowledge/daily/2026-09-05.md` whose
before-image is a byte prefix of the after-image — deltas of 217–254 bytes,
each a `<!-- llm-wiki-operation:… -->` marker line and one breadcrumb block —
and two more of `2026-09-07.md` of the same shape (the first look at them
read the xz container instead of the bytes and called them "not a prefix").
One compile replace of `knowledge/index.md` is not append-shaped and is left.

## What current practice says

- Event stores reject an append when the stream changed since it was read;
  the handler reloads and retries, and replays deduplicate by event id
  (https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing).
- This vault already decided the retry shape: a quarantined attempt wrote
  nothing durable, so the next attempt is a **new operation** with the next
  ordinal that names the refused transaction as its parent
  (`knowledge/notes/idempotent-retry-after-quarantine-decision.md`), and
  `doctor` closes the refusal on exactly that lineage.
- The marker each block opens with is the event id: `locked_append_once`
  already refuses a second copy by it.

## Decision

`scripts/repair_refused_appends.py`: for each quarantined
`precondition_failed` transaction, every `replace` under `knowledge/` whose
before-image is a prefix of its after-image owes its delta unless the delta's
marker (or, without one, its bytes) is already in the live file. `--apply`
appends the delta through `append_knowledge` under the **original operation
id**, so the coordinator issues the next ordinal and the parent itself, and
`doctor` sees the lineage. Report-only by default, like the page-creation
repair beside it. Not replayed: replaces that changed more than their tail,
creates (the other repair's job), and DLP refusals (a decision, not an
accident).

The repair is a command, not a nightly step: a breadcrumb replayed days
later lands at the end of its day's file, out of order with the entries
around it, which is correct as evidence and worth a human's glance the first
few times.
