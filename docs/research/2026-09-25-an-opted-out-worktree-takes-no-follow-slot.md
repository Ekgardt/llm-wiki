# An opted-out worktree takes no follow slot

Date: 2026-09-25. Audit item C-43 (docs/AUDIT-2026-09-25-full.md).

## Fact
- `repository_worktrees.follow_worktrees` takes the first `MAX_FOLLOWED_PER_PASS`
  (8) of `unindexed_worktrees(rows)` and indexes each through `follow_worktree`,
  which calls `require_indexing_wanted` and refuses a worktree whose Git
  configuration turned indexing off (`indexing_marked_off`).
- `_indexable`, which builds that candidate list, ignores the opt-out. A worktree
  marked off is never indexed, so it is a candidate again every night; eight such
  worktrees fill every slot and the rest are never followed.

## Source (fetched 2026-09-25)
git-config documentation, https://git-scm.com/docs/git-config: "When reading, the
values are read from the system, global and repository local configuration files
by default" and "This command will fail with non-zero status upon error. Some exit
codes are: The section or key is invalid (ret=1)". The opt-out is a durable
configuration value the worktree carries, readable before any indexing, and an
unset key is an ordinary non-zero answer (`_config_bool` returns None for it).

## Decision
`_indexable` also excludes a worktree `indexing_marked_off` names, checked after
the cheap filters. The slots go to worktrees that can be followed. The refusal
path in `follow_worktree` stays for a mark set between listing and indexing.

## Uncertainty
Other persistent refusals (a symlinked path, a submodule) can still repeat; they
are rarer and not changed here.

## Files
- scripts/repository_worktrees.py
- tests/test_an_opted_out_worktree_takes_no_follow_slot.py
