# A stray candidate is retired where it refuses

Dated 2026-09-24. Audit item A-1 (`docs/AUDIT-2026-09-24-live.md`). The owner asked for
every audit finding to be fixed, reliably and without workarounds.

Files: `scripts/installed_memory_repair.py`, `scripts/markdown_transaction.py`,
`scripts/doctor.py`, `tests/test_a_stray_candidate_after_adoption.py`,
`tests/test_a_stray_candidate_is_retired_where_it_refuses.py` (new), `CHANGELOG.md`,
`docs/research/2026-09-24-a-stray-candidate-is-retired-where-it-refuses.md`.

## What was found

- On 2026-09-23 (`2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`) three
  things were fixed: the test suite can no longer resolve its state root to the vault,
  doctor has an `adoption` check that names a refusal as an `error`, and
  `doctor --repair` moves an empty, ownerless coordinator candidate to
  `run/coordinator-quarantine/`.
- What that left, checked in the code on 2026-09-24:
  1. The retirement runs only inside `doctor --repair`, which nothing runs on a schedule.
     The nightly takes its fence through `active_markdown_coordinator`, which calls the
     adoption guard, so a stray candidate stops the nightly before its first step
     (six nights, 2026-09-18..23, `status=1/FAILURE` at 03:00:02). Every hook stops the
     same way. The self-repair needs a person to run it; the owner's rule is that the
     system works without one.
  2. Only the coordinator candidate is handled. `_stray_candidates` names a stray
     `run/queue-v3.candidate.sqlite3` too, and `_require_adoption_artifacts_complete`
     refuses on it the same way, but nothing retires it.
  3. `destination.parent.mkdir(parents=True, exist_ok=True)` creates the quarantine
     directory with the process umask (`drwxrwxr-x` on the live vault) inside a `0700`
     `run/` (audit C-6).
- Every writer — hooks, capture worker, queue, compile, nightly fence — reaches the guard
  through one function, `markdown_transaction._validate_adoption_with_retry` (graph:
  `require_reliability_v3_adopted` inbound callers are `_validate_adoption_with_retry`,
  doctor's checks and the adoption itself).

## Practice on this date

- Recovery belongs on the normal start path, not in a separate procedure someone must
  remember to run: "crash-only programs crash safely and recover quickly" (Candea and
  Fox, "Crash-Only Software", HotOS IX, 2003, fetched 2026-09-24).
- The conditions under which a candidate is provably stray are already decided and
  tested (a complete adoption record, no adoption operation artifact, a valid v3 schema,
  no row in any data table, no live owner). Running the same check at the refusal point
  changes where it runs, not what it proves.

## The decisions

1. `installed_memory_repair.retire_stray_candidates(state_root, now)` owns the rule for
   both candidates. Coordinator: the existing conditions. Queue: a valid queue-v3 schema,
   no row in any table except `queue_ownership`, and no `queue_ownership` row that is
   still live. Each retired file is renamed (never deleted) into
   `run/coordinator-quarantine/` or `run/queue-quarantine/`, both already retained
   evidence for the `run/` deletion contract; the directory is created `0700`. A file
   another process renamed first counts as retired. It returns what it retired and why it
   kept anything.
2. `_validate_adoption_with_retry` calls it once before validating, only when a candidate
   path exists (two `lstat`s; the validation is cached per process). A kept candidate still
   refuses exactly as before.
3. `doctor --repair` uses the same function, so there is one rule, and doctor's adoption
   check reports how many strays have been retired into quarantine.

## Limits (rule 3)

A candidate that holds data or a live owner is still refused and still needs a person or
a finished adoption; that is the guard doing its job. The sessions lost 2026-09-15..22 are
not recoverable: their hooks failed before anything was written.

## Cost, by rule 4

Two `lstat`s per writer process when no candidate exists; one read-only open of a small
file and a rename, once, when one does.

## Sources

- George Candea and Armando Fox, "Crash-Only Software", HotOS IX, 2003 —
  https://www.usenix.org/conference/hotos-ix/crash-only-software — fetched 2026-09-24.
- `docs/research/2026-09-23-a-stray-candidate-stopped-the-memory-for-six-days.md`;
  `journalctl --user -u llm-wiki-nightly.service` 2026-09-18..24 on the live vault.
