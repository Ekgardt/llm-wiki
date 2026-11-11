# Compile failure producers must fit the source-failure contract

Date: 2026-11-03. Investigation during the separate processor-reason correction.
Implemented separately; the queue worker correction did not fix this caller.

`_record_failed_batch` sends an exception type name to
`_record_compile_source_failures`. The latter slices to 200 characters before
calling `record_source_failure`. Since 2026-09-18, that API correctly refuses
anything outside 1..64 UTF-8 bytes, matching the adopted v3 schema. The caller
still carries the superseded bound. Even an ASCII type name of 80 characters
fails, as do 80 two-, three- or four-byte characters. Read-only validator evidence:
logs/compile-source-error-bound-baseline.json. A real producer-to-queue regression
must fail before changing production code; a validator demonstration alone is
not an end-to-end reproduction.

Correction: reuse the strict `llm_wiki.utf8_text._utf8_prefix` primitive
and the source-failure API's existing `MAX_SOURCE_FAILURE_CODE_BYTES` constant.
Keep an initial character bound to avoid encoding an unbounded type name. Normal
short type names and existing source identity/producer/deadline behavior remain.
Do not relax queue validation, swallow storage errors, or treat a failed write as
success. Explicit empty/invalid reason inputs must retain their refusal behavior.
Test stored diagnostic text and keys on the real v3 API, including boundary lengths,
Unicode and ordinary ASCII; keep preview and storage-failure propagation tests.

Primary sources checked for the same byte/character mismatch on 2026-11-03:
- https://www.rfc-editor.org/rfc/rfc3629.txt — variable-width UTF-8 and complete scalar boundaries.
- https://docs.python.org/3/library/codecs.html — strict codec errors and complete valid text.
- https://www.sqlite.org/lang_createtable.html#check_constraints — stored CHECK enforcement.
These are three independent publishers; none establishes an arbitrary new limit.
The 64-byte limit is the existing database/API contract, not a new design choice.

Alternatives rejected: revert the 2026-09-18 validator (restores SQL crashes),
expand the schema (unnecessary contract change), drop the diagnostic (loses
source failure evidence), duplicate a new prefix algorithm (two implementations).
Related history: 2026-09-18-a-refusal-is-cheaper-than-a-crash.md. The worker reason
converter has a different prefix/explicit-code contract; reuse only the byte
primitive, not its semantic reason conversion.

The real compiler producer was now invoked against a real temporary candidate
v3 queue, replacing only the factory selection with that queue. `ValueError`
was stored normally; 80-character ASCII/two-/three-/four-byte reasons all raised
`ValueError: source failure fields are invalid` and left the previous diagnostic.
Evidence: logs/compile-source-error-real-producer.json. No live runtime was used.


## Qualification

Four long-reason cases failed before production correction; eight valid-boundary
and refusal cases already passed. Afterwards, all 12 cases passed against real
v3 storage. Related compiler transactions, preview, resolution failure and queue
validation: 124 passed in 24.51 seconds. Python 3.10 focused qualification:
23 passed. Structure, test weighting and limit policy: 97 passed. Ruff import
ordering was corrected and then passed; actual whole-file CCN/shape analysis
passed for the changed compiler and new regression file. Existing tests were not
modified. No schema, retry or runtime installation changes were made.
Evidence: logs/compile-source-error-{red,green,py310,policy,weights}.txt.
