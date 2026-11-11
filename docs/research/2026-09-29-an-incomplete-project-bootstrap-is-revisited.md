# An incomplete project bootstrap is revisited on a later session

Research date: 2026-09-29. Implemented; focused qualification passed.

The session hook creates state.md before invoking bootstrap. If that child fails,
later sessions see state.md and return early. A journal-based handoff also bypasses
the bootstrap helper. Two regressions reproduce missing context on the next
session after a real child failure, even after the child is made operational.
This corrects the documented 2026-09-18 behavior that was never actually wired.

Before selecting a journal or legacy-state handoff, revisit bootstrap when an
existing state.md and a recognized project marker are present. Reuse the current
helper, which skips an existing bootstrap.md and records failure causes. Newly
created state still follows its existing creation path. This is one reconciliation
attempt per session event; it adds no immediate retry loop, timer, daemon, retry
counter, persisted record, directory, environment contract or queue kind. The
existing five-second child budget remains; no new numeric threshold is invented.
No LLM is involved in this bootstrap path.

Alternatives: retry immediately inside the first session (increases its worst-case
latency), add a new persistent queue job (unnecessary for this existing lifecycle
contract), require manual repair forever (does not implement the documented next
session behavior), or never retry once state.md exists (the reproduced defect).
The next session is a natural opportunity to reconcile missing derived project
context after permissions, contention or other operating conditions change.
Permanent faults still need their cause corrected and remain visible in the log;
this does not claim every bootstrap failure is transient.

Independent primary references inspected today:

- [Microsoft transient fault handling](https://learn.microsoft.com/en-us/azure/architecture/best-practices/transient-faults):
  account for total latency, avoid layered immediate retries, and verify repeated
  operations' effects. Its cloud-specific intervals are not imported into this
  local event-driven hook.
- [Python subprocess](https://docs.python.org/3/library/subprocess.html#subprocess.run):
  a timed-out `run` terminates and waits for its child before raising; checked
  completion exposes failure. Process creation itself may exceed the timeout.
- [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html):
  transaction completion depends on the actual commit boundary. Bootstrap already
  publishes through the project's recoverable Markdown transaction API; retry
  does not replace that API with direct production file writes.

Python 3.12.3 and SQLite 3.45.1 are installed; no new dependency, version-specific
API or runtime location is introduced. Existing bootstrap files remain untouched.
The scope is projects already carrying state.md and a recognized marker, not every
visited folder. Qualification must cover both handoff branches, retained state,
completed-bootstrap no-op, ordinary integrations and measured complexity.

## Publication race discovered during qualification

The helper's early existence check alone cannot preserve a page another process
creates before the child writes. A regression through the actual automatic helper
and real transaction writer demonstrates the old overwrite. Four more probes
demonstrate that `bootstrap` reports Written for noncommitted transaction states.

Automatic calls therefore use a new `--if-missing` option. The child skips a page
already present, and binds target absence through the existing transaction
preconditions to protect the later race. Manual `--apply` retains its explicit
refresh behavior. Both modes verify `state == committed` before reporting Written.
A lost race is reported through the protected diagnostic sink, not as a completed
write. This uses the existing ABSENT/hash contract and adds no persisted format.
The API tests also cover creation, repeated no-op and deliberate manual refresh.
The alternative of checking existence twice without a commit precondition leaves
the same race open; rewriting an existing accepted page on automatic retry is not
acceptable.

## Results

Both original next-session regressions failed; seven controls passed. Initial
retry qualification passed 169 checks with 21 platform skips. A production
bootstrap dry-run on this checkout completed in 0.180 seconds with no stderr;
this is one observation, not a latency distribution or an installed write.

Publication probes then demonstrated the overwrite and four false Written
responses; two additional tests initially failed because the new conditional
creation API did not yet exist. After correction, 27 focused/complexity checks
passed, and a real CLI process independently preserved an existing bootstrap.
Writer integration caught a scanner-contract regression after extracting the
mutation into a helper (one failure, 56 passes). The mutation was returned to
the existing `bootstrap` entrypoint; the scanner and its expected inventory were
not weakened or changed.

The final selected regression run passed 237 tests, with 21 platform skips and
three deselected unrelated stress/process cases, in 86.72 seconds. Ruff and the
actual Lizard/AST complexity gate passed. Four DeprecationWarnings remain from
an existing process-pool test using fork after threads; they are preserved and
tracked separately. This run is not the whole project suite. Proof prefixes:
`logs/audit-2026-09-29-completed-repair-bootstrap-retry-` and
`logs/audit-2026-09-29-completed-repair-bootstrap-publication-`.

Follow-up: the four fork warnings were subsequently corrected across the writer
test module. Its complete strict run, together with bootstrap publication, hook
diagnostics and complexity, passed 156 tests without warnings; see
`2026-09-29-writer-tests-start-fresh-processes.md`. The original warning-bearing
result remains retained above.

The obsolete no-retry branch and unchecked-success path are replaced, with no
parallel implementation retained. Private progress/log synchronization remains
deferred until the live compiler releases its snapshot.
