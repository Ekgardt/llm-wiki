# Temporal anchor

Owns `scripts/temporal_anchor.py` at its existing path. Resolves explicitly supported
relative day expressions against a supplied date. User-turn selection, first phrase
wins, exact date points, week/weekend spans, annotation text and search expansion
remain byte-identical. It reads no clock, file, model or index. Months, ambiguous
sub-day expressions and assistant statements retain their existing treatment.

A named span is never emitted as an invented point fact. Query windows retain the
existing neighbourhood; this package does not recalibrate it. Existing caps and
clipping retain their contracts. Pure calendar tests are owned in place; two pure
query/window tests move unchanged from mixed integration files. Actual writing,
retrieval and answer paths remain integration consumers with all assertions.

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
