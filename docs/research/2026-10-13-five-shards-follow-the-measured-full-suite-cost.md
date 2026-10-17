# Five full-suite shards follow the measured job budget

Date: 2026-10-13. CI38329687452 completed53jobs successfully, failed three
Windows test jobs and the aggregate gate, and had one Windows Python3.10
shard canceled at its existing60-minute job limit. That shard completed997
cases in3563.12seconds; its remaining cases were never qualified. Its canceled
result is not success and is not subtracted against another revision.

36uploaded JUnit reports provide observed per-file durations. Existing
weights underestimated, for example, capture-terminal cost88.07s versus
322.297s and breadcrumb-publication cost12.46s versus101.294s. Only393weight
increases are applied; previously observed larger weights remain. Timing
sums from the interrupted last module are lower bounds, not a claim that it
completed. No old failed or canceled test is excluded from the next run.

At these costs, the old allocation is4623.85/3973.29/4058.08/3777.96seconds.
Rebalancing four shards would still require4108.3seconds each. The36job
records show a maximum50seconds for setup plus cleanup. The existing Windows
job budget stays3600seconds. Thus `ceil(16433.18 / (3600 - 50))` is5: the
smallest measured allocation fitting the available job budget. The existing
longest-processing-time planner distributes the same full suite over five
runners. This is a forecast from observed costs, not a runtime guarantee.
New measurements must requalify it. Linux/macOS retain their existing job
budgets; the next complete matrix must confirm all supported profiles.

The matrix and runner `--of` argument both change from4to5. All platform and
Python combinations, actual test assertions, per-job deadlines and the
aggregate all-green gate remain. There is no per-runner load increase and no
new maximum concurrency setting. The workflow gains nine full-suite jobs;
account-level scheduling may add queue time. The operator pays more setup
work to keep each complete test partition within the measured budget.

Alternatives rejected: raising the hour bound would not correct underestimated
work; reducing test scope would weaken qualification; rebalancing four parts
cannot fit the measured costs; platform-specific weight tables would be a
broader configuration redesign. Updating the common observed-cost table and
using the minimal sufficient count preserves the existing planner.

Primary sources consulted:
- GitHub matrix execution and failure handling:
  https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
- GitHub hosted job and concurrency limits:
  https://docs.github.com/en/actions/reference/limits
- Bazel's original test-sharding execution contract:
  https://bazel.build/reference/test-encyclopedia#test-sharding
- pytest on uncontrolled threaded state and trustworthy failures:
  https://docs.pytest.org/en/stable/explanation/flaky.html

No installed runtime layout, provider contract or test content is changed by
this allocation. Full matrix success is required before installation.
