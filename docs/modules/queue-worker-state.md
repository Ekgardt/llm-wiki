# Queue worker values and progress

`llm_wiki.queue_worker_state` owns QueueRetryPolicy, ProcessorOutcome,
WorkerProgress.advance and processor_error_code with their existing constants.
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

Generic reason codes obey the existing 64-byte schema ceiling through the shared
strict UTF-8 prefix. Complete Unicode characters are preserved; ASCII codes remain
unchanged. Explicit QueueOperationError codes retain their existing contract.
Regression cases cover two-, three- and four-byte type names and real v3 retry
and terminal failure settlement. No universal exception sanitization claim is made.

Shared canonical contract: [Strict UTF-8 prefixes](utf8-text.md).

Run `pytest tests/modules/queue_worker_state tests/test_worker_text_compatibility.py
 tests/test_a_dead_task_names_its_reason.py tests/test_memory_queue.py -q`.
The complete packet includes exact sources, lower guides, unit tests, golden
fixtures, instructions, research and lossless measurement code.

## Structural reference retained from the overview

### Queue worker values

`llm_wiki.queue_worker_state` owns retry configuration values, processor
outcomes, progress counters and reason-code conversion. Queue SQL, clocks and
execution remain with current callers. Capture consumes the reason helper
directly, and root aliases retain compatibility. See
[research](../research/2026-11-03-queue-worker-state-boundary.md).

### Processor reason byte bound

Generic worker reason codes use the shared strict UTF-8 prefix to obey the
existing 64-byte SQLite constraint. ASCII and explicit QueueOperationError codes,
retry policy and schemas stay unchanged. The reproduced failure and correction
are separate from the preceding transfer; see
[research](../research/2026-11-03-processor-error-code-byte-bound.md).
