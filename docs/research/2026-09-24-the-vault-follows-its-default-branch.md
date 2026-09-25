# The vault follows its default branch

Dated 2026-09-24. Audit item B-3 (`docs/AUDIT-2026-09-24-live.md`).

Files: `scripts/self_update.py`, `tests/test_the_vault_follows_its_default_branch.py` (new),
`CHANGELOG.md`, `docs/research/2026-09-24-the-vault-follows-its-default-branch.md`.

## What was found

- The live vault's checkout is on branch `work`, which tracks `origin/work`. The nightly
  update (`self_update._update_target`) fast-forwards the *checked-out* branch from its
  tracking remote, and only to a commit the default branch already holds
  (`_outside_default_branch`). After a pull request merges, `origin/main` gains a merge
  commit `origin/work` never has, so the update answers `current` while the checkout lacks
  whatever reached `main` from any other branch. README (`:153`) promises an install that is
  "the local `main` branch tracking `origin/main`".
- The branch is `work` because the assistant's development flow fast-forwarded its branch
  into the vault checkout; an installer never leaves it there.
- A branch that is ahead of its remote is reported `diverged_branch`, which is not what it is.

## Practice on this date

- An automatic updater follows one declared channel and reports any other state by name,
  never as up to date. Git itself refuses a fast-forward to a commit that is not a
  descendant (`git merge --ff-only`: "resolve the merge as a fast-forward when possible.
  When not possible, refuse to merge and exit with a non-zero status", git-merge
  documentation, fetched 2026-09-24).

## The decisions

1. The update runs only on the default branch (the remote's `HEAD`, else `main`). On any
   other branch it answers `skipped: not_on_default_branch` and names the branch, so the
   nightly log and the pass result never say `current` for a checkout that is not following
   the product.
2. A checkout ahead of its remote says `ahead_of_remote`; `diverged_branch` is kept for a
   branch that has both local and remote commits.
3. The live vault is switched back to `main` once the pull request carrying this change is
   merged, so the switch lands on code that has passed CI.

## Sources

- git-merge documentation, `--ff-only` — https://git-scm.com/docs/git-merge — fetched 2026-09-24.
- `git -C <vault> status -sb` and the nightly log of 2026-09-24 on the live vault.
