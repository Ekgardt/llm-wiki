# The gate measures our share, not the machine

Dated 2026-09-18. The published qualification gate `warm_overhead_p95_ms` failed CI run
`35382495391` at 34.28 ms against a 30 ms threshold. Nothing in our navigation layer had
grown by 4 ms. The runner had. This note records what the gate exists to catch, why one
absolute millisecond number cannot hold still on a shared runner, and the bound that
replaces it.

Files: `benchmark/run_code_navigation.py`,
`tests/test_code_navigation_benchmark.py`,
`tests/test_the_navigation_gate_measures_our_own_share.py` (new),
`docs/CODE-NAVIGATION.md`, `CHANGELOG.md`.

## What the gate is for

`benchmark/run_code_navigation.py::_measure_warm_performance_pair` asks the same question
twice, alternating `direct, facade, facade, direct`, and averages each side. The direct
side is Pyright answering on its own. The facade side is Pyright plus our layer: the
workspace-revision walk before the query and the verification after it, which is what lets
a navigation result claim it matches the tree it cites (`scripts/code_navigation.py`).
`warm_overhead_p95_ms` is the p95 of the twenty per-query differences — *our* cost, not
Pyright's.

So the gate's subject is a share of work we own. The number it compared against was an
absolute duration, which is a property of the machine as much as of the code.

## What the recorded runs say

Five recorded qualification runs, all with `sample_count: 20`, read out of the CI job logs
and `cache/benchmarks/full-2026-09-14/navigation_qualification.log`:

| run | when | direct p95 ms | facade p95 ms | overhead p95 ms | overhead ÷ direct |
|---|---|---|---|---|---|
| `32235746281` | 2026-08-19 | 44.104 | 64.753 | 22.075 | 0.501 |
| `32239406567` | 2026-08-19 | 46.033 | 67.797 | 22.164 | 0.482 |
| previous run (15:33) | 2026-09-18 | 36.406 | 62.258 | 26.244 | 0.721 |
| `35382495391` (18:50) | 2026-09-18 | 46.452 | 80.432 | 34.282 | 0.738 |
| local full run | 2026-09-14 | 26.263 | 51.163 | 24.900 | 0.948 |

The two 2026-09-18 rows are three hours apart on `ubuntu-latest`, on the same fixture
(identical generated `git_commit`), with a 120-line change between them that touches
Windows-only branches, `O_BINARY` flags and a bounded config parse. Between them
`direct_pyright_p95_ms` — Pyright itself, which that change cannot reach — rose 27.6%,
cold readiness rose 9.4%, and our share of the work moved 2.4%, from 0.721 to 0.738.
The gate read the runner's bad afternoon and called it our regression.

The August rows say the opposite thing about the same number: at a *slower* Pyright
(44–46 ms) our overhead was 22 ms, a share of 0.48–0.50. By September the share is 0.72–0.74.
Our layer's cost relative to the work it wraps grew by about half in a month and the
absolute gate never noticed, because 26 ms is under 30 ms. The private decision page
`knowledge/notes/warm-navigation-overhead-threshold-decision.md` predicted exactly this
when the threshold was raised 20 → 30 on 2026-08-19: "A regression between 20 and 30 ms is
no longer caught automatically. If that band matters later, the answer is a second
threshold bound to a named machine class, not a return to one number for every machine."

## What the field does

Criterion's analysis chapter, on why a comparison of absolute timings misfires
(<https://bheisler.github.io/criterion.rs/book/analysis.html>):

> "In these circumstances even very small changes (eg. differences in the load from
> background processes) can change the measurements enough that the comparison process
> detects an optimization or regression."

> "Outlier classification is important because the analysis method used to estimate the
> average iteration time is sensitive to outliers."

