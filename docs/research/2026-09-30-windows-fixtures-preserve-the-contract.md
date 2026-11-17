# Windows qualification — 2026-09-30

Actual CI exposed platform assumptions in fixtures. Preserve their assertions, and change how inputs and observations are prepared:

- Write exact UTF-8 fixture bytes when the assertion requires a byte-identical preimage; parameterize LF and CRLF to test both real formats. Export manifest paths are POSIX paths, so construct expected keys with as_posix, without normalizing or relaxing stored-byte/hash assertions.
- A Bash runner needs LF and shell-usable forward-slash paths. The production PowerShell installer remains separate; no platform skip is added.
- Waiting once for a wall-clock interval does not prove that the wall clock crossed its boundary. Recheck the recorded lease boundary and wait only the remaining interval, preserving the renewal assertion. Do not add arbitrary sleeps or timing tolerance.
- LSP unit fixtures use fictitious PIDs. Mock the existing process-state boundary explicitly instead of probing unrelated Windows processes or weakening unreadable/deletion expectations. Production positive liveness and unknown-owner protection remain unchanged.

The repair CLI currently relies on Path.resolve to reject NUL. Modern Windows resolve can return such a path; reject NUL explicitly for both supplied root paths before resolution or backend access. NUL is outside POSIX and Windows path contracts. An explicit path validator is preferable to performing extra filesystem reads merely to force an exception. Missing paths retain their existing handling, and diagnostic failures still exit 2.

Primary sources checked 2026-09-30: [CPython pathlib](https://docs.python.org/3.10/library/pathlib.html), [CPython clocks](https://docs.python.org/3.10/library/time.html), [Microsoft file naming](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file), [POSIX pathname rationale](https://pubs.opengroup.org/onlinepubs/9799919799/xrat/V4_xbd_chap01.html), [MSYS2 path interoperability](https://www.msys2.org/docs/filesystem-paths/). GNU Bash and the POSIX definitions page initially failed to fetch and are not counted as fetched evidence. The POSIX rationale search result states the NUL restriction; Microsoft independently documents it. These sources explain the mechanisms, while retained CI logs establish the actual failures.

No new numeric ceiling, environment contract, runtime path or ownership role is introduced. This batch does not resolve capture admission under an exclusive maintenance fence.
