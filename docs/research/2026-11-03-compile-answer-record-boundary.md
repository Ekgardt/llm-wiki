# Saved compile answer representation

Date: 2026-11-03. Proposed next boundary after model_records qualification.
The queue's retained-answer verifier currently imports compile_controller for
_decoded_answer and _require_same_call. These functions only need checkpoint
values, the existing answer encoding and the model value records. Their owner
can be independent of provider transport, queue SQL and the controller.

Move the 17 existing representation/binding functions (_answer_event,
_call_binding, _encoded_answer, _require_response, _decoded_answer,
_require_answer_binding, _require_same_call, _decoded_response,
_require_completed_result, _require_result_text, _require_completed_refusal,
_result_document, _count_document, _encoded_candidate_result,
_decoded_candidate_result, _result_from_document, _count_from_document) plus
_COMPLETED_REFUSALS into scripts/llm_wiki/compile_answer_records.py.
Their ASTs, exception classes/messages, canonical bytes, schema names, fields,
null semantics and nominal model classes stay unchanged. The controller keeps
only direct aliases required by its callers, and the queue verifier imports
the representation owner directly. This removes that specific reverse edge.

Resolution authorization, transport, delivery scope, SQL ownership fences,
artifact IO and settlement remain with their current owners. In particular,
settle_compile_resolution still calls the controller: removing this one edge
does not resolve the entire queue/compiler cycle. A stored answer never by itself
means a validated semantic compile result or permission to repeat a provider call.

Three independent primary publishers checked today:
- IETF RFC 8259: JSON representation and interoperable value domain.
  https://www.rfc-editor.org/rfc/rfc8259
- Python 3.10 dataclasses: exact nominal equality, replacement and field semantics.
  https://docs.python.org/3.10/library/dataclasses.html
- Cockburn's original ports/adapters article: separate operations from external
  dependencies and test leaks. https://alistair.cockburn.us/hexagonal-architecture
The product's canonical JSON subset is narrower than general JSON and is retained,
not claimed to be imposed by the RFC. No new schema engine or dependency is added.
Python >=3.10 remains the unchanged product contract.

Alternatives: duplicate a smaller decoder in the queue risks validation drift;
load the controller by string hides the dependency; callbacks for every record
field obscure deterministic validation. Prefer a single full implementation
with source-complete dependencies on compile_progress, model_records,
context_budget and canonical formats. No closure exception is allowed.

Qualification must compare pre-transfer encoded/decoded fixtures and malformed
record refusals, prove exact moved and retained ASTs, exercise dispatch binding
including allowed delivery changes and rejected call-identity changes, preserve
real queue recovery/uncertain-outcome tests and measure the complete packet.
The isolated import must forbid controller, client, queue and Markdown runtime.
Native platforms and full-cycle model cost remain separate evidence requirements.

Four relevant historical records were read whole through the existing verified
compile-progress catalog: durable model-call controller, explicit repeat decision,
cross-work source uncertainty, and distinction between resolution and repeat
consent. Include them as shared context; their broader execution/authorization
history is not reassigned to the codec. Existing canonical encoding normalizes
text to NFC. Golden fixtures preserve both the original input and decoded value;
no byte-preserving Unicode claim is substituted for that current behavior.

## Qualification

Fresh built-in index f238153c / generation-18e47a45220dc26c-3719f038 found
4/5/2/4 callers for decoded answer, same-call check, encoded answer and answer event;
partial graph coverage was supplemented by source searches. Four boundary tests
failed before the transfer. All 17 function ASTs and 290 retained controller
definitions match; the entire queue AST differs only in the intended import.
Seven golden encodings and eight original refusals are retained from before the
transfer. Initial fixture capture incorrectly assumed decomposed Unicode would
round-trip unchanged; the existing NFC codec was inspected and the fixture now
records original input and actual decoded output, without changing production.

The first new boundary-test function measured CCN6. Extracting its named-function
query preserved every assertion and brought actual CCN/shape into compliance.
All moved functions, the changed queue method and new tests pass actual analysis;
Ruff passes. The initial owner selection passed 36 tests. Expanded real queue,
controller, protected dispatch, delivery, handoff, hardening and refresh checks
passed 243 tests on Python 3.12 and 243 on Python 3.10. The global module, structure,
ownership and policy selection passed 1956 tests with 2 skips. All 79 packets
reconstruct every path and byte and fit the ceiling; the answer packet was 98899
tokens before this evidence paragraph, so subsequent work must remeasure headroom.

Inventory remains incomplete: 661 of 3355 tracked files owned, 2694 unassigned.
The full db6baf57 regression is still running and predates both production transfers.
No native Windows/macOS or live-install success is claimed. Private evidence:
logs/compile-answer-records-{red,green,py310,global}.txt,
compile-answer-records-transfer-proof.json, compile-answer-records-graph.json
and context-bundle-comparison.json. Packet size is not full-cycle LLM usage.
