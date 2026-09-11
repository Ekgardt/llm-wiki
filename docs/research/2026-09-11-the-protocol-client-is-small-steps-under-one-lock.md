# The protocol client is small steps under one lock

Date: 2026-09-11. Trigger: audit finding OPS-15 (Rule 5 debt), file
`scripts/lsp_protocol.py`: 30 functions over the gate, among them
`LspProtocol.__init__` (CCN 28, a 140-line start-up with its own
cleanup ladder), `_writer_loop` (27), `request` (25),
`_raise_collected_errors` (25), `JsonRpcFrameReader.read` (23),
`json_depth` (19), `_validate_message` (16), `_become_fatal` (13).

## Sources

1. `knowledge/notes/read-only-lsp-navigation-engine-decision.md` and
   `lsp-process-containment-decision.md`: one state lock guards the
   pending table, the write accounting and the fatal/closed flags; the
   reader and writer threads are the only owners of their streams; a
   fatal transition completes every pending request and write exactly
   once. The file already names the invariant in its method names
   (`_..._locked` runs only under `_state_lock`).
2. `tests/test_lsp_protocol.py` (2 800 lines), `tests/test_pyright_session.py`:
   every violation message, terminal outcome and ownership error these
   assert is kept verbatim.
3. Rule 5's remedies: a validation ladder becomes a sequence of named
   checks; a loop body that decides "continue / stop / fail" becomes one
   function that returns that decision; the same guard written four
   times (`deadline must be a monotonic timestamp`) becomes one.

## Decision

Behaviour, messages and lock discipline unchanged. Every extracted
helper that touches shared state keeps the `_locked` suffix and is
called only from a holder of the lock; helpers that run outside the
lock take snapshots the way the original code did. The writer loop is
`_write_next` → `_process_write_task` → `_finish_written_task`, with one
`_write_deadline_outcome` for the two deadline checks. Start-up is
`_start_owners` and `_abort_startup`. The JSON validators are pure
functions over the frame. One `_require_monotonic(value, label)` replaces
the four copies of the timestamp guard.

Files: `scripts/lsp_protocol.py`, `CHANGELOG.md`,
`docs/AUDIT-2026-09-10-operations-and-reliability.md`.
