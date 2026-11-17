# The next module: managing the queue's child execution

2026-11-02. Based on f44e5a6b. The working session of that branch records the user's answer
"Yes, implement this boundary" (translated from Russian; the repository holds no copy). The decision and STRUCTURE were recorded before the implementation.

## The concrete boundary

Add `scripts/llm_wiki/queue_child_runtime.py`, its own tests
`tests/modules/queue_child_runtime/`, a description and a full manifest
`docs/modules/queue-child-runtime.md` / `queue-child-runtime-context.json`.
The module owns the spawn lifecycle, the Pipe handshake, receiving/decoding the answer,
the deadline/cancellation/lease loss checks and the call to the already extracted cleanup.

Move the existing `_encode_processor_outcome`, `_send_processor_frame`,
`_PROCESSOR_BOOLEAN_FRAMES`, `_decode_deferred_frame`, `_processor_frame_failure`,
`_decode_processor_frame`, `_ChildRun`, `CHILD_LEASE_CHECK_SECONDS`,
`_await_child_message`, `_stop_child_if_done_waiting`, `_child_stop_reason`,
`_may_keep_waiting`, `_child_ready_handshake`, `_abandon_oversize_child`,
`_child_result_frame`, `_child_left_in_time`, `_settle_child_exit`,
`_require_child_start` and the implementation of `_run_processor_child`.
Their existing definitions take 1900 o200k_base tokens, not the full package.

The shared existing `_MAX_RESULT_BYTES` (16 MiB) gets a single owner
in the existing queue_contracts.py; the queue and the transport import it from there.
The value and the current limits stay the same. It is the shared limit on storing
and transferring a result, not a copy of the constant in each module. Checks that
change internal values with monkeypatch must patch the actual consumer;
add no redirection of globals between modules.

## How to rule out the reverse dependency

`_record_processor_failure`, `_processor_result_frame` and `_processor_child_entry`
stay in memory_queue. The last one keeps its pickle/import path, the redirection of
stdout, the creation of a POSIX session and diagnostics. It uses the new owner's imported
encoder/sender helpers. `_run_processor_inline` also stays.

In the new module the function `run_processor_child` takes a mandatory keyword-only
`child_entry`: the existing top-level function with the contract `(sender, processor,
task)`. It is the target for multiprocessing.Process, not a callback registry.
The old memory_queue._run_processor_child with the old signature becomes a thin
call of this function with `_processor_child_entry`. It contains no second implementation
of spawn/cleanup. This keeps the runner identity used by `_interruptible`
and the current calls/parameters. No import cycle arises: the transport receives the target
from the caller and does not import the executor or capture diagnostics.

Its own tests can run a real spawn with a test top-level entrypoint
without loading queue/state/diagnostics. Integration tests must separately pass
with the queue's real entrypoint: their context is not hidden behind this interface.
Old internal names are kept as direct aliases only where consumers
are confirmed. Do not add wrappers for names nobody needs.

## Observed reasons and preserved properties

The built-in graph of the current base a30aed31 and the sources showed: moving the whole
child execution together with `_record_processor_failure` pulls in capture_diagnostics,
memory_state and operational diagnostics. These dependencies cannot be called
an isolated transport. A slice with an explicit entrypoint leaves them with the execution
policy. During the research, a dependency of the
QueueOperationError classification on the presence of memory_queue in sys.modules was found and fixed separately
(f44e5a6b); that is not the implementation of this new module.

Preserved: the R/A handshake after the task completes, tracking descendants until the ACK,
the ability to accept an answer at the deadline boundary, oversized/malformed errors,
stopping on cancellation/lease loss, closing the Pipe, startup and
cleanup exceptions. Timeout/interval values do not change. The existing phrase in the docstring
of _ChildRun.stop saying that an explicit None means there are no descendants is wrong:
cleanup treats it as an unknown tree. During the move, correct the text without
changing the branches, and confirm the None/empty/sentinel distinction with tests.

SQL, queue states, the heartbeat thread, data and the runtime layout do not change.
No daemon, no new dependencies, and no installation of the whole candidate branch into the live vault.
Behaviour preservation is confirmed by tests, not by matching lines alone:
the mandatory child_entry and the thin old entry are explicit changes of
structure, so full AST identity for the whole slice is not promised.

## Alternatives and primary sources

Checked 2026-11-02:

- [Python multiprocessing](https://docs.python.org/3.10/library/multiprocessing.html):
  spawn requires importable objects; recv_bytes limits the whole message,
  and exceeding it makes the connection unreadable. Spawn and bounded bytes are kept,
  without replacing them with pickle recv. The 3.10 page is marked upstream unsupported;
  the project's compatibility floor is kept and checked separately.
- [Microsoft Pipe inheritance](https://learn.microsoft.com/en-us/windows/win32/ipc/pipe-handle-inheritance):
  an extra open copy of the writer handle affects observing the end of the stream.
  The closing order of the parent/child ends is kept. This is the basis of a check,
  not a promise that an anonymous Win32 Pipe and the multiprocessing transport are identical.
- [Cockburn, Ports & Adapters](https://alistair.cockburn.us/hexagonal-architecture):
  separating the interaction contract from the external implementation lets the
  component be checked on its own. This is the original 2005 work, not a new technology.
  Here one target parameter is enough, without a framework or service layer.

Alternatives: moving the whole runner leaves heavy dependencies; moving only the
codec barely separates the lifecycle; removing diagnostics loses evidence;
a global registry/dynamic executor lookup hides the dependency. The chosen option is
a narrow entrypoint parameter with the old adapter, because it keeps the spawn ABI
and the diagnostic policy while the transport is imported independently.

## Acceptance after approval

Record the decision and STRUCTURE before the code. Pin the original wire bytes, defaults,
errors and the existing integration tests. Check the isolated import and a real
spawn without the executor, identity/default runner consumers, all the old worker/compile
handoff, noisy stdout, grandchild, deadline-answer, lease loss, cancellation,
startup death, result limits and diagnostic trail scenarios. Measure complexity,
CI discovery/weights, Python 3.10 and the full maintenance packet ≤100000 tokens.
The package includes its own sources/tests/data, instructions, documents and
the required dependency contracts. Do not claim native Windows/macOS based on Linux.
Updating the index and cleaning up the replaced implementations after qualification are required.

Evidence: logs/queue-child-boundary-graph.json;
logs/queue-child-runtime-inventory.json; scripts/memory_queue.py;
tests/test_an_answer_that_landed_at_the_deadline_is_kept.py;
tests/test_a_childs_tree_is_read_when_it_is_killed.py;
tests/test_queue_caller_cancellation.py; tests/test_compile_worker_handoff.py.

## Implementation and verification

The decision queue-child-runtime-decision-20261102 and STRUCTURE were recorded before the code.
15 helper functions kept their AST. ChildRun differs only in the corrected
documentation. The runner matched by AST after the explicitly planned changes of name,
signature and target. The remaining executor matched after excluding imports, the adapter
and the moved definitions. The old adapter signature is kept. The 20 contract
types are unchanged; only the old value of the shared result limit was added to queue_contracts.
Seven definitions of the old deadline test were kept after
replacing the module reference; they now live in tests/modules/queue_child_runtime/.

The boundary guard first detected an executor import in the moved test, then
passed. Fresh-interpreter tests run a real spawn with boolean/deferred
answers without queue/state/diagnostics in both processes. A separate guard rejects
a repeated declaration of the result limit in the transport and was checked against a shadowed import.
Own package: **40 passed** on Python 3.12 and **40 passed** on Python 3.10.

The first extended run found two required cleanup aliases removed by an autofix:
**1 failed, 447 passed**; on 3.10 the integration gave **1 failed, 99 passed**.
The aliases were restored as explicit re-exports of the same objects; assertions were not
weakened. A repeat of the whole selected extended set: **448 passed**, 133.69 s.
Two late checks protecting the shared limit passed in the module's own package separately
from the collection of this extended run. A repeat of the Python 3.10 integration:
**100 passed**, 54.24 s. The original selection before the move:35passed,24.97s.

The extended selection includes queue/CLI/races/targeted worker, real processes
and descendants, loss/cancellation, startup death, compile worker/foreground/parent-lock/
leased-entrypoint/completion, diagnostic trail, source complexity, structure and CI.
The maximum measured CCN of the new runtime is 5. Ruff passed. The CI weights of the new
files were taken from JUnit; the weight of the old deadline file was removed.

Eight budget packets were checked:72passed/265deselected. The size before this report was
88372tokens/34files; the final size is measured after the results are recorded.
memory_queue is now130277tokens, the new runtime2026tokens. This is not yet the completion of
splitting the queue or the whole project. A full regression of the whole new HEAD,
native Windows/macOS, remote CI and installation are not claimed.

Evidence: logs/queue-child-preservation.json; logs/queue-child-boundary-red.txt;
logs/queue-child-own-final.txt; logs/queue-child-python310-own-final.txt;
logs/queue-child-related.txt; logs/queue-child-final.txt and .xml;
logs/queue-child-python310-related.txt; logs/queue-child-python310-final.txt;
logs/queue-child-budgets.txt; logs/queue-child-complexity.json;
logs/queue-child-source-counts.json.
