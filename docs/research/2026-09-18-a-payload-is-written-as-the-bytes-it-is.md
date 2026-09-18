# A payload is written as the bytes it is — Windows must not rewrite its newlines

Date: 2026-09-18
Files: `scripts/memory_state.py`, `scripts/scheduled_nightly.py`, `scripts/maybe_compile.py`,
`scripts/search_memory.py`, `scripts/doctor.py`, `scripts/install_control.py`,
`scripts/install_language_server.py`, `scripts/integration_config_backup.py`,
`scripts/lsp_launch_package.py`, `scripts/memory_queue.py`, `scripts/mcp_http.py`,
`tests/test_a_payload_is_written_as_the_bytes_it_is.py`

## What was found

Run 35363057747 of `Ekgardt/llm-wiki` failed 31 jobs, almost all on Windows. About
twenty-five of them are one defect. The rest of the Windows noise is separate and is
handled elsewhere.

`tests/test_scheduled_fence.py::test_a_vault_without_a_coordinator_keeps_the_legacy_marker`
shows the mechanism in plain bytes:

```
assert [b'2504\r\nwi...58589888\r\n'] == [b'2504\nwind...0058589888\n']
```

The marker is built as `f"{os.getpid()}\n{identity}\n".encode(...)` and written with
`os.write` to a descriptor from
`os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)`. On disk it carries `\r\n`.

The cause is the Microsoft C runtime. `os.open` maps onto `_wopen`, which takes either
`_O_BINARY` — "Opens the file in binary (untranslated) mode" — or `_O_TEXT` — "Opens a
file in ANSI text (translated) mode". Given neither, the descriptor takes the process
default, and Microsoft's own page on the subject says what that default is: "The initial
default setting is ANSI text mode (`_O_TEXT`)." A text-mode descriptor then translates
on every write: "In text mode, carriage return-line feed (CRLF) combinations are
translated into single line feed (LF) characters on input, and LF characters are
translated to CRLF combinations on output."

Wrapping the descriptor in `os.fdopen(fd, "wb")` does not escape it: the translation
happens inside the runtime's `write`, below every Python layer.

On POSIX `O_BINARY` does not exist and nothing translates, which is why this was green
here and red there.

## Why it broke the state lock now

Until commit 35890638 the state lock's payload was `str(os.getpid()).encode()` — no
newline, so nothing to translate, and the defect was dormant. That commit made the
payload two lines, `f"{os.getpid()}\n{identity}\n"`, so that a reused PID could not
inherit a lock. From then on, on Windows:

1. `_claim_lock` writes `b"2504\r\nwindows:...\r\n"` while the holder keeps
   `b"2504\nwindows:...\n"` in memory as `payload`.
2. `_release_state_lock` unlinks only `if LOCK_FILE.read_bytes() == payload`. The two
   never match, so **the lock file is never removed**.
3. Every later writer finds a lock file whose recorded owner is a live process, waits
   out its 10 s timeout and raises `StateLockTimeout`.

That is the whole Windows cascade: `test_episode_consolidation`, `test_compile_failure`,
`test_context_noise`, `test_no_markdown_write_under_the_state_lock`,
`test_a_torn_state_file_is_recovered_whatever_its_bytes`,
`test_a_corrupt_state_is_not_replaced_by_an_empty_one`,
`test_a_discarded_receipt_reopens_its_day`, `test_compile_transactions`,
`test_audit_fixes::test_e2e_compile_with_fake_provider`, the capture counter tests in
`test_capture_hooks.py`, and
`test_a_lock_names_the_process_not_only_its_number::test_the_state_lock_of_a_reused_pid_is_retired_without_waiting_out_its_age`
(which asserts the lock file is gone afterwards).

The defect is in the product, not in the tests: on a real Windows vault the first writer
would wedge the state file for every writer after it.

## The class, not the instance

