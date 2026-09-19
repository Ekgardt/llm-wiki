# One bounded tool path, and a busy answer that says so

Dated 2026-09-17. Audit 3, findings K-B4 and K-B5. The research before the change.

## What was found

- **Two boundaries for one call (B5).** `mcp_server._handle_tool_call` and the `call_tool`
  callback built inside `_register_tools` are twins: each computes its own operation deadline,
  each calls `_run_bounded`, each renders its own timeout answer. Production serves through
  `call_tool`; `tests/test_mcp_server.py::TestHandleToolCall` (59 tests) and
  `tests/test_impact_analysis.py` exercise `_handle_tool_call`. `build_server`'s own docstring
  warns about exactly this second-boundary hazard.
  The twins had already drifted: `_handle_tool_call` renders
  `_tool_timeout_envelope_text(name, arguments)` for every tool, while `call_tool` renders a
  `timeout_result` computed **once at registration** and uses the per-tool envelope only for a
  precise `get_architecture` request. So a timed-out `recall` over a long-lived server carried
  `generated_at` from server start-up, and no test could see it, because no test ran the
  production path.
- **A queue that says "timeout" (B4).** `_reserve_mcp_worker` raises `TimeoutError("MCP worker
  capacity exhausted")` when all four slots are busy. Both boundaries render that as the same
  `operation_timeout` envelope as a real deadline lapse. The two mean opposite things: a lapse
  means the work was tried and did not finish, capacity exhaustion means the work never started
  and an immediate retry is reasonable. Over HTTP every agent shares the four slots, and one
  `mode=index` call can hold one for up to 600 s, so this is the answer a second agent gets.

## Practice on this date

- The MCP specification separates protocol errors from tool execution errors, and says of the
  latter that they "contain actionable feedback that language models can use to self-correct
  and retry with adjusted parameters", reported inside the result with `isError: true`, and
  that clients "SHOULD provide tool execution errors to language models to enable
  self-correction" (modelcontextprotocol.io, "Tools" → "Error Handling", fetched 2026-09-17).
  An answer of "operation_timeout" for a call that never started is not actionable feedback:
  it tells the caller the work was tried and was slow, when in fact retrying at once is right.
- One code path per behaviour is the standard argument against test doubles of production
  entry points: a twin only proves the twin works.

## The decision

- One bounded path: `_bounded_tool_call(name, arguments, execute, render)` owns the deadline,
  the worker, and the two failure answers. `_handle_tool_call` is its text form and the
  registered `call_tool` its formatted form; the only difference left between them is the
  renderer, which is itself directly tested. The registration-time `timeout_result` is gone, so
  a timeout answer is generated when the timeout happens.
  Formatting stays inside the worker thread (`_execute_formatted_tool_call`), because
  `tests/test_mcp_server.py` pins that a slow formatter must not block the event loop.
- Worker exhaustion gets its own answer: `_WorkerCapacityExhausted(TimeoutError)` — still a
  `TimeoutError`, so every existing caller and test keeps working — rendered as
  `worker_capacity_exhausted` with the warning `retry_after_running_calls_finish`. The
  envelope shape is unchanged.

Files: `scripts/mcp_server.py`, `tests/test_mcp_server.py`, `tests/test_impact_analysis.py`,
`tests/test_the_busy_answer_is_not_a_timeout.py`,
`docs/research/2026-09-17-one-bounded-tool-path-and-a-busy-answer-that-says-so.md`.
