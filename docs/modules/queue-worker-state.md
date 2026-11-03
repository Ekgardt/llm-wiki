# Queue worker values and progress

`llm_wiki.queue_worker_state` owns QueueRetryPolicy, _ProcessorOutcome,
_WorkerProgress.advance and processor_error_code with their existing constants.
The first transfer preserves all bodies, annotations, defaults and semantics.
Only stdlib and lower contracts are imported; no queue execution, SQL, filesystem
or clocks. Retry decisions, worker sleeps/deadlines and settlement remain with
callers. The capture failure helper consumes the lower reason converter directly.

Shared canonical contract: [Queue values](queue-contracts.md).
Shared canonical contract: [Reliability defaults](reliability-defaults.md).

Root aliases preserve all existing names used by callers. Classes, the public
function and the progress method retain serialization identities. Compatibility
checks and original golden bytes are retained in tests/test_worker_text_compatibility.py;
that integration test is outside the pure packet. Unit cases run against the old
owner before import retargeting. Guard tests prevent reverse imports and duplicate
bodies; all original worker/capture tests remain unchanged.

The existing generic reason converter has a known byte/character defect: its
64-character slice can exceed the schema's 64-byte ceiling for Unicode type names.
This extraction does not fix or hide it; the separate reproduced correction is
tracked in the byte-bound research. Explicit QueueOperationError codes preserve
their existing contract. No universal exception sanitization claim is made.

Run `pytest tests/modules/queue_worker_state tests/test_worker_text_compatibility.py
 tests/test_a_dead_task_names_its_reason.py tests/test_memory_queue.py -q`.
The complete packet includes exact sources, lower guides, unit tests, golden
fixtures, instructions, research and lossless measurement code.
