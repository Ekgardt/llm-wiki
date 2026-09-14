# The vault's log is private; the shipped one stays a template

Dated 2026-09-14. Item 1.3 of `docs/AUDIT-2026-09-14-2.md`, decided on the owner's
delegation («решения принимай самостоятельно после выполнения пяти правил»). The
research before the change.

## What was found

- `knowledge/log.md` is tracked. The repository ships it as a four-line template
  (`git show HEAD:knowledge/log.md`), and the runtime appends to it: the compile pass
  (`compile_memory._ApplyPlan._append_index_and_log`), the answer file-back
  (`query_memory.append_log`), and every agent, told to by `CLAUDE.md`/`AGENTS.md`
  sections 2–3 and by four skills (`knowledge-compile`, `session-memory-compile`,
  `knowledge-qa-file-back`, `bridge-promote-insight`, `contradict-check`).
- On this machine the working copy carries 1 696 uncommitted lines: 64 compile lines
  and prose about the owner's work. `git commit -a` would publish all of it. No
  filter can make free prose publishable.
- Readers: `session_start_context.last_log_entries` (the last three entries in the
  session-start block) and `compile_memory._vault_file_snapshots` (the log as a source
  snapshot for the compile prompt). The Markdown transaction layer allows writes to it
  (`markdown_transaction._ALLOWED_FILES`). Page scanners (corpus, tiers, reflection,
  archive, retrieval, advisory, graph neighbours) all walk `knowledge/notes` or
  `knowledge/projects`, never the `knowledge/` root (checked by `grep` of their roots
  today).
- **Taking `knowledge/log.md` out of git is not safe.** Every install's compile has
  modified its working copy, and `self_update` declines any update that touches a
  locally modified file (`knowledge/notes/automatic-code-update-decision.md`,
  `CLAUDE.md` section 1). A commit deleting the tracked file would stop the nightly
  update on every install, permanently.

## Practice on this date

- Keep runtime-written files out of the tracked tree; ship a template and let the
  program write the local copy (the `.env.example`/`.env` and `settings.json`/
  `settings.local.json` convention; git's own guidance to ignore files "generated at
  runtime" — [gitignore](https://git-scm.com/docs/gitignore)).
- Git offers no per-repository way to track a file and ignore local changes to it;
  `--skip-worktree` is per clone
  ([git-update-index](https://git-scm.com/docs/git-update-index#_skip_worktree_bit)).

## The decision

- The vault's editorial log moves to **`knowledge/log.local.md`**, denied in
  `.gitignore`. Every runtime writer and reader uses it: the compile pass (append and
  prompt snapshot), the answer file-back, the session-start block, the transaction
  allowlist. `CLAUDE.md` and `AGENTS.md` (kept byte-identical) and the skills tell agents
  to append there.
- The tracked `knowledge/log.md` stays the shipped template and nothing writes to it
  again, so no upstream change ever has to touch it and no install's update is blocked.
  A structure test pins that the tracked file is the template and that the private log
  is ignored.
- On this machine, the 1 696 lines already in `knowledge/log.md` move to
  `knowledge/log.local.md` and the tracked file returns to the template, once this
  change is live. Other installs keep their modified copy until they do the same; it is
  never read again.
- The publication filters on the compile and file-back lines stay: they cost nothing,
  and the private log may still be pasted somewhere public.

Why not the alternatives:

- **Untrack `knowledge/log.md`.** Blocks every install's nightly update (above).
- **Keep writing compile lines to the tracked file.** Keeps every working tree dirty,
  and snapshot hashes and dates are the vault's activity, not the product.

Files: `.gitignore`, `CLAUDE.md`, `AGENTS.md`, `skills/knowledge-compile/SKILL.md`,
`skills/session-memory-compile/SKILL.md`, `skills/knowledge-qa-file-back/SKILL.md`,
`skills/bridge-promote-insight/SKILL.md`, `skills/contradict-check/SKILL.md`,
`scripts/vault_log.py`, `scripts/compile_memory.py`, `scripts/query_memory.py`,
`scripts/session_start_context.py`, `scripts/markdown_transaction.py`,
`tests/test_the_vault_log_is_private.py`,
`docs/research/2026-09-14-the-vault-log-is-private.md`.
