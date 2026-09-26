# A grounded recall has the time it needs, and its provider call ends with it

Date: 2026-09-25. Audit item A-17 (`docs/AUDIT-2026-09-25-full.md`).

## Facts (checked in code)

- The MCP server gives every tool call `MCP_OPERATION_SECONDS` = 10 s unless
  `_tool_operation_seconds` names another budget (the LSP and index modes of
  `get_architecture` have 60 s and 600 s). `recall` with `grounded=true` gets 10 s.
- `query_memory.QA_DEADLINE_SECONDS` = 120 s, with the measurement behind it:
  one provider round trip for a 4 KiB evidence prompt took 32.5 s, retrieval and
  corpus capture 6.3 s more. A grounded recall through MCP therefore cannot
  answer; it times out every time.
- `_detached_provider` starts the provider on a daemon thread and stops waiting
  at the deadline, but nothing stops the call: the CLI process runs on under the
  client's own default ceiling, one more for every retry.
- `llm_client.call_ceiling` sets a module global. The MCP server runs tools on
  worker threads, so a compile's 300 s ceiling applied to a recall running beside
  it, and a ceiling set inside a thread would apply to every other thread too.

## Source

- Claude Code MCP documentation, https://code.claude.com/docs/en/mcp (fetched
  2026-09-25): an unset `MCP_TOOL_TIMEOUT` means a 28-hour client timeout; a
  server may take the time its tool needs.
- Python `contextvars`, https://docs.python.org/3/library/contextvars.html
  (fetched 2026-09-25): "Since each thread has its own context stack,
  `ContextVar` objects behave in a similar fashion to `threading.local()` when
  values are assigned in different threads", and `Context.run` "enters the
  Context, executes callable ... then exits the Context".

## Decision

- `recall` with `grounded=true` gets `QA_DEADLINE_SECONDS` in the server's
  per-tool budget table.
- `call_ceiling` is kept in a `ContextVar`, so it applies to the calls of its own
  context only. The grounded provider thread runs in a copy of the caller's
  context under a ceiling equal to the time left, so the provider process is
  ended at the deadline instead of outliving it.

## Files

- `scripts/llm_client.py`
- `scripts/query_memory.py`
- `scripts/mcp_server.py`
- `tests/test_a_grounded_recall_has_the_time_it_needs.py`
- `CHANGELOG.md`

## Follow-up the same day (clean run of b4f3015a)

- Fact: `tests/test_slow_machine.py::test_no_test_carries_a_literal_hang_bound` failed on this
  note's test, which bounded a thread join with a literal `5`. It uses `LONG_TIMEOUT` like every
  other test.
- File: `tests/test_a_grounded_recall_has_the_time_it_needs.py`.
