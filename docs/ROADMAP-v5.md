# v5.0 Roadmap

> These features are explicitly POST-v4.0. They are not incomplete v4.0 items —
> they require significant infrastructure or accumulated data that doesn't exist yet.
> v4.0 is considered complete without them.

## 1. Bi-temporal code edges (valid_from / valid_to per symbol)

**What:** Every code symbol carries git commit timestamps. Enables time-travel
queries ("how did auth look in March?") and causal impact analysis.

**Why deferred:** Requires git commit tracking infrastructure — mapping each
symbol version to a specific commit, maintaining append-only version chains.
This is a 2-3 day engineering effort.

**Dependencies:** code_graph.py + git integration layer.

## 2. Leiden community detection (auto-architectural modules)

**What:** Automatically discovers functional modules in the code graph by
clustering call edges. igraph + leidenalg.

**Why deferred:** Requires igraph dependency (`pip install igraph leidenalg`).
Algorithm implementation + integration with code_graph output.

**Dependencies:** code_graph.py + igraph + leidenalg.

## 3. Quality calibration (logistic regression on page features)

**What:** Objective quality score (0.0-1.0) for each knowledge page based on
word_count, evidence_section, backlinks, access_count, update_sections.
Model trained on superseded/archived pages as negative class.

**Why deferred:** Requires MONTHS of accumulated access_tracking data to have
enough signal for meaningful calibration. No training data exists yet.

**Dependencies:** access_tracking.py (needs 3+ months of data).

## 4. Code health markers (25 deterministic defect predictors)

**What:** 25 markers (cyclomatic complexity, god classes, N+1 patterns,
I/O in loops, brain methods, low cohesion, etc.) per file. Calibrated
against defect corpus. ROC AUC 0.74 (repowise benchmark).

**Why deferred:** Each marker needs: implementation + calibration + test.
This is 3-4 days of focused work. Not a quick addition.

**Dependencies:** code_graph.py (needs tree-sitter AST, not regex fallback).
