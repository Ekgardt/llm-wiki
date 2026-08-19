---
type: decision
title: "Warm Navigation Overhead Threshold"
description: "The warm navigation overhead gate is 30 ms p95, measured on the slowest supported machine class rather than on a quiet one."
date: 2026-08-19
confidence: high
source_authority: user
status: active
---
# Warm Navigation Overhead Threshold

One-sentence summary: The warm navigation overhead gate is 30 ms p95, measured on the slowest supported machine class rather than on a quiet one.

## Decision

Date: 2026-08-19.

`warm_overhead_p95_ms` in the fixed 100 KLOC Python qualification gate is 30 ms.
It was 20 ms. The gate still fails closed, is still measured from paired
alternating samples of the same query through the facade and through Pyright
directly, and is still required evidence for qualification.

The threshold now names the slowest machine the project supports for running the
gate: a four-vCPU shared runner. It is not a target for the operator's own
machine, where the measured overhead is lower.

## Why

Three consecutive GitHub-hosted runs measured 22.80, 22.08, and 22.16 ms against
the 20 ms threshold. The spread is under one millisecond, so this is a stable
property of that machine class and not measurement noise.

The overhead is not accidental. The facade computes the workspace revision
before a query and verifies it afterwards, which is the freshness guarantee that
lets a navigation result claim it matches the tree it cites. A direct Pyright
call does neither. Paying for that guarantee costs an extra revision walk, and
on four slow cores that walk costs roughly 22 ms at p95 over a 100 KLOC tree.

Two alternatives were rejected. Optimizing the double walk changes the freshness
contract's implementation under time pressure, for a cost that is already
understood and bounded. Restricting the gate to the operator's machine would
have removed the only automated evidence that the overhead is bounded at all.

## Consequences

- A regression above 30 ms p95 still fails the gate on every supported machine.
- A regression between 20 and 30 ms is no longer caught automatically. If that
  band matters later, the answer is a second threshold bound to a named machine
  class, not a return to one number for every machine.
- The recorded qualification evidence from before 2026-08-19 was produced under
  the 20 ms threshold and remains valid as measured.

## Source / Evidence

- Explicit operator approval, 2026-08-19.
- Measurements: runs `32235746281` (22.075 ms), `32238021841` (22.8 ms), and
  `32239406567` (22.164 ms) on `ubuntu-24.04`, four vCPU, threshold 20.
- Threshold: `benchmark/run_code_navigation.py` `_PRODUCTION_THRESHOLDS`.
- Measurement: `benchmark/run_code_navigation.py::_measure_warm_performance_pair`.
- Freshness walk: `scripts/code_navigation.py::CodeNavigation.query`.
- GitHub-hosted runner specification:
  https://docs.github.com/en/actions/using-github-hosted-runners/about-github-hosted-runners

## Related

- [[read-only-lsp-navigation-engine-decision]]
- [[baseline-environment-binding-decision]]
