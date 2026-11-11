# A nightly wait does not own every compiler

Date: 2026-10-03. This is a diagnostic correction, not a compiler lifecycle or scheduler architecture change.

## Observed defect

The nightly pass waits for an already running compiler as well as one it starts. After its wait expires, it unconditionally said that a service manager stops that compiler when the pass exits. The current compiler was still alive after a later pass ended. Read-only process and service inspection confirmed that the compiler belonged to a login-session cgroup, while the nightly user service had already exited. Parent PID 1 alone does not establish service ownership.

The installed environment is Python 3.12.3, systemd 255.4 on Linux 6.8.0. The project retains Python 3.10 compatibility. Evidence is retained privately in `logs/audit-2026-10-03-parent-live-compile-scheduler-ownership.json`. The built-in architecture query used the current repository generation and reported its incomplete graph explicitly; the actual owner, source, call path, tests and installed unit were checked independently.

## Current primary sources and alternatives

Three independent primary sources were checked on this date:

- [systemd upstream KillMode documentation](https://raw.githubusercontent.com/systemd/systemd/main/man/systemd.kill.xml): `control-group` stops the processes belonging to that unit's cgroup, not an unrelated compiler merely observed by it. The upstream manual endpoint returned HTTP 403; the upstream source was read instead.
- [Linux kernel cgroup v2 documentation](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html): process membership identifies its control group and the actual hierarchy must be inspected.
- [Python 3.10 subprocess documentation](https://docs.python.org/3.10/library/subprocess.html): process spawning, session creation and waiting are separate operations. Waiting on work does not establish that the waiter owns its service lifecycle.

The options were to keep the unsupported prediction, add platform-specific owner detection to every deferred pass, change the service's process ownership policy, or report only the known outcome. The last option is selected: the compiler is still running, its result is unknown, and this pass ending does not establish whether its owner stops it. This corrects the claim without a new platform layer, configuration, limit, delay, lifecycle policy or runtime contract. It deliberately does not diagnose why compilation is slow or prove eventual successful processing.

## Regression and preserved behavior

The new regression failed on the installed wording and passes on the candidate. It invokes the real nightly orchestration with test-only step and wait observations. It verifies that existing failure counts remain intact, deferred work is recorded, the outcome is reported as unknown, and termination is not predicted. All 30 related scheduler tests pass. Production wait durations, process signals, outcome reconciliation, source evidence and recovery state remain governed by their existing contracts.

The stale unconditional explanation was replaced in the runtime message, its receipt-helper docstring and the related test module documentation. No second implementation or compatibility switch is introduced. Verification and installation records are retained as recovery evidence; they are not disposable until the change and rollback are qualified.
