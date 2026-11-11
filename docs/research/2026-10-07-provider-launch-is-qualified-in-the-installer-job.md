# Qualify provider launch in the existing installer job

Research date: 2026-10-07.

The provider bootstrap has native CMD, configured PowerShell, Windows file
identity and DACL checks. Linux skips do not qualify those contracts. The full
Windows shards retain these tests, but reaching the final shard result previously
took more than half an hour. The installed launch contract needs direct evidence.

The existing three-platform installer job therefore also runs the three provider
test files. It keeps every earlier installer step, the full platform/version
matrix, failure propagation, concurrency, ownership and deadline rules. The new
step is an ordinary pytest invocation: no simulator, host override, new job,
runner, runtime path, model call or relaxed check. Native skips remain visible.
Its success qualifies the tests on that actual runner and Python version only;
it does not qualify every supported version or replace the full regression gate.

Alternatives considered: waiting only for the full shard delays this independent
qualification; adding a separate matrix duplicates runner setup; manual rerun is
unavailable with the current token; treating Linux controls as Windows evidence
is incorrect. Reusing the installer matrix gives the exact native runtime with
less repeated setup. This is a project-specific choice, not a general claim that
focused tests prove product completion.

Primary sources checked on the research date:

- [pytest test selection](https://docs.pytest.org/en/stable/how-to/usage.html):
  explicit module filenames select the complete files.
- [GitHub matrix jobs](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations):
  platform combinations execute on their respective runners.
- [Microsoft PowerShell parsing](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_parsing?view=powershell-7.6):
  native argument passing differs by runtime; shell support needs actual execution.
- [GitHub workflow concurrency](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency):
  the existing cancellation and pending-run contracts remain applicable.
- [GitHub rerun permissions](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs):
  reruns require the relevant repository permission and retain the original commit.
