# A server that dies leaves its last words, and reading them never stalls the cleanup

Dated 2026-09-17. Findings K-A9, K-B11, K-B19 and K-D2 of the third audit. The research
before the fix. These four are one story: the language server's stderr.

Files: `scripts/lsp_process.py`, `scripts/lsp_security.py`, `scripts/doctor.py`,
`tests/test_lsp_stderr_is_read_as_it_arrives_and_never_stalls_cleanup.py`,
`tests/test_a_failed_server_leaves_its_redacted_last_words.py`,
`tests/test_lsp_security.py`, `tests/test_lsp_process.py`, `tests/test_doctor.py`.

## What was found

- **K-A9, reproduced today.** `sh -c "setsid sleep 3 & exit 0"` with `stderr=PIPE`, one
  thread in `stream.read(65537)`, then `stream.close()` from the main thread: the close
  blocked 2.8 s, until the escaped child let go of the write end. In the product that close
  is `_close_one_pipe`, called by `_release_one_generation` before the drain thread is
  joined, inside `_drive_cleanup` with the driver lock held. No deadline covers it.
- **K-B11.** The same `read(65537)` on a buffered pipe returns only when 64 KiB have
  arrived or the stream ended. A server that prints one line and keeps running shows nothing.
- **K-B19 / K-D2.** `LspProcess.stderr_bytes()` has no production caller, and
  `lsp_security.redact_lsp_text` has none either. The contract says "safe log redaction"
  is implemented. What exists is a tested redactor that nothing feeds and a 4 MiB stderr
  ring that nothing reads. A failed start leaves `failure.json` with a code and no reason.
- If the redactor were fed, `_CREDENTIAL_ASSIGNMENT` lets through `api-key:`, `apikey=`,
  `X-Api-Key:`, `passwd=`, `private_key=`, `cookie:`, a bare `Bearer …` and `sk-ant-…`
  (checked by running the function on each).

## Practice on this date

- Why the close blocks: "Binary buffered objects (instances of BufferedReader,
  BufferedWriter, BufferedRandom and BufferedRWPair) protect their internal structures using
  a lock; it is therefore safe to call them from multiple threads at once." (Python `io`
  documentation, section Multi-threading, <https://docs.python.org/3/library/io.html>,
  fetched today.) The reader holds that lock for the whole blocking read; `close()` waits
  for it.
- Why the read waits for 64 KiB: `BufferedIOBase.read` — "More than one read may be made
  and calls may be retried" (same page). `os.read` on the descriptor returns what the pipe
  holds.
- When the read ends by itself: "If all file descriptors referring to the write end of a
  pipe have been closed, then an attempt to read(2) from the pipe will see end-of-file
  (read(2) will return 0)." (`pipe(7)`, <https://man7.org/linux/man-pages/man7/pipe.7.html>,
  fetched today.) A process that left the group with `setsid()` keeps a write end open, so
  end-of-file is not ours to wait for. `CLAUDE.md` calls that escape unsupported; the
  cost must still be bounded.
- The usual way to stop a thread that blocks on a descriptor without polling is a second
  descriptor it selects on beside the first (the self-pipe pattern). `select` works on
  pipes on POSIX only; on Windows the Job Object kills every holder of the write end, so
  end-of-file does arrive there.

## The decision

1. The drain thread reads with `os.read` on the stream's descriptor, so every chunk is
   visible as it arrives, and nothing else ever holds the stream's lock.
2. On POSIX the thread selects on stderr and on a private wake pipe. Cleanup closes the
   write end of the wake pipe, the thread takes what is already in stderr (bounded by the
   ring size), closes stderr itself and ends. Cleanup closes stderr only when the thread is
   gone; if the thread does not end before the caller's deadline that is a recorded
   `generation_joins` failure, not a hang.
3. The redactor is wired, because the contract promises it and the wiring is small: when
   terminal failure evidence is written, the last kilobyte of the failed generation's
   stderr goes through `redact_lsp_text` into an optional `stderr_tail` field of the
   create-only `failure.json`. The record stays under its 4096-byte bound. Doctor accepts
   the field (one set and one check in `scripts/doctor.py`, which the maintenance area
   owns) and does not print it: it is for the operator reading the file, and costs no
   tokens.
4. `stderr_bytes()` stays as the reader of the ring that the tail is taken from.
5. The credential expression learns the listed spellings, and a second expression removes
   secrets that stand alone (`Bearer …`, `sk-…`, `ghp_…`, `xox?-…`).

Rejected: a separate log file under `logs/` (a second place for the same evidence, and
`lsp_process` knows its owner directory, not the log root); a polling drain loop (wakes up
ten times a second for every idle server).
