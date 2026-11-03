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
Run owner and retained integration tests, Python 3.10 compatibility, actual complexity
and full-context reconstruction. No useful-cost or native-platform claim follows
from a packet-size check.
