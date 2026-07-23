# LSP Cancellation Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unsafe arbitrary LSP cancellation callbacks with a bounded, zero-dependency source/token signal while preserving strict deadlines and LSP wire cancellation.

**Architecture:** Keep the synchronous reader/writer transport. Add a private `threading.Event`-backed state shared by a cancellation source and read-only token, and let requests check that sticky state between bounded waits. Sent cancelled requests remain drain-only and charged against the wire limit until response, transport failure, or Task 6 restart handoff.

**Tech Stack:** Python 3.10, `threading`, LSP 3.18 JSON-RPC, pytest.

---

### Task 1: Replace Callable Cancellation With Owned Tokens

**Files:**
- Modify: `scripts/lsp_protocol.py`
- Modify: `tests/test_lsp_protocol.py`
- Modify: `docs/superpowers/plans/2026-07-22-python-pyright-navigation.md`

- [ ] **Step 1: Write failing token authority and lifecycle tests**

Add tests proving this public contract:

```python
def test_cancellation_source_exposes_read_only_sticky_token() -> None:
    source = CancellationSource()
    token = source.token
    assert token.is_cancelled() is False
    assert token.cancelled_at is None
    assert source.cancel() is True
    first = token.cancelled_at
    assert token.is_cancelled() is True
    assert token.wait(0) is True
    assert source.cancel() is False
    assert token.cancelled_at == first
    assert not hasattr(token, "cancel")
    assert not hasattr(token, "clear")
```

Also add deterministic tests for:

- pre-cancelled request allocates no ID, pending slot, or frame;
- cancellation after send returns `RequestCancelled` within 20 ms plus scheduler tolerance and emits exactly one ordered `$/cancelRequest`;
- cancellation workers and callback queues no longer exist and repeated protocol generations create no `lsp-cancel-*` threads;
- response dispatch before cancellation succeeds;
- cancellation timestamp before response dispatch wins even when the reader runs first;
- response at or after the absolute deadline cannot win;
- sent cancelled/timed-out requests remain drain-only and count toward 32 wire requests;
- a late response removes only its matching drain entry and cannot change the caller outcome;
- transport close/failure releases drain entries;
- each drain entry records a deadline exactly `CANCEL_DRAIN_GRACE_SECONDS = 2.0` after local cancellation/timeout for Task 6 health handling;
- cancellation racing queued and sending writes preserves request-before-cancel order and sends no cancel when the request was never written.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
uv run pytest tests/test_lsp_protocol.py -q
```

Expected: failures because `CancellationSource`, `CancellationToken`, token request parameters, and drain-only accounting are absent.

- [ ] **Step 3: Implement the source/token contract**

Implement in `scripts/lsp_protocol.py` without a new module or dependency:

```python
class _CancellationState:
    __slots__ = ("lock", "event", "cancelled_at")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.cancelled_at: float | None = None


class CancellationToken:
    __slots__ = ("__state",)

    def __init__(self, state: _CancellationState) -> None:
        self.__state = state

    @property
    def cancelled_at(self) -> float | None:
        with self.__state.lock:
            return self.__state.cancelled_at

    def is_cancelled(self) -> bool:
        return self.__state.event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.__state.event.wait(timeout)


class CancellationSource:
    __slots__ = ("__state", "__token")

    def __init__(self) -> None:
        self.__state = _CancellationState()
        self.__token = CancellationToken(self.__state)

    @property
    def token(self) -> CancellationToken:
        return self.__token

    def cancel(self) -> bool:
        with self.__state.lock:
            if self.__state.cancelled_at is not None:
                return False
            self.__state.cancelled_at = time.monotonic()
            self.__state.event.set()
            return True
```

`cancel()` records the first `time.monotonic()` value and sets one event. It returns `True` only for the first transition. Token methods execute no caller code and expose no mutation.

Change the exact request signature to:

```python
def request(
    self,
    method: str,
    params: object,
    *,
    deadline: float,
    cancellation: CancellationToken | None = None,
) -> object:
```

Remove `_CallbackTask`, `_CallbackWorker`, `_CALLBACK_SLOTS`, `_CALLBACK_THREADS`, `_submit_callback`, `_evaluate_cancelled`, and all callback-worker constants/imports/tests.

- [ ] **Step 4: Implement deterministic terminal ordering and drain-only state**

Store the request token, send phase, response timestamp, local terminal timestamp, one-shot cancel emission flag, and optional drain deadline in `PendingRequest`. Resolve response, cancellation, and deadline under `_state_lock` by monotonic timestamp with tie priority cancellation, deadline, response.

Wait on `pending.completed` for at most `min(remaining, 0.01)` when a token exists, then inspect the token. Never invoke user code. A local cancel or timeout wakes the caller immediately and enqueues one best-effort cancellation frame without waiting beyond the original deadline.

Keep sent locally-terminal requests in `_pending` as drain-only and charged against `MAX_PENDING_REQUESTS`. Remove them only on matching response or connection terminal state. Expose a bounded read-only helper used by Task 6:

```python
def expired_drain_keys(self, now: float) -> tuple[tuple[str, int], ...]:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("now must be a finite monotonic timestamp")
    with self._state_lock:
        return tuple(sorted(
            key
            for key, pending in self._pending.items()
            if pending.drain_deadline is not None and pending.drain_deadline <= now
        ))
```

The helper returns sorted keys whose two-second grace has expired and performs no restart or mutation.

- [ ] **Step 5: Update the main implementation plan**

In `docs/superpowers/plans/2026-07-22-python-pyright-navigation.md`:

- replace Task 4's `cancelled=None` request contract with `cancellation: CancellationToken | None = None`;
- add source/token, timestamp ordering, drain-only accounting, and two-second grace requirements;
- replace Task 5's `LspProcess.request(self, method, params, *, deadline, cancelled)` contract with `LspProcess.request(self, method, params, *, deadline, cancellation: CancellationToken | None = None)`;
- require Task 6 to restart a generation when `expired_drain_keys(time.monotonic())` is non-empty;
- replace later LSP-facing callable cancellation references with the token contract while leaving unrelated pre-existing repository helper callbacks unchanged.

- [ ] **Step 6: Verify Task 4 and compatibility suites**

Run:

```bash
uv run pytest tests/test_lsp_protocol.py tests/test_lsp_positions.py tests/test_lsp_paths.py -q
uv run ruff check scripts/lsp_protocol.py tests/test_lsp_protocol.py
git diff --check
```

Expected: all tests and Ruff pass; no `lsp-cancel-*` thread exists; only the three scoped files change.

- [ ] **Step 7: Commit**

```bash
git add scripts/lsp_protocol.py tests/test_lsp_protocol.py docs/superpowers/plans/2026-07-22-python-pyright-navigation.md
git commit -m "fix: use bounded LSP cancellation tokens"
```
