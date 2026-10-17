# Deferred maintenance is not complete

Research date: 2026-09-29. Scope: the existing scheduler health diagnostic.

The installed nightly pass ended at 13:45:44 UTC with exit status zero, after
its compile wait expired and lint/index/graph work was deferred. The service
manager stopped the unfinished child. `nightly_deferred_compile` remained in
state, yet doctor said "Nightly maintenance is current." The freshness predicate
read the parent status and date without considering this existing marker.
The regression reproduces that false success with the real scheduler check and
a temporary state file. No running service or private knowledge is needed.

## Sources and choice

Three independent primary sources were checked before this correction:

- The installed `systemd.kill(5)` manual, systemd 255.4-1ubuntu8.17, says the
  default control-group policy stops remaining unit processes. It explicitly
  discourages process/none modes that evade lifecycle ownership. The online
  freedesktop manual returned HTTP 403; the installed manual was read instead.
- [Kubernetes Job terminal conditions](https://kubernetes.io/docs/concepts/workloads/controllers/job/#terminal-job-conditions)
  distinguish completion criteria from termination in progress; current versions
  wait for all Job Pods to terminate before publishing terminal conditions.
- [Celery 5.6.3 task states](https://docs.celeryq.dev/en/stable/userguide/tasks.html#states)
  distinguish successful execution from a retry and failure. This is a comparison
  of completion semantics, not a recommendation to add Celery or Kubernetes.

The best fit for the existing local system is to report degraded health while
the persisted deferral marker is present. Completing compilation alone does not
prove the dependent steps ran. The diagnostic exposes the marker and does not
clear it or guess process liveness from its timestamp. A recorded failed nightly
still takes precedence and remains an error. A completed fresh pass with no
outstanding marker keeps its existing healthy result.

Ignoring the marker gives false assurance. Clearing it in a read-only diagnostic
destroys the only pending-work evidence. Changing KillMode lets child processes
escape ownership and does not ensure dependent steps run. Increasing the wait
without workload evidence only postpones the same condition. None is part of
this correction. The tradeoff is conservative degraded health until maintenance
clears its own marker; this correctly includes a compile that finished later.

This uses existing state and result fields, adds no dependency, schema, directory,
environment contract, runtime process, LLM call, or numeric operational limit.
It is compatible with states that predate the marker. Runtime lifetime and backlog
recovery remain separate unresolved work; correcting health output does not fix
the interrupted compile. No legacy implementation is retained or replaced here.

## Verification

Before the production correction, all three regression cases (compile running,
successful, or failed with dependent maintenance still deferred) fail because the
check reports `ok`. The failure and completed-pass controls pass. Tests also
require the original state file to remain byte-identical and the marker to be
visible in diagnostic details. Qualification results are recorded after running
the corrected code, rather than inferred from this design.

The corrected scheduler/doctor suite passed 187 tests with three skips. The
repository's Python/JavaScript/PowerShell/shell complexity and branch-shape gates
passed all 32 tests. Ruff passed for scripts, tests and benchmark. Running doctor
against the installed vault now reports scheduler `degraded` and names the
outstanding deferral; the previous run reported `ok` for the same marker.
The full current suite and completion of real maintenance are not established by
these targeted checks.
