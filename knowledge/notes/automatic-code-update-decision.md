---
type: decision
status: active
confidence: high
source_authority: user
date: 2026-08-23
---

# The Vault Updates Its Own Code, Fast-Forward Only

One-sentence summary: the nightly pass may advance the checkout to the remote
branch when that is a strict fast-forward and touches no file the owner has
modified, and may do nothing else with git.

## Context

The owner's standing requirement is that everything works without him. The
installer never updated code: re-running it synced dependencies and rewired
agents while the checkout stayed where it was, so improvements reached him only
when he typed `git pull` himself. Told that the recorded contract forbade
automatic git operations and that lifting it was his call, he answered:
"обновляй".

The prohibition was written when the vault and the public source were two
directories. Since [[knowledge/notes/single-directory-vault-decision]] they are
one, which is precisely what makes this delicate: one working tree holds the
product's source and the owner's knowledge, and the runtime keeps that tree dirty
by rewriting `knowledge/index.md` and `knowledge/log.md` on every compile.

## Decision

**The nightly pass ends by updating the code.** It runs last so that changed code
takes effect on the next pass rather than halfway through this one.

**Only a fast-forward, and only against untouched files.** The update proceeds
when all of these hold, and declines with a named reason otherwise:

- the vault is a git checkout, on a branch, with a remote;
- `fetch` succeeded inside its timeout;
- the fetched head is a strict descendant of the current head;
- no path the merge would change is locally modified.

That last rule replaces the usual "clean working tree required". Here the tree is
almost always dirty and legitimately so, and a clean-tree rule would be an off
switch wearing a safety property's clothes. The intersection test is exact: the
update happens while the index and the log are dirty, and stops the moment the
update and the owner reach for the same file.

**Nothing destructive, ever.** No `reset`, no `clean`, no `stash`, no conflict
resolution, no touching of untracked files, and nothing is ever pushed. The
merge is `--ff-only`, which either advances the branch pointer or fails leaving
the tree exactly as it was.

**Dependencies follow the code.** A successful update re-syncs from the lockfile,
because code that needs a dependency it does not have is worse than code that is
a day old.

## Why this shape

Current practice is consistent: fetch rather than pull, restrict to
fast-forward, and decline rather than resolve. Tools that automate this across
many repositories are described as smart exactly because they refuse on dirty
trees, diverged branches and detached heads instead of fixing them. Nothing in
that literature recommends automatic conflict resolution, and nothing recommends
`reset --hard` in an unattended path: both destroy work no test can recover.

The one place this vault departs from the standard advice is the dirty-tree rule,
and it departs by being stricter about what matters and looser about what does
not: it protects the exact files at stake instead of the whole tree.

## What this supersedes

The clause "there is no automatic Git operation" in the Stage 2 operational
contract and in the audit-closure contract. Both are updated in `CLAUDE.md` and
`AGENTS.md` to name this decision. Everything else in those contracts stands:
still no persistent daemon, no cloud service, no remote queue, no automatic push.

## Evidence

- Live vault before the change: `run/install/manifest.json` owns the profile
  block and the scheduler only; nothing updates the checkout.
- The tree is dirty by design — `knowledge/index.md` and `knowledge/log.md` are
  tracked and rewritten by every compile.
- Research: `docs/research/2026-08-23-self-update-without-losing-work.md`.

## Open questions

Whether an update that changes the scheduler unit or the installer contract
should re-run the installer unattended. Left out deliberately: re-running the
installer rewrites the profile and the scheduler, and doing that immediately
after a code change compounds two risks. The doctor reports the drift and the
owner re-runs the installer.

## Related

- [[knowledge/notes/single-directory-vault-decision]] — why one tree holds both
  the source and the knowledge.
- [[knowledge/notes/self-resolving-health-findings-decision]] — the same
  principle applied to health: a rule that can never pass is not a safety rule.
