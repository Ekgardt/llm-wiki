# The last three Windows readers of a name and a handle

Date: 2026-09-17. Trigger: the third audit's Q-M20 and Q-M21 were fixed in
`markdown_transaction`, and that note
(`docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md`)
named the sites the fix did not reach. This note covers those three, so the class
is closed rather than one instance of it.

## What was found (by reading; there is no Windows host here)

1. `scripts/archive_daily.py`, `DailyArchiver._require_read_only_acl`. The archive
   seal applies a read-only ACL with `icacls` and verifies it by looking for the
   owner's name in a second `icacls` listing. It decodes that listing with the
   plain `_acl_output_text`, which uses only the **first** candidate code page.
   The repaired reader `_acl_lines_naming` — which tries the console output code
   page, then `oem`, then `mbcs`, and keeps the reading in which the owner is
   actually named — is right beside it and was not used. On an account whose name
   is not ASCII and whose console page is not the one `icacls` wrote, the
   verification raises `archive read-only ACL verification failed (principal other
   than …)`: the weekly archive seal fails on that machine, every time.
2. `scripts/memory_queue.py`, `_windows_acl_lines`. The same single-code-page
   decoding behind `_is_owner_only_windows`, which decides whether the queue's
   runtime files are owner-only. A mis-decoded listing reads as "not owner-only".
3. `scripts/doctor.py`, `_create_windows_lock_handle`. The one remaining
   `ctypes.windll.kernel32` in the product: no `use_last_error=True`, and
   `argtypes` unset — `restype` alone is set, on the shared `windll` object. The
   handle is therefore passed to `CloseHandle` as a C int, and the last error the
   doctor could report is the ctypes-private copy that is never written.

## Sources

- Python `ctypes`: `use_last_error` "when set to true, enables the same mechanism
  for the Windows error code"; "By default functions are assumed to return the C
  int type". https://docs.python.org/3/library/ctypes.html
- Python `codecs`: `oem` — "Windows only: Encode the operand according to the OEM
  codepage (CP_OEMCP)"; `mbcs` — "Windows only: Encode the operand according to
  the ANSI codepage (CP_ACP)". https://docs.python.org/3/library/codecs.html
- This repository:
  `docs/research/2026-09-17-windows-names-and-windows-errors-are-read-as-they-are-written.md`
  (the decision these three follow), `markdown_transaction._acl_lines_naming`,
  `markdown_transaction._windows_kernel32`.

## Decision

Each of the three uses the repaired helper that already exists; no new mechanism,
no second answer.

- `archive_daily` reads its ACL entries with `_acl_lines_naming(stdout, identity)`
  and runs every check that follows on that one reading. Its own `_acl_lines`
  splitter goes: the shared reader does the splitting.
- `memory_queue._windows_acl_lines` takes the identity it is verifying against and
  reads the listing the same way. (That module belongs to another fix area; this
  is the smallest possible change there, and it is named in the report.)
- `doctor` opens its lock handle through `markdown_transaction._windows_kernel32()`,
  which declares `CreateFileW` and `CloseHandle` once, with `use_last_error=True`
  and full-width handles.

Not run on Windows: no host. The tests drive the boundary — a fake `icacls`
process and a fake loader — exactly as the tests of the original fix do.

Files: `scripts/archive_daily.py`, `scripts/memory_queue.py`, `scripts/doctor.py`,
`tests/test_a_windows_name_is_read_wherever_an_acl_is_checked.py`.
