# A capture failure keeps its event identity

Checked 2026-09-30 against the installed Python 3.14 runtime and adapter.
A timed-out native delegate was logged with only its kind and timestamp.
The adapter had already normalized its event ID but discarded that context
when control reached its outer error handler. Nearby committed intents could
not establish which particular event survived. This is a diagnostic defect;
fixing it does not establish the cause of the timeout or recover old identities.

Three independent primary sources inform the small change:

- [OpenTelemetry log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)
  separates correlation identifiers from log bodies.
- [W3C Trace Context](https://www.w3.org/TR/trace-context/) explains propagation
  of an existing identity across component boundaries.
- [Python logging cookbook](https://docs.python.org/3.14/howto/logging-cookbook.html#adding-contextual-information-to-your-logging-output)
  describes attaching invocation context to diagnostic records.

Use the existing normalized event ID and session, carried in the invocation's
Namespace, and add the event ID to the existing JSONL record. No global state,
new logger, dependency, runtime path, or raw prompt/tool payload is introduced.
Existing session redaction and error classification remain. Calls that failed
before normalization and worker invocations have no invented event identity.
Timestamp matching was rejected because concurrent events are ambiguous;
wrapping exceptions was rejected because outcome classification uses their type.
A separate tracing service would add unnecessary deployment and retention work.
The tradeoff is that old records remain uncorrelated and IDs alone do not prove
successful publication.

Path: native normalization -> invocation context -> CLI failure handler ->
record_capture_failure -> existing JSONL trail and state counter. Normal capture
passes the same event ID in the delegate payload; capture_operation binds it
into the operation ID used by the breadcrumb intent. The existing state entry
retains source_event_id. Doctor still classifies the original error and does
not turn a timeout into a success based on the existence of an ID.

Regression: six normalized prompt/tool failures across Codex, Claude and
OpenCode failed on the old code with missing event_id. After the change, their
real isolated JSONL records retain the normalized ID, existing session prefix,
and lost outcome, without copying the prompt. The worker-label regression also
checks that a worker has no event/session identity. The test's OpenCode fixture
uses its actual sessionID field; the initial generic session_id-only fixture
was corrected, not accepted as a product failure. No old writer is retained.
