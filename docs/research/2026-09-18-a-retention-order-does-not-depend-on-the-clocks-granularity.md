# A retention order does not depend on the clock's granularity

Date: 2026-09-18
Files: `scripts/maintenance_helpers.py`, `tests/test_the_scheduler_log_is_bounded.py`

## What was found

`tests/test_the_scheduler_log_is_bounded.py::test_a_scheduler_log_over_the_family_size_is_taken`
fails on Windows in run 35382495391 (job 105721706461):

```
assert (2, False) == (1, False)
```

The test writes `cron-nightly.log` at one byte over `REPORT_RETENTION_BYTES` and
`cron-weekly.log` at sixteen bytes, and expects the prune to take the oversized one and
leave the small one.

`_report_entries` sorts a family `(mtime, size, path)` newest first, and `_over_size`
walks that order accumulating sizes, dooming everything from the moment the running total
passes the budget:

```python
present.sort(key=lambda entry: entry[0], reverse=True)
...
for _, size, path in entries:
    used += size
    if used > max_bytes:
        doomed.add(path)
```

So the outcome turns entirely on which of the two files has the newer mtime:

- weekly newer → 16 bytes fit, then nightly busts the budget → **1 removed**, weekly kept.
- nightly newer → nightly busts it, and weekly is then already past the total → **2
  removed**.

The sort key is the mtime alone. When two files share an mtime the order is whatever
`glob` happened to yield, because Python's sort is stable. On Linux the test writes the
two files microseconds apart and the nanosecond mtimes separate them, so weekly is newer
and the test passes. Windows updates its file-time clock in ~15.6 ms ticks, both writes
land in the same tick, the tie falls back to directory order — `cron-nightly.log` before
`cron-weekly.log` — and both files are taken.

Reproducing it took one correction worth recording. Giving the two logs the same mtime
with `os.utime` did produce `removed=2` on the first try — but for the wrong reason: the
stamp chosen was in 2023, so the thirty-day **age** rule took both files and the tie was
never exercised. With a current whole-second stamp, twelve fresh directories on ext4 all
gave `removed=1`, because ext4's hash order happens to yield `cron-weekly.log` first for
this pair.

What makes Windows lose is that its order is not luck: NTFS indexes a directory by name,
so `scandir` yields `cron-nightly.log` first, every time. Listing the directory in name
order on Linux reproduces the CI assertion exactly — `(2, False, False)` against the
expected `(1, False, True)` — and that is what the new test does.

This is not only a test problem. macOS's HFS+ stores mtimes at one-second granularity,
and any pass that writes a family's logs inside one tick — which is the normal case for a
scheduler that redirects two jobs — gets an arbitrary answer about which log survives.

## Decision

The order is made total, so the same directory always prunes the same way on every
filesystem. Where the ages tie, the **smaller** file is treated as the one to keep:

```python
present.sort(key=lambda entry: (entry[0], -entry[1]), reverse=True)
```

That is deterministic, and it is the choice that keeps the most evidence: when two logs
are equally recent there is no age reason to prefer either, and dropping a sixteen-byte
log because a 32 MiB sibling shared its timestamp serves nobody. Age still dominates
completely — the tie-break only decides cases the previous code decided by accident.

The test keeps asserting what it asserts. A new case pins the tie directly, with both
files stamped to the same mtime, so the platform-independence is held by a test rather
than by the clock the test happens to run on.

## Sources

- Job 105721706461 of run 35382495391, and the local reproduction above: a name-ordered
  listing with equal current mtimes gives `(2, False, False)` before the change and
  `(1, False, True)` after it.
- `scripts/maintenance_helpers.py`, `_report_entries` and `_over_size`.
- Python's `list.sort` is documented stable, which is why the tie fell through to the
  listing order rather than being random — and why NTFS's name order made it reliably
  wrong rather than intermittently wrong.
