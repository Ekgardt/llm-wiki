# Evidence sufficiency

Owns `scripts/evidence_sufficiency.py` at its existing path. Measures the best single
shown span against question terms, figures and the explicit temporal window. Keeps
covered/missing ordering, absent score semantics, date distance, frozen threshold
and coverage-note bytes. It grants no answer correctness and reads no benchmark
label. Retrieval, model confidence, repeat reads and refusal policy remain callers.

This owner uses the full temporal_anchor implementation and its contract. Preserve
the historical distinction between the tuned coverage threshold and uncalibrated
three-day date tolerance. Three existing pure tests move unchanged from the mixed
refusal integration file; actual reader behavior and no-label tests still run.

Shared canonical contract: [Temporal anchor](temporal-anchor.md).

Qualification: [research](../research/2026-11-03-retrieval-leaf-owner-qualification.md).
Run owner and retained integration tests, Python 3.10 compatibility, actual complexity
and full-context reconstruction. No useful-cost or native-platform claim follows
from a packet-size check.
