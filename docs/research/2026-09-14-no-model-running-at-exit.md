# No model running at exit

Dated 2026-09-14. Item 3.6 of `docs/AUDIT-2026-09-14.md`. The research before the
fix.

## What was found

- `pytest tests/test_mcp_server.py` alone: 362 passed, then
  `terminate called without an active exception`, `Fatal Python error: Aborted`,
  exit 134 — the same on commit `807a693`, before today's work.
- Reproduced with two small scripts: exiting the interpreter while a daemon thread
  is inside `SentenceTransformer.encode` or the reranker's forward pass aborts with
  exit 134; exiting while the model is still loading, or after it is idle, exits 0.
- `mcp_server.run_server` starts `warmup_retrieval_path` on a daemon thread
  (`_start_encoder_warmup`) — load the reranker, then two real retrieval passes — and
  returns without waiting for it. `retrieval.py` runs its optional (dense, rerank)
  stages on daemon worker threads that a caller abandons at its deadline. So a stdio
  session that closes within the first ~20 seconds, or while an abandoned stage is
  still in inference, can end with a core dump instead of an exit code. The test
  aborts because one test calls the real `run_server`, which starts the real warm-up.

## Practice on this date

- PyTorch's own tracker: "the problem occurs when a thread tries to acquire the GIL
  during Python interpreter finalization"; PyTorch's `gil_scoped_release` "can cause
  crashes when used with daemon threads", and the abort comes from unwinding inside
  that destructor ([pytorch/pytorch#38228](https://github.com/pytorch/pytorch/issues/38228)).
  The general remedy is to never let the interpreter finalize while such a thread is
  running: join it, or stop it at a safe point, before exit
  ([terminate called without an active exception](https://www.positioniseverything.net/terminate-called-without-an-active-exception/)).
- Python's `threading` documentation: daemon threads "are abruptly stopped at
  shutdown"; threads that must finish cleanly should be joined
  ([threading — daemon threads](https://docs.python.org/3/library/threading.html#thread-objects)).

## The decision

1. **Inference threads are registered.** `scripts/inference_threads.py` (standard
   library only) starts every thread that runs model inference — the MCP warm-up and
   retrieval's optional stages — records it while it runs, and holds one `stopping`
   event.
2. **The warm-up stops at a safe point.** `warmup_retrieval_path` checks `stopping`
   before each stage, so at shutdown it finishes at most the pass it is in.
3. **The server settles them before it returns.** `run_server`, after closing the
   navigation manager, sets `stopping` and joins the registered threads within
   `SHUTDOWN_INFERENCE_SECONDS = 30`; a thread still running then is named on
   stderr. A retrieval stage is bounded by its own deadline, so the bound is not
   expected to be reached; if it is, the abort remains possible and is named, not
   hidden.
4. **Tests do not load models by accident.** `tests/conftest.py` defaults
   `LLMWIKI_NO_ENCODER_WARMUP=1` for the test process, as the warm-up test file
   already sets for itself; tests of the warm-up call it directly.

Why not the alternatives:

- **`os._exit` at the end of `run_server`.** It skips every `atexit` handler and
  buffered write the process has; the abort would be replaced by lost cleanup.
- **Non-daemon threads.** The interpreter would wait for them without bound — an
  abandoned retrieval stage has no deadline of the process's choosing.

Files: `scripts/inference_threads.py`, `scripts/mcp_server.py`,
`scripts/retrieval.py`, `tests/test_no_model_running_at_exit.py`,
`tests/conftest.py`, `docs/research/2026-09-14-no-model-running-at-exit.md`.
