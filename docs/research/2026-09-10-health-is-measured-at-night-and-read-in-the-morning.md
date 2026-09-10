# Health is measured at night and read in the morning — 2026-09-10

**Question.** Issue #23.5: every session start prints "Health was not
measured: N of 18 checks did not run inside the 0.1s budget", on a vault that
is healthy. The budget is right (session start must not wait on the doctor,
which wants 1.77 s here) and the line is honest, but it is the same line every
morning and it teaches the reader to skip the block. What should session
start say?

**What the field does.**

- AWS Builders' Library, "Implementing health checks": expensive dependency
  checks run in the background on a timer and the request path returns the
  cached result with its age; a check that runs inside the request budget
  measures the budget, not the system.
  https://aws.amazon.com/builders-library/implementing-health-checks/
- Google SRE, "Monitoring Distributed Systems": a signal that cannot be acted
  on is noise, and noise trains people to ignore the channel that will one
  day carry the page. https://sre.google/sre-book/monitoring-distributed-systems/
- Kubernetes probes separate `readiness` (is it usable now) from a periodic
  `liveness` check with its own period and timeout; neither is run on every
  request. https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/

**What we keep.** The nightly pass (`scripts/scheduled_nightly.py`) already
runs under a 15-minute generation budget and already writes maintenance
reports under `logs/`; session start (`scripts/session_start_context.py`)
already reads `run/state.json` for the nightly line rather than measuring.
`doctor.run_doctor` is the one measurement and `degraded_summary` the one
rendering.

**Decision.** The nightly writes one full doctor report to
`logs/doctor-report.json` (60 s budget, schema `health-report/v1`, with the
time it was written). Session start reads that file when it is younger than
36 hours and shows its degraded summary with the measurement time, or nothing
when it was healthy. Only when there is no recent report does session start
fall back to the 0.1 s budgeted run and the "not measured" line, which then
means what it says: there was no night. `logs/` is disposable, so the file
needs no deletion contract.

**Limit.** A problem that appears after the night is not on the block until
the next night or a manual `doctor.py`; the block names the measurement time
so the reader knows what it covers.
