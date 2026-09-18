# A lapsed deadline is not a torn stream

Dated 2026-09-18. Finding K-B17 of the third audit (2026-09-17), five sub-items. The research
before the fix. The owner delegated the decision.

Files: scripts/lsp_protocol.py, scripts/pyright_session.py,
tests/test_a_lapsed_deadline_is_not_a_torn_stream.py

## Each sub-item, checked

**(a) raw-fd read/write racing a cross-thread stream close — not a defect.** `_close_stream`
has exactly two callers: the `finally` of `_reader_loop` and the `finally` of `_writer_loop`.
Each runs on the thread that owns that descriptor, after the loop that used it has ended, so
no thread closes a descriptor another thread is selecting on or writing to, and no fd number
is freed while still in use here. The cross-thread stop path sets `_io_stopped`, which the
loops poll (`_OwnedReader._read_descriptor`, `_require_write_deadline`); the close still
happens on the owning thread. `lsp_process` closes the process pipes only after joining the
owners.

**(b) the reader loop's narrow except — a defect, fixed.** `_reader_loop` caught
`ProtocolViolation`, `OSError` and `ValueError`. Anything else -- a `KeyError` or `TypeError`
from a dispatch bug, an interruption delivered to that thread -- ended the reader thread with
no `_become_fatal`. The transport then looked healthy while nothing could ever arrive again:
every pending caller waited out its own deadline, no restart, no failure evidence. The writer
already does this correctly (`_try_write_frame` catches `BaseException` and calls
`_fail_unless_closed`), so this is one side of a pair disagreeing with itself; the reader now
matches the writer.

**(c) a lapsed write deadline is always protocol-fatal — a defect, fixed.** Two places made
the whole transport fatal for a deadline that lapsed while the stream was intact:
`_finish_written_task`, *after* a complete framed write, and `_write_live_task`, *before* any
write. Neither leaves a torn stream. `_raise_timed_out` already states the correct rule -- it
becomes fatal only when `write_phase == "sending"`, a write caught in flight -- so the
transport contradicted itself. Now: after a complete write the task succeeds (the bytes are
out; the caller's own wait still ends in `_await_outcome`); before a write the task is
completed with `TimeoutError`, its pending request is put back to `queued` (it never reached
the stream, which is what `queued` means to `_commit_terminal_locked`), and the writer takes
the next task.

**(d) the progress gate — half a defect, half a limit.** `_mark_progress_ready` already
requires the token to be one the server asked us to create, so a stray `$/progress` `end`
cannot mark readiness. What is true is that the *first* created token's `end` marks the
session ready, and rust-analyzer opens several (indexing, cache priming); that stays, stated
as a limit, because requiring every token to end would never finish on a server that keeps
opening them. What was a real cost: a progress-gated server that never asks for a token made
every query wait the caller's whole deadline for a notification that was not coming, and then
answer `not_ready` anyway. The gate now waits only a short grace while no token exists, and
the caller's full deadline once one does.

**(e) the native launch copy is unlinked — a defect, fixed.** `_native_path_launch` kept
`self._snapshot_path`, and `_LaunchServerGuard.close()` unlinks it at the end of the `with`
block -- which is just after the server was started from that very path. The method's own
docstring says the copy has to keep a name. `_package_launch_from`, the sibling strategy,
already clears `_snapshot_path` when it hands the file on; the native path now does the same.
The owner root removes it with everything else when the generation ends.

## Practice on this date

- The protocol has no notion of a per-message deadline at all: a request is answered, or
  cancelled with `$/cancelRequest`, and "the request is not aborted by the server" on
  cancellation -- the server still responds. (LSP 3.17, `$/cancelRequest` and Base Protocol,
  <https://github.com/microsoft/language-server-protocol/blob/gh-pages/_specifications/lsp/3.17/specification.md>.)
  So a deadline is entirely the client's own budget, and spending a healthy connection to
  enforce one is the client punishing itself.
- On gopls reading its own binary: the gopls telemetry path re-executes the running binary
  (`os.Executable()`) and stamps counters against its own build; a binary whose name has been
  removed cannot be re-executed by that path. Recorded when this product's gopls support was
  designed -- see `docs/research/2026-09-12-installing-go-and-building-gopls.md`, which is why
  `_native_path_launch` exists at all.

## What this costs

Nothing per message. One extra state-lock acquisition when a write task's deadline lapses
before its write, which is the rare path. The native copy now lives until the generation ends
instead of until the guard exits -- bytes already written, inside the owner root that is
removed either way.
