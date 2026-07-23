# LSP Cancellation Token Design

**Date:** 2026-07-23
**Status:** Approved for planning

## Decision

Replace the unreleased `LspProtocol.request(..., cancelled: Callable[[], bool])`
contract with an owned cooperative cancellation signal:

```python
source = CancellationSource()
result = protocol.request(
    method,
    params,
    deadline=deadline,
    cancellation=source.token,
)

# From the lifecycle owner or another thread:
source.cancel()
```

`CancellationSource` owns the authority to request cancellation.
`CancellationToken` is a read-only, sticky view of that state. The protocol never
executes caller-provided code while checking cancellation.

This is a clean contract replacement. There is no callable compatibility adapter
because the API has not shipped and such an adapter would preserve the blocking,
re-entrancy, and resource-exhaustion hazards being removed.

## Rationale

Python threads cannot be safely destroyed, stopped, or interrupted. Moving an
arbitrary cancellation predicate to a worker only relocates the problem: a stuck
predicate either leaks workers across generations, starves later requests, or
requires an unbounded number of threads. A process can be killed, but process
startup, serialization, IPC, and abrupt-termination failure modes are
disproportionate for observing one cancellation bit.

Modern cancellation APIs pass cooperative state rather than arbitrary polling
code. The source/token split also prevents the callee from clearing or initiating
the caller's cancellation signal.

## Scope

This change is limited to the owned synchronous LSP client and its immediate
callers. It does not:

- convert the MCP server or LSP transport to `asyncio` or AnyIO;
- add a runtime dependency;
- add a daemon, process, runtime path, or persistent state;
- attempt to forcibly stop Python threads;
- claim that `$/cancelRequest` forcibly stops a language server;
- add backward compatibility for the unreleased callable contract.

Other bounded synchronous operations may later accept the same token, but that is
not required to complete the current Python/Pyright slice. Immediate internal
adapters may pass `token.is_cancelled` only to already-existing trusted polling
APIs; they must not expose arbitrary callbacks at the LSP boundary.

## Public Contract

`CancellationSource` provides:

- a stable `token` property;
- an idempotent, thread-safe, non-blocking `cancel()` method;
- first-cancellation monotonic time for deterministic race resolution.

`CancellationToken` provides:

- `is_cancelled() -> bool`;
- `wait(timeout: float | None = None) -> bool`;
- read-only first-cancellation monotonic time;

The token does not expose `set()`, `clear()`, arbitrary callback registration, or
reset/reuse. One source represents one cancellation scope.

`LspProtocol.request` becomes:

```python
def request(
    self,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None = None,
) -> object:
    ...
```

`deadline` remains an absolute finite `time.monotonic()` timestamp. Caller
cancellation raises `RequestCancelled`; deadline expiry raises `TimeoutError`.
Peer JSON-RPC errors and transport failures retain their existing distinct types.

## Waiting Model

Each admitted request has one wake event for response dispatch, protocol failure,
and connection close. The request loop waits on that event in slices of at most
10 milliseconds and checks the token's sticky event between slices. A response
wakes immediately; cancellation observation latency is at most one 10 millisecond
slice plus scheduler delay. `CancellationSource.cancel()` performs one
`threading.Event.set()`, so its work is constant and it cannot execute user code.

The request loop always rechecks terminal state under the protocol state lock after
waking. It computes the remaining budget from the original absolute deadline and
never starts a new relative timeout.

No cancellation worker, callback queue, polling thread, or executor is created.

## State Machine

The logical states are:

```text
ADMITTED -> QUEUED -> SENDING -> SENT -> COMPLETED
                      |          |  \-> DRAIN_CANCELLED
                      |          \----> DRAIN_TIMED_OUT
                      \---------------> FAILED
```

The writer owns the `QUEUED -> SENDING -> SENT` transitions. The state lock
protects request identity, terminal outcome, cancellation emission, and response
dispatch. No protocol lock is held across a potentially blocking pipe operation.

## Race Semantics

Every cancellation source records `cancelled_at` using `time.monotonic()` before it
wakes waiters. The reader records `responded_at` when a complete validated response
is dispatched. The request deadline is already an absolute monotonic timestamp.

The earliest event wins:

1. caller cancellation;
2. deadline expiry;
3. validated response dispatch.

The ordering above is also the tie-break order when timestamps compare equal.
Bytes merely available in an OS pipe do not count as a response; dispatch of the
fully framed and validated response does.

Specific transitions:

- Pre-cancelled or already-expired requests allocate no ID, consume no pending
  slot, and write nothing.
- Cancellation while queued makes the writer skip the request. Since the request
  was not sent, no `$/cancelRequest` is emitted.
- Cancellation racing a write treats `SENDING` as sent. The original frame remains
  ordered before one cancellation notification.
- Cancellation after send wakes the caller immediately and enqueues exactly one
  best-effort `$/cancelRequest`; the caller never waits past the original deadline
  for that write.
- A response dispatched before cancellation and before the deadline wins even if
  the caller thread runs later.
