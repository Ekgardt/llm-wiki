# Five failures only Windows and macOS showed

Date: 2026-09-17

Files: scripts/install_language_server.py, scripts/bounded_io.py, scripts/corpus_snapshot.py,
scripts/capture_diagnostics.py, tests/test_a_moved_host_directory_is_still_the_host.py,
tests/test_a_pinned_zip_is_unpacked_as_a_zip.py,
tests/test_a_file_being_written_is_a_changed_corpus_on_every_system.py,
tests/test_the_trail_append_is_serialized_where_append_is_not_atomic.py,
tests/test_capture_hooks.py

## What was found

The pull request's checks were green on Linux and red on every Windows job and one macOS
job. Each failure was read from its job log, then traced in the code.

1. **A transcript read back with `\r\n` (Windows). The test was wrong.** The product reads
   a transcript as bytes (`bounded_io.read_stable_bytes`, opened with `O_BINARY`) and keeps
   them as they are. The fixture wrote its file with `Path.write_text`, which translates
   `\n` to `\r\n` on Windows, and then expected `\n` back. The fixture now writes bytes.

2. **A zip entry named `/absolute` was unpacked (Windows). The product was wrong.**
   `_member_path_escapes` asked the host's `Path` whether the name was absolute. On Windows
   a rooted name without a drive is not absolute, so the entry passed. The reverse is also
   true: `C:/x`, `C:x`, `\x` and `go\..\..\x` are plain names to a POSIX `Path` and
   escapes on Windows. The entry's own name must decide, not the host that unpacks it.

3. **A checkout being written was refused as `repository_exceeds_corpus_bounds`
   (Windows). The product was wrong.** On POSIX the collector reads through directory
   descriptors and raises `CorpusChanged` when a file moves under it; that is retried
   `MAX_CAPTURE_PASSES` times and then named `repository_changed_during_capture`. On
   Windows the collector seals each file by path and reads it later with
   `read_stable_bytes`, which reports the same event as a bare `PermissionError`
   ("changed before open", "changed during read"). `PermissionError` is an `OSError`, so
   the capture was neither retried nor named correctly: the operator was told the
   repository was too large. The same applies to the file's own seal in `_Discovery.add`,
   which is built and verified by two consecutive stats. A changed *ancestor* stays a
   `PermissionError`; only the source file itself being written is a changed corpus.

4. **A burst of failure lines tore each other (Windows). The product was wrong.**
   `_append_failure_line` relies on one `O_APPEND` write being atomic. It is on POSIX. On
   Windows the C runtime implements append as a seek followed by a write, so two writers
   that seek to the same end overwrite each other. The job log shows a line that no longer
   starts as JSON, which is what the tail of a longer overwritten line looks like.

5. **"Fails open quickly" measured 0.75 s (macOS, one job). The test was wrong.** The
   product's own design is a 0.1 s hook lock wait followed by recording the dropped write,
   which waits for the same lock for `STATE_LOCK_TIMEOUT` = 0.5 s: 0.6 s by construction.
   A 0.75 s literal leaves 0.15 s for a loaded hosted runner. What the test protects is
   that a hook never pays the default 10-second lock wait of `memory_state.update_state`,
   so that default is the bound, read from the function's own signature.

## Sources

- Microsoft Learn, `_open, _wopen`
  (https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/open-wopen), fetched
  2026-09-17: "`_O_APPEND` Moves the file pointer to the end of the file before every
  write operation."
- Microsoft Learn, `_locking`
  (https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking), fetched
  2026-09-17: "All locking or unlocking begins at the current position of the file pointer
  and proceeds for the next *nbytes* bytes. It's possible to lock bytes past end of file."
  "`_LK_LOCK` Locks the specified bytes. If the bytes can't be locked, the program
  immediately tries again after 1 second. If the bytes can't be locked after 10 attempts,
  the constant returns an error." "`_LK_NBLCK` Locks the specified bytes. If the bytes
  can't be locked, the constant returns an error." "Regions should be locked only briefly
  and should be unlocked before closing a file or exiting the program."
- Microsoft Learn, `LockFile`
  (https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfile),
  fetched 2026-09-17: "You can lock bytes that are beyond the end of the current file. This
  is useful to coordinate adding records to the end of a file." "If the locking process
  opens the file a second time, it cannot access the specified region through this second
  handle until it unlocks the region."
- Python documentation, `pathlib`
  (https://docs.python.org/3/library/pathlib.html), fetched 2026-09-17: "A path is
  considered absolute if it has both a root and (if the flavour allows) a drive", with
  `PureWindowsPath('/a/b').is_absolute()` shown as `False`.

## Decisions

- **Zip and tar names.** A name is refused when either `PurePosixPath` or
  `PureWindowsPath` gives it an anchor (root or drive) or a `..` part. Both flavours are
  asked on every system, so the answer is the same everywhere. The landing path is built
  from the POSIX parts, which is what both archive formats specify.
- **A file changing under a read** gets its own type, `bounded_io.SourceChangedDuringRead`,
  a subclass of `PermissionError` with the same messages, so every existing caller behaves
  as before. The corpus collector turns it into `CorpusChanged`. No error text is matched.
- **The trail append on Windows** takes a one-byte lock before the write and releases it
  after. The byte is at offset 2^30, far past the 256 KiB cap, not byte 0: Windows locks
  are mandatory, and a lock on byte 0 would make a reader of the trail fail while a line
  is being added. The write still lands at the end because the descriptor is in append
  mode. The lock is `LK_NBLCK` in a bounded loop (40 attempts, 25 ms apart: 1 second),
  not `LK_LOCK`, whose ten one-second attempts are longer than a hook's 3-second budget.
  If the lock is never obtained the line is not written and the function returns False,
  as it already does for any other write failure; it never raises. POSIX keeps the single
  append write. The locking module is a parameter, so the order lock, write, unlock and
  the bounded give-up are tested on every system with a stand-in.

## Not verified

None of this can be run on Windows or macOS from here. The Linux suite proves the new
name rules, the new error type and its translation, and the lock order; the pull request's
Windows and macOS jobs are the proof for the rest.
