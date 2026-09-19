# A re-run of the installer may ask for fewer things

Dated 2026-09-17. Finding I-A3 of the third audit (medium, reproduced). The research before
the fix.

## What was found

- `install_control.py` updates an active install by checkpointing every resource the
  manifest names (`_checkpoint_resources` → `_active_resource`). `_active_resource` raises
  `install_resource_request_mismatch` when the new request does not carry a resource the
  manifest names, or carries it under another locator.
- So a request may grow but never shrink or swap. Reproduced here with three file
  resources: install `[a, b]`, then `[a]` fails, `[a, c]` fails, `[a, b, c]` passes.
- The installer asks for a different set in ordinary life: `--scheduler cron` after a native
  install (another scheduler id), an agent that is no longer detected (its flag is not
  passed), a Codex inline hook state that is no longer `absent`, and a login shell that
  moved from bash to zsh (same id `unix-profile`, another locator). Each ends in "Install
  ownership transaction failed", while the header of `install.sh` says "Safe to re-run.
  Idempotent." The only way out is `install_control.py uninstall`, which nothing names.

## Practice on this date

- Debian Policy 6.2 on scripts that install things: "It is necessary for the error recovery
  procedures that the scripts be idempotent. This means that if it is run successfully, and
  then it is called again, it doesn't bomb out or cause any harm, but just ensures that
  everything is the way it ought to be."
  (<https://www.debian.org/doc/debian-policy/ch-maintainerscripts.html>, fetched today.)
- The control plane already has the operation that takes back exactly what an install
  wrote: `uninstall_resources` restores each recorded origin under the same lock, with
  preimages, and refuses on drift. After it commits, `_require_settled_transaction` lets a
  fresh install start. Replacing a set is therefore "take the old one back, then install
  the new one" — two transactions that already exist and are each resumable, rather than a
  third kind of record inside the update transaction.
- What was considered and rejected: teaching the update transaction to carry records whose
  desired state is their origin. It touches resume, rollback and checkpoint validation of a
  5000-line transaction core for the sake of a rare path, and a mistake there damages the
  common path.

## The decision

- The `install` command looks at the active manifest before it installs. If every recorded
  resource is still requested under the same identity, nothing changes. If not, and no
  transaction is in flight, it rebuilds the recorded resources from the manifest (the
  profile path is taken from the record, not from the new request), uninstalls them, and
  then installs the new request as a fresh install. The result says `"replaced": true`.
- `uninstall` and `rollback` read the profile path from the record for the same reason: a
  changed login shell must not make the old block impossible to take back.
- Drift stays fail-closed: a recorded resource that was changed by hand still refuses, as
  before.
- If the process dies between the two transactions the machine is cleanly uninstalled and
  the next run installs — the second call "merely does the things that were left undone".

Files: `scripts/install_control.py`,
`tests/test_a_rerun_may_ask_for_fewer_things.py`,
`docs/research/2026-09-17-a-rerun-may-ask-for-fewer-things.md`.
