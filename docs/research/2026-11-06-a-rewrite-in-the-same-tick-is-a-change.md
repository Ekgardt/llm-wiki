# A rewrite in the same tick is a change

Date: 2026-11-06.

## What was true

`scripts/compile_cache.py` reads a cache entry in three steps: `lstat` the path
(regular file, not a symlink, owner-only), `open` it with `O_NOFOLLOW`, then
`fstat` the descriptor and compare it with the `lstat`. The comparison was
`(st_dev, st_ino, st_size, st_mtime_ns)`; the same tuple is compared again after
the read.

`tests/test_compile_cache.py::test_fd_read_rejects_same_inode_change_between_lstat_and_open`
rewrites an entry in place between `lstat` and `open` with the same size (only
`"schema_version":1` becomes `2`) and expects `PermissionError("cache entry changed
before open")`. Measured on Linux 6.8, `CONFIG_HZ=1000`:

| storage | old code, runs that raised | 
|---|---|
| tmpfs (`/dev/shm`), this session | 3 of 30 (27 `DID NOT RAISE`) |
| tmpfs, the platform's measurement | 8 of 30 |
| ext4 on disk, the platform's measurement | 30 of 30 |

The test is right and the reader was wrong: the entry is written by
`CompileCache.put` and rewritten within the same clock tick, so size and mtime are
equal and the change is invisible to the tuple. On disk `put`'s `fsync` is slow
enough to cross a tick, which is why it passed there. Debian 13 and Fedora mount
`/tmp` in RAM, so developers see the failure.

Whether this mattered to a user: everything read is still checked by content
(canonical JSON, schema version, action key, payload digest, plan schema and the
application validator), so a torn or foreign entry was never accepted as a plan.
What failed was the reader's own promise that the bytes it returns belong to the
file it inspected, unchanged; with a same-tick rewrite that promise was false.

## Sources (read 2026-11-06)

1. Linux kernel, `fs/inode.c` at v6.8, `current_time()`:
   `ktime_get_coarse_real_ts64(&now); return timestamp_truncate(now, inode);` —
   https://raw.githubusercontent.com/torvalds/linux/v6.8/fs/inode.c
2. Linux kernel documentation, *Multigrain Timestamps* —
   https://docs.kernel.org/filesystems/multigrain-ts.html: the coarse time "is
   updated every jiffy, so any change that happens within that jiffy will end up
   with the same timestamp"; multigrain timestamps (opt-in per filesystem,
   `FS_MGTIME`, merged after 6.8 — the series was posted as v6.13 material,
   https://lkml.iu.edu/hypermail/linux/kernel/2409.1/09516.html) make "the next
   timestamp update ... display a visible change" after a query. Not available on
   the 6.8 host, and not on older kernels this project supports.
3. Linux `statx` `STATX_CHANGE_COOKIE` (i_version) is kernel-only: "mask
   STATX_CHANGE_COOKIE off in requests from userland and in the result mask" —
   https://lore.kernel.org/all/20230126082355.ejgunkoqr4kocz3b@wittgenstein/
   (2023). The current `fs/stat.c` could not be fetched (HTTP 429), so whether a
   later kernel exposes it is unverified; Python 3.10 has no `statx` either way.
4. POSIX.1-2024, `write()` —
   https://pubs.opengroup.org/onlinepubs/9799919799/functions/write.html:
   "shall mark for update the last data modification and last file status change
   timestamps of the file". `st_ctime` cannot be set by `utime`/`utimensat`, so on
   POSIX it moves even when mtime is forged.
5. Git, *Racy Git* — https://git-scm.com/docs/racy-git: entries whose stat data
   matches but whose mtime is not older than the index are "racily clean", and Git
   compares their content instead of trusting stat.
6. Microsoft, *File Times* —
   https://learn.microsoft.com/en-us/windows/win32/sysinfo/file-times: times are in
   100 ns units, and "the file time is correctly reflected when the handle that
   makes the change is closed".
7. Python, `os` — https://docs.python.org/3/library/os.html: since 3.12
   `st_ctime`/`st_ctime_ns` "is deprecated on Windows. Use `st_birthtime` for the
   file creation time" — on Windows it is not a change time.

8. Python, *What's New In Python 3.13*, `time` —
   https://docs.python.org/3/whatsnew/3.13.html: on Windows `time()` now uses
   `GetSystemTimePreciseAsFileTime()` (1 µs) instead of `GetSystemTimeAsFileTime()`
   (15.6 ms), gh-63207. `time_ns()` reads the same system clock. The page was too long
   to read in full here, so this entry was taken from a summary of its `time`
   section and not quoted.

