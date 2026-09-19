# A published record is let go before it is flushed

Date: 2026-09-18
Files: `scripts/lsp_process.py`,
`tests/test_a_failed_server_leaves_its_redacted_last_words.py`,
`tests/test_a_lapsed_deadline_is_not_a_torn_stream.py`

## What was found

Three Windows failures in run 35363057747 of `Ekgardt/llm-wiki`: one product defect and
two tests that assert more than the contract gives them.

### A record is visible under its final name while still held exclusively

`tests/test_a_failed_server_leaves_its_redacted_last_words.py::test_the_failed_server_reason_is_kept_with_its_credentials_removed`
fails with

```
PermissionError: [Errno 13] Permission denied: '...\dddddddddddddddddddddddddddddddd\failure.json'
```

Not an ACL problem: `LspProcess.start` returned, so
`_secure_windows_owner_root` had already verified an `icacls … /grant:r` full-control ACE
for the very user running the test.

It is a share-mode collision. `windows_workspace._share_mode` returns `0` for a file — no
sharing at all — and `_delete_access` adds `DELETE`, because publication is a rename
*through that handle* (`FileRenameInformation`). `_write_record_windows` then does the
rename and, **while the handle is still open**, calls `sync_directory()`:

```python
try:
    self._publish_record_windows(handle, name, payload)   # write, flush, RENAME
    published = True
    self.sync_directory()          # FlushFileBuffers on the owner directory
except BaseException as error:
    operation_error = error
self._finish_windows_temporary(...)  # only this closes the handle
```

So between the rename and the close, `failure.json` exists under its contract name and no
other process can open it for data. The window is exactly as long as a
`FlushFileBuffers` on an NTFS directory handle — a metadata flush, on the same hosted
disk `tests/slow_machine.py` records as the one where "a migration that costs 0.1 s
locally measured 12.89 s there".

The test loses that race by construction: it polls `evidence.is_file()`, which an
attribute-only open satisfies without a sharing check, and then calls `read_bytes()`,
which asks for `FILE_READ_DATA` and gets `ERROR_SHARING_VIOLATION (32)` — mapped by the
CRT to `EACCES`, printed as the `[Errno 13]` above with no `[WinError]`, exactly as CI
shows. The file becomes visible at the rename, so the first poll that sees it is inside
the window.

The product's sibling path already gets the ordering right. `_write_lease_windows` calls
`_finish_windows_temporary(...)` and only then `self.sync_directory()`. The record path
is simply inconsistent with the lease path.

The defect is not confined to a test. `doctor` reads these records with the same
share-0 `open_file` and, on a sharing violation, classifies the owner as unreadable —
a spurious degraded finding against a record that is perfectly well formed.

Widening the writer's share mode would not fix it: the Windows share check is mutual, the
open handle holds `DELETE`, and CPython's `open()` does not request `FILE_SHARE_DELETE`.
The only thing that frees the file is closing the handle. That is also why the owner-root
design's exclusivity should not be touched here — it is deliberate, and changing it is an
architecture decision, not a CI fix.

### The transport is fatal before it says why

`test_any_reader_failure_makes_the_transport_fatal` asserts
`(_await(lambda: protocol.fatal), reasons) == (True, ["failed to read LSP stdout"])`.

`LspProtocol._become_fatal` sets `_fatal_error` under the state lock, completes pending
work, calls `_stop_io()`, and calls the reason callback **last** — deliberately, so a
callback can never see a half-stopped transport or deadlock against the lock. The test
waits on the first of those events and asserts the second. It is wrong on every platform;
Windows merely widens the gap past the 5 ms poll, because `_stop_io` there additionally
calls `CancelSynchronousIo` through ctypes (which drops the GIL) and tears down a
`socket.socketpair()` that Windows emulates as a loopback TCP pair rather than the cheap
`AF_UNIX` pair POSIX gets.

No production consumer of `.fatal` reads the callback's reason synchronously; each
queues its own through `_refuse_unusable_generation`.

### A POSIX-only mechanism asserted without a platform mark

`test_the_native_launch_copy_keeps_its_name` asserts
`isinstance(launch, GenerationLaunch)`. `_LaunchServerGuard.__enter__` returns a
`GenerationLaunch` only on POSIX:

```python
if os.name == "posix":
    return self._posix_launch(before)
self.verify()
...
return self
```

On Windows there is no launch copy and no name to keep: the source is held open with
`FILE_SHARE_READ` only and re-verified on exit, which is that platform's answer to the
verify-then-execute race. The product expects the non-launch return —
`launch = entered if isinstance(entered, GenerationLaunch) else None`. The test simply
describes a POSIX mechanism and never said so.

## Decision

**Product.** `_write_record_windows` closes the handle before flushing the directory,
matching `_write_lease_windows`. `published` was already `True` before the old
`sync_directory()`, the file's own bytes were already flushed inside
`_publish_record_windows`, and a directory-flush failure still propagates out of
`write_record` — now raised directly rather than through `_raise_temporary_errors`. The
exclusive window over the final name shrinks to the microseconds between the rename and
`NtClose`.

**Tests.** The evidence reader waits for the record to be *readable*, not merely named,
within the same deadline, and a denial that persists still fails — naming the real error
as its cause. This is needed even with the product fix: a rename-published file on
Windows is unopenable until the handle closes, and the product itself already tolerates
exactly that for the lease (`_WINDOWS_LEASE_RETRY_ERRORS` = {5, 32, 33}).

The fatal test waits on the reason and asserts the flag as well, which is strictly
stronger than before: `protocol.fatal`, the exact reason list, and a reason that never
arrives all still fail it.

The launch-copy test takes the platform mark its two neighbours in
`tests/test_pyright_session.py` already carry
(`@pytest.mark.skipif(os.name != "posix", …)`). Scoping a test to the platform whose
mechanism it describes is not weakening it; asserting the POSIX return value on Windows
was simply false. Windows coverage of the guard's verify-in-place path is a separate
gap and is not opened here.

## Sources

- `scripts/windows_workspace.py`, `_share_mode` (`return 0` for files) and
  `_delete_access` (`return _DELETE` when creating a file) — read in this checkout.
- `scripts/lsp_process.py`, `_write_record_windows` beside `_write_lease_windows` — the
  two orderings, read side by side.
- `scripts/lsp_protocol.py`, `_become_fatal` — the callback is the last statement.
- `scripts/pyright_session.py`, `_LaunchServerGuard.__enter__` — the `os.name == "posix"`
  branch, and `scripts/lsp_process.py`'s
  `launch = entered if isinstance(entered, GenerationLaunch) else None`.
- The CI evidence: job 105658783885 of run 35363057747 for the `PermissionError` and the
  empty reason list, jobs 105658783885 and 105658783942 for the launch-copy assertion.
