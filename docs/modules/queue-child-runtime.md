# Queue child runtime

Approved 2026-11-02. `llm_wiki.queue_child_runtime` owns child process lifecycle
and the bounded byte protocol independently of queue storage and capture policy.
Dependencies are stdlib, queue_contracts and queue_process_cleanup. It imports
neither memory_queue, memory_state nor capture diagnostics.

## Boundary and contract

`run_processor_child(processor, task, timeout, lease_lost=None, cancelled=None,
*, child_entry)` launches the explicitly supplied top-level entry using spawn.
The entry has `(sender, processor, task)` arguments, establishes its existing
process session, computes the task before sending R, waits for A, sends the
result frame and closes its sender. The caller must supply a spawn-importable
entry and processor. Cancellation and lease-loss callbacks stay in the parent.
The process is owned and settled by its creating parent. This is the existing
trusted local queue protocol, not a general arbitrary-process sandbox.

The module retains exact T/F/D/E/? frames, whole-frame rejection, result limits,
handshake ordering, deadline-answer race handling and cleanup exceptions. The
shared `_MAX_RESULT_BYTES` now has one definition in queue_contracts, preserving
16 MiB for transport and storage. Existing timing values are unchanged.
An oversized recv_bytes message leaves the connection unreadable; the existing
abandon/stop path is retained rather than attempting to reuse it.

memory_queue keeps `_processor_child_entry`, `_processor_result_frame` and
`_record_processor_failure`: stdout handling, POSIX session setup and redacted
failure evidence remain the executor's policy. Its old `_run_processor_child`
signature delegates once to this module with that unchanged entry. This adapter
also preserves the identity used by `_interruptible` to attach lease/cancellation
callbacks. There is no callback registry or dynamic lookup of executor globals.

Required historical names are direct aliases for actual existing consumers:
`_ChildRun`, `_await_child_message`, `_child_ready_handshake`,
`_decode_processor_frame`, `_settle_child_exit`, plus encoder/sender used by the
remaining executor. Test patches target the actual owner. The adapter remains
necessary while old callers and identity-sensitive runner selection use it;
retiring it requires migrating those contracts. There is only one lifecycle
implementation. Process entry serialization still resolves the old queue path,
so isolated production serialization is not claimed.

`_ChildRun.stop` discovers descendants when its own tracking is None, and passes
a known empty set explicitly. The cleanup owner's explicit None means unknown,
not empty. A misleading old docstring is corrected; branching is unchanged.

## Maintenance packet and qualification

The context manifest counts complete files: implementation, own tests/data,
dependency implementation and guides, shared instructions, structure and relevant
design/history. Its pinned o200k_base test enforces the user's 100,000-token
ceiling without truncation. This is a maintenance context, not measured end-to-end
LLM cost or proof that all of memory_queue is now below the ceiling.

```sh
PYTHONPATH=scripts uv run pytest --noconftest tests/modules/queue_child_runtime -q -s
```

Owned tests cover byte frames, exact result ceiling, malformed replies, ACK,
pre-start cancellation/deadline, tracking semantics and actual deadline races.
Fresh-interpreter real spawn tests exercise boolean/deferred replies with no
executor/state/diagnostics in either process. Their entry is a test entry;
production queue/compile/diagnostic integration is additional mandatory coverage.
The original deadline test's seven function/class definitions retain their AST
after changing the module reference; assertions and behavior remain.

Fifteen helper function ASTs are unchanged from f44e5a6b. ChildRun differs only
in its corrected docstring. The lifecycle runner intentionally gains a name and
required child_entry argument and changes its Process target to that argument;
the queue adapter preserves its previous signature. Other runtime logic is
unchanged. See `docs/research/2026-11-02-queue-child-runtime-boundary.md` for
approval, alternatives, primary sources and validation results. Local evidence:
logs/queue-child-*. Native Windows/macOS and installation are not inferred from
Linux tests. The installed vault remains separate from candidate qualification.