macOS: Apple's APFS reference could not be read as text in this session, so the
macOS timestamp clock granularity is unverified; its `st_ctime` is the POSIX
change time (source 4).

## Alternatives

- **Add `st_ctime_ns` only.** Stamped from the same coarse clock (source 1), so a
  same-tick rewrite keeps it too. Necessary against a forged mtime, not
  sufficient.
- **`statx` change cookie / i_version.** Not exposed to user space (source 3), no
  Windows or macOS equivalent in `os.stat`.
- **Content digest.** The only stamp-free signal, but there is nothing to compare
  it with before `open`: the reader has no earlier copy of the bytes, and the
  entry's own `payload_digest` is part of the content it would vouch for.
- **Open first, check the descriptor.** Removes the window, but the `lstat` before
  `open` is what refuses a symlink on Windows, where `O_NOFOLLOW` does not exist,
  and a content change before the only look is not detectable at all.
- **Wait out the tick (chosen).** Git's racily-clean rule made explicit: a stat
  observation proves "unchanged later" only when its change stamp is older than
  one clock tick, because then any later write is stamped in a later tick.

## Decision

The settle step lives in `scripts/bounded_io.py`, the shared reader, as
`settled_lstat(path, label=, deadline=)`:

- The file's change stamp is `change_stamp_ns`: `st_ctime_ns` on POSIX, which every
  write moves and `utime` cannot set (source 4), and `st_mtime_ns` on Windows, where
  `st_ctime` is the creation time (source 7). On POSIX a forged mtime therefore
  neither delays nor shortens the wait.
- When the stamp is within `TIMESTAMP_SETTLE_NS` (20 ms: Linux's jiffy is at most
  10 ms at `CONFIG_HZ=100`, Windows' default timer tick 15.6 ms) of the clock, in
  either direction, the reader sleeps until it is one window old and looks again.
  Within a window ahead is the same tick seen through two clocks.
- A stamp more than one window ahead of the clock is settled. No write made before
  the clock reaches that tick can carry that stamp, so a later write in this read is
  stamped differently. Only a write landing exactly in that future tick would look
  the same, and that needs a read that lasts until the clock gets there.
- Each look decides afresh. A file written again during the wait has a new stamp
  and is waited out again; a clock that has not moved far enough (Python's
  `time.time_ns()` on Windows before 3.13 ticks at the system timer, source 8) costs
  another look, not a refusal. A burst of writes is read once it stops.
- The waiting ends at the caller's deadline (`TimeoutError`, raised before a sleep
  that would cross it) or, for a caller with no deadline, after `SETTLE_BUDGET_NS`
  (1 s) with `SourceChangedDuringRead` "still being written". Basis for 1 s: it is
  well inside the 60 s default timeout of an agent hook, and 50 windows is far more
  than a writer that stops needs. A caller that needs another bound passes a
  deadline.
- `file_identity` is `(st_dev, st_ino, st_size, st_mtime_ns, ctime)`, so on POSIX
  a rewrite that restores mtime with `utime` is caught too. On Windows the ctime
  field is 0: Python reports the creation time there (source 7), and
  `markdown_transaction.py` already records that the path and descriptor views of
  it can differ.

`read_stable_bytes` (75 call sites in 31 other scripts) takes its first `lstat`
through `settled_lstat`; `compile_cache._read_cache_entry` calls the same helper
and `file_identity` instead of its own copies; `search_memory.py`'s seal
observation uses `change_stamp_ns` and the same constant.

## Trade-offs and limits

- Cost: one `time.time_ns()` per read; a sleep of at most 40 ms (20 ms for a stamp
  that is now, 40 ms for one a window ahead) only for a file written that recently.
  Most reads are of files written in an earlier run and never wait.
- A file written continuously, more often than every 20 ms, can no longer be read
  between two writes: the reader waits for its budget and refuses it. Measured
  below: an append every 10 ms that never stops was read 199 of 200 times by the
  old reader and 0 of 200 by this one, each refusal after 1 s. Such a read was only
  safe before because appends change the size; a same-size rewrite in that stream
  would have been missed. The refusal is a `PermissionError`, the same class as the
  existing "file changed during read" refusals, so callers that catch those treat
  it the same way. Only `corpus_snapshot._sealed_source_bytes` catches
  `SourceChangedDuringRead` by name.
- A stamp just over a window ahead (a clock stepped back by a few tens of
  milliseconds) is trusted at once; a same-size rewrite in that future tick, during
  a read that lasts until then, would be missed. Further ahead the read would have
  to last longer.
- Windows has no change time in `os.stat` (source 7): a same-size rewrite that also
  restores mtime with `os.utime` is not caught there; a plain rewrite is, once its
  handle is closed (source 6). The deterministic `utime` tests are skipped on
  Windows for this reason. The content checks remain the defence.
