# Forgetting by use, not only by age (2026-08-25)

## The question

`archive_stale.py` moves a page out of the active tree when its type's window
expires: 60 days for debugging notes, 180 for patterns, 365 for workflows and
Q&A. Age is the only input. A page nobody has ever opened and a page answered
from twice a week last month are treated identically. `MEM-06` asks for the
minimal honest form of forgetting by usefulness.

## What the practice says

The 2026 surveys describe forgetting as reducing the influence of obsolete or
*low-utility* entries, and note that practice implements it with recency decay
or importance thresholds, sometimes learned under a resource constraint
([Memory for autonomous LLM agents][mem]; [Rethinking memory mechanisms][rethink]).
Baselines named in that literature are retain-all, fixed-window,
least-recently-used, permanent-delete and oracle-context — LRU sits exactly
between "keep everything" and "delete by clock".

The strongest recent framing for our case is reversible forgetting: memory has
*active*, *dormant* and *retired* states, and a **reactivation** transition
restores dormant knowledge when its relevance returns ([Towards reversible
forgetting][reversible]). Permanent deletion is explicitly not the goal.

## What this vault does

- Age still opens the question, but use answers it: a page past its window that
  was **retrieved within that same window** stays active. Retrieval is already
  recorded per page (`retrieval_telemetry` plus the legacy access log), so this
  needs no new signal.
- A page that goes says why in one line: the window it passed and that nothing
  read it in that time.
- Archiving stays a move, never a delete — the vault's "dormant" state — and
  `--restore <slug>` is the reactivation transition the literature asks for.
- No learned policy, no scoring model. The one number that decides is "was this
  read since its window opened", which the operator can check by hand.

## What is deliberately not done

No permanent deletion, no LRU eviction under a size cap (the vault has no size
pressure), and no automatic reactivation: a page comes back because someone
asks for it, so a retrieval of an archived page cannot silently resurrect it.

[mem]: https://arxiv.org/html/2603.07670v1
[rethink]: https://arxiv.org/pdf/2602.06052
[reversible]: https://arxiv.org/abs/2608.18177
