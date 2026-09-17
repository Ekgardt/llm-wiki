# A verified first install then follows main

Dated 2026-09-17. Finding I-A8 of the third audit (medium-low, confirmed), the part the first
round left open: it added a notice, not a way to update. The research before the fix.

## What was found

- The remote bootstrap of both installers fetches one commit with `fetch --depth 1` and
  runs `checkout --detach`. `self_update._update_target` answers `skipped (detached_head)`
  for such a checkout, so the nightly step never updates a vault installed the advertised
  remote way. The contract in `CLAUDE.md` says the product's single automatic Git operation
  is the nightly fast-forward; a whole class of installs was outside it.
- The pin exists for one reason: the first code that runs on the machine is the commit the
  operator verified, not whatever a branch name resolved to at that second. That reason is
  spent once the checkout has been verified (`HEAD` equals the OID, origin equals the
  repository URL, required files present).
- A checkout made with `git clone` already follows its branch every night. The two install
  routes differed by accident, not by decision.

## Practice on this date

- A shallow repository can be fetched into normally. git-fetch(1): "`--depth=<depth>` Limit
  fetching to the specified number of commits from the tip of each remote branch history."
  and gitrepository shallow notes: "`$GIT_DIR/shallow` lists commit object names and tells
  Git to pretend as if they are root commits" (<https://git-scm.com/docs/git-fetch>,
  <https://git-scm.com/docs/shallow>, both fetched today). A later plain fetch of the branch
  brings the commits between the shallow boundary and the new tip, so
  `merge-base --is-ancestor` and `merge --ff-only` work.
- Measured today with git 2.43.0 in a temp directory: upstream with two commits, `fetch
  --depth 1` of the second by OID, `checkout -B main <oid>`, `branch.main.remote=origin`,
  `branch.main.merge=refs/heads/main`; a third commit upstream; `self_update.update_checkout`
  returned `status: updated` and the working tree held the new file.

## Options

1. Document the pin and leave the vault frozen. Cheap, but it keeps a product promise
   (automatic improvement) false for the advertised install route, and a frozen vault also
   never receives a security fix.
2. After verification, put the checkout on a local `main` that tracks `origin/main`. The
   update then passes through every guard `self_update` already has: fast-forward only,
   only what the default branch holds, never over a locally modified file. A pin that is
   not an ancestor of `main` is answered `diverged_branch` and left alone.
3. A new environment variable choosing between the two. It would be a new env contract for
   a choice git already expresses.

## The decision

Option 2. The verified commit becomes local branch `main` with `origin/main` as its
upstream, in both installers. No new environment variable: an operator who wants the vault
frozen runs `git -C ~/LLM-wiki checkout --detach`, and the installer's summary and the
nightly report already name that state (`pinned`, `skipped (detached_head)`). README×3,
`docs/USER-GUIDE.md` and `docs/STRUCTURE.md` say so. The test builds a real upstream,
runs the installer's own function against it, adds a commit upstream and asks
`self_update.update_checkout` — it must answer `updated`.

Files: `install.sh`, `install.ps1`, `README.md`, `README.ru.md`, `README.zh-CN.md`,
`docs/USER-GUIDE.md`, `docs/STRUCTURE.md`,
`tests/test_a_verified_first_install_then_follows_main.py`,
`tests/test_the_installer_says_what_it_needs.py`,
`docs/research/2026-09-17-a-verified-first-install-then-follows-main.md`.
