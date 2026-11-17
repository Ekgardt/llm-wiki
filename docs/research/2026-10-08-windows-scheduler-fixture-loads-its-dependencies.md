# Load the Windows scheduler fixture's complete function dependencies

Date: 2026-10-08.

The full CI run for `6e3f3c5d` reproduced a scheduler-status test failure on
Linux, macOS, and Windows. The fixture constructs a PowerShell library by
selecting named functions from the production script's syntax tree. Its list
omitted `Get-LLMWikiConfiguredLimitHours`, which the status checker now calls.
The complete production script already defines that function.

Native PowerShell 7.6.6 reproduced `[false, false]` instead of the expected
`[true, false]` before correction. The fixture now loads the missing dependency
and stops immediately on a PowerShell error. The assertion is unchanged: a
matching task is accepted and the incompatible service-account task is refused.
No production PowerShell parameter or scheduler behavior is changed.

The registration test also exercises the complete script with omitted settings,
as well as explicit settings. Both use the existing expected limits. This
disproved the initial hypothesis that omitted integer arguments broke the
production defaults.

Qualification: the native related Python 3.10 run passes 123 tests with 10 skips.
Actual AST and Lizard analysis measures both changed Python functions at CCN
at most 2, with the required branch and nesting limits. The generation-window
file's shard weight is 1.01 seconds, rounded upward from the successful run's
1.005-second summed testcase durations. These results do not establish that
the full CI run, installed generation, or audit point 7 has completed.

Private evidence:

- `logs/step7-ci-6e3f3c5d-macos-py314-s3-job-api-20261008.log`
- `/dev/shm/step7-windows-omitted-window-causal-red-20261008.xml`
- `/dev/shm/step7-windows-function-fixture-related-20261008.xml`
- `/dev/shm/step7-windows-function-fixture-static-20261008.json`
