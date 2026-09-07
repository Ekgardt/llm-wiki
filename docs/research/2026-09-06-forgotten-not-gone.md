# Forgotten, not gone

Dated 2026-09-06. Written before changing what search returns.

## The state we are in

A page marked `status: archived` or `superseded` is dropped by `_collect_pages`
and never reaches any leg of retrieval. The file stays on disk and in git; it is
simply unreachable. `archive_stale`'s own documentation says "excluded from
active search", and a test pins that wording so nobody softens it by accident.

That exclusion is right for a general question. It is wrong for one case: asking
for the page **by name**. Today a question that names an archived page exactly
gets nothing back, and there is no way to learn from the system that the page
exists at all.

## What the biology says

A 2026 *Nature Neuroscience* result on *Drosophila*: forgotten memories persist
as **silent traces**, and a reminder cue recovers them — the same work shows
that manipulating the reminder can even create false memories, which is a
caution about what a cue is allowed to do. Forgetting there is regulated, with
named machinery (Rac1, the dopamine receptor DAMB, Scribble), not decay.

The shape to copy: stop retrieving, keep recoverable, restore **on cue** — and
keep the cue narrow, because a loose cue reconstructs things that were not there.

## What the field does with deprecated content

The same pattern, arrived at from the other end. Deprecated documentation is not
deleted: it is kept, marked, dropped from default results, and remains reachable
when asked for explicitly — GitHub's issue search excludes archived repositories
unless the `archived` parameter is given; Google leaves deprecated structured
data valid and simply stops featuring it, labelling the page rather than removing
it.

Excluded by default, reachable by name, and labelled when it comes back.

## The change

An archived or superseded page is returned **only** when the query names it: the
normalised query stem equals the page's filename stem, exactly, and no active
page already matched. Nothing else changes — it never enters a general ranking,
never competes on similarity, and cannot be surfaced by a topic.

The row carries `retired: true` so a reader can see what they were given.

**The cue is deliberately the narrowest one available.** A looser cue —
similarity, or a title match — would let history compete with current knowledge,
which is the thing archiving exists to prevent, and the fly work is a warning
about what a permissive cue reconstructs.

## Sources

- https://www.nature.com/articles/s41593-026-02381-2 — forgotten information
  persists as a silent trace and is recovered by a reminder cue
- https://www.nature.com/articles/s41586-020-03154-y — dopamine-based transient
  forgetting; forgetting as regulated machinery
- https://github.com/orgs/community/discussions/139239 — archived items excluded
  from search unless asked for explicitly
- https://developers.google.com/search/updates — deprecated documentation kept,
  labelled, and dropped from featured results rather than removed
