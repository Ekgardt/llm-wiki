# Health does not start work after its deadline

Date: 2026-09-29. Investigated after the Windows Python 3.12 CI health-context
latency check exceeded its unchanged 0.5-second threshold. The same test took
0.10 seconds on Linux; the Windows log alone does not identify the slow call.

An independent deterministic reproduction found that `_collect_checks` always
started its first eight checks, even when an earlier check had already exhausted
the caller's budget. Only the later checks used `_completed_or_deferred`.
`_run_deletion_check` also reopened adoption before checking that deadline.

All health checks now use the existing admission guard. The special LSP path
still executes its own expired-deadline handling. A completed early error keeps
its verdict; checks that never start report budget exhaustion. Runtime deletion
observation checks its budget before adoption and returns unknown, with both
`permit` and `quiescent` false, if no time remains. This neither grants deletion
nor claims that an unmeasured store is healthy. The redundant deferred-call
wrapper is removed. No timeout is increased and no new limit is introduced.

The existing expired-deadline LSP test previously expected an adoption diagnosis
obtained by doing work after the deadline. It now requires the honest unknown
deletion blocker and explicitly verifies that deletion remains forbidden; its
LSP-call and unreadable-state assertions remain unchanged. Two new regression
tests fail on the old code when it starts runtime/adoption work after expiry.

This corrects a demonstrated deadline-admission defect. It does not prove that
every individual filesystem operation can be interrupted, or by itself explain
the Windows runner's entire delay. The original latency test and Windows CI
remain required verification, as does the cold MCP health scenario.
