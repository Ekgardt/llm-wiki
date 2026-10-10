# Refused history does not consume a new append's budget

Research and reproduction: 2026-10-03. Python compatibility remains 3.10;
operational SQLite retains rollback journals and synchronous=FULL.

The installed vault retained 64 precondition-failed breadcrumb transactions for
one intent. `_append_until_committed` started at ordinal zero on every call,
counted those historical refusals against its fixed 64-candidate loop and never
reached a fresh candidate. A real temporary vault reproduces the failure by
preparing each append, allowing an external editor to change the file, and
applying through the unchanged coordinator. No transaction state is fabricated.

The repair removes the candidate-count cutoff. Existing caller deadlines,
cancellation, stalled-attempt detection, canonical ownership, content guards,
file hashes and quarantine records remain in force. Successful retry must
preserve the external edits, append once and provide committed lineage evidence.
Historical refusals stay available; they are not deleted or marked successful.

Alternatives considered: increasing 64 merely postpones the same defect; clearing
history destroys evidence; restarting ordinals loses idempotent identities;
a new retry setting adds an unnecessary contract. Retaining the existing elapsed
budget and cancellation avoids each of these changes. With no caller deadline,
the existing stall guard stops a candidate that does not settle; continuously
advancing external contention still requires caller cancellation or a deadline.
This change does not claim to solve every contention or throughput problem.

Primary sources checked on the research date:

- [SQLite isolation](https://www.sqlite.org/isolation.html): separate connections
  observe committed transactions; rollback-mode writes remain serialized. No WAL
  or isolation weakening is needed for application-level retry.
- [Python 3.10 time.monotonic](https://docs.python.org/3.10/library/time.html#time.monotonic):
  elapsed budgets use a clock unaffected by wall-clock changes.
- [RFC 9110 section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2):
  repeated requests require known idempotent semantics or evidence of prior
  application. This is a semantic comparison, not an HTTP requirement on the
  local coordinator; its persisted identities and verified hashes provide proof.

Qualification results and installed verification are recorded separately in the
private audit log. A passing temporary-vault test alone does not certify live
queue completion, native host activation or the entire audit.
