# The nightly update moves only to what the default branch holds

Dated 2026-09-14. Item 4.4 of `docs/AUDIT-2026-09-14-2.md`, decided on the owner's
delegation. The research before the change.

## What was found

- `self_update.update_checkout` fast-forwards the checkout to its branch's upstream
  (`branch.<name>.remote`, `git fetch <remote> <branch>`, `merge --ff-only FETCH_HEAD`).
  The live vault on this machine is on `work`, so every push to `origin/work` reaches
  the installed product the next night, including commits whose CI has not run or has
  failed.
- CI runs for `main` and for pull requests into `main` (`.github/workflows/tests.yml`).
  The owner's rule is that nothing reaches `main` unless every check on that commit
  passed («Нельзя сливать не зелёное»), and pull requests are merged with merge commits
  (`git log --merges origin/main`: #30–#34), so a merged branch's commits are ancestors of
  `origin/main`. `origin/HEAD` points at `origin/main` here.
- The code graph: `update_checkout` ← `scheduled_nightly` (the update step);
  `_attempted_update` → `_prepared_update` → `_fast_forward` → `_merged_update`.

## Practice on this date

- Continuous deployment promotes only artifacts that passed the pipeline; the gate is a
  property of the artifact, checked where it is deployed
  ([Google SRE Workbook, "Canarying Releases"](https://sre.google/workbook/canarying-releases/);
  GitHub protected branches exist to make "merged" mean "checks passed",
  [GitHub docs, protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)).
- Asking GitHub's API for check results would add a network dependency and a GitHub-only
  code path; ancestry of the remote's default branch is the same fact, checked offline
  with `git merge-base --is-ancestor` after a fetch.

## The decision

- The update fetches the remote's default branch (`refs/remotes/<remote>/HEAD`, else
  `main`) as well as the tracked branch, and fast-forwards only when the fetched commit
  is an ancestor of the default branch's tip. Otherwise it is skipped with
  `not_in_default_branch`.
- A checkout that tracks the default branch itself is unaffected (its tip is its own
  ancestor). A checkout on `work` receives a commit once its pull request is merged.

Files: `scripts/self_update.py`, `tests/test_an_update_only_to_what_main_holds.py`,
`docs/research/2026-09-14-an-update-only-to-what-main-holds.md`.
