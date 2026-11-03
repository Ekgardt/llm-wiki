# Durable compile call lifecycle without compiler orchestration

Date: 2026-11-03. Internal boundary refinement under the completion mandate.
After saved-answer representation was separated, memory_queue still imports the
large compile_controller for resolve_model_call at settlement and operator recovery.
The lifecycle already uses an explicit queue object and a supplied protected invoke
callback; it does not need to import the queue or choose a provider.

Move 25 existing definitions (23 functions and two classes) unchanged to
scripts/llm_wiki/compile_call_runtime.py: ProviderOutcomeUnknown, CompileDelivery,
_require_checkpoint, _dispatch_event, _resume_call, _invoke_and_save, run_model_call,
_block_unknown_call, _run_reserved_call, _repeat_checkpoint,
_require_authorization_text, _require_repeat_request, authorize_model_repeat,
_recorded_repeat, _authorize_uncertain_call, resolve_model_call,
_retained_dispatch_checkpoint, _checkpoint_before_dispatch, _resolution_repeat,
_retained_repeat_authorization, _require_resolution_consent, _resolve_or_authorize,
_resolve_unknown_call, _resume_recorded_call and _resume_historical_call.

Keep the controller's actual aliases: both classes, run_model_call,
authorize_model_repeat, resolve_model_call, _require_checkpoint and _dispatch_event.
The last helper is used by existing checkpoint tests; private duplication is not
needed. Retain both class __module__ identities. The two queue resolver imports
point directly to the lifecycle owner. Semantic CompileReplay, protected candidate
preparation, dependency capture, compiler frames, publication and CLI approval
remain where they are. This removes the remaining direct queue-to-controller
imports; the queue's separate compile_memory/flush/Markdown composition edges
still need later work. No complete queue isolation is claimed.

The queue remains the one SQL/lease/lineage/artifact owner. Lifecycle reservation
precedes invocation, saved answers are verified before replay, and missing durable
evidence retains uncertainty. A callback must use the existing protected transport;
this internal API does not establish DLP itself or prove operator consent from
an actor/reason string. Repeated delivery is not permission to repeat paid work.
No SQL transaction is introduced around the provider callback. No new retry,
timeout, schema, database, process, protocol or runtime path is added.

Three independent primary publishers checked 2026-11-03:
- SQLite transaction boundaries and one-writer semantics:
  https://www.sqlite.org/lang_transaction.html
- AWS Builders' Library, idempotency, caller intent and changed request parameters:
  https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/
- Temporal replay checks commands against retained event history:
  https://docs.temporal.io/workflow-execution
These do not give this product provider-side idempotency or exactly-once execution.
No AWS/Temporal package or service is introduced. Existing Python >=3.10 remains
the product requirement; no dependency or vendor CLI version changes.

Alternatives: callback injection merely to avoid importing a resolver would leave
its dependency ownership obscure; moving SQL methods into a mixin would split
transaction authority; duplicating recovery in the queue risks inconsistent consent
and uncertainty. Prefer the existing cohesive call lifecycle and explicit queue
object with one implementation, then verify the full source-complete packet fits.
If it does not, refine a real responsibility rather than waive closure or tests.

Before transfer: fresh built-in graph, actual callers/patch points, original
checkpoint/dispatch/repeat fixtures and related baseline. Exact AST comparison
must cover all moved definitions, retained controller definitions and the two queue
import changes. Unit tests cover identity/authorization values and delivery scope;
existing real SQLite, source-overlap, os._exit/restart, protected-dispatch, heartbeat
and operator-resolution tests remain integration gates with all assertions.
Run actual CCN/shape, both Python versions, full packet/ownership checks and exact
round-trip measurement. Native platform and full-cycle model-cost evidence remain
separate. No installation into the live vault is authorized.

Follow-up source inspection found one relevant monkeypatch at
`test_recovery_cli_consent_releases_work_without_calling_provider`: it patches
`compile_controller._invoke_and_save`. Move that test's patch to the new owner,
keeping all assertions and its failure callback. Retaining a disconnected alias
would make the no-provider-call check ineffective. Other controller helper
consumers continue through the listed aliases. The delivery-scope history record
(uncertainty belongs to the call; blocking happens at exit) is necessary context
in addition to the four saved-answer records and was read in full. All are shared
historical evidence, not claims of current platform or performance qualification.

Measured first full packet: 100,751 tokens, refused by the unchanged gate.
Reformatting the complete compile-progress history catalog as one JSON record per
line saves 1,428 tokens (9,293 to 7,865), with decoded JSON values exactly equal.
All 86 records, labels, byte boundaries and hashes remain; the 380,666-byte source
history and its digest are unchanged. No record or test is omitted to fit.
The controller's answer codec helpers now remain only where observed consumers
need them (_answer_event and _encoded_answer in corruption tests); unused decoder
and call-binding imports are removed, with no duplicate or dead adapter retained.

Validation: the pre-transfer related baseline passed 219 tests. All 25 moved
definitions and 265 retained controller definitions have identical ASTs; the queue
AST differs only in two imports. Three missing-boundary tests failed before code.
The first new suite exposed an incorrect test constructor and the 100,751-token
packet; neither was hidden. The corrected 23 owner tests and real integration set
passed 267 tests on Python 3.12 and 3.10 (47.09 s / 48.35 s). Actual changed-function
CCN/shape and Ruff pass. A consumer-import check initially had CCN 7; splitting
its source-query helper preserved every assertion and brought it within 5.
Native CI is running at b5cee495, not this extraction; reported fixture encoding,
installer, dependency-audit and secret-scanner findings remain separate open work.

The global qualification set passed 2,070 tests with 2 skips; 257 final checks
passed. All 80 declared packets were serialized and restored with exact paths and
bytes. None exceeds the unchanged limit. Whole-project completion remains open.
