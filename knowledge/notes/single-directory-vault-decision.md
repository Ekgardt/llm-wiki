---
type: decision
title: "One Directory Holds Both the Vault and the Public Source"
description: "The vault and the public source live in one directory; what keeps private knowledge out of the public repository is a deny-by-default .gitignore with an explicit allowlist, not a directory boundary."
date: 2026-08-21
confidence: high
source_authority: user
status: active
---
# One Directory Holds Both the Vault and the Public Source

One-sentence summary: The vault and the public source live in one directory;
what keeps private knowledge out of the public repository is a deny-by-default
`.gitignore` with an explicit allowlist, not a directory boundary.

## The decision

The owner asked for one directory. On 2026-08-21 the second directory was
uninstalled through the product's own `uninstall` path — reported
`{"state":"committed","status":"uninstalled"}`, the profile block removed, the
timers unregistered — and then deleted. `$LLM_WIKI_ROOT` and
`$LLM_WIKI_STATE_ROOT` both point at the remaining directory, which is also the
clone that pushes to `Ekgardt/llm-wiki`.

## Why this needs writing down

The operating contract in `CLAUDE.md` and `AGENTS.md` described two separate
directories and named that separation as the thing keeping private knowledge
out of a public repository. After the merge that sentence was false, and false
in the worst direction: an agent following it would read "personal knowledge
goes in `$LLM_WIKI_ROOT`, not here" and write personal knowledge into the tree
that has a GitHub remote. Nothing would have stopped it except the mechanism
the contract did not mention.

A contract that misdescribes the safety mechanism is worse than one that says
nothing, because it directs confident action in the wrong place.

## What actually protects private knowledge

Deny by default, publish by exception:

- `knowledge/daily/*.md` and `knowledge/notes/*` are ignored, with explicit `!`
  lines for each published example and decision page.
- `cache/`, `logs/` and `run/` are ignored outright.
- `knowledge/index.md` and `knowledge/log.md` are tracked because the runtime
  rewrites them, and
  `tests/test_structure.py::test_the_vault_index_and_log_name_only_published_notes`
  requires every page they name by path to be published.

So publishing is a deliberate act — adding an allowlist line — and the default
for anything the runtime writes is private.

The index generator reads that same allowlist, so a private page's title never
reaches `knowledge/index.md` in the first place. Before 2026-08-21 it listed
every page it found, and the structure test was the only thing between that and
a push. It reads `.gitignore` directly rather than asking git, because the index
rebuild is an automatic writer and the writer suite requires that none of them
shell out to git.

## What follows from it

- Writing a knowledge page in this directory is normal runtime behaviour, not
  a violation.
- Running the compile and flush pipelines here is correct. The old prohibition
  existed because a second directory was the one to run them against.
- The pre-commit obligation moves to the person or agent committing: check that
  nothing under `cache/`, `logs/`, `run/` is staged, and that every staged
  `knowledge/` path is one you can defend publishing.

## Source / Evidence

- `docs/DEVELOPER-AUDIT-STATUS-2026-08-18.md`, section "2026-08-21: один
  каталог, `NEW-20`" — the merge, the uninstall receipt, and the
  `git check-ignore` verification of every path the pipeline writes.
- `.gitignore` lines 26-30 and 35 onward — the denials and the allowlist.
- `tests/test_structure.py::test_the_vault_index_and_log_name_only_published_notes`.
- Verified 2026-08-21: `$LLM_WIKI_ROOT` and `$LLM_WIKI_STATE_ROOT` both resolve
  to the repository root, and `run/`, `cache/`, `logs/` exist inside it.

## Related

- [[knowledge/notes/centralized-memory-subsystem]] — the earlier decision that
  memory paths resolve to one canonical location regardless of worktree; this
  extends the same principle from worktrees to the vault itself.
- [[knowledge/notes/hook-scripts-defense-in-depth]] — the same defence-in-depth
  reasoning applied to hook state roots.
- [[knowledge/notes/automatic-code-update-decision]] — links to this page.
- [[knowledge/notes/memory-keeps-a-second-copy-decision]] — links to this page.
