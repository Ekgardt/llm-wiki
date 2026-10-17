# Hook error sinks protect their diagnostic text

Research date: 2026-09-29. Implemented; focused qualification passed.

Follow-up: bootstrap exit diagnostics and the missing next-session retry mentioned
below were subsequently corrected; see
`2026-09-29-bootstrap-failure-keeps-its-cause.md` and
`2026-09-29-an-incomplete-project-bootstrap-is-revisited.md`.

The two session project hooks pass raw exception messages and tracebacks to
`_safe_write_error`. The adapter's `_log_hook_error` normalizes whitespace but
depends on each caller to redact secrets. Three synthetic regression probes
demonstrate that all three sinks currently write a fake token verbatim. The two
project hooks also allow an exception newline to create a second log record.
This demonstrates unsafe diagnostic output, not an observed real credential leak.

Use the existing `redact_secrets` implementation at each sink, then normalize
whitespace before writing the existing single-line record. Keep exception type
and non-secret explanation. This corrects the shared boundary rule across its
three existing implementations; no new logging subsystem or duplicate redaction
algorithm is needed. No runtime path, environment contract, schema or limit changes.

Three independently maintained primary sources inspected today:

- [OWASP Logging Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html):
  omit credentials and sanitize CR/LF at the event boundary.
- [MITRE CWE-117](https://cwe.mitre.org/data/definitions/117.html): external text
  entering a log without neutralization can forge records.
- [Python traceback documentation](https://docs.python.org/3/library/traceback.html):
  formatted tracebacks include exception values and line separators. Using
  `format_exc` does not itself make text safe for this log format.

Alternatives: keep per-caller redaction (already inconsistent), remove diagnostic
text entirely (loses the cause), add a new logging package (unnecessary), or switch
the log to structured JSON (requires changing existing readers). The existing
redactor and one-line format are the smallest compatible correction. Reformatting
whitespace loses visual traceback indentation, but retains the diagnostic text and
prevents synthetic record boundaries. Redaction protects the supported credential
patterns, not every conceivable sensitive string. The installed Python is 3.12.3;
this change introduces no new library or newer language feature.

Separate findings remain open: bootstrap ignores child exit failures and its
promised retry is bypassed once state.md exists; failed stdout emission can return
success; several diagnostic-write failures remain best-effort. This correction
does not claim those failures fixed or certify all log writers.

The three original probes failed on the unmodified sinks. After correction,
101 checks passed and 21 platform-dependent checks were skipped in 55.36 seconds:
the new regressions, integration injection, the actual Lizard/AST complexity gate,
and the broad-handler diagnostic guard. Ruff passed. The skipped cases are not
claimed as qualified on this Linux host. Proof files use
`logs/audit-2026-09-29-completed-repair-hook-log-redaction-`.
The old unredacted write expressions are replaced, with no alternate path retained.
Private progress/log writes remain deferred while compilation owns its snapshot.
