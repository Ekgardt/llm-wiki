---
type: decision
status: accepted
date: 2026-08-17
---

# Baseline environment binding

One-sentence summary: The frozen retrieval-v2 baseline is bound to the exact
package versions the benchmark loads, not to the byte digest of the whole
`uv.lock`.

## What changed

`_verify_baseline_package_contract` in `benchmark/run_retrieval_v2.py` no longer
compares the baseline's recorded `uv_lock_sha256` against the current
`uv.lock`. It still requires:

* the frozen package set to be exactly `jieba`, `numpy`,
  `sentence-transformers`, `torch`, `transformers`;
* every frozen version to be present in the current `uv.lock`;
* `environment.packages` to be derived from the frozen map;
* `verified_lock.package_map_sha256` to match that map;
* the recorded lock digest to be a well-formed SHA-256, and the two places the
  report records it to agree with each other.

## Why

Commit `350eec8` on `main` retired the Cognee bridge and regenerated `uv.lock`.
Every package version the baseline froze is still locked exactly as recorded —
`jieba 0.42.1`, `numpy 2.5.1`, `sentence-transformers 5.6.0`, `torch 2.13.0`,
`transformers 5.13.0` — and `package_map_sha256` still matches. Only the digest
of the file as a whole moved, because that file locks several hundred packages
the benchmark never loads.

Twelve `tests/test_retrieval_v2_benchmark.py` cases failed on every platform
from that moment, including locally. The contract had stopped testing the
measurement environment and started testing the file, and it could not be
satisfied without either re-running the real benchmark offline with the pinned
model artifacts or editing the frozen evidence to match — the second of which
is exactly the fabrication the contract exists to prevent.

## What this gives up

A change that alters one of the five bound packages still invalidates the
baseline, which is the case that matters. What no longer invalidates it is a
change to any of the other locked packages. If a future dependency indirectly
changes the numerics of those five — a transitive constraint on `torch`, say —
this contract will not catch it, and the baseline would have to be re-measured
to notice. That is the accepted cost.

## Alternatives considered

* **Re-attest the baseline.** Correct, and impossible here: the benchmark runs
  `offline-local-files-only` against `BAAI/bge-small-en-v1.5` artifacts this
  machine does not hold. It also changes the recorded quality numbers, which is
  a measurement, not a fix.
* **Leave it failing.** Keeps CI red on every platform for a condition nobody
  can satisfy, which trains readers to ignore the signal.

## Approval

Requested and granted by the machine owner on 2026-08-17, in response to the
three options above. Research recorded in
`docs/research/2026-08-17-cross-platform-identity-and-publish-semantics.md`.

## Source / Evidence

- `benchmark/run_retrieval_v2.py` — baseline environment binding and the five
  packages the benchmark actually loads.
- `benchmark/baseline-2026-07-16.md` — the frozen measurement this contract protects.
- Commit `350eec8` — the lock regeneration that broke the previous byte-digest binding.
- `docs/research/2026-08-17-cross-platform-identity-and-publish-semantics.md`.

## Related

- [[knowledge/notes/audit-closure-security-recovery-control-plane-decision]]
- [[knowledge/notes/derived-evidence-generation-decision]]
- [[knowledge/notes/warm-navigation-overhead-threshold-decision]]
- [[knowledge/notes/one-trust-weight-across-retrieval-paths-decision]]
