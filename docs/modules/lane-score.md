# Lane score

Owns `scripts/lane_score.py` at its existing path. Scores supplied lexical/dense
ranks, lane silence, user-turn text and optional reranker value with the seven
shipped constants. No retrieval, index reads, training, provider calls or benchmark
labels. Candidate access uses the supplied candidate attributes and display mapping.
Preserve rank clipping, missing-rank semantics, marker order and floating-point
expression order; changing these is a scoring change, not packaging.

The calibration and later refusal to refit are retained whole in this packet.
They are dated evidence, not a new measurement. The offline fitter and real
retrieval ordering remain integration consumers. Preserve their pins and assertions.
The pure score test moves unchanged into this owner; production bytes stay identical.

Qualification: [research](../research/2026-11-03-retrieval-leaf-owner-qualification.md).
Run owner and retained integration tests, Python 3.14 compatibility, actual complexity
and full-context reconstruction. No useful-cost or native-platform claim follows
from a packet-size check.

## Structural reference retained from the overview

### Existing retrieval leaf owners

The existing production paths remain unchanged: [lane score](lane-score.md)
owns deterministic candidate scoring, [temporal anchor](temporal-anchor.md)
owns explicit day arithmetic, and [evidence sufficiency](evidence-sufficiency.md)
owns single-span coverage with a downward dependency on the date resolver. Pure tests
move out of mixed integration files without changing assertions; real retrieval and
answer checks stay. No ranking constants, date rules or thresholds change.
See [qualification](../research/2026-11-03-retrieval-leaf-owner-qualification.md).
