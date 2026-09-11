# The comparative stand checks one thing per function

Date: 2026-09-11. Trigger: audit finding L13, `benchmark/run_comparative.py`:
12 functions over the gate, led by `_validate_real_manifest` (CCN 85: one
function validating twelve manifest sections), `compute_paired_statistics`
(39: pairing, cluster means, bootstrap and sign-flip in one body),
`preflight_real_run` (31: seven probes), `execute_comparison` (25: a
four-deep loop with retry), `_canonical_evidence_value` (15),
`_gate_f_evidence_complete` (15), `load_contract` (13), `main` (13).

## Sources

1. `tests/test_comparative_benchmark.py`: every `ValueError` message, every
   preflight finding code and message, the ledger and report shapes, the
   RNG-driven bootstrap numbers (the SHA-256 counter RNG is consumed in
   a fixed order) and the frozen claim-gate conditions are asserted.
2. Rule 5's remedies: one `_require_*` per manifest section; one finding
   collector per probe; the statistics as pairing → clusters → observed
   → bootstrap → sign-flip, each a function; the attempt loop as a
   per-task, per-adapter run over one context object.

## Decision

Messages, finding codes, orders and RNG consumption unchanged. One
provably dead check is dropped: `seeds != manifest.get("seeds")` compared
the list with itself and could never be true. The claim gate constant
becomes a module value. The bootstrap draws happen in the same order:
one draw per cluster slot, then one per row slot inside each selected
cluster; the sign-flip draws one value per cluster difference.

Files: `benchmark/run_comparative.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-memory-and-retrieval.md`.
