# Refactoring of compilation, the queue, diagnostics and nightly maintenance

Research date: 2026-10-17. Status: recorded as approved by the owner on 2026-10-17 in the working session of that
branch, which the repository does not hold; implementation and installation are not yet complete.

The goal is to separate checking current work from checking the whole accumulated history, to make the nightly cycle's results unambiguous, and to stop blocking usable sources because of a neighbouring source that is too large. This is a change in the behaviour of diagnostics and execution. The owner gave explicit approval under section 0 of AGENTS.md; the decision is recorded in knowledge/notes/compile-queue-health-nightly-refactoring-decision-20261017.md. No new databases, daemon, dependencies or runtime roots are proposed.

## What is confirmed now

In the installed code, the check for completion of the compile process calls `maybe_compile.status()`. When the process has already finished, this function additionally checks the accumulated daily logs and the authority of receipts. So the completion of the process can be hidden behind a long history check or its error. The fix in a separate working copy separates `process_status()` from the full `status()`. The original failure is reproduced by a test; 69 related tests pass. The code is not yet installed. In addition, a distinction between a physical SQLite error and a foreign-key violation has been prepared without changing the checks: two new tests reproduced the previous ambiguous message; the related checks gave 117 passed, 5 skipped. The complexity of the changed function is 4; Ruff and the Python 3.10 grammar pass.

Profiling `doctor` with a 60-second budget showed about 17.5 seconds on database certification and 42.8 seconds on transactions. Most of the following sections were not checked. This is a measurement with profiler overhead, not a clean performance test. The full check really is needed: it found corruption of five indexes of the coordinator's owners table. Index recovery was verified on a separate online copy, then performed under the existing maintenance owner in one SQLite transaction. The coordinator checks passed and the owner rows were preserved. The cause of the corruption is not established; this is a recovery, not proof that the cause has been removed.

When checking the packing of the current daily log with the installed Codex, model gpt-6-luna and reasoning medium, 12 of 63 parts exceeded the computed budget. The implementation excluded the whole day, including the other 51 parts. The numbers refer to a saved snapshot; the daily log keeps growing. The size calculation here is not a measurement of the provider's actual tokens. The provider did not give a confirmed planning window. Raising the assumed model capacity without evidence is not allowed.

The nightly cycle already reports a deferred compile with a separate flag, and doctor takes it into account. However, the overall result of a pass can contain `success` and deferred dependent stages at the same time. This is an ambiguous contract, not a total absence of diagnostics.

The evidence is kept privately in `logs/refactor-20261017-*`: profiles, a snapshot of the packing refusal, the original failing test, check results, and the corruption and recovery checks. Their content does not need to be published with the code.

## Proposed behaviour

### Compilation

Planning, execution and write confirmation become separate sequential steps inside the existing mechanism. The unit of work is a whole verified source element with its required context. Linked parts of a native event stay one unit. Splitting must not break citations, provenance, source hashes or required context.

A refusal of one independent unit does not prevent processing the others. At the same time, the day is not marked fully processed while unconfirmed units remain. For each refusal, the reason, the source identity and the possibility of later execution are kept. A refusal because of the size of a stored source is not counted as a capture loss. Historical error counters are not cleared.

The working batch size and the confirmed provider capacity are different quantities. Unconfirmed capacity stays unknown. The policy for a source larger than the working batch must rest on verified splitting or on confirmed provider capabilities. Until such qualification the source stays explicitly blocked; raising a number in the configuration does not count as a fix.

Reuse is allowed for pure parsing of unchanged bytes within one plan. Checks of current rights, owners, current sources, complete citations and write conditions are performed again at the corresponding boundaries. Efficiency measurement covers planning, all model calls, corrections, verification and the confirmed useful result.

### Queue

Keep the existing Reliability V3 databases, at-least-once delivery, payload hash checks and owner protection. A normal operation checks its own request, state transition and related records. A full survey of all completed tasks does not become its hidden dependency. The already implemented separation of actor admission from history certification is kept.

Check the real SQL plans for selecting ready work, expired owners and results on the installed data volume. Changing indexes or the schema, if needed, will require a separate migration description and approval. History is not deleted to speed up a test. Retries belong to one execution level and are not multiplied between the scheduler, the queue and the provider.

### Health check

Split into two explicitly named scopes: operational state and the full integrity check. The operational check answers about availability, owners, active work, backlog, recent outcomes and known errors. It does not give a verdict on the physical integrity of all historical records.

The full check keeps SQLite integrity_check, foreign keys, semantic invariants, artifacts and provenance checks. For each scope the report states the coverage, the observation time and the completeness. The last historical result does not stand in for the current certification of a changed database. An interrupted check stays incomplete. Known corruption stays an error until a confirmed recovery.

Proposed interface: `doctor --scope full|operational`; the default `full` keeps the current purpose of the CLI and the installer checks. MCP and the automatic session context explicitly use `operational`. The report explicitly names the scope of the check; the overall status refers only to it, and a separate item reports the last full check, its age and known failures. The full nightly report stays in `logs/doctor-report.json`; the short operational result does not overwrite it. A change to the response schema and all its consumers are updated together. The quick check never permits deleting `run/`; the current full deletion conditions are kept.

### Nightly cycle

Keep one sequential pass with explicit stage dependencies. Each stage returns completion, an error or a reason for deferral. Overall success is allowed only when the required stages are complete; the absence of errors in the stages that ran is not enough.

The compile result must refer to a specific started or observed job, not be inferred only from a change of a global timestamp. A repeated nightly run uses the existing receipts and task identity. It does not promise exactly-once execution and does not delete sources on the mere fact of enqueueing.

The full history check runs as a separate explicit stage. Its cost and completeness are visible in the final report. Operational diagnostics do not wait for this traversal. Raising a timeout does not replace removing repeated work.

## Grounds for the choice and alternatives

Sources checked on 2026-10-17. These are three independent primary sources; the chosen scheme is a conclusion for this project, not a ready recipe from one document.

