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

Implementation baseline is now7854a031 after the independent LSP fixture repair.
Fresh generation-18e46b1d294849a6-bf509fe1 indexes1851 sources/7145 chunks. Direct
callers are index_capture_intent_pending/mark_capture_intent_ready, publication,
pending/ready scans, and the retained semantic-fence orchestrator. Source search
finds no outside consumers of the two private lookup constants or fault-injection
patches on these helpers. Graph evidence: logs/queue-capture-validation-graph.json.

Implementation preserves26 function bodies exactly; the27th is the documented
identical digest alias. Both constant values and every remaining queue definition
are unchanged.36 new adversarial checks first passed against the original root
functions, then against their owner. The context guard reproduced the missing
four new source dependencies; their full files and owner guides are now declared.
The first ownership-guide run correctly refused the untracked new test file;
after staging it, all68 source/guide guards pass. No guard was bypassed.

Qualification:61 weighted owner tests,195 queue/capture/adoption integrations,
85 checks onPython3.10, and1123 complete module/global guards pass (2 platform
skips in the last group). Ruff and actual CCN/shape pass.48 emitted packets
round-trip byte-for-byte; validation84463 tokens before this paragraph, global
maximum96512. Overlapping totals3838219 serialized/4197692 raw. Registry380/3110
files,2730 unassigned. Full7854a031 remains running and predates this production
transfer; no current full pass or native platform qualification is claimed.

Whole-source search additionally found an existing independent path-builder body
in flush_memory, specialized to the flush stage. It is not a monkeypatch consumer
of the moved function. Consolidation into a call to the now-shared pure path
builder is the next source step after refresh; do not claim all duplicate cleanup
finished until that producer is updated and its capture-terminal tests pass.

Next cleanup, still under this boundary: flush_memory._capture_decision_relative_path
keeps its one-argument signature but delegates to queue_validation's canonical
builder with stage='flush'. The wrapper is needed by its two current producer
call sites; the duplicate canonical-JSON/hash body is not. All returned paths
must remain identical. Add a producer seam regression that fails while flush owns
a second algorithm, then run the existing18 terminal/redrive scenarios. The same
current canonical-format/validation research applies; no new API, runtime path,
normalization or dependency technology is introduced. Flush already calls the
queue in these scenarios, and the dependency is now on its pure lower owner.

Cleanup graph refreshed at2d4c57d0: generation-18e46bc68269f9c7-6c7d011f.
The producer has two call sites (_index_capture_decision and the capture-decision
reader); the shared validator retains its direct caller. Existing18 terminal and
redrive regressions passed after the preceding validator transfer.

Producer cleanup complete: the new seam regression failed against the duplicated
body, then passed after delegation.80 related cases pass onPython3.12 and3.10;
18 weighted terminal checks pass.141 relevant structure/source/guide/privacy/weight
guards pass,1 platform skip. Ruff and actual complexity/shape pass. The old terminal
test file is byte-identical up to the appended regression, and the wrapper keeps
its signature and flush stage. All48 complete artifacts still round-trip within
the cap. No full-current or native platform pass is claimed;7854a031 is still running.