- A response dispatched after cancellation or deadline is consumed and discarded.
- Repeated `cancel()` and repeated wakeups do not duplicate cancellation frames or
  caller completion.
- Transport failure atomically fails nonterminal requests. A request whose local
  cancellation or timeout already won keeps that caller-visible outcome.

## Wire Drain And Bounds

LSP 3.18 cancellation is advisory. The server must still return one response, but
it may ignore `$/cancelRequest` and may return success or partial results.

A sent request that is locally cancelled or timed out therefore becomes
drain-only. It remains correlated and charged against `MAX_PENDING_REQUESTS` until
one of these events:

- its response is consumed;
- the transport fails or closes;
- `CANCEL_DRAIN_GRACE_SECONDS = 2.0` expires and the owning generation is declared
  unhealthy for Task 6 restart.

Keeping drain-only requests charged prevents cancellation floods from creating
unbounded concurrent work inside Pyright. A bounded writer queue reserves control
capacity for cancellation notifications. Request IDs are not reused within one
protocol generation.

Task 4 owns correlation and bounded local state. Task 6 owns process restart and
process-tree reclamation after an unresponsive cancellation grace period.

## Error Handling

- Local caller cancellation: `RequestCancelled`.
- Local deadline: `TimeoutError`.
- Peer `-32800`: peer acknowledgement that client cancellation was detected.
- Peer `-32802`: server-initiated cancellation, retained as peer error metadata.
- Peer `-32801`: content modified, handled only by an explicit freshness retry
  policy in the higher layer.
- Reader, writer, framing, or close failure: connection-wide fatal transition.

Local cancellation does not fabricate a peer `-32800` response. A late peer
response cannot change an already returned local outcome.

## Verification

Tests must prove:

- source/token authority separation, stickiness, idempotence, and thread safety;
- no callback workers or cancellation-related thread growth;
- pre-cancel, queued-cancel, sending-cancel, and sent-cancel behavior;
- response/cancellation/deadline ordering with deterministic monotonic clocks;
- exactly one `$/cancelRequest` and request-before-cancel wire ordering;
- cancellation enqueue and caller return remain inside the original deadline;
- drain-only entries remain bounded and charged against the active wire limit;
- late success, late peer cancellation, transport failure, and close races;
- 32 simultaneous requests, cancellation flood, grace expiry, and generation
  restart handoff;
- unchanged hostile-frame, semantic-result, and cross-platform pipe tests.

There must be no test that treats failure to run an arbitrary predicate as an
acceptable cancellation outcome because arbitrary predicates are no longer part
of the contract.

## Alternatives Rejected

### Keep `Callable[[], bool]`

Rejected. It permits unbounded blocking, exceptions, re-entrancy, lock inversion,
side effects, and inconsistent observations inside the protocol lifecycle.

### Rewrite The Transport With `asyncio` Or AnyIO

Rejected for this slice. Structured cancellation is valuable for async-native
systems, but synchronous work remains cooperative and a sync facade requires an
event-loop bridge. The rewrite adds complexity without solving the arbitrary
callable problem and AnyIO would add a runtime dependency.

### Evaluate Predicates In Processes

Rejected. General callables are not reliably pickleable, especially under Windows
spawn. Dedicated processes add startup and IPC costs, while abrupt termination can
skip cleanup or damage IPC state. Process isolation remains appropriate for
genuinely untrusted or killable workloads, not cancellation observation.

## Current Sources

- Python 3.10.20 `threading`: threads cannot be destroyed, stopped, or
  interrupted; `Event` is the standard sticky cross-thread signal. Updated
  2026-03-11. <https://docs.python.org/3.10/library/threading.html#event-objects>
- Python 3.14.6 `concurrent.futures`: a running future cannot be cancelled, and a
  timeout bounds waiting rather than running work.
  <https://docs.python.org/3/library/concurrent.futures.html>
- LSP 3.18 cancellation: `$/cancelRequest` is advisory and the server still owes a
  response. <https://microsoft.github.io/language-server-protocol/specifications/lsp/3.18/specification/#cancelRequest>
- .NET cooperative cancellation, updated 2026-03-30: source/token authority split,
  polling, bounded wait handles, and fast registered callbacks.
  <https://learn.microsoft.com/en-us/dotnet/standard/threading/cancellation-in-managed-threads>
- AnyIO cancellation: threads require cooperation; cancel scopes carry state and
  deadlines. <https://anyio.readthedocs.io/en/stable/cancellation.html>
- Tokio Util 0.7.19 `CancellationToken`, released 2026-07-21: sticky state,
  `is_cancelled`, and a waitable cancellation future.
  <https://docs.rs/tokio-util/0.7.19/tokio_util/sync/struct.CancellationToken.html>
- Go `context`: request-scoped deadlines and cancellation signals propagate across
  API boundaries. <https://pkg.go.dev/context>
- `vscode-languageclient` 10.1.0 and `vscode-jsonrpc` 9.0.1: cancellation tokens
  trigger `$/cancelRequest`, while response correlation remains until a response or
  connection failure.
  <https://github.com/microsoft/vscode-languageserver-node/releases/tag/release/client/10.1.0>