- [Google SRE, Monitoring](https://sre.google/workbook/monitoring/) distinguishes fast operational signals from detailed history analysis and discusses freshness and the cost of large samples. Hence the choice of two explicitly labelled check scopes.
- [AWS Builders Library, Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/) explains load amplification by retries and the need to account for side effects. Here this is the ground for checking who owns retries and for keeping idempotency, rather than adding random delays.
- [SQLite, Transactions](https://www.sqlite.org/lang_transaction.html) describes read snapshots, the single writer and locking behaviour. This is the ground for keeping short consistent reads and transactional transitions, without promising that a snapshot is current after it ends.
- [SQLite, How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html) describes the danger of a third party closing a file descriptor while connections are open, and safe ways to copy. This mechanism was fixed in the project earlier; its connection to the present corruption is not proven.

Installed runtime: Python 3.12.3, SQLite 3.45.1, local ext4. Minimum compatibility with Python 3.10 is kept. Switching to WAL, replacing SQLite, installing a queue server or a background daemon are not proposed: they expand the system and do not remove the proven redundant traversals. Platform support should be confirmed by the existing CI matrix, not only by Linux tests.

The alternative "raise all timeouts" leaves the cost dependent on the whole history and does not resolve the ambiguity of outcomes. The alternative "cache a green health result" hides new corruption. The alternative "delete history" breaks recovery and data retention. Splitting the scopes makes the result more precise, but requires updating all status consumers and explicitly showing the unchecked scope.

## Acceptance under the nine laws

1. Before each part, research the current graph, calls, configuration and running processes; supplement incomplete graph information from sources. Update the index after installation.
2. For contract changes, record the owner's decision and update STRUCTURE before the code; for new design questions, extend the research.
3. Report separately what is prepared, verified and installed. The present index recovery does not close the cause of the corruption.
4. Compare the original and new implementations on identical snapshots, including the full useful model cycle, latency, memory and operator attention.
5. Measure the CCN of each changed function, the nesting and the number of ifs. The 5/2/2 thresholds are mandatory for code and tests.
6. Keep refusal on a security violation, mandatory checks and errors. Do not mark the unverified as successful.
7. First reproduce the original defect, then get the regression and related scenarios to pass. The cause of the physical database corruption stays open until proven.
8. After a verified installation, remove the replaced calls and implementations, and update the documentation and the index. Keep existing historical formats only with named consumers and a removal condition.
9. Do not introduce new numeric limits without grounds. Check existing limits against resources, the provider and contracts; do not treat unconfirmed values as technical capacity.

Completion criteria: compilation of the problem source to a verified result or to an exact unresolved limitation; correct retry after interruption; the queue does not lose payload; diagnostics cover the declared scope; the full nightly cycle has an honest outcome; installation and cleanup of replaced code are confirmed. Local tests and a separate database recovery alone do not close the whole task.

## Implementation in the working copy, 2026-10-17

The changes are not yet installed. Admission now separates atomic units instead of excluding the whole day. Required context and physical boundaries are kept. A successful commit does not lift a refusal for another occurrence of identical bytes. Recording a stored compile refusal as a capture loss was removed; the refusal itself is kept in the queue. Historical counters were not reset automatically.

In a paired preliminary-planning experiment on the same real source and context, the original implementation admitted 0 of 63 parts; the candidate admitted 51, leaving 12 refusals. The candidate completed this phase in 4.905 seconds. This is not a measurement of provider tokens and not a check of the useful result: the model was not called. A separate initial probe used premature assembly of the final context for all requests and was interrupted; it cannot be compared with the production sequential path as evidence of speed.

The operational `doctor` checks writer admission, the environment, runtime directories, capture diagnostic records, the schedule, models, settings, hook errors, LSP, file locking and the SQL counters of the queue/transactions. It does not check the content of the whole history, its artifacts, archives, claims, backup, checkpoints, MCP/integration installation, Pyright installation or generation completeness. The response explicitly contains `scope`, `coverage` and `duration_seconds`; these are additional fields of the existing JSON response. MCP status passes on the scope, coverage and measurement time. Deleting runtime in operational mode always stays unverified and forbidden. Full mode keeps the previous checks and stays the default. No SQLite schema or index changes are required: the measured aggregates on live databases took about 0.3 seconds for the queue and 0.25 seconds for transactions; the transaction aggregate performs a table scan and does not promise constant time.

The candidate's operational pass over the live system finished in 1.143 seconds with all checks of its scope. It reported real current errors; this is not a certificate of health for the whole system. The session context is still bounded by the previous 0.1-second budget and may be incomplete. Completed checks stay visible; unchecked ones are listed. The historical nightly report is dated separately and no longer stands in for the current check.

The nightly compile runs as its own child process through the existing runner. Its output and artifacts belong to a specific step. The shared compile start/end marks are no longer used to infer its result. The existing configurable `MEMORY_COMPILE_WAIT_SECONDS` is kept; it is now the wait limit for its own child process, whose timeout is a step failure that keeps the receipts already confirmed. A busy foreign compiler leads to an explicit deferral without changing its source pages. Dependent stages do not start after their own compiler fails. The night state distinguishes `success`, `failed`, `deferred`; only full completion updates the last-success mark. An incomplete full health report gives a deferral, a check execution error gives a failure, and a fault found by the check stays in the report itself. This distinguishes completion of diagnostics from the health of the checked system.

Removed: the replaced daily filters, the unused whole-day size check, the fake refusal-recording test and the nightly polling of the result through global timestamps. The historical field `nightly_deferred_compile` is still read until the first successfully completed new pass; then it is cleared, because the new result expresses completion directly. Old receipt formats keep their existing readers and do not become authoritative receipts for the current physical parts.

Checks at this intermediate stage: the combined set gave 1169 passed, 3 skipped; separate checks of physical receipts gave another 3 passed. The analyzer checked 119 changed/new functions; the 5/2/2 thresholds are met; Ruff passed. The first full run was stopped after 1959 passed, 4 skipped to fix the classification of historical refusals; this is not a successful full run. A new full run is in progress. An inaccuracy of the `adoption_error` field found during an additional check was reproduced by a separate failing test and fixed; 70 diagnostics and context checks passed separately, because the main process had already loaded the previous version of the module. The structural linter with the working copy's roots set explicitly found no violations; a separate run on the live memory found a stale compile of the October 15 daily log. Installation and final cleanup are not yet complete.

### Useful cycle with a real provider

A paired experiment was run in two isolated test stores with identical source bytes and required context, Codex/gpt-6-luna, reasoning medium. One source has two independent parts: the first contains two verifiable facts; the required context of the second does not fit into the confirmed working budget. The original implementation refused both parts: zero model calls and zero saved pages. The candidate saved one page with both confirmed facts in 15.889 seconds: two calls (draft and critic), 23,783 input and 571 output tokens. The 15,104 cached input tokens are included in the stated input number and are not added again.

The repeated run took 1.695 seconds and did not call the model. The source bytes are kept, an authoritative receipt exists only for the first part, the whole day stays incomplete, and the refusal of the second part stays in the queue. This confirms the useful cycle on a controlled example, including verification and retry; it does not confirm completion of the whole installed memory. The preliminary experiment without the provider counter is not included in these numbers; its cost is unknown.

Checking the SQL plans on live databases: queue grouping uses `queue_claim_order`, the search for a retry task uses `queue_redrive_parent`, and the refusal count uses the existing `source_failures` index. The transaction aggregate reads the table; the measurement under the current load took 0.465 seconds. These results do not justify a schema change or a new index.

### Dependencies and repeated runs: additional check on October 17

The original daily catch-up contract was broken after an error or a deferral: a new session started the whole pass again, because only the day of the last success was checked. Three regressions reproduced this through the real producer of the nightly result and the consumer at session start. Catch-up now takes the day of the recorded attempt into account separately from the day of success. This is the existing daily periodicity, not a new numeric limit. A manual run stays available; an expired claim without a recorded outcome stays retryable. 99 related tests passed.

CI found CVE-2026-59951 in the pinned `mcp 1.29.0`. Our HTTP adapter creates `StreamableHTTPSessionManager` in the affected stateful mode. Version 1.30.0 keeps Python 3.10 support and fixes session release. The minimum dependency, the lock file and the sync contract that checks it were updated together. In the lock file only MCP changed; moving to the incompatible 2.x branch is not required. 456 MCP, HTTP, resource and sync checks passed, 6 were skipped. Both dependency checks from CI, PyPI and strict OSV, including `torch +cpu`, found no known vulnerabilities. Complexity analysis covered 135 changed functions with no violations.

The SDK's new defaults for HTTP sessions, release after 1800 seconds of idleness and a maximum of 10000 sessions, are upstream's documented protection against a confirmed memory exhaustion. Running requests are not interrupted because of idleness. These are `StreamableHTTPSessionManager` settings, not compile or queue limits; they should be revisited when the available memory or the HTTP load profile changes. The upstream-supported parameters `session_idle_timeout` and `max_sessions` are described in the advisory.

Sources: [the official CVE description](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv), [SDK 1.30.0 changes](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v1.30.0), [the Python requirements in the release](https://github.com/modelcontextprotocol/python-sdk/blob/v1.30.0/pyproject.toml). The previous local full run was stopped after 2514 passed, 6 skipped to replace the vulnerable dependency; it does not count as a full pass. The final full run is in progress in a separate environment with the new lock file.

### Search after a partial result

A check of the dependency on the search index found another defect: the CLI refreshed the generation only on exit code 0. Pages saved by a partly failed pass stayed outside the active index. The regression built a real source index, simulated a confirmed page write and a refusal of another part at the compiler boundary, and then reproduced the absence of the saved fact in search. After the change, a completed CLI attempt refreshes the derived generation regardless of the final compile code; dry-run still does not start the refresh. The failure code is kept. The existing generation fencing, snapshot check, resource limits and explicit builder refusals do not change.

78 generation and search checks passed, 2 were skipped; 13 CLI boundary checks passed after completing the incomplete test argument `dry_run`. In the earlier isolated example with a real provider, the retry took 3.370 seconds, did not call the model and built the index of the previously saved page. Public search over the active generation found both verified facts. The second part is still refused; the compile code is 1. This is a check of the useful result up to search, not only of the presence of Markdown.

CI found three remaining test calls to the removed nightly polling. They were replaced with checks of the new result and migration: an incomplete pass keeps the historical deferral flag, a full pass clears it, and a foreign compile mark does not turn one's own deferral into success. 50 related tests passed. A search for references to the removed functions found no remaining consumers; the same-named `_safe_state` in `episode_consolidation` is a separate live function. Complexity was checked for 145 changed functions; the thresholds are met.

Full diagnostics of the live store finished in 1618 seconds without exhausting the overall budget. This is not a certificate of health: reading transaction artifacts ended with `TimeoutError`, the queue evidence check with `database is locked`, and the generation check found that the configured `corpus.max_total_bytes` was exceeded. The full check was not replaced by the operational one, faults were not cleared, and production timeouts and the corpus limit were not raised. The current candidate's operational pass completed its scope in 1.816 seconds with an explicit error status. Installation and confirmation of the full nightly cycle remain incomplete.

### The child compiler's deadline accounts for index building

A check of the whole chain found an error in the new synchronous call: the outer deadline equalled only `MEMORY_COMPILE_WAIT_SECONDS`, although before exiting the CLI performs a refresh with a separate `generation.post_compile_seconds`. For the installed profile, 1800 seconds would cut off an index for which the configuration allows 6919 seconds. Two regressions confirmed the insufficient deadline and the loss of the target vault's setting in the scheduler's calculation. The child process deadline is now the sum of these existing budgets and the existing launch margin; the overall pass deadline uses the settings of the passed vault. The budgets of the operations themselves were not raised, and no new settings were introduced.

The earlier Windows check also found that the default full budget, 15092 seconds in auto mode, exceeds the fallback four-hour deadline. The shared table and PowerShell now set five hours, the full budget rounded up with the launch margin; the installer still derives a larger deadline from the specific settings. The check of this inequality is kept. A revision is required when the set of steps, the budgets or the provider change; the test checks the sum automatically.

The full local run, started before the last search change, finished with 12671 passed, 234 skipped and three failures of old, already loaded tests of the removed nightly functions. The current versions of the three files gave 21 passed separately. Such a result is not labelled a full pass of the last commit; the current related checks and the CI matrix are counted separately.

### Installation and check state, October 17, 13:25 UTC

Code e09c7b19 was installed by the standard controller; installation record 43 confirms the commit, the lock file and four managed resources. MCP was updated from 1.29.0 to 1.30.0; other installed packages were kept. The local scheduler got seven hours: its 25200 seconds exceed the computed 24668 seconds of the auto profile with the working settings. This installation state refines the earlier records above about a not-yet-installed candidate.

Operational diagnostics of the installed version finished in 1.344 seconds with error. A new MCP process offered the same 12 tools and returned an explicit operational scope with exclusions. Already open processes keep the old modules until a normal restart; active sessions were not interrupted. An isolated repeat gave 73 passed. Running these tests directly in the live vault also recorded that the standard-deadline test is not comparable with the working settings, and new files from parallel capture; this run does not count as successful. The actual service and the working deadline calculation were checked separately.

CI found a missed old number in the guide and an installation test whose substitution of `TimeoutStartSec=4h` no longer changed the file. The guide was synchronized. The test now takes the original deadline from the configuration and requires the substitution to actually change the line; the checks that a user change is preserved were not weakened. The full CI matrix and completion of the full nightly pass on the working corpus remain separate open checks.

### The full capture traversal uses the queue budget

A repeated full doctor finished in 506.555 seconds: transactions were read without `read_error`, and one unresolved refused operation stays visible. An empty unregistered preparation leftover was removed beforehand by the existing method, only after checking for the absence of a row/operations, empty images and proof of the owner's death. This is recovery of a leftover, not establishing the cause of the process's death or of the earlier SQLite corruption. The different passes ran under different loads; their durations are not a paired performance comparison.

The traversal of stored captures failed twice with `database is locked`. A regression on real SQLite reproduced the failure with a commit longer than doctor's initial 250 ms wait but shorter than the budget of the calling full survey. Another test showed that the previous wait was not reduced to the remaining 125 ms. Both checks fail on the original function. Now the existing `busy_timeout` is refreshed before each SQL batch: it takes the already established queue budget `DEFAULTS.queue_busy_ms`, and a shared helper bounds it by the remaining deadline. The content checks do not change, and no delays or retries are added on top of SQLite; a completed SELECT still releases the reader before the file check.

Grounds: [SQLite busy timeout](https://www.sqlite.org/c3ref/busy_timeout.html) and [Python sqlite3 timeout](https://docs.python.org/3/library/sqlite3.html#sqlite3.connect), checked on 2026-10-17; the general choice of short operational signals and a full survey also rests on the Google SRE and AWS research cited above. No new numeric limit is introduced. A fresh ROOT graph for 1166e92a was researched; a separate working copy was created from the same commit.

83 related checks passed, 3 were skipped; another 31 deadline and consumer checks passed, and 2 PowerShell checks were skipped because the shell is absent. On the live corpus the candidate checked 100382 stored captures in 113.826 seconds without a read error. The subsequent full queue survey took 152.430 seconds: `read_error=false`, no incomplete captures, and two finished pending records were kept for recovery. The queue stays degraded because of real pending work, rather than being declared fully healthy. This is not proof that the full nightly cycle completed. Complexity analysis covered 154 changed Python functions with no violations.

CI also found the old value 4 in the expected result of a PowerShell test; the expectation is now taken from the installer's shared table, and explicit operator values are checked as before. A manual update also revealed a procedure error: calling the low-level installer without the saved environment replaced the provider choice. The original commands were restored from the hashes of the previous installation, and Codex hooks trust was confirmed again. For the next update it is mandatory to read the verified `installed_codex_provider_bundle` before the change and to compare the desired fragments before writing; the trust settings themselves were not changed. The effect of the interval with changed hooks on capture completeness is not separately proven.

### Configurable full survey and limit review, October 17

The working session records that the owner said to continue after a specific settings
proposal and separately required all limits to comply with law 9. The decision and STRUCTURE were recorded before
the code change. The registry now contains `doctor.nightly_seconds` with the usual environment
alias; the nightly doctor and the outer deadline calculation read one value of the selected
vault. Invalid values are explicitly refused. The previous 60 seconds is a compatible original
profile whose sufficiency for an arbitrary corpus is not proven. The mere existence
of a setting does not prove that its value is justified. For this vault
3600 seconds is proposed: two full surveys took 507 and 1618 seconds, and the margin
accounts for the observed load variation at a bounded maintenance cost.
A revision is mandatory after growth of history, load or a change of resources.

Three independent sources were checked again on 2026-10-17: Google SRE Monitoring,
AWS Timeouts/retries/backoff and SQLite Transaction (links above). The choice of 3600 is
a local operational trade-off, not their numeric recommendation. Alternatives
and consequences are recorded in the decision. No checks were excluded for the sake of the deadline.
New regressions reproduced the absence of the setting; then 97 related checks
passed, and 2 PowerShell checks were skipped locally. The complexity of 158 changed functions
passed 5/2/2, and Ruff passed. Installation and the actual full survey are separate.

The final previous CI matrix e909 ended with a failure of one test on five
Windows Python versions: the test looked for a POSIX separator in a Windows path. Now it
compares the full expected path through pathlib; the requirement to launch
compile_memory directly is kept. This is not evidence of an error in the working Windows launch.

The live compile PID 1376057 kept publishing parts for almost three hours.
Two traces showed the receipt directory being fully read and parsed again inside
publication and the status mirror after each part. This is confirmed repeated
work, but its share of the total time is not yet measured. The process was not killed, and the authority
checks were not bypassed. Optimization and full acceptance remain open;
raising the doctor budget does not fix this cause. This narrow set of changes does not prove
full compliance of all old limits with law 9.

### Repeated receipt parsing, October 17

A 30-second profile of the running compiler gave 2328 stack samples, of which 1365
(58.6%) were inside loading the receipt directory. This is a measurement of that interval, not
a share of the whole pass. A regression on two selectors reproduced repeated full
validation of an unchanged document. Now one compile-call owns the pure-parse
context. For each path only the last SHA256 of the bytes read
and the canonically encoded source descriptor are kept. A repeated traversal again
reads all files and checks the current schema and name; a change of bytes starts
a full check. Each consumer gets a separate object. The checks of the
SQL transaction, the external source, the receipt on a positive match and the
deadline stay live. The context is released in finally before index building;
outside a compile-call there is no reuse. There is no new numeric limit:
memory grows with the number of paths of the already existing directory and does not keep their versions.

On one frozen list of 8036 real paths, two original traversals took
13.790 and 13.086 s, and two candidate traversals 13.678 and 1.690 s. In all cases one SHA256
of the directory and 8024 source keys. Peak RSS of the separate processes was 54204 and 59196 KiB;
the additional memory is about 4.9 MiB. The measurement covers reading and comparing the directory,
not the whole compile and not its LLM calls. A shared machine and the order of runs limit
the precision of the comparison. The first measurement script failed because of a tuple instead of a JSON
list; only the script was fixed, not the product. The experiment did not change the sources.

149 related checks passed; in addition 51 checks of native sources and
historical authority passed. The sets overlap. The regressions keep
re-reading bytes, catch a change with a preserved mtime, a schema change,
mutation of the returned object and an authority refusal after a successful read.
174 changed functions pass the real 5/2/2 measurements. The Python 3.10 grammar and
Ruff passed; Python 3.10 support in the product does not mean upstream security support.

Sources checked on 2026-10-17: [Python contextvars](https://docs.python.org/3.10/library/contextvars.html),
[Git racy-file identity](https://git-scm.com/docs/racy-git),
[SQLite isolation](https://www.sqlite.org/isolation.html). Because of these constraints,
an mtime-based cache and keeping the authority verdict were rejected; only
isolated content parsing was chosen. The alternative of a persistent index would require
new coordination and a migration, which are not needed to remove the repeated validation.

### Why old LSP owners are kept, October 17

The full check of the installed c4a8 completed all 22 sections in 409.7 s, but
kept the LSP error. All 20 historical records are readable and pass the format
check; they contain no launch identity of the owner. A PID without this identity
does not prove the death of the stored process, including in another PID
namespace. Diagnostics now name the missing identities and the reason cleanup
is forbidden. The error codes, read_error and the proven-death criteria are not changed.
Neither the record nor a new observation fills in the historical identity retroactively.

The regression first reproduced the insufficient diagnostics. Then 205 checks of
doctor and process_liveness passed, and 3 platform checks were skipped. Reading
the real 20 owners showed 20 missing identities and 0 proven
deaths; the SHA256 of all record files before and after match. This fixes the
explanation of the block; it does not complete the recovery of the historical owners.
There are no new limits or dependencies; keeping these records is necessary
until a verifiable ground for their safe completion appears.

Sources checked on 2026-10-17:
[Linux PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html),
[Python os.kill](https://docs.python.org/3/library/os.html#os.kill),
[Windows GetProcessTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes).
Checking only the local PID and inventing the missing identity were rejected:
they give no proof of the historical owner's state. The chosen option is an exact
explanation of the available data while keeping the current deletion ban.

### Wait deadline for a project write, October 17

CI 39055059082, Linux Python 3.14, found a failure of two concurrent project
writes while waiting for the shared writer. The trace goes through checkpoint,
prepare and recover. The passed writer_wait_seconds reached apply but was lost
in prepare/recover; after adoption it was also lost when borrowing the gate of
the existing owner. The standard deadline was used instead of the explicitly passed one.

The regression creates a real competing writer and releases it after
the first failed acquisition attempt. The standard deadline in the test is set to 0,
so that the loss of the explicit budget shows without waiting on wall-clock time.
On the original code a TimeoutError is reproduced; the original check of the single
event record is kept. A separate adopted scenario checks passing the budget
during a real checkpoint while keeping the previous canonical owner.

Now prepare accepts the existing optional writer_wait_seconds and
passes it to recover; ProjectStore passes it at both boundaries.
Numeric deadlines, retries, acquisition order and lock lifetimes are not changed.
There is no new dependency. The option of holding the legacy gate through prepare and apply
was rejected: the lease-renewal check revealed a change in the ownership duration.
Excluding TimeoutError from the test and arbitrarily raising deadlines were also
rejected: they do not remove the parameter loss.

Research on 2026-10-17:
[SQLite rollback locking](https://www.sqlite.org/lockingv3.html),
[Python threading context managers](https://docs.python.org/3.14/library/threading.html),
[PostgreSQL lock ordering](https://www.postgresql.org/docs/current/explicit-locking.html).
The principles of explicit ownership, symmetric release and preserved
acquisition order were used; PostgreSQL is not added to the dependencies. The change is compatible
with Python 3.10 and keeps the previous default for the other callers.

Checks of the final version: 198 passed/5 platform skips for project and
transactions; 226 passed for recovery, automatic writers and checkpoint
rollback. On Python 3.14.7 with dependencies from the same uv.lock, 108 project
checks passed, including the original parallel CI scenario. These are related
sets, not a claim that the full matrix is complete. 189 changed functions
pass the real Lizard/AST 5/2/2 limits; the Python 3.10 grammar and Ruff pass.
uv pip check confirmed the compatibility of 97 installed packages.

### Capacity is measured by the collector of its own corpus, October 17

Settings diagnostics summed all Markdown from notes/projects/daily for
corpus and extraction. That is not the indexer's scope: ordinary daily logs are not selected,
the project journal is excluded, and verified linked session records are included.
On the live store this estimate diverged from a direct reading of the corpus.
The extraction threshold applies to the input of a specific call: knowledge extraction
gets the selected memory sources without projects, and project extraction gets one
source. The total number of Markdown files is neither of these measurements.

Now CorpusSnapshot carries optional observations of the completed collection:
the number of directory entries scanned, sources found and bytes read
before semantic filtering. They are not part of the persisted manifest/hash and
do not become evidence of authority. A snapshot created by hand,
narrowed for search or received from an old live consumer does not get
invented zeros. Doctor takes these observations from the full generation check that already
runs; there is no repeated traversal. The number of knowledge extraction sources
is taken from the same selection function that its adapter calls.

Settings no longer traverses projects/daily for this false estimate and explicitly
writes that corpus capacity is not measured here. The full check publishes separately
entries/sources/bytes, the knowledge extraction size and the maximum size of
one project extraction, together with their actual limits.
The previous 80% warning threshold is kept; nearness to the limit does not permit
an automatic rebuild of a healthy index. Exceeding the entry budget now
passes the existing CorpusCapacityExceeded with observed_minimum: a capacity
refusal does not mean corruption of the unchanged generation. Other read
and check errors still stay errors; freshness on refusal is unknown.

The shared hint settings.raise_hint keeps the names of the TOML/env controls, but
requires measuring resources before a change, rather than automatically raising the number.
Limit values were not changed. There is no new limit, dependency, store or
runtime layout change. A separate count with new copies of the filters was rejected:
it would diverge from the collector again and double the reading. Substituting a missing
measurement with a number from the old generation was also rejected.

Research on 2026-10-17:
[Prometheus instrumentation](https://prometheus.io/docs/practices/instrumentation/),
[Google SRE monitoring](https://sre.google/workbook/monitoring/),
[OpenTelemetry metrics data model](https://opentelemetry.io/docs/specs/otel/metrics/data-model/).
Counting at the place where the operation runs, with an explicit observation scope, was chosen; these
monitoring systems are not added to the dependencies. Their general recommendations do not
replace the local store's contract and do not justify new numeric limits.

Four new regressions failed on the previous code; the hint with an unconditional
limit increase was reproduced separately. The checks keep the confirmation of complete
breadcrumb parts, the exclusion of ordinary dumps and journals, counting superseded bytes
before filtering, one collection call and the absence of a fake measurement.
Related sets 226 passed/3 skipped and 213 passed/3 skipped; snapshot consumers
443 passed/1 skipped; the final corpus/settings set 164 passed/3 skipped.
The sets overlap; this is not a sum of unique tests of the full matrix.
On the live tree settings took 0.317 s; the generation check confirmed a minimum of 204011
entries against 204010, keeping freshness=unknown and repairable=false. The measurement of
29.580 s covers both calls and does not prove the speed of a full compile.

## Adoption reads wait for the existing store lock budget (2026-10-17)

The active-database snapshot shared by writer admission and full adoption
certification omitted `busy_ms`; the common read-only opener defaults to zero.
Operational doctor calls admission directly, and full capture inspection also
reaches this opener. An ordinary exclusive writer could therefore produce
`reliability_v3_record_invalid: OperationalError: database is locked` immediately.
Six regression cases failed before the fix: both stores, admission and full
certification under a real separate-process exclusive writer, and observation of
the connection wait with and without a caller deadline. This establishes a defect
matching the live symptom; it does not prove every historical lock failure had
this cause.

Use the existing queue (5,000 ms) and coordinator (10,000 ms) lock budgets at this
shared opener. The existing opener clamps them to the caller's remaining time;
identity, schema, metadata, physical integrity and foreign-key checks remain.
No new number, retry loop, dependency, persistent setting, or schema is added.
These inherited store budgets are not newly qualified resource limits: revisit
under measured contention and latency, especially if writes outlast the store
budget. A lock held longer still fails; corruption still fails. There is no
claim that complete retained-history reconciliation can now succeed while a
compiler continues changing the filesystem.

Sources checked on 2026-10-17:
- [SQLite busy timeout](https://sqlite.org/c3ref/busy_timeout.html): the native
  handler waits for contention within its budget, then returns busy.
- [Python sqlite3](https://docs.python.org/3.13/library/sqlite3.html): connection
  timeout is the lock wait before OperationalError; this code uses the existing
  Python 3.10-compatible API, installed Python 3.12.3.
- [PostgreSQL explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html):
  conflicting transactions require coordination and consistent lock order.
  This is an independent concurrency reference, not a PostgreSQL runtime proposal.

Alternatives: suppressing lock errors would invent successful validation;
repeated whole-record validation would repeat work and add a second retry policy;
holding a global writer gate through health would stall capture. Native waiting
at the existing connection boundary is smaller and keeps the read transaction's
lock order. The separate-process fixture holds a controlled 0.2-second write;
that duration is test stimulus, not a product delay or capacity limit.

The 7f142b56 CI exposed a separate fixture error on Windows: write_text produced
CRLF bytes (34), while the new capacity test expected the pre-write LF string
(30). Explicit LF/CRLF byte fixtures reproduced the CRLF failure on Linux; the
assertion now measures the supplied bytes, retaining both variants on every OS.
This changes no production counting rule and excludes no platform.

## Fence time is sampled after write admission (2026-10-17)

Investigation of the new live `owner_fence_lost` and Windows CI failures found a
separate reproducible authority bug. Intent and task fences sampled the current
time before opening SQLite and entering `BEGIN IMMEDIATE`. Waiting could move
the real clock beyond expiry while the subsequent SQL predicate still used the
old time. This could renew an expired intent, admit an expired queue owner,
project a binding under an expired intent, or create an intent whose useful
lifetime had already been spent waiting.

Five regressions reproduce those cases by advancing a controlled clock when a
real SQLite write transaction is admitted. They failed against the old code;
fixture setup failures (an unsealed binding and a wrong queue path attribute)
were corrected before counting the five defect reproductions. Cleanup now
releases the test's intent before its owner, preserving foreign-key checks.
The change moves time sampling into the existing transaction in five methods:
intent acquisition/renewal/binding projection and task acquisition/renewal.
No TTL, retry count, timeout, schema, dependency or public interface changes.
The two owner-projection callers and all live-intent guard callers were checked.
Canonical owner heartbeat already samples its clock inside the write transaction.

Sources checked 2026-10-17:
- [SQLite transactions](https://www.sqlite.org/lang_transaction.html), updated
  2026-02-18: write admission is serialized; BEGIN IMMEDIATE can encounter a
  competing writer. A timestamp preceding admission cannot certify its end.
- [Kubernetes Lease API](https://kubernetes.io/docs/reference/kubernetes-api/coordination/lease-v1/):
  renewal and duration determine a lease's validity. This is an independent
  lease contract reference, not a new Kubernetes dependency.
- [Kleppmann on fencing](https://martin.kleppmann.com/2016/02/08/how-to-do-distributed-locking.html):
  a paused client can resume after expiry; storage must fence stale authority.
  This is foundational 2016 analysis, not a claim of a newly published design.

Alternatives rejected: extending TTL cannot repair a stale-time check; treating
an expired fence as valid weakens authority; another lock or renewal abstraction
adds complexity without closing the ordering error. Existing rollback-journal,
synchronous=FULL SQLite and Python 3.10-compatible APIs are retained. Sampling
at admission does not promise that an arbitrarily stalled commit will finish
before expiry, nor solve cross-database coordination beyond existing contracts.

This is not proven to explain the 19:58:48 live capture loss. Nor does it explain
the Windows trim failure or b0e769ad CI's Windows processor cleanup failure.
Those remain open; no failed test is disabled or reclassified as successful.

## Windows process snapshots preserve unknown state and handle width (2026-10-17)

The queue's Toolhelp enumeration treated every false return from Process32First
or Process32Next as successful exhaustion. An API failure before the first row
became an empty tree; a failure later became a supposedly complete partial tree.
This contradicted the caller's existing rule that an unknown descendant snapshot
cannot prove process cleanup. CreateToolhelp32Snapshot and the handle consumers
also lacked ctypes prototypes, leaving the default signed C-int return type.
A real ctypes callback returning a pointer-width value reproduced truncation.
Six enumeration-error cases and the width case failed before the change.

Bind HANDLE/BOOL/DWORD and the exact PROCESSENTRY32 pointer types explicitly.
Accept enumeration completion only with the documented ERROR_NO_MORE_FILES (18);
otherwise return the existing unknown value, None. The snapshot handle is closed
through the existing finally path even on enumeration failure. No new retry,
timeout, limit, dependency, runtime path or public interface is introduced.
The existing cleanup refusal remains; its message now records snapshot_known,
tree_verified and termination_verified. Two diagnostic assertions failed before
this message change. Processor-error assertions retain their expected codes and
include the exception detail when they fail, so a future Windows failure can be
investigated rather than guessed at.

Sources inspected 2026-10-17:
- Microsoft [Process32First](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-process32first)
  and [Process32Next](https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-process32next)
  specify HANDLE, the entry pointer, and the normal end code.
- [Python ctypes](https://docs.python.org/3.13/library/ctypes.html) documents the
  default C-int return and explicit restype/argtypes. These APIs also exist in
  the supported Python 3.10 baseline; local runtime is Python 3.12.3.
- [Go's Windows syscall implementation](https://go.dev/src/syscall/syscall_windows.go)
  uses a pointer-width Handle, sets the entry size, propagates enumeration errors
  and closes the acquired snapshot. It is an independent implementation reference,
  not a proposal to add Go or its syscall package.

Alternatives: treating all failures as exhaustion silently weakens safety;
repeating a failed enumeration without a defined retry contract adds policy;
replacing Toolhelp with a subprocess or new library adds dependencies without
fixing the current API boundary. Explicit types and terminal-code validation are
smaller. The LSP thread-snapshot code already binds handle types; it enumerates
threads for a different ownership contract and is not replaced here.

The Linux-hosted API simulations and ctypes callback do not qualify native
Windows behavior. A new native-only check requires that a real complete snapshot
contains the current process, alongside the existing spawned-grandchild tests.
The earlier process_cleanup_failed CI incident is not declared explained by
these findings. Windows native CI, the trim incident, the live capture loss,
full retained-history reconciliation and the complete nightly cycle remain open.

Local validation: 77 related queue/process tests passed in 96.04 seconds; the
native Windows check was skipped on Linux. Initial diagnostic assertions confused
str(exception), which includes the stable code, with its detail field; the final
tests check code and detail separately. Lizard/AST checked 245 changed functions
without violations; Ruff and Python 3.10 grammar passed. All 97 installed packages
are compatible; the lockfile and dependency set are unchanged.

## Capture diagnostics check actual write progress (2026-10-17)

The trail writer called os.write and discarded the returned byte count on both
platform paths. A short write therefore returned True with a truncated JSON line;
a zero-byte write returned True with no new record. Four regressions reproduced
this before the fix, with and without the Windows byte-lock adapter. The short
writer performs actual one-byte filesystem writes, including splitting UTF-8,
and asserts that the sidecar owner is held throughout.

Reuse reliable_memory._write_all, already loaded through memory_state: it advances
only by the actual byte count and rejects no progress. The complete append stays
inside the existing sidecar lock and, on Windows, the existing byte lock. No new
retry budget, sleep, size cap, package, runtime path or configuration is introduced.
The normal full write still uses one syscall. Tests check exact existing/new JSON
records, false on no progress, and lock release. This is an internal reuse of the
existing durability primitive, not a new public API. Python 3.10 remains supported.

Primary sources checked on 2026-10-17:
- [Python os.write](https://docs.python.org/3.12/library/os.html#os.write) returns
  the actual count; interrupted calls are already retried by Python when the signal
  handler does not raise. No extra EINTR retry loop is added.
- [Linux write(2)](https://man7.org/linux/man-pages/man2/write.2.html) documents
  short writes and completing the remaining bytes. O_APPEND does not make a whole
  multi-call record atomic; the existing cooperating-writer sidecar provides that
  exclusion here.
- [Microsoft CRT _write](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/write?view=msvc-170)
  documents byte-count results, errors, append behavior and text translation. The
  existing binary open flag and Windows byte lock are retained. Linux fault
  injection on the adapter does not establish native Windows qualification.
The Open Group write page could not be fetched and is not counted as a source.

Alternatives: checking the count and abandoning a recoverable short write loses
available progress; a local duplicate loop is unnecessary; buffered file objects
would change descriptor/locking behavior without a benefit. Related integration
adapter and LSP writers already account for actual progress. Their differing
text/evidence contracts are unchanged. The reused helper has no dependency on
capture_diagnostics, so this adds no circular import.

Limits: a permanent OS error after a partial write may still leave an incomplete
line; this change does not promise atomic rollback or power-loss durability for
the best-effort log. The existing caller reports failure and retains its separate
counter path. This is not evidence explaining the Windows trim CI failure, nor
the live owner_fence_lost capture event. The existing trail-lock wait policy and
other historical limits have not been qualified by this change.

Validation: 106 related diagnostic, lock, append and reliable-memory tests passed
in 11.15 seconds; three platform-specific tests were skipped. Actual Lizard/AST
analysis passed for 250 changed functions; Ruff and Python 3.10 grammar passed.
All 97 installed packages remain compatible. CI for the preceding 7fc79008 commit
again failed the Windows 3.13 trim/append test (run 39071113802, job 117509064677).
That failure is retained, not disabled or attributed to short writes without
proof. The live compiler was still running after nearly eight hours; it was not
stopped for this change.

## Host capture latency: keep the failed budget and expose phase evidence (2026-10-18)

CI f233212d / 39074021574 failed Windows 3.13 shard 2: the ready-writer host
scenario accepted its durable capture but ingestion took 6.8594699 seconds,
exceeding HOST_HOOK_TIMEOUT_SECONDS=5. The log has no phase timings, so it does
not establish whether publication, registration, filesystem operations or prompt
follow-up consumed that time. The old trim failure did not recur in this run;
that is not proof that its cause is resolved.

A Linux cProfile baseline passed both ready-writer cases; ingestion averaged
0.132 seconds (0.127 in publication). This is a different platform and cannot
explain the Windows incident. The profile was a separate diagnostic invocation,
not a replacement for the unprofiled timing test.

One independently reproduced fixture defect: the wrapper calls a test function
from a marked module but does not inherit that module's shipped_append_budgets
mark. The conftest fixture therefore substitutes LONG_TIMEOUT append budgets
inside a scenario that purports to use shipped values. Two assertions checking
the collected item's marker failed before adding the explicit module mark. Keep
those assertions. This correction is not claimed to explain the 6.86-second
incident; the exercised breadcrumb path need not spend the replaced budgets.

The original scenario now records lightweight nested elapsed times around queue
and coordinator admission, whole publication, bundle storage, registration,
follow-up and worker wake. Wrappers forward arguments/results/exceptions and are
removed by monkeypatch.context. The five-second total still includes wrapper
cost, and no overhead is subtracted. On failure the assertion includes phase
counts/times and the existing registration/follow-up errors. Nested durations
must not be added as independent phases. Fixture readiness, durable capture,
pending-intent, eventual worker delivery and project-journal assertions remain.
No production behavior, deadline, dependency, path or environment contract changes.

Sources inspected 2026-10-18:
- [pytest marks](https://docs.pytest.org/en/stable/how-to/mark.html): marks attach
  to collected tests/classes/modules. An ordinary Python call does not collect
  another test item or transfer its module metadata. Installed pytest is 9.0.3.
- [Python clocks](https://docs.python.org/3/library/time.html#time.perf_counter):
  elapsed-time clocks include waiting; the implementation uses APIs present in
  Python 3.10 and locally runs on 3.12.3, with no new 3.15-only API.
- [Microsoft high-resolution timing](https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps):
  QPC is intended for interval measurements. The test uses Python's clock rather
  than another native binding or wall-clock timestamps.

Alternatives rejected: raising the host timeout or removing its assertion hides
the failed contract; declaring a Windows fix from a Linux pass lacks evidence;
permanent full profiling adds avoidable measurement overhead. The phase report
provides the evidence needed for a later reproduced fix. Native Windows latency,
trim failure, live capture ownership loss, full nightly/history qualification and
law-9 review remain open.

Validation: 12 related tests passed in 14.58 seconds. Lizard/AST checked 255
changed functions without violations; Ruff and Python 3.10 grammar passed. All
97 installed packages are compatible. Native Windows qualification is pending.

## Retry only confirmed SQLite contention (2026-10-18)

Tracing capture ownership renewal found a shared classification defect:
lease_renewal.busy_database accepted every OperationalError, while the maintenance
renewer accepted every non-RuntimeError. A failed SELECT against an absent table
and a write on a query-only database were retried as temporary contention; a later
successful beat could hide the original cause. The queue's separate classifier
also treated any message containing "locked" or "busy" as contention, including
SQLite's real "no such table: locked" error. Five lease/queue regressions and three
maintenance regressions failed before their respective fixes.

Use lease_renewal.busy_database for lease renewal, queue claim and maintenance.
The numeric SQLite primary result (low byte) is authoritative when available:
BUSY=5 and LOCKED=6 are contention. Other codes stay failures even if their text
contains a lock-related word. Python 3.10 lacks sqlite_errorcode, so the fallback
accepts only SQLite's database/table/schema locked messages, including the object
suffix after a colon. It never searches arbitrary error text for a substring.
The old permissive queue and maintenance implementations are replaced by calls
to the shared function. No extra module, package, retry, sleep, deadline, schema,
path, provider call or environment setting is introduced.

The existing maintenance fixture used the invented message "locked"; it now uses
SQLite's real "database is locked". Its expected retry/fence-loss behavior is
unchanged. Positive tests acquire an actual conflicting SQLite write lock, test
both its native exception and a code-less Python-3.10-style copy, and verify
extended BUSY/LOCKED codes. Negative tests retain the original exception and
verify that a permanent renewal error ends after the first attempt.

Sources checked on 2026-10-18:
- [SQLite result codes](https://www.sqlite.org/rescode.html) defines primary and
  extended results, the low-byte encoding and the difference between contention,
  readonly, SQL and I/O errors. The integers are an external protocol, not limits.
- [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html) documents
  OperationalError as a broad category; it does not distinguish contention by
  exception type alone. Local runtime is Python 3.12.3; the fallback retains the
  supported 3.10 baseline without assuming newer symbolic constants exist.
- [Microsoft retry guidance](https://learn.microsoft.com/en-us/azure/architecture/patterns/retry)
  distinguishes transient conditions from failures unlikely to succeed on retry.
  This is classification guidance, not a dependency on Azure or a new retry policy.

Alternatives rejected: repeating every OperationalError hides permanent failures;
message substring matching misclassifies object names; copying the classifier
into each actor perpetuates drift. Existing consumers include capture publication,
capture workers, source/task fences, Markdown writer ownership, maintenance and
private-vault backup. Unknown failures stop renewal and retain their cause; no
failure is converted to successful ownership. The former transient wait policy,
TTL and attempt budgets are unchanged and are not newly qualified by this fix.

This establishes the classification defect, not the cause of the historical
owner_fence_lost event or Windows host-latency/trim failures. Those remain open.

Validation: 232 related lease, queue, capture and doctor tests passed in 54.66
seconds; four platform-specific tests were skipped. Lizard/AST checked 266 changed
functions; Ruff and Python 3.10 grammar passed. All 97 installed packages remain
compatible. A normal uv cache prune refused an in-use cache; its waiting process
was stopped without --force, and no cache deletion was performed.

## 2026-10-18: publication retry time starts with the canonical owner

The fresh bd839c84 built-in graph identifies the publication keepalive call;
source inspection supplies its unresolved registry and coordinator edges.
Capture owners last 30 seconds. Acquiring the intent fence can wait for SQLite,
and that fence's expiry is capped at the owner's expiry. The keepalive nevertheless
stamped `held_since` after this second acquisition, adding the acquisition wait
back into its retry allowance. Two controlled-clock regressions fail on the old
code: after 9 or 12 seconds acquiring the fence, it makes four renewal attempts
where only the initial attempt fits the existing retry policy. These tests advance
time without sleeps; a no-delay control still permits the original retries.

The caller now records the monotonic reading immediately after acquiring the owner
and passes it as a required keyword to the keepalive. Its constructor no longer
invents a new acquisition time. This preserves the common renewal algorithm,
shutdown joining and final authoritative database checks. It does not introduce
an expiry, retry count, timing override, package or runtime contract. The existing
30-second lifetime and two-database attempt allowance are unchanged. The residual
time between acquisition returning and the clock read is unchanged from the
existing acquisition-clock contract; this correction removes the subsequent
blocking call from that gap, not every possible process scheduling delay.

Related-path inspection: worker capture uses a 120-second canonical owner and
acquires the 30-second intent fence last; its situation differs from the publisher's
30-second owner capped fence. Worker queue/task-fence age and general acquisition
clock accuracy are not declared fully qualified by this change. Nor does this
reproduction establish the cause of historical `owner_fence_lost` records.

Primary sources checked on 2026-10-18:

- [Python monotonic clock](https://docs.python.org/3/library/time.html#time.monotonic):
  elapsed-time differences survive wall-clock adjustments; available at Python 3.10 floor.
- [AWS leader election](https://aws.amazon.com/builders-library/leader-election-in-distributed-systems/):
  lease validity must account for elapsed work and pauses before protected actions.
- [Kubernetes leases](https://kubernetes.io/docs/concepts/architecture/leases/):
  renewal time and lease duration govern ownership, rather than when a helper starts.

Alternatives: increasing the TTL hides elapsed acquisition time; retrying all errors
was already removed; adding a separate renewal thread during fence acquisition adds
lifecycle complexity. Passing the existing acquisition reading is the smallest change
that preserves the current ownership protocol. No claim of universally optimal lease
design or complete law-9 qualification follows from these sources.

Validation: 67 related tests pass in 56.81 seconds, including real publication
beyond its original lease, expired-authority refusal, worker renewal, shutdown
joining and queue/breadcrumb integration. Lizard plus AST checks cover 274 changed
functions across the ongoing refactor with no CCN/if/nesting violations; Ruff and
Python 3.10 parsing pass. All 97 installed packages remain compatible. Evidence:
`logs/refactor-20261018-capture-clock-red.txt` and
`logs/refactor-20261018-capture-clock-green.txt` (private installed vault logs).

## 2026-10-18: diagnostic publication no longer holds shared state

The current Windows CI (39087779301) fails the concurrent trail burst on Python
3.11: some expected reasons are missing. Its second Windows failure has no
available step log; its cause is unknown. Separately, the installed trail's latest
loss records include SQLite disk-full errors during adoption and two failed
locking probes at 02:19. These are not evidence that every recurrence shares the
previous `owner_fence_lost` cause. Cleanup recovered space; recovery of each
retained capture remains to be verified.

Fresh built-in graph and source inspection expose a common diagnostic path used
by hooks, the queue and other failure reporters: `record_capture_failure` acquired
shared `state.json` before writing and trimming the separately locked trail.
That couples diagnostic I/O to every unrelated state writer. Append and trim
then acquired the sidecar separately. A trim exception after a completed append
also made the entire operation report false, so the fallback duplicated the line.
Three new tests reproduce these defects before changes; a fourth control confirms
that a failed state commit must not replay a completed append.

The pipeline now publishes to the trail first, releases its ownership, and only
then updates counters. Its state callback only mutates the supplied dictionary.
Append and trim share one sidecar acquisition, including the existing verified
same-thread ownership path. A trim failure leaves the append result intact.
The existing fallback remains for actual append refusal, not maintenance failure.
No counter or historical failure is cleared. The old callback that performed
file I/O under shared state and its mutable success list are removed.

This is an internal lock-scope refactor: files, formats, runtime roots, providers,
public interfaces and dependencies are unchanged. Existing byte-lock cooperation
is retained for current consumers. No timeout/retry/count/size limit is increased
or added; the historical limits are not declared justified by this change.
Diagnostics remain best effort under real exhaustion or I/O errors. Independent
trail and counter commits were already non-atomic; changing their order does not
promise atomicity or guarantee delivery after an exhausted trail lock wait.
The Windows burst failure is not declared fixed without matching platform evidence.

Primary sources reviewed 2026-10-18:

- [Python Logging Cookbook](https://docs.python.org/3/howto/logging-cookbook.html#logging-to-a-single-file-from-multiple-processes):
  multi-process file logging requires explicit coordination; switching to a plain
  standard FileHandler does not solve this product's cross-process writes.
- [Microsoft critical sections](https://learn.microsoft.com/en-us/windows/win32/sync/using-critical-section-objects):
  contenders have no guaranteed acquisition order; polling is not a fairness guarantee.
- [SQLite locking](https://www.sqlite.org/lockingv3.html): exclusive work prevents
  competing access, supporting the requirement to minimize unrelated work under locks.

Alternatives: a logging listener/daemon changes the existing no-daemon contract;
a new durable spool changes paths and recovery semantics; increasing lock waits
hides coupling and spends host time. The chosen refactor keeps the existing protocol
and removes avoidable nested work. A broader redesign must first establish durable
acceptance, recovery and resource evidence; it is not justified solely by a passing
unit test or by clearing health counters.

Validation: 55 trail/capture/ownership checks pass (38.12 s), followed by 224
hook/worker/host-budget/doctor consumer checks (52.63 s), with three platform
skips. The original burst and trim tests retain their assertions. Lizard/AST
checks pass for 285 changed functions across the refactor; Ruff and Python 3.10
parsing pass. All 97 installed packages are compatible. Evidence lives in
`logs/refactor-20261018-diagnostic-pipeline-{red,green,consumers}.txt`.

## 2026-10-18: qualify the installed Codex dependency for compile planning

The 03:05–05:32 nightly compile emitted 459 failed-batch messages, 134 zero-page
publications and eight one-page publications before its launcher timed out at
8839 seconds. Failures include rejected critic plans followed by draft input
budget refusals. Those counts do not prove that all failures have one cause.

The installed dependency is Codex CLI 0.162.1. Both discovery and advertised-window
interpretation accepted only 0.160.0, so `_planned_candidate` returned a descriptor
with no planning basis. The compiler's previously implemented repair window
therefore could not use the model's native advertised capacity. A direct read-only
native probe on 0.162.1 successfully resolved the configured gpt-6-luna model and
OpenAI provider, verified two configuration files, and cleaned up its ephemeral
bootstrap; the old version check still suppressed the window. No inference turn
or user source was sent by that probe.

Two regressions reproduce the excluded installed version: complete resolver
binding and matching catalog capacity. The qualification policy is now shared by
discovery and catalog interpretation in one immutable set, covering tested 0.160.0
and 0.162.1. Existing unknown-version fallback and all identity, configuration,
cleanup, model/provider matching and malformed-reply refusals remain unchanged.
The installed 0.162.1 resolver now returns an advertised planning window of
258400 (catalog context_window 272000 × effective_context_window_percent 95 / 100).
This is native planning evidence, not an exact tokenizer count, account entitlement,
wire admission guarantee or proof that the full nightly cycle now succeeds.

No model, reasoning setting, provider choice, source context, review requirement,
retry count, timeout, output reserve or safety margin changes. No runtime path,
environment contract or package is added. The two duplicated single-version
conditions are replaced by the shared qualification set. 0.160.0 remains supported
for existing installations; it is a compatibility entry, not a parallel engine.
Unknown versions require requalification rather than an assumed capacity increase.
This restriction is tied to experimentally checked bootstrap/catalog semantics;
revisit it whenever the selected CLI changes. Complete protocol-based compatibility
without release qualification would require separate evidence about semantic drift.

Primary sources checked on 2026-10-18:

- [Official Codex App Server](https://learn.chatgpt.com/docs/app-server) describes
  initialization and version-specific generated schemas, and ephemeral thread use.
- [Official CLI reference](https://learn.chatgpt.com/docs/cli/reference) describes
  `debug models` and the optional bundled catalog. The qualification uses the native
  selected executable and the existing verified invocation configuration.
- [JSON-RPC specification](https://www.jsonrpc.org/specification) distinguishes
  method-not-found from invalid parameters and malformed responses; optional
  capability absence must not hide genuine protocol failure.
- [Python subprocess documentation](https://docs.python.org/3/library/subprocess.html)
  documents process completion and cleanup; the existing owned process-tree and
  joined-reader tests remain part of this dependency qualification.

Alternatives: copying the advertised number into TOML would lose model/config
binding; accepting every version would claim untested semantics; reverting the
user's CLI would affect other consumers. Qualifying the actual installed dependency
preserves the existing contract with the smallest change. Native evidence:
`logs/refactor-20261018-codex-162-native-probe.json` and
`logs/refactor-20261018-codex-162-native-qualified.json`. Regression evidence:
`logs/refactor-20261018-codex-162-red.txt` (two failures before the change) and
`logs/refactor-20261018-codex-162-green.txt` (81 related checks pass).

Extended validation: 310 related consumer tests pass, with 20 platform/optional
skips (25.09 s); this includes the earlier 81 checks rather than adding to them.
Ruff, Python 3.10 parsing and complexity checks pass for all 291 changed functions
in this refactoring series. All 97 installed packages have compatible dependencies.
Full nightly completion and Windows CI remain separate, unresolved qualifications.

## 2026-10-18: provider upgrades use observed capabilities

The owner explicitly requires upgrades of Codex, Claude and OpenCode to remain
supported. This supersedes the exact-release qualification policy above. A release
allowlist itself reproduces the original refusal on the next otherwise compatible
Codex upgrade. A second upgrade defect exists in Claude: cached `--help` results
were keyed only by pathname, so replacement at that pathname retained old flags
inside a long-lived process. Both defects were reproduced before changing code.

Codex now attempts the existing native handshake irrespective of release text.
The reported version remains provenance, not a permission to parse the protocol.
Capacity still requires matching model/provider, validated catalog numbers,
verified configuration layers, stable executable/config/environment, successful
owned cleanup and the existing deadline. Missing optional methods retain the
existing implicit execution fallback; malformed replies, invalid parameters,
identity drift and cleanup failure still refuse rather than invent capacity.
The exact-version set and obsolete catalog version arguments are removed.

Claude still discovers flags through `--help`, without any release allowlist.
The cache now includes resolved target, device/inode, size, modification and change
timestamps. An unchanged executable reuses its help; a replacement refreshes it.
A change during discovery refuses that result and does not cache it. Missing files
return unknown capabilities. This is ordinary upgrade detection, not protection
against a hostile actor able to rewrite a process's files and metadata. Cache size
remains one because only the current selected CLI's answer needs retention.

OpenCode already checks the authenticated live health response, not its version,
and has no release-keyed capability cache. The existing HTTP interaction test now
covers unfamiliar version strings and additive health fields without changing its
assertions about authentication, prompt framing, response and session cleanup.
No extra transport, daemon, path, package, environment variable, model selection,
retry, token ceiling or persistent compatibility database is introduced.

Sources checked 2026-10-18 (three independent maintainers):
- [OpenAI Docs: App Server](https://learn.chatgpt.com/docs/app-server): native
  initialize/config/thread operations and version-specific schemas. The interface
  is experimental; future incompatible semantic changes cannot be promised away.
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference): print
  mode, output formatting and isolation options consumed by the existing adapter.
- [OpenCode Server](https://docs.opencode.ai/docs/server/): authenticated health,
  session operations and published OpenAPI contract.

Alternatives rejected: adding each release forever repeats the defect; blindly
accepting successful process exit ignores required capabilities; disabling checks
would expose private inputs or invent capacity; a new persistent compatibility
layer would duplicate the existing per-call validation. Contract-based checks
support compatible releases automatically while incompatible external changes
remain explicit failures. This does not certify future releases, repair every
nightly failure or prove every deferred capture was recovered.

Validation: the initial run reproduced seven failures (including repeated
parameterized catalog assertions) in the new upgrade scenarios. The final related
suite passes 486 checks, with 22 platform/optional skips (60.03 s). Complexity,
Python 3.10 syntax and Ruff pass; all 97 installed dependencies are compatible.
The candidate against installed Codex 0.162.1 resolves gpt-6-luna and its advertised
258400 planning window; installed Claude exposes 71 discovered flags. No inference
turn or private source was submitted. There is no configured authenticated OpenCode
server here, so its qualification is the local HTTP regression, not a live-provider
claim. Evidence: `logs/provider-upgrades-{red,green,consumers}.txt` and
`logs/provider-upgrades-native.json`.

## 2026-10-18: writer admission retains its error identity

Recent hooks repeatedly log `TimeoutError: timed out waiting for the global
Markdown writer gate`. The canonical gate converts `owner_busy` to an untyped
TimeoutError on deadline expiry; legacy admission and SQLite contention expiry do
the same. This loses the distinction used by capture diagnostics. Meanwhile compile
publication reconstructs the distinction by comparing the exception's full message,
so an unrelated timeout with identical text is retried. Three regressions reproduce
both real competing-writer paths and the incorrect message-based retry.

Use one `WriterAdmissionTimeout(TimeoutError)` at the three admission-expiry
sites. Existing callers catching TimeoutError remain compatible. Compile retries
only the explicit subtype under its original deadline/cancellation checks; capture
classification recognizes the same subtype. Cause chains, deadlines, delays,
leases, pending work and persisted history are unchanged. Generic timeouts remain
errors even if their text matches. No historical counters are rewritten. The old
compiler string comparison is removed. This repairs error identity, not the actual
resource contention or the full nighttime failure.

Sources checked 2026-10-18:
- [Python exceptions](https://docs.python.org/3/library/exceptions.html): subclass
  matching and explicit cause chaining separate failure meaning from presentation.
- [SQLite result codes](https://www.sqlite.org/rescode.html): BUSY/LOCKED are distinct
  from storage and corruption failures; preserve the original cause and existing
  typed/code-based admission retry policy.
- [Microsoft LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex):
  lock acquisition can wait or return an error, distinct from successful ownership.
  No platform locking policy is changed here.

Alternatives: matching more message strings repeats brittle inference; classifying
all timeouts as contention hides unrelated failures; increasing wait time would not
restore the lost identity. A subtype is the smallest backwards-compatible contract.
No new resource or numerical limit is introduced, and no source/queue records are
deleted. The legacy admission implementation remains because v2 readers and their
qualification tests still consume it; this change does not authorize its removal.

Validation: three failures reproduced before the fix; 422 related checks pass
(127.14 s), including real competing legacy/v3 writers, publication retry,
cancellation, pending checkpoints and capture diagnostics. Separate checks verify
that SQLite BUSY expiry retains its cause and an unrelated timeout is not classified
as contention. Ruff and Python 3.10/complexity checks pass for 316 changed functions
in the series; all 97 installed package dependencies are compatible.

The preceding provider-upgrade CI exposed a missing measured shard weight for
`test_provider_upgrade_contracts.py`. The documented shard-plan measurement now
adds that file; its three tests and six weight-integrity tests pass. Existing weights
are preserved. This CI preparation error is acknowledged, not evidence that the
previous full CI passed. Logs: `writer-admission-{red,green,consumers,shard-weight}.txt`.

## 2026-10-18: SQLite contention across supported Python versions

CI 39117071266 exposed use of `sqlite3.SQLITE_BUSY` in the new cause-chain test,
which is absent in Python 3.10. Reproduction on real CPython 3.10.20 also exposed
that the production `_sqlite_contention` had the same dependency when given a
numeric error code. Ordinary 3.10 exceptions without that attribute use the
existing message fallback; the numeric-code branch must still be portable.

The shared writer classifier now uses SQLite's documented primary codes 5 and 6,
masking extended codes with 0xff. These are external protocol constants, not new
limits. If a numeric code exists, it is authoritative: a FULL or CORRUPT error
cannot be reclassified by the words `busy` or `locked` in its message. Text fallback
remains only for drivers without a numeric code, preserving Python 3.10 support.
No wait, retry count, database format, dependency, runtime path or safety check is
relaxed. The test supplies the stable SQLite code rather than a newer Python name.

Sources checked 2026-10-18: [Python 3.10 sqlite3](https://docs.python.org/3.10/library/sqlite3.html),
[SQLite result codes](https://www.sqlite.org/rescode.html),
[DB-API exception contract](https://peps.python.org/pep-0249/), and
[Microsoft system error codes](https://learn.microsoft.com/en-us/windows/win32/debug/system-error-codes--0-499-).
Python and PEP describe one ecosystem, not two independent maintainers. SQLite
and Microsoft independently distinguish contention from storage failures. Windows
sharing classification was inspected and is unchanged. Alternatives rejected:
skipping the regression on 3.10, upgrading the project's minimum Python version,
or interpreting arbitrary error text despite an explicit non-contention code.

Runtime recovery observations: the supported adoption sweep reported zero skipped,
zero orphaned and zero half-published records. A fresh quarantine inspection found
one unresolved breadcrumb publication, versus 17 in the earlier snapshot. Its
source reconstructs to 560 bytes and its head matches the staged digest; its linked
queue task succeeded. No committed create witness exists in current transaction
history, so the quarantine remains retained, not manually marked resolved. These
observations do not prove full queue completion or explain the missing witness.
The macOS Pyright fixture CI independently failed with QualifiedIdentityError;
its 1203 tests passed and 101 skipped before that fixture failure. It is not fixed
by this SQLite change.

Validation: CPython 3.10.20 reproduced the missing-constant failure (1 failed,
9 passed), then seven failures with the numeric-code regression matrix. After the
fix, 436 related checks pass on that interpreter (141.11 s); 24 targeted checks
pass on installed CPython 3.12.3. Ruff and measured complexity pass for 318 changed
functions in the series. Dependency checks pass for the isolated 66-package 3.10
environment and installed 97-package 3.12 environment; the lock file is unchanged.
Evidence: `logs/step7-python310-{red,consumers}.txt`,
`logs/step7-sqlite-classification-red.txt`, `logs/step7-python312-sqlite.txt`.

## 2026-10-18: qualification failures retain the failed predicates

The macOS correctness fixture in CI 39117071266 emitted only
`QualifiedIdentityError`, hiding which of seven required identity checks failed.
The next run, 39118878569, passed both the Linux and macOS Pyright jobs without a
Pyright change. This is not evidence that the original intermittent failure was
fixed. Its precise cause remains unknown from the old log.

The confirmed diagnostic defect is reproduced by seven individual identity
mismatches and one CLI rendering check. Qualification now names the same seven
predicates in one mapping and attaches only the failed names to its existing
exception. The CLI preserves these names in its error JSON. Status, exit code,
package/version pins, Node check, degradation refusal and all gate requirements
are unchanged. There is no new retry, timeout or accepted identity. Arbitrary
identity values, paths and exception text are not printed; the finite set of
check names is output structure, not a new operating limit. The previous unnamed
boolean tuple is removed, not kept as a parallel decision path.

The active graph did not cover the benchmark directory, and live graph extraction
returned operation_timeout. Dependencies, consumers, CLI rendering and fixture
checks were therefore inspected directly in benchmark/run_code_navigation.py,
tests/test_code_navigation_benchmark.py, pyright_profile.py and the workflow.
The updated code index will include benchmark with scripts and tests.

Sources checked 2026-10-18 (three independent projects):
- [Python exceptions](https://docs.python.org/3/library/exceptions.html): carry
  diagnostic information through the existing exception boundary.
- [OpenTelemetry exception logs](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/):
  distinguish exception classification from descriptive diagnostic information.
- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html):
  log useful failure context while excluding sensitive values.

Alternatives rejected: weakening exact identity checks, adding blind retries,
changing timeouts without a measured cause, or dumping the entire identity and
its filesystem paths. This change repairs observability only; the intermittent
macOS cause remains open pending evidence from a recurrence.

Validation: eight diagnostic regressions fail before the change. Afterwards the
benchmark suite passes 150 tests with four platform/native skips (34.77 s).
Eleven targeted identity/CLI checks pass on real Python 3.10.20 (2.44 s); those
checks overlap the full suite and are not additional unique coverage. Ruff,
Python 3.10 syntax and complexity pass for 326 changed functions in the series.
All 97 installed dependencies are compatible. Evidence:
`logs/step7-pyright-diagnostics-{red,green,python310}.txt`.

## First review uses verified model capacity, 2026-10-18

The live automatic compile still reports `critique:...:input_budget`. Source and
fresh graph inspection found a reproducible admission defect: the initial review
used the draft packing target as a hard ceiling, while only a later corrective
draft could use the selected model's verified capacity. A review carries the
proposed operations and their evidence in addition to existing context; its size
need not equal the draft's packing target. This defect is proven independently;
it is not proof that every historical review refusal had this cause.

Two regressions reproduce the preflight/dispatch mismatch with verified capacity.
A third uses the real native citation layout and complete context: the unchanged
baseline refuses its single review operation although the request fits the
verified window. The change reuses the existing complete-context budget for both
review and corrective draft, with one condition shared by preflight and dispatch.
The previous correction-only helper and keyword are replaced. Ordinary draft
packing stays unchanged. Matching model identity, verified Codex basis, output
reserve, safety margin, evidence validation, DLP and real-overflow refusal remain.
No numeric threshold, retry, dependency, runtime path or environment contract is
introduced. Unknown capacity is not inferred from online model descriptions.

Sources inspected 2026-10-18, three independent providers:
- [Anthropic context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows):
  the complete request and output consume capacity; more context alone does not
  establish better quality.
- [Google token counting](https://ai.google.dev/gemini-api/docs/tokens):
  input measurement and model input/output limits are separate concerns.
- [OpenAI reasoning guidance](https://developers.openai.com/api/docs/guides/reasoning):
  output/reasoning require capacity beyond the input prompt.

Alternatives rejected: dropping evidence or context to fit the packing target,
blindly retrying a deterministic preflight failure, hardcoding a larger model
window, or changing the ordinary source packing target. The tradeoff is that a
qualified review may be larger, including combining operations that previously
required separate reviews. Every combined request is still measured. This may
reduce repeated context, but full-cycle token/latency improvement is not claimed
without a paired provider measurement.

A direct installed compile invocation on October 15's source refused because the
automatic compiler already owned its lock (PID 79619, started 09:15 UTC). It made
zero provider calls, then generation refresh reported `corpus_entry_limit`.
The live compiler was not terminated or mistaken for a successful rerun. The
review fix alone does not close the corpus-capacity, transaction-history or whole
nightly-cycle conditions of point 7.

Validation: the baseline fails two stage-budget regressions and the separate
native-layout regression. The candidate passes 143 related tests (25.77 s);
20 overlapping budget/dispatch tests pass on real Python 3.10.20 (2.18 s).
Ruff and Python 3.10 syntax pass; all 334 changed functions in the refactoring
series meet CCN <= 5, at most two `if` statements and nesting <= 2.
Evidence: `logs/step7-critic-context-{red,native-red,consumers,python310}.txt`.

## 2026-10-18: make the critic's citation addresses explicit

The installed October 15 compile was restarted gracefully after verifying the
old owner's process identity. Its real provider replies exposed a second issue:
one review called evidence index 3 nonexistent although the operation contained
five citations. The prompt carried operation-local claim indices alongside a
separately sorted, flattened citation list with no operation/index addresses.
This was ambiguous presentation, not proof that the rejected content was sound.

The critic's cited records now carry `operation_slug` and `evidence_index`.
The instructions define the index as zero-based and local to that operation,
and distinguish already computed hashes from the semantic judgment being asked
of the model. Existing source, quote, claim-origin, DLP and verdict checks remain.
The critique program becomes v8 so previously cached plans cannot bypass the new
review. No runtime path, environment contract, dependency or numeric limit changes.
The old unaddressed representation is replaced, not retained as an alternate mode.

Research inspected on 2026-10-18:
- [Anthropic prompting guidance](https://platform.claude.com/docs/en/build-with-claude/prompt-engineering/claude-prompting-best-practices)
  recommends explicit structure and document metadata where relationships matter.
- [Google prompt design](https://ai.google.dev/gemini-api/docs/prompting-strategies)
  describes clear instructions and consistently structured context.
- [OpenAI prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)
  recommends distinct context sections and evaluation when changing prompts.

Keeping the flat list without addresses relies on inference about its ordering.
Embedding all evidence again inside every claim would increase duplication.
Explicit addresses preserve the existing full evidence and make the relationship
machine-checkable at small prompt cost. These sources support the design choice;
they do not establish that any prompt makes a model infallible.

A regression uses two operations with the same two quotes in reversed order.
It fails on the old prompt because neither citation has an operation address.
The candidate passes 244 related tests and 42 overlapping tests on Python 3.10.20.
Ruff, installed dependency compatibility (97 packages), and complexity analysis
of 338 functions pass. Real Codex/gpt-6-luna reviews of the same public fixture
provide a narrower causal check: the baseline wrongly drops the second operation
because it reads the nightly-schedule quote instead of its selected lease quote;
the candidate passes both correctly. A negative candidate with a 60-second claim
against a 30-second quote is still dropped. These are three single-review calls,
not a whole-cycle benchmark: baseline 10404 input/83 output tokens, 4.954 s;
candidate 10525/91, 4.728 s; negative 10525/96, 5.536 s. Prompt growth is 568 bytes.
No full-cycle latency or token saving is claimed from this small fixture.

Other observed failures remain separate. Windows Python 3.12 CI job 117659144774
failed four of eight telemetry writers at the second BEGIN IMMEDIATE; Linux's
31 telemetry tests pass, so the Windows cause is still unproven. Real draft
responses also contain malformed JSON. Codex 0.162.1 accepts --output-schema,
but the original compile schema is refused (optional properties, then unsupported
regex lookaround). An experimental all-required schema without regex works on an
empty public fixture only; it is not installed or equivalent to production
validation. The live corpus census found 220525 sources / 305660563 source bytes,
exceeding the installed profile. This census is not a successful full generation.
Point 7 remains open pending real compile/nightly, capacity and recovery evidence.

Evidence: `logs/step7-critic-address-{red,tests,python310}-20261018.txt`,
`logs/step7-critic-address-{baseline,candidate,negative}-probe-20261018.json`,
`logs/step7-response-evidence-cycle-20261018.log`,
`logs/step7-windows312-failure-20261018.log`,
`logs/step7-current-corpus-capacity-20261018.json`.

### 2026-10-18: structural refusals participate in correction

The installed e4aaff4e run of the October 15 source ended after 2596.40 seconds
and 88 completed provider calls: 31 published batches, five failed batches, and
memory-generation publication deferred by `corpus_entry_limit`. Local evidence is
`logs/step7-critic-address-full-cycle-20261018.{json,log}`. Its failures include
malformed JSON, non-string related entries and an unsupported category, besides
semantic refusals. `_planned` and `_normalized` recorded structural refusals but
sent neither the refused answer nor the validation diagnostic to the next draft.
This made retries blind. A subsequent empty answer could even complete the source
because only semantic criticism had populated the unresolved-work feedback.

Research checked on 2026-10-18, independent primary sources:

- [Pydantic AI output validators](https://pydantic.dev/docs/ai/core-concepts/output/)
  return validation errors to the model through ModelRetry.
- [LangChain structured-output error handling](https://docs.langchain.com/oss/python/langchain/structured-output)
  supplies the specific schema failure to the next attempt.
- [Anthropic structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
  distinguish native schema constraints from original-schema local validation;
  supported schema subsets differ. Native output does not replace our evidence
  and source-completion requirements.

Chosen implementation reuses the existing complete-source correction path for
both structural validation stages. One helper carries the exact rejected answer,
stage and validation diagnostic as untrusted correction data. Existing semantic
review records survive; prior invalid addresses are explicitly not authority.
The existing model-capacity check measures the whole correction before calling.
The existing empty-rewrite refusal and cache bypass now also cover structural
refusals. Successful corrections undergo the same schema, citation, critic and
normalization checks. Draft program v20 invalidates v19 cached answers. No second
repair loop, dependency, persistent format, path or environment contract is added.

Alternatives rejected for this change: repeating the same request conveys no
new diagnostic; coercing malformed fields would silently invent meaning; enabling
native output without qualifying the provider's schema subset is incompatible
with the current contract. Native constraints remain a separate improvement.
No retry count or timeout is raised. The inherited two-retry policy is still not
qualified as an optimal cost/quality tradeoff and remains open under law 9.

Five initial regression cases failed on the installed implementation and passed
with the correction path. Additional tests cover normalization diagnostics,
whole-response sizing before calls, preservation of prior review feedback,
refusal of empty completion, cache bypass and cache-version invalidation.
A public-source paired Codex probe seeded the same non-string related entry:
base accepted an empty result in one call (11415 input / 8 output tokens, 4.83 s);
candidate produced one exactly evidenced, critic-approved fact in two calls
(21641 input / 169 output tokens, 10.96 s). This is a correctness improvement in
that observed fixture, not evidence of aggregate token savings or a successful
production-day compilation. Reports: local `logs/step7-validation-feedback-*-probe-20261018.json`.

Remaining compiler work is explicit: source-wide verification of no-content and
completeness, optional claim/body review semantics, malformed review handling,
qualification of retries and native output, and a successful full production
cycle. None is claimed solved by repairing structural feedback.

Validation for this change: 656 related Python 3.12 tests passed; the final nine
new regressions passed after two extra cache assertions were added; 38 focused
Python 3.10.20 tests passed. Ruff and measured complexity passed (353 changed
functions since the refactor baseline, each within the user limits). Installed
97-package and isolated 66-package dependency checks passed; uv.lock unchanged.
An initial `pip install -e .[dev]` attempt failed because this uv project is not
an editable setuptools package; the supported `uv sync --locked` succeeded.
The preceding e4aaff4e GitHub matrix completed successfully; it does not validate
this new commit and does not establish the cause of the earlier Windows failure.

### 2026-10-18: selected capacity and independent source completeness

Current implementation evidence: the automatic provider chain selected a verified
Codex planning window, but `_mandatory_planning_window` discarded that window
whenever any fallback provider was present. On the complete October 17 snapshot,
136 of 875 atomic source groups were refused by that calculation; using the
selected identity's verified window refused none. The candidate calculation also
refuses none, without splitting atomic groups or increasing the model window.
An unverified fallback cannot borrow the selected model's expanded capacity.
Local reports: `logs/step7-auto-capacity-{measure,candidate}-20261018.json`.

Independent primary references checked on 2026-10-18:

- [Anthropic context windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)
  explains model-specific total context accounting and the quality implications
  of long inputs.
- [Google token accounting](https://ai.google.dev/gemini-api/docs/tokens)
  distinguishes input/output limits and counting actual request content.
- [OpenAI conversation state](https://developers.openai.com/api/docs/guides/conversation-state)
  describes context consumed across requests and the need to respect the window.
- [Ragas context recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)
  separates missing reference information from faithfulness of generated claims.
- [Anthropic evaluation guidance](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)
  supports explicit success criteria and representative evaluations.
- [OpenAI evaluation guidance](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
  supports task-specific, separately measured quality criteria and regression cases.

The compiler now independently reviews the complete selected sources and existing
context against the whole proposed plan, including an empty plan. Per-operation
citation and meaning checks still apply. Missing durable facts prevent a receipt
and return the omission diagnostic for correction. An empty correction can finish
only when that independent review establishes no new durable content. This
supersedes the preceding categorical empty-correction refusal. Structured claims
are optional enrichment; every body assertion still needs supporting evidence.
The prior unchecked-empty action/cache path is removed. Review program v9 and its
schema-bound hash prevent reuse of the earlier unchecked cached results.

Alternatives: rejecting all empty plans prevents legitimate duplicate/no-content
completion; judging only proposed operations cannot detect omitted source facts;
a keyword inventory cannot reliably decide semantic completeness. The chosen
model review is a fallible assessment, not a mathematical proof. It costs another
full-source call per successful batch. No aggregate efficiency gain is claimed;
full production-cycle quality, tokens, retries and elapsed time remain required.
No evaluation framework or other package was added.

Any stage's input-budget refusal now permits the existing finite repartitioning
of complete source groups. Structural correction diagnostics are preserved whole
as untrusted data for each child; previously that path assumed every feedback
record contained operation reviews and raised KeyError. Regression tests first
reproduced this failure. No source/context truncation is introduced.

The owner approved `compile.validation_retries` (standard environment override
`LLM_WIKI_COMPILE_VALIDATION_RETRIES`) before implementation. Default 2 correction
passes is an explicit cost/quality compromise, not a measured optimum. It is
nonnegative with no additional upper cap, and must be reconsidered after changes
to models, prompts or workload using complete-cycle evidence. The hidden constant
and its unsupported claim about customary retry counts are removed.

Candidate validation: 824 related tests passed on Python 3.12; 46 focused tests
passed on Python 3.10.20. Ruff and complexity checks passed (420 changed functions
since the refactor baseline). Installed 97-package and isolated 66-package checks
found no dependency conflict; the lockfile is unchanged. Real Codex/luna probes
refused an omitted durable fact, accepted its complete plan, and accepted empty
only when existing context already held it. An optional-claims probe passed a
fully evidenced two-fact body with one structured claim, and rejected changing
30 seconds to an unsupported 60. These public fixtures do not qualify a complete
production day. Malformed-review correction, native output qualification, full
production/nightly completion and corpus capacity remain open.

A complete public-fixture run subsequently committed the exact Cedar fact through
normal compile transactions: seven real model calls, 44.25 seconds. The first
empty draft was refused by source review; a wrong citation in its correction was
refused by operation review; the final corrected plan passed both and published.
This demonstrates recovery without weakening either check, while also exposing
its cost. Report: `logs/step7-source-completeness-cycle-20261018.json`. A prior
harness attempt omitted the required paired batch descriptor and failed before
publication; the corrected harness used the ordinary batch contract. An additional
41 structure tests passed. Production-day validation remains outstanding.

### 2026-10-18: one durability rubric for all compile roles

The installed 87453316 production run of the October 15 pending sources ended
with three failed batches after 21 calls / 350.02 seconds; nothing was published,
and generation refresh deferred on `corpus_entry_limit`. Source completeness
exposed omissions, but also exposed inconsistent role criteria: response 10
required ownership/coordination information, while response 14 rejected the
corrected generic work directives as transient. Response 6 required a separate
research step for an unresolved issue, while response 8 correctly found that
requirement unsupported. These are failures, not successful source completion.
Local evidence: `logs/step7-source-completeness-full-cycle-20261018.json` and the
numbered provider-response records. No refused source was receipted as complete.

Research rechecked on 2026-10-18: [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
requires task-specific criteria and comparison against historical failures;
[Anthropic evaluation guidance](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)
recommends clear rubrics and ambiguous edge cases;
[Ragas context recall](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_recall/)
requires supported reference information for omission assessment. These sources
motivate explicit criteria; they do not prove that any model judge is infallible.

Chosen correction: one shared durability rubric in the draft, operation review
and source review, bound into both program hashes. Explicit scoped assignments,
constraints and unresolved risks can be durable; generic instructions to continue
or check dependencies create no new project fact by themselves. Reported findings,
uncertainty, suggestions, requests and completion must remain distinct. Neither
review may invent a requirement for research or another next step from an open
problem. No project-name special case, additional retry, model change, dependency,
public interface or relaxed evidence check is introduced. Draft v21 and review v10
replace the preceding programs. A shared rubric reduces inconsistent instructions;
it cannot guarantee identical stochastic judgements.

Alternatives rejected: raising retries leaves contradictory criteria intact;
accepting either reviewer overrides a necessary check; hardcoded exclusions for
individual projects hide the general defect. The extra rubric increases request
size. Four existing tests correctly found their 19 KB admission fixture no longer
fit the unknown-model whole-prompt budget. Their positive fixture is now 17 KB;
all exact-byte and mandatory-context assertions remain, and the separate 38 KB
unknown-capacity refusal remains unchanged. The product budget was not raised.

Validation: 825 related Python 3.12 tests and 35 focused Python 3.10 tests passed;
Ruff and measured complexity passed (424 changed functions since the refactor
baseline). The regression first failed because no shared rubric existed. A real
candidate probe passed eight judgments covering generic continuation, omitted and
preserved explicit ownership, preserved unresolved status, and invented research
requirements. The earlier probe used an unrelated fixture title; corrected probes
use a neutral matching project title and are recorded separately as `*-v2`.
The complete public Cedar cycle published through normal transactions in three
calls / 16.81 seconds / 32083 input and 254 output tokens. The preceding version's
observed cycle required seven calls / 44.25 seconds. This small stochastic example
is not an aggregate performance guarantee or a production-day success.

### 2026-10-18: complete feedback before correction, repair the failed role

The 415fbff5 pending-source run left two failed batches after 22 calls /390.76s,
with one batch published. Inspection found that `_review` raised on an operation
refusal before `_review_source_completeness` could run. A correction therefore
received citation/scope problems first and missing-fact problems only after the
next complete generation, exhausting the same correction allowance piecemeal.
Malformed reviewer output also went straight back to drafting with no review-format
diagnostic. A further operation-verdict retry lived outside the configurable
correction budget. New regressions reproduced these paths before implementation.

Primary research checked 2026-10-18: [Pydantic AI output validation](https://pydantic.dev/docs/ai/core-concepts/output/)
returns validation diagnostics to the generating role and charges the output retry
budget; [LangChain structured-output error handling](https://docs.langchain.com/oss/python/langchain/structured-output)
distinguishes recoverable format errors and supplies specific correction feedback;
[Anthropic evaluation design](https://platform.claude.com/docs/en/test-and-evaluate/develop-tests)
uses explicit task-specific criteria and edge cases. Chosen implementation uses
those principles in the existing pipeline, without importing these frameworks.

Validated operation refusals now collect source-completeness findings in the same
pass. Neither check can override the other: a refused operation still prevents
publication even when completeness passes. A malformed or incomplete review is
kept as untrusted correction data for the exact review request. The next existing
correction pass resumes that review with its unchanged source/proposals, system
role and schema; it does not redraft pages. Invalid review data never enters the
semantic draft-feedback channel. Full correction requests are sized before calls,
and a new provider starts without the previous provider's pending format repair.
The hidden one-off missing-verdict retry is removed; all such corrections spend
`compile.validation_retries`. No nested retry allowance, arbitrary upper cap,
provider switch, external dependency, path or environment contract is introduced.

Draft v22 explicitly asks for coverage of all selected sources on the first pass;
review v11 explicitly counts facts already preserved in context, compares meaning
rather than identical wording, and must identify what is absent from both context
and proposals. This addresses the observed refusal that acknowledged the existing
internet-policy decision yet demanded another operation for it. The claim that a
particular reason is genuinely new still requires semantic judgement; prompts
cannot guarantee perfect model agreement. Both program identities invalidate prior
cached plans. Alternatives rejected: raising the retry limit leaves staged blind
feedback intact; passing a malformed review weakens safety; a second repair loop
multiplies spending; redrafting an unchanged valid proposal wastes work.

CI39135555067 failures were independently identified: an older fake-provider fixture
returned draft JSON for source review, one address test asserted obsolete draft v20,
and four newly added test files lacked measured shard weights. The fixture now
explicitly supplies both expected response shapes while retaining its audit-tail
parsing check; the cache-version assertion remains; actual test timings populate
the weight table. No failing check is skipped or relaxed.

Final local validation: 892 related Python3.12 tests passed, including structure,
limits, provider, failed-CI cases and measured shard registration; 39 focused
Python3.10 tests passed. Ruff and measured complexity passed (442 changed functions
since the refactor baseline). Both dependency checks passed (97 installed and66
isolated packages); uv.lock is unchanged. A real model repaired an injected review
missing `reason` with two review-role calls, no new draft, and preserved the fact.
A complete public fixture committed in three calls/17.90s. A longer first-draft
instruction initially displaced optional context in two packing regressions; the
instruction was shortened in place and both original full-context assertions passed
without changing their fixtures or product budgets. Real pending-day validation
and the new full platform CI still have to qualify installation.

## 2026-10-18: the filesystem probe uses the caller's deadline

CI39145827607 on22e54757 failed only Windows/Python3.11 shard3: the real
SQLite filesystem probe took1.12s and reported unknown locking. The caller
allowed30s, but `_filesystem_check` silently replaced that deadline with1s.
The constant itself documented that its numerical basis was unknown. This
also affected operational/full doctor and generation maintenance, not just CI.

Research checked2026-10-18:
- [SQLite locking](https://www.sqlite.org/lockingv3.html): rollback-mode
  correctness requires actual lock exclusion; elapsed startup time is not
  evidence that locking is broken.
- [CPython3.11 test support](https://docs.python.org/3.11/library/test.html):
  completion waits must accommodate slow supported machines.
- [pytest flaky tests](https://docs.pytest.org/en/stable/explanation/flaky.html):
  timing-sensitive assertions can fail independently of functional correctness.

Chosen: propagate the finite doctor/generation caller deadline unchanged.
Standalone private calls use the existing doctor default,5s. Delete the
independent1s constant. No new setting, dependency, timeout retry or success
substitution. A longer caller allowance can spend more of that allowance on
filesystem I/O; cooperative deadlines still cannot preempt an OS syscall.
Rejected: increasing a hidden constant, skipping Windows, accepting degraded
as success, or merely rerunning the failed job.

Regression uses real SQLite and a controlled clock: each connection open
consumes1.1s; both standalone and30s caller cases must prove writer exclusion
and remove every probe artifact. A separate case exhausts the caller deadline
and must remain degraded with no artifacts. The initial test setup omitted
run/ and therefore did not exercise locking; only the corrected red-v2 run
(2 failed,1 passed) is reproduction evidence. Candidate:241 passed,6 skipped
across doctor and reliable-memory tests. Native Windows qualification still
requires the subsequent CI run; Linux success is not a Windows result.

## 2026-10-18: restored breadcrumb bytes still need a committed retry witness

Installed refusal9bf23154f31543b090e7e431e2ed49d1 has a byte-identical
source head, restorable560-byte content and a succeeded linked queue task,
but no committed create witness. The original mechanism that restored the
file has not been established. The independently reproduced recovery defect
is that `_create_source_document` returns immediately for matching bytes,
so retrying this refused create cannot establish transactional completion.

Research checked2026-10-18:
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html): durable
  completion requires the transaction's write/flush/commit sequence.
- [AWS idempotent APIs](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/):
  request identity must bind the intended operation, and retries must preserve
  caller intent rather than infer completion from a similar outcome.
- [Microsoft idempotent consumer](https://learn.microsoft.com/en-us/azure/architecture/patterns/idempotent-consumer):
  durable processed state and business effects must agree when messages repeat.

Chosen: when the same path/content identity has a quarantined attempt and
no committed retry, reassert identical bytes through the existing writer gate,
hash-preconditioned replace, recoverable transaction and parent lineage.
The original refusal remains quarantined history; the actual committed retry
resolves it. Already committed requests and matching files without a refused
attempt keep their no-write behavior. Conflicting bytes still fail; a moved
archived file does not authorize recreating a missing hot path. Current capture
preconditions remain mandatory for a worker retry. No new runtime path, setting,
external dependency or diagnostic exemption is added.

Rejected: manually changing a refused state to committed; trusting queue
success alone; treating file existence as a durable transaction witness;
deleting and recreating the immutable source. The chosen recovery incurs one
same-byte transactional write and retained undo evidence only for an unresolved
refusal. The recovery API's existing gate, CAS and transaction recovery provide
the concurrency boundary. A regression now runs the existing refused-create
scenario both with an absent source and with independently restored identical
bytes, checks idempotent repeated retry, unchanged bytes, committed parent
lineage and resolved-quarantine evidence. Before the change the restored case
failed while the absent case passed.

Validation:200 breadcrumb/queue/recovery tests passed, including real process
termination and recovery after the persisted lease expiry (349.68s);57 lineage,
retention and structure guards passed;210 doctor/breadcrumb tests passed on
Python3.10 (3 platform skips). Ruff and measured complexity passed for453
changed functions across the accumulated refactor. Installed97 and isolated66
packages are compatible; uv.lock remains unchanged. Production recovery has
not yet run: installation must wait for the live generation maintenance owner.

## 2026-10-18: impact timeout propagation is independent of runner speed

Windows Python 3.10 job 117755905783 failed
`test_active_graph_propagates_deadline_timeout[graph]`: the fixture allowed five
real seconds for Git scope resolution and code-generation lookup before reaching
its injected active-generation timeout. The trace shows the real code-generation
deadline firing first after 5.39 seconds. Production propagated that timeout
correctly; the fixture expected another exception's message. This establishes a
test-isolation defect, not evidence that production lost the deadline.

The test now supplies controlled collaborators for scope, code generation,
catalog registration and active generation. Each stage independently raises one
sentinel TimeoutError. Assertions require the identical exception, the unchanged
caller deadline at every visited stage, and no calls after refusal. Four cases
replace the two partially isolated cases. Existing real Git, generation-open,
expired-deadline and public analysis tests remain. There is no new dependency,
production timeout, retry or configuration. The synthetic absolute deadline is
only a routing token; this test makes no wall-time claim.

Research checked 2026-10-18:
- https://docs.pytest.org/en/stable/explanation/flaky.html — insufficiently
  controlled system state can make a test intermittent; isolate the relevant unit.
- https://docs.python.org/3/library/unittest.mock.html — side_effect and call
  inspection support exact exception and argument checks with standard-library
  test collaborators.
- https://testing.googleblog.com/2020/12/test-flakiness-one-of-main-challenges.html
  — nondeterministic test outcomes reduce the usefulness of the testing signal.

Rejected: increasing five seconds (retains a speed-dependent routing test),
accepting any timeout text (could pass without reaching the selected stage), and
changing production exception handling (the trace confirms correct propagation).
Tradeoff: this unit test no longer exercises real Git/catalog latency; existing
integration cases retain that coverage. The changed function satisfies measured
CCN, branch-count and nesting requirements.

Validation: 188 related impact/code-graph tests pass on Python 3.12 (10.12 s)
and Python 3.10.20 (12.43 s). Deliberately replacing the production TimeoutError
rethrow with `return None` makes all four propagation cases fail; the mutation was
then reverted byte-for-byte. Ruff passes across scripts/tests; the cumulative
complexity check covers 454 changed functions with no violations. Installed
97-package and isolated 66-package dependency checks pass. Evidence is retained in
`logs/step7-impact-timeout-{tests,python310,mutation}-20261018.log`.

### 2026-10-18: secondary claim retrieval inherits compilation's stop contract

The isolated Sol trials exposed a `corpus collection deadline reached` failure
while publishing reviewed plans. `compile_memory._ApplyPlan._pipeline` called
`default_secondary_search` without its deadline or cancellation callback. The
installed-vault route consequently reached `collect_corpus(deadline=None)` when
no generation existed, selecting the collector's 30-second default regardless
of the remaining compilation budget. The other-vault route also ignored stop
requests while scanning pages. The exact changed source files were identical
between the installed 22e54757 and candidate 97b46523. The built-in graph was
queried, then the isolated checkout was indexed at 97b46523 and inspected via
`get_architecture`; unresolved calls were checked directly in source.

The correction propagates the existing caller deadline and cancellation flag
through both routes. An unbounded compile retains the search default rather
than passing infinity to the corpus collector, whose finite-deadline contract
is unchanged. The foreign-vault scan checks cancellation before collection,
between pages, and before returning; directory enumeration retains its deadline
checks. No search scope, claim verification, lifecycle rule, corpus bound, retry
count, or dependency changes. This is not a claim that a large unindexed corpus
can always be searched within the caller's budget.

Primary-source review on 2026-10-18:

- [gRPC deadline propagation](https://grpc.io/docs/guides/deadlines/): downstream
  work should honor the original caller's deadline and cancellation.
- [AWS timeout guidance](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/):
  select budgets from observed latency; premature timeouts can amplify retries.
- [Python 3.10 monotonic clock](https://docs.python.org/3.10/library/time.html#time.monotonic):
  relative deadlines use a clock unaffected by wall-clock changes. The patch
  uses the existing monotonic contract and remains Python 3.10-compatible.

Alternatives rejected: increasing the global corpus default changes unrelated
callers; suppressing the exception disguises an incomplete search; limiting
this search to notes drops previously eligible context. Propagation retains
all sources and reports actual exhaustion. It does not introduce a dependency
on gRPC, AWS, or another search engine.

Operational evidence, distinct from code validation: the installed generation
rebuild was killed by Linux OOM at 21:04:39 UTC (PID 1297563, 3,477,212 KiB
anonymous RSS). Concurrent code indexing added memory pressure. The dependent
installation chain stopped without claiming success. The already-tested
97b46523 changes were subsequently installed as manifest generation 71; the
source's quarantined attempt received a committed matching-byte retry without
deleting the original refusal. The approved health budget was installed as
generation 72, followed by the user's selected Sol 6.1 / medium provider.
Full generation, full nightly/weekly completion, and full CI remain unverified.
The remaining Windows 3.12 shard exceeded its existing one-hour CI limit;
GitHub refused an explicit rerun with HTTP 403 (admin rights required).

Validation: 125 contradiction/publication tests passed on Python 3.12 and 125
on Python 3.10. Restoring the original two production files makes all five new
stop-contract cases fail; restored candidate passes. Ruff passes across
`scripts/` and `tests/`; the cumulative changed-function check measured 472
functions with no CCN/branch/nesting violations. Dependency consistency checks
passed for all 97 installed packages and 66 isolated Python 3.10 packages.
No package or lockfile change was needed. Operational doctor completed all 14
checks in 1.48 seconds: 7 ok, 6 degraded, 1 error (scheduler history); this does
not certify retained history or generation health.

## 2026-10-19: shared index capacity must stop the compile pass

The live run reached exactly 3,452 notes, the installed `index.max_pages`.
Publication of another page failed, but `_apply_batch` treated this global
resource refusal as an independent bad source. Later batches continued spending
model calls. The old process was interrupted with SIGINT; committed receipts and
source data were retained. The interrupted run is not a successful compile.

The index now raises `IndexCapacityError`, a `ValueError` subtype, for its two
existing aggregate admission checks. The compiler checks the real index before
model planning and propagates that exception through publication, stopping the
whole pass, including recursively repaired partitions. Other independent source
failures retain their existing continuation behavior. Publication still checks
current bytes under the writer gate; the preflight is not a reservation. No new
setting, dependency, runtime path or retry limit is introduced.

Research checked on 2026-10-19:
- [Google SRE, Handling Overload](https://sre.google/sre-book/handling-overload/):
  reject resource refusals cheaply; capacity proxies need review as workloads grow.
- [AWS, Timeouts, retries, and backoff with jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/):
  retries can amplify failure and load; repeated attempts cannot fix unchanged capacity.
- [Python 3.10, user-defined exceptions](https://docs.python.org/3.10/tutorial/errors.html#user-defined-exceptions):
  a specific exception type permits selective handling without matching message text.

Alternatives rejected: suppressing the publication error would lie about durability;
continuing every batch repeats an unchanged failure; stopping on every ValueError
would hold independent good sources behind malformed ones. Raising the capacity
alone would leave the same failure mode at the next boundary. The selected change
retains existing admission rules and distinguishes their shared failure precisely.

Read-only live measurement: 3,452 notes, 25,724,108 source bytes, index rendering
0.531 s, process peak RSS 54,228 KiB, published index 626 bytes. Evidence:
`logs/step7-index-capacity-measurement-20261019.json`. The existing 80% inventory
warning convention gives a reviewed page allowance of 4,315. This measures today's
workload, not every workload at that allowance; byte guards remain unchanged.
Tests cover both preflight limits before model planning and a publication failure
that must stop the next batch. Removing the compiler fix makes those three cases
fail. The full live compile, CI and health are separate completion gates.

Validation: 126 relevant tests passed on Python 3.12 and 3.10; the initial claim
that Ruff passed was incorrect (see correction below);
all 478 cumulatively changed functions passed actual lizard/AST CCN, if-count,
and nesting checks. `uv pip check` passed for installed 97 packages and isolated
Python 3.10's 66 packages. The lockfile is unchanged. Evidence is in
`logs/step7-index-capacity-{tests,python310,original-red}-20261019.log`.


### 2026-10-19: correction of the lint evidence

CI run 39218333004 correctly rejected the new single-line import with I001.
Local and CI Ruff are both lockfile version 0.15.21. The local lint also failed,
but its asynchronously returned output was not inspected; the shell command
continued to pytest and returned that successful exit status. This was an agent
verification/reporting error, not a version discrepancy or a Ruff cache defect.
The import is now formatted by that same Ruff version. A separate full
`ruff check --no-cache scripts/ tests/ benchmark/` succeeds, and the cumulative
lizard/AST checks succeed. The existing CI rule caught the original error and
remains enabled. No behavioral code, dependencies, or checks were weakened.
Subsequent checks are invoked independently and each exit status is inspected.

## 2026-10-19: stale model context is terminal even without structured claims

Live publication twice failed against the progress-note precondition after the
agent edited that note during compilation. Each plan carries immutable target
snapshots. `_ApplyPlan.assess_claims` checked their freshness only after the
`claims` guard; plans without structured claims skipped that check. Their
transaction refused correctly, but `_published` retried the same stale target
preconditions instead of classifying this as `compile_snapshot_changed`.

Move the existing snapshot check before the claims guard and reuse the manifest
for claim assessment when needed. No context hash is refreshed to make an old
plan pass. No read dependency, transaction precondition, receipt check, cache
validation or completeness review is removed. The existing committed-receipt
shortcut still runs first. No new retry limit, setting, path or dependency is
introduced. Derived-tree races remain distinct from changed model inputs.

The two new public-apply regressions cover a no-claims plan whose input page
changes before preparation and one whose page changes during publication. Both
fail on the original implementation; after the fix the latter attempts publication
once, then refuses the stale snapshot, preserving the concurrent edit and issuing
no success receipt. The existing tests also exercise cached resolution without
LLM calls, committed replay, interrupted transaction recovery and claim-tree races.

Research checked 2026-10-19:
- [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html):
  a serialization conflict needs a fresh transaction snapshot, not blind reuse of stale reads.
- [SQLite isolation](https://sqlite.org/isolation.html): serialized writes do not make
  application decisions based on an older read valid. This product retains rollback-journal mode.
- [AWS retry guidance](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/retry-backoff.html):
  distinguish transient from non-transient failures and preserve idempotency.

These sources support the failure classification; they do not establish that
Markdown transactions have database-wide isolation. The simpler reuse of the
existing freshness check was selected over adding another plan store (validated
plans are already cached), overwriting stale hashes, or holding a writer gate
through model calls. A genuinely changed model input still requires a fresh plan;
this change does not pretend to solve all quality refusals or concurrent edits.

Evidence: `logs/step7-publication-snapshot-red-20261019.log`,
`logs/step7-publication-snapshot-tests-20261019.log`. Python 3.12: 96 passed;
Ruff over scripts/tests/benchmark passed separately; cumulative lizard/AST: 482
changed functions, no CCN/branch/nesting violations.
Python 3.10: the same 96 tests passed; its 66 installed packages pass `uv pip check`.
