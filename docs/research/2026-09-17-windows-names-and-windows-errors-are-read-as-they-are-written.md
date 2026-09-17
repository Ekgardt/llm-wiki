# Windows names and Windows errors are read as they are written

Dated 2026-09-17. Findings Q-M20 (medium, simulated) and Q-M21 (medium, by reading) of the
third audit. Neither can be run here: there is no Windows host. Both were checked against
the code and the Python documentation; the tests simulate the operating-system boundary.
The research before the fix.

Files: scripts/markdown_transaction.py,
tests/test_a_windows_owner_with_a_non_ascii_name_is_verified.py,
tests/test_windows_handles_keep_their_width_and_their_error.py

`scripts/markdown_transaction.py` belongs to another fix area; the change is confined to
the Windows ACL and handle helpers (`_acl_*`, `_harden_windows_acl`, `_open_windows_*`,
`_close_windows_handle`, `_delete_windows_handle`), which that area does not touch.

## What was found

- M20. `_harden_windows_acl` grants the owner full control with `icacls` and then verifies
  it by looking for `DOMAIN\user` in the output of a second `icacls`. The output is decoded
  by `_acl_output_text` as ASCII with `errors="ignore"`, so every non-ASCII letter of the
  name is dropped from the output while the identity, read from `USERNAME`, keeps them. For
  an account such as `PC\Оператор` the substring can never match: `PermissionError: owner-only
  ACL verification failed`. The check sits under coordinator publishing, artifact roots and
  queue start-up, so the runtime cannot initialise for that account at all.
- M21, the error. Every helper loads `ctypes.windll.kernel32` and reads
  `ctypes.get_last_error()`. The documentation: the `use_last_error` parameter "when set to
  true, enables the same mechanism for the Windows error code", and `get_last_error()`
  "Returns the current value of the ctypes-private copy of the system `LastError` variable"
  (https://docs.python.org/3/library/ctypes.html). `ctypes.windll` is loaded without it, so
  the private copy is never written and the helpers raise `OSError(0, ...)`.
  `_windows_sharing_violation` looks for 32 and 33 and never sees them, so a sharing
  violation is never retried as the contention it is.
- M21, the handle. "By default functions are assumed to return the C int type" (same page).
  `CreateFileW.restype` is set to `HANDLE` only inside `_open_windows_directory`, as a side
  effect on the shared `windll` object. If `_open_windows_file_for_mutation` runs first, the
  handle comes back as a 32-bit int, and the comparison with `c_void_p(-1).value` (2**64-1)
  can never be true: an invalid handle goes undetected. `CloseHandle`,
  `GetFileInformationByHandle` and `SetFileInformationByHandle` have no `argtypes`, so a
  handle is passed to them as a C int as well.
- Siblings of the same shape elsewhere: the rest of the product already loads
  `ctypes.WinDLL("kernel32", use_last_error=True)` (`operational_ownership`,
  `lsp_process_tree`, `process_liveness`, `memory_queue`, `generation_catalog`).
  `doctor.py` (`ctypes.windll.kernel32.CreateFileW`, about line 6426) is the one other
  site and belongs to another area; it is named in the report.

## Practice on this date

- A console program writes a pipe in a code page, not in Unicode. Python names the two
  system code pages as codecs: `oem` — "Windows only: Encode the operand according to the
  OEM codepage (CP_OEMCP)", and `mbcs` — "Windows only: Encode the operand according to the
  ANSI codepage (CP_ACP)" (https://docs.python.org/3/library/codecs.html). Which of the
  console output code page, OEM or ANSI `icacls` uses for a pipe is not something an API
  reports, and it cannot be observed here.

## The decision

- M20: the ACL listing is decoded under the code pages `icacls` may have used — the console
  output code page when there is a console, then `oem`, then `mbcs` — and the listing is
  read under the first one in which the owner is named. All the checks that follow (exactly
  one owner line, full control, not inherited, nobody else) run on that one decoding, so the
  verification is as strict as before. Undecodable bytes are replaced, never dropped.
- M21: one loader, `_windows_kernel32()`, loads `kernel32` with `use_last_error=True` and
  declares `argtypes` and `restype` of the four functions once. Every helper uses it, and
  `_delete_windows_handle` reads the same private copy instead of calling `GetLastError`
  after the fact.
- Not verified on Windows. The tests replace the loader and the `icacls` process; a Windows
  runner exercises the real calls through the existing `test_code_workspace` cases.
