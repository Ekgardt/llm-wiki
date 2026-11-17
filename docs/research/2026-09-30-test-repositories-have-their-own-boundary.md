# Test repositories have their own boundary

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.

Date: 2026-09-30. Status: qualified isolated changes; not installed.

The initial whole-suite attempt stopped after 491 test outcomes and 15 failures.
Fourteen failures came from temporary directories inheriting an inaccessible
ancestor Git marker. One ordering test assumed this checkout had a readable HEAD,
although the isolated verification copy intentionally has no commit. These were
fixture failures, not evidence that the namespace correction caused regressions.

## Evidence and correction

The native repository index was verified fresh before graph investigation.
Callers of `_scope_at_commit`, `resolve_repository_scope`, and the two generation
fixtures, plus the callees of `_intake_steps`, were examined. The graph reports
incomplete coverage, so the actual test setup and production readers were read.
The existing rejection of an inaccessible Git marker remains unchanged.

Fixtures that require a repository now initialize their own real Git repository.
The ordering test supplies a known HEAD-arrival timestamp through its existing
dependency; its separate no-HEAD test still checks that redrive is absent.
No production timestamp is fabricated, no commit is created in the installed
vault, and no tests are skipped to conceal these failures.

Running the entire generation-catalog file exposed the same setup issue in its
shared v2 publisher and further repository-scoped cases. Their repository setup
was corrected too. The next run reached 169 passed, 3 existing platform skips,
and two different failures involving a missing declared artifact.

## Missing declared files

Both existing negative tests refused the damaged generation but received a raw
`FileNotFoundError`, outside their existing validation-error contract. Source and
fresh native graph investigation traced this to `_ArtifactScan._verify` through
the metadata/digest validation path. The correction converts only this specific
exception into `ValueError` naming the missing declared artifact, with the original
exception preserved as `__cause__`. Permission errors, cancellation, deadlines,
and other failures are not converted or suppressed.

The existing missing-file assertions were strengthened. Two additional cases
remove the file between metadata inspection and hashing, or after hashing and
before the final metadata check. Both must retain the original missing-file cause.
The resulting related-file run passed **173 tests**, with **3 existing platform
skips**, in 40.91 seconds. It includes the real global CCN/branch-shape gates.
Ruff passed for every file changed in this slice. A new complete-suite attempt
was then started with stop-on-first-failure for diagnosis; this checkpoint does
not claim that the complete suite passed.

## Sources and alternatives

Primary references checked on 2026-09-30:

- [Git init](https://git-scm.com/docs/git-init): initializing an empty repository
  establishes its local repository metadata without requiring a commit.
- [pytest monkeypatch](https://docs.pytest.org/en/stable/how-to/monkeypatch.html):
  a test can provide a dependency value with automatic restoration afterward.
- [Python exception chaining](https://docs.python.org/3/tutorial/errors.html#exception-chaining):
  `raise ... from ...` preserves the cause when translating an exception.

Observed tools: Git 2.43.0, Python 3.12.3, pytest 9.0.3. No dependencies or versions
were changed. These existing mechanisms are also available on the project's
Python 3.10 floor; other platforms have not been qualified by this Linux run.
The current Python documentation resolves to 3.14; no 3.14-only feature is used.

Alternatives rejected: weakening repository containment, deleting the protected
ancestor marker, skipping failing tests, assuming the host's Git history, and
accepting any exception as successful validation. Real fixture repositories cost
small local Git initialization work but exercise the actual scope resolver.
Exception translation stays at the declared-artifact validation boundary, not
in the general filesystem reader where missing files can have other meanings.

## Deployment and remaining work

All six changed code/test files remain in
`/tmp/llm-wiki-provider-verification-ewdevhsa`. The installed runtime code was not
changed. Before-image hashes of the five test files still matched the installed
checkout when verified. Diagnostic evidence and a reviewable patch are retained
under `logs/audit-2026-09-30-fixture-repair-*`.

The socket test remains blocked by the environment's `EPERM`. The namespace
candidate still needs a safe old-client shutdown, legacy-identity recovery,
complete regression and platform qualification, installation, and post-install
cleanup. A read-only inventory at 00:25:59 UTC found no registered owners, writers,
or project leases; it does not prove that idle old clients have stopped. Queue
counts were 23 cancelled, 25 dead, and 167 succeeded. No production owner or queue
record was cleared. Private progress/log writes and production index refresh
remain deferred because the installed cross-namespace ownership boundary is unsafe.

No new architectural path, runtime root, operational limit, dependency, or LLM
call was introduced. The current patch has no obsolete parallel implementation
to remove; installed-system cleanup remains pending the safe transition.
The broader audit and compliance of the completed release with all nine laws
are not certified by this checkpoint.
# Follow-up qualification, 2026-09-30

The continued diagnostic suite exposed two more bare checkout fixtures in
`test_a_question_is_answered_by_its_own_kind_of_generation.py`. Their inherited
`/tmp/.git` marker prevented scope resolution (two failures reproduced separately).
Each checkout now initializes its own Git boundary; no repository refusal was
weakened. A separate PID-reuse fixture in
`test_a_reused_process_number_does_not_keep_a_dead_attempt.py` used the arbitrary
string `another-start` as proof of death. The scoped reader correctly treats that
string as unknown. The fixture now changes only the start ticks of a valid
same-scope identity, and two additional cases preserve an attempt whose stored
identity cannot establish its scope.

The two-file correction exists only in the isolated candidate. The targeted
group passed 70 tests in 50.24 seconds, including the actual CCN and AST gates;
Ruff passed. Native graph queries preceded editing, from fresh generation
`generation-18d9f7530e649c73-2f8e0432`; incomplete graph coverage was checked against
source. The broader diagnostic run began before these test corrections and is
not final qualification of the amended test tree. Runtime code is unchanged by
this correction. Evidence prefix: `logs/audit-2026-09-30-fixture-followup-`.

The same diagnostic run also exposed a separate unresolved compatibility issue:
legacy v2 writer rows do not store a process start identity, so immediate recovery
after a real child crash cannot prove death with the scoped reader. The
`after_applying` append crash test independently failed with a writer-gate timeout.
This is not resolved by the fixture corrections above, and expired leases must
not be substituted for missing ownership evidence without validating the actual
protocol. Safe upgrade/recovery remains open.

The adopted-v3 recovery group then passed 72 cases and failed one generation
maintenance case for the same missing Git boundary in
`test_doctor_maintenance_owner_v3._adopted_vault`. That local fixture now initializes
its vault repository before adoption. Re-running the group with the complexity
gates passed 84 tests in 65.33 seconds; Ruff passed. This qualifies those v3
scenarios, not legacy v2 recovery or the full runtime. Native queries before the
edit used fresh generation `generation-18da0df77d732f30-d7dce7c1`. Evidence prefix:
`logs/audit-2026-09-30-v3-recovery-`.