- A filesystem with 1 s or 2 s stamps (FAT, HFS+, ext4 with 128-byte inodes) is
  coarser than the window; the state root must already be a local filesystem with
  correct locking, and the defaults on all three platforms (ext4, XFS, btrfs,
  tmpfs, APFS, NTFS) stamp at 100 ns or finer.

### Readers that keep their own stat identity

Other code in `scripts/` compares stat fields without the settle step. They are
not changed here. The condition for each is stated so it can be checked.

Readers that return bytes and check one open descriptor before and after the
read: `doctor._read_opened_lsp_record`, `evidence_graph._require_manifest_unchanged`,
`integration_adapter._require_stable_transcript`, `memory_queue._read_stable_descriptor`
and `_descriptor_identity`, `reliable_memory._require_stable_read`,
`search_memory._require_stable_identity` and `_require_same_file`,
`lsp_security._posix_read_identity`, and the Windows branch of
`install_pyright._artifact_is_stable`. `corpus_snapshot`, `generation_catalog`,
`project_journal`, `markdown_transaction`, `private_vault_backup`,
`workspace_revision._strong_identity` and the POSIX branch of `install_pyright`
already compare ctime, but none of them settles. A same-size rewrite in the
same tick during their read is the same defect, and it is still there. Each one
has its own errors, deadline and callers. This change was asked to stay small,
so moving them is not part of it. Condition: each one is closed when it reads
through `bounded_io.read_stable_bytes` or takes its first stat through
`bounded_io.settled_lstat`.

Caches that use stat only to decide whether to look again, and return no bytes
on its strength: `pyright_session._file_identity` (its docstring states this
trade-off), `evidence_reader_cache._stat_identity`,
`pyright_profile._node_probe_key`, `mcp_supervisor._file_identity`,
`install_models._file_identity` (it already has its own 2 s racy window) and
`workspace_revision._identity`. A missed same-tick change there means an answer
is recomputed later, not that changed bytes are vouched for. Condition: revisit
any of them once a test shows a same-tick rewrite producing a wrong result
there.

## Verification

Linux 6.8, tmpfs (`/dev/shm`) unless stated. macOS and Windows were not run in
this session; CI runs them.

- `test_fd_read_rejects_same_size_rewrite_that_keeps_its_mtime` (compile cache)
  and `test_stable_read_rejects_same_size_rewrite_that_keeps_its_mtime`
  (`read_stable_bytes`) set the rewritten file's mtime back to the original
  nanosecond with `os.utime`. The old compile-cache code failed the first one in
  10 of 10 runs. With ctime removed from `file_identity`, the second fails with
  `DID NOT RAISE`.
- A probe of the first reviewer's case: write a file, rewrite it in place at the
  same size just before `read_stable_bytes` opens it. 200 runs each: with
  `settled_lstat` replaced by a plain `lstat` (ctime still in the identity), 182
  rewrites were missed. With the fix, 0 were missed.
- A probe of the second reviewer's case, old reader against new: 10 appends 10 ms
  apart, the read starting 5 ms into them. Old 100 of 100 read, new 100 of 100 read,
  the slowest new read 0.106 s. With appends that never stop: old 199 of 200, new 0
  of 200 ("still being written", 1 s each; see Trade-offs).
- `tests/test_bounded_io.py` has fake-clock tests with exact boundaries:
  - a fresh file waits once;
  - a clock that stands still through two waits gets a third look and is not
    refused;
  - five appends landing during the waits are waited out, and the read returns
    all of them; a writer that never stops is refused once the budget is spent;
  - a stamp exactly one window ahead waits, and one nanosecond more does not;
  - a page with a +1 h mtime is read, and a same-size rewrite of it is still
    caught; in `tests/test_compile_cache.py` a +60 s entry is a hit;
  - a wait past the deadline times out without sleeping.
- `tests/test_bounded_io.py`, `tests/test_compile_cache.py` and
  `tests/test_a_file_being_written_is_a_changed_corpus_on_every_system.py`:
  50 of 50 fresh pytest runs passed.
- The earlier round of this change: the whole `tests/test_compile_cache.py`
  passed 100 of 100 runs on tmpfs and 30 of 30 on ext4 on disk.
- `docs/research/2026-11-05-scripts-is-a-regular-package.md` (#54) made the
  original test move the mtime on after its rewrite, and recorded the limit:
  ctime alone kept both stamps in 1 373 of 2 000 rewrites, which matches the
  measurement above. The settle wait removes that limit, so the test again
  rewrites in place without touching the stamps.
