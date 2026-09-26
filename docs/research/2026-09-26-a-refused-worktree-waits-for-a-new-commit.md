# A refused worktree waits for a new commit

Date: 2026-09-26. Audit 2026-09-26 B-12.

## Facts

- `repository_worktrees.follow_worktrees` takes the first `MAX_FOLLOWED_PER_PASS`
  (8) worktrees without a generation each night. C-43 took opted-out worktrees
  out of the candidates, but a worktree refused for any other reason (missing code
  roots, bounds) stayed a candidate and was refused again every night, so eight of
  them held every slot and a new worktree was never followed.
- A refusal at one commit is the same refusal at that commit: nothing in the
  checkout's history changed. RFC 2308 (https://www.rfc-editor.org/rfc/rfc2308.html,
  fetched 2026-09-26) on negative caching: it "reduces the response time for
  negative answers" and "reduces the number of messages" — remembering a negative
  answer until what it depends on changes.

## Decision

- Each refusal is remembered in `run/state.json` under `worktree_refusals` as the
  worktree's path and its `HEAD` at that moment, at most 256 entries. A worktree
  whose `HEAD` still matches is not asked again and takes no slot; a new commit, or
  a HEAD that cannot be read, makes it a candidate again; a followed worktree is
  forgotten. No new runtime file is added.

## Files

- `scripts/repository_worktrees.py`
- `tests/test_a_refused_worktree_waits_for_a_new_commit.py`
- `CHANGELOG.md`