Bencher's continuous-benchmarking explanation, on the size of the effect on exactly our
hardware (<https://bencher.dev/docs/explanation/continuous-benchmarking/>):

> "general purpose CI environments are often noisy and inconsistent when measuring wall
> clock time"

> "GitHub Action Runners, which can see greater than 30% variance between runs"

and its prescription, Relative Continuous Benchmarking
(<https://bencher.dev/docs/how-to/track-benchmarks/>):

> "Relative Continuous Benchmarking runs a side-by-side comparison of two versions of your
> code."

> "This can be useful when dealing with noisy CI/CD environments, where the resources
> available can be highly variable between runs."

pytest-benchmark's FAQ names the same cause (<https://pytest-benchmark.readthedocs.io/en/latest/faq.html>):

> "You run other services in your machine that eat up your cpu or you run in a VM and that
> makes machine performance inconsistent."

The 27.6% we measured is inside the ">30% variance between runs" the second source names.
The prescription is the one thing this benchmark already does and then threw away: it
measures a control — Pyright alone — in the same run, on the same queries, interleaved with
the measurement. The control was recorded and reported and never used to judge anything.

## Decision

`warm_overhead_p95_ms` is compared against a bound that carries the run's own control:

    threshold = max(30 ms, 0.90 × direct_pyright_p95_ms)

The 30 ms floor is the operator's approved 2026-08-19 number and does not move, so nothing
that passed before newly fails. Above it the bound scales with the machine, measured by the
machine, in the same run: our layer may take up to 90% of the time Pyright itself takes at
p95. `direct_pyright_p95_ms` must be a finite positive number or the evidence is incomplete
and the gate fails closed, as it already does for every other missing measurement.

Why 0.90. The current baseline is 0.721 and 0.738; the noise floor of the ratio, from two
runs three hours apart on the same machine class, is 2.4%. 0.90 leaves the baseline 22% of
headroom — about nine noise widths — while the absolute number it replaces had, on the
slow afternoon, none. It is deliberately not tighter: the local 2026-09-14 run shows the
share reaching 0.948 when Pyright is fast (26 ms), because part of our cost is a fixed
revision walk that does not shrink with the query. That case stays under the 30 ms floor
and passes there, which is the floor's purpose.

Replayed through `evaluate_gates` with the recorded numbers, the rule passes all five runs
above, including `35382495391`; and the same overhead of 34.282 ms measured against the
previous run's own control (36.406 ms) fails, because 34.282 > 0.90 × 36.406 = 32.77. That
is the pair of directions the gate has to get right.

## Rejected

- **More samples with a trimmed statistic.** Twenty pairs, each already the mean of two
  counterbalanced repetitions, is the current cost; raising it raises CI time on every
  platform for a variance that the control measurement removes for free. Worth revisiting
  if the ratio's own noise floor ever exceeds a few percent.
- **A pure ratio with no floor.** On a machine where Pyright is fast, a fixed cost of ours
  is a large share of a small number (0.948 above), so a pure ratio would have to be set so
  loose it would stop catching anything.
- **A per-machine named threshold**, the option the 2026-08-19 decision page offered. It
  needs a list of machine classes kept in step with whatever GitHub provisions; the control
  measurement identifies the machine's speed without anyone maintaining a list.
- **Raising 30 to 40.** It buys one afternoon and loses another; the August rows show the
  absolute number is already blind to a 50% growth in our share.

## Open

Our share of the work grew from 0.48–0.50 (2026-08-19) to 0.72–0.74 (2026-09-18) on the
same fixture. That is a real change in our layer, not noise, and it is not explained here.
The new bound flags any further growth; the growth that already happened deserves its own
investigation.

## Source / Evidence

- Measurements: CI job logs for runs `32235746281`, `32239406567`, the 2026-09-18 15:33 run
  and `35382495391`; `cache/benchmarks/full-2026-09-14/navigation_qualification.log`.
- Measurement code: `benchmark/run_code_navigation.py::_measure_warm_performance_pair`,
  `_FixtureRun.performance`.
- Gate code: `benchmark/run_code_navigation.py::_gate_entry`, `_meets_threshold`,
  `evaluate_gates`.
- Prior decision: `knowledge/notes/warm-navigation-overhead-threshold-decision.md`
  (2026-08-19, threshold 20 → 30).
- <https://bheisler.github.io/criterion.rs/book/analysis.html>
- <https://bencher.dev/docs/explanation/continuous-benchmarking/>
- <https://bencher.dev/docs/how-to/track-benchmarks/>
- <https://pytest-benchmark.readthedocs.io/en/latest/faq.html>
