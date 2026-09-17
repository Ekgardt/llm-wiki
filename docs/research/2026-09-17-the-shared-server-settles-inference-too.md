# The shared server settles inference too

Date: 2026-09-17. Audit 3, code intelligence, finding A8 (and contract drift D3).

Files: `scripts/mcp_http.py`,
`tests/test_the_shared_server_settles_inference_before_it_exits.py`

## What was found

`docs/research/2026-09-14-no-model-running-at-exit.md` decided that a closing
server sets `inference_threads.stopping` and joins the registered inference
threads before the interpreter finalizes, because exiting while a daemon thread
is inside a model forward pass aborts the process with exit 134. The stdio
transport does this (`mcp_server.run_server` calls `_settle_inference()` in its
`finally`). The HTTP transport does not: `mcp_http._shutdown` closes the
navigation manager and returns. Read on 2026-09-17: the only `settle` call in
`scripts/` is the stdio one.

The shared HTTP server is where the risk is larger, not smaller: it is the
long-lived process in which abandoned optional retrieval stages run, and with
`LLMWIKI_NO_SHARED_WARMUP=1` it starts the same background warm-up stdio does.
No test touched `_shutdown`.

## Sources

- Python `threading` documentation
  (https://docs.python.org/3/library/threading.html#thread-objects): "Daemon
  threads are abruptly stopped at shutdown. Their resources (such as open files,
  database transactions, etc.) may not be released properly."
- The 2026-09-14 note above, which reproduced the abort with two scripts and
  cites pytorch/pytorch#38228 for the cause.

## Alternatives

1. Copy the settle code into `mcp_http`. Two copies of one shutdown rule is how
   this transport was missed in the first place.
2. Call the one existing `mcp_server._settle_inference()` from
   `mcp_http._shutdown`, in a `finally`, so a navigation close that fails still
   leaves no model running.

## Decision

Alternative 2. The bound stays `SHUTDOWN_INFERENCE_SECONDS = 30`, and a thread
still running after it is named on stderr exactly as on stdio.
