# A cached reader needs one thread at a time, not a serialized build

Date: 2026-09-10. Trigger: PR30 run 34500804888 on 3cca61c went red in 13 of
50 jobs after the merge of issue #24 section A (0453c11). Four distinct
classes, none of them a flake:

| class | where it failed | symptom |
|---|---|---|
| A | py3.10 on linux, macOS, windows (s1, s3) | `test_evidence_reader_cache` opened a reader twice, cached 0; `EvidenceGraph` has no `cached_scope`; `test_repository_refresh` answer has no `freshness` |
| B | windows py3.11–3.14 (s1) | `test_an_idle_reader_is_closed_on_the_next_access_and_a_held_one_is_not` → `(False, True, False)` |
| C | windows py3.10–3.14 (s3) | `test_refresh_rebuilds_only_after_an_edit_and_under_the_repository_fence` → `PermissionError: [WinError 5]` on `.git/objects/0b/f7a0…` inside the test's own `shutil.rmtree` |
| D | windows py3.10 (s3) | `test_a_loaded_machine_scales_every_wait_at_the_invocation` → the child `python -c` exits 1 |

## Sources

1. Python `sqlite3` reference — `threadsafety`: 0 single-thread "threads may
   not share the module"; 1 multi-thread "may share the module, but not
   connections"; 3 serialized "may share the module, connections and cursors".
   "Changed in version 3.11: Set threadsafety dynamically instead of
   hard-coding it to 1." `check_same_thread=False`: "the connection may be
   accessed in multiple threads; write operations may need to be serialized by
   the user". https://docs.python.org/3/library/sqlite3.html
2. SQLite "Using SQLite In Multi-Threaded Applications" — multi-thread mode:
   "SQLite can be safely used by multiple threads provided that no single
   database connection nor any object derived from database connection, such
   as a prepared statement, is used in two or more threads at the same time."
   https://www.sqlite.org/threadsafe.html
3. SQLAlchemy pooling — `SingletonThreadPool` "maintains one connection per
   each thread, never moving a connection to a thread other than the one which
   it was created in"; the SQLite dialect's answer to the same constraint.
   https://docs.sqlalchemy.org/en/20/core/pooling.html
4. What's New in Python 3.13, `time`: "On Windows, monotonic() now uses the
   QueryPerformanceCounter() clock for a resolution of 1 microsecond, instead
   of the GetTickCount64() clock which has a resolution of 15.6 milliseconds."
   (gh-88494). https://docs.python.org/3/whatsnew/3.13.html
5. Python `shutil` reference — `rmtree` example "remove_readonly": clear the
   read-only bit and reattempt, because on Windows a read-only file refuses
   `os.unlink` with access denied; `onexc` since 3.12, `onerror` deprecated.
   https://docs.python.org/3/library/shutil.html
6. python/cpython#105436 "`subprocess.run(..., env={})` broken on Windows" and
   the recurring reports (cosmic-ray#435, Travis community 9615): a Windows
   child Python started without `SYSTEMROOT` dies in
   `_Py_HashRandomization_Init: failed to get random numbers`.
   https://github.com/python/cpython/issues/105436

## Findings

**A — the gate was stricter than the invariant it protects.** The cache lends
one `SharedEvidenceGraph` (`check_same_thread=False`) to worker threads, and
every lease holds the entry's re-entrant lock for its whole lifetime
(`_Entry.lock`, measured 2026-09-10: without it eight concurrent `find_callers`
gave three answers). So the connection is never used by two threads at the
same time — exactly SQLite's multi-thread condition (source 2). The gate
`sqlite3.threadsafety == 3` demanded the *serialized* build instead, which is
never reported by Python 3.10 (hard-coded 1, source 1). On 3.10 the product
silently fell back to one cold open per answer and the MCP answer lost its
`freshness` block, because only a lease carries `cached_scope`. Only a
single-thread build (0) forbids the cache, and that build already forbids the
existing fallback, which opens a connection per worker thread. Per-thread
readers (source 3) would also be correct but add N page caches and a thread
identity to the key for no gain, since the lease lock serializes anyway.

**B — a strict inequality on a 15.6 ms clock.** The test set `IDLE_SECONDS`
to 0 and expected `now - last_used > 0` to hold across three consecutive calls;
on Windows before 3.13 `time.monotonic()` advances in 15.6 ms steps (source
4), so the difference was 0.0 and the idle reader survived. The product
condition is right; the test depended on the clock advancing. The cache now
reads its clock through one module attribute `_now`, and the test drives it,
so the expectation is stated in seconds, not in the granularity of a runner.

**C — Git's object files are read-only.** `_assert_a_missing_checkout_is_named_not_deleted`
removed a test repository with `shutil.rmtree`; loose objects are 0444 and
Windows refuses `unlink` on them (source 5). pytest's own `tmp_path` cleanup
knows this (`rm_rf`); the test's direct call did not. One helper,
`tests/filesystem.py::remove_tree`, clears the read-only bit on every entry
first, and it is the one way tests remove a checkout from now on. Sibling
search: `grep -ln rmtree tests/*.py | xargs grep -l _repository(` — only this
test removes a Git repository itself; `test_generation_catalog.py` patches
`rmtree` to simulate failure and removes no checkout.

**D — an empty environment is not hermetic on Windows.** The scale test
spawned the child with `env={"PATH": "", …}`; on Windows 3.10 the child needs
`SYSTEMROOT` to seed hash randomization (source 6), so it died before the
import and the test saw exit 1 with its stderr hidden by `check=True`. The
child now inherits the environment plus the scale variable, and a failure
prints the child's stderr. Sibling search: `grep -n 'env={' tests/*.py` —
this was the only test building a child environment from scratch.

## Decision

1. `shared_readers_supported()` returns `sqlite3.threadsafety != 0`; the
   docstring names the real invariant (one thread at a time, held by the
   lease lock). No per-thread keying.
2. `evidence_reader_cache._now` is the cache's clock; tests set it.
3. `tests/filesystem.py::remove_tree` for removing checkouts in tests, with
   `tests/test_filesystem.py` holding a read-only file.
4. The scale test inherits the environment and reports the child's stderr.

Files: `scripts/evidence_reader_cache.py`, `scripts/evidence_graph.py`,
`scripts/code_graph.py`, `tests/test_evidence_reader_cache.py`,
`tests/test_repository_refresh.py`, `tests/test_slow_machine.py`,
`tests/filesystem.py`, `tests/test_filesystem.py`, `CHANGELOG.md`,
`docs/ISSUES-2026-09-10.md`.

## What this does not settle

The reranker MCP window and the race test under load remain open
(`docs/ISSUES-2026-09-10.md`). Whether a Windows runner can prune a
generation while a reader is idle for less than `IDLE_SECONDS` is unchanged
by this note: the pin is documented in the cache module header.
