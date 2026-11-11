# Shard timings retain platform evidence

Date: 2026-11-03. Internal correction under the approved decomposition plan.

Run 41720238934 at c2dd7466 cancelled Windows shard 4 at the existing hour
limit. Its report contains 2912 cases and 3481.29 summed case seconds. The
remaining Windows shards completed in approximately 1887–2278 seconds. Local
focused measurements had replaced slower platform costs: blackboard was
weighted at 15.81 seconds but took 229.82 on Windows; compile publication
checkpoint was weighted at 4.22 but took 108.74. This evidence identifies
incorrect cost inputs, not a test hang.

`refreshed()` will retain the maximum of recorded and incremental measurements
by default. `--weigh` uses that default. Existing `--refresh` remains an explicit
replacement operation, passing a keyword-only option to the helper. Supply a
reviewed complete cross-platform artifact set to that operation; a report from
a cancelled job cannot prove the full cost of its incomplete file. Deleted
test files leave the table in either mode. JSON, test discovery, LPT packing,
five shards, job deadlines and all test assertions remain unchanged.

For this correction, use the latest Linux/macOS/Windows reports plus all five
Windows reports from successful run 41713078655. The earlier complete Windows
measurement covers the incomplete current file. New files absent from native
reports retain their existing local measurements. SHA256 verification against
GitHub artifact metadata precedes reading each downloaded ZIP.

Preserving maxima forever would predict 3677 seconds per shard from the mixed
historical table. Refreshing from the reviewed native evidence predicts about
2691 seconds per shard. These are estimates, not a native pass or a guarantee
against runner variability. A new native run must check the resulting plan.

Alternatives considered: raising deadlines masks the observed imbalance;
unconditional incremental replacement reproduces it; separate per-platform
tables add migration that this repair does not require. Explicit replacement
allows genuine speed improvements to retire stale costs, while conservative
incremental updates avoid forgetting slower platforms.

Regression checks cover a faster local measurement preserving a recorded
Windows cost, higher measurements updating it, new/deleted files, the CLI
incremental path and explicit replacement. Existing failed-pytest checks
continue requiring unchanged weight bytes.

Validation: the two lower-cost regression paths failed before the fix;
31 focused checks and 722 integration/context/structure checks pass after it.
Actual function complexity and Ruff pass. All 136 exact context artifacts
round-trip, largest 98828 tokens. The reviewed refresh excludes the 63 recorded
cases of the incomplete current Windows `test_pyright_profile.py`; its earlier
complete Windows report supplies that file's cost. All test files remain
assigned exactly once. New native CI and a complete local regression are pending.

Sources reviewed today: [pytest output and JUnit documentation](https://docs.pytest.org/en/stable/how-to/output.html)
describes report measurements; [GitHub runner specifications](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
show differing execution environments; [Google Benchmark variance guidance](https://google.github.io/benchmark/reducing_variance.html)
describes environment-dependent measurement variability. The merge policy is
a project-specific conclusion from these sources and the actual CI reports.
