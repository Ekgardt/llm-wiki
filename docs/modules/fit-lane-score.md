# Offline lane fitting

Owns `benchmark/fit_lane_score.py` at its existing CLI path. Reads supplied JSON or
JSONL files and prints fitted coefficients and per-type comparisons. It runs no
provider or benchmark and never changes shipped constants. Preserve question-level
folds, deterministic iteration/float order, feature order, rank interpretation,
empty-input refusal and rejection of a matrix that cannot judge its requested depth.
Synthetic-fixture success is not evidence of a better real-world ranking.

Production source stays byte-identical. Existing pure fitter tests and constant pins
move unchanged; actual recorder and reader-window tests remain integration obligations.
NumPy is the existing declared dependency, not a newly introduced learning framework.
The import guard uses scripts and benchmark roots and denies unlisted local modules
from both. Static closure includes lazy imports and the full score implementation.

Qualification: [research](../research/2026-11-03-offline-lane-fit-owner-qualification.md).
Run synthetic CLI, negative refusal, fold-isolation, retained recorder/reader checks,
Python 3.14, complexity and complete packet reconstruction. No live refit or deployment.

Shared canonical contract: [Lane score](lane-score.md).

## Structural reference retained from the overview

### Offline fitting and answer representation owners

Existing paths remain: [offline lane fitting](fit-lane-score.md) reads saved
results and depends on lane_score; [answer budget](answer-budget.md) owns
answer shaping and estimates; [answer cost](answer-cost.md) depends on that
estimate and owns optional telemetry. Pure tests move unchanged; real recorder,
reader and MCP integration assertions remain. The test import guard gains explicit
benchmark roots without changing its scripts-only default. No production byte,
public command, threshold, environment contract, provider call or runtime path changes.
