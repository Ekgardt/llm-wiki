# The machine's own patches leave the public repository

Dated 2026-09-18. Finding I-D3 of the third audit, decided by the owner today.

## What was found

- `docs/enforcement/` holds four tracked files, about 1 200 lines: three scripts that edit
  the rule gates installed at `/etc/claude-code/enforcement/` on this machine, and one note
  describing the hole they close (edits arriving as shell heredocs, which the gates scoped to
  the file-editing tools did not see).
- Nothing in the product imports or runs them: the only reader is
  `tests/test_enforcement_patch.py`, which checks that one script's preimages still match the
  installed gate sources, and skips where no gate is installed.
- They describe one machine's private configuration, and the repository is public. The gates
  they patch are already live here, so the scripts are either applied or obsolete.

## The decision

- The owner asked for them to be deleted ("удаляй", 2026-09-18). The four files and the test
  that exists only for them are removed; the audit row records the decision.
- `tests/test_nothing_private_reaches_the_public_repository.py` already sweeps every tracked
  file for machine paths and personal names, so nothing needs to replace the removed test.
- The patches themselves are not lost: they live in this repository's history, and the gate
  sources they edit are on the machine that owns them.

Files: `docs/enforcement/*` (removed), `tests/test_enforcement_patch.py` (removed),
`docs/AUDIT-2026-09-17.md`,
`docs/research/2026-09-18-the-machines-own-patches-leave-the-public-repository.md`.
