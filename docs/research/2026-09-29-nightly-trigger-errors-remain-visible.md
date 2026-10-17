# Nightly trigger errors remain visible

Current status, 2026-09-30: the qualified source changes described here are installed. See [the installation evidence](2026-09-30-durable-capture-installation.md) for the final regression, live capture and nightly results. The checkpoints below retain their original dates and describe the state at that checkpoint; their pending-installation statements are historical. Installation does not close the remaining warning review, every native-event qualification, or the wider limit audit.


## Follow-up checked on 2026-09-30

The outer adapter correction below did not cover exceptions consumed inside
`session_start_context`: claim, release, recovery database validation and
transaction recovery. Four injected storage failures reproduced missing logs;
three new shared-writer tests initially failed because the helper did not exist.
The pre-change result was 7 failed and 13 passed. An absent recovery database
remains a normal first-start case and does not generate a failure record.

In the isolated candidate, `capture_diagnostics.record_hook_error` now owns the
existing `logs/hook-errors.log` append. The adapter and both project lifecycle
hooks delegate to it, replacing their duplicated append implementations. The
four internal failure handlers retain a redacted exception class and message.
Both the event kind and message are sanitized into one physical line. The helper
returns false if the destination cannot be written; this is best-effort
observability, not durable recovery evidence or a successful recovery receipt.
Existing wrapper names remain because callers and tests use them; their old
implementations were removed. No format, runtime path, dependency, timeout,
retention bound or environment contract was added.

The three independent primary references in the original report were checked again on 2026-09-30;
Python's version-specific [3.12 Logging HOWTO](https://docs.python.org/3.12/howto/logging.html)
was used for the installed interpreter. Only existing standard-library APIs
compatible with the project's Python 3.10 floor are required. Rethrowing from
the helpers would change their host-availability contract. A second log store or
global logger configuration adds unnecessary state. Importing the adapter from
the session context reverses its dependency direction; locating the shared
writer in the existing diagnostics module avoids that dependency. The tradeoff
remains that an unavailable filesystem can prevent recording the failure.

The native repository graph was fresh before implementation (generation
`generation-18da0e62511bf9e1-6b9fe808`); caller/callee queries and source inspection
covered all three previous writers and the affected internal handlers. Initial
post-change regression and repository-wide complexity/branch-shape checks passed
31 tests in 27.95 seconds. Ruff passed after correcting three import-order
findings. Wider qualification yielded 247 passed, 21 skipped and one failure in 59.45 seconds: the isolated copy lacked the tracked public project template. Copying that exact template allowed the unchanged slug test file to pass (the recheck output is retained). No test assertion or guard was relaxed. The post-change index refresh is pending.

This follow-up is not installed in the running vault. It does not resolve the
separate legacy writer crash-recovery gap or prove the nightly healthy. Private
wiki/log updates remain deferred while ownership-safe production mutation is
unresolved. The historical 2026-09-29 evidence below is not current whole-project
qualification.

## Original correction (2026-09-29)

Research date: 2026-09-29. Implemented; targeted qualification passed.

`integration_adapter._catch_up_missed_nightly` caught every exception and did
nothing. Its neighboring maintenance and compile calls already send redacted
errors to `_log_hook_error`. A temporary-vault regression reproduces the missing
nightly error record while preserving the successful host-safe return code.
Codebase Memory confirms the adapter's maintenance entry and the shared logger;
source inspection covers the catch-up claim/spawn and exception redaction paths.

Use the same existing logger and `describe_error` for this missing branch.
At the shared logger, normalize message whitespace into one line so exception
text cannot manufacture another log entry. Preserve the error type and useful
message, redact secrets before logging, and retain host availability. This does
not add a logging service, schema, directory, dependency, timeout or size cap.

Three independent primary references were checked today:

- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)
  calls for recording operational failures, excluding secrets and sanitizing
  event data against forged log entries.
- [MITRE CWE-778](https://cwe.mitre.org/data/definitions/778.html)
  describes missing event/error records as an obstacle to investigation.
- [Python Logging HOWTO](https://docs.python.org/3/howto/logging.html)
  distinguishes reporting errors from raising exceptions to interrupt execution.
  The current page is 3.14.7; this correction uses only existing project helpers
  and string operations supported by Python 3.10, tested on installed 3.12.3.

Failing the whole session-start pass would violate the existing host-safe
contract. Suppressing the error prevents diagnosis. Adding another diagnostic
store or changing global logging configuration duplicates the existing path.
Reusing the shared logger is the smallest correction to the established policy.
Filesystem failure of the logger remains best-effort under its existing contract;
this change does not guarantee durable logs on a failed device. A spawn that
returns no PID is a separate existing path and is not claimed fixed here.

## Verification and remaining work

The missing-error regression failed before correction. A second regression
proved that a newline in a shared hook error produces multiple apparent log
lines. The combined pre-fix result was two failures and one passing control.
After correction, the catch-up, session context, integration and actual
complexity checks passed 155 tests with 21 platform/tool skips in 49.53 seconds.
Ruff and diff checks passed. The checked copy and working tree matched on all
1101 files in the source manifest, including the two root installer scripts.

Two setup mistakes are preserved in the evidence: an initial command named a
nonexistent test file and ran no tests; the next run lacked root installers in
the isolated source copy and produced 15 failures, 140 passes and 21 skips.
Copying the tracked root files allowed the unchanged test set to run. No
assertion or guard was disabled to pass it. This does not constitute a full
project run or execution of the skipped platforms.

The old empty handler was replaced directly, with no alternate implementation
or compatibility switch retained. Codebase Memory was refreshed after the code
change. Long-lived processes are not claimed reloaded. Private wiki/log updates
are deferred until the running compile ends: an earlier concurrent checkpoint
invalidated a compiler snapshot. Completed proof files are retained under
`logs/audit-2026-09-29-completed-repair-nightly-trigger-*` and this public report
records what must be appended to the private progress page afterward.