Any descriptor this repository opens for writing and then fills with bytes has the same
hazard the moment those bytes contain `\n` — today's payload, or tomorrow's. The
codebase already knows the idiom: `getattr(os, "O_BINARY", 0)` appears at twenty-odd
read sites and in `memory_state._steal_guard`. It was simply missing from the write
sites.

An AST sweep of `scripts/` for `os.open` calls carrying a write flag and no `O_BINARY`
found fourteen. Three are POSIX-only branches (`doctor._open_existing_lock`,
`lsp_process._publish_record_posix`, `lsp_process._publish_lease_posix`) where the flag
does not exist and cannot matter. The other eleven are reachable on Windows and each
writes bytes a reader later compares, digests or parses:

| Site | What it writes |
|---|---|
| `memory_state._claim_lock` | the state lock payload, compared on release |
| `scheduled_nightly._write_marker` | the nightly fence marker, compared on retirement |
| `maybe_compile._exclusive_write_lock` | the four-line compile lock |
| `search_memory._write_lock_claim` | the index lock claim |
| `doctor._create_owned_lock` | the doctor lock record |
| `install_control._write_preimage` | a preimage addressed by its own SHA-256 |
| `install_language_server._claimed_lock` | the install lock record |
| `integration_config_backup._create_verified_backup` | a verbatim copy of a config file |
| `lsp_launch_package._write_sealed_file` | a sealed launch file |
| `memory_queue._write_durable_file` | a durable queue file, compared on link conflict |
| `mcp_http._write_private_file` | the HTTP MCP shared secret |

`install_control._write_preimage` and `integration_config_backup._create_verified_backup`
are the two that could have corrupted the operator's own data rather than merely wedged
a lock: both copy arbitrary file bytes, and a text-mode descriptor would have inserted
carriage returns into the copy. Both verify afterwards, so the visible failure would
have been a refusal, not silent damage — but a refusal on every multi-line config on
Windows.

## Decision

Add `getattr(os, "O_BINARY", 0)` to all eleven Windows-reachable write sites. The
expression is `0` on POSIX, so Linux and macOS behaviour is unchanged, and the identity
line that commit 35890638 bought is kept exactly as it is.

`mcp_http._write_private_file` additionally wraps its descriptor in a text stream;
that stream is given `newline=""` so the token is written verbatim rather than through
`TextIOWrapper`'s own `\n` → `os.linesep` translation, which would have reintroduced the
carriage return above the now-binary descriptor.

A regression test asserts the bytes on disk equal the bytes handed in, and that the
state lock file is gone after `update_state` returns. On Linux it passes before and
after the fix by construction, so the test also drives `_claim_lock`'s descriptor flags
directly and asserts `O_BINARY` is requested wherever the platform defines it — the only
part of the defect a Linux machine can observe. Windows CI is what proves the rest.

## Sources

Fetched 2026-09-18 and quoted verbatim:

- Microsoft Learn, "`_open`, `_wopen`"
  (learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen), *`oflag`*
  table: `_O_BINARY` — "Opens the file in binary (untranslated) mode."; `_O_TEXT` —
  "Opens a file in ANSI text (translated) mode."
- Microsoft Learn, "Text and Binary Mode File I/O"
  (learn.microsoft.com/en-us/cpp/c-runtime-library/text-and-binary-mode-file-i-o):
  "The initial default setting is ANSI text mode (`_O_TEXT`)."
- Microsoft Learn, "`fopen`, `_wfopen`"
  (learn.microsoft.com/en-us/cpp/c-runtime-library/reference/fopen-wfopen):
  "In text mode, carriage return-line feed (CRLF) combinations are translated into
  single line feed (LF) characters on input, and LF characters are translated to CRLF
  combinations on output."; and "If **`t`** or **`b`** isn't given in *`mode`*, the
  default translation mode is defined by the global variable `_fmode`."
- The CI evidence itself: job 105658784081 of run 35363057747, which prints the
  translated marker bytes beside the untranslated expectation.
