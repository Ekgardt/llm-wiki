# A fix arrives when the checkout moves

Date: 2026-09-26. Audit 2026-09-26 item B-28 (a regression of the A-5 redrive).

## Fact
- The nightly gives dead captures one redrive "once the code changed after they
  died" (`scheduled_nightly._dead_capture_redrive_steps`), and measures "changed"
  as the committer time of HEAD (`git log -1 --format=%cI`).
- A fix committed on Monday 10:00 and fast-forwarded into the vault by Tuesday's
  nightly is later than every capture that died on the old code between Monday
  10:00 and the update, so exactly those never get their redrive — the window in
  which the bug was live in this vault.

## Source (fetched 2026-09-26)
git-reflog documentation, https://git-scm.com/docs/git-reflog: "Reference logs,
or "reflogs", record when the tips of branches and other references were updated
in the local repository." The time HEAD moved in this checkout is the time the
fix arrived here. Checked on the live checkout (read-only):
`git reflog -1 --date=iso-strict --format=%gd HEAD` →
`HEAD@{2026-09-25T13:39:49+00:00}`.

## Decision
The redrive uses the time of HEAD's newest reflog entry; when the reflog cannot
answer (disabled, pruned), it falls back to the commit time as before.

## Files
- scripts/scheduled_nightly.py
- tests/test_a_fix_arrives_when_the_checkout_moves.py
- tests/test_a_dead_capture_gets_its_second_chance_after_a_fix.py

## Follow-up (2026-09-26): git may spell UTC as `Z`

Fact: CI run 36217815665 (linux, Python 3.10) failed with
`ValueError: Invalid isoformat string: '2026-09-26T04:27:04Z'`: that runner's git
printed the reflog time with `Z`, which Python 3.10's `datetime.fromisoformat` does
not read (3.11 does); the local git printed `+00:00`. The same string is passed on as
`--changed-after`. Decision: `head_arrival_time` returns `+00:00` for a trailing `Z`.
