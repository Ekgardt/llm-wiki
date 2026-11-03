# Complete the existing queue argument-validation owner

2026-11-03; baseline016257e0; approved autonomous decomposition mandate.
Extend existing `llm_wiki.queue_validation` rather than creating another generic
utility module. Move the remaining pure capture descriptor/path/identity/fence
checks and their direct pure helpers from memory_queue. SQL, transaction and
connection ownership, current time, task-fence class identity, persisted-state
transitions, publication and the semantic-decision orchestration stay in the queue.
No validation is moved before or after a transaction boundary at its call site.

Exact function set (27):
_require_capture_intent_path, _require_lower_sha256,
_require_capture_intent_size, _require_capture_intent_descriptor,
_require_capture_decision_path, _capture_decision_relative_path,
_require_capture_terminal_path, _capture_terminal_disposition_fields,
_require_bounded_intent_path, _require_capture_identity, _require_capture_fence,
_require_semantic_decision_shape, _require_worker_intent_fence,
_require_matching_decision_owner, _require_repair_owner, _require_task_identifier,
_check_quarantine_names, _check_capture_intent_digest,
_check_capture_intent_relative_path, _check_capture_intent_size,
_capture_scan_cursor, _capture_cursor_text, _check_capture_intent_descriptor,
_check_ready_intent_identity, _check_resolution_reason,
_check_consumer_kind, _check_consumer_id.

Move the two private lookup constants used solely by these validators:
_CAPTURE_TERMINAL_DISPOSITION_FIELDS and _CONSUMER_KINDS. Preserve values and
returned set objects. _check_capture_intent_digest and _require_lower_sha256 have
identical bodies/signatures: retain the former as an alias of the latter rather
than a duplicate implementation. Other validation differences remain intentional:
publication versus pending-intent shape, exact int versus int-subclass acceptance,
messages and codes. Retain root helper aliases used by queue call sites; removal
requires moving those callers or explicitly retiring their compatibility surface.
The two private lookup names have no external source consumers and need no facade.

Dependencies are explicit lower owners: queue contracts, canonical formats and
payload limit, and restricted-relative-path validation. SQLite Row is only an
annotation; the owner must still never open/commit/rollback/close a connection or
read the current clock. Concrete TaskFence validation stays at the orchestration
boundary. Existing injected fence/owner types are passed through unchanged.

Primary sources checked 2026-11-03:
- OWASP distinguishes syntactic and semantic validation and early input checks:
  https://cheatsheetseries.owasp.org/cheatsheets/Input_Validation_Cheat_Sheet.html
- SQLite describes transaction lifetime and one-writer semantics:
  https://www.sqlite.org/lang_transaction.html
- Python 3.10 JSON documentation describes encoding choices and untrusted input:
  https://docs.python.org/3.10/library/json.html
Use these to preserve the existing separation and byte/hash contract, not to
change normalization, accepted inputs, isolation or limits. Python3.10 and all
dependencies stay unchanged. Alternatives: leave checks in the executor, create
one module per check, or centralize database orchestration with validation. The
existing cohesive validation owner is simpler and avoids a new runtime layer.

Baseline related queue/capture/adoption regressions:70 passed. Before code,
refresh and inspect the built-in graph plus direct dynamic/callback consumers.
Qualify exact moved ASTs (one documented duplicate alias), unchanged remaining
queue definitions, adversarial descriptor/fence tests, root alias compatibility,
existing integrations, actual CCN/shape, complete source/guide manifest coverage,
full serialized round-trip and Python3.10. Old duplicated implementations must
not remain. Full regression6780ef23 runs separately and predates this change.
