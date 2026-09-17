# A new token never lands in someone else's file

Date: 2026-09-17. Audit 3, code intelligence, finding B6 (low).

Files: `scripts/mcp_http.py`, `tests/test_a_new_token_never_lands_in_someone_elses_file.py`

## What was found

`mcp_http._write_new_token` opens the token path with
`O_WRONLY | O_CREAT | O_TRUNC`. It runs when no usable token exists, which
includes the case where the path is a symbolic link (`Path.is_file()` is false
for a dangling link, and an empty target yields no token). `open` follows the
link, so the fresh bearer secret is written into whatever the link names, with
that file's existing permissions — and that file is truncated first. The token
directory is the user's own `run/mcp-http/` with mode 0700, so this needs someone
who can already write there; it is a hardening gap, not a remote hole.

## Sources

- POSIX `open()` (https://pubs.opengroup.org/onlinepubs/9699919799/functions/open.html):
  with `O_CREAT` and `O_EXCL` set, "open() shall fail if the file exists", and
  "if O_EXCL and O_CREAT are set, and path names a symbolic link, open() shall
  fail and set errno to [EEXIST], regardless of the contents of the symbolic
  link."
- Measured here, not quoted: `os.replace` onto a path that is a symbolic link
  replaces the link itself and leaves the link's target untouched. The new test
  asserts exactly that, so the claim is checked on every run rather than taken
  from memory.

## Decision

The token is written to a new, randomly named file in the same directory,
created with `O_CREAT | O_EXCL` (plus `O_NOFOLLOW` where the platform has it)
and mode 0600, and then moved over the token path with `os.replace`. A link or
a stale file at the token path is replaced as a directory entry and never opened
for writing. A temporary file whose write failed is removed.
