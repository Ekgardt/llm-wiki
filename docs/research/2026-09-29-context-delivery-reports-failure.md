# Context delivery reports failure

Research date: 2026-09-29. Implementation qualification is recorded below separately.

Three regression cases reproduce successful status after a broken output pipe,
failed flush, or unhandled context preparation error. The last case fabricates
an empty successful response. The cause is the hook's unconditional success
contract, not the adapter: the adapter already rejects nonzero delegate output
and records failed delegates. Codebase Memory was refreshed and the connected
emission and adapter functions inspected against source.

## Sources and choice

- [Claude Code hook reference](https://code.claude.com/docs/en/hooks): exit 1
  can report a nonblocking failure; valid JSON can override a nonzero result.
- [Python exit status](https://docs.python.org/3/library/sys.html#sys.exit):
  zero denotes success and nonzero denotes abnormal termination.
- [OWASP logging guidance](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html):
  retain useful failure evidence, redact secrets and neutralize record separators.

Installed versions inspected: Python 3.12.3 and Claude Code 2.1.283. Current
Python documentation describes 3.14.7; the exit-status behavior used here is
also present in the installed interpreter. A version query is not native-hook
qualification.

Return 1 after failed emission or unhandled preparation; flush before declaring
delivery successful. Keep the existing protected error log. Do not emit an empty
success response after an exception. Intentional no-op paths remain no-ops.
The canonical adapter already separates host continuation from delegate success.
No structure, runtime location, environment contract, dependency or limit changes.

Alternatives: unconditional success conceals the reproduced error; a blocking
exit code is unnecessary for optional context; retrying stdout can duplicate
partially delivered output. Existing logging is preferable to a second sink.
For a directly registered native hook, bytes already written cannot be retracted;
valid JSON may override a later flush failure in the host. This change proves
the delegate status, not universal host presentation of every partial-write case.
Log persistence remains best-effort when the filesystem itself is unavailable.

## Verification

Before the implementation: 3 failed, 1 passed in the isolated verification tree
(`/tmp/repair-hook-output-status-red.txt`). The first attempted post-change run
used an old verification copy because the copy command addressed the same source
and destination; its three failures are retained, not treated as qualification.
After correcting the copy and comparing SHA-256 digests, the complete relevant
run passed 200 tests with 21 existing skips in 54.78 seconds. This includes the
actual Lizard/AST complexity gate, hook output faults, bootstrap publication,
integration injection and existing adapter tests that reject failed delegate
JSON and record failed delegates. Ruff passed. The old unconditional-success
branch was removed. No new numeric limit or alternate implementation remains.

Evidence: `logs/audit-2026-09-29-completed-repair-hook-output-status-*`.
This is local component/integration qualification, not a live native-host test
or the final full-project suite. The private progress/log append remains deferred
while compilation owns its knowledge snapshot.

## Related session-end boundary

Connected graph/source review found the same unconditional-success contract in
`session_end_project_tag.main`, plus suppressed acknowledgement write errors in
`_report`. Three additional regressions fail before correction; both legitimate
write/no-op controls pass. The existing adapter requires both return code zero
and `daily_log_written: true`, so preserving that envelope on genuine success
keeps its contract. The same three independently maintained primary sources
above support nonblocking failure status and protected diagnostics here.

Select one outer error boundary covering tagging and flushed acknowledgement.
Exceptions return 1 without manufacturing a no-op response. Remove the suppress
branch and its now-unused import. This distinguishes an unavailable writer from
intentional skipping. An acknowledgement failure after a committed write does
not undo it: status means the overall invocation failed, not proof of no write.
Existing operation identities and writer deduplication remain authoritative;
this change does not add automatic retries. Native-host qualification and the
durable ingress cutover remain separate open work.

Session-end qualification: the old code fails three cases and passes both
controls. After correction, 137 tests pass in 35.33 seconds, covering skip
semantics, adapter consumers, hook budgets, daily clock handling, protected
diagnostics and the actual repository-wide complexity gate. Ruff passes.
The source and isolated test copy are compared before archiving evidence under
`logs/audit-2026-09-29-completed-repair-session-end-status-*`.

## The command reports the write result

On 2026-09-29, the native LLM Wiki index was checked fresh and its
get_architecture callers/callees inspected against the CLI and adapter source.
The graph is explicitly incomplete; source searches confirm the affected helper's
only caller. Two current regressions reproduce the CLI saying "Daily log tagged"
when the adapter returns `daily_log_written: false`, both with and without a
separately spawned flush. Both actual-write controls pass.

The cause is unconditional wording in `_print_daily_tag`, not the writer. Select
the message from the existing write-result boolean and keep the independent
flush-spawn report. Rename this private helper to `_print_daily_capture` because
it also reports a no-write result. This repairs presentation of the existing
contract; no architecture, JSON schema, retry, runtime path, dependency, model
call or limit changes. Claiming a write from exit status alone repeats the bug;
calling an intentional skip a failure changes the contract unnecessarily.
The existing source research above governs the related status/error boundary.
The corrected batch passed 105 tests in 34.55 seconds, including the actual
Lizard/AST gate and existing JSON, heartbeat and delegate-failure tests. Ruff
passed. The old private helper has no remaining code/test references. The
regression failed twice before correction while both positive controls passed.
Evidence: `logs/audit-2026-09-29-completed-daily-report-*`. Native host delivery
and full-project qualification are not inferred from this result.
