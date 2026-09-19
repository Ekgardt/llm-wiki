# A listed entry is stated now, not remembered

Date: 2026-09-18
Files: `scripts/corpus_snapshot.py`,
`tests/test_a_file_that_vanishes_mid_walk_is_a_changed_corpus.py`,
`tests/test_small_corrections_in_the_generations_area.py`

## What was found

Two Windows failures in run 35363057747 of `Ekgardt/llm-wiki`, one in the product and
one in a test.

### The corpus walk trusts a listing it should re-read

`tests/test_a_file_that_vanishes_mid_walk_is_a_changed_corpus.py::test_an_entry_that_goes_between_the_listing_and_its_metadata_is_a_changed_corpus`
fails on Windows with `DID NOT RAISE <class 'corpus_snapshot.CorpusChanged'>`. It
passes on Linux and macOS.

The Windows walk reads each listed entry's metadata through
`corpus_snapshot._CorpusWalk._entry_info`:

```python
def _entry_info(self, entry: os.DirEntry) -> os.stat_result:
    """This listed entry's metadata; one that has gone is a changed corpus."""
    try:
        return entry.stat(follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CorpusChanged(...) from exc
```

`os.DirEntry.stat()` is not a fresh look at the file. CPython's own documentation says
so plainly: "On Unix, this method always requires a system call. On Windows, it only
requires a system call if *follow_symlinks* is `True` and the entry is a reparse point
(for example, a symbolic link or directory junction)," and "The result is cached on the
`os.DirEntry` object, with a separate cache for *follow_symlinks* `True` and `False`.
Call `os.stat()` to fetch up-to-date information." The interpreter on this machine
repeats the short version in `os.DirEntry.stat.__doc__`: "Return stat_result object for
the entry; cached per entry."

On Windows the metadata arrives with the listing itself, from `FindFirstFile` /
`FindNextFile`, so `entry.stat(follow_symlinks=False)` never touches the disk and never
raises. A name deleted after the listing still answers with the metadata it had. On
POSIX the same call is a real `lstat`, which is why the walk noticed there and the test
was green.

The unit test understates the damage. With stale metadata the walk does not stop: the
entry still looks like a regular file, `_windows_entry` calls `add()`, and the seal is
built from `_safe_info`, which raises a bare `FileNotFoundError`. Only `CorpusChanged`
is retried (`_captured_after_retries`), so on Windows a file that vanishes mid-walk
aborts the whole capture with an unretryable error instead of being retried — the exact
outcome the class was written to prevent.

The POSIX branch of the same walker already does the right thing:
`_posix_entries` takes its own `os.stat(entry.name, dir_fd=descriptor,
follow_symlinks=False)` rather than trusting the listing. The Windows branch is the odd
one out.

### A row's path is compared as text, not as a path

`tests/test_small_corrections_in_the_generations_area.py::test_a_checkouts_row_is_headed_by_its_code_generation`
fails on Windows with `StopIteration`: no row's `checkout_root` equals
`str(repository)`.

The stored value is deliberately canonical. `repository_scope._local_serialized_path`
upper-cases the drive letter and rewrites every backslash as a forward slash, and
`repository_index._repository_row` copies that spelling into the row verbatim. So the row
holds `C:/Users/.../repo` while `str(WindowsPath(...))` is `C:\Users\...\repo`.

The product already carries the scar from this class, in a comment at
`scripts/repository_index.py`: on 2026-08-30 every repository on Windows was refused as
"not the checkout root", on all fifteen Windows shards, for exactly this reason, and the
fix was to compare as paths. Two sibling tests in `tests/test_repository_index.py`
already write `Path(row["checkout_root"]) == repository.resolve()`. This one test
repeats the mistake the product fixed.

## Decision

The corpus walk is the product's defect and is fixed in the product:
`_entry_info` takes the metadata itself, by path, exactly as the POSIX branch does. The
cost is one `lstat` per entry on Windows and nothing at all on POSIX, where
`entry.stat(follow_symlinks=False)` was already an `lstat`. A second benefit falls out:
`_require_safe_entry` reads `info.st_file_attributes` to see a reparse point, and it now
reads the attributes the name carries now, so a name swapped for a junction after the
listing is caught rather than trusted.

The checkout-row comparison is the test's defect and is fixed in the test, by the idiom
the product and its neighbours already use. A sweep of `tests/` for
`checkout_root` comparisons found this as the only remaining text comparison.

## Sources

- CPython documentation, `os.scandir` / `os.DirEntry.stat`, fetched 2026-09-18 from
  `raw.githubusercontent.com/python/cpython/3.12/Doc/library/os.rst`, quoted verbatim
  above.
- `os.DirEntry.stat.__doc__` on the interpreter this repository runs.
- The CI evidence: the test raises on the Linux and macOS jobs of run 35363057747 and
  does not raise on jobs 105658783925, 105658784016 and 105658784036.
- `scripts/repository_index.py`, the 2026-08-30 comment recording the same
  path-as-text failure across all fifteen Windows shards.
