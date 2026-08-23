# Updating the vault's own code without losing anyone's work (2026-08-23)

## Why this was researched

The owner's standing requirement is that everything works without him. The
installer does not update code: re-running it syncs dependencies and rewires
agents, but the checkout stays where it was, so the product only improves when
somebody types `git pull`. Asked directly, he answered "обновляй" — lift the
prohibition and make it automatic.

That prohibition is not an oversight. Two recorded contracts say in as many
words that there is "no automatic Git operation", and they were written when the
vault and the public source were two directories. Since 2026-08-21 they are one,
which is exactly what makes an automatic update delicate: the same working tree
holds the product's source and the owner's knowledge.

## What makes this vault different

A general auto-updater refuses to run on a dirty working tree. Here the tree is
dirty almost always and legitimately: the runtime rewrites two tracked files —
`knowledge/index.md` and `knowledge/log.md` — on every compile, and writes
private pages that git ignores. A "clean tree required" rule would mean the
update never runs, which is the same defect as a warning that can never be
cleared: it looks like a safety property and behaves like an off switch.

## What current practice says

The safe shape is consistent across sources: fetch rather than pull, restrict to
fast-forward, and refuse rather than resolve. `git fetch origin branch:branch`
updates the local reference "if and only if the update is a fast-forward"; the
same advice recommends "using `git fetch` with specific refspecs rather than
`git pull` for more controlled updates", "restricting updates to fast-forward
merges only", and "checking for a clean working tree before attempting updates".
Tools that automate this for many repositories are described as smart precisely
because they handle "dirty working directories, diverged local branches,
detached HEADs" by declining, not by fixing.

Nothing recommends resolving conflicts automatically, and nothing recommends
`reset --hard` or `clean` in an unattended path. Both destroy work that no test
can bring back.

## What this changes here

The nightly pass gains a last step that updates the code, and only under proofs
it can compute:

- The vault is a git checkout, on a branch, with a remote. Detached HEAD or no
  remote means no update.
- `fetch` succeeds within a bounded timeout. Network failure is not an error of
  the vault; the pass reports it and carries on.
- The fetched head is a strict descendant of the current head. A diverged branch
  is left alone: merging it is a judgement call, and the owner makes those.
- **No path the update would change is modified locally.** This is the rule that
  replaces "clean tree". It is exact — the intersection of what the merge would
  touch with what `git status` reports modified — and it lets a vault whose index
  and log are dirty still receive product changes, while refusing the moment the
  update and the owner reach for the same file.
- The merge is `--ff-only`. Nothing is reset, nothing is cleaned, untracked files
  are never touched.
- Dependencies are re-synced from the lockfile afterwards, because code that
  needs a new dependency is worse than code that is a day old.

The step runs last in the pass, so the code that changed takes effect on the next
run rather than halfway through this one. Nothing is ever pushed: this reads from
the remote and writes only to the checkout.

## Sources

- Git documentation, `git-pull` (fast-forward semantics and refspec fetch) —
  https://git-scm.com/docs/git-pull
- "How To Fast-Forward & Update a Git Branch" —
  https://www.howtogeek.com/devops/how-to-fast-forward-update-a-git-branch/
- "Git Branch Update Without Checkout: Fast-Forward Merge Techniques" —
  https://sqlpey.com/git/git-branch-update-without-checkout-fast-forward-merge-techniques/
- GitHub Docs, "Dealing with non-fast-forward errors" —
  https://docs.github.com/en/get-started/using-git/dealing-with-non-fast-forward-errors
- `gitup` (declines on dirty trees, diverged branches, detached heads) —
  https://pypi.org/project/gitup

## Open question

Whether an update that changes the scheduler unit or the installer contract
should re-run the installer by itself. It is left out on purpose: re-running the
installer rewrites the profile and the scheduler, and doing that unattended right
after a code change compounds two risks. The doctor reports drift, and the owner
re-runs the installer.
