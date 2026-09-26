# A fresh vault has nothing to prune

Date: 2026-09-26. Found while checking audit 2026-09-26 C-13 ("scheduled passes
never executed after install"): a nightly pass run on a fresh sandbox vault.

## What was seen

A nightly pass on a clone of this branch with an empty state root (HOME, state and
snapshot roots all temporary; provider `fake`) finished in 202 s with three failed
steps. Two came from the sandbox itself: the vault had not been adopted to
Reliability v3, which the installer does (capture adoption and dead-capture redrive
refused). The third is a defect: `prune_generations` (step 13) printed
`ERROR: catalog names no active generation; nothing is collectable` and failed the
step, because the first memory generation is built only later in the same pass
(step 19). The catalog did hold one registration — the vault's code generation from
the repository refresh (step 11), which is registered and never activated by design.

## Decision

`_rootless_lines` reports an error only when a memory publication is registered and
none is active (the state `_repair_active_pointer` leaves behind, which the
existing test pins). With no memory publication registered it prints
`no memory generation is registered yet; nothing to prune` and the step succeeds.

## Source

GNU make manual, Running, https://www.gnu.org/software/make/manual/html_node/Running.html,
fetched 2026-09-26: "The exit status is zero if `make` is successful." / "The exit
status is two if `make` encounters any errors." — the convention this step follows:
having nothing to do is success, and only an error is a failure.

## Files

- `scripts/prune_generations.py`
- `tests/test_prune_generations.py`
