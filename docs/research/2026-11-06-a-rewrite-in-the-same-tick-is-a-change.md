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
  observation proves "unchanged later" only when its newest stamp is older than
  one clock tick, because then any later write is stamped in a later tick.

## Decision

`_read_cache_entry` takes its `lstat` through `_settled_lstat`: when the entry's
newest stamp (`max(st_mtime_ns, st_ctime_ns)`) is younger than
`bounded_io.TIMESTAMP_SETTLE_NS` (20 ms: Linux's jiffy is at most 10 ms at
`CONFIG_HZ=100`, Windows' default timer tick 15.6 ms), the reader sleeps until it
is not and looks again; an entry still fresh then is being written and is refused,
and one stamped more than the window in the future is refused. The identity tuple
gains `st_ctime_ns`, so on POSIX a rewrite that restores mtime is caught too.
`search_memory.py` used the same 20 ms under a private name; both now read the one
constant.

## Trade-offs and limits

- Cost: one `time.time_ns()` per read; a sleep of at most 20 ms only for an entry
  written less than 20 ms ago. The compile writes an entry after a model call and
  reads it in a later run, so production reads almost never wait.
- Windows has no change time in `os.stat` (source 7): a same-size rewrite that also
  restores mtime with `os.utime` is not caught there; a plain rewrite is, once its
  handle is closed (source 6). The deterministic test is skipped on Windows for
  this reason. The content checks remain the defence.
- A filesystem with 1 s or 2 s stamps (FAT, HFS+, ext4 with 128-byte inodes) is
  coarser than the window; the state root must already be a local filesystem with
  correct locking, and the defaults on all three platforms (ext4, XFS, btrfs,
  tmpfs, APFS, NTFS) stamp at 100 ns or finer.
- A wall clock stepped backwards by more than 20 ms makes existing entries look
  stamped in the future; they are cache misses until the clock catches up.
- The same `(dev, ino, size, mtime)` identity is used by `bounded_io.read_stable_bytes`
  and about fifteen other readers in `scripts/`. They are not changed here; each
  has its own callers and cost profile and needs its own measurement.

## Verification

- `test_fd_read_rejects_same_size_rewrite_that_keeps_its_mtime` sets the rewritten
  entry's mtime back to the original nanosecond with `os.utime`; the old code
  failed it 10 of 10 runs on tmpfs, with `DID NOT RAISE`.
- `test_entry_stamped_in_the_future_is_a_miss` covers the refusal of a future stamp.
- With the fix, the whole `tests/test_compile_cache.py` passed 100 of 100 runs on
  tmpfs and 30 of 30 on ext4 on disk (Linux 6.8). macOS and Windows were not run
  in this session; CI runs them.
- `docs/research/2026-11-05-scripts-is-a-regular-package.md` (#54) made the
  original test move the mtime on after its rewrite and recorded the limit, with
  ctime alone keeping both stamps in 1 373 of 2 000 rewrites, which matches the
  measurement above. The settle wait removes that limit, so the test again rewrites
  in place without touching the stamps. After rebasing onto #54, the test and the
  deterministic test passed 200 of 200 fresh pytest runs on tmpfs.
