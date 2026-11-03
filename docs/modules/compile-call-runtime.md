# Durable model-call lifecycle

`llm_wiki.compile_call_runtime` owns the existing reservation, saved-answer replay,
uncertain-outcome handling and explicit-repeat lifecycle. Its queue object owns
SQL, live lease validation, source-overlap fences and artifact IO. Its supplied
invoke callback must use the protected transport; this module does not establish
DLP, provider identity, source independence or operator consent by itself.

Reservation precedes invocation. Missing durable evidence never authorizes an
automatic repeat. A ready checkpoint, request digest and retained dispatch bind
recovery. Existing repeat consent names one dispatch, actor, reason and request;
strings alone are not proof of consent. Resolution never invokes the provider or
settles the task. A recovered answer still needs semantic validation.

CompileDelivery is entered once. It remembers uncertainty even if a caller catches
the exception to process independent work, and blocks on exit. Successful work
leaves publication and completion to the caller. Only a renewed expiry can differ
when matching the same lease; real database checks still enforce ownership.
ProviderOutcomeUnknown and CompileDelivery retain their compile_controller class
identities; existing used controller aliases remain one implementation. The two
queue resolver imports now use this owner. Other queue/compile composition edges
remain unfinished. No new runtime store, dependency, retry or limit is introduced.

Tests pin pre-transfer dispatch/repeat events and exercise lifecycle ordering,
refusal, owner identity and uncertainty. Existing real-queue, crash/restart,
source-overlap, protected-dispatch and recovery CLI tests remain integration gates.
The recovery CLI test patches the new invocation owner so its forbidden-call trap
remains effective. Historical records are context, not current validation claims.

Source: ../research/2026-11-03-compile-call-runtime-boundary.md.
Shared canonical contract: [Saved answers](compile-answer-records.md).
Shared canonical contract: [Compile progress](compile-progress.md).
Shared canonical contract: [Queue value contracts](queue-contracts.md).
