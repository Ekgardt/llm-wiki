# Live observation refusals retain their cause

Research and verification date: 2026-10-01. Compatible diagnostic corrections; no new runtime, resource limit, architecture, or automatic repair.

A real file created between corpus-root sealing and descriptor opening reproduces PermissionError in the existing containment check. Doctor previously attributed this to immutable generation corruption. Generation artifact validation still runs first. Live collection now distinguishes a refused source observation from an invalid immutable artifact. It reports error, unknown freshness, partial observation, and no automatic repair. SettingsError from actual invalid live settings is also a source refusal. Capacity and deadline handling retain their existing contracts. Corrupt immutable manifests retain corruption handling.

Runtime deletion observation caught errors twice. Its inner catch discarded the cause before the outer observer could report it. The redundant inner catch is removed; the existing outer safety boundary retains the same unknown-state blocker and false deletion permit and includes the redacted actual error. Real exclusive SQLite locking and POSIX permission refusal reproduce the missing diagnostic. This does not establish the cause of the earlier Windows database-replacement test failure. That test keeps its exact assertion and now includes the observation result on failure.

CI run 36905835874 failed. Some other jobs were cancelled and establish no success. Two Windows test fixtures wrote text whose platform newline translation changed their asserted byte counts; they now write explicit ASCII bytes. Production byte accounting is unchanged. Three previously added test files lacked measured shard weights. Their timings and those of the two new files must be measured using tests.shard_plan --weigh, not invented or bypassed.

## Sources and alternatives

- [Microsoft health endpoint monitoring](https://learn.microsoft.com/en-us/azure/architecture/patterns/health-endpoint-monitoring): component health needs meaningful diagnostic context; read-only probes should distinguish transient observations.
- [Google SRE monitoring](https://sre.google/sre-book/monitoring-distributed-systems/): distinguish the observed symptom from its cause.
- [Python 3.10 exception hierarchy](https://docs.python.org/3.10/library/exceptions.html): preserve concrete exception types rather than conflating different failures.
- [Windows CreateFile sharing contracts](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew): file replacement depends on sharing and delete access. This is relevant background, not proof of this CI failure's cause.

Rejected alternatives: weaken directory seals, report healthy on a failed scan, rebuild immutable artifacts in response to an unverified live source, suppress errors, raise budgets, or weaken Windows assertions. The chosen correction preserves refusal and records the cause. The tradeoff is that a changing live corpus can still be unverifiable; the operator receives an accurate failure rather than an unjustified corruption claim.

## Evidence and remaining qualification

Real before-change reproductions and related regression results are retained privately under logs/audit-2026-10-01-*. New tests use actual source changes, invalid settings, immutable artifact damage, SQLite locks, and filesystem permissions. Linux verification alone does not qualify Windows. Full audit completion is not claimed: historical completeness, all seven native event paths, and the basis of all numerical bounds remain partial.
