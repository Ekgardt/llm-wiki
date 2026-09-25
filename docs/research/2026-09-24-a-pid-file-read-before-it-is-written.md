# A pid file read before it is written

Dated 2026-09-24. The owner: «41 упал с ошибками, постоянно падает с этими ошибками».
The push run on `main` after PR 41 (run 36023732204, job `windows_full::py3.10-s2`)
failed `tests/test_memory_queue_cli.py::test_worker_timeout_kills_spawned_grandchild_tree`
with `ValueError: invalid literal for int() with base 10: ''`. The PR run on the same
tree passed. The three push runs on `main` before it failed three different Windows
tests (network reset, `database is locked`, this one); the first two were fixed in PR 41.

Files: `tests/pid_files.py` (new), `tests/test_memory_queue_cli.py`,
`tests/test_a_childs_tree_is_read_when_it_is_killed.py`,
`docs/research/2026-09-24-a-pid-file-read-before-it-is-written.md`.

## What was found

- The processor under test spawns a grandchild and writes its pid with
  `Path.write_text`, then sleeps. The test gives the processor child a 1-second
  deadline. `write_text` opens (creating an empty file), then writes. On a Windows
  runner, starting a `spawn` child and its grandchild takes most of that second, so
  the kill can land between the open and the write. The test checks that the file
  exists and then parses it: an existing, empty file is the observed failure.
  It is a race in the test, not in the product; the product did kill the tree.
- The same helper shape is used by four processors in `test_memory_queue_cli.py`
  and one in `test_a_childs_tree_is_read_when_it_is_killed.py`. The second one is
  read right after the child's ready handshake, which the child sends before it
  calls the processor, so that read can also find no file or an empty one.

## Practice on this date

- A file another process must never see half-written is written to a temporary
  name and renamed into place; `os.replace` is atomic on POSIX and on Windows
  within one volume (Python documentation, `os.replace`, fetched 2026-09-24:
  "If successful, the renaming will be an atomic operation (this is a POSIX
  requirement)").
- A test waits for the condition it needs with a bounded poll, not for a fixed
  sleep that a slow runner can outrun.

## The decisions

1. `tests/pid_files.py`: `write_pid(path, pid)` writes through a temporary file and
   `os.replace`; `read_pid(path, timeout)` polls until the file holds a whole
   integer or the timeout passes, then fails naming the path.
2. Every processor helper that writes a pid uses `write_pid`; every test that reads
   one uses `read_pid`.
3. The kill test's deadline is 10 s, not 1 s, so the processor always reaches its
   pid before the deadline; it then sleeps 120 s, so the deadline is still what
   stops it. The test's assertion — the grandchild is dead after the timeout — is
   unchanged.

## Sources

- Python 3 documentation, `os.replace` — https://docs.python.org/3/library/os.html#os.replace — fetched 2026-09-24.
- GitHub Actions run 36023732204, job log read 2026-09-24.
